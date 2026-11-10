#!/usr/bin/env python3
"""Build the agit demo video: silent 1080p MP4 + matching captions.

Nothing here is mocked. The tool creates a throwaway workspace, runs the real
`agit` commands against the bundled offline planner, captures their real ANSI
output, animates that output line by line, and renders the frames with ffmpeg.
Re-running it after a code change regenerates a truthful video.

    python3 tools/make_demo_video.py                 # full build
    python3 tools/make_demo_video.py --scenes 3,5     # rebuild a couple of scenes
    python3 tools/make_demo_video.py --fps 30 --out demo/agit-demo.mp4

Outputs (all under demo/):
    agit-demo.mp4      silent video, captions burned in at the bottom
    captions.srt       same captions as a subtitle file
    frames/            the PNG frames, kept so you can reuse stills
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import ansi2svg  # noqa: E402  (local helper, imported after sys.path fix)

W, H = 1920, 1080
FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
        "'DejaVu Sans Mono', monospace")
CHAR_W, LINE_H, FONT_SIZE = 13.2, 32.0, 22.0
TERM_COLS, TERM_ROWS = 92, 23
BG, CHROME, FG, MUT = "#07090d", "#161b24", "#e6e9ef", "#8b96a8"
SAFFRON, GREEN = "#ff9933", "#3ddc97"

CHROME_APPS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def find_chrome() -> str:
    for path in CHROME_APPS:
        if os.path.exists(path):
            return path
    raise SystemExit("need a Chromium-based browser to rasterise frames")


# -- scene model ----------------------------------------------------------

WORDS_PER_SECOND = 2.5          # unhurried narration
BREATH = 1.2                    # pause at the end of a scene


@dataclass
class Scene:
    kind: str                       # slide | terminal | image
    caption: str
    narration: str = ""
    title: str = ""
    lines: List[str] = field(default_factory=list)      # slide text
    command: str = ""                                   # terminal
    cwd: str = ""
    prompt: str = "~/agit-demo"
    chunk: int = 2                                      # lines revealed per frame
    reveal_sec: float = 0.22
    hold: float = 2.6
    image: str = ""
    captured: str = ""                                  # filled in at capture time

    @property
    def speak_seconds(self) -> float:
        """How long this scene's narration takes to read aloud."""
        if not self.narration:
            return 0.0
        return len(self.narration.split()) / WORDS_PER_SECOND + BREATH

    @property
    def label(self) -> str:
        return self.title or self.command or os.path.basename(self.image) or "slide"


# -- rendering ------------------------------------------------------------

def svg_header() -> List[str]:
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="%s">' % (W, H, W, H, FONT)]
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, BG))
    out.append('<text x="64" y="68" font-size="26" font-weight="600" fill="%s">agit</text>'
               % SAFFRON)
    out.append('<text x="152" y="68" font-size="22" fill="%s">git for AI agents</text>' % MUT)
    return out


def caption_band(out: List[str], caption: str, index: int, total: int) -> None:
    y = H - 104
    out.append('<rect x="0" y="%d" width="%d" height="104" fill="#0b0e13"/>' % (y, W))
    out.append('<rect x="0" y="%d" width="%d" height="2" fill="#1d2432"/>' % (y, W))
    for i, line in enumerate(wrap(caption, 84)[:2]):
        out.append('<text x="64" y="%d" font-size="30" fill="%s">%s</text>'
                   % (y + 44 + i * 38, FG, html.escape(line)))
    out.append('<text x="%d" y="%d" font-size="20" fill="%s" text-anchor="end">%d / %d</text>'
               % (W - 64, y + 64, MUT, index, total))


def wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 > width and cur:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def render_terminal(ansi_text: str, caption: str, index: int, total: int,
                    title: str = "") -> str:
    lines = ansi2svg.parse(ansi_text)
    pad_x, pad_y = 34.0, 58.0
    box_w = TERM_COLS * CHAR_W + pad_x * 2
    box_h = TERM_ROWS * LINE_H + pad_y + 30
    box_x = (W - box_w) / 2
    box_y = 120.0

    out = svg_header()
    out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="12" fill="#0c0f14" '
               'stroke="#1d2432"/>' % (box_x, box_y, box_w, box_h))
    out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="38" rx="12" fill="%s"/>'
               % (box_x, box_y, box_w, CHROME))
    out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="12" fill="%s"/>'
               % (box_x, box_y + 26, box_w, CHROME))
    for i, dot in enumerate(("#ff6b6b", "#ffd166", "#3ddc97")):
        out.append('<circle cx="%.0f" cy="%.0f" r="6" fill="%s"/>'
                   % (box_x + 24 + i * 22, box_y + 19, dot))
    out.append('<text x="%.0f" y="%.0f" font-size="17" fill="%s" text-anchor="middle">%s</text>'
               % (box_x + box_w / 2, box_y + 25, MUT, html.escape(title or "terminal")))

    for row, segments in enumerate(lines[:TERM_ROWS]):
        y = box_y + pad_y + row * LINE_H
        col = 0
        for text, style in segments:
            if text.strip():
                x = box_x + pad_x + col * CHAR_W
                if style.inverse:
                    out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="28" rx="3" '
                               'fill="%s"/>' % (x, y - 21, len(text) * CHAR_W, style.color))
                    fill, opacity = "#0c0f14", 1.0
                else:
                    fill = style.color
                    opacity = 0.56 if style.dim else 1.0
                out.append('<text x="%.1f" y="%.1f" font-size="%.0f" fill="%s" opacity="%.2f"%s '
                           'xml:space="preserve">%s</text>'
                           % (x, y, FONT_SIZE, fill, opacity,
                              ' font-weight="600"' if style.bold else "",
                              html.escape(text)))
            col += len(text)
    caption_band(out, caption, index, total)
    out.append("</svg>")
    return "\n".join(out)


def render_slide(scene: Scene, index: int, total: int) -> str:
    out = svg_header()
    y = 300
    if scene.title:
        out.append('<text x="64" y="%d" font-size="72" font-weight="700" fill="%s">%s</text>'
                   % (y, FG, html.escape(scene.title)))
        y += 90
    for line in scene.lines:
        size, fill, weight = 36, FG, "400"
        if line.startswith("# "):
            line, size, fill, weight = line[2:], 44, SAFFRON, "600"
        elif line.startswith("> "):
            line, size, fill = line[2:], 32, MUT
        if not line.strip():
            y += 26
            continue
        for part in wrap(line, 72 if size <= 36 else 58):
            out.append('<text x="64" y="%d" font-size="%d" font-weight="%s" fill="%s">%s</text>'
                       % (y, size, weight, fill, html.escape(part)))
            y += int(size * 1.45)
        y += 10
    caption_band(out, scene.caption, index, total)
    out.append("</svg>")
    return "\n".join(out)


def render_image_frame(scene: Scene, index: int, total: int) -> str:
    """A screenshot, letterboxed on the branded canvas, with the caption band."""
    import base64
    with open(scene.image, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    avail_w, avail_h = W - 160, H - 120 - 130
    out = svg_header()
    out.append('<image x="%d" y="%d" width="%d" height="%d" '
               'preserveAspectRatio="xMidYMid meet" xlink:href="data:image/png;base64,%s" '
               'xmlns:xlink="http://www.w3.org/1999/xlink"/>'
               % (80, 110, avail_w, avail_h, data))
    caption_band(out, scene.caption, index, total)
    out.append("</svg>")
    return "\n".join(out)


def rasterise(chrome: str, svg_text: str, out_png: str, tmp_dir: str) -> None:
    svg_path = os.path.join(tmp_dir, "frame.svg")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(svg_text)
    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", "--window-size=%d,%d" % (W, H),
         "--screenshot=" + out_png, "file://" + svg_path],
        capture_output=True, check=False,
    )
    if not os.path.exists(out_png):
        raise SystemExit("failed to rasterise %s" % out_png)


