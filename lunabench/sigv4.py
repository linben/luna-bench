"""AWS credential resolution and SigV4 request signing (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ALGORITHM = "AWS4-HMAC-SHA256"
REFRESH_MARGIN = timedelta(seconds=300)


@dataclass(frozen=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str | None = field(repr=False)
    expiration: datetime | None

    def __post_init__(self) -> None:
        values = (self.access_key, self.secret_key)
        if self.session_token is not None:
            values += (self.session_token,)
        for value in values:
            if not isinstance(value, str) or not value or not all("!" <= char <= "~" for char in value):
                raise ValueError("invalid AWS credential value")


def _parse_expiration(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class CredentialProvider:
    """Resolves credentials from env vars, then `aws configure export-credentials`; caches until near expiry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Credentials | None = None

    def get(self) -> Credentials:
        with self._lock:
            if self._cached is not None and not self._stale(self._cached):
                return self._cached
            self._cached = self._resolve()
            return self._cached

    @staticmethod
    def _stale(creds: Credentials) -> bool:
        if creds.expiration is None:
            return False
        return creds.expiration - datetime.now(timezone.utc) < REFRESH_MARGIN

    @staticmethod
    def _resolve() -> Credentials:
        ak = os.environ.get("AWS_ACCESS_KEY_ID")
        sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if ak or sk:
            if not ak or not sk:
                raise RuntimeError("both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set")
            return Credentials(ak, sk, os.environ.get("AWS_SESSION_TOKEN") or None, None)
        try:
            proc = subprocess.run(
                ["aws", "configure", "export-credentials", "--format", "process"],
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            )
            data = json.loads(proc.stdout)
            if not isinstance(data, dict):
                raise ValueError("invalid credential document")
            return Credentials(
                data["AccessKeyId"],
                data["SecretAccessKey"],
                data.get("SessionToken") or None,
                _parse_expiration(data.get("Expiration")),
            )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError(
                "no AWS credentials: set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or configure the aws CLI"
            ) from None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _canonical_headers(headers: dict[str, str]) -> list[tuple[str, str]]:
    return sorted((k.lower(), " ".join(v.split())) for k, v in headers.items())


def _all_headers(
    url_host: str,
    headers: dict[str, str],
    payload_hash: str,
    amz_date: str,
    session_token: str | None,
) -> dict[str, str]:
    out = {key.lower(): value for key, value in headers.items()}
    out.pop("authorization", None)
    out.pop("x-amz-security-token", None)
    out["host"] = url_host
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    if session_token:
        out["x-amz-security-token"] = session_token
    return out


def canonical_request(
    method: str,
    url_host: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    amz_date: str,
    session_token: str | None = None,
) -> str:
    """Canonical request over all given headers plus host/x-amz-date/x-amz-content-sha256[/x-amz-security-token]."""
    payload_hash = _sha256_hex(body)
    canon = _canonical_headers(_all_headers(url_host, headers, payload_hash, amz_date, session_token))
    return _canonical_request(method, path, canon, payload_hash)


def _canonical_request(method: str, path: str, canon: list[tuple[str, str]], payload_hash: str) -> str:
    signed_names = ";".join(k for k, _ in canon)
    return "\n".join(
        [
            method,
            path,
            "",  # query string always empty in this suite
            "".join(f"{k}:{v}\n" for k, v in canon),
            signed_names,
            payload_hash,
        ]
    )


def string_to_sign(canonical: str, amz_date: str, scope: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, _sha256_hex(canonical.encode("utf-8"))])


def _signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def sign(
    method: str,
    url_host: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    region: str,
    service: str,
    creds: Credentials,
    now: datetime,
) -> dict[str, str]:
    """Return the full header dict to send: input headers plus x-amz-* and authorization."""
    amz_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    payload_hash = _sha256_hex(body)
    full = _all_headers(url_host, headers, payload_hash, amz_date, creds.session_token)
    canon = _canonical_headers(full)
    canonical = _canonical_request(method, path, canon, payload_hash)
    sts = string_to_sign(canonical, amz_date, scope)
    signature = hmac.new(
        _signing_key(creds.secret_key, date, region, service), sts.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    signed_names = ";".join(k for k, _ in canon)
    full["authorization"] = (
        f"{ALGORITHM} Credential={creds.access_key}/{scope}, SignedHeaders={signed_names}, Signature={signature}"
    )
    return full
