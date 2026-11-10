"""Thin wrapper around the git CLI. agit uses real git, not a reimplementation,
so `git log`, `git blame` and `git diff` keep all of their power inside the ledger."""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional, Sequence, Tuple


class GitError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str):
        super().__init__("git %s failed (%d): %s" % (" ".join(args), code, stderr.strip()))
        self.code = code
        self.stderr = stderr


_IDENTITY = [
    "-c", "user.name=agit",
    "-c", "user.email=agit@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "advice.detachedHead=false",
    "-c", "core.quotepath=false",
]


def git(repo: str, *args: str, check: bool = True, input_text: Optional[str] = None) -> str:
    cmd = ["git", "-C", repo] + _IDENTITY + list(args)
    env = dict(os.environ)
    # Keep the user's global hooks/templates/signing out of the ledger.
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "agit",
        "GIT_AUTHOR_EMAIL": "agit@localhost",
        "GIT_COMMITTER_NAME": "agit",
        "GIT_COMMITTER_EMAIL": "agit@localhost",
    })
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc.stdout


def available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def is_repo(path: str) -> bool:
    try:
        out = git(path, "rev-parse", "--is-inside-work-tree")
        return out.strip() == "true"
    except (GitError, OSError):
        return False


def tracked_and_untracked(path: str) -> Optional[List[str]]:
    """Files git would show, honouring .gitignore. None when `path` is not a repo."""
    if not is_repo(path):
        return None
    try:
        out = git(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    except GitError:
        return None
    return [p for p in out.split("\0") if p]


def head_sha(repo: str) -> Optional[str]:
    try:
        return git(repo, "rev-parse", "HEAD").strip()
    except GitError:
        return None


def current_branch(repo: str) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def branches(repo: str, prefix: str = "") -> List[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/" + prefix)
    return [b for b in out.splitlines() if b]


def trailers(repo: str, sha: str) -> Tuple[str, dict]:
    """Return (subject+body, trailer dict) for a commit."""
    raw = git(repo, "show", "-s", "--format=%B", sha)
    body: List[str] = []
    found: dict = {}
    for line in raw.splitlines():
        if line.startswith("Agit-") and ":" in line:
            key, _, value = line.partition(":")
            found[key.strip()] = value.strip()
        else:
            body.append(line)
    return "\n".join(body).strip(), found
