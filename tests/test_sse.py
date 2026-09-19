import json
import unittest
from unittest.mock import patch

from lunabench.transport import parse_stream

MS = 1_000_000


def raw(t_ms, line):
    return t_ms * MS, line


def data(t_ms, obj):
    return raw(t_ms, b"data: " + json.dumps(obj).encode() + b"\n")


def event(t_ms, obj):
    return [data(t_ms, obj), raw(t_ms, b"\n")]


def chat_chunk(content=None, finish_reason=None, usage=None):
    return {
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage,
    }


def chat_done(t_ms):
    return [raw(t_ms, b"data: [DONE]\n"), raw(t_ms, b"\n")]


def resp_usage():
    return {
        "input_tokens": 20,
        "output_tokens": 11,
        "output_tokens_details": {"reasoning_tokens": 0},
        "input_tokens_details": {"cached_tokens": 0},
    }


def completed(t_ms):
    return event(t_ms, {"type": "response.completed", "response": {"status": "completed", "usage": resp_usage()}})


class SSEFraming(unittest.TestCase):
    def test_multiline_data_dispatches_at_blank_delimiter(self):
        lines = [
            raw(10, b'\xef\xbb\xbf: heartbeat\r\n'),
            raw(20, b'data: {"type": "response.output_text.delta",\r\n'),
            raw(30, b'data: "delta": "hello"}\r\n'),
            raw(40, b'event: response.output_text.delta\r\n'),
            raw(90, b'\r\n'),
            *completed(100),
        ]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "completed")
        self.assertEqual(res.output_text, "hello")
        self.assertEqual(res.t_first_token_ns, 90 * MS)
        self.assertEqual(res.t_done_ns, 100 * MS)

    def test_crlf_split_across_reads_does_not_create_blank_event(self):
        lines = [
            raw(10, b'data: {"type":"response.output_text.delta","delta":"x"}\r'),
            raw(20, b'\n'),
            raw(50, b'\r'),
            raw(60, b'\n'),
            *completed(70),
        ]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "completed")
        self.assertEqual(res.t_first_token_ns, 50 * MS)

    def test_bare_cr_and_fragmented_utf8_are_supported(self):
        encoded = json.dumps({"type": "response.output_text.delta", "delta": "café"}, ensure_ascii=False).encode()
        split = encoded.index(b"\xc3") + 1
        lines = [raw(10, b"data: " + encoded[:split]), raw(20, encoded[split:] + b"\r\r"), *completed(30)]
        res = parse_stream("responses", lines)
        self.assertEqual(res.output_text, "café")
        self.assertEqual(res.t_first_token_ns, 20 * MS)
        self.assertEqual(res.outcome, "completed")

    def test_unterminated_eof_never_dispatches_final_event(self):
        res = parse_stream("responses", [data(10, {"type": "response.output_text.delta", "delta": "hidden"})])
        self.assertEqual(res.output_text, "")
        self.assertIsNone(res.t_first_token_ns)
        self.assertEqual(res.outcome, "incomplete")
        lines = [*event(10, {"type": "response.output_text.delta", "delta": "x"}), data(20, {"type": "response.completed"})]
        res = parse_stream("responses", lines)
        self.assertEqual(res.output_text, "x")
        self.assertEqual(res.outcome, "incomplete")
        self.assertIsNone(res.t_done_ns)

    def test_malformed_event_preserves_already_dispatched_text(self):
        lines = [
            *event(10, {"type": "response.output_text.delta", "delta": "x"}),
            raw(20, b"data: {not json\n\n"),
            *event(30, {"type": "response.output_text.delta", "delta": "never"}),
        ]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "failed")
        self.assertEqual(res.output_text, "x")
        self.assertFalse(res.reached_eof)

    def test_oversized_line_is_rejected_without_unbounded_buffering(self):
        with patch("lunabench.transport._MAX_EVENT_BYTES", 16):
            res = parse_stream("responses", [raw(10, b"data: " + b"x" * 17)])
        self.assertEqual(res.outcome, "failed")
        self.assertEqual(res.output_text, "")

    def test_deeply_nested_json_is_a_failed_stream_not_an_escaping_exception(self):
        payload = b"[" * 2000 + b"0" + b"]" * 2000
        res = parse_stream("responses", [raw(10, b"data: " + payload + b"\n\n")])
        self.assertEqual(res.outcome, "failed")
        self.assertFalse(res.reached_eof)

    def test_malformed_payload_and_peer_exception_details_are_not_exposed(self):
        secret = "credential-canary"
        malformed = parse_stream("responses", [raw(10, f"data: {secret}\n\n".encode())])

        def disconnecting():
            yield from event(10, {"type": "response.output_text.delta", "delta": "x"})
            raise ConnectionResetError(secret)

        disconnected = parse_stream("responses", disconnecting())
        for res in (malformed, disconnected):
            self.assertEqual(res.outcome, "failed")
            self.assertNotIn(secret, res.error)


