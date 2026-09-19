import json
import os
import subprocess
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from lunabench import sigv4
from lunabench.targets import TargetUnavailable, auth_headers, build_targets

# awslabs/aws-c-auth test vector: v4/post-x-www-form-urlencoded
HOST = "example.amazonaws.com"
HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "13"}
BODY = b"Param1=value1"
NOW = datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc)
AMZ_DATE = "20150830T123600Z"
CREDS = sigv4.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", None, None)

EXPECTED_CANONICAL = (
    "POST\n"
    "/\n"
    "\n"
    "content-length:13\n"
    "content-type:application/x-www-form-urlencoded\n"
    "host:example.amazonaws.com\n"
    "x-amz-content-sha256:9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e\n"
    "x-amz-date:20150830T123600Z\n"
    "\n"
    "content-length;content-type;host;x-amz-content-sha256;x-amz-date\n"
    "9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e"
)
EXPECTED_STS = (
    "AWS4-HMAC-SHA256\n"
    "20150830T123600Z\n"
    "20150830/us-east-1/service/aws4_request\n"
    "b1edd1d03544c25390e32085d55b57acc9a3961bb59415ff86c45c3d89d16cfb"
)
EXPECTED_SIG = "d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b"


class SigV4Vector(unittest.TestCase):
    def test_canonical_request(self):
        self.assertEqual(sigv4.canonical_request("POST", HOST, "/", HEADERS, BODY, AMZ_DATE), EXPECTED_CANONICAL)

    def test_string_to_sign(self):
        canon = sigv4.canonical_request("POST", HOST, "/", HEADERS, BODY, AMZ_DATE)
        self.assertEqual(sigv4.string_to_sign(canon, AMZ_DATE, "20150830/us-east-1/service/aws4_request"), EXPECTED_STS)

    def test_signature(self):
        hdrs = sigv4.sign("POST", HOST, "/", HEADERS, BODY, "us-east-1", "service", CREDS, NOW)
        auth = hdrs["authorization"]
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "))
        self.assertIn("SignedHeaders=content-length;content-type;host;x-amz-content-sha256;x-amz-date, ", auth)
        self.assertTrue(auth.endswith(f"Signature={EXPECTED_SIG}"))
        self.assertEqual(hdrs["x-amz-date"], AMZ_DATE)
        self.assertEqual(hdrs["host"], HOST)
        self.assertNotIn("x-amz-security-token", hdrs)

    def test_session_token_is_signed(self):
        creds = sigv4.Credentials("AKIDEXAMPLE", "secret", "tok", None)
        hdrs = sigv4.sign("POST", HOST, "/openai/v1/chat/completions", {"content-type": "application/json"}, b"{}", "us-east-1", "bedrock", creds, NOW)
        self.assertEqual(hdrs["x-amz-security-token"], "tok")
        self.assertIn("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token,", hdrs["authorization"])

    def test_equivalent_instants_produce_the_same_signature(self):
        shifted = NOW.astimezone(timezone(timedelta(hours=9)))
        actual = sigv4.sign("POST", HOST, "/", HEADERS, BODY, "us-east-1", "service", CREDS, shifted)
        self.assertTrue(actual["authorization"].endswith(f"Signature={EXPECTED_SIG}"))

    def test_resigning_mixed_case_headers_replaces_old_authentication(self):
        original = sigv4.sign("POST", HOST, "/", HEADERS, BODY, "us-east-1", "service", CREDS, NOW)
        previous = {key.title(): value for key, value in original.items()}
        previous["X-Amz-Security-Token"] = "expired-token"
        actual = sigv4.sign("POST", HOST, "/", previous, BODY, "us-east-1", "service", CREDS, NOW)
        self.assertEqual(actual, original)


class CredentialSafety(unittest.TestCase):
    def test_credentials_do_not_expose_secrets_in_repr(self):
        creds = sigv4.Credentials("access-canary", "secret-canary", "session-canary", None)
        for secret in (creds.access_key, creds.secret_key, creds.session_token):
            self.assertNotIn(secret, repr(creds))

    def test_partial_environment_does_not_silently_select_another_identity(self):
        fallback = SimpleNamespace(stdout=json.dumps({"AccessKeyId": "other", "SecretAccessKey": "other"}))
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "access-canary"}, clear=True), patch(
            "lunabench.sigv4.subprocess.run", return_value=fallback
        ):
            with self.assertRaises(RuntimeError):
                sigv4.CredentialProvider().get()

    def test_invalid_credential_document_is_generic_and_hides_traceback_content(self):
        secret = "credential-canary"
        values = [
            [secret],
            {"AccessKeyId": "access", "SecretAccessKey": secret, "Expiration": secret},
            {"AccessKeyId": "access", "SecretAccessKey": [secret]},
        ]
        for value in values:
            with self.subTest(document_type=type(value).__name__):
                with patch.dict(os.environ, {}, clear=True), patch(
                    "lunabench.sigv4.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(value))
                ):
                    try:
                        sigv4.CredentialProvider().get()
                    except RuntimeError:
                        self.assertNotIn(secret, traceback.format_exc())
                    else:
                        self.fail("invalid credentials were accepted")

    def test_credential_process_timeout_is_generic(self):
        secret = "credential-canary"
        timeout = subprocess.TimeoutExpired(["aws"], 30, output=secret, stderr=secret)
        with patch.dict(os.environ, {}, clear=True), patch("lunabench.sigv4.subprocess.run", side_effect=timeout):
            with self.assertRaises(RuntimeError) as raised:
                sigv4.CredentialProvider().get()
        self.assertNotIn(secret, str(raised.exception))

    def test_invalid_bearer_values_fail_before_http_can_echo_them(self):
        target = build_targets("us-east-1", "unused")["openai"]
        for key in ("credential-canary\r\nInjected: value", "credential-canary\u2603"):
            with self.subTest(kind="newline" if "\n" in key else "unicode"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": key}):
                    with self.assertRaises(TargetUnavailable) as raised:
                        auth_headers(target, "bearer", b"{}", target.path("chat"), "us-east-1", None)
                self.assertNotIn("credential-canary", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
