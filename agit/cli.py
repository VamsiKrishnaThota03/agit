"""agit command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from . import blame as blame_mod
from . import config as config_mod
from . import dashboard, gitutil, report, snapshot, tools
from .agent import Agent, RunResult
from .config import AGIT_DIR, PRESETS, Config
from .ledger import Ledger, LedgerError, RunInfo, StepInfo
from .model import Cassette, ModelClient, ModelError
from .script import ScriptError, ScriptProvider
from .ui import Console, human_ms, human_tokens

VERSION = "1.0.0"

EPILOG = """examples
  agit demo                                 offline, no API key, 20 seconds
  agit run "add retry to fetch()" --verify "pytest -q"
  agit log                                  the run as a list of steps
  agit blame src/api.py                     which step wrote each line
  agit why src/api.py 42                    the rationale behind one line
  agit fork 4 "try a simpler fix"           branch the agent at step 4
  agit diff @last run/1110-ab12             compare two attempts
  agit replay @last                         re-drive the run with no network
  agit report @last -o run.html             shareable audit trail
  agit dashboard --open                     every run, visually, in a browser

the ledger is real git:
  git -C .agit/ledger log --graph --all --oneline
"""


# -- helpers --------------------------------------------------------------

def find_root(start: str = ".") -> str:
    here = os.path.abspath(start)
    probe = here
    while True:
        if os.path.isdir(os.path.join(probe, AGIT_DIR)):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return here
        probe = parent


def short_path(path: str) -> str:
    """Relative when that is shorter and still inside the tree, else absolute."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def build_config(args: argparse.Namespace, root: str) -> Config:
    overrides: Dict[str, Any] = {}
    for key in ("provider", "model", "base_url", "verify", "temperature",
                "max_steps", "budget_tokens"):
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = value
    if getattr(args, "yes", False):
        overrides["allow_commands"] = True
    if getattr(args, "no_native_tools", False):
        overrides["native_tools"] = False
    return config_mod.resolve(root, overrides)


def make_client(cfg: Config, root: str, script_path: str = "",
                pace_ms: int = 0) -> ModelClient:
    cassette = Cassette(os.path.join(root, AGIT_DIR, "cassette.json"))
    provider = None
    if cfg.offline:
        provider = ScriptProvider.discover(root, script_path, pace_ms=pace_ms)
    elif script_path:
        raise ScriptError("--script only applies to --provider script")
    return ModelClient(cfg, cassette=cassette, script=provider)


