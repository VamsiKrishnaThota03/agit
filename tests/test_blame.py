import unittest

from agit import blame
from tests.support import FIX_STEPS, WorkspaceCase


class TestBlame(WorkspaceCase):
    def test_agent_lines_and_human_lines_are_separated(self):
        result = self.run_agent(FIX_STEPS)
        lines = blame.blame_file(self.ledger, "calc.py", result.branch)
        changed = [b for b in lines if "return a + b" in b.text]
        untouched = [b for b in lines if b.text.startswith("def add")]
        self.assertTrue(changed and changed[0].by_agent)
        self.assertEqual(changed[0].author, "step 3")
        self.assertTrue(untouched and not untouched[0].by_agent)
        self.assertEqual(untouched[0].author, "human")

    def test_authorship_summary_counts_agent_share(self):
        result = self.run_agent(FIX_STEPS)
        lines = blame.blame_file(self.ledger, "calc.py", result.branch)
        agent_lines, total = blame.summarise_authorship(lines)
        self.assertEqual(agent_lines, 1)
        self.assertEqual(total, 3)

    def test_explain_line_returns_the_rationale(self):
        result = self.run_agent(FIX_STEPS)
        lines = blame.blame_file(self.ledger, "calc.py", result.branch)
        target = next(b for b in lines if "return a + b" in b.text)
        exp = blame.explain_line(self.ledger, "calc.py", target.lineno, result.branch)
        self.assertEqual(exp.step.step, 3)
        self.assertEqual(exp.tool, "edit_file")
        self.assertIn("minus sign", exp.rationale)
        self.assertIn("calc.py", exp.args_preview)
        self.assertIn("return a + b", exp.diff)

    def test_explain_line_on_human_code(self):
        result = self.run_agent(FIX_STEPS)
        exp = blame.explain_line(self.ledger, "calc.py", 1, result.branch)
        self.assertTrue(exp.step is None or exp.step.kind == "baseline")

    def test_blame_on_a_file_the_agent_created(self):
        steps = [
            {"why": "new module", "tool": "write_file",
             "args": {"path": "util.py", "content": "def nop():\n    return None\n"}},
            {"why": "done", "tool": "finish", "args": {"summary": "added util"}},
        ]
        result = self.run_agent(steps)
        lines = blame.blame_file(self.ledger, "util.py", result.branch)
        self.assertTrue(lines)
        self.assertTrue(all(b.by_agent for b in lines))

    def test_preview_args_summarises_file_bodies(self):
        preview = blame.preview_args({"content": "line\n" * 40, "path": "a.py"})
        self.assertTrue(preview.startswith("path=a.py"))
        self.assertIn("content=<41 lines", preview)
        self.assertNotIn("line\\nline", preview)

    def test_preview_args_keeps_short_values(self):
        preview = blame.preview_args({"pattern": "def add", "regex": False})
        self.assertIn("pattern=def add", preview)
        self.assertIn("regex=False", preview)

    def test_blame_rejects_unknown_file(self):
        result = self.run_agent(FIX_STEPS)
        with self.assertRaises(Exception):
            blame.blame_file(self.ledger, "ghost.py", result.branch)


if __name__ == "__main__":
    unittest.main()
