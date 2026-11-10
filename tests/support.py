"""Shared fixtures: a tiny workspace plus a scripted agent run."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from agit import config as config_mod
from agit import tools
from agit.agent import Agent, RunResult
from agit.ledger import Ledger
from agit.model import Cassette, ModelClient
from agit.script import ScriptProvider

BUGGY = '''def add(a, b):
    """Add two numbers."""
    return a - b
'''

FIXED = '''def add(a, b):
    """Add two numbers."""
    return a + b
'''

TEST_FILE = '''import unittest

from calc import add


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
'''


class WorkspaceCase(unittest.TestCase):
    """A temp workspace with calc.py (buggy) and test_calc.py."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agit-test-")
        self.root = os.path.realpath(self.tmp)
        self.write("calc.py", BUGGY)
        self.write("test_calc.py", TEST_FILE)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- files ------------------------------------------------------------
    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def write(self, rel: str, text: str) -> str:
        full = self.path(rel)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        return full

    def read(self, rel: str) -> str:
        with open(self.path(rel), "r", encoding="utf-8") as fh:
            return fh.read()

    # -- agent ------------------------------------------------------------
    def script_file(self, steps: List[Dict[str, Any]], name: str = "script.json") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"name": name, "steps": steps}, fh)
        return path

    def config(self, **overrides: Any):
        base: Dict[str, Any] = {
            "provider": "script",
            "allow_commands": True,
            "max_steps": 12,
        }
        base.update(overrides)
        return config_mod.resolve(self.root, base)

    def run_agent(self, steps: List[Dict[str, Any]], task: str = "fix add()",
                  cfg: Optional[Any] = None, root: Optional[str] = None,
                  label: str = "test", fork_from: str = "",
                  start_step: int = 0) -> RunResult:
        root = root or self.root
        cfg = cfg or self.config()
        ledger = Ledger.ensure(self.root)
        client = ModelClient(
            cfg,
            cassette=Cassette(os.path.join(self.root, ".agit", "cassette.json")),
            script=ScriptProvider(steps, name=label),
        )
        toolbox = tools.Toolbox(root, allow_commands=cfg.allow_commands)
        agent = Agent(root, cfg, ledger, client, toolbox)
        result = agent.run(task, label=label, fork_from=fork_from, start_step=start_step)
        client.cassette.save()
        return result

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.root)


FIX_STEPS: List[Dict[str, Any]] = [
    {"why": "look around first", "tool": "list_dir", "args": {"path": ".", "depth": 1}},
    {"why": "read the broken function", "tool": "read_file", "args": {"path": "calc.py"}},
    {"why": "a minus sign where a plus belongs", "tool": "edit_file",
     "args": {"path": "calc.py", "find": "return a - b", "replace": "return a + b"}},
    {"why": "prove it", "tool": "run_command",
     "args": {"command": "python3 -m unittest -q test_calc"}},
    {"why": "done", "tool": "finish", "args": {"summary": "add() now adds."}},
]
