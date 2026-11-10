#!/usr/bin/env python3
"""Render captured ANSI terminal output as a standalone SVG.

A development helper for the README images, not part of the agit package:

    AGIT_FORCE_COLOR=1 python3 -m agit demo /tmp/d --fast --force \
        | python3 tools/ansi2svg.py docs/demo.svg --title "agit demo"

Supports the subset of SGR codes agit emits: reset, bold, dim, inverse and
256-colour foregrounds.
"""
from __future__ import annotations

import html
import re
import sys
from typing import List, Optional, Tuple

PALETTE = {
    "208": "#ff9933",   # saffron
    "42": "#3ddc97",    # green
    "203": "#ff6b6b",   # red
    "75": "#58a6ff",    # blue
    "141": "#b48ead",   # violet
    "221": "#ffd166",   # yellow
}
FG = "#e6e9ef"
BG = "#0c0f14"
CHROME = "#161b24"
CHAR_W = 8.0
LINE_H = 19.0
FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
        "'DejaVu Sans Mono', monospace")
SGR = re.compile(r"\x1b\[([0-9;]*)m")


class Style:
    __slots__ = ("color", "bold", "dim", "inverse")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.color = FG
        self.bold = False
        self.dim = False
        self.inverse = False

    def copy(self) -> "Style":
        other = Style()
        other.color, other.bold, other.dim, other.inverse = (
            self.color, self.bold, self.dim, self.inverse)
        return other

    def apply(self, params: str) -> None:
        codes = [c for c in params.split(";") if c != ""] or ["0"]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in ("0", ""):
                self.reset()
            elif code == "1":
                self.bold = True
            elif code == "2":
                self.dim = True
            elif code == "7":
                self.inverse = True
            elif code == "22":
                self.bold = self.dim = False
            elif code == "27":
                self.inverse = False
            elif code == "38" and codes[i + 1:i + 2] == ["5"]:
                self.color = PALETTE.get(codes[i + 2] if len(codes) > i + 2 else "", FG)
                i += 2
            i += 1


Segment = Tuple[str, Style]


def parse(text: str) -> List[List[Segment]]:
    style = Style()
    lines: List[List[Segment]] = []
    for raw in text.replace("\r", "").split("\n"):
        segments: List[Segment] = []
        pos = 0
        for match in SGR.finditer(raw):
            chunk = raw[pos:match.start()]
            if chunk:
                segments.append((chunk, style.copy()))
            style.apply(match.group(1))
            pos = match.end()
        tail = raw[pos:]
        if tail:
            segments.append((tail, style.copy()))
        lines.append(segments)
    return lines


def render(lines: List[List[Segment]], title: str = "agit") -> str:
    widths = [sum(len(t) for t, _ in line) for line in lines] or [0]
    cols = max(max(widths), len(title) + 8, 40)
    pad_x, pad_top, pad_bottom = 18.0, 46.0, 16.0
    width = cols * CHAR_W + pad_x * 2
    height = len(lines) * LINE_H + pad_top + pad_bottom

    out: List[str] = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
               'viewBox="0 0 %.0f %.0f" font-family="%s" font-size="13">'
               % (width, height, width, height, FONT))
    out.append('<rect width="%.0f" height="%.0f" rx="10" fill="%s"/>'
               % (width, height, BG))
    out.append('<rect width="%.0f" height="30" rx="10" fill="%s"/>' % (width, CHROME))
    out.append('<rect y="20" width="%.0f" height="10" fill="%s"/>' % (width, CHROME))
    for i, dot in enumerate(("#ff6b6b", "#ffd166", "#3ddc97")):
        out.append('<circle cx="%.0f" cy="15" r="4.5" fill="%s"/>' % (18 + i * 17, dot))
    out.append('<text x="%.0f" y="19.5" fill="#8b96a8" font-size="11.5" '
               'text-anchor="middle">%s</text>' % (width / 2, html.escape(title)))

    for row, segments in enumerate(lines):
        y = pad_top + row * LINE_H
        col = 0
        pieces: List[str] = []
        for text, style in segments:
            if not text.strip():
                col += len(text)
                continue
            x = pad_x + col * CHAR_W
            if style.inverse:
                pieces.append(
                    '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s"/>'
                    % (x, y - 12.5, len(text) * CHAR_W, 17.0, style.color))
                fill, opacity = BG, 1.0
            else:
                fill = style.color
                opacity = 0.56 if style.dim else 1.0
            pieces.append(
                '<text x="%.1f" y="%.1f" fill="%s" opacity="%.2f"%s '
                'xml:space="preserve">%s</text>'
                % (x, y, fill, opacity,
                   ' font-weight="600"' if style.bold else "",
                   html.escape(text)))
            col += len(text)
        out.extend(pieces)
    out.append("</svg>")
    return "\n".join(out)


def main(argv: List[str]) -> int:
    out_path: Optional[str] = argv[1] if len(argv) > 1 else None
    title = "agit"
    if "--title" in argv:
        title = argv[argv.index("--title") + 1]
    text = sys.stdin.read()
    svg = render(parse(text), title)
    if out_path and out_path != "-":
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(svg + "\n")
        sys.stderr.write("wrote %s\n" % out_path)
    else:
        sys.stdout.write(svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
