"""The transport layer: retries, backoff and error translation.

No sockets are opened. urlopen is replaced, which keeps the suite offline and
makes failure modes (429 then success, 401, 404, 400) trivial to assert.
"""
import io
import json
import unittest
import urllib.error
from typing import Any, Dict, List, Optional

from agit import model as model_mod
from agit.config import Config
from agit.model import Cassette, ModelClient, ModelError, ToolCall, wrap_response


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.test/v1/chat/completions", code, "err", {},
        io.BytesIO(body.encode("utf-8")))


class Transport:
    """Scripted replacement for urllib.request.urlopen."""

    def __init__(self, outcomes: List[Any]):
        self.outcomes = list(outcomes)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "headers": dict(request.headers),
            "body": json.loads(request.data.decode("utf-8")),
            "timeout": timeout,
        })
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode("utf-8"))


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.real_urlopen = model_mod.urllib.request.urlopen
        self.real_sleep = model_mod.time.sleep
        model_mod.time.sleep = lambda seconds: None     # no real backoff waits
        self.addCleanup(self._restore)

    def _restore(self):
        model_mod.urllib.request.urlopen = self.real_urlopen
        model_mod.time.sleep = self.real_sleep

    def client(self, outcomes, **cfg_kwargs) -> ModelClient:
        transport = Transport(outcomes)
        model_mod.urllib.request.urlopen = transport
        cfg = Config(provider="openai-compatible", base_url="https://api.test/v1",
                     api_key="k-secret", model="test-model", **cfg_kwargs)
        client = ModelClient(cfg, cassette=None)
        self.transport = transport
        return client

    def ask(self, client) -> Any:
        return client.complete([{"role": "user", "content": "hi"}], tools=None)


class TestRequestShape(HttpCase):
    def test_posts_to_chat_completions_with_bearer_auth(self):
        client = self.client([wrap_response("hello")])
        reply = self.ask(client)
        call = self.transport.calls[0]
        self.assertEqual(call["url"], "https://api.test/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer k-secret")
        self.assertEqual(call["body"]["model"], "test-model")
        self.assertEqual(call["body"]["messages"][0]["content"], "hi")
        self.assertEqual(reply.text, "hello")
        self.assertEqual(client.network_calls, 1)

    def test_tools_are_sent_only_when_native_tools_are_on(self):
        client = self.client([wrap_response("x")])
        client.complete([{"role": "user", "content": "hi"}],
                        tools=[{"type": "function", "function": {"name": "f"}}])
        self.assertIn("tools", self.transport.calls[0]["body"])

        client = self.client([wrap_response("x")], native_tools=False)
        client.complete([{"role": "user", "content": "hi"}],
                        tools=[{"type": "function", "function": {"name": "f"}}])
        self.assertNotIn("tools", self.transport.calls[0]["body"])

    def test_no_key_means_no_auth_header(self):
        transport = Transport([wrap_response("x")])
        model_mod.urllib.request.urlopen = transport
        cfg = Config(provider="ollama", base_url="http://localhost:11434/v1",
                     model="qwen2.5-coder:7b")
        ModelClient(cfg, cassette=None).complete([{"role": "user", "content": "hi"}], None)
        self.assertNotIn("Authorization", transport.calls[0]["headers"])


class TestRetries(HttpCase):
    def test_rate_limit_is_retried_then_succeeds(self):
        client = self.client([http_error(429, "slow down"),
                              http_error(503, "unavailable"),
                              wrap_response("finally")])
        reply = self.ask(client)
        self.assertEqual(reply.text, "finally")
        self.assertEqual(len(self.transport.calls), 3)

    def test_network_error_is_retried(self):
        client = self.client([urllib.error.URLError("connection reset"),
                              wrap_response("ok")])
        self.assertEqual(self.ask(client).text, "ok")
        self.assertEqual(len(self.transport.calls), 2)

    def test_retries_eventually_give_up_with_advice(self):
        client = self.client([http_error(429, "nope")] * 4)
        with self.assertRaises(ModelError) as caught:
            self.ask(client)
        self.assertIn("rate limited", str(caught.exception))
        self.assertEqual(len(self.transport.calls), 4)

    def test_unreachable_host_explains_itself(self):
        client = self.client([urllib.error.URLError("nodename nor servname provided")] * 4)
        with self.assertRaises(ModelError) as caught:
            self.ask(client)
        message = str(caught.exception)
        self.assertIn("cannot reach", message)
        self.assertIn("agit doctor", message)


class TestErrorTranslation(HttpCase):
    def test_401_points_at_the_key(self):
        client = self.client([http_error(401, "bad token")])
        with self.assertRaises(ModelError) as caught:
            self.ask(client)
        self.assertIn("credentials", str(caught.exception))

    def test_404_points_at_url_and_model(self):
        client = self.client([http_error(404, "not found")])
        with self.assertRaises(ModelError) as caught:
            self.ask(client)
        message = str(caught.exception)
        self.assertIn("base_url", message)
        self.assertIn("test-model", message)

    def test_400_suggests_disabling_native_tools(self):
        client = self.client([http_error(400, "tools unsupported")])
        with self.assertRaises(ModelError) as caught:
            self.ask(client)
        self.assertIn("--no-native-tools", str(caught.exception))

    def test_non_json_body_is_reported_clearly(self):
        class Garbage(Transport):
            def __call__(self, request, timeout=None):
                self.calls.append({"url": request.full_url})
                return FakeResponse(b"<html>gateway timeout</html>")

        transport = Garbage([])
        model_mod.urllib.request.urlopen = transport
        cfg = Config(provider="openai-compatible", base_url="https://api.test/v1",
                     api_key="k", model="m")
        with self.assertRaises(ModelError) as caught:
            ModelClient(cfg, cassette=None).complete([{"role": "user", "content": "x"}], None)
        self.assertIn("non-JSON", str(caught.exception))

    def test_missing_base_url_fails_before_any_request(self):
        cfg = Config(provider="openai-compatible", base_url="", model="m")
        with self.assertRaises(ModelError) as caught:
            ModelClient(cfg, cassette=None).complete([{"role": "user", "content": "x"}], None)
        self.assertIn("no API base URL", str(caught.exception))


class TestRecordingLiveCalls(HttpCase):
    def test_a_live_call_is_written_to_the_cassette(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="agit-http-"), "cassette.json")
        transport = Transport([wrap_response("recorded",
                                             ToolCall("read_file", {"path": "a.py"}))])
        model_mod.urllib.request.urlopen = transport
        cfg = Config(provider="openai-compatible", base_url="https://api.test/v1",
                     api_key="k", model="m")
        cassette = Cassette(path)
        client = ModelClient(cfg, cassette=cassette)
        reply = client.complete([{"role": "user", "content": "hi"}], None)
        cassette.save()
        self.assertEqual(reply.call.name, "read_file")
        self.assertEqual(len(Cassette(path)), 1)

        # Replaying the identical prompt must not touch the network again.
        cfg.cassette_mode = "replay"
        model_mod.urllib.request.urlopen = Transport([])
        again = ModelClient(cfg, cassette=Cassette(path))
        self.assertEqual(again.complete([{"role": "user", "content": "hi"}], None).call.name,
                         "read_file")
        self.assertEqual(again.network_calls, 0)


if __name__ == "__main__":
    unittest.main()
