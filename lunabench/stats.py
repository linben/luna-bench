"""Configuration-safe aggregation and markdown rendering of latency samples."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape as escape_html
from itertools import combinations
from typing import Iterable

CONFIG_KEYS = (
    "api", "scenario", "workload_id", "workload_mix", "validation_scope", "reasoning_effort",
    "cache_mode", "max_output_tokens", "concurrency", "load_mode", "arrival_rate",
    "max_pending", "timeout_s", "region", "auth", "fresh_connection", "client_label",
    "schema_version", "seed",
)
# Workloads may share a load window. Other settings remain separate even here.
LOAD_CONFIG_KEYS = tuple(k for k in CONFIG_KEYS if k not in {"scenario", "workload_id", "validation_scope"})
LAT_KEYS = (
    "ttfb_ms", "ttft_ms", "t_last_text_ms", "t_done_ms", "t_eof_ms", "total_ms",
    "queue_ms", "client_ttft_ms", "client_total_ms",
)
_REPORT_CONTROLS = dict.fromkeys(
    [code for code in range(32) if code != 10] + list(range(127, 160)), " "
)


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; a small sample's p99 is not a reliable tail estimate."""
    if not sorted_values:
        return None
    idx = max(0, math.ceil(p / 100 * len(sorted_values)) - 1)
    return sorted_values[min(idx, len(sorted_values) - 1)]


