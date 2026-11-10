"""`agit report`: a self-contained HTML audit trail for a run.

No CDN, no JavaScript dependency, no network. One file you can attach to a pull
request, a bug report or a compliance ticket.
"""
from __future__ import annotations

import html
import json
import os
from typing import List, Optional

from .ledger import Ledger, RunInfo, StepInfo
from .ui import human_ms, human_tokens

CSS = """
:root{--bg:#0c0f14;--panel:#141922;--line:#222a37;--fg:#e6e9ef;--mut:#8b96a8;
--saffron:#ff9933;--green:#3ddc97;--red:#ff6b6b;--blue:#58a6ff;--violet:#b48ead}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--blue)}
.wrap{max-width:1040px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.5px}
h1 span{color:var(--saffron)}
.sub{color:var(--mut);margin-bottom:28px}
.task{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--saffron);
padding:14px 18px;border-radius:6px;margin-bottom:24px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:32px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.stat b{display:block;font-size:20px;font-weight:600}
.stat small{color:var(--mut);text-transform:uppercase;letter-spacing:.08em;font-size:10px}
.step{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin:0 0 14px;
padding:14px 18px;position:relative}
.step:before{content:"";position:absolute;left:-1px;top:-1px;bottom:-1px;width:3px;
border-radius:6px 0 0 6px;background:var(--line)}
.step.ok:before{background:var(--green)}
.step.bad:before{background:var(--red)}
.step.fin:before{background:var(--saffron)}
.head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.n{color:var(--mut)}
.tool{color:var(--violet);font-weight:600}
.target{color:var(--fg);opacity:.9;word-break:break-all}
.meta{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap}
.why{margin:8px 0 0;color:var(--fg);opacity:.92;white-space:pre-wrap}
details{margin-top:10px}
summary{cursor:pointer;color:var(--mut);font-size:12px;outline:none}
pre{background:#0a0d12;border:1px solid var(--line);border-radius:5px;padding:10px 12px;
overflow:auto;max-height:420px;font-size:12.5px;margin:8px 0 0}
.add{color:var(--green)}.del{color:var(--red)}.hunk{color:var(--violet)}
.pill{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.pass{color:var(--green);border-color:var(--green)}
.pill.fail{color:var(--red);border-color:var(--red)}
.pill.cache{color:var(--blue);border-color:var(--blue)}
footer{color:var(--mut);margin-top:40px;border-top:1px solid var(--line);padding-top:16px;font-size:12px}
"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _diff_html(diff: str, limit: int = 400) -> str:
    out: List[str] = []
    for i, line in enumerate(diff.splitlines()):
        if i >= limit:
            out.append('<span class="hunk">... diff trimmed</span>')
            break
        safe = _esc(line)
        if line.startswith("+") and not line.startswith("+++"):
            out.append('<span class="add">%s</span>' % safe)
        elif line.startswith("-") and not line.startswith("---"):
            out.append('<span class="del">%s</span>' % safe)
        elif line.startswith("@@") or line.startswith("diff --git"):
            out.append('<span class="hunk">%s</span>' % safe)
        else:
            out.append(safe)
    return "\n".join(out)


def render(ledger: Ledger, run: RunInfo, out_path: str, include_io: bool = True) -> str:
    steps = ledger.steps(run.branch)
    real = [s for s in steps if s.kind in ("tool", "finish")]
    tokens = sum(s.tokens for s in steps)
    duration = sum(s.duration_ms for s in steps)
    writes = sum(1 for s in real if s.tool in ("write_file", "edit_file"))
    added = sum(s.added for s in real)
    removed = sum(s.removed for s in real)

    verdict = "VERIFIED" if run.verified else (
        "NOT VERIFIED" if run.verified is False else run.status.upper())
    verdict_class = "pass" if run.verified else ("fail" if run.verified is False else "")

    parts: List[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append("<title>agit run %s</title><style>%s</style></head><body><div class='wrap'>"
                 % (_esc(run.run), CSS))
    parts.append("<h1><span>agit</span> run %s</h1>" % _esc(run.run))
    parts.append("<div class='sub'>%s &middot; %s &middot; started %s &middot; "
                 "<span class='pill %s'>%s</span></div>"
                 % (_esc(run.provider), _esc(run.model), _esc(run.started_at),
                    verdict_class, _esc(verdict)))
    parts.append("<div class='task'>%s</div>" % _esc(run.task))

    stats = [
        ("steps", str(len(real))),
        ("file writes", str(writes)),
        ("lines", "+%d / -%d" % (added, removed)),
        ("tokens", human_tokens(tokens)),
        ("model time", human_ms(duration)),
        ("forked from", run.forked_from or "-"),
    ]
    parts.append("<div class='grid'>")
    for label, value in stats:
        parts.append("<div class='stat'><small>%s</small><b>%s</b></div>" % (_esc(label), _esc(value)))
    parts.append("</div>")

    for step in steps:
        if step.kind == "baseline":
            continue
        klass = "fin" if step.kind == "finish" else ("ok" if step.ok else "bad")
        parts.append("<div class='step %s'>" % klass)
        parts.append("<div class='head'><span class='n'>step %02d</span>"
                     "<span class='tool'>%s</span><span class='target'>%s</span>"
                     % (step.step, _esc(step.tool or step.kind), _esc(step.target)))
        bits = []
        if step.added or step.removed:
            bits.append("+%d/-%d" % (step.added, step.removed))
        if step.tokens:
            bits.append("%s tok" % human_tokens(step.tokens))
        if step.duration_ms:
            bits.append(human_ms(step.duration_ms))
        bits.append(step.sha[:8])
        parts.append("<span class='meta'>%s</span>" % _esc(" · ".join(bits)))
        if step.verify:
            parts.append("<span class='pill %s'>verify %s</span>"
                         % ("pass" if step.verify == "pass" else "fail", _esc(step.verify)))
        parts.append("</div>")
        if step.rationale:
            parts.append("<p class='why'>%s</p>" % _esc(step.rationale))
        diff = ledger.step_diff(step) if step.kind != "run-end" else ""
        if diff.strip():
            parts.append("<details><summary>workspace diff</summary><pre>%s</pre></details>"
                         % _diff_html(diff))
        result = ledger.step_artifact(step, "result.txt")
        if result and result.strip():
            parts.append("<details><summary>tool result shown to the model</summary><pre>%s</pre></details>"
                         % _esc(result[:20000]))
        verify_text = ledger.step_artifact(step, "verify.txt")
        if verify_text:
            parts.append("<details><summary>verify output</summary><pre>%s</pre></details>"
                         % _esc(verify_text[:20000]))
        if include_io:
            response = ledger.step_artifact(step, "response.json")
            if response:
                parts.append("<details><summary>raw model response</summary><pre>%s</pre></details>"
                             % _esc(_pretty(response)[:30000]))
        parts.append("</div>")

    parts.append("<details><summary>ledger graph (git log --graph --all)</summary><pre>%s</pre></details>"
                 % _esc(ledger.graph()[:20000]))
    parts.append(
        "<footer>Generated by agit &mdash; every line above came from the git ledger at "
        "<code>.agit/ledger</code>. Reproduce with "
        "<code>agit show &lt;step&gt; --run %s</code> or <code>agit replay %s</code>.</footer>"
        % (_esc(run.run), _esc(run.run)))
    parts.append("</div></body></html>")

    html_text = "\n".join(parts)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return out_path


def _pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2)[:30000]
    except ValueError:
        return raw