def confirm_command(console: Console) -> Any:
    def ask(command: str) -> bool:
        t = console.theme
        console.write("")
        console.write("  " + t.yellow("the agent wants to run a command:"))
        console.write("    " + t.bold(command))
        if not sys.stdin.isatty():
            console.note("  no tty, so declining. Pass --yes to allow commands.")
            return False
        try:
            answer = input("  allow? [y/N/a=allow all] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("a", "all"):
            ask.allow_all = True  # type: ignore[attr-defined]
            return True
        return answer in ("y", "yes")

    def wrapper(command: str) -> bool:
        if getattr(ask, "allow_all", False):
            return True
        return ask(command)

    return wrapper


def step_label(console: Console, step: StepInfo) -> str:
    t = console.theme
    mark = t.green(t.dot) if step.ok else t.red(t.dot)
    if step.kind == "finish":
        mark = t.saffron(t.dot)
    tool = {"write_file": "write", "edit_file": "edit", "read_file": "read",
            "run_command": "run", "list_dir": "ls", "search": "grep",
            "finish": "finish"}.get(step.tool, step.tool or step.kind)
    bits = "  %s %s %s %s" % (
        mark,
        t.dim("step %02d" % step.step),
        t.violet(tool.ljust(6)),
        step.target[:46],
    )
    tail: List[str] = []
    if step.added or step.removed:
        tail.append(t.green("+%d" % step.added) + " " + t.red("-%d" % step.removed))
    if step.tokens:
        tail.append(t.dim("%s tok" % human_tokens(step.tokens)))
    if step.verify == "pass":
        tail.append(t.green(t.tick + " verified"))
    elif step.verify == "fail":
        tail.append(t.red(t.cross + " verify failed"))
    return bits + ("   " + "  ".join(tail) if tail else "")


# -- commands -------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    console = Console()
    root = os.path.abspath(args.path or ".")
    os.makedirs(root, exist_ok=True)
    ledger = Ledger.ensure(root)
    console.banner()
    console.ok("ledger ready at %s" % os.path.relpath(ledger.path, root))
    added = ensure_gitignore(root)
    if added:
        console.ok("added .agit/ to .gitignore (the ledger is yours, not your project's history)")
    cfg = config_mod.resolve(root)
    console.kv("provider", cfg.provider)
    console.kv("model", cfg.model or "(unset)")
    console.write("")
    console.note("next:  agit run \"your task\" --verify \"<your test command>\"")
    console.note("       agit doctor     to check model access")
    return 0


def ensure_gitignore(root: str) -> bool:
    path = os.path.join(root, ".gitignore")
    line = ".agit/"
    try:
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read()
            if any(l.strip() in (".agit", ".agit/") for l in existing.splitlines()):
                return False
        elif not gitutil.is_repo(root):
            return False
        with open(path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("# agit agent ledger\n%s\n" % line)
        return True
    except OSError:
        return False


def cmd_doctor(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    cfg = build_config(args, root)
    console.banner("environment check")
    console.kv("agit", VERSION)
    console.kv("python", sys.version.split()[0])
    console.kv("git", (gitutil.git(".", "--version").strip() if gitutil.available() else "MISSING"))
    console.kv("workspace", root)
    console.kv("ledger", "present" if Ledger(root).exists else "not created yet")
    console.write("")
    console.kv("provider", cfg.provider)
    console.kv("base url", cfg.base_url or "(unset)")
    console.kv("model", cfg.model or "(unset)")
    console.kv("api key", "set" if cfg.api_key else "unset")
    console.write("")

    problems = config_mod.check(cfg)
    if cfg.offline:
        console.ok("offline scripted provider: agit demo and the test suite work with no key")
    if problems:
        for p in problems:
            console.fail(p)
        console.write("")
        console.rule("free ways to get a model")
        for name, preset in PRESETS.items():
            if name == "script":
                continue
            console.bullet("%s %s %s" % (console.theme.bold(name.ljust(18)),
                                         console.theme.dim("-"), preset["free"]))
            console.note("     " + preset["hint"])
    else:
        console.ok("configuration looks complete")

    if args.ping and not cfg.offline and not problems:
        console.write("")
        console.note("pinging %s ..." % cfg.endpoint())
        client = ModelClient(cfg, cassette=None)
        try:
            reply = client.complete(
                [{"role": "user", "content": "Reply with the single word: ready"}], tools=None)
            console.ok("model replied: %s" % (reply.text or "(empty)")[:80])
        except ModelError as exc:
            console.fail(str(exc))
            return 1
    return 1 if problems and not cfg.offline else 0


def cmd_run(args: argparse.Namespace) -> int:
    console = Console(quiet=args.quiet)
    root = find_root()
    cfg = build_config(args, root)
    if args.cassette:
        cfg.cassette_mode = args.cassette

    problems = config_mod.check(cfg)
    if problems:
        console.banner()
        for p in problems:
            console.fail(p)
        console.note("run `agit doctor` for free provider options")
        return 2

    task = args.task.strip()
    if not task:
        console.fail("give the agent a task")
        return 2

    ledger = Ledger.ensure(root)
    ensure_gitignore(root)
    try:
        client = make_client(cfg, root, getattr(args, "script", "") or "")
    except ScriptError as exc:
        console.fail(str(exc))
        return 2

    fork_ref = ""
    if args.fork:
        fork_ref, _ = resolve_step_ref(ledger, args.fork, args.run)
    toolbox = tools.Toolbox(root, allow_commands=cfg.allow_commands,
                            confirm=confirm_command(console))

    console.banner()
    console.kv("task", task[:120])
    console.kv("provider", "%s  %s" % (cfg.provider, console.theme.dim(cfg.model)))
    if cfg.verify:
        console.kv("done when", cfg.verify)
    if fork_ref:
        console.kv("forked from", args.fork)
    console.write("")

    agent = Agent(
        root, cfg, ledger, client, toolbox,
        on_step=lambda step, result, reply: _render_step(console, step, result, args.verbose),
        on_notice=lambda text: console.note("  " + text),
    )
    try:
        result = agent.run(task, fork_from=fork_ref, label=args.label or "")
    except KeyboardInterrupt:
        console.write("")
        console.warn("interrupted. The ledger keeps every completed step: agit log")
        return 130
    finally:
        if client.cassette:
            client.cassette.save()

    _render_outcome(console, result, cfg)
    return 0 if result.ok else 1


def _render_step(console: Console, step: StepInfo, result: tools.ToolResult,
                 verbose: bool) -> None:
    console.write(step_label(console, step))
    if step.rationale:
        console.write("      " + console.theme.dim(step.rationale.splitlines()[0][:96]))
    if step.tool in ("write_file", "edit_file") and result.output:
        diff = "\n".join(l for l in result.output.splitlines() if l.startswith(("+", "-", "@@")))
        if diff:
            console.diff(diff, limit=8 if not verbose else 60, indent="      ")
    elif verbose and result.output:
        console.block(result.output, limit=12, indent="      ")
    elif not result.ok and result.output:
        console.block(result.output, limit=4, indent="      ")


def _render_outcome(console: Console, result: RunResult, cfg: Config) -> None:
    t = console.theme
    console.write("")
    if result.verified:
        console.write("  " + t.inverse(t.green(" VERIFIED ")) + " " + t.dim(cfg.verify))
    elif result.verified is False:
        console.write("  " + t.inverse(t.red(" NOT VERIFIED ")) + " " + t.dim(cfg.verify or result.status))
    elif result.status == "completed":
        console.write("  " + t.inverse(t.saffron(" DONE ")) + t.dim("  (no --verify command given)"))
    else:
        console.write("  " + t.inverse(t.red(" " + result.status.upper() + " ")))
    if result.summary:
        console.write("")
        for line in result.summary.splitlines()[:8]:
            console.write("  " + line)
    console.write("")
    console.kv("run", result.run)
    done = len(result.step_log)
    console.kv("steps", str(done) if done == result.steps
               else "%d new (run is %d steps deep)" % (done, result.steps))
    console.kv("tokens", "%s in / %s out" % (human_tokens(result.tokens_in),
                                             human_tokens(result.tokens_out)))
    console.kv("wall time", human_ms(result.duration_ms))
    console.write("")
    console.note("agit log                 every step, in order")
    console.note("agit blame <file>        which step wrote each line")
    console.note("agit report @last -o run.html")


def cmd_runs(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    runs = ledger.runs()
    if not runs:
        console.note("no runs yet")
        return 0
    t = console.theme
    rows = []
    for info in runs:
        verdict = (t.green("verified") if info.verified else
                   (t.red("unverified") if info.verified is False else t.dim(info.status)))
        rows.append([
            info.run,
            verdict,
            str(info.steps),
            human_tokens(info.tokens),
            (info.forked_from or "-"),
            (info.task or "")[:48],
        ])
    console.write("")
    console.table(["run", "outcome", "steps", "tokens", "forked from", "task"], rows)
    console.write("")
    console.note("agit log --run <id> · agit diff <a> <b> · agit report <id>")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    if args.graph:
        console.write("")
        console.write(ledger.graph())
        return 0
    info = ledger.resolve_run(args.run)
    steps = ledger.steps(info.branch)
    t = console.theme
    console.write("")
    console.write("  " + t.bold(info.run) + "  " + t.dim(info.task[:70]))
    console.write("")
    for step in steps:
        if step.kind == "baseline":
            console.write("  " + t.dim("%s baseline  %s" % (t.ring, "workspace before the agent touched it")))
            continue
        if step.kind == "run-end":
            continue
        console.write(step_label(console, step))
        if step.rationale and not args.oneline:
            console.write("      " + t.dim(step.rationale.splitlines()[0][:96]))
    console.write("")
    verdict = ("verified" if info.verified else
               ("not verified" if info.verified is False else info.status))
    console.kv("outcome", verdict)
    console.kv("tokens", human_tokens(info.tokens))
    console.note("agit show <step> --run %s" % info.run)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    info = ledger.resolve_run(args.run)
    try:
        step = ledger.step(int(args.step), info.branch)
    except (ValueError, LedgerError) as exc:
        console.fail(str(exc))
        return 2
    meta = ledger.step_meta(step)
    t = console.theme
    console.write("")
    console.rule("step %02d  %s  %s" % (step.step, step.tool or step.kind, step.target))
    console.kv("commit", step.sha[:12])
    console.kv("model", step.model)
    console.kv("tokens", "%d in / %d out" % (step.tokens_in, step.tokens_out))
    console.kv("latency", human_ms(step.duration_ms))
    console.kv("outcome", t.green("ok") if step.ok else t.red("failed"))
    if step.verify:
        console.kv("verify", step.verify)
    if meta.get("cached"):
        console.kv("source", "cassette (no network call)")
    if step.rationale:
        console.write("")
        console.write("  " + t.bold("why"))
        for line in step.rationale.splitlines():
            console.write("  " + line)
    args_preview = meta.get("args") or {}
    if args_preview:
        console.write("")
        console.write("  " + t.bold("arguments"))
        console.block(json.dumps(args_preview, indent=2)[:4000], limit=40, indent="    ")
    diff = ledger.step_diff(step)
    if diff.strip():
        console.write("")
        console.write("  " + t.bold("workspace diff"))
        console.diff(diff, limit=200 if args.full else 40)
    result = ledger.step_artifact(step, "result.txt")
    if result and result.strip():
        console.write("")
        console.write("  " + t.bold("tool result the model saw"))
        console.block(result, limit=200 if args.full else 20)
    if args.io:
        for name in ("request.json", "response.json"):
            body = ledger.step_artifact(step, name)
            if body:
                console.write("")
                console.write("  " + t.bold(name))
                console.block(body, limit=400, indent="    ")
    return 0


def cmd_blame(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    info = ledger.resolve_run(args.run)
    t = console.theme
    try:
        lines = blame_mod.blame_file(ledger, args.path, info.branch)
    except LedgerError as exc:
        console.fail(str(exc))
        return 2
    start, end = 1, len(lines)
    if args.lines:
        start, end = parse_line_range(args.lines, len(lines))
    console.write("")
    console.rule("%s  %s" % (args.path, t.dim("run " + info.run)))
    for row in lines:
        if not (start <= row.lineno <= end):
            continue
        if row.step is None or row.step.kind == "baseline":
            tag = t.dim("human   ")
        else:
            tag = t.saffron("step %02d" % row.step.step) + " "
        console.write("  %s %s %s %s" % (
            tag, t.dim("%4d" % row.lineno), t.dim(t.pipe), row.text[:100]))
    agent_lines, total = blame_mod.summarise_authorship(lines)
    console.write("")
    pct = (100.0 * agent_lines / total) if total else 0.0
    console.kv("agent lines", "%d of %d (%.0f%%)" % (agent_lines, total, pct))
    console.note("agit why %s <line>   for the reasoning behind one line" % args.path)
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    info = ledger.resolve_run(args.run)
    path, line = split_path_line(args.path, args.line)
    if line is None:
        console.fail("give a line number: agit why %s 42" % path)
        return 2
    try:
        exp = blame_mod.explain_line(ledger, path, line, info.branch)
    except LedgerError as exc:
        console.fail(str(exc))
        return 2
    t = console.theme
    console.write("")
    console.write("  " + t.dim("%s:%d" % (path, line)))
    console.write("  " + t.bold(exp.text[:120] or "(blank line)"))
    console.write("  " + t.dim(t.hbar * min(console.width - 4, 72)))
    if exp.step is None or exp.step.kind == "baseline":
        console.write("  " + t.dim("written by a human before this run started"))
        return 0
    console.kv("author", "agit step %d" % exp.step.step)
    console.kv("tool", "%s %s" % (exp.tool, t.dim(exp.args_preview)))
    console.kv("model", exp.step.model)
    console.kv("when", exp.step.at[:19].replace("T", " "))
    if exp.verify:
        console.kv("verify", exp.verify)
    console.write("")
    console.write("  " + t.bold("because"))
    console.paragraph(exp.rationale or "(the model gave no rationale for this step)")
    if exp.run_task:
        console.write("")
        console.write("  " + t.dim("task".ljust(14)) + t.dim("asked of the agent:"))
        console.paragraph(exp.run_task, indent="  ", dim=True)
    if exp.diff.strip() and args.diff:
        console.write("")
        console.write("  " + t.bold("the change that introduced this line"))
        console.diff(exp.diff, limit=60)
    console.write("")
    console.note("full model request/response: git -C .agit/ledger show %s" % exp.request_ref)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    console = Console()
    ledger = Ledger(find_root()).require()
    left = ledger.resolve_run(args.left).branch
    right = ledger.resolve_run(args.right).branch
    diff = ledger.diff_refs(left, right)
    console.write("")
    console.rule("%s  ->  %s" % (args.left, args.right))
    if not diff.strip():
        console.note("the two runs produced an identical workspace")
        return 0
    console.diff(diff, limit=600 if args.full else 120, indent="  ")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    sha, info = resolve_step_ref(ledger, args.step, args.run)
    cfg = build_config(args, root)
    if args.cassette:
        cfg.cassette_mode = args.cassette
    task = (args.task or "").strip() or info.task
    console.banner("forking %s at step %s" % (info.run, args.step))
    stats = ledger.restore_workspace(sha, root)
    console.ok("workspace rewound to step %s (%d files written, %d removed)"
               % (args.step, stats["written"], stats["removed"]))
    if args.no_run:
        console.note("stopped before running (--no-run). Start the branch with:")
        console.note("  agit run \"%s\" --fork %s --run %s" % (task[:40], args.step, info.run))
        return 0
    problems = config_mod.check(cfg)
    if problems:
        for p in problems:
            console.fail(p)
        return 2
    try:
        client = make_client(cfg, root, getattr(args, "script", "") or "")
    except ScriptError as exc:
        console.fail(str(exc))
        return 2
    toolbox = tools.Toolbox(root, allow_commands=cfg.allow_commands,
                            confirm=confirm_command(console))
    agent = Agent(root, cfg, ledger, client, toolbox,
                  on_step=lambda s, r, rep: _render_step(console, s, r, args.verbose),
                  on_notice=lambda text: console.note("  " + text))
    console.kv("task", task[:110])
    console.write("")
    try:
        result = agent.run(task, fork_from=sha, label=args.label or "fork",
                           start_step=int(args.step))
    finally:
        if client.cassette:
            client.cassette.save()
    _render_outcome(console, result, cfg)
    console.note("agit diff %s %s   to compare the two attempts" % (info.run, result.run))
    return 0 if result.ok else 1


def cmd_goto(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    sha, info = resolve_step_ref(ledger, args.step, args.run)
    stats = ledger.restore_workspace(sha, root)
    console.write("")
    console.ok("workspace restored to run %s step %s" % (info.run, args.step))
    console.kv("written", str(stats["written"]))
    console.kv("removed", str(stats["removed"]))
    console.note("the ledger is append-only: nothing was lost, agit goto forward again anytime")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    info = ledger.resolve_run(args.run)
    steps = [s for s in ledger.steps(info.branch) if s.kind in ("tool", "finish", "baseline")]
    if len(steps) < 2:
        console.fail("nothing to undo in run %s" % info.run)
        return 1
    back = max(1, int(args.steps))
    target = steps[max(0, len(steps) - 1 - back)]
    sha = target.sha
    stats = ledger.restore_workspace(sha, root)
    console.write("")
    console.ok("rewound %d step(s): workspace is now as it was after step %02d"
               % (back, target.step))
    console.kv("written", str(stats["written"]))
    console.kv("removed", str(stats["removed"]))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    info = ledger.resolve_run(args.run_spec)
    recorded = [s for s in ledger.steps(info.branch) if s.kind in ("tool", "finish")]
    baseline = next((s for s in ledger.steps(info.branch) if s.kind == "baseline"), None)
    if baseline is None:
        console.fail("run %s has no baseline snapshot to replay from" % info.run)
        return 1
    recorded_sigs = _recorded_signatures(ledger, info, recorded)

    target = args.into or os.path.join(root, AGIT_DIR, "replays", info.run)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    ledger.restore_workspace(baseline.sha, target)

    cfg = build_config(args, root)
    cfg.cassette_mode = "live" if args.live else "replay"
    # Rebuild the recorded run's prompt-affecting settings, or the prompt hash
    # changes and every step looks like a divergence.
    recorded_cfg = recorded_config(ledger, info)
    for key in ("verify", "allow_commands", "native_tools", "temperature", "max_steps"):
        if key in recorded_cfg:
            setattr(cfg, key, recorded_cfg[key])
    cfg.model = recorded_cfg.get("model") or info.model or cfg.model
    if not args.live:
        cfg.provider = recorded_cfg.get("provider") or info.provider or cfg.provider
    allowed_commands = {s.target for s in recorded if s.tool == "run_command"}

    console.banner("replaying %s%s" % (info.run, "  (live)" if args.live else "  (from cassette)"))
    console.kv("task", info.task[:110])
    console.kv("into", short_path(target))
    console.kv("mode", cfg.cassette_mode)
    console.write("")

    try:
        client = make_client(cfg, root, getattr(args, "script", "") or "")
    except ScriptError as exc:
        console.fail(str(exc))
        return 2
    toolbox = tools.Toolbox(
        target,
        allow_commands=False,
        confirm=lambda command: command in allowed_commands,
    )
    agent = Agent(target, cfg, ledger, client, toolbox,
                  on_step=lambda s, r, rep: _render_step(console, s, r, args.verbose),
                  on_notice=lambda text: console.note("  " + text))
    try:
        result = agent.run(info.task, label="replay-of-" + info.run)
    finally:
        if client.cassette:
            client.cassette.save()

    console.write("")
    console.rule("divergence")
    diverged_at = first_divergence(recorded_sigs, result.signatures)
    if result.status == "diverged":
        at = len(result.signatures) + 1
        console.fail("the cassette has no recorded answer for the prompt at step %d" % at)
        console.note("  %s" % result.summary.splitlines()[0][:140])
        console.note("  the workspace or settings differ from the recorded run; "
                     "use --live to re-ask the model")
        return 1
    if diverged_at is None and len(recorded_sigs) == len(result.signatures):
        console.ok("identical: %d/%d steps matched the recorded run"
                   % (len(result.signatures), len(recorded_sigs)))
    elif diverged_at is None:
        console.warn("same prefix, different length: recorded %d steps, replay %d steps"
                     % (len(recorded_sigs), len(result.signatures)))
    else:
        console.fail("diverged at step %d" % (diverged_at + 1))
        console.write("    " + console.theme.dim("recorded: ") +
                      _sig(recorded_sigs, diverged_at))
        console.write("    " + console.theme.dim("replay:   ") +
                      _sig(result.signatures, diverged_at))
    if cfg.verify:
        console.kv("verify", "pass" if result.verified else "fail")
    console.note("replay wrote to %s and left your workspace untouched"
                 % os.path.relpath(target, root))
    return 0 if diverged_at is None else 1


def _sig(sigs: List[str], index: int) -> str:
    return sigs[index][:100] if index < len(sigs) else "(run ended)"


def _recorded_signatures(ledger: Ledger, info: RunInfo, recorded: List[StepInfo]) -> List[str]:
    end = next((s for s in ledger.steps(info.branch) if s.kind == "run-end"), None)
    if end is not None:
        raw = ledger.step_artifact(end, "result.json")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data.get("signatures"), list) and data["signatures"]:
                    return [str(s) for s in data["signatures"]]
            except ValueError:
                pass
    sigs = []
    for step in recorded:
        meta = ledger.step_meta(step)
        sigs.append(str(meta.get("signature") or "%s(%s)" % (step.tool, step.target)))
    return sigs


def recorded_config(ledger: Ledger, info: RunInfo) -> Dict[str, Any]:
    """The config a recorded run actually used, from its run-end commit."""
    end = next((s for s in ledger.steps(info.branch) if s.kind == "run-end"), None)
    if end is None:
        return {}
    raw = ledger.step_artifact(end, "result.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    found = dict(data.get("config") or {})
    if data.get("verify_command") and "verify" not in found:
        found["verify"] = data["verify_command"]
    return found


def first_divergence(a: List[str], b: List[str]) -> Optional[int]:
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None


def cmd_report(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    info = ledger.resolve_run(args.run_spec)
    out = args.output or os.path.join(root, "agit-run-%s.html" % info.run)
    path = report.render(ledger, info, out, include_io=not args.no_io)
    console.write("")
    console.ok("wrote %s" % os.path.relpath(path, root))
    console.note("open it in a browser: no network, no JS dependencies, safe to attach to a PR")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    if not ledger.runs():
        console.fail("no runs to show yet. Try `agit demo` or `agit run \"<task>\"`.")
        return 1
    out = args.output or os.path.join(root, "agit-dashboard.html")
    path = dashboard.build(ledger, out, include_io=args.io)
    size = os.path.getsize(path)
    console.write("")
    console.ok("wrote %s (%.0f KB)" % (short_path(path), size / 1024.0))
    console.note("every run in the ledger: timeline, blame, spend. One static file,")
    console.note("no server and no network, so it opens by double-clicking.")
    if args.open:
        _open_in_browser(path, console)
    else:
        console.note("open it with: agit dashboard --open")
    return 0


def _open_in_browser(path: str, console: Console) -> None:
    import webbrowser
    try:
        if webbrowser.open("file://" + path):
            console.ok("opened in your browser")
            return
    except Exception:
        pass
    console.note("could not launch a browser; open the file manually")


def cmd_stats(args: argparse.Namespace) -> int:
    console = Console()
    root = find_root()
    ledger = Ledger(root).require()
    info = ledger.resolve_run(args.run)
    steps = [s for s in ledger.steps(info.branch) if s.kind in ("tool", "finish")]
    by_tool: Dict[str, int] = {}
    for step in steps:
        by_tool[step.tool or "?"] = by_tool.get(step.tool or "?", 0) + 1
    files = sorted({s.target for s in steps if s.tool in ("write_file", "edit_file") and s.target})
    console.write("")
    console.rule("run %s" % info.run)
    console.kv("task", info.task[:100])
    console.kv("steps", str(len(steps)))
    console.kv("tokens", human_tokens(sum(s.tokens for s in steps)))
    console.kv("model time", human_ms(sum(s.duration_ms for s in steps)))
    console.kv("lines", "+%d / -%d" % (sum(s.added for s in steps), sum(s.removed for s in steps)))
    console.write("")
    console.table(["tool", "calls"], sorted(by_tool.items(), key=lambda kv: -kv[1]))
    if files:
        console.write("")
        rows = []
        for rel in files:
            try:
                lines = blame_mod.blame_file(ledger, rel, info.branch)
            except LedgerError:
                continue
            agent_lines, total = blame_mod.summarise_authorship(lines)
            rows.append([rel, "%d/%d" % (agent_lines, total),
                         "%.0f%%" % (100.0 * agent_lines / total if total else 0)])
        console.table(["file touched", "agent/total lines", "agent share"], rows)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    console = Console()
    here = os.path.dirname(os.path.abspath(__file__))
    demo_src = os.path.join(here, "demo")
    workspace_src = os.path.join(demo_src, "workspace")
    script_path = os.path.join(demo_src, "script.json")
    alt_script = os.path.join(demo_src, "script-stdlib.json")
    if not os.path.isdir(workspace_src) or not os.path.isfile(script_path):
        console.fail("demo files are missing from this install (%s)" % demo_src)
        return 2

    target = os.path.abspath(args.path or "agit-demo")
    if os.path.exists(target):
        if not args.force:
            console.fail("%s already exists (use --force to replace it)" % target)
            return 2
        shutil.rmtree(target)
    shutil.copytree(workspace_src, target)

    cfg = config_mod.resolve(target, {
        "provider": "script",
        "verify": 'python3 -m unittest discover -q -p "test_*.py" .',
        "allow_commands": True,
        "max_steps": 12,
    })
    ledger = Ledger.ensure(target)
    client = ModelClient(
        cfg,
        cassette=Cassette(os.path.join(target, AGIT_DIR, "cassette.json")),
        script=ScriptProvider.from_file(script_path, pace_ms=0 if args.fast else 220),
    )
    toolbox = tools.Toolbox(target, allow_commands=True)

    console.banner("offline demo: no API key, no network, deterministic")
    console.kv("workspace", short_path(target))
    console.kv("planner", "bundled script (%s)" % os.path.basename(script_path))
    console.kv("done when", cfg.verify)
    console.write("")
    console.note("  the demo auto-approves the shell commands in the bundled script")
    console.write("")

    task = ("csvkit.parse_row splits on every comma, so commas inside quoted fields "
            "break the row. Fix it, with a test that proves it.")
    agent = Agent(target, cfg, ledger, client, toolbox,
                  on_step=lambda s, r, rep: _render_step(console, s, r, args.verbose),
                  on_notice=lambda text: console.note("  " + text))
    result = agent.run(task, label="demo")
    client.cassette.save()
    _render_outcome(console, result, cfg)

    console.rule("now try these, inside " + short_path(target))
    console.note("  agit log                       the run, step by step")
    console.note("  agit blame csvkit.py           human lines vs agent lines")
    console.note("  agit why csvkit.py 24          the reasoning behind one line")
    console.note("  agit replay @last              re-drive it with zero network calls")
    console.note("  agit dashboard --open          every run, visually, in a browser")
    console.note("  agit report @last -o run.html  shareable audit trail")
    console.note("  agit fork 4 'use the stdlib csv module instead' \\")
    console.note("       --provider script --script %s --yes" % short_path(alt_script))
    console.note("  agit diff @last <that new run>  compare the two solutions")
    console.note("  git -C .agit/ledger log --graph --all --oneline")
    return 0 if result.ok else 1


# -- ref parsing ----------------------------------------------------------

def resolve_step_ref(ledger: Ledger, step_spec: str, run_spec: str = "") -> Any:
    """'4' or 'run-id:4' -> (commit sha, RunInfo)."""
    spec = str(step_spec)
    if ":" in spec:
        run_part, _, step_part = spec.partition(":")
        info = ledger.resolve_run(run_part)
    else:
        info = ledger.resolve_run(run_spec)
        step_part = spec
    try:
        number = int(step_part)
    except ValueError:
        raise LedgerError("step must be a number, got %r" % step_part)
    step = ledger.step(number, info.branch)
    return step.sha, info


def parse_line_range(spec: str, total: int) -> Any:
    if "," in spec:
        left, _, right = spec.partition(",")
    elif ":" in spec:
        left, _, right = spec.partition(":")
    elif "-" in spec:
        left, _, right = spec.partition("-")
    else:
        left = right = spec
    try:
        start = int(left) if left.strip() else 1
        end = int(right) if right.strip() else total
    except ValueError:
        return 1, total
    return max(1, start), min(total, end)


def split_path_line(path: str, line: Optional[int]) -> Any:
    if line is not None:
        return path, int(line)
    if ":" in path:
        head, _, tail = path.rpartition(":")
        if tail.isdigit():
            return head, int(tail)
    return path, None


# -- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agit",
        description="git for AI agents: every step a commit, every line blamable.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="agit " + VERSION)
    sub = parser.add_subparsers(dest="command")

    def model_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", help="bharatcode | ollama | groq | openrouter | gemini | "
                                          "openai-compatible | script")
        p.add_argument("--model", help="model name")
        p.add_argument("--base-url", dest="base_url", help="API base URL ending in /v1")
        p.add_argument("--temperature", type=float)
        p.add_argument("--no-native-tools", action="store_true", dest="no_native_tools",
                       help="drive the model with a JSON protocol instead of tool calling")
        p.add_argument("--script", default="", help="script file for --provider script")

    p = sub.add_parser("init", help="create the ledger in this project")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check git, provider, key and model")
    p.add_argument("--ping", action="store_true", help="make one tiny real request")
    model_flags(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run the agent on a task")
    p.add_argument("task")
    p.add_argument("--verify", help="command that must exit 0 for the run to be VERIFIED")
    p.add_argument("--max-steps", dest="max_steps", type=int)
    p.add_argument("--budget-tokens", dest="budget_tokens", type=int)
    p.add_argument("--yes", "-y", action="store_true", help="allow shell commands without asking")
    p.add_argument("--label", default="", help="short label for the run")
    p.add_argument("--fork", default="", help="start from a step of an earlier run")
    p.add_argument("--run", default="", help="run that --fork refers to")
    p.add_argument("--cassette", choices=["live", "reuse", "replay"],
                   help="live (default: always ask and record), reuse (answer from "
                        "the cassette on an exact prompt match), replay (cassette only)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true")
    model_flags(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("runs", help="list recorded runs")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("log", help="show a run step by step")
    p.add_argument("--run", default="", help="run id (default: the last one)")
    p.add_argument("--graph", action="store_true", help="raw git graph of every run")
    p.add_argument("--oneline", action="store_true")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("show", help="everything about one step")
    p.add_argument("step")
    p.add_argument("--run", default="")
    p.add_argument("--io", action="store_true", help="include the raw model request and response")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("blame", help="which step wrote each line of a file")
    p.add_argument("path")
    p.add_argument("--run", default="")
    p.add_argument("-L", "--lines", dest="lines", default="", help="line range, e.g. 10,40")
    p.set_defaults(func=cmd_blame)

    p = sub.add_parser("why", help="the reasoning behind one line")
    p.add_argument("path")
    p.add_argument("line", nargs="?", type=int)
    p.add_argument("--run", default="")
    p.add_argument("--diff", action="store_true", help="also show the introducing diff")
    p.set_defaults(func=cmd_why)

    p = sub.add_parser("diff", help="compare the workspaces two runs produced")
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("fork", help="rewind to a step and try a different approach")
    p.add_argument("step")
    p.add_argument("task", nargs="?", default="")
    p.add_argument("--run", default="")
    p.add_argument("--label", default="fork")
    p.add_argument("--verify")
    p.add_argument("--max-steps", dest="max_steps", type=int)
    p.add_argument("--budget-tokens", dest="budget_tokens", type=int)
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--no-run", action="store_true", help="just rewind the workspace")
    p.add_argument("--cassette", choices=["live", "reuse", "replay"])
    p.add_argument("--verbose", "-v", action="store_true")
    model_flags(p)
    p.set_defaults(func=cmd_fork)

    p = sub.add_parser("goto", help="restore the workspace to a step")
    p.add_argument("step")
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_goto)

    p = sub.add_parser("undo", help="rewind the workspace by N steps")
    p.add_argument("steps", nargs="?", default=1, type=int)
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("replay", help="re-drive a recorded run and report divergence")
    p.add_argument("run_spec", nargs="?", default="@last")
    p.add_argument("--live", action="store_true", help="re-ask the model instead of the cassette")
    p.add_argument("--into", default="", help="directory to replay into")
    p.add_argument("--verbose", "-v", action="store_true")
    model_flags(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("report", help="write a self-contained HTML audit trail")
    p.add_argument("run_spec", nargs="?", default="@last")
    p.add_argument("-o", "--output", default="")
    p.add_argument("--no-io", action="store_true", help="leave raw model I/O out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="one static HTML page for every run in the ledger")
    p.add_argument("-o", "--output", default="")
    p.add_argument("--open", action="store_true", help="open it in your browser")
    p.add_argument("--io", action="store_true", help="embed raw model responses too")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("stats", help="where the tokens and the lines went")
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("demo", help="offline end-to-end demo, no API key needed")
    p.add_argument("path", nargs="?", default="agit-demo")
    p.add_argument("--force", action="store_true")
    p.add_argument("--fast", action="store_true", help="no pacing pauses")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except LedgerError as exc:
        Console().fail(str(exc))
        return 2
    except ScriptError as exc:
        Console().fail(str(exc))
        return 2
    except ModelError as exc:
        Console().fail(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
