import contextlib
import io
import os
import shutil
import tempfile
import unittest

from agit import cli
from tests.support import FIX_STEPS, WorkspaceCase


def run_cli(argv):
    """Invoke the CLI the way a shell would, capturing what the user sees."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(argv)
    return code, out.getvalue()


class CliCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        os.environ["AGIT_PROVIDER"] = "script"
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(os.environ.pop, "AGIT_PROVIDER", None)


class TestCliRun(CliCase):
    def test_run_then_inspect(self):
        script = self.script_file(FIX_STEPS)
        code, out = run_cli(["run", "fix add()", "--provider", "script",
                             "--script", script, "--yes",
                             "--verify", "python3 -m unittest -q test_calc"])
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFIED", out)
        self.assertIn("return a + b", self.read("calc.py"))

        code, out = run_cli(["log"])
        self.assertEqual(code, 0)
        self.assertIn("step 03", out)
        self.assertIn("edit", out)

        code, out = run_cli(["blame", "calc.py"])
        self.assertEqual(code, 0)
        self.assertIn("step 03", out)
        self.assertIn("human", out)

        code, out = run_cli(["why", "calc.py", "3"])
        self.assertEqual(code, 0)
        self.assertIn("minus sign", out)

        code, out = run_cli(["show", "3", "--io"])
        self.assertEqual(code, 0)
        self.assertIn("edit_file", out)
        self.assertIn("workspace diff", out)

        code, out = run_cli(["stats"])
        self.assertEqual(code, 0)
        self.assertIn("agent share", out.lower())

        code, out = run_cli(["runs"])
        self.assertEqual(code, 0)
        self.assertIn("verified", out)

        code, out = run_cli(["report", "@last", "-o", "r.html"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.path("r.html")))
        with open(self.path("r.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("agit run", html)
        self.assertIn("minus sign", html)
        self.assertNotIn("<script", html.lower())

    def test_run_without_verify_is_done_not_verified(self):
        script = self.script_file(FIX_STEPS)
        code, out = run_cli(["run", "fix add()", "--provider", "script",
                             "--script", script, "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("DONE", out)

    def test_failing_verify_exits_nonzero(self):
        bad = [
            {"why": "wrong fix", "tool": "edit_file",
             "args": {"path": "calc.py", "find": "return a - b", "replace": "return a * b"}},
            {"why": "done", "tool": "finish", "args": {"summary": "x"}},
        ]
        script = self.script_file(bad, "bad.json")
        code, out = run_cli(["run", "fix add()", "--provider", "script",
                             "--script", script, "--yes", "--max-steps", "3",
                             "--verify", "python3 -m unittest -q test_calc"])
        self.assertEqual(code, 1)
        self.assertIn("NOT VERIFIED", out)

    def test_undo_and_goto_move_the_workspace(self):
        script = self.script_file(FIX_STEPS)
        run_cli(["run", "fix add()", "--provider", "script", "--script", script, "--yes"])
        self.assertIn("return a + b", self.read("calc.py"))
        code, out = run_cli(["goto", "2"])
        self.assertEqual(code, 0, out)
        self.assertIn("return a - b", self.read("calc.py"))

    def test_missing_task_is_rejected(self):
        code, out = run_cli(["run", "   "])
        self.assertEqual(code, 2)

    def test_doctor_reports_offline_provider(self):
        code, out = run_cli(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("offline scripted provider", out)

    def test_init_is_idempotent(self):
        self.assertEqual(run_cli(["init"])[0], 0)
        self.assertEqual(run_cli(["init"])[0], 0)

    def test_help_mentions_the_ledger(self):
        code, out = run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("blamable", out)


class TestCliDemo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agit-demo-test-")
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["NO_COLOR"] = "1"

    def test_demo_runs_offline_and_replays_identically(self):
        code, out = run_cli(["demo", "ws", "--fast"])
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFIED", out)
        os.chdir(os.path.join(self.tmp, "ws"))

        code, out = run_cli(["replay", "@last"])
        self.assertEqual(code, 0, out)
        self.assertIn("identical", out)

        code, out = run_cli(["blame", "csvkit.py"])
        self.assertEqual(code, 0)
        self.assertIn("human", out)
        self.assertIn("step 05", out)

        code, out = run_cli(["log", "--graph"])
        self.assertEqual(code, 0)
        self.assertIn("step 0005", out)

    def test_demo_refuses_to_clobber_without_force(self):
        self.assertEqual(run_cli(["demo", "ws2", "--fast"])[0], 0)
        code, out = run_cli(["demo", "ws2", "--fast"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", out)


class TestRefParsing(unittest.TestCase):
    def test_line_ranges(self):
        self.assertEqual(cli.parse_line_range("10,40", 100), (10, 40))
        self.assertEqual(cli.parse_line_range("10:40", 100), (10, 40))
        self.assertEqual(cli.parse_line_range("7", 100), (7, 7))
        self.assertEqual(cli.parse_line_range("90,999", 100), (90, 100))

    def test_path_with_line_suffix(self):
        self.assertEqual(cli.split_path_line("a/b.py:12", None), ("a/b.py", 12))
        self.assertEqual(cli.split_path_line("a/b.py", 5), ("a/b.py", 5))
        self.assertEqual(cli.split_path_line("a/b.py", None), ("a/b.py", None))

    def test_first_divergence(self):
        self.assertIsNone(cli.first_divergence(["a", "b"], ["a", "b"]))
        self.assertEqual(cli.first_divergence(["a", "b"], ["a", "c"]), 1)
        self.assertIsNone(cli.first_divergence(["a"], ["a", "b"]))


if __name__ == "__main__":
    unittest.main()