class ChatStream(unittest.TestCase):
    def test_success_requires_finish_reason_and_done_and_preserves_usage(self):
        usage = {"prompt_tokens": 20, "completion_tokens": 11, "completion_tokens_details": {"reasoning_tokens": 0}}
        lines = [
            *event(100, chat_chunk("")),
            *event(200, chat_chunk("1")),
            *event(250, chat_chunk(", 2")),
            *event(300, chat_chunk(", 3", "stop")),
            *event(350, {"choices": [], "usage": usage}),
            *chat_done(360),
        ]
        res = parse_stream("chat", lines)
        self.assertEqual(res.outcome, "completed")
        self.assertIsNone(res.error)
        self.assertEqual(res.t_first_token_ns, 200 * MS)
        self.assertEqual(res.itl_ns, [50 * MS, 50 * MS])
        self.assertEqual(res.output_text, "1, 2, 3")
        self.assertEqual(res.usage["completion_tokens"], 11)
        self.assertEqual(res.usage["reasoning_tokens"], 0)
        self.assertIsNone(res.usage["cached_tokens"])
        self.assertEqual(res.t_done_ns, 360 * MS)

    def test_done_without_finish_reason_is_not_success(self):
        res = parse_stream("chat", [*event(10, chat_chunk("partial")), *chat_done(20)])
        self.assertEqual(res.outcome, "incomplete")

    def test_finish_reason_without_done_is_not_success(self):
        res = parse_stream("chat", event(10, chat_chunk("partial", "stop")))
        self.assertEqual(res.outcome, "incomplete")
        self.assertIsNone(res.t_done_ns)

    def test_length_limit_and_content_filter_are_not_success(self):
        for reason, outcome in [("length", "incomplete"), ("content_filter", "refused")]:
            with self.subTest(reason=reason):
                lines = [*event(10, chat_chunk("partial", reason, {"completion_tokens": 99})), *chat_done(20)]
                res = parse_stream("chat", lines)
                self.assertEqual(res.outcome, outcome)
                self.assertEqual(res.finish_reason, reason)
                self.assertEqual(res.usage["completion_tokens"], 99)

    def test_refusal_delta_cannot_be_hidden_by_stop(self):
        refusal = {"choices": [{"index": 0, "delta": {"refusal": "No"}, "finish_reason": None}]}
        res = parse_stream("chat", [*event(10, refusal), *event(20, chat_chunk(None, "stop")), *chat_done(30)])
        self.assertEqual(res.outcome, "refused")

    def test_whitespace_only_completion_is_empty(self):
        res = parse_stream("chat", [*event(10, chat_chunk("  \n", "stop")), *chat_done(20)])
        self.assertEqual(res.outcome, "empty")

    def test_unknown_finish_reason_is_not_copied_into_diagnostics(self):
        secret = "credential-canary"
        res = parse_stream("chat", [*event(10, chat_chunk("x", secret)), *chat_done(20)])
        self.assertEqual(res.outcome, "incomplete")
        self.assertNotIn(secret, res.error)
        self.assertNotIn(secret, res.finish_reason)


