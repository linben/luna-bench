"""Timed HTTPS transport and SSE stream parsing. Produces JSONL-friendly Sample dicts."""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter_ns
from typing import Iterable, Iterator

from .targets import Target

_DEFAULT_CTX = ssl.create_default_context()

RETRYABLE_ON_REUSE = (
    BrokenPipeError,
    ConnectionResetError,
    http.client.RemoteDisconnected,
    ssl.SSLEOFError,
)

STREAM_ERRORS = (OSError, ssl.SSLError, http.client.HTTPException)


SAMPLE_RESULT_KEYS = (
    "schema_version",
    "outcome",
    "finish_reason",
    "reused_connection",
    "reconnected",
    "reconnect_error",
    "reconnect_ms",
    "status",
    "error",
    "request_id",
    "retry_after",
    "t_connect_ms",
    "t_tls_ms",
    "ttfb_ms",
    "ttft_ms",
    "t_last_text_ms",
    "t_done_ms",
    "t_eof_ms",
    "total_ms",
    "itl_ms",
    "max_stall_ms",
    "stall_count",
    "n_content_chunks",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "text_delivery_chars_per_s",
    "output_chars",
    "output_text",
)



class TimedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that records TCP-connect and TLS-handshake completion timestamps."""

    def __init__(self, host: str, timeout: float) -> None:
        super().__init__(host, timeout=timeout, context=_DEFAULT_CTX)
        self.t_connect_start_ns: int | None = None
        self.t_tcp_ns: int | None = None
        self.t_tls_ns: int | None = None

    def connect(self) -> None:
        self.t_connect_start_ns = perf_counter_ns()
        self.t_tcp_ns = self.t_tls_ns = None
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = sock
        self.t_tcp_ns = perf_counter_ns()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise
        self.t_tls_ns = perf_counter_ns()


class ConnectionPool:
    """Per-thread keep-alive connections, with a registry for post-drain cleanup."""

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: set[TimedHTTPSConnection] = set()
        self._closed = False

    def _conns(self) -> dict[str, TimedHTTPSConnection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        return conns

    def acquire(self, host: str, fresh: bool) -> tuple[TimedHTTPSConnection, bool]:
        """Return (connection, reused). A fresh connection is not yet connected."""
        conns = self._conns()
        with self._lock:
            if self._closed:
                raise RuntimeError("connection pool is closed")
            conn = conns.get(host)
            if conn is not None and not fresh and conn.sock is not None:
                return conn, True
            if conn is not None:
                self._connections.discard(conn)
                conn.close()
            conn = TimedHTTPSConnection(host, self.timeout)
            conns[host] = conn
            self._connections.add(conn)
            return conn, False

    def discard(self, host: str) -> None:
        conn = self._conns().pop(host, None)
        if conn is not None:
            with self._lock:
                self._connections.discard(conn)
            conn.close()

    def close_all(self) -> None:
        """Permanently close the pool after all request workers have drained."""
        with self._lock:
            self._closed = True
            conns = list(self._connections)
            self._connections.clear()
        for conn in conns:
            conn.close()
        self._conns().clear()


@dataclass
class StreamResult:
    # These are text-event timestamps, never model-token timestamps.
    t_first_token_ns: int | None = None
    token_times_ns: list[int] = field(default_factory=list)
    first_delta_chars: int = 0
    output_text: str = ""
    usage: dict | None = None
    t_done_ns: int | None = None
    reached_eof: bool = False
    t_eof_ns: int | None = None
    outcome: str = "incomplete"
    finish_reason: str | None = None
    error: str | None = None

    @property
    def itl_ns(self) -> list[int]:
        t = self.token_times_ns
        return [b - a for a, b in zip(t, t[1:])]


def _int(d: dict | None, key: str) -> int | None:
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    return v if type(v) is int and v >= 0 else None


def normalize_usage(api: str, usage: dict) -> dict:
    if api == "chat":
        return {
            "prompt_tokens": _int(usage, "prompt_tokens"),
            "completion_tokens": _int(usage, "completion_tokens"),
            "reasoning_tokens": _int(usage.get("completion_tokens_details"), "reasoning_tokens"),
            "cached_tokens": _int(usage.get("prompt_tokens_details"), "cached_tokens"),
        }
    return {
        "prompt_tokens": _int(usage, "input_tokens"),
        "completion_tokens": _int(usage, "output_tokens"),
        "reasoning_tokens": _int(usage.get("output_tokens_details"), "reasoning_tokens"),
        "cached_tokens": _int(usage.get("input_tokens_details"), "cached_tokens"),
    }


class _StreamProtocolError(ValueError):
    """Locally generated diagnostics that never include peer-controlled content."""


# Safety bounds protect the benchmark process from malformed or unbounded peers.
_MAX_EVENT_BYTES = 1 << 20
_MAX_OUTPUT_CHARS = 4 << 20
_MAX_TEXT_EVENTS = 100_000
_LINE_END = re.compile(br"\r\n|\r|\n")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_RETRY_AFTER = re.compile(r"(?:[0-9]{1,10}|[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT)")


def _sse_lines(chunks: Iterable[tuple[int, bytes]]) -> Iterator[tuple[int, bytes]]:
    """Split UTF-8 SSE bytes on CRLF, LF, or CR, even across network reads."""
    pending = bytearray()
    skip_lf = False
    first = True
    for timestamp, chunk in chunks:
        start = 1 if skip_lf and chunk.startswith(b"\n") else 0
        if not chunk:
            continue
        skip_lf = chunk.endswith(b"\r")
        for match in _LINE_END.finditer(chunk, start):
            if len(pending) + match.start() - start > _MAX_EVENT_BYTES:
                raise _StreamProtocolError("SSE line exceeds size limit")
            pending.extend(chunk[start:match.start()])
            line = bytes(pending)
            pending.clear()
            if first:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first = False
            yield timestamp, line
            start = match.end()
        if len(pending) + len(chunk) - start > _MAX_EVENT_BYTES:
            raise _StreamProtocolError("SSE line exceeds size limit")
        pending.extend(chunk[start:])
    # SSE does not dispatch an unfinished line or event at EOF.


def _sse_events(chunks: Iterable[tuple[int, bytes]]) -> Iterator[tuple[int, str, str]]:
    data: list[str] = []
    event = ""
    size = 0
    for timestamp, raw in _sse_lines(chunks):
        if not raw:
            if data:
                yield timestamp, event, "\n".join(data)
            data.clear()
            event = ""
            size = 0
            continue
        size += len(raw)
        if size > _MAX_EVENT_BYTES:
            raise _StreamProtocolError("SSE event exceeds size limit")
        if raw.startswith(b":"):
            continue
        name, _, value = raw.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if name == b"data":
            data.append(value.decode("utf-8", "replace"))
        elif name == b"event":
            event = value.decode("utf-8", "replace")




def parse_stream(api: str, lines: Iterable[tuple[int, bytes]]) -> StreamResult:
    """Parse timestamped SSE bytes; dispatch only at blank-line delimiters.

    A successful Chat stream needs finish_reason=stop and [DONE]. Responses needs
    response.completed. Both require non-whitespace output text and clean EOF.
    Errors after the terminal event still fail the transport, retaining t_done.
    """
    res = StreamResult()
    parts: list[str] = []
    chars = 0
    terminal_success = False
    wire_done = False
    refused = False
    incomplete_reason: str | None = None

    def usage_from(value: object) -> None:
        if isinstance(value, dict):
            normalized = normalize_usage(api, value)
            if res.usage is None:
                res.usage = normalized
            else:
                res.usage.update({key: val for key, val in normalized.items() if val is not None})

    def append_text(timestamp: int, text: object) -> None:
        nonlocal chars
        if text is None or text == "":
            return
        if not isinstance(text, str):
            raise _StreamProtocolError("non-string output text delta")
        if res.t_done_ns is not None or wire_done:
            raise _StreamProtocolError("output text after terminal event")
        if chars + len(text) > _MAX_OUTPUT_CHARS or len(parts) >= _MAX_TEXT_EVENTS:
            raise _StreamProtocolError("streamed output exceeds size limit")
        if res.t_first_token_ns is None:
            res.t_first_token_ns = timestamp
            res.first_delta_chars = len(text)
        parts.append(text)
        chars += len(text)
        res.token_times_ns.append(timestamp)

    try:
        for timestamp, event, payload in _sse_events(lines):
            if payload == "[DONE]":
                wire_done = True
                if api == "chat":
                    if res.t_done_ns is None:
                        res.t_done_ns = timestamp
                    terminal_success = res.finish_reason == "stop"
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise _StreamProtocolError("bad SSE JSON") from exc
            if not isinstance(obj, dict):
                raise _StreamProtocolError("SSE payload is not an object")
            response = obj.get("response")
            if not isinstance(response, dict):
                response = {}
            usage_from(obj.get("usage"))
            usage_from(response.get("usage"))
            kind = obj.get("type") or event
            if event == "error" or kind == "error" or obj.get("error") is not None:
                res.error = "remote_stream_error"
                if res.t_done_ns is None:
                    res.t_done_ns = timestamp
                continue
            if api == "chat":
                choices = obj.get("choices", [])
                if not isinstance(choices, list):
                    raise _StreamProtocolError("invalid Chat choices")
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise _StreamProtocolError("invalid Chat choice")
                    if choice.get("index", 0) != 0:
                        raise _StreamProtocolError("unexpected additional Chat choice")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise _StreamProtocolError("invalid Chat delta")
                    if delta.get("refusal"):
                        refused = True
                    if res.finish_reason is not None and delta.get("content"):
                        raise _StreamProtocolError("output text after Chat finish_reason")
                    append_text(timestamp, delta.get("content"))
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if not isinstance(reason, str):
                            raise _StreamProtocolError("invalid Chat finish_reason")
                        if reason not in ("stop", "length", "content_filter", "tool_calls", "function_call"):
                            reason = "unknown_finish_reason"
                        if wire_done:
                            raise _StreamProtocolError("Chat finish_reason after [DONE]")
                        if res.finish_reason is not None and res.finish_reason != reason:
                            raise _StreamProtocolError("conflicting Chat finish_reason")
                        res.finish_reason = reason
                        if reason == "content_filter":
                            refused = True
                        elif reason != "stop":
                            incomplete_reason = reason
                continue
            if kind == "response.output_text.delta":
                append_text(timestamp, obj.get("delta"))
            elif kind in ("response.refusal.delta", "response.refusal.done"):
                refused = True
            elif kind in ("response.completed", "response.failed", "response.incomplete"):
                status = kind.removeprefix("response.")
                if response.get("status") not in (None, status):
                    raise _StreamProtocolError("conflicting Responses terminal status")
                if res.t_done_ns is not None:
                    raise _StreamProtocolError("duplicate Responses terminal event")
                res.t_done_ns = timestamp
                res.finish_reason = status
                output = response.get("output") or []
                if not isinstance(output, list):
                    raise _StreamProtocolError("invalid Responses output")
                for item in output:
                    if not isinstance(item, dict):
                        raise _StreamProtocolError("invalid Responses output item")
                    content_parts = item.get("content") or []
                    if not isinstance(content_parts, list):
                        raise _StreamProtocolError("invalid Responses content")
                    for content in content_parts:
                        if isinstance(content, dict) and content.get("type") == "refusal":
                            refused = True
                if status == "completed":
                    terminal_success = True
                elif status == "failed":
                    res.error = "response.failed"
                else:
                    details = response.get("incomplete_details") or {}
                    reason = details.get("reason") if isinstance(details, dict) else None
                    incomplete_reason = reason if reason in ("max_output_tokens", "content_filter") else "response.incomplete"
                    res.finish_reason = incomplete_reason
                    if reason == "content_filter":
                        refused = True
        res.reached_eof = True
        res.t_eof_ns = perf_counter_ns()
    except _StreamProtocolError as exc:
        res.error = str(exc)
    except (ValueError, RecursionError):
        res.error = "invalid_sse_json"
    except STREAM_ERRORS as exc:
        # HTTP exceptions can embed remote status lines, and OS errors can
        # include request values. Persist the category, never their arguments.
        res.error = type(exc).__name__
    res.output_text = "".join(parts)
    if res.error is not None:
        res.outcome = "failed"
    elif refused:
        res.outcome = "refused"
        res.error = "model_refusal"
    elif incomplete_reason is not None:
        res.outcome = "incomplete"
        res.error = incomplete_reason
    elif not terminal_success:
        res.outcome = "incomplete"
        res.error = "missing_completion"
    elif not res.output_text.strip():
        res.outcome = "empty"
        res.error = "empty_output"
    else:
        res.outcome = "completed"
    return res


def _timed_chunks(resp: http.client.HTTPResponse) -> Iterator[tuple[int, bytes]]:
    while True:
        chunk = resp.read1(64 * 1024)
        if not chunk:
            if resp.length not in (None, 0):
                raise http.client.IncompleteRead(b"", resp.length)
            return
        yield perf_counter_ns(), chunk


def _ms(ns: int | None) -> float | None:
    return None if ns is None else round(ns / 1e6, 3)


def execute(
    target: Target,
    api: str,
    body: bytes,
    headers: dict[str, str],
    pool: ConnectionPool,
    fresh: bool,
) -> dict:
    """POST once logically, including at most one stale-connection reconnect."""
    path = target.path(api)
    sample: dict = {k: None for k in SAMPLE_RESULT_KEYS}
    sample.update(
        schema_version=2,
        outcome="failed",
        itl_ms=[],
        stall_count=0,
        n_content_chunks=0,
        output_chars=0,
        output_text="",
        reconnected=False,
        ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    )
    t0 = perf_counter_ns()
    conn: TimedHTTPSConnection | None = None
    resp: http.client.HTTPResponse | None = None
    try:
        conn, reused = pool.acquire(target.host, fresh)
        sample["reused_connection"] = reused
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
        except RETRYABLE_ON_REUSE as exc:
            if not reused:
                raise
            # This is still the same logical request: never reset t0. A stale
            # keep-alive retry can replay an accepted POST; expose the ambiguity.
            sample["reconnected"] = True
            sample["reconnect_error"] = type(exc).__name__
            sample["reconnect_ms"] = _ms(perf_counter_ns() - t0)
            pool.discard(target.host)
            conn, _ = pool.acquire(target.host, fresh=True)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
        sample["ttfb_ms"] = _ms(perf_counter_ns() - t0)
        sample["status"] = resp.status
        request_id = resp.getheader("x-amzn-requestid") or resp.getheader("x-request-id")
        retry_after = resp.getheader("retry-after")
        sample["request_id"] = request_id if request_id and _REQUEST_ID.fullmatch(request_id) else None
        sample["retry_after"] = retry_after if retry_after and _RETRY_AFTER.fullmatch(retry_after) else None

        if resp.status != 200:
            # Error bodies can echo credentials or prompts. Do not read or
            # persist them; this connection will not be reused.
            sample["error"] = f"HTTP {resp.status}"
            sample["total_ms"] = _ms(perf_counter_ns() - t0)
            pool.discard(target.host)
            return sample

        res = parse_stream(api, _timed_chunks(resp))
        sample["total_ms"] = _ms(perf_counter_ns() - t0)
        if res.t_eof_ns is not None:
            sample["t_eof_ms"] = _ms(res.t_eof_ns - t0)
        sample["outcome"] = res.outcome
        sample["finish_reason"] = res.finish_reason
        sample["error"] = res.error
        sample["ttft_ms"] = _ms(res.t_first_token_ns - t0) if res.t_first_token_ns is not None else None
        sample["t_done_ms"] = _ms(res.t_done_ns - t0) if res.t_done_ns is not None else None
        gaps = res.itl_ns
        sample["itl_ms"] = [_ms(gap) for gap in gaps]
        sample["max_stall_ms"] = _ms(max(gaps)) if gaps else None
        sample["stall_count"] = sum(gap > 100_000_000 for gap in gaps)
        sample["n_content_chunks"] = len(res.token_times_ns)
        sample["output_text"] = res.output_text
        sample["output_chars"] = len(res.output_text)
        if res.token_times_ns:
            last = res.token_times_ns[-1]
            sample["t_last_text_ms"] = _ms(last - t0)
            span = last - res.token_times_ns[0]
            if span > 0:
                sample["text_delivery_chars_per_s"] = round(
                    (len(res.output_text) - res.first_delta_chars) / (span / 1e9), 3
                )
        if res.usage is not None:
            sample.update(res.usage)
        if res.error or resp.will_close:
            pool.discard(target.host)
        return sample
    except STREAM_ERRORS as exc:
        sample["error"] = type(exc).__name__
        sample["total_ms"] = _ms(perf_counter_ns() - t0)
        pool.discard(target.host)
        return sample
    finally:
        if resp is not None:
            resp.close()
        # Capture a successful TCP phase even if TLS or the subsequent read fails.
        if conn is not None and conn.t_connect_start_ns is not None and conn.t_connect_start_ns >= t0:
            if conn.t_tcp_ns is not None:
                sample["t_connect_ms"] = _ms(conn.t_tcp_ns - conn.t_connect_start_ns)
                if conn.t_tls_ns is not None:
                    sample["t_tls_ms"] = _ms(conn.t_tls_ns - conn.t_tcp_ns)
