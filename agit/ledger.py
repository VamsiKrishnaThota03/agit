"""The ledger: an agent run stored as a real git history.

Layout of the ledger repository (`.agit/ledger`):

    run.json              state of the run that owns this branch
    steps/0001/meta.json  structured record of one step
    steps/0001/request.json / response.json   exact model I/O
    steps/0001/result.txt tool output the model was shown
    workspace/...         full snapshot of the workspace after the step

One step == one commit on a branch named `run/<id>`. Branches fork, so an
agent run forks. Because the workspace snapshot lives inside the commit,
`git blame` on `workspace/<file>` attributes every line to the step that
wrote it -- that is what powers `agit blame`.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import gitutil, snapshot
from .config import AGIT_DIR

BASE_BRANCH = "base"
RUN_PREFIX = "run/"
STEP_PAD = 4


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def new_run_id(label: str = "") -> str:
    stamp = time.strftime("%m%d-%H%M%S")
    salt = "%04x" % random.randrange(0x10000)
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")[:18]
    return "%s-%s%s" % (stamp, salt, ("-" + slug) if slug else "")


@dataclass
class StepInfo:
    sha: str
    step: int
    run: str
    tool: str = ""
    target: str = ""
    ok: bool = True
    kind: str = "tool"
    rationale: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    verify: str = ""
    added: int = 0
    removed: int = 0
    at: str = ""

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class RunInfo:
    run: str
    branch: str
    task: str = ""
    status: str = "running"
    steps: int = 0
    model: str = ""
    provider: str = ""
    started_at: str = ""
    ended_at: str = ""
    verified: Optional[bool] = None
    forked_from: str = ""
    tokens: int = 0
    label: str = ""
    tip: str = ""


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, AGIT_DIR, "ledger")

    # -- lifecycle --------------------------------------------------------
    @property
    def exists(self) -> bool:
        return os.path.isdir(os.path.join(self.path, ".git"))

    @classmethod
    def ensure(cls, root: str) -> "Ledger":
        led = cls(root)
        if led.exists:
            return led
        if not gitutil.available():
            raise LedgerError("git is required: agit stores agent runs as git history")
        os.makedirs(led.path, exist_ok=True)
        gitutil.git(led.path, "init", "-q", "-b", BASE_BRANCH)
        readme = os.path.join(led.path, "LEDGER.md")
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(
                "# agit ledger\n\n"
                "This is a real git repository holding the history of AI agent runs in\n"
                "the parent project. Each commit is one agent step: `steps/NNNN/` has the\n"
                "exact model request/response and tool result, `workspace/` is the project\n"
                "snapshot after the step.\n\n"
                "Useful plain-git commands:\n\n"
                "    git log --graph --all --oneline\n"
                "    git blame -- workspace/<file>\n"
                "    git diff run/<a> run/<b> -- workspace/\n"
            )
        gitutil.git(led.path, "add", "-A")
        gitutil.git(led.path, "commit", "-q", "-m", "agit: ledger root")
        return led

    def require(self) -> "Ledger":
        if not self.exists:
            raise LedgerError("no ledger here. Run `agit init` or `agit run \"<task>\"` first.")
        return self

    # -- low level --------------------------------------------------------
    def _git(self, *args: str, **kw: Any) -> str:
        return gitutil.git(self.path, *args, **kw)

    def _write(self, rel: str, text: str) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _read_at(self, ref: str, rel: str) -> Optional[str]:
        try:
            return self._git("show", "%s:%s" % (ref, rel))
        except gitutil.GitError:
            return None

    # -- runs -------------------------------------------------------------
    def begin_run(
        self,
        task: str,
        provider: str,
        model: str,
        label: str = "",
        fork_from: str = "",
        run_id: str = "",
    ) -> RunInfo:
        run_id = run_id or new_run_id(label)
        branch = RUN_PREFIX + run_id
        start_point = fork_from or BASE_BRANCH
        fork_label = ""
        if fork_from:
            try:
                _, trail = gitutil.trailers(self.path, fork_from)
                if trail.get("Agit-Run"):
                    fork_label = "%s@%s" % (trail["Agit-Run"], trail.get("Agit-Step", "?"))
            except gitutil.GitError:
                fork_label = ""
        self._git("checkout", "-q", "-B", branch, start_point)
        info = RunInfo(
            run=run_id,
            branch=branch,
            task=task,
            status="running",
            model=model,
            provider=provider,
            started_at=_now_iso(),
            forked_from=fork_label or (fork_from[:10] if fork_from else ""),
            label=label,
        )
        if fork_from:
            parent = self.read_run(fork_from)
            if parent:
                info.steps = parent.steps
                info.task = task or parent.task
        self._save_run(info)
        self._git("add", "-A")
        self._git(
            "commit", "-q", "-m",
            "run %s: start\n\ntask: %s\n\nAgit-Run: %s\nAgit-Event: run-start\nAgit-Forked-From: %s"
            % (run_id, task, run_id, fork_from or "-"),
        )
        return info

    def _save_run(self, info: RunInfo) -> None:
        self._write("run.json", json.dumps(info.__dict__, indent=2, sort_keys=True) + "\n")

    def read_run(self, ref: str = "HEAD") -> Optional[RunInfo]:
        raw = self._read_at(ref, "run.json")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        allowed = {f for f in RunInfo.__dataclass_fields__}  # type: ignore[attr-defined]
        info = RunInfo(**{k: v for k, v in data.items() if k in allowed})
        info.tip = (gitutil.head_sha(self.path) or "") if ref == "HEAD" else ref
        return info

    def update_run(self, **changes: Any) -> Optional[RunInfo]:
        info = self.read_run("HEAD")
        if info is None:
            return None
        for k, v in changes.items():
            setattr(info, k, v)
        self._save_run(info)
        return info

    def runs(self) -> List[RunInfo]:
        out: List[RunInfo] = []
        for branch in gitutil.branches(self.path, "run/"):
            info = self.read_run(branch)
            if info is None:
                continue
            info.branch = branch
            info.tip = self._git("rev-parse", "--short", branch).strip()
            out.append(info)
        out.sort(key=lambda r: r.started_at, reverse=True)
        return out

    def last_run(self) -> Optional[RunInfo]:
        runs = self.runs()
        return runs[0] if runs else None

    def resolve_run(self, spec: str = "") -> RunInfo:
        """Accept '', '@last', a run id, a branch name or a unique prefix."""
        if not spec or spec in ("@last", "last", "."):
            info = self.last_run()
            if info is None:
                raise LedgerError("no runs recorded yet")
            return info
        runs = self.runs()
        for info in runs:
            if spec in (info.run, info.branch, RUN_PREFIX + info.run):
                return info
        matches = [i for i in runs if i.run.startswith(spec) or spec in i.label]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LedgerError("no run matching %r (try `agit runs`)" % spec)
        raise LedgerError("ambiguous run %r: %s" % (spec, ", ".join(m.run for m in matches)))

    # -- steps ------------------------------------------------------------
    def commit_step(
        self,
        meta: Dict[str, Any],
        artifacts: Optional[Dict[str, str]] = None,
        workspace_root: Optional[str] = None,
        snapshot_max_bytes: int = 512 * 1024,
    ) -> str:
        step_no = int(meta["step"])
        sdir = "steps/%s" % str(step_no).zfill(STEP_PAD)
        meta = dict(meta)
        meta.setdefault("at", _now_iso())
        self._write(sdir + "/meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")
        for name, body in (artifacts or {}).items():
            self._write("%s/%s" % (sdir, name), body if body.endswith("\n") else body + "\n")
        if workspace_root:
            snapshot.copy_into(
                workspace_root,
                os.path.join(self.path, "workspace"),
                max_bytes=snapshot_max_bytes,
            )
        if meta.get("kind") in (None, "tool", "finish"):
            self.update_run(steps=step_no)
        self._git("add", "-A")

        subject = "step %s %s%s" % (
            str(step_no).zfill(STEP_PAD),
            meta.get("tool") or meta.get("kind") or "think",
            (" " + meta["target"]) if meta.get("target") else "",
        )
        body = (meta.get("rationale") or "").strip()
        trailers = [
            ("Agit-Run", meta.get("run", "")),
            ("Agit-Step", str(step_no)),
            ("Agit-Kind", meta.get("kind", "tool")),
            ("Agit-Tool", meta.get("tool", "")),
            ("Agit-Target", meta.get("target", "")),
            ("Agit-Ok", "true" if meta.get("ok", True) else "false"),
            ("Agit-Model", meta.get("model", "")),
            ("Agit-Tokens-In", str(meta.get("tokens_in", 0))),
            ("Agit-Tokens-Out", str(meta.get("tokens_out", 0))),
            ("Agit-Duration-Ms", str(meta.get("duration_ms", 0))),
            ("Agit-Added", str((meta.get("diffstat") or {}).get("added", 0))),
            ("Agit-Removed", str((meta.get("diffstat") or {}).get("removed", 0))),
            ("Agit-Verify", (meta.get("verify") or {}).get("state", "") if isinstance(meta.get("verify"), dict) else str(meta.get("verify") or "")),
        ]
        message = subject + ("\n\n" + body if body else "") + "\n\n" + "\n".join(
            "%s: %s" % (k, v if v != "" else "-") for k, v in trailers
        )
        try:
            self._git("commit", "-q", "--allow-empty", "-m", message)
        except gitutil.GitError as exc:  # pragma: no cover - defensive
            raise LedgerError(str(exc))
        return self._git("rev-parse", "HEAD").strip()

    def steps(self, ref: str = "HEAD", limit: int = 0) -> List[StepInfo]:
        fmt = "%H%x1f%s%x1f%b%x1e"
        args = ["log", "--format=" + fmt]
        if limit:
            args.append("-n%d" % limit)
        args.append(ref)
        raw = self._git(*args)
        found: List[StepInfo] = []
        for record in raw.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) < 3:
                continue
            sha, _subject, body = parts[0], parts[1], parts[2]
            trail: Dict[str, str] = {}
            text: List[str] = []
            for line in body.splitlines():
                if line.startswith("Agit-") and ":" in line:
                    k, _, v = line.partition(":")
                    trail[k.strip()] = v.strip()
                else:
                    text.append(line)
            if "Agit-Step" not in trail:
                continue
            found.append(StepInfo(
                sha=sha,
                step=int(trail.get("Agit-Step", "0") or 0),
                run=trail.get("Agit-Run", ""),
                tool=_undash(trail.get("Agit-Tool", "")),
                target=_undash(trail.get("Agit-Target", "")),
                ok=trail.get("Agit-Ok", "true") == "true",
                kind=_undash(trail.get("Agit-Kind", "tool")) or "tool",
                rationale="\n".join(text).strip(),
                model=_undash(trail.get("Agit-Model", "")),
                tokens_in=_int(trail.get("Agit-Tokens-In")),
                tokens_out=_int(trail.get("Agit-Tokens-Out")),
                duration_ms=_int(trail.get("Agit-Duration-Ms")),
                verify=_undash(trail.get("Agit-Verify", "")),
                added=_int(trail.get("Agit-Added")),
                removed=_int(trail.get("Agit-Removed")),
                at=self._git("show", "-s", "--format=%cI", sha).strip(),
            ))
        found.sort(key=lambda s: s.step)
        return found

    def step(self, step_no: int, ref: str = "HEAD") -> StepInfo:
        for s in self.steps(ref):
            if s.step == step_no:
                return s
        raise LedgerError("step %d not found in %s" % (step_no, ref))

    def step_meta(self, step: StepInfo) -> Dict[str, Any]:
        raw = self._read_at(step.sha, "steps/%s/meta.json" % str(step.step).zfill(STEP_PAD))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def step_artifact(self, step: StepInfo, name: str) -> Optional[str]:
        return self._read_at(step.sha, "steps/%s/%s" % (str(step.step).zfill(STEP_PAD), name))

    def step_diff(self, step: StepInfo, context: int = 3) -> str:
        parent = "%s^" % step.sha
        try:
            return self._git(
                "diff", "-U%d" % context, parent, step.sha, "--", "workspace/"
            )
        except gitutil.GitError:
            return self._git("show", "--format=", "-U%d" % context, step.sha, "--", "workspace/")

    # -- navigation -------------------------------------------------------
    def checkout(self, ref: str) -> None:
        self._git("checkout", "-q", ref)

    def restore_workspace(self, ref: str, workspace_root: str) -> Dict[str, int]:
        """Materialise the workspace as it was at `ref` into the project.

        Uses a throwaway git worktree so the ledger's own HEAD and branch are
        never disturbed.
        """
        tmp = os.path.join(self.root, AGIT_DIR, "restore-tmp")
        if os.path.isdir(tmp):
            self._git("worktree", "remove", "--force", tmp, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
        self._git("worktree", "prune")
        self._git("worktree", "add", "-q", "--detach", tmp, ref)
        try:
            src = os.path.join(tmp, "workspace")
            if not os.path.isdir(src):
                raise LedgerError("no workspace snapshot at %s" % ref)
            stats = snapshot.restore_from(src, workspace_root)
        finally:
            self._git("worktree", "remove", "--force", tmp, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
            self._git("worktree", "prune", check=False)
        return stats

    def diff_refs(self, a: str, b: str, context: int = 3) -> str:
        return self._git("diff", "-U%d" % context, a, b, "--", "workspace/")

    def blame(self, rel_path: str, ref: str = "HEAD") -> List[Tuple[int, str, str]]:
        """Return [(line_no, sha, text)] for a workspace file at `ref`."""
        target = "workspace/" + rel_path.lstrip("./")
        try:
            raw = self._git("blame", "--line-porcelain", ref, "--", target)
        except gitutil.GitError:
            raise LedgerError("%s is not in the ledger snapshot at %s" % (rel_path, ref))
        out: List[Tuple[int, str, str]] = []
        sha = ""
        lineno = 0
        for line in raw.splitlines():
            if line and not line.startswith("\t") and len(line.split(" ")[0]) == 40:
                bits = line.split(" ")
                sha = bits[0]
                if len(bits) >= 3:
                    lineno = int(bits[2])
            elif line.startswith("\t"):
                out.append((lineno, sha, line[1:]))
        return out

    def graph(self) -> str:
        return self._git(
            "log", "--graph", "--all", "--decorate", "--oneline", "--date-order"
        )


def _int(value: Optional[str]) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _undash(value: str) -> str:
    return "" if value.strip() in ("", "-") else value.strip()


