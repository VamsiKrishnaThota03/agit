"""`agit dashboard`: every run in the ledger, as one static HTML file.

No server, no CDN, no build step. The ledger is read once, serialised into the
page as JSON, and rendered by a small amount of inline vanilla JavaScript. You
double-click the file; nothing listens on a port.

Everything the model produced -- rationales, diffs, tool output, file contents --
is untrusted text, so it is only ever inserted into the DOM with `textContent`.
The JSON block itself has `<` escaped, so a model that writes `</script>` into a
rationale cannot break out of it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import blame as blame_mod
from .ledger import Ledger, LedgerError, RunInfo

MAX_DIFF_LINES = 400
MAX_RESULT_CHARS = 8000
MAX_BLAME_FILES = 40
MAX_BLAME_LINES = 1500

CSS = """
:root{--bg:#0c0f14;--panel:#141922;--panel2:#10151d;--line:#222a37;--fg:#e6e9ef;
--mut:#8b96a8;--saffron:#ff9933;--green:#3ddc97;--red:#ff6b6b;--blue:#58a6ff;
--violet:#b48ead;--yellow:#ffd166}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
font:13.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:baseline;gap:14px;padding:14px 20px;
border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:19px;margin:0;letter-spacing:-.4px}
header h1 b{color:var(--saffron)}
header .mut{color:var(--mut);font-size:12px}
header .right{margin-left:auto;color:var(--mut);font-size:12px}
main{display:flex;flex:1;min-height:0}
aside{width:290px;flex:none;border-right:1px solid var(--line);overflow:auto;
background:var(--panel2);padding:14px 0 40px}
section.body{flex:1;overflow:auto;padding:20px 24px 80px}
.sg{padding:0 16px;margin-bottom:22px}
.sg h2{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);
margin:0 0 8px;font-weight:600}
.runrow{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:5px;
cursor:pointer;border:1px solid transparent}
.runrow:hover{background:var(--panel)}
.runrow.on{background:var(--panel);border-color:var(--line)}
.runrow .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.runrow .sub{color:var(--mut);font-size:11px}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--mut)}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}
.dot.warn{background:var(--yellow)}
.kid{margin-left:14px;border-left:1px solid var(--line);padding-left:8px}
.filerow{margin-bottom:9px;cursor:pointer}
.filerow .top{display:flex;justify-content:space-between;gap:8px}
.filerow .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.bar{height:5px;background:var(--line);border-radius:3px;margin-top:3px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--saffron)}
.tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.tab{padding:7px 14px;cursor:pointer;color:var(--mut);border-bottom:2px solid transparent}
.tab.on{color:var(--fg);border-bottom-color:var(--saffron)}
h3.task{font-size:16px;font-weight:600;margin:0 0 4px}
.meta{color:var(--mut);font-size:12px;margin-bottom:16px}
.task-box{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--saffron);
padding:12px 16px;border-radius:6px;margin-bottom:18px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:24px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px}
.stat b{display:block;font-size:18px;font-weight:600}
.stat small{color:var(--mut);text-transform:uppercase;letter-spacing:.08em;font-size:10px}
.step{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin-bottom:12px;
padding:12px 16px;border-left:3px solid var(--line)}
.step.ok{border-left-color:var(--green)}
.step.bad{border-left-color:var(--red)}
.step.fin{border-left-color:var(--saffron)}
.step .hd{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.step .n{color:var(--mut)}
.step .tool{color:var(--violet);font-weight:600}
.step .tgt{opacity:.92;word-break:break-all}
.step .num{margin-left:auto;color:var(--mut);font-size:11.5px;white-space:nowrap}
.why{margin:7px 0 0;white-space:pre-wrap}
details{margin-top:8px}
summary{cursor:pointer;color:var(--mut);font-size:11.5px}
pre{background:#080b10;border:1px solid var(--line);border-radius:5px;padding:9px 11px;
overflow:auto;max-height:400px;font-size:12.5px;margin:7px 0 0;white-space:pre}
.add{color:var(--green)}.del{color:var(--red)}.hunk{color:var(--violet)}
.pill{font-size:10.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.pass{color:var(--green);border-color:var(--green)}
.pill.fail{color:var(--red);border-color:var(--red)}
.pill.on{color:var(--saffron);border-color:var(--saffron)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.chip{padding:3px 10px;border:1px solid var(--line);border-radius:999px;cursor:pointer;
color:var(--mut);font-size:12px}
.chip.on{color:var(--fg);border-color:var(--saffron)}
table.blame{width:100%;border-collapse:collapse}
table.blame td{padding:0 8px;vertical-align:top;white-space:pre;font-size:12.5px}
table.blame td.who{width:76px;color:var(--mut);text-align:right;user-select:none}
table.blame td.ln{width:46px;color:var(--mut);text-align:right;user-select:none}
table.blame tr.agent td.who{color:var(--saffron)}
table.blame tr.agent{background:rgba(255,153,51,.07)}
table.blame tr:hover{background:rgba(88,166,255,.12)}
table.blame tr.sel{background:rgba(88,166,255,.2)}
table.blame tr{cursor:pointer}
.whypanel{position:sticky;top:0;z-index:3;background:var(--panel);box-shadow:0 6px 18px rgba(0,0,0,.45);border:1px solid var(--line);
border-radius:6px;padding:12px 16px;margin:0 0 14px;border-left:3px solid var(--blue)}
.whypanel .lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.spend{display:flex;flex-direction:column;gap:7px}
h2.sectitle{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:600;margin:26px 0 10px}
.sbar{display:flex;align-items:center;gap:10px}
.sbar .lab{width:190px;color:var(--mut);font-size:12px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;flex:none}
.sbar .track{flex:1;height:14px;background:var(--line);border-radius:3px;overflow:hidden}
.sbar .track i{display:block;height:100%;background:var(--blue)}
.sbar .track i.write{background:var(--green)}
.sbar .track i.run{background:var(--yellow)}
.sbar .val{width:74px;text-align:right;color:var(--mut);font-size:12px;flex:none}
.empty{color:var(--mut);padding:40px 0}
footer{color:var(--mut);font-size:11.5px;padding:10px 20px;border-top:1px solid var(--line)}
a{color:var(--blue)}
"""

JS = r"""
const DATA = JSON.parse(document.getElementById('agit-data').textContent);

/* A replay is a copy of another run, so it is a poor thing to land on. */
function defaultRun() {
  const real = DATA.runs.find(r => !(r.label || '').startsWith('replay-of-'));
  return (real || DATA.runs[0] || {}).id;
}
const state = {run: defaultRun(), tab: 'timeline', file: null, line: null};

/* Deep links: #run=<id>&tab=blame&file=csvkit.py&line=24 so you can send a
   colleague the exact line and its reasoning. */
function readHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) return;
  raw.split('&').forEach(pair => {
    const [key, value] = pair.split('=').map(decodeURIComponent);
    if (key === 'run' && DATA.runs.some(r => r.id === value)) state.run = value;
    if (key === 'tab' && ['timeline', 'blame', 'spend'].includes(value)) state.tab = value;
    if (key === 'file') state.file = value;
    if (key === 'line') state.line = parseInt(value, 10) || null;
  });
}
function writeHash() {
  const bits = ['run=' + encodeURIComponent(state.run), 'tab=' + state.tab];
  if (state.tab === 'blame' && state.file) bits.push('file=' + encodeURIComponent(state.file));
  if (state.tab === 'blame' && state.line) bits.push('line=' + state.line);
  history.replaceState(null, '', '#' + bits.join('&'));
}

const $ = (sel) => document.querySelector(sel);
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function run() { return DATA.runs.find(r => r.id === state.run) || DATA.runs[0]; }
function fmtTok(n) {
  if (!n) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
function fmtMs(ms) {
  if (!ms) return '0ms';
  if (ms < 1000) return ms + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.floor(ms / 60000) + 'm' + String(Math.floor((ms % 60000) / 1000)).padStart(2, '0') + 's';
}
function outcomeClass(r) {
  if (r.verified === true) return 'ok';
  if (r.verified === false) return 'bad';
  return r.status === 'completed' ? 'warn' : 'bad';
}
function outcomeText(r) {
  if (r.verified === true) return 'verified';
  if (r.verified === false) return 'not verified';
  return r.status;
}

/* ---------- sidebar ---------- */
function renderSidebar() {
  const aside = $('#side');
  aside.textContent = '';

  const runsGroup = el('div', 'sg');
  runsGroup.appendChild(el('h2', null, 'runs (' + DATA.runs.length + ')'));
  const byParent = {};
  DATA.runs.forEach(r => {
    const key = r.parent_run || '';
    (byParent[key] = byParent[key] || []).push(r);
  });
  const emit = (parentId, depth) => {
    (byParent[parentId] || []).forEach(r => {
      const row = el('div', 'runrow' + (r.id === state.run ? ' on' : '') + (depth ? ' kid' : ''));
      row.appendChild(el('span', 'dot ' + outcomeClass(r)));
      const box = el('div', 'nm');
      const name = r.label || r.id;
      box.appendChild(el('div', null, name.length > 26 ? name.slice(0, 25) + '\u2026' : name));
      box.appendChild(el('div', 'sub',
        r.steps + ' steps · ' + fmtTok(r.tokens) + ' tok' +
        (r.parent_step ? ' · forked @' + r.parent_step : '')));
      row.appendChild(box);
      row.onclick = () => { state.run = r.id; state.file = null; state.line = null; render(); };
      runsGroup.appendChild(row);
      emit(r.id, depth + 1);
    });
  };
  emit('', 0);
  Object.keys(byParent).forEach(key => {       // parents outside this ledger
    if (key && !DATA.runs.some(r => r.id === key)) emit(key, 0);
  });
  aside.appendChild(runsGroup);

  const r = run();
  if (r && r.files.length) {
    const filesGroup = el('div', 'sg');
    filesGroup.appendChild(el('h2', null, 'files the agent touched'));
    r.files.forEach(f => {
      const row = el('div', 'filerow');
      const top = el('div', 'top');
      top.appendChild(el('span', 'nm', f.path));
      top.appendChild(el('span', 'sub', f.share + '%'));
      row.appendChild(top);
      const bar = el('div', 'bar');
      const fill = el('i');
      fill.style.width = f.share + '%';
      bar.appendChild(fill);
      row.appendChild(bar);
      row.onclick = () => { state.tab = 'blame'; state.file = f.path; state.line = null; render(); };
      filesGroup.appendChild(row);
    });
    aside.appendChild(filesGroup);
  }

  const legend = el('div', 'sg');
  legend.appendChild(el('h2', null, 'reading this'));
  [['●', 'green = step succeeded'], ['●', 'red = step failed (often on purpose)'],
   ['█', 'orange = line written by the agent'], ['↳', 'indented run = forked from above']]
    .forEach(([g, t]) => {
      const row = el('div', 'sub');
      row.textContent = g + '  ' + t;
      row.style.color = 'var(--mut)';
      row.style.fontSize = '11.5px';
      legend.appendChild(row);
    });
  aside.appendChild(legend);
}

/* ---------- main ---------- */
function renderMain() {
  const body = $('#body');
  body.textContent = '';
  const r = run();
  if (!r) { body.appendChild(el('div', 'empty', 'This ledger has no runs yet.')); return; }

  body.appendChild(el('h3', 'task', r.task || '(no task recorded)'));
  const meta = el('div', 'meta');
  meta.textContent = [r.provider, r.model, r.started_at, r.id].filter(Boolean).join(' · ') + ' · ';
  meta.appendChild(el('span', 'pill ' + (r.verified === true ? 'pass' : (r.verified === false ? 'fail' : '')),
                      outcomeText(r)));
  body.appendChild(meta);

  const tabs = el('div', 'tabs');
  [['timeline', 'timeline'], ['blame', 'blame'], ['spend', 'spend']].forEach(([key, label]) => {
    const tab = el('div', 'tab' + (state.tab === key ? ' on' : ''), label);
    tab.onclick = () => { state.tab = key; render(); };
    tabs.appendChild(tab);
  });
  body.appendChild(tabs);

  if (state.tab === 'timeline') renderTimeline(body, r);
  else if (state.tab === 'blame') renderBlame(body, r);
  else renderSpend(body, r);
}

function statGrid(parent, pairs) {
  const grid = el('div', 'grid');
  pairs.forEach(([label, value]) => {
    const card = el('div', 'stat');
    card.appendChild(el('small', null, label));
    card.appendChild(el('b', null, value));
    grid.appendChild(card);
  });
  parent.appendChild(grid);
}

function preDiff(text) {
  const pre = el('pre');
  text.split('\n').slice(0, 400).forEach(line => {
    let cls = null;
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del';
    else if (line.startsWith('@@') || line.startsWith('diff --git')) cls = 'hunk';
    pre.appendChild(el('span', cls, line + '\n'));
  });
  return pre;
}
function fold(parent, label, text, asDiff) {
  if (!text || !text.trim()) return;
  const box = el('details');
  box.appendChild(el('summary', null, label));
  box.appendChild(asDiff ? preDiff(text) : el('pre', null, text));
  parent.appendChild(box);
}

function renderTimeline(body, r) {
  if (r.task) body.appendChild(el('div', 'task-box', r.task));
  statGrid(body, [
    ['steps', String(r.steps)],
    ['file writes', String(r.writes)],
    ['lines', '+' + r.added + ' / -' + r.removed],
    ['tokens', fmtTok(r.tokens)],
    ['model time', fmtMs(r.duration_ms)],
    r.cost ? ['cost', r.cost]
           : ['forked from', r.parent_run ? (r.parent_label || r.parent_run) + '@' + r.parent_step : '—'],
  ]);
  r.steps_detail.forEach(s => {
    const card = el('div', 'step ' + (s.kind === 'finish' ? 'fin' : (s.ok ? 'ok' : 'bad')));
    const head = el('div', 'hd');
    head.appendChild(el('span', 'n', 'step ' + String(s.n).padStart(2, '0')));
    head.appendChild(el('span', 'tool', s.tool || s.kind));
    head.appendChild(el('span', 'tgt', s.target || ''));
    const bits = [];
    if (s.added || s.removed) bits.push('+' + s.added + '/-' + s.removed);
    if (s.tokens) bits.push(fmtTok(s.tokens) + ' tok');
    if (s.duration_ms) bits.push(fmtMs(s.duration_ms));
    bits.push(s.sha.slice(0, 8));
    head.appendChild(el('span', 'num', bits.join(' · ')));
    if (s.verify) head.appendChild(el('span', 'pill ' + (s.verify === 'pass' ? 'pass' : 'fail'),
                                      'verify ' + s.verify));
    card.appendChild(head);
    if (s.rationale) card.appendChild(el('p', 'why', s.rationale));
    fold(card, 'workspace diff', s.diff, true);
    fold(card, 'tool result shown to the model', s.result, false);
    fold(card, 'verify output', s.verify_output, false);
    body.appendChild(card);
  });
}

function renderBlame(body, r) {
  const paths = r.blame.map(b => b.path);
  if (!paths.length) {
    body.appendChild(el('div', 'empty', 'This run did not write to any file.'));
    return;
  }
  if (!state.file || !paths.includes(state.file)) state.file = paths[0];
  const chips = el('div', 'chips');
  paths.forEach(path => {
    const chip = el('div', 'chip' + (path === state.file ? ' on' : ''), path);
    chip.onclick = () => { state.file = path; state.line = null; render(); };
    chips.appendChild(chip);
  });
  body.appendChild(chips);

  const file = r.blame.find(b => b.path === state.file);
  const agentLines = file.lines.filter(l => l[1] !== null).length;
  statGrid(body, [
    ['file', file.path],
    ['lines', String(file.lines.length)],
    ['written by the agent', agentLines + ' (' +
      Math.round(100 * agentLines / Math.max(1, file.lines.length)) + '%)'],
    ['written by a human', String(file.lines.length - agentLines)],
  ]);

  const picked = file.lines.find(l => l[0] === state.line);
  const panel = el('div', 'whypanel');
  if (!picked) {
    panel.appendChild(el('div', 'lbl', 'click any line'));
    panel.appendChild(el('div', null, 'and the reason it was written appears here'));
  } else if (picked[1] === null) {
    panel.appendChild(el('div', 'lbl', state.file + ':' + picked[0]));
    panel.appendChild(el('div', null, picked[2].trim() || '(blank line)'));
    panel.appendChild(el('div', 'sub', 'written by a human before this run started'));
  } else {
    const s = r.steps_detail.find(x => x.n === picked[1]) || {};
    panel.appendChild(el('div', 'lbl', state.file + ':' + picked[0] + '  ·  agit step ' + picked[1] +
                                       '  ·  ' + (s.tool || '') + '  ·  ' + (s.model || '')));
    panel.appendChild(el('div', null, picked[2].trim() || '(blank line)'));
    panel.appendChild(el('div', 'lbl', 'because'));
    panel.appendChild(el('div', 'why', s.rationale || '(the model gave no rationale for this step)'));
  }
  body.appendChild(panel);

  const table = el('table', 'blame');
  let selectedRow = null;
  file.lines.forEach(([lineno, step, text]) => {
    const row = el('tr', (step !== null ? 'agent' : '') + (state.line === lineno ? ' sel' : ''));
    row.appendChild(el('td', 'who', step !== null ? 'step ' + String(step).padStart(2, '0') : 'human'));
    row.appendChild(el('td', 'ln', String(lineno)));
    row.appendChild(el('td', 'src', text));
    row.onclick = () => { state.line = lineno; render(); };
    if (state.line === lineno) selectedRow = row;
    table.appendChild(row);
  });
  body.appendChild(table);
  if (selectedRow && !window.__agitScrolled) {
    window.__agitScrolled = true;        /* only for a deep link, not every click */
    selectedRow.scrollIntoView({block: 'center'});
  }
}

function renderSpend(body, r) {
  const steps = r.steps_detail;
  const maxTok = Math.max(1, ...steps.map(s => s.tokens));
  statGrid(body, [
    ['tokens in', fmtTok(r.tokens_in)],
    ['tokens out', fmtTok(r.tokens_out)],
    ['total', fmtTok(r.tokens)],
    ['model time', fmtMs(r.duration_ms)],
    ['steps', String(r.steps)],
    r.cost ? ['cost', r.cost] : ['cached steps', String(steps.filter(s => s.cached).length)],
  ]);
  body.appendChild(el('h2', 'sectitle', 'tokens per step'));
  const wrap = el('div', 'spend');
  steps.forEach(s => {
    const row = el('div', 'sbar');
    row.appendChild(el('div', 'lab', 'step ' + String(s.n).padStart(2, '0') + '  ' +
                                     (s.tool || s.kind) + (s.target ? ' ' + s.target : '')));
    const track = el('div', 'track');
    const fill = el('i', s.tool === 'write_file' || s.tool === 'edit_file' ? 'write'
                        : (s.tool === 'run_command' ? 'run' : null));
    fill.style.width = Math.max(2, Math.round(100 * s.tokens / maxTok)) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'val', s.cost ? s.cost : fmtTok(s.tokens) + ' tok'));
    wrap.appendChild(row);
  });
  body.appendChild(wrap);

  const mix = {};
  steps.forEach(s => { mix[s.tool || s.kind] = (mix[s.tool || s.kind] || 0) + 1; });
  const entries = Object.entries(mix).sort((a, b) => b[1] - a[1]);
  const maxCalls = Math.max(1, ...entries.map(e => e[1]));
  body.appendChild(el('h2', 'sectitle', 'tool mix'));
  const mixWrap = el('div', 'spend');
  entries.forEach(([tool, calls]) => {
    const row = el('div', 'sbar');
    row.appendChild(el('div', 'lab', tool));
    const track = el('div', 'track');
    const fill = el('i');
    fill.style.width = Math.round(100 * calls / maxCalls) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'val', calls + (calls === 1 ? ' call' : ' calls')));
    mixWrap.appendChild(row);
  });
  body.appendChild(mixWrap);
}

