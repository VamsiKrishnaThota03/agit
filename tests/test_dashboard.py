import json
import os
import re
import unittest

from agit import dashboard
from tests.support import FIX_STEPS, WorkspaceCase


class TestDashboardData(WorkspaceCase):
    def test_collect_describes_every_run(self):
        first = self.run_agent(FIX_STEPS, label="first")
        data = dashboard.collect(self.ledger)
        self.assertEqual(data["totals"]["runs"], 1)
        run = data["runs"][0]
        self.assertEqual(run["id"], first.run)
        self.assertEqual(run["steps"], len(FIX_STEPS))
        self.assertEqual(run["writes"], 1)
        self.assertTrue(run["tokens"] > 0)
        self.assertEqual([s["tool"] for s in run["steps_detail"]],
                         [s["tool"] for s in FIX_STEPS])

    def test_collect_carries_blame_with_human_and_agent_lines(self):
        self.run_agent(FIX_STEPS)
        data = dashboard.collect(self.ledger)
        blame = data["runs"][0]["blame"]
        calc = next(b for b in blame if b["path"] == "calc.py")
        authors = {row[1] for row in calc["lines"]}
        self.assertIn(None, authors)       # human lines
        self.assertIn(3, authors)          # the edit step
        self.assertEqual(data["runs"][0]["files"][0]["path"], "calc.py")

    def test_fork_is_linked_to_its_parent(self):
        first = self.run_agent(FIX_STEPS, label="first")
        fork_point = self.ledger.step(2, first.branch).sha
        self.ledger.restore_workspace(fork_point, self.root)
        alt = [{"why": "other way", "tool": "write_file",
                "args": {"path": "calc.py", "content": "def add(a, b):\n    return sum((a, b))\n"}},
               {"why": "done", "tool": "finish", "args": {"summary": "sum()"}}]
        self.run_agent(alt, label="alt", fork_from=fork_point, start_step=2)
        data = dashboard.collect(self.ledger)
        child = next(r for r in data["runs"] if r["label"] == "alt")
        self.assertEqual(child["parent_run"], first.run)
        self.assertEqual(child["parent_step"], 2)


class TestDashboardHtml(WorkspaceCase):
    def html(self, steps=None, **cfg):
        self.run_agent(steps or FIX_STEPS, cfg=self.config(**cfg))
        out = dashboard.build(self.ledger, self.path("dash.html"))
        with open(out, encoding="utf-8") as fh:
            return fh.read()

    def test_single_file_with_no_external_resources(self):
        html = self.html()
        self.assertNotIn("src=\"http", html)
        self.assertNotIn("cdn", html.lower())
        self.assertNotIn("<link", html.lower())
        self.assertIn("<style>", html)

    def test_payload_is_parseable_json(self):
        html = self.html()
        match = re.search(r"<script type='application/json' id='agit-data'>(.*?)</script>",
                          html, re.S)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1).replace("\\u003c", "<"))
        self.assertEqual(payload["totals"]["runs"], 1)

    def test_model_text_cannot_break_out_of_the_json_block(self):
        nasty = '</script><script>alert("xss")</script>'
        steps = [
            {"why": nasty, "tool": "write_file",
             "args": {"path": "x.py", "content": "y = 1\n"}},
            {"why": "done", "tool": "finish", "args": {"summary": nasty}},
        ]
        html = self.html(steps)
        # Exactly the two script tags agit writes itself, and nothing injected.
        self.assertEqual(html.count("<script"), 2)
        self.assertNotIn('alert("xss")', html)
        self.assertIn("\\u003c/script", html)

    def test_verified_run_is_marked(self):
        html = self.html(verify="python3 -m unittest -q test_calc")
        payload = re.search(r"id='agit-data'>(.*?)</script>", html, re.S).group(1)
        data = json.loads(payload.replace("\\u003c", "<"))
        self.assertTrue(data["runs"][0]["verified"])

    def test_build_refuses_nothing_and_creates_parent_dirs(self):
        self.run_agent(FIX_STEPS)
        out = dashboard.build(self.ledger, self.path("nested/deep/dash.html"))
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
