import os
import subprocess
import unittest

from agit import gitutil, snapshot
from tests.support import WorkspaceCase


def git_init(path: str) -> None:
    subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)


class TestFileListing(WorkspaceCase):
    def test_plain_directory_is_walked(self):
        files = snapshot.list_files(self.root)
        self.assertIn("calc.py", files)
        self.assertIn("test_calc.py", files)

    def test_noise_directories_are_skipped(self):
        self.write("node_modules/pkg/index.js", "x\n")
        self.write("__pycache__/calc.cpython-39.pyc", "x\n")
        self.write("build/out.o", "x\n")
        files = snapshot.list_files(self.root)
        self.assertEqual([f for f in files if "node_modules" in f], [])
        self.assertEqual([f for f in files if "__pycache__" in f], [])
        self.assertEqual([f for f in files if f.startswith("build/")], [])

    def test_own_gitignore_is_honoured(self):
        git_init(self.root)
        self.write(".gitignore", "secret.txt\n")
        self.write("secret.txt", "nope\n")
        files = snapshot.list_files(self.root)
        self.assertIn("calc.py", files)
        self.assertNotIn("secret.txt", files)

    def test_a_parent_repo_ignoring_the_workspace_does_not_hide_it(self):
        """A workspace inside someone else's repo keeps its own files.

        Without this, running agit in a scratch directory that the surrounding
        repository gitignores produced empty snapshots, which silently broke
        `agit blame`.
        """
        git_init(self.root)
        self.write(".gitignore", "scratch/\n")
        inner = os.path.join(self.root, "scratch", "project")
        os.makedirs(inner, exist_ok=True)
        with open(os.path.join(inner, "main.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")
        self.assertTrue(gitutil.is_repo(inner))            # inside the parent repo
        self.assertIsNone(gitutil.tracked_and_untracked(inner))
        self.assertIn("main.py", snapshot.list_files(inner))

    def test_snapshot_round_trip_keeps_content(self):
        dest = os.path.join(self.tmp, "snap")
        stats = snapshot.copy_into(self.root, dest)
        self.assertEqual(stats["files"], 2)
        with open(os.path.join(dest, "calc.py"), encoding="utf-8") as fh:
            self.assertIn("return a - b", fh.read())

    def test_large_files_are_skipped_and_listed(self):
        self.write("big.txt", "x" * 5000)
        dest = os.path.join(self.tmp, "snap2")
        stats = snapshot.copy_into(self.root, dest, max_bytes=1000)
        self.assertEqual(stats["skipped"], 1)
        with open(os.path.join(dest, ".agit-skipped"), encoding="utf-8") as fh:
            self.assertIn("big.txt", fh.read())


if __name__ == "__main__":
    unittest.main()
