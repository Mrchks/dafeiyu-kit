"""配置时检测：直接请求服务商的检测和验证。全部用假的 HTTP 响应，不联网；检查失败原因的分类，以及结果中不出现密钥。"""

import io
import json
import socket
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import modelprobe as mp  # noqa: E402

KEY = "sk-TEST-SECRET-1234567890"


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode("utf-8") if not isinstance(data, bytes) else data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, text=""):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(text.encode("utf-8")))


class ProbeTests(unittest.TestCase):
    def run_probe(self, effect, protocol="openai", **kw):
        seen = []

        def fake(req, timeout=None):
            seen.append(req)
            if isinstance(effect, Exception):
                raise effect
            return effect
        with patch.object(mp.urllib.request, "urlopen", fake):
            r = mp.probe(protocol, "https://api.example.com/v1", KEY, **kw)
        self.assertNotIn(KEY, json.dumps(r, ensure_ascii=False))
        return r, seen

    def test_openai_list(self):
        r, seen = self.run_probe(FakeResponse({"data": [{"id": "m1", "supports_image_in": True, "supports_reasoning": True}, {"id": "m2"}]}))
        self.assertTrue(r["ok"]) ; self.assertTrue(r["listed"])
        self.assertEqual(r["models"][0], {"id": "m1", "vision": True, "reasoning": True})
        self.assertEqual(seen[0].full_url, "https://api.example.com/v1/models")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer " + KEY)

    def test_anthropic_and_gemini_endpoints(self):
        r, seen = self.run_probe(FakeResponse({"data": [{"id": "claude-haiku-5-5"}]}), "anthropic")
        self.assertEqual(seen[0].full_url, "https://api.example.com/v1/models?limit=1000")
        self.assertEqual(seen[0].get_header("X-api-key"), KEY)
        self.assertEqual(seen[0].get_header("Anthropic-version"), mp.ANTHROPIC_VERSION)
        r, seen = self.run_probe(FakeResponse({"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                                                          {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]}), "gemini")
        self.assertEqual([m["id"] for m in r["models"]], ["gemini-3.8-flash"])
        self.assertIn("/v1beta/models", seen[0].full_url)
        self.assertEqual(seen[0].get_header("X-goog-api-key"), KEY)

    def test_failure_reasons_are_distinct(self):
        cases = [(http_error(401, '{"error":"invalid api key"}'), "auth"),
                 (http_error(402, "insufficient balance"), "quota"),
                 (http_error(429, "rate limit"), "quota"),
                 (http_error(503), "server"),
                 (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "network"),
                 (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
                 (socket.timeout("timed out"), "timeout"),
                 (urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")), "tls"),
                 (urllib.error.URLError(OSError("Tunnel connection failed: 407 Proxy Authentication Required")), "tls"),
                 (urllib.error.URLError(OSError("Tunnel connection failed: 502 Bad Gateway")), "network")]
        for effect, want in cases:
            with self.subTest(want=want):
                r, _ = self.run_probe(effect)
                self.assertFalse(r["ok"])
                self.assertEqual(r["status"], want)
                self.assertEqual(r["message"], mp.MESSAGES[want])

    def test_missing_list_endpoint(self):
        r, _ = self.run_probe(http_error(404, "<html>not found</html>"))
        self.assertTrue(r["ok"]) ; self.assertFalse(r["listed"]) ; self.assertEqual(r["status"], "nolist")
        r, _ = self.run_probe(http_error(404, "not found"), expect_list=True)          # 官方提供列表的服务商：说明地址或协议不对
        self.assertFalse(r["ok"]) ; self.assertEqual(r["status"], "notfound")

    def test_same_connection_not_probed_twice_at_once(self):
        fp = mp._fingerprint("openai", "https://api.example.com/v1", KEY)
        mp._inflight.add(fp)
        try:
            r, seen = self.run_probe(FakeResponse({"data": []}))
        finally:
            mp._inflight.discard(fp)
        self.assertEqual(r["status"], "busy") ; self.assertEqual(seen, [])

    def test_blank_inputs(self):
        self.assertEqual(mp.probe("openai", "https://a/v1", "")["status"], "auth")
        self.assertEqual(mp.probe("openai", "", KEY)["status"], "notfound")


class VerifyTests(unittest.TestCase):
    def run_verify(self, effect, protocol="openai", extra=None):
        seen = []

        def fake(req, timeout=None):
            seen.append(req)
            if isinstance(effect, Exception):
                raise effect
            return effect
        with patch.object(mp.urllib.request, "urlopen", fake):
            r = mp.verify(protocol, "https://api.example.com/v1", KEY, "m1", extra)
        self.assertNotIn(KEY, json.dumps(r, ensure_ascii=False))
        return r, seen

    def test_success_sends_minimal_request_with_thinking_parameters(self):
        r, seen = self.run_verify(FakeResponse({"choices": []}), extra={"thinking": {"type": "disabled"}})
        self.assertTrue(r["ok"])
        body = json.loads(seen[0].data)
        self.assertEqual(seen[0].full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(body["max_tokens"], 16)
        self.assertEqual(body["thinking"], {"type": "disabled"})

    def test_responses_converts_effort(self):
        _, seen = self.run_verify(FakeResponse({}), "responses", {"reasoning_effort": "low"})
        body = json.loads(seen[0].data)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning_effort", body)

    def test_model_and_rejection_reasons(self):
        r, _ = self.run_verify(http_error(400, '{"error":{"code":"model_not_found","message":"The model does not exist"}}'))
        self.assertEqual(r["status"], "model")
        r, _ = self.run_verify(http_error(404, '{"error":"model not found"}'))
        self.assertEqual(r["status"], "model")
        r, _ = self.run_verify(http_error(400, '{"error":"unsupported parameter: thinking"}'))
        self.assertEqual(r["status"], "rejected")
        r, _ = self.run_verify(http_error(404, "<html></html>"))
        self.assertEqual(r["status"], "notfound")


if __name__ == "__main__":
    unittest.main()
