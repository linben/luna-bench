import http.client
import io
import json
import ssl
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from lunabench.targets import Target
from lunabench.transport import ConnectionPool, TimedHTTPSConnection, execute

MS = 1_000_000
TARGET = Target("local", "example.invalid", "/v1", "test", None, None)


class Clock:
    def __init__(self):
        self.now = 1000 * MS

    def __call__(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds * MS


class Response:
    status = 200
    will_close = False

    length = None
    def __init__(self, clock, chunks):
        self.clock = clock
        self.chunks = iter(chunks)
        self.closed = False

    def read1(self, size):
        delay, value = next(self.chunks)
        self.clock.advance(delay)
        if isinstance(value, Exception):
            raise value
        return value

    def getheader(self, name):
        return None

    def close(self):
        self.closed = True


class Connection:
    t_connect_start_ns = None
    t_tcp_ns = None
    t_tls_ns = None

    def __init__(self, clock, response, fail_at=None):
        self.clock = clock
        self.response = response
        self.fail_at = fail_at

    def request(self, *args, **kwargs):
        if self.fail_at == "request":
            self.clock.advance(70)
            raise BrokenPipeError("stale connection")
        self.clock.advance(10)

    def getresponse(self):
        if self.fail_at == "headers":
            self.clock.advance(60)
            raise http.client.RemoteDisconnected("stale connection")
        self.clock.advance(20)
        return self.response


class Pool:
    def __init__(self, connections):
        self.connections = iter(connections)
        self.discarded = False

    def acquire(self, host, fresh):
        return next(self.connections)

    def discard(self, host):
        self.discarded = True


def text(value):
    return b'data: {"type":"response.output_text.delta","delta":"' + value + b'"}\n\n'


COMPLETED = b'data: {"type":"response.completed","response":{"usage":{"output_tokens":500}}}\n\n'


class LogicalRequestTiming(unittest.TestCase):
    def test_stale_retry_time_is_included_for_write_and_header_failures(self):
        for phase in ("request", "headers"):
            with self.subTest(phase=phase):
                clock = Clock()
                response = Response(clock, [(50, text(b"a")), (200, text(b"bcd")), (100, COMPLETED), (500, b"")])
                first = Connection(clock, response, fail_at=phase)
                replacement = Connection(clock, response)
                pool = Pool([(first, True), (replacement, False)])
                with patch("lunabench.transport.perf_counter_ns", clock):
                    sample = execute(TARGET, "responses", b"{}", {}, pool, False)
                self.assertEqual(sample["outcome"], "completed")
                self.assertEqual(sample["reconnect_ms"], 70)
                self.assertTrue(sample["reused_connection"])
                self.assertTrue(sample["reconnected"])
                self.assertIsNotNone(sample["reconnect_error"])
                self.assertEqual(sample["ttfb_ms"], 100)
                self.assertEqual(sample["ttft_ms"], 150)
                self.assertEqual(sample["t_last_text_ms"], 350)
                self.assertEqual(sample["t_done_ms"], 450)
                self.assertEqual(sample["total_ms"], 950)
                self.assertEqual(sample["t_eof_ms"], 950)
                self.assertEqual(sample["output_text"], "abcd")
                self.assertEqual(sample["text_delivery_chars_per_s"], 15)
                self.assertEqual(sample["max_stall_ms"], 200)
                self.assertEqual(sample["stall_count"], 1)
    def test_content_length_truncation_after_terminal_is_failed(self):
        body = text(b"x") + COMPLETED

        class Socket:
            def makefile(self, mode):
                headers = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body) + 100}\r\n\r\n".encode()
                return io.BytesIO(headers + body)

        response = http.client.HTTPResponse(Socket())
        response.begin()
        clock = Clock()
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "failed")
        self.assertIsNotNone(sample["t_done_ms"])
        self.assertIsNone(sample["t_eof_ms"])
        self.assertEqual(sample["completion_tokens"], 500)
        self.assertTrue(pool.discarded)


    def test_single_delta_has_no_delivery_rate_or_inter_text_stall(self):
        clock = Clock()
        response = Response(clock, [(10, text(b"entire answer")), (1000, COMPLETED), (500, b"")])
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "completed")
        self.assertEqual(sample["completion_tokens"], 500)
        self.assertIsNone(sample["text_delivery_chars_per_s"])
        self.assertIsNone(sample["max_stall_ms"])
        self.assertEqual(sample["stall_count"], 0)
        self.assertEqual(sample["ttft_ms"], sample["t_last_text_ms"])
        self.assertEqual(sample["t_done_ms"], 1040)
        self.assertEqual(sample["total_ms"], 1540)

    def test_stall_threshold_is_strict_and_equal_timestamp_rate_is_unknown(self):
        clock = Clock()
        response = Response(clock, [(10, text(b"a")), (100, text(b"b")), (0, COMPLETED), (0, b"")])
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["max_stall_ms"], 100)
        self.assertEqual(sample["stall_count"], 0)
        response = Response(clock, [(10, text(b"a") + text(b"b") + COMPLETED), (0, b"")])
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "completed")
        self.assertIsNone(sample["text_delivery_chars_per_s"])

    def test_post_terminal_disconnect_preserves_usage_but_has_no_eof_timestamp(self):
        clock = Clock()
        response = Response(clock, [(10, text(b"x")), (10, COMPLETED), (500, ConnectionResetError("reset"))])
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "failed")
        self.assertEqual(sample["t_done_ms"], 50)
        self.assertEqual(sample["total_ms"], 550)
        self.assertIsNone(sample["t_eof_ms"])
        self.assertEqual(sample["completion_tokens"], 500)
        self.assertEqual(sample["output_text"], "x")
        self.assertTrue(pool.discarded)

    def test_fresh_connection_failures_are_not_retried(self):
        clock = Clock()
        conn = Connection(clock, None, fail_at="request")
        pool = Pool([(conn, False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, True)
        self.assertEqual(sample["outcome"], "failed")
        self.assertEqual(sample["total_ms"], 70)
        self.assertFalse(sample["reconnected"])

    def test_error_body_is_not_consumed_or_persisted_and_response_is_closed(self):
        clock = Clock()
        response = Response(clock, [])
        response.status = 401
        response.read = Mock(return_value=b"credential-canary")
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "failed")
        self.assertEqual(sample["status"], 401)
        self.assertNotIn("credential-canary", json.dumps(sample))
        response.read.assert_not_called()
        self.assertTrue(response.closed)
        self.assertTrue(pool.discarded)

    def test_remote_status_line_is_not_persisted(self):
        class BadStatusConnection(Connection):
            def getresponse(self):
                raise http.client.BadStatusLine("credential-canary")

        clock = Clock()
        pool = Pool([(BadStatusConnection(clock, None), False)])
        sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "failed")
        self.assertNotIn("credential-canary", json.dumps(sample))
        self.assertTrue(pool.discarded)

    def test_partial_response_file_is_closed_on_protocol_failure(self):
        clock = Clock()
        response = Response(clock, [(1, b"data: invalid\n\n")])
        pool = Pool([(Connection(clock, response), False)])
        sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "failed")
        self.assertTrue(response.closed)
        self.assertTrue(pool.discarded)

    def test_eof_precedes_post_parse_bookkeeping(self):
        class TickingClock(Clock):
            def __call__(self):
                self.advance(1)
                return self.now

        clock = TickingClock()
        response = Response(clock, [(10, text(b"x") + COMPLETED), (50, b"")])
        pool = Pool([(Connection(clock, response), False)])
        with patch("lunabench.transport.perf_counter_ns", clock):
            sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertEqual(sample["outcome"], "completed")
        self.assertLess(sample["t_done_ms"], sample["t_eof_ms"])
        self.assertLess(sample["t_eof_ms"], sample["total_ms"])

    def test_header_metadata_cannot_persist_control_characters(self):
        clock = Clock()
        response = Response(clock, [(0, text(b"x") + COMPLETED), (0, b"")])
        response.getheader = lambda name: "credential-canary\r\nforged: value"
        pool = Pool([(Connection(clock, response), False)])
        sample = execute(TARGET, "responses", b"{}", {}, pool, False)
        self.assertNotIn("credential-canary", json.dumps(sample))
        self.assertIsNone(sample["request_id"])
        self.assertIsNone(sample["retry_after"])


