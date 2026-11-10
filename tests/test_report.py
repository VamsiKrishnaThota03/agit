import os
import unittest

from agit import report
from tests.support import FIX_STEPS, WorkspaceCase


class TestReport(WorkspaceCase):
    def build(self, **cfg_kwargs):
        result = self.run_agent(FIX_STEPS, cfg=self.config(**cfg_kwargs))
        info = self.ledger.resolve_run(result.run)
        out = report.render(self.ledger, info, self.path("r.html"))
        with open(out, encoding="utf-8") as fh:
            return fh.read()

    def test_report_is_self_contained(self):
        html = self.build()
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("cdn", html.lower())
        self.assertIn("<style>", html)

    def test_report_carries_the_rationale_and_the_diff(self):
        html = self.build()
        self.assertIn("minus sign", html)
        self.assertIn("return a + b", html)
        self.assertIn("edit_file", html)

    def test_report_marks_a_verified_run(self):
        html = self.build(verify="python3 -m unittest -q test_calc")
        self.assertIn("VERIFIED", html)
        self.assertIn("verify pass", html)

    def test_report_escapes_model_text(self):
        steps = [
            {"why": "inject <script>alert(1)</script> & \"quotes\"",
             "tool": "write_file", "args": {"path": "x.py", "content": "y = 1\n"}},
            {"why": "done", "tool": "finish", "args": {"summary": "<b>done</b>"}},
        ]
        result = self.run_agent(steps)
        info = self.ledger.resolve_run(result.run)
        out = report.render(self.ledger, info, self.path("r2.html"))
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<b>done</b>", html)


if __name__ == "__main__":
    unittest.main()
