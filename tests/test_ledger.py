import os
import unittest

from agit.ledger import Ledger, LedgerError
from tests.support import FIX_STEPS, WorkspaceCase


class TestLedgerBasics(WorkspaceCase):
    def test_ensure_creates_a_real_git_repo(self):
        led = Ledger.ensure(self.root)
        self.assertTrue(os.path.isdir(os.path.join(led.path, ".git")))
        self.assertTrue(led.exists)

    def test_require_complains_before_init(self):
        with self.assertRaises(LedgerError):
            Ledger(self.root).require()

    def test_every_step_is_one_commit(self):
        result = self.run_agent(FIX_STEPS)
        steps = self.ledger.steps(result.branch)
        tool_steps = [s for s in steps if s.kind in ("tool", "finish")]
        self.assertEqual(len(tool_steps), len(FIX_STEPS))
        self.assertEqual([s.tool for s in tool_steps],
                         [s["tool"] for s in FIX_STEPS])
        self.assertEqual(len({s.sha for s in steps}), len(steps))

    def test_trailers_survive_the_round_trip(self):
        result = self.run_agent(FIX_STEPS)
        edit = next(s for s in self.ledger.steps(result.branch) if s.tool == "edit_file")
        self.assertEqual(edit.target, "calc.py")
        self.assertEqual(edit.added, 1)
        self.assertEqual(edit.removed, 1)
        self.assertTrue(edit.ok)
        self.assertIn("minus sign", edit.rationale)

    def test_baseline_records_the_untouched_workspace(self):
        result = self.run_agent(FIX_STEPS)
        baseline = next(s for s in self.ledger.steps(result.branch) if s.kind == "baseline")
        snapshot = self.ledger._read_at(baseline.sha, "workspace/calc.py")
        self.assertIn("return a - b", snapshot)

    def test_run_metadata_is_queryable(self):
        result = self.run_agent(FIX_STEPS, task="fix the adder")
        runs = self.ledger.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run, result.run)
        self.assertEqual(runs[0].task, "fix the adder")
        self.assertEqual(runs[0].status, "completed")

    def test_resolve_run_accepts_prefix_and_last(self):
        result = self.run_agent(FIX_STEPS)
        led = self.ledger
        self.assertEqual(led.resolve_run("@last").run, result.run)
        self.assertEqual(led.resolve_run(result.run[:6]).run, result.run)
        with self.assertRaises(LedgerError):
            led.resolve_run("nope-nope")

    def test_prefix_match_prefers_ids_over_labels(self):
        """A replay's label contains its parent's id; that must not make the
        parent id ambiguous."""
        parent = self.run_agent(FIX_STEPS, label="demo")
        self.run_agent(FIX_STEPS, label="replay-of-" + parent.run)
        resolved = self.ledger.resolve_run(parent.run[:11])
        self.assertEqual(resolved.run, parent.run)

    def test_step_artifacts_hold_the_model_io(self):
        result = self.run_agent(FIX_STEPS)
        step = next(s for s in self.ledger.steps(result.branch) if s.tool == "edit_file")
        self.assertIn("edit_file", self.ledger.step_artifact(step, "response.json"))
        self.assertIn("calc.py", self.ledger.step_artifact(step, "result.txt"))
        self.assertIn("role", self.ledger.step_artifact(step, "request.json"))


class TestTimeTravel(WorkspaceCase):
    def test_restore_rewinds_the_workspace(self):
        result = self.run_agent(FIX_STEPS)
        self.assertIn("return a + b", self.read("calc.py"))
        led = self.ledger
        baseline = next(s for s in led.steps(result.branch) if s.kind == "baseline")
        led.restore_workspace(baseline.sha, self.root)
        self.assertIn("return a - b", self.read("calc.py"))

    def test_restore_deletes_files_created_later(self):
        steps = [
            {"why": "add a file", "tool": "write_file",
             "args": {"path": "extra.py", "content": "x = 1\n"}},
            {"why": "done", "tool": "finish", "args": {"summary": "added extra.py"}},
        ]
        result = self.run_agent(steps)
        self.assertTrue(os.path.exists(self.path("extra.py")))
        led = self.ledger
        baseline = next(s for s in led.steps(result.branch) if s.kind == "baseline")
        led.restore_workspace(baseline.sha, self.root)
        self.assertFalse(os.path.exists(self.path("extra.py")))

    def test_restore_leaves_the_ledger_branch_alone(self):
        result = self.run_agent(FIX_STEPS)
        led = self.ledger
        before = led._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        baseline = next(s for s in led.steps(result.branch) if s.kind == "baseline")
        led.restore_workspace(baseline.sha, self.root)
        self.assertEqual(led._git("rev-parse", "--abbrev-ref", "HEAD").strip(), before)

    def test_forking_shares_history_then_diverges(self):
        first = self.run_agent(FIX_STEPS)
        led = self.ledger
        fork_point = led.step(2, first.branch).sha
        led.restore_workspace(fork_point, self.root)
        alt = [
            {"why": "different fix", "tool": "write_file",
             "args": {"path": "calc.py", "content": "def add(a, b):\n    return sum((a, b))\n"}},
            {"why": "done", "tool": "finish", "args": {"summary": "used sum()"}},
        ]
        second = self.run_agent(alt, label="alt", fork_from=fork_point, start_step=2)
        self.assertNotEqual(first.run, second.run)
        diff = led.diff_refs(first.branch, second.branch)
        self.assertIn("sum((a, b))", diff)
        shared = {s.sha for s in led.steps(first.branch)} & {s.sha for s in led.steps(second.branch)}
        self.assertTrue(shared, "fork should share the prefix commits")


if __name__ == "__main__":
    unittest.main()
