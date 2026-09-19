"""Bounded load generation; scheduled arrivals remain visible during overload."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Event
from typing import Callable


def drive(
    executor: ThreadPoolExecutor,
    request: Callable[[int, float, float], dict],
    dropped: Callable[[int, float, float], dict],
    record: Callable[[dict], None],
    *,
    concurrency: int,
    mode: str,
    count: int | None,
    duration: float | None,
    arrival_rate: float | None = None,
    max_pending: int = 0,
    stop: Event | None = None,
) -> None:
    """Run one target/API cohort, draining admitted requests before returning.

    Rate mode schedules independently of completions, with at most concurrency +
    max_pending admitted requests. Every excess arrival produces a dropped row.
    Sustained mode replaces each finished request; burst mode waits for a batch.
    When stop is set, no new work is admitted and pending results still drain.
    """
    if concurrency < 1 or max_pending < 0 or mode not in {"sustained", "rate", "burst"}:
        raise ValueError("invalid load configuration")
    if (count is None) == (duration is None):
        raise ValueError("choose exactly one of count or duration")
    if mode == "rate" and (arrival_rate is None or arrival_rate <= 0):
        raise ValueError("rate mode requires a positive arrival rate")
    start = time.perf_counter()
    epoch = time.time()
    deadline = start + duration if duration is not None else None
    pending: set[Future] = set()
    index = 0

    def available(due: float) -> bool:
        return (not (stop and stop.is_set()) and (count is None or index < count)
                and (deadline is None or due < deadline))

    def collect(done: set[Future]) -> None:
        for future in done:
            pending.remove(future)
            record(future.result())

    failed = False
    try:
        while True:
            collect({future for future in pending if future.done()})
            now = time.perf_counter()
            due = start + index / arrival_rate if mode == "rate" else now
            if not available(due):
                break
            if mode == "rate":
                if now < due:
                    if pending:
                        timeout = min(due - now, 0.1) if stop else due - now
                        done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                        collect(done)
                    elif stop:
                        stop.wait(due - now)
                    else:
                        time.sleep(due - now)
                    continue
                if len(pending) >= concurrency + max_pending:
                    record(dropped(index, due, epoch + due - start))
                else:
                    pending.add(executor.submit(request, index, due, epoch + due - start))
                index += 1
            else:
                while len(pending) < concurrency and available(time.perf_counter()):
                    due = time.perf_counter()
                    pending.add(executor.submit(request, index, due, epoch + due - start))
                    index += 1
                if pending:
                    if mode == "burst":
                        done, _ = wait(pending)
                    else:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
    except BaseException:
        failed = True
        raise
    finally:
        # Preserve admitted successes even if another worker fails while draining.
        error = None
        for future in tuple(pending):
            pending.remove(future)
            try:
                record(future.result())
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None and not failed:
            raise error
