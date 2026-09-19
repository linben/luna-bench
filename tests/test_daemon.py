import argparse
import contextlib
import io
import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lunabench import daemon


def arguments(directory, interval=0.03, no_tui=True):
    return argparse.Namespace(
        daemon=True, interval=interval, no_tui=no_tui,
        log_dir=str(Path(directory) / "session"), out=None, dry_run=False,
    )


def log_events(args):
    return [json.loads(line) for line in (Path(args.log_dir) / "daemon.jsonl").read_text().splitlines()]


class DaemonLifecycle(unittest.TestCase):
    def test_failed_overrun_skips_slots_then_runs_in_distinct_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(directory)
            paths = []
            active = 0
            maximum_active = 0

            def run_once(run_args, *, event, stop):
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                paths.append(Path(run_args.out))
                try:
                    with paths[-1].open("x") as sink:
                        sink.write(json.dumps({"cycle": len(paths)}) + "\n")
                    if len(paths) == 1:
                        time.sleep(args.interval * 3.2)
                        raise RuntimeError("first cycle failed after writing a row")
                    stop.set()
                    return 0
                finally:
                    active -= 1

            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(daemon.run(args, run_once), 0)
            events = log_events(args)
            starts = [event for event in events if event["event"] == "cycle_started"]
            ends = [event for event in events if event["event"] == "cycle_finished"]
            self.assertEqual(maximum_active, 1)
            self.assertEqual(len(paths), 2)
            self.assertNotEqual(paths[0], paths[1])
            self.assertEqual([json.loads(path.read_text())["cycle"] for path in paths], [1, 2])
            self.assertEqual(starts[0]["scheduled_slot"], 0)
            self.assertGreaterEqual(starts[1]["scheduled_slot"], 4)
            self.assertEqual(ends[0]["status"], "failed")
            self.assertTrue(ends[1]["interrupted"])
            self.assertTrue(any(event["event"] == "worker_exception" and "RuntimeError" in event["traceback"] for event in events))
            self.assertGreaterEqual(sum(event["count"] for event in events if event["event"] == "cadence_skipped"), 3)
            self.assertIsNone(args.out)
            self.assertTrue(args.daemon)

    def test_signal_and_quit_drain_active_work_and_restore_resources(self):
        for source in ("signal", "quit"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                args = arguments(directory, interval=600, no_tui=source == "signal")
                handlers = {}
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
                calls = []
                active = threading.Event()
                restored = []

                def install(sig, handler):
                    handlers[sig] = handler

                class Dashboard:
                    def __enter__(self):
                        return self

                    def render(self, snapshot):
                        pass

                    def poll(self):
                        return "quit" if active.is_set() else None

                    def __exit__(self, *exc):
                        restored.append(True)

                def run_once(run_args, *, event, stop):
                    calls.append(run_args.out)
                    active.set()
                    if source == "signal":
                        handlers[signal.SIGTERM](signal.SIGTERM, None)
                    if not stop.wait(3):
                        raise AssertionError("stop was not delivered to the active benchmark")
                    with open(run_args.out, "x") as sink:
                        sink.write('{"admitted_request":"drained"}\n')
                    return 0

                with contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    stack.enter_context(patch.object(daemon.signal, "signal", side_effect=install))
                    if source == "quit":
                        stack.enter_context(patch("lunabench.tui.Dashboard", Dashboard))
                        stack.enter_context(patch.object(daemon.sys.stdin, "isatty", return_value=True))
                        stack.enter_context(patch.object(daemon.sys.stdout, "isatty", return_value=True))
                    self.assertEqual(daemon.run(args, run_once), 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(Path(calls[0]).read_text()), {"admitted_request": "drained"})
                self.assertEqual(handlers, previous)
                self.assertEqual(restored, [True] if source == "quit" else [])
                ended = next(event for event in log_events(args) if event["event"] == "cycle_finished")
                self.assertTrue(ended["interrupted"])
                self.assertEqual(ended["status"], "interrupted")

    def test_log_io_failure_stops_scheduling_but_does_not_abort_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(directory)
            calls = []
            stderr = io.StringIO()

            def run_once(run_args, *, event, stop):
                calls.append(run_args.out)
                with open(run_args.out, "x") as sink:
                    sink.write('{"request":1}\n')
                    with patch.object(daemon._StrictRotatingHandler, "flush", side_effect=OSError("disk failed")):
                        event("sample", sample={"status": 200})
                    self.assertTrue(stop.is_set())
                    sink.write('{"request":2}\n')
                    event("sample", sample={"status": 200})
                return 0

            with contextlib.redirect_stderr(stderr):
                self.assertEqual(daemon.run(args, run_once), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual([json.loads(line)["request"] for line in Path(calls[0]).read_text().splitlines()], [1, 2])

    def test_callback_exception_details_are_not_persisted_or_printed(self):
        secret = "callback-secret-must-not-be-recorded"
        for source in ("worker", "dashboard"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                args = arguments(directory, no_tui=source == "worker")
                stderr = io.StringIO()
                active = threading.Event()
                finished = threading.Event()
                restored = []

                def run_once(run_args, *, event, stop):
                    if source == "worker":
                        stop.set()
                        raise RuntimeError("callback-secret-must-not-be-recorded")
                    active.set()
                    if not stop.wait(3):
                        raise AssertionError("dashboard failure did not stop active work")
                    finished.set()
                    return 0

                testcase = self

                class Dashboard:
                    def __enter__(self):
                        return self

                    def render(self, snapshot):
                        testcase.assertTrue(active.wait(3), "benchmark did not start")
                        raise RuntimeError("callback-secret-must-not-be-recorded")

                    def __exit__(self, *exc):
                        restored.append(True)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stderr(stderr))
                    if source == "dashboard":
                        stack.enter_context(patch("lunabench.tui.Dashboard", Dashboard))
                        stack.enter_context(patch.object(daemon.sys.stdin, "isatty", return_value=True))
                        stack.enter_context(patch.object(daemon.sys.stdout, "isatty", return_value=True))
                    self.assertEqual(daemon.run(args, run_once), int(source == "dashboard"))
                events = log_events(args)
                event_name = "worker_exception" if source == "worker" else "scheduler_exception"
                failure = next(event for event in events if event["event"] == event_name)
                self.assertEqual(failure["error"], "RuntimeError")
                self.assertNotIn(secret, json.dumps(events) + stderr.getvalue())
                self.assertNotIn(str(Path(__file__).resolve().parent), failure["traceback"])
                if source == "dashboard":
                    self.assertTrue(finished.is_set())
                    self.assertEqual(restored, [True])

    def test_existing_session_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(directory)
            Path(args.log_dir).mkdir()
            path = Path(args.log_dir) / "daemon.jsonl"
            path.write_text("existing audit log\n")
            calls = []
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(daemon.run(args, lambda *a, **k: calls.append(a)), 1)
            self.assertEqual(calls, [])
            self.assertEqual(path.read_text(), "existing audit log\n")


if __name__ == "__main__":
    unittest.main()