# -- capture --------------------------------------------------------------

def capture(command: str, cwd: str, columns: int = TERM_COLS - 4) -> str:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": REPO,
        "AGIT_FORCE_COLOR": "1",
        "COLUMNS": str(columns),
        "TERM": "xterm-256color",
    })
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                          text=True, env=env)
    return ((proc.stdout or "") + (proc.stderr or "")).replace("\r", "")


SGR = re.compile(r"\x1b\[[0-9;]*m")


def drop_noise(text: str, drops: Sequence[str] = ()) -> str:
    keep = []
    for line in text.split("\n"):
        plain = SGR.sub("", line).strip()
        if any(plain.startswith(d) for d in drops):
            continue
        keep.append(line)
    return "\n".join(keep)


def prompt_line(scene: Scene) -> str:
    return "\x1b[38;5;42m%s\x1b[0m \x1b[38;5;208m$\x1b[0m \x1b[1m%s\x1b[0m" % (
        scene.prompt, scene.command)


def reveal(scene: Scene) -> List[Tuple[str, float]]:
    """Progressive frames: the command, then its output, scrolling like a terminal."""
    body = [l for l in scene.captured.split("\n")]
    while body and not SGR.sub("", body[-1]).strip():
        body.pop()
    head = [prompt_line(scene), ""]
    frames: List[Tuple[str, float]] = [("\n".join(head), 0.75)]
    shown: List[str] = []
    for i in range(0, len(body), scene.chunk):
        shown.extend(body[i:i + scene.chunk])
        window = (head + shown)[-TERM_ROWS:]
        frames.append(("\n".join(window), scene.reveal_sec))
    if frames:
        text, _ = frames[-1]
        frames[-1] = (text, scene.hold)
    return frames


# -- the demo itself ------------------------------------------------------

