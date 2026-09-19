"""CLI for grounded workloads, bounded load generation, and latency reports."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from . import load, scenarios as sc, sigv4, stats, targets, transport

EFFORT_CHOICES = ("none", "low", "medium", "high", "xhigh", "default")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def _csv(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _open_private(path: Path):
    """Create an owner-only artifact without following links or replacing files."""
    return open(path, "x", encoding="utf-8",
                opener=lambda name, flags: os.open(name, flags, 0o600))


class Bench:
    def __init__(self, args: argparse.Namespace, kind: str, *, event=None, stop=None) -> None:
        self.args = args
        self.event = event
        self.stop = stop if stop is not None else Event()
        self.region = args.region
        self.effort = None if args.reasoning_effort == "default" else args.reasoning_effort
        self.apis = _csv(args.apis)
        self.scenario_ids = _csv(args.scenarios)
        if not self.apis or any(api not in targets.API_PATHS for api in self.apis):
            raise ValueError(f"--apis must select from {','.join(targets.API_PATHS)}")
        if not self.scenario_ids or any(s not in sc.SCENARIOS for s in self.scenario_ids):
            raise ValueError(f"--scenarios must select from {','.join(sc.SCENARIOS)}")
        self.run_id = uuid.uuid4().hex
        self.order = self.scenario_ids.copy()
        random.Random(args.seed).shuffle(self.order)
        self.creds = sigv4.CredentialProvider()
        self.pool = transport.ConnectionPool(timeout=args.timeout)
        self.fresh = args.fresh_connection
        table = targets.build_targets(self.region, args.runtime_model)
        self.targets = []
        for target_id in _csv(args.targets):
            if target_id not in table:
                raise ValueError(f"unknown target {target_id!r}; choose from {','.join(table)}")
            target = table[target_id]
            if not args.dry_run:
                try:
                    targets.auth_headers(target, args.auth, b"{}", target.path(self.apis[0]), self.region, self.creds)
                except targets.TargetUnavailable as exc:
                    if self.event:
                        self.event("target_skipped", target=target_id, reason=str(exc))
                    else:
                        print(f"skip {exc}", file=sys.stderr)
                    continue
            self.targets.append(target)
        if not self.targets:
            raise ValueError("no available targets")
        self.out = Path(args.out or f"results/{kind}-{_utc_stamp()}.jsonl")
        self._fh = None
        if not args.dry_run:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self._fh = _open_private(self.out)
        self.rows: list[dict] = []

    def prepare(self, index: int):
        scenario_id = self.order[index % len(self.order)]
        scenario = sc.SCENARIOS[scenario_id]
        case_index = index // len(self.order)
        prepared = sc.prepare(scenario, case_index, self.args.cache_mode, uuid.uuid4().hex)
        return scenario, prepared

    def metadata(self, target, api, index, scenario, identity, due_epoch):
        return {
            "schema_version": 2,
            "run_id": self.run_id,
            "request_index": index,
            "fixture_id": identity[0],
            "workload_id": identity[1],
            "validation_scope": scenario.validation_scope,
            "target": target.id,
            "api": api,
            "model": target.model,
            "region": self.region,
            "scenario": scenario.id,
            "reasoning_effort": self.effort,
            "auth": targets.effective_auth(target, self.args.auth),
            "concurrency": self.args.concurrency,
            "round": index // len(self.order),
            "fresh_connection": self.fresh,
            "cache_mode": self.args.cache_mode,
            "max_output_tokens": self.args.max_output_tokens or scenario.max_output_tokens,
            "load_mode": self.args.load_mode,
            "workload_mix": ",".join(self.order),
            "arrival_rate": self.args.arrival_rate,
            "max_pending": self.args.max_pending,
            "timeout_s": self.args.timeout,
            "client_label": self.args.client_label,
            "seed": self.args.seed,
            "scheduled_ts": _iso(due_epoch),
        }

    def one_request(self, target, api, index, due, due_epoch):
        started = time.perf_counter()
        dispatch_ts = _iso(time.time())
        scenario, prepared = self.prepare(index)
        meta = self.metadata(target, api, index, scenario,
                             (prepared.fixture_id, prepared.workload_id), due_epoch)
        body_obj = targets.build_body(
            api, target.model, prepared.prompt, meta["max_output_tokens"], self.effort,
            output_schema=prepared.output_schema,
        )
        body = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        headers = targets.auth_headers(target, self.args.auth, body, target.path(api), self.region, self.creds)
        before_transport = time.perf_counter()
        result = transport.execute(target, api, body, headers, self.pool, self.fresh)
        output = result.pop("output_text", "")
        valid, validation_error = sc.validate_output(scenario, prepared, output)
        row = {
            **meta, **result,
            "valid_output": valid,
            "validation_error": validation_error,
            "dispatch_ts": dispatch_ts,
            "queue_ms": round(max(0.0, started - due) * 1000, 3),
            "client_total_ms": round((time.perf_counter() - due) * 1000, 3),
            "client_ttft_ms": None,
        }
        if result.get("ttft_ms") is not None:
            row["client_ttft_ms"] = round((before_transport - due) * 1000 + result["ttft_ms"], 3)
        return row

    def dropped(self, target, api, index, due, due_epoch):
        scenario = sc.SCENARIOS[self.order[index % len(self.order)]]
        identity = sc.identity(scenario, index // len(self.order))
        return {
            **self.metadata(target, api, index, scenario, identity, due_epoch),
            "ts": _iso(due_epoch), "dispatch_ts": None,
            "outcome": "dropped", "status": None, "error": "capacity_exceeded",
            "valid_output": False, "validation_error": None,
            "queue_ms": round(max(0.0, time.perf_counter() - due) * 1000, 3),
            "client_total_ms": round(max(0.0, time.perf_counter() - due) * 1000, 3),
            "client_ttft_ms": None,
        }

    def record(self, sample):
        self._fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.rows.append(sample)
        if self.event:
            self.event("sample", sample=sample)

    def warmup(self, executor, target, api):
        # Exercise the actual selected templates, not the synthetic short prompt.
        for _ in range(self.args.warmup):
            if self.stop.is_set():
                break
            futures = []
            error = None
            try:
                for index in range(max(self.args.concurrency, len(self.order))):
                    if self.stop.is_set():
                        break
                    now = time.perf_counter()
                    futures.append(executor.submit(self.one_request, target, api, index, now, time.time()))
            except BaseException as exc:
                error = exc
            # A failed worker or callback must not discard other admitted results.
            for future in futures:
                while True:
                    try:
                        row = future.result()
                        if self.event:
                            self.event("warmup_result", sample=row)
                        elif stats.is_error(row):
                            print(f"warmup {target.id}/{api}: {row.get('error') or row.get('validation_error')}", file=sys.stderr)
                        break
                    except BaseException as exc:
                        if error is None:
                            error = exc
                        if isinstance(exc, KeyboardInterrupt):
                            self.stop.set()
                        if future.done():
                            break
            if error is not None:
                raise error

    def run_cohort(self, executor, target, api):
        start_index = len(self.rows)
        load.drive(
            executor,
            lambda index, due, epoch: self.one_request(target, api, index, due, epoch),
            lambda index, due, epoch: self.dropped(target, api, index, due, epoch),
            self.record,
            concurrency=self.args.concurrency,
            mode=self.args.load_mode,
            count=None if self.args.duration else self.args.rounds * len(self.order),
            duration=self.args.duration,
            arrival_rate=self.args.arrival_rate,
            max_pending=self.args.max_pending,
            stop=self.stop,
        )
        summary = stats.summarize(self.rows[start_index:])
        if self.event:
            self.event("cohort_finished", target=target.id, api=api, summary=summary)
        else:
            print(f"{target.id}/{api}: {summary['n']} arrivals, {summary['errors']} unsuccessful; "
                  f"TTFT p50={stats.fmt_int(summary['ttft_ms']['p50'])}ms", flush=True)

    def preview(self):
        # No credentials, network, output file or inference needed to inspect cases.
        for target in self.targets:
            for api in self.apis:
                for index in range(len(self.order)):
                    scenario, prepared = self.prepare(index)
                    meta = self.metadata(target, api, index, scenario,
                                         (prepared.fixture_id, prepared.workload_id), time.time())
                    body = targets.build_body(api, target.model, prepared.prompt,
                                              meta["max_output_tokens"], self.effort,
                                              output_schema=prepared.output_schema)
                    print(json.dumps({"metadata": meta, "body": body}, ensure_ascii=False))

    def finish(self, bucket_s=0):
        self.pool.close_all()
        if self._fh is None:
            return
        self._fh.close()
        report = stats.render_report(self.rows, bucket_s)
        report_path = Path(str(self.out) + ".md")
        with _open_private(report_path) as output:
            output.write(report)
        if self.event:
            self.event("run_finished", run_id=self.run_id, samples=str(self.out),
                       report=str(report_path), summary=stats.summarize(self.rows),
                       interrupted=self.stop.is_set())
        else:
            print(report)
            print(f"\nsamples: {self.out} ({len(self.rows)} rows)\nreport: {report_path}", file=sys.stderr)


def cmd_run(args):
    if args.daemon:
        from . import daemon

        return daemon.run(args, _run_once)
    return _run_once(args)


def _run_once(args, *, event=None, stop=None):
    bench = Bench(args, "run", event=event, stop=stop)
    try:
        if args.dry_run:
            bench.preview()
            return 0
        if event:
            event("run_started", run_id=bench.run_id, samples=str(bench.out),
                  targets=[target.id for target in bench.targets],
                  apis=bench.apis, scenarios=bench.scenario_ids)
        cohort_targets = bench.targets.copy()
        random.Random(args.seed).shuffle(cohort_targets)
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            try:
                for target in cohort_targets:
                    for api in bench.apis:
                        if bench.stop.is_set():
                            break
                        if event:
                            event("cohort_started", target=target.id, api=api)
                        bench.warmup(executor, target, api)
                        if not bench.stop.is_set():
                            bench.run_cohort(executor, target, api)
            except KeyboardInterrupt:
                bench.stop.set()
                if not event:
                    print("\ninterrupted; draining admitted work and writing partial report", file=sys.stderr)
    finally:
        bench.finish(bucket_s=args.bucket)
    return 0


def cmd_probe(args):
    bench = Bench(args, "probe")
    try:
        if args.dry_run:
            bench.preview()
            return 0
        start, epoch = time.perf_counter(), time.time()
        tick = 0
        # A probe is serial and intentionally low-rate. Overruns are recorded,
        # not silently removed from the offered-work denominator.
        while start + tick * args.interval < start + args.duration:
            due = start + tick * args.interval
            time.sleep(max(0.0, due - time.perf_counter()))
            for target in bench.targets[tick % len(bench.targets):] + bench.targets[:tick % len(bench.targets)]:
                for api in bench.apis:
                    for offset in range(len(bench.order)):
                        index = tick * len(bench.order) + offset
                        if time.perf_counter() - due >= args.interval:
                            row = bench.dropped(target, api, index, due, epoch + tick * args.interval)
                        else:
                            row = bench.one_request(target, api, index, due, epoch + tick * args.interval)
                        bench.record(row)
            tick += 1
    except KeyboardInterrupt:
        print("\ninterrupted; writing partial probe report", file=sys.stderr)
    finally:
        bench.finish(bucket_s=args.bucket)
    return 0


def cmd_report(args):
    rows = []
    for path in args.files:
        with open(path, encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                location = f"{path}:{line_number}"
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    raise ValueError(f"{location}: invalid JSON sample") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{location}: sample must be a JSON object")
                for key in (*stats.CONFIG_KEYS, "target", "model", "run_id", "outcome"):
                    value = row.get(key)
                    if (value is not None and type(value) not in (str, int, float, bool)
                            or type(value) is float and not math.isfinite(value)):
                        raise ValueError(f"{location}: {key} must be a finite JSON scalar")
                if row.get("schema_version") is not None and type(row["schema_version"]) is not int:
                    raise ValueError(f"{location}: schema_version must be an integer")
                if row.get("valid_output") is not None and type(row["valid_output"]) is not bool:
                    raise ValueError(f"{location}: valid_output must be a boolean")
                rows.append(row)
    report = stats.render_report(rows, args.bucket)
    print(report)
    if args.markdown:
        with _open_private(Path(args.markdown)) as output:
            output.write(report)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="lunabench", description="Grounded workload latency benchmark")
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--targets", default="bedrock-runtime,bedrock-mantle,openai")
    common.add_argument("--apis", default="chat,responses")
    common.add_argument("--scenarios", default="extraction,answer,synthesis")
    common.add_argument("--reasoning-effort", choices=EFFORT_CHOICES, default="none",
                        help="explicit control; default omits the field (provider-selected effort)")
    common.add_argument("--cache-mode", choices=("cold", "warm"), default="warm")
    common.add_argument("--max-output-tokens", type=_positive_int)
    common.add_argument("--auth", choices=("sigv4", "bearer"), default="sigv4")
    common.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    common.add_argument("--runtime-model", default="global.openai.gpt-5.6-luna")
    common.add_argument("--fresh-connection", action="store_true")
    common.add_argument("--out", help="new JSONL path; existing files are never overwritten or appended")
    common.add_argument("--timeout", type=_positive, default=120.0, help="socket inactivity timeout, not whole-request deadline")
    common.add_argument("--client-label", default="unspecified", help="deployment/SDK/network-path identifier for grouping")
    common.add_argument("--seed", type=int, default=42)
    common.add_argument("--dry-run", action="store_true", help="print request payloads without authentication or inference")
    common.add_argument("--bucket", type=_nonnegative_int, default=0)

    run = sub.add_parser("run", parents=[common], help="bounded sustained, rate or burst load per target/API")
    length = run.add_mutually_exclusive_group()
    length.add_argument("--rounds", type=_positive_int, default=20, help="requests per scenario per target/API (not multiplied by concurrency)")
    length.add_argument("--duration", type=_positive, help="seconds of offered load per target/API instead of request count")
    run.add_argument("--concurrency", type=_positive_int, default=1)
    run.add_argument("--load-mode", choices=("sustained", "rate", "burst"), default="sustained")
    run.add_argument("--arrival-rate", type=_positive, help="requests/second across scenario mix per target/API; requires rate mode")
    run.add_argument("--max-pending", type=_nonnegative_int, default=0, help="bounded queue in addition to active concurrency in rate mode")
    run.add_argument("--warmup", type=_nonnegative_int, default=1)
    run.add_argument("--daemon", action="store_true", help="repeat the suite until stopped; live TUI on terminals")
    run.add_argument("--interval", type=_positive, default=600.0,
                     help="daemon start interval in seconds (default: 600); missed slots are skipped")
    run.add_argument("--no-tui", action="store_true", help="disable daemon TUI for service/redirected use")
    run.add_argument("--log-dir", help="new daemon artifact directory; must not exist (default: results/daemon-<UTC>)")
    run.set_defaults(fn=cmd_run)

    probe = sub.add_parser("probe", parents=[common], help="serial low-rate probe with explicit overrun drops")
    probe.add_argument("--interval", type=_positive, default=15.0)
    probe.add_argument("--duration", type=_positive, default=3600.0)
    probe.set_defaults(fn=cmd_probe, concurrency=1, load_mode="probe", arrival_rate=None, max_pending=0)

    report = sub.add_parser("report", help="render configuration-separated JSONL reports")
    report.add_argument("files", nargs="+")
    report.add_argument("--bucket", type=_nonnegative_int, default=0)
    report.add_argument("--markdown")
    report.set_defaults(fn=cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        if (args.load_mode == "rate") != (args.arrival_rate is not None):
            parser.error("--load-mode rate and --arrival-rate must be used together")
        if args.max_pending and args.load_mode != "rate":
            parser.error("--max-pending applies only to rate mode")
        if args.daemon and (args.out or args.dry_run):
            parser.error("--daemon cannot be combined with --out or --dry-run; use --log-dir for artifacts")
        if not args.daemon and (args.no_tui or args.log_dir or args.interval != 600.0):
            parser.error("--interval, --no-tui and --log-dir require --daemon")
    try:
        return args.fn(args)
    except (ValueError, RuntimeError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
