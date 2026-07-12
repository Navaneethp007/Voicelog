"""Read commits from the current git repository."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from voicelog.models import Commit


class NotAGitRepo(Exception):
    """Raised when the current directory is not inside a git work-tree."""


class NoCommitsFound(Exception):
    """Raised when the commit range yields no commits (reserved for future use)."""


class RefNotFound(Exception):
    """Raised when a --since/--pull ref cannot be resolved to a commit."""


@dataclass
class GitResult:
    commits: list[Commit]
    used_fallback: bool  # True when no prior tag existed
    tag: str | None = None  # the latest tag, or None when no tags exist
    diff: str | None = None  # aggregate range diff, only populated when with_diff=True


# Git's magic hash for an empty tree — always resolvable, used as the diff base
# when the oldest commit in range has no parent (i.e. it's the repo's root commit).
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Diff is capped to keep prompts bounded; truncation is noted so it's never silent.
_MAX_DIFF_CHARS = 20_000


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _parse_numstat_block(files_raw: str) -> tuple[list[str], int, int]:
    """Parse ``--numstat`` lines: "<ins>\\t<del>\\t<path>" per file.

    Binary files report "-" for both counts (git can't diff them as text) — we
    still list the filename but contribute 0 to the insertion/deletion totals.
    """
    files: list[str] = []
    insertions = 0
    deletions = 0
    for line in files_raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue  # unexpected git output — skip rather than crash
        ins, del_, path = parts
        files.append(path)
        if ins != "-":
            insertions += int(ins)
        if del_ != "-":
            deletions += int(del_)
    return files, insertions, deletions


def _parse_log_output(output: str) -> list[Commit]:
    """Parse the output of:
        git log ... --pretty=format:"%x1e%H%x1f%s%x1f%b%x1f%an%x1f" --numstat

    Each commit record starts with a record separator (\\x1e) and contains five
    \\x1f-delimited fields: hash, subject, body, author, and the trailing
    numstat block (from --numstat: "<insertions>\\t<deletions>\\t<path>" per
    file). Putting \\x1e at the START of every record and a \\x1f *after* the
    author makes the numstat block a self-delimiting 5th field, so commits with
    no files (e.g. --allow-empty) parse just like any other.
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
        files, insertions, deletions = _parse_numstat_block(files_raw)
        commits.append(
            Commit(
                hash=commit_hash,
                subject=subject,
                body=body,
                author=author,
                files=files,
                insertions=insertions,
                deletions=deletions,
            )
        )

    return commits


_PRETTY = "--pretty=format:\x1e%H\x1f%s\x1f%b\x1f%an\x1f"


def read_recent_commits(n: int = 15, with_diff: bool = False) -> GitResult:
    """Return the last ``n`` commits, ignoring tags entirely.

    Used by ``--new`` to orient a developer on a repo they just cloned: "what's
    been happening lately" regardless of releases. Unlike :func:`read_commits`,
    a tag on HEAD does not make this return an empty list.
    """
    check = _run("git", "rev-parse", "--is-inside-work-tree")
    if check.returncode != 0:
        raise NotAGitRepo("Current directory is not inside a git repository.")

    log_result = _run("git", "log", f"-n{n}", _PRETTY, "--numstat")
    output = log_result.stdout

    if not output.strip():
        return GitResult(commits=[], used_fallback=False, tag=None)

    commits = _parse_log_output(output)
    diff = _get_diff(commits[-1].hash) if with_diff and commits else None
    return GitResult(commits=commits, used_fallback=False, tag=None, diff=diff)


def detect_base_branch() -> str | None:
    """Best-effort guess of the branch a PR would target.

    Prefers the remote's default branch (``origin/HEAD``); otherwise falls back
    to the first conventional trunk that exists. Returns ``None`` if none found.
    """
    remote_head = _run("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if remote_head.returncode == 0 and remote_head.stdout.strip():
        return remote_head.stdout.strip()  # e.g. "origin/main"

    for candidate in ("origin/main", "origin/master", "main", "master"):
        found = _run("git", "rev-parse", "--verify", "--quiet", candidate)
        if found.returncode == 0:
            return candidate
    return None


def _get_diff(oldest_commit_hash: str, max_chars: int = _MAX_DIFF_CHARS) -> str:
    """Aggregate unified diff for everything after ``oldest_commit_hash``'s parent.

    Diffs against the actual oldest commit IN THE PARSED RANGE (not an assumed
    ref/count), so it's exactly the code behind those commits — correct whether
    the range came from a tag, ``--since``, or the no-tags fallback. Falls back
    to git's empty-tree hash when the oldest commit has no parent (it's the
    repo's root commit), which always resolves.
    """
    has_parent = _run("git", "rev-parse", "--verify", "--quiet", f"{oldest_commit_hash}^")
    base = f"{oldest_commit_hash}^" if has_parent.returncode == 0 else _EMPTY_TREE_SHA

    diff_result = _run("git", "diff", f"{base}..HEAD")
    diff = diff_result.stdout
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n... (diff truncated)"
    return diff


def diff_for_commits(commits: list[Commit], max_chars: int = _MAX_DIFF_CHARS) -> str | None:
    """Aggregate diff scoped to exactly the given (already filtered/capped)
    commit list — not the original unfiltered range.

    ``GitResult.diff`` is computed once, up front, against the full range
    ``read_commits``/``read_recent_commits`` fetched. If a caller then
    noise-filters or caps that commit list, the oldest commit still in the
    final list may differ from what ``GitResult.diff`` was scoped to — so the
    diff would cover commits no longer being summarised. Call this after
    filtering/capping to get a diff that matches what's actually sent.
    """
    if not commits:
        return None
    return _get_diff(commits[-1].hash, max_chars)


def read_commits(
    fallback_commits: int = 50,
    since: str | None = None,
    with_diff: bool = False,
) -> GitResult:
    """Return commits to summarise.

    - ``since`` given → commits in ``since..HEAD`` (e.g. what a git pull brought
      in via ``ORIG_HEAD``). Raises :class:`RefNotFound` if the ref is invalid.
    - otherwise → commits since the last tag, or the last ``fallback_commits``
      when the repo has no tags.
    - ``with_diff=True`` → also fetch the aggregate code diff for the range
      (``GitResult.diff``). Opt-in: sends actual code to the LLM downstream.
    """
    # 1. Verify we're inside a git work-tree.
    check = _run("git", "rev-parse", "--is-inside-work-tree")
    if check.returncode != 0:
        raise NotAGitRepo("Current directory is not inside a git repository.")

    if since is not None:
        # 2a. Explicit range: <since>..HEAD. Validate the ref first.
        verify = _run("git", "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}")
        if verify.returncode != 0:
            raise RefNotFound(since)
        used_fallback = False
        last_tag = None
        log_args = ["git", "log", f"{since}..HEAD", _PRETTY, "--numstat"]
    else:
        # 2b. Find the most recent tag.
        tag_result = _run("git", "describe", "--tags", "--abbrev=0")
        used_fallback = tag_result.returncode != 0
        last_tag = None if used_fallback else tag_result.stdout.strip()

        if not used_fallback:
            log_args = [
                "git", "log", f"{last_tag}..HEAD", _PRETTY, "--numstat",
            ]
        else:
            log_args = [
                "git", "log", f"-n{fallback_commits}", _PRETTY, "--numstat",
            ]

    # 3. Run git log.
    log_result = _run(*log_args)
    output = log_result.stdout

    # 4. Parse commits.
    if not output.strip():
        return GitResult(commits=[], used_fallback=used_fallback, tag=last_tag)

    commits = _parse_log_output(output)

    # 5. Optionally fetch the aggregate diff for exactly this commit range.
    diff = _get_diff(commits[-1].hash) if with_diff and commits else None

    return GitResult(commits=commits, used_fallback=used_fallback, tag=last_tag, diff=diff)
