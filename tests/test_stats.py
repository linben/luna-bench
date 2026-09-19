import math
import unittest

from lunabench import stats


def row(**changes):
    base = {
        "schema_version": 2,
        "run_id": "run-1",
        "ts": "2026-01-01T12:00:00.000+00:00",
        "scheduled_ts": "2026-01-01T12:00:00.000+00:00",
        "target": "bedrock-runtime",
        "api": "chat",
        "model": "model-a",
        "scenario": "answer",
        "workload_id": "grounded-v1",
        "workload_mix": "answer,synthesis",
        "validation_scope": "structure-and-citation-references",
        "reasoning_effort": "none",
        "cache_mode": "cold",
        "max_output_tokens": 1024,
        "concurrency": 1,
        "load_mode": "sustained",
        "arrival_rate": None,
        "max_pending": 4,
        "timeout_s": 30.0,
        "region": "us-east-1",
        "auth": "sigv4",
        "fresh_connection": False,
        "client_label": "test-client",
        "seed": 1,
        "status": 200,
        "error": None,
        "outcome": "completed",
        "valid_output": True,
        "validation_error": None,
        "finish_reason": "stop",
        "ttfb_ms": 100.0,
        "ttft_ms": 200.0,
        "t_last_text_ms": 230.0,
        "t_done_ms": 250.0,
        "t_eof_ms": 290.0,
        "total_ms": 300.0,
        "queue_ms": 20.0,
        "client_ttft_ms": 230.0,
        "client_total_ms": 330.0,
        "itl_ms": [10.0, 20.0],
        "max_stall_ms": 20.0,
        "stall_count": 0,
    }
    base.update(changes)
    return base


