import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from lunabench.load import drive


class LoadGeneration(unittest.TestCase):
    def test_sustained_refills_before_slow_peer_finishes(self):
        slow_started = threading.Event()
        replacement_started = threading.Event()
        release_slow = threading.Event()
        rows = []
        failures = []

        def request(index, due, epoch):
            if index == 0:
                slow_started.set()
                if not release_slow.wait(3):
                    raise AssertionError("slow request was not released")
            elif index == 1:
                if not slow_started.wait(3):
                    raise AssertionError("peer did not start")
            else:
                replacement_started.set()
            return {"index": index}

        def run():
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    drive(executor, request, None, rows.append, concurrency=2,
                          mode="sustained", count=3, duration=None)
            except BaseException as exc:
                failures.append(exc)

        runner = threading.Thread(target=run)
        runner.start()
        try:
            self.assertTrue(replacement_started.wait(2), "fast worker remained idle behind slow peer")
        finally:
            release_slow.set()
            runner.join(4)
        self.assertFalse(runner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual({row["index"] for row in rows}, {0, 1, 2})

    def test_rate_records_overload_without_delaying_arrivals(self):
        release = threading.Event()
        rows = []

        def request(index, due, epoch):
            if not release.wait(3):
                raise AssertionError("arrivals were blocked behind pending requests")
            return {"index": index, "dropped": False, "due": due}

        def drop(index, due, epoch):
            if index == 5:
                release.set()
            return {"index": index, "dropped": True, "due": due}

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                drive(executor, request, drop, rows.append, concurrency=1,
                      max_pending=1, mode="rate", arrival_rate=1000,
                      count=6, duration=None)
        finally:
            release.set()
        ordered = sorted(rows, key=lambda row: row["index"])
        self.assertEqual([row["dropped"] for row in ordered], [False, False, True, True, True, True])
        self.assertAlmostEqual(ordered[-1]["due"] - ordered[0]["due"], 0.005, places=6)

    def test_admitted_results_are_drained_on_interruption(self):
        rows = []
        gate = threading.Barrier(2)

        def request(index, due, epoch):
            gate.wait(timeout=3)
            return {"index": index}

        def record(row):
            rows.append(row)
            if len(rows) == 1:
                raise KeyboardInterrupt()

        with ThreadPoolExecutor(max_workers=2) as executor:
            with self.assertRaises(KeyboardInterrupt):
                drive(executor, request, None, record, concurrency=2,
                      mode="sustained", count=4, duration=None)
        self.assertEqual({row["index"] for row in rows}, {0, 1})

    def test_drain_failure_does_not_replace_interruption(self):
        rows = []
        gate = threading.Barrier(2)

        def request(index, due, epoch):
            gate.wait(timeout=3)
            return {"index": index}

        def record(row):
            rows.append(row)
            if len(rows) == 1:
                raise KeyboardInterrupt()
            raise OSError("sink failed while draining")

        with ThreadPoolExecutor(max_workers=2) as executor:
            with self.assertRaises(KeyboardInterrupt):
                drive(executor, request, None, record, concurrency=2,
                      mode="sustained", count=4, duration=None)
        self.assertEqual({row["index"] for row in rows}, {0, 1})

    def test_stop_drains_admitted_requests_without_refilling(self):
        stop = threading.Event()
        rows = []
        gate = threading.Barrier(2)

        def request(index, due, epoch):
            gate.wait(timeout=3)
            stop.set()
            return {"index": index}

        with ThreadPoolExecutor(max_workers=2) as executor:
            drive(executor, request, None, rows.append, concurrency=2,
                  mode="sustained", count=20, duration=None, stop=stop)
        self.assertEqual({row["index"] for row in rows}, {0, 1})

    def test_stop_wakes_idle_rate_scheduler(self):
        stop = threading.Event()
        first_recorded = threading.Event()
        rows = []
        failures = []

        def record(row):
            rows.append(row)
            first_recorded.set()

        def run():
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    drive(executor, lambda index, *_: {"index": index}, None, record,
                          concurrency=1, mode="rate", arrival_rate=0.01,
                          count=2, duration=None, stop=stop)
            except BaseException as exc:
                failures.append(exc)

        runner = threading.Thread(target=run)
        runner.start()
        try:
            self.assertTrue(first_recorded.wait(3))
        finally:
            stop.set()
            runner.join(3)
        self.assertFalse(runner.is_alive(), "stop waited for the next 100-second arrival")
        self.assertEqual(failures, [])
        self.assertEqual(rows, [{"index": 0}])


if __name__ == "__main__":
    unittest.main()