def group(rows: Iterable[dict], keys: tuple[str, ...]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        out[tuple(row.get(k) for k in keys)].append(row)
    return dict(out)


def _schema(row: dict):
    return row.get("schema_version") if row.get("schema_version") is not None else 1


def is_error(row: dict) -> bool:
    transport_error = row.get("status") != 200 or bool(row.get("error"))
    if _schema(row) == 2:
        return transport_error or row.get("outcome") != "completed" or row.get("valid_output") is False
    return transport_error


def _valid(row: dict) -> bool:
    return _schema(row) == 2 and not is_error(row) and row.get("valid_output") is True


def _number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _values(rows: list[dict], key: str) -> list[float]:
    return [row[key] for row in rows if _number(row.get(key))]


def _distribution(values: list[float]) -> dict:
    values.sort()
    n = len(values)
    warnings = []
    if n == 0:
        warnings.append("no measured samples")
    else:
        if n < 20:
            warnings.append("p95 has fewer than 20 samples")
        if n < 100:
            warnings.append("p99 has fewer than 100 samples")
    return {
        "n": n,
        **{f"p{p}": percentile(values, p) for p in (50, 90, 95, 99)},
        "max": values[-1] if values else None,
        "warnings": warnings,
    }


def _median_or_none(rows: list[dict], key: str) -> float | None:
    values = sorted(_values(rows, key))
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    low, high = values[middle - 1], values[middle]
    return low + (high - low) / 2


def _stall(row: dict) -> tuple[bool, float | None, bool]:
    """Return measurement availability, maximum inter-text gap, stalled flag."""
    if _schema(row) == 2:
        maximum = row.get("max_stall_ms")
        maximum = maximum if _number(maximum) else None
        count = row.get("stall_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            # The count uses unrounded gaps; the reported maximum can round to 100ms.
            # A one-delta stream has zero intervals, not a zero-millisecond interval.
            return True, maximum, count > 0
        return maximum is not None, maximum, maximum is not None and maximum > 100
    gaps = row.get("itl_ms")
    if not isinstance(gaps, list):
        return False, None, False
    measured = [gap for gap in gaps if _number(gap)]
    maximum = max(measured) if measured else None
    return not gaps or bool(measured), maximum, maximum is not None and maximum > 100


def summarize(rows: list[dict]) -> dict:
    """Summarize one cell; use grouped_summaries to keep configurations separate.

    Legacy HTTP-success latency is retained as unvalidated historical data, never
    included in valid_n. V2 latency requires explicit successful output validation.
    """
    legacy = [row for row in rows if _schema(row) != 2]
    valid = [row for row in rows if _valid(row)]
    measured = [row for row in legacy if not is_error(row)] + valid
    errors = [row for row in rows if is_error(row)]
    outcomes = Counter(
        (row.get("outcome") or "missing_outcome") if _schema(row) == 2
        else ("legacy_failed" if is_error(row) else "legacy_http_success_unvalidated")
        for row in rows
    )
    statuses = Counter(str(row["status"]) for row in errors if row.get("status") not in (None, 200))
    stalls = [_stall(row) for row in measured]
    stall_n = sum(available for available, _, _ in stalls)
    stalled = sum(stalled for _, _, stalled in stalls)
    gaps = sorted(
        gap for row in measured if isinstance(row.get("itl_ms"), list)
        for gap in row["itl_ms"] if _number(gap)
    )
    warnings = []
    if legacy:
        warnings.append("legacy/unsupported schema: HTTP-success latency is unvalidated, not v2 success")
    if not measured:
        warnings.append("no eligible latency samples")
    elif len(measured) < 100:
        warnings.append(f"only {len(measured)} eligible samples; p99 needs at least 100, p95 at least 20")
    unvalidated = len(legacy) + sum(
        _schema(row) == 2 and row.get("outcome") == "completed" and row.get("valid_output") is None
        for row in rows
    )
    if unvalidated > len(legacy):
        warnings.append("completed v2 rows missing validation are excluded from latency and valid counts")
    connect = sorted(_values(measured, "t_connect_ms"))
    tls = sorted(_values(measured, "t_tls_ms"))
    summary = {
        "n": len(rows),
        "valid_n": len(valid),
        "latency_n": len(measured),
        "legacy_n": len(legacy),
        "unvalidated_n": unvalidated,
        "errors": len(errors),
        "err_rate": len(errors) / len(rows) if rows else 0.0,
        "drops": sum(_schema(row) == 2 and row.get("outcome") == "dropped" for row in rows),
        "validation_failures": sum(
            _schema(row) == 2 and row.get("outcome") == "completed" and row.get("valid_output") is False
            for row in rows
        ),
        "outcomes": dict(outcomes),
        "statuses": dict(statuses),
        "finish_reasons": dict(Counter(str(row["finish_reason"]) for row in rows if row.get("finish_reason") is not None)),
        "reconnects": sum(bool(row.get("reconnected")) for row in rows),
        "max_stall_ms": _distribution([maximum for _, maximum, _ in stalls if maximum is not None]),
        "stall_measured_n": stall_n,
        "stalled_n": stalled,
        "stall_fraction": stalled / stall_n if stall_n else None,
        # Text-event statistics are descriptive, never a token-generation rate.
        "itl_p50_ms": percentile(gaps, 50),
        "itl_p95_ms": percentile(gaps, 95),
        "text_delivery_chars_per_s": _distribution(_values(valid, "text_delivery_chars_per_s")),
        "prompt_tokens_med": _median_or_none(measured, "prompt_tokens"),
        "completion_tokens_med": _median_or_none(measured, "completion_tokens"),
        "reasoning_tokens_med": _median_or_none(measured, "reasoning_tokens"),
        "cached_tokens_med": _median_or_none(measured, "cached_tokens"),
        "cached_hits": sum(_number(row.get("cached_tokens")) and row["cached_tokens"] > 0 for row in measured),
        "t_connect_p50_ms": percentile(connect, 50),
        "t_tls_p50_ms": percentile(tls, 50),
        "n_connect": len(connect),
        "n_tls": len(tls),
        "warnings": warnings,
    }
    for key in LAT_KEYS:
        summary[key] = _distribution(_values(measured, key))
    for key, start, end in (
        ("text_to_done_ms", "t_last_text_ms", "t_done_ms"),
        ("done_to_eof_ms", "t_done_ms", "t_eof_ms"),
    ):
        summary[key] = _distribution([
            row[end] - row[start] for row in valid
            if _number(row.get(start)) and _number(row.get(end)) and row[end] >= row[start]
        ])
    return summary


def _epoch(ts: str) -> float:
    value = datetime.fromisoformat(ts)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def bucket_start(ts: str, bucket_s: int) -> int:
    if bucket_s <= 0:
        raise ValueError("bucket_s must be positive")
    return int(math.floor(_epoch(ts) / bucket_s) * bucket_s)


def _timestamp(row: dict):
    return row.get("scheduled_ts") or row.get("ts")


def _config(row: dict, keys: tuple[str, ...] = CONFIG_KEYS) -> tuple:
    return tuple(_schema(row) if key == "schema_version" else row.get(key) for key in keys)


def grouped_summaries(rows: list[dict], bucket_s: int = 0) -> list[dict]:
    """The grouping contract shared by summary, comparisons, and time buckets."""
    cells = defaultdict(list)
    for row in rows:
        bucket = None
        if bucket_s > 0:
            try:
                bucket = bucket_start(_timestamp(row), bucket_s)
            except (TypeError, ValueError, OverflowError):
                continue
        key = (_config(row), row.get("target"), row.get("model"), bucket)
        cells[key].append(row)
    return [
        {"config": dict(zip(CONFIG_KEYS, config)), "target": target, "model": model,
         "bucket": bucket, "summary": summarize(samples)}
        for (config, target, model, bucket), samples in sorted(cells.items(), key=lambda item: repr(item[0]))
    ]


def comparison_ratios(rows: list[dict]) -> list[dict]:
    """Explicit model pairs across targets, only under identical configurations."""
    return _comparison_ratios(grouped_summaries(rows))


def _comparison_ratios(summaries: list[dict]) -> list[dict]:
    cells = defaultdict(list)
    for cell in summaries:
        cells[tuple(cell["config"].values())].append(cell)
    comparisons = []
    for members in cells.values():
        for denominator, numerator in combinations(members, 2):
            if denominator["target"] == numerator["target"]:
                continue
            ratios = {}
            for metric in ("ttft_ms", "total_ms", "client_ttft_ms", "client_total_ms"):
                a = numerator["summary"][metric]["p50"]
                b = denominator["summary"][metric]["p50"]
                ratios[metric] = a / b if a is not None and b is not None and b > 0 else None
            comparisons.append({
                "config": denominator["config"],
                "numerator": (numerator["target"], numerator["model"]),
                "denominator": (denominator["target"], denominator["model"]),
                "numerator_n": numerator["summary"]["latency_n"],
                "denominator_n": denominator["summary"]["latency_n"],
                "ratios": ratios,
            })
    return comparisons


def load_summaries(rows: list[dict]) -> list[dict]:
    """Observed per-run target/API throughput, not reciprocal request latency.

    All workload/configuration subgroups share their parent cohort's complete
    scheduled-arrival-to-client-finish window. A subgroup's RPS is its contribution
    during that window, not its own active-phase rate. Incomplete timing disables
    rates, since computing a shorter window would overstate throughput.
    """
    cohorts = group([row for row in rows if _schema(row) == 2 and row.get("run_id")], ("run_id", "target", "api"))
    result = []
    for (run_id, target, api), cohort in sorted(cohorts.items(), key=lambda item: repr(item[0])):
        starts, finishes = [], []
        for row in cohort:
            try:
                start = _epoch(row.get("scheduled_ts"))
            except (TypeError, ValueError, OverflowError):
                continue
            elapsed = row.get("client_total_ms")
            if not _number(elapsed) or not math.isfinite(start):
                continue
            starts.append(start)
            finishes.append(start + elapsed / 1000)
        window = max(finishes) - min(starts) if len(starts) == len(cohort) and starts else None
        if window is not None and window <= 0:
            window = None
        cells = defaultdict(list)
        for row in cohort:
            cells[(_config(row, LOAD_CONFIG_KEYS), row.get("model"))].append(row)
        for (config, model), samples in sorted(cells.items(), key=lambda item: repr(item[0])):
            valid_n = sum(_valid(row) for row in samples)
            errors = sum(is_error(row) for row in samples)
            drops = sum(row.get("outcome") == "dropped" for row in samples)
            workloads = sorted({(row.get("scenario"), row.get("workload_id"), row.get("validation_scope")) for row in samples}, key=repr)
            result.append({
                "run_id": run_id, "target": target, "api": api, "model": model,
                "config": dict(zip(LOAD_CONFIG_KEYS, config)), "workloads": workloads,
                "cohort_n": len(cohort), "timed_n": len(starts), "window_s": window,
                "n": len(samples), "valid_n": valid_n,
                "errors": errors, "drops": drops,
                "arrival_rps": len(samples) / window if window else None,
                "valid_rps": valid_n / window if window else None,
                "drop_rps": drops / window if window else None,
            })
    return result


def fmt_int(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _tri(distribution: dict, ps: tuple[str, ...] = ("p50", "p95", "p99", "max")) -> str:
    return "/".join(fmt_int(distribution.get(key)) for key in ps)


def _latency(distribution: dict) -> str:
    return f"{_tri(distribution)} (n={distribution['n']})"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    def escape(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["| " + " | ".join(escape(value) for value in headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(escape(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _counts(counts: dict) -> str:
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "-"


def _label(target, model) -> str:
    return " ".join(f"{target} / model={model}".splitlines())


def render_report(rows: list[dict], bucket_s: int = 0) -> str:
    out = ["# Luna latency report", ""]
    if not rows:
        return "\n".join(out + ["_no samples_"])
    cells = grouped_summaries(rows)
    config_ids = {}
    for cell in cells:
        key = tuple(cell["config"].values())
        if key not in config_ids:
            config_ids[key] = f"C{len(config_ids) + 1}"
    by_config = defaultdict(list)
    for cell in cells:
        by_config[tuple(cell["config"].values())].append(cell)

    def config_id(config: dict) -> str:
        return config_ids[tuple(config.values())]

    legacy_n = sum(_schema(row) != 2 for row in rows)
    out += [f"- recorded arrivals: {len(rows)}", f"- validated successful requests: {sum(_valid(row) for row in rows)}", ""]
    if legacy_n:
        out += [f"**UNVALIDATED LEGACY/UNSUPPORTED DATA: {legacy_n} rows.** HTTP-success latency is shown separately, not treated as validated v2 success. Missing legacy configuration fields are unknown, not inferred.", ""]
    unset_clients = sum(row.get("client_label") in (None, "", "unspecified") for row in rows)
    if unset_clients:
        out += [f"**Client provenance warning:** {unset_clients} rows have no specific client_label. Machine/network differences cannot be separated for these rows.", ""]
    out += ["## Configurations", "", "Every configuration below is isolated in summaries, comparisons, and time buckets. Models remain separate within each target.", ""]
    for config_key, cid in config_ids.items():
        config = by_config[config_key][0]["config"]
        out += [f"### {cid}", "", _table(["setting", "value"], [[key, str(value) if value is not None else "unknown"] for key, value in config.items()]), ""]

    out += ["## Summary", "", "Latency cells: p50/p95/p99/max in ms (measured n). V2 latency includes only completed, validated HTTP successes. Legacy cells contain unvalidated HTTP successes only.", "", "Tail warning: fewer than 20 measured samples cannot support p95; fewer than 100 cannot support p99. These are minimum counts, not confidence guarantees. Missing measurements remain missing.", ""]
    for config_key, cid in config_ids.items():
        members = by_config[config_key]
        out += [f"### {cid}", ""]
        table = []
        for cell in members:
            summary = cell["summary"]
            table.append([
                _label(cell["target"], cell["model"]), str(summary["n"]), str(summary["valid_n"]),
                str(summary["unvalidated_n"]), f"{summary['errors']} ({100 * summary['err_rate']:.1f}%)",
                str(summary["drops"]), str(summary["validation_failures"]), _counts(summary["outcomes"]),
                _counts(summary["statuses"]), _counts(summary["finish_reasons"]),
            ])
        out += [_table(["target / model", "n", "valid", "unvalidated", "errors incl. drops", "drops", "completed but invalid", "outcomes", "HTTP errors", "finish reasons"], table), ""]
        table = []
        for cell in members:
            summary = cell["summary"]
            table.append([_label(cell["target"], cell["model"])] + [_latency(summary[key]) for key in ("ttfb_ms", "ttft_ms", "t_last_text_ms", "t_done_ms", "t_eof_ms", "total_ms")])
        out += [_table(["target / model", "TTFB", "TTFT", "last text", "completion signal", "actual EOF", "transport total"], table), ""]
        table = []
        for cell in members:
            summary = cell["summary"]
            fraction = summary["stall_fraction"]
            stall_cell = "-" if fraction is None else f"{100 * fraction:.1f}% ({summary['stalled_n']}/{summary['stall_measured_n']})"
            table.append([
                _label(cell["target"], cell["model"]), *[_latency(summary[key]) for key in ("queue_ms", "client_ttft_ms", "client_total_ms", "max_stall_ms")],
                stall_cell, _latency(summary["text_to_done_ms"]), _latency(summary["done_to_eof_ms"]),
            ])
        out += [_table(["target / model", "queue", "client TTFT", "client total", "per-request max text gap", "requests stalled >100ms", "last text to completion", "completion to EOF"], table), ""]
        table = []
        for cell in members:
            summary = cell["summary"]
            table.append([
                _label(cell["target"], cell["model"]),
                "/".join(fmt_int(summary[key]) for key in ("prompt_tokens_med", "completion_tokens_med", "reasoning_tokens_med")),
                str(summary["cached_hits"]), fmt_int(summary["cached_tokens_med"]), str(summary["reconnects"]),
                f"{fmt_int(summary['t_connect_p50_ms'])} (n={summary['n_connect']})",
                f"{fmt_int(summary['t_tls_p50_ms'])} (n={summary['n_tls']})",
            ])
        out += [_table(["target / model", "prompt/completion/reasoning tokens (med)", "cached>0", "cached tokens (med)", "reconnects (all arrivals)", "connect p50 ms", "TLS p50 ms"], table), ""]
        for cell in members:
            summary = cell["summary"]
            if summary["warnings"]:
                out.append(f"- {_label(cell['target'], cell['model'])}: {'; '.join(summary['warnings'])}.")
        out.append("")

    out += ["## Relative p50 ratios", "", "Numerator / denominator; model identities are explicit. Only identical configurations are compared. Tail/sample warnings still apply.", ""]
    comparisons = []
    for comparison in _comparison_ratios(cells):
        comparisons.append([
            config_id(comparison["config"]), _label(*comparison["numerator"]), _label(*comparison["denominator"]),
            f"{comparison['numerator_n']}/{comparison['denominator_n']}",
            *["-" if value is None else f"{value:.2f}x" for value in comparison["ratios"].values()],
        ])
    out += [_table(["config", "numerator", "denominator", "eligible n (num/den)", "TTFT", "transport total", "client TTFT", "client total"], comparisons) if comparisons else "No configuration-matched cross-target pairs.", ""]

    out += ["## Observed load throughput", "", "Each run/target/API cohort uses min(scheduled arrival) through max(scheduled arrival + client total), including drops and failures. Mixed workloads share that complete window. Separate load-configuration/model rows show contributions to that cohort's RPS, not active-phase rates. Runs are never concatenated or timed by summing request durations. Rates require complete client timing for the whole cohort; legacy rates are unavailable.", ""]
    loads = load_summaries(rows)
    for index, load in enumerate(loads, 1):
        run_id = " ".join(str(load["run_id"]).splitlines())
        out += [f"### Load {index}: run={run_id} / {_label(load['target'], load['model'])}", ""]
        out += [_table(["setting", "value"], [[key, str(value) if value is not None else "unknown"] for key, value in load["config"].items()]), ""]
        out += [_table(["scenario", "workload_id", "validation_scope"], [[str(value) for value in workload] for workload in load["workloads"]]), ""]
        rates = [load[key] for key in ("arrival_rps", "valid_rps", "drop_rps")]
        out += [_table(
            ["arrivals", "valid", "errors incl. drops", "drops", "cohort timed n/n", "window s", "arrival/s", "valid completion/s", "drop/s"],
            [[str(load["n"]), str(load["valid_n"]), str(load["errors"]), str(load["drops"]), f"{load['timed_n']}/{load['cohort_n']}",
              "-" if load["window_s"] is None else f"{load['window_s']:.3f}", *["-" if rate is None else f"{rate:.3f}" for rate in rates]]],
        ), ""]
    if not loads:
        out += ["No v2 run cohorts with identifiers are available.", ""]

    if bucket_s > 0:
        out += [f"## Time buckets ({bucket_s}s)", "", "Buckets use scheduled arrival (legacy: recorded ts), retain every configuration and model boundary, and display a full UTC date. Rows without usable timestamps are omitted from buckets only.", ""]
        table = []
        for cell in grouped_summaries(rows, bucket_s):
            summary = cell["summary"]
            table.append([
                config_id(cell["config"]), datetime.fromtimestamp(cell["bucket"], tz=timezone.utc).isoformat(),
                _label(cell["target"], cell["model"]), str(summary["n"]), str(summary["valid_n"]),
                str(summary["errors"]), str(summary["drops"]), _latency(summary["ttft_ms"]),
                _latency(summary["client_total_ms"]), _latency(summary["max_stall_ms"]),
            ])
        out += [_table(["config", "bucket UTC", "target / model", "n", "valid", "errors", "drops", "TTFT", "client total", "max text gap"], table), ""]

    out += [
        "## Metric definitions", "",
        "- V2 errors include non-200 HTTP status, transport/protocol error, non-completed outcomes (including empty, incomplete, refused and dropped), and failed validation. A completed row missing explicit validation is unvalidated and has no eligible latency.",
        "- Validation means the recorded validation_scope only; structural/schema or citation-reference checks do not imply factual verification.",
        "- Transport clocks start once per logical request, including connection setup and any reconnect. TTFB is status/headers; TTFT and last text are the first/last non-empty text events. Completion is the protocol terminal signal, actual EOF is transport EOF, and transport total is return/failure after drain. They are not interchangeable.",
        "- Queue is scheduled arrival to worker start. Client TTFT includes queue, preparation and transport TTFT; client total includes queue through output validation. Latency distributions exclude v2 failures and drops; their counts remain in the arrival/error totals and throughput window.",
        "- Stall maxima are per-request inter-text-event gaps, not pooled gaps or token latencies. The fraction counts requests with a gap strictly greater than 100ms among requests with known stall measurements, using the unrounded stall_count when recorded rather than recomputing from rounded maxima. Single-delta streams have no gap maximum but count as not stalled when stall_count is recorded as zero. TTFT and trailing completion/EOF silence are shown separately, not included in this inter-text stall metric.",
        "- Nearest-rank percentiles report observed order statistics, not tail confidence. Each timing cell reports its own measured count; absent/non-finite measurements are excluded rather than replaced by zero. No token-generation throughput is inferred from chunks or usage tokens.",
        "- Token/cache medians and connect/TLS timings use the same eligible requests as latency. Provider usage is retained as reported: completion tokens may include reasoning, and missing usage is not zero. cached>0 counts eligible requests with reported cache hits. Reconnects count all recorded arrivals, including failures. Connect is DNS+TCP; TLS is handshake time, only when measured.",
        "",
    ]
    return escape_html("\n".join(out).translate(_REPORT_CONTROLS), quote=False)