function render() { writeHash(); renderSidebar(); renderMain(); }
readHash();
window.addEventListener('hashchange', () => { readHash(); renderSidebar(); renderMain(); });
document.addEventListener('keydown', (ev) => {
  const order = ['timeline', 'blame', 'spend'];
  if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
    const at = order.indexOf(state.tab);
    state.tab = order[(at + (ev.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length];
    render();
  }
});
render();
"""


def _steps_of(ledger: Ledger, run: RunInfo, include_io: bool,
              rate: Optional[Any] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for step in ledger.steps(run.branch):
        if step.kind not in ("tool", "finish"):
            continue
        meta = ledger.step_meta(step)
        diff = ledger.step_diff(step)
        if diff.count("\n") > MAX_DIFF_LINES:
            diff = "\n".join(diff.split("\n")[:MAX_DIFF_LINES]) + "\n... diff trimmed"
        result = ledger.step_artifact(step, "result.txt") or ""
        verify_output = ledger.step_artifact(step, "verify.txt") or ""
        row = {
            "n": step.step,
            "kind": step.kind,
            "tool": step.tool,
            "target": step.target,
            "ok": step.ok,
            "rationale": step.rationale,
            "model": step.model,
            "tokens": step.tokens,
            "tokens_in": step.tokens_in,
            "tokens_out": step.tokens_out,
            "duration_ms": step.duration_ms,
            "added": step.added,
            "removed": step.removed,
            "verify": step.verify,
            "verify_output": verify_output[:MAX_RESULT_CHARS],
            "sha": step.sha,
            "diff": diff,
            "result": result[:MAX_RESULT_CHARS],
            "cached": bool(meta.get("cached")),
            "cost": (rate.format(step.tokens_in, step.tokens_out) if rate else ""),
        }
        if include_io:
            row["response"] = (ledger.step_artifact(step, "response.json") or "")[:MAX_RESULT_CHARS]
        out.append(row)
    return out


def collect(ledger: Ledger, include_io: bool = False,
            rate: Optional[Any] = None) -> Dict[str, Any]:
    """Read the whole ledger into a JSON-serialisable shape.

    `rate` is an optional pricing.Rate; when absent, cost is simply absent
    rather than guessed.
    """
    runs: List[Dict[str, Any]] = []
    by_id = {}
    for info in ledger.runs():
        by_id[info.run] = info
    for info in ledger.runs():
        steps = _steps_of(ledger, info, include_io, rate)
        parent_run, parent_step = "", 0
        if info.forked_from and "@" in info.forked_from:
            parent_run, _, raw_step = info.forked_from.partition("@")
            try:
                parent_step = int(raw_step)
            except ValueError:
                parent_step = 0
        touched = sorted({s["target"] for s in steps
                          if s["tool"] in ("write_file", "edit_file") and s["target"]})
        files: List[Dict[str, Any]] = []
        blame_rows: List[Dict[str, Any]] = []
        for path in touched[:MAX_BLAME_FILES]:
            try:
                lines = blame_mod.blame_file(ledger, path, info.branch)
            except LedgerError:
                continue
            agent_lines, total = blame_mod.summarise_authorship(lines)
            files.append({
                "path": path,
                "agent": agent_lines,
                "total": total,
                "share": int(round(100.0 * agent_lines / total)) if total else 0,
            })
            blame_rows.append({
                "path": path,
                "lines": [
                    [b.lineno, (b.step.step if b.by_agent and b.step else None), b.text]
                    for b in lines[:MAX_BLAME_LINES]
                ],
            })
        parent = by_id.get(parent_run)
        runs.append({
            "id": info.run,
            "label": info.label or info.run,
            "branch": info.branch,
            "task": info.task,
            "status": info.status,
            "verified": info.verified,
            "provider": info.provider,
            "model": info.model,
            "started_at": info.started_at,
            "steps": len(steps),
            "writes": sum(1 for s in steps if s["tool"] in ("write_file", "edit_file")),
            "added": sum(s["added"] for s in steps),
            "removed": sum(s["removed"] for s in steps),
            "tokens": sum(s["tokens"] for s in steps),
            "tokens_in": sum(s["tokens_in"] for s in steps),
            "tokens_out": sum(s["tokens_out"] for s in steps),
            "cost": (rate.format(sum(s["tokens_in"] for s in steps),
                                 sum(s["tokens_out"] for s in steps)) if rate else ""),
            "duration_ms": sum(s["duration_ms"] for s in steps),
            "parent_run": parent_run,
            "parent_label": (parent.label or parent.run) if parent else "",
            "parent_step": parent_step,
            "steps_detail": steps,
            "files": files,
            "blame": blame_rows,
        })
    return {
        "project": os.path.basename(ledger.root) or ledger.root,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runs": runs,
        "totals": {
            "runs": len(runs),
            "steps": sum(r["steps"] for r in runs),
            "tokens": sum(r["tokens"] for r in runs),
        },
        "graph": ledger.graph()[:20000],
    }


def render(data: Dict[str, Any]) -> str:
    # Escaping `<` keeps a model-written "</script>" from closing the block.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    totals = data["totals"]
    head = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>agit dashboard - %s</title><style>%s</style></head><body>"
        % (_esc(data["project"]), CSS)
    )
    header = (
        "<header><h1><b>agit</b> dashboard</h1>"
        "<span class='mut'>%s</span>"
        "<span class='right'>%d runs · %d steps · %s tokens · generated %s</span></header>"
        % (_esc(data["project"]), totals["runs"], totals["steps"],
           _human(totals["tokens"]), _esc(data["generated_at"]))
    )
    shell = ("<main><aside id='side'></aside><section class='body' id='body'></section></main>"
             "<footer>Read straight from the git ledger at <code>.agit/ledger</code>. "
             "Static file: no server, no network, no JavaScript dependencies. "
             "&larr; &rarr; switch tabs.</footer>")
    script = ("<script type='application/json' id='agit-data'>%s</script><script>%s</script>"
              % (payload, JS))
    return head + header + shell + script + "</body></html>"


def build(ledger: Ledger, out_path: str, include_io: bool = False,
          rate: Optional[Any] = None) -> str:
    html_text = render(collect(ledger, include_io=include_io, rate=rate))
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return out_path


def _esc(text: str) -> str:
    import html as html_mod
    return html_mod.escape(text or "", quote=True)


def _human(n: int) -> str:
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)
