import json
import os
import tempfile
import unittest

from agit.config import Config
from agit.model import (Cassette, ModelClient, ModelError, ReplayMiss, ToolCall,
                        mask_volatile, parse_text_action, wrap_response)


class TestParsing(unittest.TestCase):
    def test_native_tool_call_is_read(self):
        cfg = Config(provider="script", model="m")
        client = ModelClient(cfg)
        raw = wrap_response("why", ToolCall("read_file", {"path": "a.py"}))
        reply = client._to_reply(raw, cached=False)
        self.assertEqual(reply.call.name, "read_file")
        self.assertEqual(reply.call.args["path"], "a.py")
        self.assertEqual(reply.text, "why")

    def test_string_arguments_are_decoded(self):
        cfg = Config(provider="script", model="m")
        client = ModelClient(cfg)
        raw = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "1", "function": {"name": "search",
                                     "arguments": '{"pattern": "def add"}'}}]}}]}
        reply = client._to_reply(raw, cached=False)
        self.assertEqual(reply.call.args["pattern"], "def add")

    def test_json_protocol_fallback(self):
        why, call = parse_text_action(
            '```json\n{"rationale": "because", "tool": "write_file", '
            '"args": {"path": "a.py", "content": "x"}}\n```')
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.args["path"], "a.py")
        self.assertEqual(why, "because")

    def test_json_protocol_tolerates_surrounding_chatter(self):
        why, call = parse_text_action(
            'Sure! Here is the step:\n{"thought":"t","action":"list_dir",'
            '"parameters":{"path":"."}}\nLet me know.')
        self.assertEqual(call.name, "list_dir")
        self.assertEqual(call.args["path"], ".")
        self.assertEqual(why, "t")

    def test_prose_without_json_is_not_a_tool_call(self):
        why, call = parse_text_action("I think we should look at calc.py first.")
        self.assertIsNone(call)

    def test_provider_error_payload_raises(self):
        client = ModelClient(Config(provider="script", model="m"))
        with self.assertRaises(ModelError):
            client._to_reply({"error": {"message": "bad key"}}, cached=False)

    def test_empty_choices_raises(self):
        client = ModelClient(Config(provider="script", model="m"))
        with self.assertRaises(ModelError):
            client._to_reply({"choices": []}, cached=False)


class TestCassette(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agit-cassette-")
        self.path = os.path.join(self.tmp, "cassette.json")

    def payload(self, text):
        return {"model": "m", "temperature": 0.0,
                "messages": [{"role": "user", "content": text}], "tools": None}

    def test_key_is_stable_and_content_sensitive(self):
        a = Cassette.key(self.payload("hello"))
        self.assertEqual(a, Cassette.key(self.payload("hello")))
        self.assertNotEqual(a, Cassette.key(self.payload("goodbye")))

    def test_volatile_noise_does_not_change_the_key(self):
        a = Cassette.key(self.payload("Ran 4 tests in 0.004s"))
        b = Cassette.key(self.payload("Ran 4 tests in 0.131s"))
        self.assertEqual(a, b)

    def test_mask_volatile_covers_times_pids_addresses(self):
        masked = mask_volatile("took 1.234s at 12:00:01 pid=9123 at 0xdeadbeef1")
        self.assertNotIn("1.234s", masked)
        self.assertNotIn("12:00:01", masked)
        self.assertNotIn("9123", masked)
        self.assertNotIn("deadbeef", masked)

    def test_roundtrip_persists_entries(self):
        cassette = Cassette(self.path)
        cassette.put("k", wrap_response("hi"))
        cassette.save()
        self.assertEqual(len(Cassette(self.path)), 1)
        self.assertIsNotNone(Cassette(self.path).get("k"))

    def test_replay_mode_raises_on_miss(self):
        cfg = Config(provider="openai-compatible", base_url="http://127.0.0.1:1/v1",
                     model="m", cassette_mode="replay")
        client = ModelClient(cfg, cassette=Cassette(self.path))
        with self.assertRaises(ReplayMiss):
            client.complete([{"role": "user", "content": "x"}], tools=None)

    def test_reuse_mode_answers_from_the_cassette(self):
        from agit.script import ScriptProvider
        cfg = Config(provider="script", model="m", cassette_mode="reuse")
        cassette = Cassette(self.path)
        payload = {"model": "m", "temperature": 0.0,
                   "messages": [{"role": "user", "content": "x"}]}
        cassette.put(Cassette.key(payload), wrap_response("from the cassette"))
        client = ModelClient(cfg, cassette=cassette,
                             script=ScriptProvider([{"why": "fresh", "tool": "list_dir"}]))
        reply = client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(reply.text, "from the cassette")
        self.assertTrue(reply.cached)

    def test_live_mode_ignores_the_cassette_but_records(self):
        from agit.script import ScriptProvider
        cfg = Config(provider="script", model="m", cassette_mode="live")
        cassette = Cassette(self.path)
        payload = {"model": "m", "temperature": 0.0,
                   "messages": [{"role": "user", "content": "x"}]}
        key = Cassette.key(payload)
        cassette.put(key, wrap_response("stale answer"))
        client = ModelClient(cfg, cassette=cassette,
                             script=ScriptProvider([{"why": "fresh", "tool": "list_dir"}]))
        reply = client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(reply.text, "fresh")
        self.assertFalse(reply.cached)
        self.assertIn("fresh", json.dumps(cassette.get(key)))

    def test_replay_mode_never_touches_the_network(self):
        cfg = Config(provider="openai-compatible", base_url="http://127.0.0.1:1/v1",
                     model="m", cassette_mode="replay")
        cassette = Cassette(self.path)
        payload = {"model": "m", "temperature": 0.0,
                   "messages": [{"role": "user", "content": "x"}]}
        cassette.put(Cassette.key(payload), wrap_response("cached answer"))
        client = ModelClient(cfg, cassette=cassette)
        reply = client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(reply.text, "cached answer")
        self.assertTrue(reply.cached)
        self.assertEqual(client.network_calls, 0)


if __name__ == "__main__":
    unittest.main()