class Percentile(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(stats.percentile([1], 50), 1)
        self.assertEqual(stats.percentile(list(range(1, 101)), 99), 99)
        self.assertEqual(stats.percentile(list(range(1, 101)), 50), 50)
        self.assertIsNone(stats.percentile([], 50))


class Buckets(unittest.TestCase):
    def test_bucket_boundaries(self):
        start = stats.bucket_start("2026-01-01T12:00:00+00:00", 300)
        self.assertEqual(stats.bucket_start("2026-01-01T12:04:59+00:00", 300), start)
        self.assertEqual(stats.bucket_start("2026-01-01T12:05:00+00:00", 300), start + 300)


class Summarize(unittest.TestCase):
    def test_error_row_excluded_from_latency(self):
        samples = [
            row(ttft_ms=200.0, total_ms=300.0),
            row(ttft_ms=400.0, total_ms=600.0),
            row(status=429, outcome="failed", valid_output=False, error="throttled", ttft_ms=None, total_ms=9999.0),
        ]
        summary = stats.summarize(samples)
        self.assertEqual((summary["n"], summary["valid_n"], summary["errors"]), (3, 2, 1))
        self.assertEqual(summary["statuses"], {"429": 1})
        self.assertEqual(summary["total_ms"]["p50"], 300.0)
        self.assertEqual(summary["total_ms"]["p99"], 600.0)
        self.assertEqual(summary["ttft_ms"]["p50"], 200.0)

    def test_stream_error_with_200_is_error(self):
        summary = stats.summarize([row(error="ConnectionResetError(104)")])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["statuses"], {})
        self.assertIsNone(summary["ttft_ms"]["p50"])

    def test_terminal_and_validation_failures_are_not_successful_latency(self):
        samples = [row(total_ms=500.0)] + [
            row(outcome=outcome, valid_output=False, ttft_ms=1.0, total_ms=2.0)
            for outcome in ("empty", "incomplete", "refused", "failed")
        ] + [
            row(valid_output=False, validation_error="missing required citation", total_ms=3.0),
            row(outcome="dropped", status=None, error="capacity_exceeded", valid_output=False, total_ms=None),
        ]
        summary = stats.summarize(samples)
        self.assertEqual(summary["errors"], 6)
        self.assertEqual(summary["valid_n"], 1)
        self.assertEqual(summary["validation_failures"], 1)
        self.assertEqual(summary["drops"], 1)
        self.assertEqual(summary["outcomes"]["empty"], 1)
        self.assertEqual(summary["outcomes"]["incomplete"], 1)
        self.assertEqual(summary["total_ms"]["p50"], 500.0)
        self.assertEqual(summary["total_ms"]["n"], 1)

    def test_missing_validation_never_becomes_valid_sample(self):
        summary = stats.summarize([row(valid_output=None)])
        self.assertEqual(summary["valid_n"], 0)
        self.assertEqual(summary["unvalidated_n"], 1)
        self.assertIsNone(summary["total_ms"]["p50"])
        self.assertTrue(summary["warnings"])

    def test_single_request_stall_is_not_hidden_by_many_fast_events(self):
        summary = stats.summarize([
            row(itl_ms=[1.0] * 1000, max_stall_ms=1.0, stall_count=0),
            row(itl_ms=[500.0], max_stall_ms=500.0, stall_count=1),
        ])
        self.assertEqual(summary["itl_p95_ms"], 1.0)
        self.assertEqual(summary["max_stall_ms"]["p95"], 500.0)
        self.assertEqual(summary["max_stall_ms"]["max"], 500.0)
        self.assertEqual(summary["stall_fraction"], 0.5)

    def test_stall_boundary_and_single_delta_denominators(self):
        summary = stats.summarize([
            row(itl_ms=[100.0], max_stall_ms=100.0, stall_count=0),
            row(itl_ms=[], max_stall_ms=None, stall_count=0),
            row(itl_ms=[101.0], max_stall_ms=101.0, stall_count=1),
            row(itl_ms=[], max_stall_ms=None, stall_count=None),
            row(outcome="incomplete", valid_output=False, max_stall_ms=5000.0, stall_count=1),
        ])
        self.assertEqual(summary["max_stall_ms"]["n"], 2)
        self.assertEqual(summary["stall_measured_n"], 3)
        self.assertEqual(summary["stalled_n"], 1)
        self.assertAlmostEqual(summary["stall_fraction"], 1 / 3)

    def test_unrounded_stall_count_survives_rounded_maximum(self):
        summary = stats.summarize([
            row(itl_ms=[100.0], max_stall_ms=100.0, stall_count=0),
            row(itl_ms=[100.0], max_stall_ms=100.0, stall_count=1),
        ])
        self.assertEqual(summary["max_stall_ms"]["p50"], 100.0)
        self.assertEqual(summary["stall_measured_n"], 2)
        self.assertEqual(summary["stalled_n"], 1)
        self.assertEqual(summary["stall_fraction"], 0.5)

    def test_malformed_measurements_do_not_crash_or_invent_stall_measurements(self):
        summary = stats.summarize([
            row(total_ms=10 ** 400, itl_ms=10, max_stall_ms=None, stall_count=None),
            row(total_ms=50.0, itl_ms=[40.0], max_stall_ms=40.0, stall_count=0),
        ])
        self.assertEqual(summary["total_ms"]["n"], 1)
        self.assertEqual(summary["total_ms"]["p50"], 50.0)
        self.assertEqual(summary["itl_p95_ms"], 40.0)
        self.assertEqual(summary["stall_measured_n"], 1)

    def test_invalid_stall_counts_and_legacy_gaps_stay_unmeasured(self):
        summary = stats.summarize([
            row(max_stall_ms=None, stall_count=0.5),
            row(max_stall_ms=None, stall_count=True),
            row(schema_version=1, itl_ms=[None, math.nan, "missing"]),
        ])
        self.assertEqual(summary["stall_measured_n"], 0)
        self.assertEqual(summary["stalled_n"], 0)
        self.assertIsNone(summary["stall_fraction"])

    def test_safe_tails_use_finite_measured_count_and_warn(self):
        summary = stats.summarize([
            row(total_ms=40.0), row(total_ms=80.0), row(total_ms=None),
            row(total_ms=math.nan), row(total_ms=math.inf), row(total_ms=-1.0),
        ])
        self.assertEqual(summary["total_ms"]["n"], 2)
        self.assertEqual(summary["total_ms"]["p95"], 80.0)
        self.assertEqual(summary["total_ms"]["p99"], 80.0)
        self.assertEqual(summary["total_ms"]["max"], 80.0)
        self.assertTrue(summary["total_ms"]["warnings"])
        empty = stats.summarize([])["total_ms"]
        self.assertEqual(empty["n"], 0)
        self.assertIsNone(empty["p99"])
        self.assertIsNone(empty["max"])

    def test_large_finite_token_median_stays_finite(self):
        summary = stats.summarize([
            row(prompt_tokens=1e308), row(prompt_tokens=1e308),
        ])
        self.assertEqual(summary["prompt_tokens_med"], 1e308)

    def test_terminal_delays_remain_distinct_from_text_stalls(self):
        summary = stats.summarize([row(t_last_text_ms=230.0, t_done_ms=700.0, t_eof_ms=1700.0, total_ms=1800.0)])
        self.assertEqual(summary["text_to_done_ms"]["p50"], 470.0)
        self.assertEqual(summary["done_to_eof_ms"]["p50"], 1000.0)
        self.assertEqual(summary["total_ms"]["p50"], 1800.0)
        self.assertEqual(summary["max_stall_ms"]["p50"], 20.0)


