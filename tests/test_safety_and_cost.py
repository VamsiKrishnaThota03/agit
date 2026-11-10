"""The overwrite guard, resuming a run, and cost reporting."""
import json
import os
import unittest

from agit import pricing
from agit import snapshot
from agit.config import Config
from tests.support import FIX_STEPS, WorkspaceCase
from tests.test_cli import CliCase, run_cli


class TestDriftDetection(WorkspaceCase):
    def test_clean_workspace_reports_no_drift(self):
        result = self.run_agent(FIX_STEPS)
        drift = self.ledger.workspace_drift(result.branch, self.root)
        self.assertEqual(drift, {"modified": [], "added": [], "deleted": []})

    def test_human_edits_are_detected(self):
        result = self.run_agent(FIX_STEPS)
        self.write("calc.py", self.read("calc.py") + "\n# mine\n")
        self.write("notes.md", "my notes\n")
        os.remove(self.path("test_calc.py"))
        drift = self.ledger.workspace_drift(result.branch, self.root)
        self.assertEqual(drift["modified"], ["calc.py"])
        self.assertEqual(drift["added"], ["notes.md"])
        self.assertEqual(drift["deleted"], ["test_calc.py"])

    def test_compare_ignores_the_skipped_marker(self):
        import shutil
        import tempfile
        # Outside the workspace: a snapshot stored inside it would itself count
        # as new files, which is correct behaviour but not what we are testing.
        outside = tempfile.mkdtemp(prefix="agit-snap-")
        self.addCleanup(shutil.rmtree, outside, True)
        dest = os.path.join(outside, "snap")
        snapshot.copy_into(self.root, dest)
        self.assertEqual(snapshot.compare(dest, self.root),
                         {"modified": [], "added": [], "deleted": []})


class TestOverwriteGuard(CliCase):
    def setUp(self):
        super().setUp()
        self.script = self.script_file(FIX_STEPS)
        run_cli(["run", "fix add()", "--provider", "script",
                 "--script", self.script, "--yes"])

    def test_undo_refuses_when_the_human_has_unrecorded_work(self):
        self.write("calc.py", "my own version\n")
        code, out = run_cli(["undo"])
        self.assertEqual(code, 3)
        self.assertIn("never recorded", out)
        self.assertIn("refusing", out)
        self.assertEqual(self.read("calc.py"), "my own version\n")

    def test_force_overrides_the_guard(self):
        self.write("calc.py", "my own version\n")
        code, out = run_cli(["undo", "--force"])
        self.assertEqual(code, 0, out)
        self.assertIn("continuing anyway", out)
        self.assertNotEqual(self.read("calc.py"), "my own version\n")

    def test_goto_is_guarded_too(self):
        self.write("extra.py", "x = 1\n")
        code, out = run_cli(["goto", "2"])
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(self.path("extra.py")))

    def test_a_clean_workspace_is_never_blocked(self):
        code, out = run_cli(["goto", "2"])
        self.assertEqual(code, 0, out)
        self.assertIn("return a - b", self.read("calc.py"))


class TestContinue(CliCase):
    def test_an_interrupted_run_resumes_on_the_same_branch(self):
        first = self.script_file(
            [{"why": "look around", "tool": "list_dir", "args": {"path": "."}},
             {"why": "read the code", "tool": "read_file", "args": {"path": "calc.py"}}],
            "part1.json")
        rest = self.script_file(
            [{"why": "fix the operator", "tool": "edit_file",
              "args": {"path": "calc.py", "find": "return a - b", "replace": "return a + b"}},
             {"why": "done", "tool": "finish", "args": {"summary": "fixed it"}}],
            "part2.json")

        code, out = run_cli(["run", "fix add()", "--provider", "script",
                             "--script", first, "--max-steps", "2"])
        self.assertIn("exhausted", out.lower())

        code, out = run_cli(["continue", "@last", "--provider", "script", "--script", rest])
        self.assertEqual(code, 0, out)
        self.assertIn("return a + b", self.read("calc.py"))

        code, out = run_cli(["runs", "--json"])
        runs = json.loads(out)
        self.assertEqual(len(runs), 1, "continuing must not start a second run")
        self.assertEqual(runs[0]["steps"], 4)
        self.assertEqual(runs[0]["status"], "completed")

        code, out = run_cli(["log"])
        for step in ("step 01", "step 02", "step 03", "step 04"):
            self.assertIn(step, out)

    def test_resumed_run_is_told_what_already_happened(self):
        """The recap is what stops a resumed agent from redoing finished work."""
        from agit.agent import Agent
        from agit.ledger import Ledger
        result = self.run_agent(FIX_STEPS)
        info = Ledger(self.root).resolve_run(result.run)
        agent = Agent(self.root, self.config(), Ledger(self.root), None, None)
        recap = agent._recap(info)
        self.assertIn("interrupted", recap)
        self.assertIn("step 3: edit_file calc.py", recap)
        self.assertIn("minus sign", recap)