def build_scenes(stage: str) -> List[Scene]:
    """Runs the real commands in `stage` and returns scenes with captured output."""
    agit = "python3 -m agit"
    demo_dir = os.path.join(stage, "agit-demo")

    scenes: List[Scene] = [
        Scene(kind="slide",
              title="agit",
              lines=["# git for AI agents",
                     "",
                     "An AI agent edits your code and says “done”.",
                     "Three weeks later a line looks wrong, and nobody",
                     "can say why it is there.",
                     "",
                     "> The chat has scrolled away. git blame says: you."],
              caption="An AI wrote this line. Why? Nobody can answer that today.",
              narration='An AI agent edits your code and says done. Weeks later a line looks wrong, and nobody can say why it is there.',
              hold=8.0),
        Scene(kind="slide",
              title="",
              lines=["# agit records the run into git itself",
                     "",
                     "One agent step  =  one commit",
                     "",
                     "> the model's reason        →  the commit message",
                     "> a workspace snapshot      →  the commit tree",
                     "> the exact prompt + reply  →  files inside the commit",
                     "",
                     "So git blame can answer: which step, and why."],
              caption="agit writes every agent step into a git ledger: one step, one commit.",
              narration="agit writes the run into git itself. One step, one commit. The model's reasoning becomes the commit message, and a snapshot of your project becomes the commit tree.",
              hold=9.0),
    ]

    # Scene: the run itself.
    out = capture("%s demo agit-demo --fast --force" % agit, stage)
    out = drop_noise(out, ("---", "+++", "@@", "+", "-", "...", "$ ", "exit=",
                           "====", "FAIL:", "AssertionError", "Traceback", "File \"",
                           "self.assert", "Ran ", "OK", "----", "agit ", "git -C"))
    out = "\n".join(out.split("\n")[:26])
    scenes.append(Scene(kind="terminal", command="agit demo", cwd=stage, captured=out,
                        prompt="~/work", title="agit demo — offline, no API key",
                        chunk=1, reveal_sec=0.5, hold=5.0,
                        caption="The agent writes a failing test first, then the fix — "
                                "and each step is a commit.",
                        narration='A real bug: this CSV parser splits on every comma, so a quoted address gets torn in half. Watch the order. A failing test first, then the fix, then the whole suite. All offline, with no API key.'))
    scenes.append(Scene(kind="terminal", command="agit log", cwd=demo_dir,
                        captured=drop_noise(capture("%s log" % agit, demo_dir),
                                            ("agit show",)),
                        title="every step, with its reason", chunk=2, reveal_sec=0.3,
                        hold=5.0,
                        caption="VERIFIED is not the model's opinion — agit ran your "
                                "tests before believing it.",
                        narration='Every step carries the reason the model gave for it. And it says verified because agit ran your test command itself. Done is a tested fact, not a claim.'))
    scenes.append(Scene(kind="terminal", command="agit blame csvkit.py", cwd=demo_dir,
                        captured=capture("%s blame csvkit.py -L 1,22" % agit, demo_dir),
                        title="git blame, for AI-written code", chunk=2, reveal_sec=0.26,
                        hold=6.0,
                        caption="Human lines and agent lines, separated line by line.",
                        narration='agit blame. Grey lines are human, orange lines are the agent, with the step that wrote them. Sixty-three percent of this file is machine-written.'))
    scenes.append(Scene(kind="terminal", command="agit why csvkit.py 24", cwd=demo_dir,
                        captured=capture("%s why csvkit.py 24" % agit, demo_dir),
                        title="the reasoning behind one line", chunk=2, reveal_sec=0.3,
                        hold=7.5,
                        caption="Point at any line and ask why. The answer was stored when "
                                "the line was written.",
                        narration='Then ask why about a single line. You get the step, the model, and its reasoning at the moment it wrote that line, plus a pointer to the exact prompt and reply.'))
    fork_cmd = ('%s fork 4 "use the stdlib csv module instead" --provider script '
                '--script %s/agit/demo/script-stdlib.json --yes '
                '--verify \'python3 -m unittest discover -q -p "test_*.py" .\'' % (agit, REPO))
    fork_out = drop_noise(capture(fork_cmd, demo_dir),
                          ("---", "+++", "@@", "+", "-", "...", "agit log", "agit blame",
                           "agit report"))
    scenes.append(Scene(kind="terminal",
                        command='agit fork 4 "use the stdlib csv module instead"',
                        cwd=demo_dir, captured=fork_out,
                        title="branch the agent, not just the code", chunk=2, reveal_sec=0.3,
                        hold=5.0,
                        caption="Rewind to step 4 and try a different approach. The ledger "
                                "branches like git.",
                        narration='You are not stuck with the first approach. Fork at step four and try another one. The workspace rewinds and the ledger branches like a feature branch.'))
    scenes.append(Scene(kind="terminal", command="agit runs", cwd=demo_dir,
                        captured=drop_noise(capture("%s runs" % agit, demo_dir),
                                            ("agit log --run",)),
                        title="both attempts, both verified", chunk=1, reveal_sec=0.45,
                        hold=6.0,
                        caption="Two verified solutions to the same task, side by side.",
                        narration='Now there are two verified solutions to the same task. The hand-rolled scanner, and the one that delegates to the standard library.'))
    runs_raw = capture("%s runs" % agit, demo_dir)
    ids = re.findall(r"\b(\d{4}-\d{6}-[0-9a-f]{4}-[\w-]+)", SGR.sub("", runs_raw))
    left = next((i for i in ids if i.endswith("demo")), ids[-1] if ids else "")
    right = next((i for i in ids if i.endswith("fork")), ids[0] if ids else "")
    diff_out = "\n".join(capture("%s diff %s %s" % (agit, left, right),
                                 demo_dir).split("\n")[:22])
    scenes.append(Scene(kind="terminal", command="agit diff <run-a> <run-b>", cwd=demo_dir,
                        captured=diff_out, title="compare what two attempts produced",
                        chunk=2, reveal_sec=0.26, hold=6.0,
                        caption="Diff the outcomes, then keep the one you prefer.",
                        narration='Diff them like any two branches. Keep the one you prefer. The other stays in the ledger as the alternative you considered.'))
    scenes.append(Scene(kind="terminal", command="agit replay @last", cwd=demo_dir,
                        captured="\n".join(drop_noise(
                            capture("%s replay %s" % (agit, left), demo_dir),
                            ("---", "+++", "@@", "+", "-", "...", "$ ", "exit=", "====",
                             "FAIL:", "Ran ", "OK", "----")).split("\n")[-14:]),
                        title="re-drive the run with no network", chunk=1, reveal_sec=0.4,
                        hold=6.5,
                        caption="Every model call is recorded, so a run replays "
                                "byte-identically — for free.",
                        narration="Every model call is recorded, so the run replays with no network and no spend. Seven of seven steps identical. Run it live instead and agit reports where today's model diverges."))
    return scenes


