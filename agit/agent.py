"""The agent loop. Every iteration becomes exactly one commit in the ledger."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import snapshot, tools
from .config import Config
from .ledger import Ledger, RunInfo, StepInfo
from .model import ModelClient, ModelError, ReplayMiss, Reply, ToolCall

SYSTEM = """You are agit, an autonomous software engineer working inside one workspace.

Every action you take is committed to a git ledger: your rationale becomes the
commit message and the workspace snapshot becomes the commit tree. A human will
later run `agit blame` on the lines you wrote and read exactly why you wrote
them. Write for that reader.

Rules of engagement:
1. Look before you leap. list_dir / read_file / search before editing.
2. Change the minimum. Prefer edit_file over rewriting a whole file.
3. Prove it. When tests exist, run them. When they do not, add one that fails
   for the stated reason, then make it pass.
4. One tool call per turn, and one sentence of rationale in your message content
   explaining WHY this call, not what the call is.
5. Never touch .agit or .git. Never reach outside the workspace.
6. Call finish only when the task is genuinely done, with a summary a reviewer
   can act on.
"""

JSON_PROTOCOL = """This model is being driven without native tool calling. Reply with
exactly one JSON object and nothing else:

{"rationale": "one sentence on why", "tool": "<tool name>", "args": {...}}

