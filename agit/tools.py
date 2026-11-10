"""The agent's tools, plus the safety rails around them."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import snapshot

MAX_OUTPUT = 6000

# Commands agit refuses outright: irreversible, or they reach outside this
# machine. The model cannot talk its way past this list.
HARD_DENY = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-?[rf]{1,2}\s+/(\s|$)", "recursive delete of /"),
    (r"\brm\s+-rf\s+~", "recursive delete of home"),
    (r"\bsudo\b", "privilege escalation"),
    (r"\bmkfs(\.|\s)", "filesystem format"),
    (r"\bdd\s+if=", "raw disk write"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "host power control"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\bchmod\s+-R\s+777\s+/", "world-writable root"),
    (r"\bgit\s+push\b", "pushing to a remote"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", "destroying uncommitted work"),
    (r"\bnpm\s+publish\b", "publishing a package"),
    (r"\b(twine|cargo)\s+(upload|publish)\b", "publishing a package"),
    (r"\bkubectl\s+(delete|apply)\b", "cluster mutation"),
    (r"\b(terraform|cdk|serverless)\s+(apply|deploy|destroy)\b", "infrastructure change"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "piping the internet into a shell"),
    (r"\bwget\b[^|]*\|\s*(ba)?sh", "piping the internet into a shell"),
    (r"\bhistory\s+-c", "clearing shell history"),
    (r">\s*/dev/sd", "raw device write"),
]


@dataclass
class ToolResult:
    ok: bool
    output: str
    target: str = ""
    diffstat: Dict[str, int] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    terminal: bool = False          # finish()
    denied: bool = False

    def truncated(self, limit: int = MAX_OUTPUT) -> str:
        if len(self.output) <= limit:
            return self.output
        head = self.output[: limit - 200]
        return head + "\n...[%d characters trimmed]..." % (len(self.output) - limit + 200)


class ToolError(Exception):
    pass


SPECS: List[Dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": "List files and directories in the workspace. Start here to orient yourself.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
                "depth": {"type": "integer", "description": "How deep to recurse (1-4, default 2)"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Returns numbered lines so you can refer to them precisely.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-based, optional"},
                "end_line": {"type": "integer", "description": "1-based inclusive, optional"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "Search the workspace for a string or regular expression. Returns file:line matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Limit to this directory or glob. Default whole workspace."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regex. Default false."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with exact content. Use for new files; prefer edit_file for small changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact substring in a file. `find` must appear exactly once unless count is given.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "count": {"type": "integer", "description": "Number of occurrences to replace. Default 1."},
            },
            "required": ["path", "find", "replace"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the workspace (tests, linters, build). Destructive and outward-facing commands are refused.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds, default 120, max 600"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the task complete. If a verify command is configured it runs now and must pass.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What you changed and why, for the human reviewer."},
            },
            "required": ["summary"],
        },
    },
]

MUTATING = {"write_file", "edit_file", "run_command"}


def openai_tools() -> List[Dict[str, Any]]:
    return [{"type": "function", "function": spec} for spec in SPECS]


def tool_help() -> str:
    lines = []
    for spec in SPECS:
        props = spec["parameters"].get("properties", {})
        args = ", ".join(props)
        lines.append("- %s(%s): %s" % (spec["name"], args, spec["description"]))
    return "\n".join(lines)


class Toolbox:
    def __init__(
        self,
        root: str,
        allow_commands: bool = False,
        confirm: Optional[Callable[[str], bool]] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
    ):
        self.root = os.path.abspath(root)
        self.allow_commands = allow_commands
        self.confirm = confirm
        self.on_event = on_event or (lambda kind, text: None)

    # -- paths ------------------------------------------------------------
    def resolve(self, path: str) -> str:
        if not path or path in (".", "./"):
            return self.root
        if os.path.isabs(path):
            candidate = os.path.abspath(path)
        else:
            candidate = os.path.abspath(os.path.join(self.root, path))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise ToolError("path %r is outside the workspace" % path)
        rel = os.path.relpath(candidate, self.root).replace(os.sep, "/")
        first = rel.split("/")[0]
        if first in (".agit", ".git"):
            raise ToolError("%s is agit's own bookkeeping and is off limits" % first)
        return candidate

    def rel(self, abspath: str) -> str:
        return os.path.relpath(abspath, self.root).replace(os.sep, "/")

    # -- dispatch ---------------------------------------------------------
    def run(self, name: str, args: Dict[str, Any]) -> ToolResult:
        fn = getattr(self, "_t_" + name, None)
        if fn is None:
            return ToolResult(False, "unknown tool %r. Available: %s" % (
                name, ", ".join(s["name"] for s in SPECS)))
        try:
            result = fn(args or {})
        except ToolError as exc:
            result = ToolResult(False, "error: %s" % exc)
        except Exception as exc:  # the model must see its own mistakes
            result = ToolResult(False, "%s: %s" % (type(exc).__name__, exc))
        result.output = self.scrub(result.output)
        return result

    def scrub(self, text: str) -> str:
        """Strip machine-specific paths out of anything the model will see.

        Two reasons: your home directory is nobody else's business, including
        the model provider's, and a prompt without absolute paths is identical
        on every machine -- which is what makes `agit replay` work.
        """
        if not text:
            return text
        # Longest first: /private/tmp/x must be replaced before /tmp/x, or the
        # leftover prefix glues itself onto the filename.
        for base in sorted({self.root, os.path.realpath(self.root)}, key=len, reverse=True):
            text = text.replace(base + os.sep, "").replace(base, ".")
        home = os.path.expanduser("~")
        if home and home != "/":
            text = text.replace(home, "~")
        return text

    # -- tools ------------------------------------------------------------
    def _t_list_dir(self, args: Dict[str, Any]) -> ToolResult:
        path = self.resolve(str(args.get("path", ".")))
        depth = max(1, min(4, int(args.get("depth", 2) or 2)))
        if not os.path.isdir(path):
            raise ToolError("not a directory: %s" % args.get("path", "."))
        base_depth = path.rstrip(os.sep).count(os.sep)
        lines: List[str] = []
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(d for d in dirnames if d not in snapshot.ALWAYS_SKIP_DIRS)
            level = dirpath.rstrip(os.sep).count(os.sep) - base_depth
            if level >= depth:
                dirnames[:] = []
            indent = "  " * level
            label = self.rel(dirpath) or "."
            lines.append("%s%s/" % (indent, os.path.basename(label) or label))
            for fn in sorted(filenames):
                if fn in snapshot.ALWAYS_SKIP_FILES or fn.lower().endswith(snapshot.SKIP_SUFFIXES):
                    continue
                try:
                    size = os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    size = 0
                lines.append("%s  %s  (%d B)" % (indent, fn, size))
            if len(lines) > 400:
                lines.append("... listing trimmed")
                break
        return ToolResult(True, "\n".join(lines), target=self.rel(path) or ".")

    def _t_read_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.resolve(str(args["path"]))
        if not os.path.isfile(path):
            raise ToolError("no such file: %s" % args["path"])
        text = _read_text(path)
        lines = text.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or len(lines))
        end = min(end, len(lines))
        body = "\n".join("%5d | %s" % (i, lines[i - 1]) for i in range(start, end + 1))
        header = "%s (%d lines)" % (self.rel(path), len(lines))
        return ToolResult(True, header + "\n" + body, target=self.rel(path))

    def _t_search(self, args: Dict[str, Any]) -> ToolResult:
        pattern = str(args["pattern"])
        use_regex = bool(args.get("regex"))
        scope = str(args.get("path") or ".")
        try:
            rx = re.compile(pattern if use_regex else re.escape(pattern))
        except re.error as exc:
            raise ToolError("bad regex: %s" % exc)
        candidates = snapshot.list_files(self.root)
        if scope not in (".", "", "./"):
            if any(ch in scope for ch in "*?["):
                candidates = [p for p in candidates if fnmatch.fnmatch(p, scope)]
            else:
                prefix = scope.strip("./") + "/"
                candidates = [p for p in candidates if p == scope.strip("./") or p.startswith(prefix)]
        hits: List[str] = []
        files_hit = 0
        for rel in candidates:
            full = os.path.join(self.root, rel)
            try:
                text = _read_text(full)
            except (OSError, ToolError):
                continue
            matched = False
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append("%s:%d: %s" % (rel, i, line.strip()[:200]))
                    matched = True
                    if len(hits) >= 60:
                        break
            if matched:
                files_hit += 1
            if len(hits) >= 60:
                hits.append("... more matches not shown")
                break
        if not hits:
            return ToolResult(True, "no matches for %r" % pattern, target=pattern)
        return ToolResult(
            True,
            "%d matches in %d files\n%s" % (len(hits), files_hit, "\n".join(hits)),
            target=pattern,
        )

    def _t_write_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.resolve(str(args["path"]))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        before = _read_text(path) if os.path.isfile(path) else ""
        if before == content:
            return ToolResult(True, "no change: %s already has this content" % self.rel(path),
                              target=self.rel(path))
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        stat = diffstat(before, content)
        return ToolResult(
            True,
            "wrote %s (+%d -%d)\n%s" % (self.rel(path), stat["added"], stat["removed"],
                                        unified(before, content, self.rel(path))),
            target=self.rel(path),
            diffstat=stat,
        )

    def _t_edit_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.resolve(str(args["path"]))
        if not os.path.isfile(path):
            raise ToolError("no such file: %s" % args["path"])
        find = str(args["find"])
        replace = str(args.get("replace", ""))
        count = int(args.get("count", 1) or 1)
        before = _read_text(path)
        occurrences = before.count(find)
        if occurrences == 0:
            raise ToolError("`find` text not present in %s (check whitespace and indentation)"
                            % self.rel(path))
        if occurrences > count:
            raise ToolError(
                "`find` appears %d times in %s but count=%d. Include more surrounding "
                "context to make it unique, or raise count." % (occurrences, self.rel(path), count))
        after = before.replace(find, replace, count)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(after)
        stat = diffstat(before, after)
        return ToolResult(
            True,
            "edited %s (+%d -%d)\n%s" % (self.rel(path), stat["added"], stat["removed"],
                                         unified(before, after, self.rel(path))),
            target=self.rel(path),
            diffstat=stat,
        )

    def _t_run_command(self, args: Dict[str, Any]) -> ToolResult:
        command = str(args["command"]).strip()
        if not command:
            raise ToolError("empty command")
        reason = deny_reason(command)
        if reason:
            return ToolResult(
                False,
                "refused: %s. agit will not run this. Pick a safe alternative." % reason,
                target=command,
                denied=True,
            )
        if not self.allow_commands:
            approved = self.confirm(command) if self.confirm else False
            if not approved:
                return ToolResult(
                    False,
                    "the human declined to run %r. Continue without it or propose another step."
                    % command,
                    target=command,
                    denied=True,
                )
        timeout = max(1, min(600, int(args.get("timeout", 120) or 120)))
        result = shell(command, self.root, timeout)
        body = "$ %s\nexit=%s\n%s" % (command, result["code"], result["output"])
        return ToolResult(result["code"] == 0, body, target=command,
                          meta={"exit_code": result["code"]})

    def _t_finish(self, args: Dict[str, Any]) -> ToolResult:
        summary = str(args.get("summary", "")).strip() or "(no summary given)"
        return ToolResult(True, summary, target="", terminal=True)


# -- helpers --------------------------------------------------------------

def deny_reason(command: str) -> str:
    low = " " + command.lower() + " "
    for pattern, why in HARD_DENY:
        if re.search(pattern, low):
            return why
    return ""


def shell(command: str, cwd: str, timeout: int = 120) -> Dict[str, Any]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["AGIT_INSIDE"] = "1"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return {"code": proc.returncode, "output": out.strip()[:MAX_OUTPUT]}
    except subprocess.TimeoutExpired:
        return {"code": 124, "output": "timed out after %ds" % timeout}
    except OSError as exc:
        return {"code": 127, "output": str(exc)}


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ToolError(str(exc))
    if b"\0" in raw[:4096]:
        raise ToolError("%s looks binary" % os.path.basename(path))
    return raw.decode("utf-8", "replace")


def diffstat(before: str, after: str) -> Dict[str, int]:
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"added": added, "removed": removed, "files": 1}


def unified(before: str, after: str, label: str, context: int = 2, limit: int = 120) -> str:
    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="a/" + label, tofile="b/" + label, lineterm="", n=context,
    ))
    if len(diff) > limit:
        diff = diff[:limit] + ["... diff trimmed (%d more lines)" % (len(diff) - limit)]
    return "\n".join(diff)