class Grouping(unittest.TestCase):
    def test_configuration_boundaries_apply_to_summaries_ratios_and_buckets(self):
        # Every setting can independently invalidate a cross-target comparison.
        changes = {
            "api": "responses", "scenario": "extraction", "workload_id": "grounded-v2",
            "workload_mix": "answer",
            "validation_scope": "schema-only", "reasoning_effort": "low", "cache_mode": "warm",
            "max_output_tokens": 512, "concurrency": 2, "load_mode": "rate", "arrival_rate": 5.0,
            "max_pending": 8, "timeout_s": 60.0, "region": "us-west-2", "auth": "bearer",
            "fresh_connection": True, "client_label": "different-machine", "schema_version": 1, "seed": 2,
        }
        for key, value in changes.items():
            with self.subTest(setting=key):
                samples = [row(), row(target="openai", ttft_ms=2000.0, **{key: value})]
                summaries = stats.grouped_summaries(samples)
                self.assertEqual(len(summaries), 2)
                self.assertEqual(len({tuple(cell["config"].items()) for cell in summaries}), 2)
                self.assertEqual(stats.comparison_ratios(samples), [])
                buckets = stats.grouped_summaries(samples, bucket_s=300)
                self.assertEqual(len({tuple(cell["config"].items()) for cell in buckets}), 2)

    def test_models_stay_separate_and_comparisons_name_both(self):
        samples = [
            row(model="model-a", ttft_ms=100.0),
            row(model="model-b", ttft_ms=400.0),
            row(target="openai", model="model-c", ttft_ms=800.0),
        ]
        summaries = stats.grouped_summaries(samples)
        self.assertEqual(len(summaries), 3)
        self.assertEqual({cell["summary"]["ttft_ms"]["p50"] for cell in summaries}, {100.0, 400.0, 800.0})
        comparisons = stats.comparison_ratios(samples)
        self.assertEqual(len(comparisons), 2)
        self.assertEqual({comparison["denominator"][1] for comparison in comparisons}, {"model-a", "model-b"})
        self.assertEqual({comparison["numerator"][1] for comparison in comparisons}, {"model-c"})
        self.assertEqual({comparison["ratios"]["ttft_ms"] for comparison in comparisons}, {2.0, 8.0})

    def test_run_id_does_not_fragment_statistical_configuration(self):
        summaries = stats.grouped_summaries([row(run_id="first"), row(run_id="second")])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["summary"]["valid_n"], 2)

    def test_legacy_is_separate_unvalidated_data(self):
        legacy = row(ttft_ms=5.0)
        for key in ("schema_version", "outcome", "valid_output", "run_id", "scheduled_ts"):
            legacy.pop(key)
        legacy["output_tps"] = 1000.0
        samples = [legacy, row()]
        cells = stats.grouped_summaries(samples)
        historical = next(cell["summary"] for cell in cells if cell["config"]["schema_version"] == 1)
        current = next(cell["summary"] for cell in cells if cell["config"]["schema_version"] == 2)
        self.assertEqual(historical["valid_n"], 0)
        self.assertEqual(historical["unvalidated_n"], 1)
        self.assertEqual(historical["ttft_ms"]["p50"], 5.0)
        self.assertEqual(current["ttft_ms"]["p50"], 200.0)
        self.assertEqual(historical["outcomes"], {"legacy_http_success_unvalidated": 1})
        self.assertTrue(historical["warnings"])
        self.assertEqual(stats.load_summaries([legacy]), [])

    def test_buckets_use_scheduled_arrival_not_dispatch_and_keep_date(self):
        cells = stats.grouped_summaries([
            row(scheduled_ts="2026-01-01T12:04:59+00:00", ts="2026-01-01T12:05:10+00:00"),
            row(scheduled_ts="2026-01-02T12:04:59+00:00"),
        ], bucket_s=300)
        self.assertEqual(len(cells), 2)
        self.assertEqual({cell["bucket"] for cell in cells}, {
            stats.bucket_start("2026-01-01T12:00:00+00:00", 300),
            stats.bucket_start("2026-01-02T12:00:00+00:00", 300),
        })