class TestPricing(unittest.TestCase):
    def cfg(self, provider="groq", model="llama-3.3-70b-versatile"):
        return Config(provider=provider, model=model)

    def test_no_rate_configured_means_no_invented_number(self):
        self.assertIsNone(pricing.rate_for(self.cfg(), {}))

    def test_offline_providers_are_free(self):
        for provider in ("script", "ollama"):
            rate = pricing.rate_for(self.cfg(provider=provider), {})
            self.assertIsNotNone(rate)
            self.assertTrue(rate.free)
            self.assertEqual(rate.format(10_000, 2_000), "free")

    def test_stored_rate_is_used_and_does_the_arithmetic(self):
        project = pricing.store({}, self.cfg(), 0.59, 0.79)
        rate = pricing.rate_for(self.cfg(), project)
        self.assertAlmostEqual(rate.cost(1_000_000, 1_000_000), 1.38, places=6)
        self.assertEqual(rate.format(1_000_000, 0), "$0.59")

    def test_small_amounts_keep_four_decimals(self):
        rate = pricing.Rate(0.2, 0.6)
        self.assertEqual(rate.format(1000, 100), "$0.0003")

    def test_currency_symbol_follows_the_rate(self):
        rate = pricing.Rate(18.0, 54.0, "INR")
        self.assertTrue(rate.format(1_000_000, 0).startswith("₹"))

    def test_env_overrides_project_config(self):
        project = pricing.store({}, self.cfg(), 1.0, 1.0)
        os.environ["AGIT_PRICE_IN"] = "2.0"
        os.environ["AGIT_PRICE_OUT"] = "4.0"
        self.addCleanup(os.environ.pop, "AGIT_PRICE_IN", None)
        self.addCleanup(os.environ.pop, "AGIT_PRICE_OUT", None)
        rate = pricing.rate_for(self.cfg(), project)
        self.assertEqual(rate.source, "env")
        self.assertAlmostEqual(rate.input_per_m, 2.0)

    def test_per_model_keys_do_not_bleed(self):
        project = pricing.store({}, self.cfg(model="a"), 1.0, 2.0)
        self.assertIsNotNone(pricing.rate_for(self.cfg(model="a"), project))
        self.assertIsNone(pricing.rate_for(self.cfg(model="b"), project))


class TestCostInTheCli(CliCase):
    def test_stats_json_is_machine_readable(self):
        script = self.script_file(FIX_STEPS)
        run_cli(["run", "fix add()", "--provider", "script", "--script", script, "--yes",
                 "--verify", "python3 -m unittest -q test_calc"])
        code, out = run_cli(["stats", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["verified"])
        self.assertEqual(data["steps"], len(FIX_STEPS))
        self.assertIn("calc.py", [f["path"] for f in data["files"]])
        self.assertGreater(data["agent_share"], 0)
        self.assertIn("add() now adds", data["summary"])
        self.assertEqual(data["cost"], "free")      # script provider

    def test_price_command_sets_and_reports_a_rate(self):
        code, out = run_cli(["price", "--provider", "groq", "--model", "m"])
        self.assertEqual(code, 1)
        self.assertIn("no rate set", out)

        code, out = run_cli(["price", "--provider", "groq", "--model", "m",
                             "--in", "0.5", "--out", "1.5"])
        self.assertEqual(code, 0, out)

        code, out = run_cli(["price", "--provider", "groq", "--model", "m"])
        self.assertEqual(code, 0)
        self.assertIn("$0.50 per 1M tokens", out)


if __name__ == "__main__":
    unittest.main()
