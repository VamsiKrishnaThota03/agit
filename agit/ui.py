"""Terminal rendering. Stdlib only, degrades to plain ASCII."""
from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("AGIT_FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _supports_unicode(stream) -> bool:
    if os.environ.get("AGIT_ASCII"):
        return False
    enc = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in enc


class Theme:
    def __init__(self, color: bool, unicode_ok: bool):
        self.color = color
        self.unicode = unicode_ok

    # -- colors -----------------------------------------------------------
    def _c(self, code: str, text: str) -> str:
        return "\x1b[%sm%s\x1b[0m" % (code, text) if self.color else text

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def saffron(self, t: str) -> str:
        return self._c("38;5;208", t)

    def green(self, t: str) -> str:
        return self._c("38;5;42", t)

    def red(self, t: str) -> str:
        return self._c("38;5;203", t)

    def blue(self, t: str) -> str:
        return self._c("38;5;75", t)

    def violet(self, t: str) -> str:
        return self._c("38;5;141", t)

    def yellow(self, t: str) -> str:
        return self._c("38;5;221", t)

    def inverse(self, t: str) -> str:
        return self._c("7", t)

    # -- glyphs -----------------------------------------------------------
    @property
    def dot(self) -> str:
        return "●" if self.unicode else "*"

    @property
    def ring(self) -> str:
        return "○" if self.unicode else "o"

    @property
    def pipe(self) -> str:
        return "│" if self.unicode else "|"

    @property
    def tee(self) -> str:
        return "├" if self.unicode else "+"

    @property
    def elbow(self) -> str:
        return "└" if self.unicode else "\\"

    @property
    def tick(self) -> str:
        return "✓" if self.unicode else "OK"

    @property
    def cross(self) -> str:
        return "✗" if self.unicode else "X"

    @property
    def arrow(self) -> str:
        return "→" if self.unicode else "->"

    @property
    def hbar(self) -> str:
        return "─" if self.unicode else "-"

    @property
    def fork(self) -> str:
        return "⑂" if self.unicode else "Y"


class Console:
    def __init__(self, stream=None, quiet: bool = False):
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self.theme = Theme(_supports_color(self.stream), _supports_unicode(self.stream))

    @property
    def width(self) -> int:
        try:
            return max(48, min(100, shutil.get_terminal_size((88, 24)).columns))
        except Exception:
            return 88

    def write(self, text: str = "") -> None:
        if self.quiet:
            return
        self.stream.write(text + "\n")
        self.stream.flush()

    def rule(self, label: str = "") -> None:
        t = self.theme
        if not label:
            self.write(t.dim(t.hbar * self.width))
            return
        head = t.hbar * 2 + " "
        tail = t.hbar * max(0, self.width - len(label) - 4)
        self.write(t.dim(head) + t.bold(label) + " " + t.dim(tail))

    def banner(self, subtitle: str = "") -> None:
        t = self.theme
        self.write("")
        self.write("  " + t.saffron(t.bold("agit")) + t.dim("  git for AI agents"))
        if subtitle:
            self.write("  " + t.dim(subtitle))
        self.write("")

    def kv(self, key: str, value: str, pad: int = 14) -> None:
        self.write("  " + self.theme.dim(key.ljust(pad)) + value)

    def bullet(self, text: str, mark: Optional[str] = None) -> None:
        t = self.theme
        self.write("  " + (mark or t.dim(t.dot)) + " " + text)

    def ok(self, text: str) -> None:
        self.write("  " + self.theme.green(self.theme.tick) + " " + text)

    def fail(self, text: str) -> None:
        self.write("  " + self.theme.red(self.theme.cross) + " " + text)

    def warn(self, text: str) -> None:
        self.write("  " + self.theme.yellow("!") + " " + text)

    def note(self, text: str) -> None:
        self.write("  " + self.theme.dim(text))

    def paragraph(self, text: str, indent: str = "  ", dim: bool = False) -> None:
        """Wrap prose to the terminal width instead of running off the edge."""
        import textwrap

        width = max(40, self.width - len(indent))
        for block in (text or "").splitlines():
            if not block.strip():
                self.write("")
                continue
            for line in textwrap.wrap(block, width=width) or [""]:
                self.write(indent + (self.theme.dim(line) if dim else line))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
        rows = [[str(c) for c in r] for r in rows]
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row[: len(widths)]):
                widths[i] = max(widths[i], len(_visible(cell)))
        t = self.theme
        self.write("  " + t.dim("  ".join(h.upper().ljust(w) for h, w in zip(headers, widths))))
        for row in rows:
            cells = []
            for i, w in enumerate(widths):
                cell = row[i] if i < len(row) else ""
                cells.append(cell + " " * max(0, w - len(_visible(cell))))
            self.write("  " + "  ".join(cells))

    def block(self, text: str, limit: int = 20, indent: str = "    ") -> None:
        lines = text.rstrip("\n").split("\n")
        clipped = lines[:limit]
        for line in clipped:
            self.write(indent + self.theme.dim(line[: self.width]))
        if len(lines) > limit:
            self.write(indent + self.theme.dim("... %d more lines" % (len(lines) - limit)))

    def diff(self, text: str, limit: int = 24, indent: str = "    ") -> None:
        t = self.theme
        lines = text.rstrip("\n").split("\n")
        for line in lines[:limit]:
            if line.startswith("+") and not line.startswith("+++"):
                self.write(indent + t.green(line[: self.width]))
            elif line.startswith("-") and not line.startswith("---"):
                self.write(indent + t.red(line[: self.width]))
            elif line.startswith("@@"):
                self.write(indent + t.violet(line[: self.width]))
            else:
                self.write(indent + t.dim(line[: self.width]))
        if len(lines) > limit:
            self.write(indent + t.dim("... %d more diff lines" % (len(lines) - limit)))


class Spinner:
    """A live 'still working' indicator for the seconds a model takes to answer.

    No-op when stdout is not a terminal, so logs and CI output stay clean.
    """
    FRAMES_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    FRAMES_ASCII = "|/-\\"

    def __init__(self, console: "Console"):
        self.console = console
        self._thread: Optional[Any] = None
        self._stop: Optional[Any] = None
        self.enabled = (not console.quiet
                        and bool(getattr(console.stream, "isatty", lambda: False)())
                        and not os.environ.get("AGIT_NO_SPINNER"))

    def start(self, label: str) -> None:
        if not self.enabled or self._thread is not None:
            return
        import threading

        theme = self.console.theme
        frames = self.FRAMES_UNICODE if theme.unicode else self.FRAMES_ASCII
        stream = self.console.stream
        self._stop = threading.Event()
        stop = self._stop

        def spin() -> None:
            started = time.time()
            i = 0
            while not stop.is_set():
                elapsed = time.time() - started
                stream.write("\r  %s %s %s" % (
                    theme.saffron(frames[i % len(frames)]),
                    theme.dim(label),
                    theme.dim("%.1fs" % elapsed)))
                stream.flush()
                i += 1
                stop.wait(0.12)
            stream.write("\r\x1b[2K" if theme.color else "\r" + " " * 60 + "\r")
            stream.flush()

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._stop is not None:
            self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None
        self._stop = None

    def __enter__(self) -> "Spinner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _visible(text: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\x1b":
            j = text.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1_000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def human_ms(ms: int) -> str:
    if ms < 1000:
        return "%dms" % ms
    if ms < 60_000:
        return "%.1fs" % (ms / 1000.0)
    return "%dm%02ds" % (ms // 60_000, (ms % 60_000) // 1000)