class ResponsesStream(unittest.TestCase):
    def test_completed_event_is_terminal_without_done_sentinel(self):
        lines = [
            *event(10, {"type": "response.created"}),
            *event(20, {"type": "response.output_text.delta", "delta": "one"}),
            *event(30, {"type": "response.output_text.delta", "delta": " two"}),
            *event(40, {"type": "response.output_text.done", "text": "one two"}),
            *completed(50),
        ]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "completed")
        self.assertEqual(res.output_text, "one two")
        self.assertEqual(res.itl_ns, [10 * MS])
        self.assertEqual(res.t_done_ns, 50 * MS)
        self.assertEqual(res.usage["completion_tokens"], 11)
        trailing = parse_stream("responses", [*lines, *chat_done(60)])
        self.assertEqual(trailing.t_done_ns, res.t_done_ns)
        self.assertEqual(trailing.outcome, "completed")

    def test_failed_and_incomplete_terminal_events_preserve_usage(self):
        for status, outcome in [("failed", "failed"), ("incomplete", "incomplete")]:
            with self.subTest(status=status):
                lines = [
                    *event(10, {"type": "response.output_text.delta", "delta": "partial"}),
                    *event(20, {"type": "response." + status, "response": {
                        "status": status, "usage": resp_usage(), "error": {"message": "failure"},
                        "incomplete_details": {"reason": "max_output_tokens"},
                    }}),
                ]
                res = parse_stream("responses", lines)
                self.assertEqual(res.outcome, outcome)
                self.assertEqual(res.output_text, "partial")
                self.assertEqual(res.usage["completion_tokens"], 11)
                self.assertEqual(res.t_done_ns, 20 * MS)

    def test_response_refusal_is_not_empty_success(self):
        res = parse_stream("responses", [*event(10, {"type": "response.refusal.delta", "delta": "No"}), *completed(20)])
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.usage["completion_tokens"], 11)

    def test_done_sentinel_alone_is_not_responses_completion(self):
        lines = [*event(10, {"type": "response.output_text.delta", "delta": "partial"}), *chat_done(20)]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "incomplete")
        self.assertIsNone(res.t_done_ns)

    def test_error_event_after_completion_fails_and_keeps_terminal_time(self):
        lines = [
            *event(10, {"type": "response.output_text.delta", "delta": "x"}),
            *completed(20),
            raw(30, b'event: error\ndata: {"message":"late failure"}\n\n'),
        ]
        res = parse_stream("responses", lines)
        self.assertEqual(res.outcome, "failed")
        self.assertEqual(res.t_done_ns, 20 * MS)
        self.assertEqual(res.usage["completion_tokens"], 11)
        self.assertTrue(res.reached_eof)

    def test_disconnect_after_completion_is_not_clean_eof(self):
        def disconnecting():
            yield from event(10, {"type": "response.output_text.delta", "delta": "x"})
            yield from completed(20)
            raise ConnectionResetError("late disconnect")

        res = parse_stream("responses", disconnecting())
        self.assertEqual(res.outcome, "failed")
        self.assertEqual(res.t_done_ns, 20 * MS)
        self.assertEqual(res.usage["completion_tokens"], 11)
        self.assertFalse(res.reached_eof)

    def test_remote_errors_and_incomplete_details_never_expose_messages(self):
        secret = "credential-canary"
        payloads = [
            {"type": "error", "error": {"message": secret}},
            {"type": "response.failed", "response": {"error": {"message": secret}}},
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": secret}}},
        ]
        for payload in payloads:
            with self.subTest(kind=payload["type"]):
                res = parse_stream("responses", event(10, payload))
                self.assertIn(res.outcome, ("failed", "incomplete"))
                self.assertNotIn(secret, res.error)
                self.assertNotIn(secret, res.finish_reason or "")


if __name__ == "__main__":
    unittest.main()
