"""Workspace snapshots.

Every agent step stores a full copy of the agent-visible workspace inside the
ledger. Git deduplicates unchanged blobs, so the cost is one tree per step --
and in exchange `git blame` inside the ledger gives exact per-line attribution
of AI-written code for free.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional, Tuple

from . import gitutil

ALWAYS_SKIP_DIRS = {
    ".git", ".agit", ".hg", ".svn",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env", ".tox", ".next", ".nuxt", ".gradle", ".idea", ".vscode",
    "dist", "build", "target", "coverage", ".cache", ".parcel-cache", ".terraform",
}
ALWAYS_SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".class", ".jar", ".zip", ".gz", ".tgz",
    ".bz2", ".xz", ".7z", ".rar", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2", ".ttf", ".eot",
    ".sqlite", ".db", ".bin", ".exe", ".o", ".a", ".wasm",
)


def _skip_path(rel: str) -> bool:
    parts = rel.replace(os.sep, "/").split("/")
    if any(p in ALWAYS_SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in ALWAYS_SKIP_FILES:
        return True
    return name.lower().endswith(SKIP_SUFFIXES)


def _walk(root: str) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if not _skip_path(rel):
                found.append(rel.replace(os.sep, "/"))
    return found


def list_files(root: str) -> List[str]:
    """Agent-visible files: git's view when available (so .gitignore is honoured),
    otherwise a filtered walk."""
    tracked = gitutil.tracked_and_untracked(root)
    if tracked is None:
        files = _walk(root)
    else:
        files = [p for p in tracked if not _skip_path(p) and os.path.isfile(os.path.join(root, p))]
    return sorted(set(files))


def copy_into(root: str, dest: str, max_bytes: int = 512 * 1024) -> Dict[str, int]:
    """Replace `dest` with a fresh snapshot of `root`. Returns stats."""
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    kept = skipped = total = 0
    skipped_names: List[str] = []
    for rel in list_files(root):
        src = os.path.join(root, rel)
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if size > max_bytes:
            skipped += 1
            skipped_names.append("%s (%d bytes)" % (rel, size))
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            shutil.copy2(src, target)
        except OSError:
            skipped += 1
            skipped_names.append(rel)
            continue
        kept += 1
        total += size
    if skipped_names:
        with open(os.path.join(dest, ".agit-skipped"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(skipped_names)) + "\n")
    return {"files": kept, "skipped": skipped, "bytes": total}


def restore_from(src: str, root: str) -> Dict[str, int]:
    """Make `root` match the snapshot in `src`: overwrite, create and delete."""
    wanted = {}
    if os.path.isdir(src):
        for rel in _walk(src):
            if rel == ".agit-skipped":
                continue
            wanted[rel] = os.path.join(src, rel)
    existing = set(list_files(root))
    written = removed = 0
    for rel, path in wanted.items():
        target = os.path.join(root, rel)
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        shutil.copy2(path, target)
        written += 1
    for rel in existing - set(wanted):
        try:
            os.remove(os.path.join(root, rel))
            removed += 1
        except OSError:
            pass
    _prune_empty_dirs(root)
    return {"written": written, "removed": removed}


def _prune_empty_dirs(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        parts = os.path.relpath(dirpath, root).split(os.sep)
        if any(p in ALWAYS_SKIP_DIRS for p in parts):
            continue
        if not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