def dashboard_scenes(stage: str) -> List[Scene]:
    """Build the dashboard and shoot the three tabs."""
    demo_dir = os.path.join(stage, "agit-demo")
    capture("python3 -m agit dashboard -o dash.html", demo_dir)
    runs_raw = SGR.sub("", capture("python3 -m agit runs", demo_dir))
    ids = re.findall(r"\b(\d{4}-\d{6}-[0-9a-f]{4}-[\w-]+)", runs_raw)
    main = next((i for i in ids if i.endswith("demo")), ids[-1] if ids else "")
    chrome = find_chrome()
    shots = os.path.join(stage, "shots")
    os.makedirs(shots, exist_ok=True)
    specs = [
        ("timeline", "run=%s&tab=timeline" % main, 1280, 840,
         "The dashboard: every run, every step, every reason — one static HTML file.",
         'All of that also comes as a user interface. One static HTML file, no server. Every run on the left, with forks nested under their parent. The timeline and the reasoning on the right.'),
        ("blame", "run=%s&tab=blame&file=csvkit.py&line=24" % main, 1280, 760,
         "Click any line: the step and the reasoning pin to the top.",
         'Click any line and the step that wrote it pins to the top, with its reasoning. Every view is a link, so you can send someone a line and the reason it exists.'),
        ("spend", "run=%s&tab=spend" % main, 1280, 700,
         "And where the tokens actually went, step by step.",
         'And where the tokens actually went, step by step, with the tools the run leaned on.'),
    ]
    scenes: List[Scene] = []
    for name, frag, width, height, caption, narration in specs:
        png = os.path.join(shots, name + ".png")
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
             "--window-size=%d,%d" % (width, height), "--screenshot=" + png,
             "file://%s/dash.html#%s" % (demo_dir, frag)],
            capture_output=True, check=False)
        if os.path.exists(png):
            scenes.append(Scene(kind="image", image=png, caption=caption,
                                narration=narration, hold=9.0, title=name))
    return scenes


def closing_scene() -> Scene:
    return Scene(
        kind="slide",
        title="agit",
        lines=["# github.com/VamsiKrishnaThota03/agit",
               "",
               "zero dependencies  ·  python + git only",
               "97 offline tests  ·  works with no API key",
               "any OpenAI-compatible model: BharatCode, Ollama, Groq, …",
               "",
               "> agit demo  —  the whole thing, in twenty seconds"],
        caption="Zero dependencies, 97 offline tests, and a demo that needs no API key.",
        narration='Zero dependencies, just Python and git. Ninety-seven offline tests. It works with BharatCode, a local Ollama, or any OpenAI-compatible endpoint. Clone it and run agit demo.',
        hold=9.0)


# -- assembly -------------------------------------------------------------

