"""Read commits from the current git repository."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from voicelog.models import Commit


class NotAGitRepo(Exception):
    """Raised when the current directory is not inside a git work-tree."""


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


# What _run reports when git could not be launched at all, rather than running
# and refusing. 127 is the shell convention for "command not found".
_GIT_MISSING_RC = 127


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run one git command. The single git boundary for this package.

    Two things are pinned here rather than at each of the twelve call sites:

    - **UTF-8, not the locale codepage.** git emits UTF-8; ``text=True`` alone
      decodes with ``locale.getpreferredencoding()``, which is cp1252 on a
      default Windows box. A Cyrillic commit subject came back as mojibake and
      went on into the prompt, the changelog and the cache key; a byte that
      codepage does not define could end the run outright. ``errors="replace"``
      makes an undecodable byte a mangled character instead of a dead run -
      git itself permits a non-UTF-8 commit message, so this is reachable.
    - **OSError is a result, not an exception.** With git off PATH this raised a
      bare FileNotFoundError from whichever caller ran first, while
      ``state.private_path`` guarded the identical call. Returning a 127 result
      routes it to the same NotAGitRepo path every caller already handles.
    """
    try:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args, _GIT_MISSING_RC, "", f"git could not be run ({exc})"
        )


def _not_a_repo(result: subprocess.CompletedProcess) -> NotAGitRepo:
    """The right NotAGitRepo for a failed work-tree check.

    "git is not installed" and "this is not a repository" have the same handler
    but must not have the same message - the first one's fix is not `cd`.
    """
    if result.returncode == _GIT_MISSING_RC:
        return NotAGitRepo(result.stderr.strip() or "git could not be run.")
    return NotAGitRepo(
        "not a git repository (run voicelog from inside a git repo)"
    )


def _as_count(value: str) -> int:
    """A numstat count, or 0 when git did not print a number.

    Binary files report "-" by design; anything else non-numeric is git
    output we did not anticipate, and a changelog is not worth a crash
    over a diffstat.
    """
    try:
        return int(value)
    except ValueError:
        return 0


# A rename with a common prefix, as --numstat spells it: "dir/{old => new}".
_RENAME_RE = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def _rename_target(path: str) -> str:
    """The post-rename path for something git may have written as a rename.

    --numstat encodes a rename inline, where --name-only (which it replaced)
    gave a plain path. Left alone, "src/{old.py => new.py}" reached the prompt
    as a filename that never existed.

    Two spellings, because git only uses braces when the paths share a prefix:
    "dir/{old.txt => new.txt}" and, for an unrelated move, "old.txt => new.txt".
    """
    if "{" in path:
        return _RENAME_RE.sub(lambda m: m.group(2), path)
    before, sep, after = path.partition(" => ")
    return after if sep else path


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
        files.append(_rename_target(path))
        # Binary files report "-", and an unreadable count is not worth a crash
        # in the middle of a changelog, so anything non-numeric contributes 0.
        insertions += _as_count(ins)
        deletions += _as_count(del_)
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
        raise _not_a_repo(check)

    log_result = _run("git", "log", f"-n{n}", _PRETTY, "--numstat")
    output = log_result.stdout

    if not output.strip():
        return GitResult(commits=[], used_fallback=False, tag=None)

    commits = _parse_log_output(output)
    diff = _get_diff(commits[-1].hash) if with_diff and commits else None
    return GitResult(commits=commits, used_fallback=False, tag=None, diff=diff)


def head_sha() -> str | None:
    """Full sha of HEAD, or None in an empty repo or outside one.

    Used for the watermark rather than ``GitResult.commits[0].hash``: git log
    orders by commit date, so with clock skew or a rebase the newest record is
    not necessarily HEAD - and after noise filtering it definitely is not.
    """
    result = _run("git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_ancestor(candidate: str, descendant: str = "HEAD") -> bool:
    """Whether ``candidate`` is reachable from ``descendant``.

    Guards a stored watermark. After a rebase, a hard reset, a gc or a shallow
    clone the sha may not resolve at all; after a branch switch it can resolve
    while ``<sha>..HEAD`` means nothing useful. git exits 1 for "no" and 128 for
    "no such commit", so both collapse to False - which is what keeps a stale
    watermark from reaching read_commits and ending the run with "unknown git
    ref".
    """
    if not candidate:
        return False
    return _run(
        "git", "merge-base", "--is-ancestor", candidate, descendant
    ).returncode == 0


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


def _get_diff(
    oldest_commit_hash: str,
    max_chars: int = _MAX_DIFF_CHARS,
    newest_commit_hash: str = "HEAD",
) -> str:
    """Aggregate unified diff for everything after ``oldest_commit_hash``'s parent.

    Diffs against the actual oldest commit IN THE PARSED RANGE (not an assumed
    ref/count), so it's exactly the code behind those commits — correct whether
    the range came from a tag, ``--since``, or the no-tags fallback. Falls back
    to git's empty-tree hash when the oldest commit has no parent (it's the
    repo's root commit), which always resolves.
    """
    has_parent = _run("git", "rev-parse", "--verify", "--quiet", f"{oldest_commit_hash}^")
    base = f"{oldest_commit_hash}^" if has_parent.returncode == 0 else _EMPTY_TREE_SHA

    diff_result = _run("git", "diff", f"{base}..{newest_commit_hash}")
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
    # Both ends, not just the oldest. Scoping only the oldest end left the range
    # running to HEAD, so a noise-filtered newest commit was dropped from the
    # prompt and the cache key while its code still went to the provider -
    # against this function's own promise and the opt-in framing of --with-diff.
    return _get_diff(commits[-1].hash, max_chars, commits[0].hash)


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
        raise _not_a_repo(check)

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
