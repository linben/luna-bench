"""Foreground, fixed-cadence benchmark scheduling with durable event logging."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import stats


_REFRESH_S = 0.2


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _console(message: str) -> None:
    text = "".join(char if char.isprintable() else " " for char in message)
    try:
        print(f"lunabench: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # A closed service console must not break the durable file sink.
        try:
            os.write(2, (f"lunabench: {text}\n").encode("utf-8", errors="replace"))
        except OSError:
            pass


def _exception_fields(exc):
    """Keep failure locations without exception text or source-code disclosure."""
    category = type(exc).__name__
    frames = [f"{Path(frame.f_code.co_filename).name}:{line} in {frame.f_code.co_name}"
              for frame, line in traceback.walk_tb(exc.__traceback__)]
    return {"error": category, "traceback": "\n".join([*frames, category])}


class _StrictRotatingHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def handleError(self, record):
        # logging's default implementation swallows I/O failures, allowing
        # further billable requests despite losing their audit trail.
        raise


class _Session:
    def __init__(self, args, log_dir, stop, wake):
        self.log_dir = log_dir
        self.stop = stop
        self.wake = wake
        self.lock = threading.RLock()
        self.handler = _StrictRotatingHandler(
            log_dir / "daemon.jsonl", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.log_failed = False
        self.headless = True
        self.started = time.monotonic()
        self.run_started = None
        self.run_ended = None
        self.interrupted = False
        self.recent = deque(maxlen=32)
        self.state = {
            "state": "starting", "interval_s": float(args.interval),
            "cycle": 0, "completed": 0, "failed": 0,
            "started_at": _utc(), "target": "", "api": "",
            "samples": 0, "errors": 0, "dropped": 0,
            "last_status": "not started", "log_dir": str(log_dir),
            "samples_path": "", "report_path": "",
        }

    def logging_failure(self, exc):
        if not self.log_failed:
            self.log_failed = True
            message = f"FATAL: daemon log failed ({type(exc).__name__}); stopping and draining active work"
            self.recent.append(message)
            _console(message)
        self.stop.set()
        self.wake.set()

    def emit(self, name, **fields):
        """Log synchronously; sink failures request stop without breaking drain."""
        with self.lock:
            if not self.log_failed:
                try:
                    payload = {"ts": _utc(), "event": name, "cycle": self.state["cycle"], **fields}
                    record = logging.LogRecord(
                        "lunabench.daemon", logging.INFO, __file__, 0,
                        json.dumps(payload, ensure_ascii=True, separators=(",", ":")), (), None,
                    )
                    self.handler.handle(record)
                    if name in {"scheduler_started", "cycle_started", "cycle_finished", "scheduler_stopped"}:
                        os.fsync(self.handler.stream.fileno())
                except Exception as exc:
                    self.logging_failure(exc)
            message = self._reduce(name, fields)
            if message:
                self.recent.append(message[:600])
                if self.headless and name not in {"sample", "warmup_result"}:
                    _console(message)
            return not self.log_failed

    def _reduce(self, name, fields):
        state = self.state
        if name == "sample":
            sample = fields["sample"]
            failed = stats.is_error(sample)
            state["samples"] += 1
            state["errors"] += int(failed)
            state["dropped"] += int(sample.get("outcome") == "dropped")
            if failed:
                return f"Sample {sample.get('target', '')}/{sample.get('api', '')}: {sample.get('error') or sample.get('validation_error') or sample.get('outcome')}"
        elif name == "warmup_result":
            sample = fields["sample"]
            if stats.is_error(sample):
                return f"Warmup {sample.get('target', '')}/{sample.get('api', '')}: {sample.get('error') or sample.get('validation_error') or sample.get('outcome')}"
        elif name == "cohort_started":
            state["target"], state["api"] = fields["target"], fields["api"]
            return f"Cohort {state['target']}/{state['api']} started"
        elif name == "cohort_finished":
            summary = fields["summary"]
            return f"Cohort {fields['target']}/{fields['api']}: {summary.get('n', 0)} samples, {summary.get('errors', 0)} errors"
        elif name == "run_started":
            state["samples_path"] = str(fields["samples"])
            return f"Benchmark {fields['run_id']} started"
        elif name == "run_finished":
            self.interrupted = bool(fields["interrupted"])
            state["samples_path"] = str(fields["samples"])
            state["report_path"] = str(fields["report"])
            summary = fields["summary"]
            for key, source in (("samples", "n"), ("errors", "errors"), ("dropped", "drops")):
                state[key] = summary.get(source, state[key])
            return f"Benchmark {fields['run_id']} finished; report: {state['report_path']}"
        elif name == "target_skipped":
            return f"Target {fields['target']} skipped: {fields['reason']}"
        elif name == "scheduler_started":
            return f"Daemon started; interval {state['interval_s']:g}s; logs: {self.log_dir}"
        elif name == "cycle_started":
            return f"Cycle {state['cycle']} started; samples: {state['samples_path']}"
        elif name == "cycle_finished":
            return f"Cycle {state['cycle']} {fields['status']} in {fields['duration_s']:.3f}s; {state['samples']} samples, {state['errors']} errors, {state['dropped']} drops"
        elif name == "cadence_skipped":
            return f"Skipped {fields['count']} cadence slot(s): {fields['reason']}"
        elif name == "stop_requested":
            return f"Stop requested ({fields['reason']}); draining active work"
        elif name == "scheduler_stopped":
            return f"Daemon stopped; {state['completed']} cycles settled, {state['failed']} failed; logs: {self.log_dir}"
        elif name in {"worker_exception", "scheduler_exception", "tui_warning"}:
            return f"{name}: {fields['error']}"
        return None

    def snapshot(self, now, next_deadline):
        with self.lock:
            run_elapsed = 0.0 if self.run_started is None else (self.run_ended or now) - self.run_started
            return {
                **self.state,
                "elapsed_s": max(0.0, now - self.started),
                "run_elapsed_s": max(0.0, run_elapsed),
                "next_in_s": None if self.stop.is_set() else max(0.0, next_deadline - now),
                "recent": list(self.recent),
            }

    def begin(self, cycle, run_args):
        with self.lock:
            self.run_started = time.monotonic()
            self.run_ended = None
            self.interrupted = False
            self.state.update(
                state="running", cycle=cycle, target="", api="", samples=0,
                errors=0, dropped=0, samples_path=run_args.out,
                report_path=run_args.out + ".md",
            )

    def execute(self, run_args, run_once, done):
        code = 0
        failed = False
        try:
            if not self.stop.is_set():
                code = run_once(run_args, event=self.emit, stop=self.stop)
                failed = code != 0
        except BaseException as exc:
            failed = True
            code = 1
            if isinstance(exc, KeyboardInterrupt):
                self.stop.set()
            self.emit("worker_exception", **_exception_fields(exc))
        finally:
            try:
                with self.lock:
                    interrupted = self.interrupted or self.stop.is_set()
                    failed = failed or self.log_failed or self.state["errors"] > 0
                    status = ("failed" if code != 0 or self.log_failed else "interrupted" if interrupted
                              else "completed_with_errors" if failed else "completed")
                    self.run_ended = time.monotonic()
                    self.state["completed"] += 1
                    self.state["failed"] += int(failed)
                    self.state["last_status"] = status
                    self.state["state"] = "stopping" if self.stop.is_set() else "waiting"
                    self.emit(
                        "cycle_finished", status=status, exit_code=code,
                        interrupted=interrupted, failed=failed,
                        duration_s=max(0.0, self.run_ended - self.run_started),
                        samples=self.state["samples"], errors=self.state["errors"],
                        dropped=self.state["dropped"], samples_path=run_args.out,
                        report_path=run_args.out + ".md",
                        samples_exists=Path(run_args.out).is_file(),
                        report_exists=Path(run_args.out + ".md").is_file(),
                    )
            finally:
                # Completion is published only after state/log updates. The
                # main thread joins before it considers another cadence slot.
                with self.lock:
                    self.run_ended = time.monotonic()
                done.set()
                self.wake.set()

    def close(self):
        with self.lock:
            try:
                self.handler.close()
            except Exception as exc:
                self.logging_failure(exc)


def run(args, run_once) -> int:
    """Run immediately, then on monotonic slots; never overlap or catch up."""
    interval = float(args.interval)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("daemon interval must be finite and greater than zero")
    stop = threading.Event()
    wake = threading.Event()
    done = threading.Event()
    reason = [None]
    session = None
    dashboard = None
    worker = None
    previous_signals = {}
    exit_code = 0

    def handle_signal(signum, frame):
        if reason[0] is None:
            reason[0] = signal.Signals(signum).name
        # Signal callbacks must not acquire Event/Condition locks: a signal
        # can interrupt the main thread while that same lock is held.

    try:
        log_dir = Path(args.log_dir) if args.log_dir else Path("results") / f"daemon-{_stamp()}-{uuid.uuid4().hex}"
        # Refuse reuse rather than allowing independent processes to race on
        # rotation. Private directories protect reports without changing umask.
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        session = _Session(args, log_dir, stop, wake)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        if not args.no_tui and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                from .tui import Dashboard
                dashboard = Dashboard()
                dashboard.__enter__()
                session.headless = False
            except Exception as exc:
                if dashboard is not None:
                    try:
                        dashboard.__exit__(*sys.exc_info())
                    except Exception as restore_exc:
                        _console(f"Terminal restoration failed: {type(restore_exc).__name__}")
                dashboard = None
                session.emit("tui_warning", error=f"{type(exc).__name__}; continuing headless")
        config = {key: value for key, value in vars(args).items() if key != "fn" and not callable(value)}
        config["log_dir"] = str(log_dir)
        session.emit("scheduler_started", config=config, pid=os.getpid(), interval_s=interval)
        origin = time.monotonic()
        next_slot = 0
        next_frame = origin
        stop_logged = False
        while True:
            wake.clear()
            now = time.monotonic()
            if reason[0] is not None:
                stop.set()
            if stop.is_set() and not stop_logged:
                with session.lock:
                    session.state["state"] = "stopping"
                session.emit("stop_requested", reason="logging_failure" if session.log_failed else reason[0] or "benchmark_requested_stop")
                stop_logged = True
            if worker is not None and done.is_set():
                worker.join()
                worker = None
                # Skip every slot strictly before completion, not a tick that
                # happens to arrive while the main thread notices completion.
                available_slot = math.ceil((session.run_ended - origin) / interval)
                if not stop.is_set() and available_slot > next_slot:
                    session.emit("cadence_skipped", count=available_slot - next_slot,
                                 first_slot=next_slot, reason="previous_cycle_running")
                    next_slot = available_slot
            if stop.is_set() and worker is None:
                break
            deadline = origin + next_slot * interval
            if not stop.is_set() and now >= deadline:
                latest_slot = math.floor((now - origin) / interval)
                if worker is not None:
                    skipped = latest_slot - next_slot + 1
                    session.emit("cadence_skipped", count=skipped, first_slot=next_slot,
                                 reason="previous_cycle_running")
                    next_slot = latest_slot + 1
                else:
                    if latest_slot > next_slot:
                        session.emit("cadence_skipped", count=latest_slot - next_slot,
                                     first_slot=next_slot, reason="scheduler_delayed")
                    next_slot = latest_slot
                    cycle = session.state["cycle"] + 1
                    cycle_dir = log_dir / f"run-{cycle:06d}-{_stamp()}-{uuid.uuid4().hex}"
                    cycle_dir.mkdir(mode=0o700)
                    run_args = argparse.Namespace(**vars(args))
                    run_args.daemon = False
                    run_args.out = str(cycle_dir / "samples.jsonl")
                    session.begin(cycle, run_args)
                    session.emit("cycle_started", scheduled_slot=next_slot,
                                 scheduled_offset_s=next_slot * interval,
                                 lateness_s=max(0.0, now - (origin + next_slot * interval)),
                                 samples_path=run_args.out, report_path=run_args.out + ".md")
                    next_slot += 1
                    if not stop.is_set() and reason[0] is None:
                        done.clear()
                        worker = threading.Thread(
                            target=session.execute, args=(run_args, run_once, done),
                            name=f"lunabench-cycle-{cycle}", daemon=False,
                        )
                        worker.start()
                deadline = origin + next_slot * interval
            if dashboard is not None and now >= next_frame:
                dashboard.render(session.snapshot(now, deadline))
                if dashboard.poll() == "quit":
                    reason[0] = "quit"
                    stop.set()
                    wake.set()
                next_frame = time.monotonic() + _REFRESH_S
            timeout = _REFRESH_S
            if not stop.is_set() and worker is None:
                timeout = min(timeout, max(0.0, deadline - time.monotonic()))
            wake.wait(timeout)
    except KeyboardInterrupt:
        reason[0] = reason[0] or "KeyboardInterrupt"
        stop.set()
        if session is not None:
            session.emit("stop_requested", reason=reason[0])
    except Exception as exc:
        exit_code = 1
        stop.set()
        if session is None:
            _console(f"FATAL: daemon startup failed: {type(exc).__name__}")
        else:
            session.emit("scheduler_exception", **_exception_fields(exc))
    finally:
        stop.set()
        try:
            if worker is not None and worker.ident is not None:
                worker.join()
            if session is not None:
                session.emit("scheduler_stopped", exit_code=1 if session.log_failed else exit_code,
                             reason=reason[0] or ("error" if exit_code or session.log_failed else "benchmark_requested_stop"),
                             completed=session.state["completed"], failed=session.state["failed"],
                             duration_s=max(0.0, time.monotonic() - session.started))
        finally:
            try:
                if dashboard is not None:
                    dashboard.__exit__(None, None, None)
            except Exception as exc:
                exit_code = 1
                _console(f"Terminal restoration failed: {type(exc).__name__}")
            finally:
                try:
                    for signum, handler in previous_signals.items():
                        try:
                            signal.signal(signum, handler)
                        except (OSError, ValueError) as exc:
                            exit_code = 1
                            _console(f"Signal restoration failed: {type(exc).__name__}")
                finally:
                    if session is not None:
                        session.close()
                        if session.log_failed:
                            exit_code = 1
    return exit_code
