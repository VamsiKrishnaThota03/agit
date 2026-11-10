import unittest

from agit import tools
from agit.agent import _compact, _turn_messages
from agit.model import Reply, ToolCall
from tests.support import FIX_STEPS, WorkspaceCase


class TestAgentLoop(WorkspaceCase):
    def test_run_applies_the_fix_and_reports_verified(self):
        cfg = self.config(verify="python3 -m unittest -q test_calc")
        result = self.run_agent(FIX_STEPS, cfg=cfg)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.verified)
        self.assertTrue(result.ok)
        self.assertIn("return a + b", self.read("calc.py"))

    def test_verify_failure_blocks_finish(self):
        broken = [
            {"why": "break it", "tool": "edit_file",
             "args": {"path": "calc.py", "find": "return a - b", "replace": "return a * b"}},
            {"why": "claim victory too early", "tool": "finish",
             "args": {"summary": "surely fine"}},
        ]
        cfg = self.config(verify="python3 -m unittest -q test_calc", max_steps=4)
        result = self.run_agent(broken, cfg=cfg)
        self.assertFalse(result.verified)
        self.assertFalse(result.ok)
        steps = [s for s in self.ledger.steps(result.branch) if s.verify]
        self.assertTrue(any(s.verify == "fail" for s in steps))

    def test_step_budget_is_enforced(self):
        loop = [{"why": "spin", "tool": "list_dir", "args": {"path": "."}}] * 10
        cfg = self.config(max_steps=3)
        result = self.run_agent(loop, cfg=cfg)
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(len(result.step_log), 3)

    def test_failed_tool_step_is_recorded_not_hidden(self):
        steps = [
            {"why": "wrong path", "tool": "read_file", "args": {"path": "nope.py"}},
            {"why": "give up", "tool": "finish", "args": {"summary": "could not read"}},
        ]
        result = self.run_agent(steps)
        first = self.ledger.step(1, result.branch)
        self.assertFalse(first.ok)
        self.assertIn("no such file", self.ledger.step_artifact(first, "result.txt"))

    def test_signatures_are_recorded_for_replay(self):
        result = self.run_agent(FIX_STEPS)
        self.assertEqual(len(result.signatures), len(FIX_STEPS))
        self.assertTrue(result.signatures[0].startswith("list_dir("))


class TestMessagePlumbing(unittest.TestCase):
    def test_native_turn_has_tool_call_and_tool_result(self):
        reply = Reply(text="because", tool_calls=[ToolCall("read_file", {"path": "a.py"})])
        result = tools.ToolResult(True, "contents")
        messages = _turn_messages(reply, reply.tool_calls[0], result, native=True)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertIn("[ok]", messages[1]["content"])

    def test_fallback_turn_is_plain_json(self):
        reply = Reply(text="because", tool_calls=[ToolCall("read_file", {"path": "a.py"})])
        result = tools.ToolResult(False, "boom")
        messages = _turn_messages(reply, reply.tool_calls[0], result, native=False)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertIn('"tool": "read_file"', messages[0]["content"])
        self.assertIn("[failed]", messages[1]["content"])

    def test_compact_trims_old_tool_results_only(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
        for i in range(12):
            messages.append({"role": "assistant", "content": "a%d" % i})
            messages.append({"role": "tool", "tool_call_id": "c%d" % i,
                             "content": "x" * 1000})
        compacted = _compact(messages, keep_full=3)
        tool_messages = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 12)
        self.assertEqual(sum(1 for m in tool_messages if len(m["content"]) > 500), 3)
        self.assertEqual(compacted[0]["content"], "sys")
        self.assertEqual(compacted[1]["content"], "task")


if __name__ == "__main__":
    unittest.main()