Available tools:
%s
"""


@dataclass
class RunResult:
    run: str
    branch: str
    status: str = "completed"
    verified: Optional[bool] = None
    steps: int = 0
    summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    verify_command: str = ""
    verify_output: str = ""
    step_log: List[StepInfo] = field(default_factory=list)
    signatures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.status not in ("completed", "verified"):
            return False
        return self.verified is not False


class Agent:
    def __init__(
        self,
        root: str,
        cfg: Config,
        ledger: Ledger,
        model: ModelClient,
        toolbox: tools.Toolbox,
        on_step: Optional[Callable[[StepInfo, tools.ToolResult, Reply], None]] = None,
        on_notice: Optional[Callable[[str], None]] = None,
    ):
        self.root = os.path.abspath(root)
        self.cfg = cfg
        self.ledger = ledger
        self.model = model
        self.toolbox = toolbox
        self.on_step = on_step or (lambda *a: None)
        self.on_notice = on_notice or (lambda text: None)

    # -- prompts ----------------------------------------------------------
    def _system_prompt(self) -> str:
        files = snapshot.list_files(self.root)
        listing = "\n".join("  " + f for f in files[:80])
        if len(files) > 80:
            listing += "\n  ... and %d more files" % (len(files) - 80)
        parts = [SYSTEM]
        if not self.cfg.native_tools:
            parts.append(JSON_PROTOCOL % tools.tool_help())
        # Deliberately path-relative: keeping absolute paths out of the prompt is
        # what lets `agit replay` reproduce a run byte for byte in another
        # directory, since the prompt hash is the cassette key.
        parts.append("All paths are relative to the workspace root.\n"
                     "Files agit can see:\n%s" % (listing or "  (empty)"))
        if self.cfg.verify:
            parts.append(
                "Definition of done: the command `%s` must exit 0. It runs automatically "
                "when you call finish, and the run is only marked VERIFIED if it passes."
                % self.cfg.verify
            )
        if not self.cfg.allow_commands:
            parts.append("run_command needs human approval each time, so use it deliberately.")
        return "\n\n".join(parts)

    # -- main loop --------------------------------------------------------
    def run(self, task: str, fork_from: str = "", label: str = "",
            start_step: int = 0, run_id: str = "") -> RunResult:
        started = time.time()
        info: RunInfo = self.ledger.begin_run(
            task=task, provider=self.cfg.provider, model=self.cfg.model,
            label=label, fork_from=fork_from, run_id=run_id,
        )
        step_no = start_step or info.steps
        if not fork_from:
            # Step 0 records the starting state, so blame can tell human code
            # from agent code. A fork already inherits that history.
            self.ledger.commit_step(
                {"run": info.run, "step": step_no, "kind": "baseline", "tool": "",
                 "target": "", "ok": True, "model": self.cfg.model,
                 "rationale": "Baseline snapshot of the workspace before any change."},
                {"task.md": task + "\n"},
                workspace_root=self.root,
                snapshot_max_bytes=self.cfg.snapshot_max_bytes,
            )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": task},
        ]
        result = RunResult(run=info.run, branch=info.branch, verify_command=self.cfg.verify)
        stalls = 0
        tool_specs = tools.openai_tools()

        while True:
            if step_no - (start_step or info.steps) >= self.cfg.max_steps:
                result.status = "exhausted"
                self.on_notice("step budget of %d reached" % self.cfg.max_steps)
                break
            if self.model.tokens > self.cfg.budget_tokens:
                result.status = "exhausted"
                self.on_notice("token budget of %d reached" % self.cfg.budget_tokens)
                break
            try:
                reply = self.model.complete(_compact(messages), tool_specs)
            except ReplayMiss as exc:
                result.status = "diverged"
                result.summary = str(exc)
                break
            except ModelError as exc:
                result.status = "error"
                result.summary = str(exc)
                break

            call = reply.call
            if call is None:
                stalls += 1
                messages.append({"role": "assistant", "content": reply.text or ""})
                if stalls >= 2:
                    result.status = "stalled"
                    result.summary = (reply.text or "model stopped calling tools").strip()[:2000]
                    break
                messages.append({
                    "role": "user",
                    "content": "You replied without calling a tool. Call exactly one tool now, "
                               "or call finish if the task is done.",
                })
                self.on_notice("model replied without a tool call; nudging")
                continue
            stalls = 0

            step_no += 1
            verify_record: Dict[str, Any] = {}
            tool_result = self.toolbox.run(call.name, call.args)

            if tool_result.terminal and self.cfg.verify:
                verify_record = self._verify()
                if not verify_record["ok"]:
                    tool_result = tools.ToolResult(
                        False,
                        "NOT DONE. The verify command failed, so the task is not complete:\n"
                        "$ %s\nexit=%s\n%s\nFix the cause and try again."
                        % (self.cfg.verify, verify_record["code"], verify_record["output"]),
                        target=self.cfg.verify,
                    )
                else:
                    tool_result.output = (tool_result.output +
                                          "\n\nverify passed: $ %s" % self.cfg.verify)

            meta = {
                "run": info.run,
                "step": step_no,
                "kind": "finish" if tool_result.terminal else "tool",
                "tool": call.name,
                "target": tool_result.target or _target_of(call),
                "args": _safe_args(call.args),
                "ok": tool_result.ok,
                "denied": tool_result.denied,
                "rationale": (reply.text or "").strip(),
                "model": reply.model or self.cfg.model,
                "tokens_in": reply.tokens_in,
                "tokens_out": reply.tokens_out,
                "duration_ms": reply.duration_ms,
                "cached": reply.cached,
                "diffstat": tool_result.diffstat,
                "signature": call.signature(),
            }
            if verify_record:
                meta["verify"] = {
                    "command": self.cfg.verify,
                    "state": "pass" if verify_record["ok"] else "fail",
                    "code": verify_record["code"],
                }
            artifacts = {
                "request.json": json.dumps(_redact_messages(messages), indent=1)[:400_000],
                "response.json": json.dumps(reply.raw, indent=1)[:200_000],
                "result.txt": tool_result.output,
            }
            if verify_record:
                artifacts["verify.txt"] = "$ %s\nexit=%s\n%s" % (
                    self.cfg.verify, verify_record["code"], verify_record["output"])

            sha = self.ledger.commit_step(
                meta, artifacts, workspace_root=self.root,
                snapshot_max_bytes=self.cfg.snapshot_max_bytes,
            )
            step = StepInfo(
                sha=sha, step=step_no, run=info.run, tool=call.name,
                target=meta["target"], ok=tool_result.ok, kind=meta["kind"],
                rationale=meta["rationale"], model=meta["model"],
                tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                duration_ms=reply.duration_ms,
                verify=(meta.get("verify") or {}).get("state", ""),
                added=(tool_result.diffstat or {}).get("added", 0),
                removed=(tool_result.diffstat or {}).get("removed", 0),
            )
            result.step_log.append(step)
            result.signatures.append(call.signature())
            self.on_step(step, tool_result, reply)

            messages.extend(_turn_messages(reply, call, tool_result, self.cfg.native_tools))

            if tool_result.terminal and tool_result.ok:
                result.status = "completed"
                result.summary = str(call.args.get("summary", "")).strip()
                if verify_record:
                    result.verified = True
                    result.verify_output = verify_record["output"]
                break

        result.steps = step_no
        result.tokens_in = self.model.tokens_in
        result.tokens_out = self.model.tokens_out
        result.duration_ms = int((time.time() - started) * 1000)

        if result.status == "completed" and self.cfg.verify and result.verified is None:
            final = self._verify()
            result.verified = final["ok"]
            result.verify_output = final["output"]
        if self.cfg.verify and result.status != "completed":
            result.verified = False

        self.ledger.update_run(
            status=result.status,
            steps=step_no,
            verified=result.verified,
            ended_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            tokens=result.tokens_in + result.tokens_out,
        )
        self.ledger.commit_step(
            {"run": info.run, "step": step_no + 1, "kind": "run-end", "tool": "",
             "target": "", "ok": result.ok, "model": self.cfg.model,
             "rationale": "Run %s: %s%s\n\n%s" % (
                 info.run, result.status,
                 "" if result.verified is None else (
                     " (verified)" if result.verified else " (NOT verified)"),
                 result.summary or ""),
             },
            {"result.json": json.dumps({
                "status": result.status, "verified": result.verified,
                "summary": result.summary, "steps": result.steps,
                "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
                "duration_ms": result.duration_ms,
                "verify_command": result.verify_command,
                "signatures": result.signatures,
                # Everything replay needs to rebuild an identical prompt.
                "config": {
                    "provider": self.cfg.provider,
                    "model": self.cfg.model,
                    "temperature": self.cfg.temperature,
                    "verify": self.cfg.verify,
                    "allow_commands": self.cfg.allow_commands,
                    "native_tools": self.cfg.native_tools,
                    "max_steps": self.cfg.max_steps,
                },
            }, indent=2) + "\n"},
            workspace_root=None,
        )
        return result

    # -- verify -----------------------------------------------------------
    def _verify(self) -> Dict[str, Any]:
        self.on_notice("verify: %s" % self.cfg.verify)
        out = tools.shell(self.cfg.verify, self.root, timeout=600)
        return {"ok": out["code"] == 0, "code": out["code"], "output": out["output"]}


# -- message plumbing -----------------------------------------------------

def _turn_messages(reply: Reply, call: ToolCall, result: tools.ToolResult,
                   native: bool) -> List[Dict[str, Any]]:
    body = result.truncated()
    status = "ok" if result.ok else "failed"
    if native:
        return [
            {
                "role": "assistant",
                "content": reply.text or "",
                "tool_calls": [{
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name,
                                 "arguments": json.dumps(call.args, ensure_ascii=False)},
                }],
            },
            {"role": "tool", "tool_call_id": call.id, "name": call.name,
             "content": "[%s] %s" % (status, body)},
        ]
    return [
        {"role": "assistant", "content": json.dumps(
            {"rationale": reply.text, "tool": call.name, "args": call.args}, ensure_ascii=False)},
        {"role": "user", "content": "Result of %s [%s]:\n%s" % (call.name, status, body)},
    ]


def _compact(messages: List[Dict[str, Any]], keep_full: int = 8,
             summary_limit: int = 240) -> List[Dict[str, Any]]:
    """Keep the prompt bounded: older tool results collapse to one line each."""
    out: List[Dict[str, Any]] = []
    tool_indexes = [i for i, m in enumerate(messages)
                    if m.get("role") == "tool" or (
                        m.get("role") == "user" and str(m.get("content", "")).startswith("Result of "))]
    stale = set(tool_indexes[:-keep_full]) if len(tool_indexes) > keep_full else set()
    for i, message in enumerate(messages):
        if i in stale:
            content = str(message.get("content", ""))
            squeezed = content[:summary_limit].replace("\n", " ")
            trimmed = dict(message)
            trimmed["content"] = squeezed + (
                " ...[older result trimmed]" if len(content) > summary_limit else "")
            out.append(trimmed)
        else:
            out.append(message)
    return out


def _safe_args(args: Dict[str, Any], limit: int = 4000) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > limit:
            safe[key] = value[:limit] + "...[%d chars]" % len(value)
        else:
            safe[key] = value
    return safe


def _redact_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in m.items() if k != "api_key"} for m in messages]


def _target_of(call: ToolCall) -> str:
    for key in ("path", "command", "pattern"):
        value = call.args.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return ""