class Throughput(unittest.TestCase):
    def test_mixed_workloads_share_actual_cohort_window(self):
        loads = stats.load_summaries([
            row(scenario="answer", scheduled_ts="2026-01-01T12:00:00+00:00", client_total_ms=1000.0),
            row(scenario="synthesis", workload_id="synthesis-v1", scheduled_ts="2026-01-01T12:00:09+00:00", client_total_ms=1000.0),
        ])
        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0]["window_s"], 10.0)
        self.assertEqual(loads[0]["valid_rps"], 0.2)
        self.assertEqual(len(loads[0]["workloads"]), 2)

    def test_config_and_model_contributions_use_entire_parent_window(self):
        loads = stats.load_summaries([
            row(client_total_ms=1000.0),
            row(cache_mode="warm", model="other", scheduled_ts="2026-01-01T12:00:09+00:00", client_total_ms=1000.0),
        ])
        self.assertEqual(len(loads), 2)
        self.assertEqual({load["window_s"] for load in loads}, {10.0})
        self.assertEqual({load["valid_rps"] for load in loads}, {0.1})
        self.assertEqual(sum(load["valid_rps"] for load in loads), 0.2)

    def test_drops_extend_observation_without_becoming_completions(self):
        load = stats.load_summaries([
            row(client_total_ms=1000.0),
            row(outcome="dropped", status=None, valid_output=False, error="capacity_exceeded",
                scheduled_ts="2026-01-01T12:00:10+00:00", client_total_ms=0.0),
        ])[0]
        self.assertEqual((load["valid_n"], load["drops"], load["errors"]), (1, 1, 1))
        self.assertEqual(load["window_s"], 10.0)
        self.assertEqual(load["arrival_rps"], 0.2)
        self.assertEqual(load["valid_rps"], 0.1)
        self.assertEqual(load["drop_rps"], 0.1)

    def test_runs_have_separate_windows_and_missing_timing_disables_rates(self):
        loads = stats.load_summaries([
            row(run_id="first", client_total_ms=1000.0),
            row(run_id="second", scheduled_ts="2026-01-03T12:00:00+00:00", client_total_ms=2000.0),
        ])
        self.assertEqual({load["valid_rps"] for load in loads}, {1.0, 0.5})
        incomplete = stats.load_summaries([row(client_total_ms=1000.0), row(client_total_ms=None)])[0]
        self.assertEqual(incomplete["timed_n"], 1)
        self.assertEqual(incomplete["cohort_n"], 2)
        self.assertIsNone(incomplete["window_s"])
        self.assertIsNone(incomplete["valid_rps"])


class ReportSafety(unittest.TestCase):
    def test_untrusted_labels_cannot_emit_controls_or_raw_html(self):
        report = stats.render_report([
            row(target="provider\x1b[2J", model="model\x9b2J", run_id="run\n# forged heading",
                client_label='<img src="x" onerror="alert(1)">'),
        ])
        self.assertNotIn("\x1b", report)
        self.assertNotIn("\x9b", report)
        self.assertNotIn('<img src="x"', report)
        self.assertIn('&lt;img src="x"', report)
        self.assertNotIn("\n# forged heading", report)


if __name__ == "__main__":
    unittest.main()
