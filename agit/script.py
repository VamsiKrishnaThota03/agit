"""The scripted provider: a deterministic stand-in for a model.

This is NOT a model and never pretends to be one. It replays a fixed list of
tool calls from a JSON file, which gives agit three things no live model can:
a demo that runs offline with no API key, a test suite that exercises the full
agent loop in CI, and a stable baseline to diff real model runs against.

Script format:

    {
      "name": "fix the csv parser",
      "steps": [
        {"why": "find the parser", "tool": "search", "args": {"pattern": "def parse_row"}},
        {"why": "...", "tool": "write_file",
         "args": {"path": "test_quoted.py"}, "content_file": "fixed/test_quoted.py"},
        {"why": "done", "tool": "finish", "args": {"summary": "..."}}
      ]
    }

`content_file` is read relative to the script and injected as args.content, so
large file bodies stay readable instead of being buried in JSON strings.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .model import ToolCall, wrap_response

MODEL_NAME = "agit-script/deterministic"


class ScriptError(RuntimeError):
    pass


class ScriptProvider:
    def __init__(self, steps: Optional[List[Dict[str, Any]]] = None, name: str = "generic",
                 pace_ms: int = 0):
        self.steps = steps if steps is not None else list(GENERIC_STEPS)
        self.name = name
        self.pace_ms = pace_ms

    # -- loading ----------------------------------------------------------
    @classmethod
    def from_file(cls, path: str, pace_ms: int = 0) -> "ScriptProvider":
        with open(path, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except ValueError as exc:
                raise ScriptError("%s is not valid JSON: %s" % (path, exc))
        if isinstance(data, list):
            data = {"steps": data}
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ScriptError("%s has no 'steps' list" % path)
        base = os.path.dirname(os.path.abspath(path))
        resolved: List[Dict[str, Any]] = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or "tool" not in step:
                raise ScriptError("step %d in %s needs a 'tool' field" % (i + 1, path))
            step = dict(step)
            step.setdefault("args", {})
            holder = step.pop("content_file", None)
            if holder:
                target = os.path.join(base, holder)
                if not os.path.isfile(target):
                    raise ScriptError("step %d references missing content_file %s" % (i + 1, holder))
                with open(target, "r", encoding="utf-8") as cf:
                    step["args"] = dict(step["args"], content=cf.read())
            resolved.append(step)
        return cls(resolved, name=data.get("name", os.path.basename(path)), pace_ms=pace_ms)

    @classmethod
    def discover(cls, root: str, explicit: str = "", pace_ms: int = 0) -> "ScriptProvider":
        candidates = [explicit] if explicit else []
        candidates += [
            os.environ.get("AGIT_SCRIPT", ""),
            os.path.join(root, ".agit", "script.json"),
            os.path.join(root, "agit.script.json"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return cls.from_file(path, pace_ms=pace_ms)
        if explicit:
            raise ScriptError("script not found: %s" % explicit)
        return cls(pace_ms=pace_ms)

    # -- provider interface ----------------------------------------------
    def respond(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        turn = sum(1 for m in payload.get("messages", []) if m.get("role") == "assistant")
        if turn >= len(self.steps):
            call = ToolCall(
                name="finish",
                args={"summary": "Scripted plan %r finished after %d steps."
                                 % (self.name, len(self.steps))},
                id="call_%d" % turn,
            )
            return wrap_response("end of script", call, MODEL_NAME, 0, 0)
        step = self.steps[turn]
        if self.pace_ms:
            time.sleep(self.pace_ms / 1000.0)
        call = ToolCall(name=step["tool"], args=dict(step.get("args") or {}), id="call_%d" % turn)
        why = str(step.get("why", "") or "")
        prompt_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in payload.get("messages", []))
        return wrap_response(
            why, call, MODEL_NAME,
            tokens_in=max(1, prompt_chars // 4),
            tokens_out=max(1, (len(why) + len(json.dumps(call.args))) // 4),
        )


#: What the scripted provider does when no script file is supplied: a safe,
#: read-only look around so `--provider script` is never a dead end.
GENERIC_STEPS: List[Dict[str, Any]] = [
    {"why": "Orient: list the workspace before touching anything.",
     "tool": "list_dir", "args": {"path": ".", "depth": 2}},
    {"why": "Look for unfinished work markers.",
     "tool": "search", "args": {"pattern": "TODO|FIXME", "regex": True}},
    {"why": "No script was supplied, so stop instead of guessing at edits.",
     "tool": "finish", "args": {
         "summary": "agit's scripted provider ran a read-only survey. Point agit at a "
                    "real model (agit doctor) or supply --script to drive edits."}},
]
