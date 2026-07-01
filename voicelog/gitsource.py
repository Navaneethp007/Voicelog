"""Read commits from the current git repository."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from voicelog.models import Commit


class NotAGitRepo(Exception):
    """Raised when the current directory is not inside a git work-tree."""


class NoCommitsFound(Exception):
    """Raised when the commit range yields no commits (reserved for future use)."""


@dataclass
class GitResult:
    commits: list[Commit]
    used_fallback: bool  # True when no prior tag existed
    tag: str | None = None  # the latest tag, or None when no tags exist


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _parse_log_output(output: str) -> list[Commit]:
    """Parse the output of:
        git log ... --pretty=format:"%x1e%H%x1f%s%x1f%b%x1f%an%x1f" --name-only

    Each commit record starts with a record separator (\\x1e) and contains five
    \\x1f-delimited fields: hash, subject, body, author, and the trailing file
    list (from --name-only). Putting \\x1e at the START of every record and a
    \\x1f *after* the author makes the file list a self-delimiting 5th field, so
    commits with no files (e.g. --allow-empty) parse just like any other.
    """
    commits: list[Commit] = []
    for record in output.split("\x1e"):
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 4:
            continue
        commit_hash = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip()
        author = parts[3].strip()
        files_raw = parts[4] if len(parts) > 4 else ""
        files = [f for f in files_raw.splitlines() if f.strip()]
        commits.append(
            Commit(hash=commit_hash, subject=subject, body=body, author=author, files=files)
        )

    return commits


def read_commits(fallback_commits: int = 50) -> GitResult:
    """Return commits since the last tag (or last *fallback_commits* if no tag)."""
    # 1. Verify we're inside a git work-tree.
    check = _run("git", "rev-parse", "--is-inside-work-tree")
    if check.returncode != 0:
        raise NotAGitRepo("Current directory is not inside a git repository.")

    # 2. Find the most recent tag.
    tag_result = _run("git", "describe", "--tags", "--abbrev=0")
    used_fallback = tag_result.returncode != 0
    last_tag = None if used_fallback else tag_result.stdout.strip()

    if not used_fallback:
        revision_range = f"{last_tag}..HEAD"
        log_args = [
            "git", "log", revision_range,
            "--pretty=format:\x1e%H\x1f%s\x1f%b\x1f%an\x1f",
            "--name-only",
        ]
    else:
        log_args = [
            "git", "log", f"-n{fallback_commits}",
            "--pretty=format:\x1e%H\x1f%s\x1f%b\x1f%an\x1f",
            "--name-only",
        ]

    # 3. Run git log.
    log_result = _run(*log_args)
    output = log_result.stdout

    # 4. Parse commits.
    if not output.strip():
        return GitResult(commits=[], used_fallback=used_fallback, tag=last_tag)

    commits = _parse_log_output(output)
    return GitResult(commits=commits, used_fallback=used_fallback, tag=last_tag)