class TLSLifecycle(unittest.TestCase):
    def test_setup_failures_close_socket_and_discard_old_phase_timestamps(self):
        for phase in ("socket_option", "handshake"):
            with self.subTest(phase=phase):
                sock = Mock()
                context = Mock()
                if phase == "socket_option":
                    sock.setsockopt.side_effect = OSError("setup failed")
                else:
                    context.wrap_socket.side_effect = ssl.SSLCertVerificationError("untrusted")
                conn = TimedHTTPSConnection("example.invalid", 1)
                conn._context = context
                conn.t_tls_ns = 1
                with patch("lunabench.transport.socket.create_connection", return_value=sock):
                    with self.assertRaises(OSError):
                        conn.connect()
                self.assertIsNone(conn.sock)
                self.assertIsNone(conn.t_tls_ns)
                sock.close.assert_called_once()


class PoolLifecycle(unittest.TestCase):
    def test_main_thread_closes_connections_created_by_drained_workers(self):
        class SocketConnection:
            def __init__(self, host, timeout):
                self.sock = object()
                self.closed = False

            def close(self):
                self.closed = True
                self.sock = None

        barrier = threading.Barrier(2)
        pool = ConnectionPool()

        def worker():
            conn, _ = pool.acquire("example.invalid", False)
            barrier.wait(timeout=5)
            return conn

        with patch("lunabench.transport.TimedHTTPSConnection", SocketConnection):
            with ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(worker)
                b = executor.submit(worker)
                connections = [a.result(), b.result()]
            self.assertIsNot(connections[0], connections[1])
            self.assertTrue(all(not conn.closed for conn in connections))
            pool.close_all()
            self.assertTrue(all(conn.closed for conn in connections))
            pool.close_all()
            with self.assertRaises(RuntimeError):
                pool.acquire("example.invalid", False)


if __name__ == "__main__":
    unittest.main()