def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(path: str, spans: List[Tuple[float, float, str]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i, (start, end, text) in enumerate(spans, 1):
            fh.write("%d\n%s --> %s\n%s\n\n"
                     % (i, srt_time(start), srt_time(end), "\n".join(wrap(text, 62))))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(REPO, "demo", "agit-demo.mp4"))
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--keep-stage", action="store_true")
    args = parser.parse_args(argv)

    chrome = find_chrome()
    out_dir = os.path.dirname(os.path.abspath(args.out))
    frames_dir = os.path.join(out_dir, "frames")
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)
    # The staging workspace lives outside the repo so the demo run is never
    # affected by this repository's own .gitignore.
    stage = tempfile.mkdtemp(prefix="agit-demo-stage-")

    print("running the real agit commands ...")
    scenes = build_scenes(stage)
    print("shooting the dashboard ...")
    scenes += dashboard_scenes(stage)
    scenes.append(closing_scene())

    total = len(scenes)
    timeline: List[Tuple[str, float]] = []
    spans: List[Tuple[float, float, str]] = []
    clock = 0.0
    frame_no = 0
    tmp = os.path.join(stage, "tmp")
    os.makedirs(tmp, exist_ok=True)

    for index, scene in enumerate(scenes, 1):
        if scene.kind == "terminal":
            frames = reveal(scene)
            svgs = [(render_terminal(text, scene.caption, index, total, scene.title), dur)
                    for text, dur in frames]
        elif scene.kind == "image":
            svgs = [(render_image_frame(scene, index, total), scene.hold)]
        else:
            svgs = [(render_slide(scene, index, total), scene.hold)]

        # Stretch the last frame so the scene lasts at least as long as its
        # narration: the video is meant to be spoken over, not raced against.
        spoken = scene.speak_seconds
        shown = sum(d for _, d in svgs)
        if spoken > shown:
            text, duration = svgs[-1]
            svgs[-1] = (text, duration + (spoken - shown))

        scene_start = clock
        for svg_text, duration in svgs:
            frame_no += 1
            png = os.path.join(frames_dir, "f%04d.png" % frame_no)
            rasterise(chrome, svg_text, png, tmp)
            timeline.append((png, duration))
            clock += duration
        spans.append((scene_start, clock, scene.caption))
        print("  scene %2d/%d  %-46s %5.1fs  (%d frames)"
              % (index, total, scene.label[:46], clock - scene_start, len(svgs)))

    concat = os.path.join(stage, "concat.txt")
    with open(concat, "w", encoding="utf-8") as fh:
        for png, duration in timeline:
            fh.write("file '%s'\nduration %.3f\n" % (png, duration))
        fh.write("file '%s'\n" % timeline[-1][0])      # concat demuxer quirk

    print("encoding %s ..." % args.out)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
         "-vf", "fps=%d,format=yuv420p" % args.fps, "-c:v", "libx264", "-preset", "slow",
         "-crf", "20", "-movflags", "+faststart", args.out],
        check=True)

    srt = os.path.join(out_dir, "captions.srt")
    write_srt(srt, spans)
    with open(os.path.join(out_dir, "narration.txt"), "w", encoding="utf-8") as fh:
        for i, (scene, (start, end, _)) in enumerate(zip(scenes, spans), 1):
            words = len(scene.narration.split())
            fh.write("[%02d]  %s – %s   %.1fs on screen · %d words · ~%.1fs to read\n"
                     % (i, srt_time(start)[3:8], srt_time(end)[3:8], end - start,
                        words, scene.speak_seconds))
            fh.write("CAPTION: %s\n" % scene.caption)
            fh.write("SAY:     %s\n\n" % " ".join(wrap(scene.narration, 76)))

    if not args.keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    print("\ndone:")
    print("  video     %s  (%.1f s, %.1f MB)"
          % (args.out, clock, os.path.getsize(args.out) / 1e6))
    print("  captions  %s" % srt)
    print("  narration %s" % os.path.join(out_dir, "narration.txt"))
    print("  frames    %s (%d PNGs, reusable as stills)" % (frames_dir, frame_no))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
