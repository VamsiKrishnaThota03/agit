"""`agit blame`: which agent step wrote this line, and why.

Implemented on top of real `git blame` inside the ledger, so the attribution is
git's own, not a heuristic. Lines that trace back to a run's baseline commit are
pre-existing human code; everything else carries a step number, a rationale and
a pointer to the exact model request/response that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .ledger import Ledger, LedgerError, StepInfo

HUMAN = "human"


@dataclass
class BlameLine:
    lineno: int
    text: str
    sha: str
    step: Optional[StepInfo] = None

    @property
    def author(self) -> str:
        if self.step is None:
            return HUMAN
        if self.step.kind == "baseline":
            return HUMAN
        return "step %d" % self.step.step

    @property
    def by_agent(self) -> bool:
        return self.step is not None and self.step.kind not in ("baseline", "run-start")


def _index(ledger: Ledger, ref: str) -> Dict[str, StepInfo]:
    return {s.sha: s for s in ledger.steps(ref)}


def blame_file(ledger: Ledger, rel_path: str, ref: str = "HEAD") -> List[BlameLine]:
    by_sha = _index(ledger, ref)
    rows = ledger.blame(rel_path, ref)
    return [BlameLine(lineno=no, text=text, sha=sha, step=by_sha.get(sha))
            for no, sha, text in rows]


@dataclass
class Explanation:
    path: str
    line: int
    text: str
    step: Optional[StepInfo]
    rationale: str = ""
    tool: str = ""
    args_preview: str = ""
    verify: str = ""
    diff: str = ""
    sha: str = ""
    request_ref: str = ""
    run_task: str = ""


def explain_line(ledger: Ledger, rel_path: str, line: int, ref: str = "HEAD") -> Explanation:
    lines = blame_file(ledger, rel_path, ref)
    match = next((b for b in lines if b.lineno == line), None)
    if match is None:
        raise LedgerError("%s has no line %d in the snapshot at %s" % (rel_path, line, ref))
    run = ledger.read_run(ref)
    exp = Explanation(
        path=rel_path, line=line, text=match.text, step=match.step, sha=match.sha,
        run_task=(run.task if run else ""),
    )
    if match.step is None:
        return exp
    meta = ledger.step_meta(match.step)
    exp.rationale = match.step.rationale or str(meta.get("rationale", ""))
    exp.tool = match.step.tool
    exp.args_preview = preview_args(meta.get("args") or {})
    exp.verify = match.step.verify
    exp.request_ref = "%s:steps/%04d/" % (match.sha[:10], match.step.step)
    if match.step.kind not in ("baseline",):
        diff = ledger.step_diff(match.step)
        exp.diff = _file_hunks(diff, rel_path)
    return exp


#: Shown first, because they say what the step acted on.
PRIMARY_ARGS = ("path", "command", "pattern", "find")
#: Summarised by size instead of dumped; a file body is not a preview.
BULKY_ARGS = ("content", "replace", "find", "summary")


def preview_args(args: Dict[str, object], limit: int = 56) -> str:
    def order(item):
        key = item[0]
        return (PRIMARY_ARGS.index(key) if key in PRIMARY_ARGS else len(PRIMARY_ARGS), key)

    parts: List[str] = []
    for key, value in sorted(args.items(), key=order):
        text = str(value)
        if key in BULKY_ARGS and len(text) > limit:
            parts.append("%s=<%d lines, %d chars>" % (key, text.count("\n") + 1, len(text)))
        else:
            flat = text.replace("\n", "\\n")
            parts.append("%s=%s" % (key, flat if len(flat) <= limit else flat[:limit] + "..."))
    return ", ".join(parts[:4])


def _file_hunks(diff: str, rel_path: str) -> str:
    """Keep only the hunks of `diff` that touch rel_path."""
    wanted = "workspace/" + rel_path.lstrip("./")
    chunks: List[str] = []
    current: List[str] = []
    keep = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            if keep and current:
                chunks.append("\n".join(current))
            current = [line]
            keep = wanted in line
            continue
        current.append(line)
    if keep and current:
        chunks.append("\n".join(current))
    return "\n".join(chunks)


def summarise_authorship(lines: List[BlameLine]) -> Tuple[int, int]:
    agent = sum(1 for b in lines if b.by_agent)
    return agent, len(lines)
