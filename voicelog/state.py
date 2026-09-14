"""Per-repo local state: how far voicelog has summarised.

Stored in the repo's own git directory (``.git/voicelog/state.json``) because
that is the one place git guarantees is outside the work tree. Three properties
follow, and all three matter:

- **Nothing to gitignore.** It cannot be committed by accident, so voicelog
  needs no entry in anybody's ``.gitignore`` and writes nothing into the tree.
- **The same file from any directory.** ``cache`` resolves its path against the
  current directory, so running voicelog from a subdirectory reads a *different*
  cache. A watermark with that bug would quietly forget where it had got to.
- **Per worktree.** ``rev-parse --git-path`` resolves correctly for submodules
  (where ``.git`` is a file) and for linked worktrees - which is what you want,
  since each worktree has its own HEAD.

Failing to store the watermark is never fatal: it is a convenience, and a run
that has already printed its changelog must not be brought down by it.
"""
from __future__ import annotations

import datetime
import json
import os

from voicelog import fileio, gitsource

STATE_VERSION = 1


def private_path(relpath: str, repo_dir: str | None = None) -> str | None:
    """Absolute path to a voicelog-private file inside a repo's git directory.

    ``repo_dir`` defaults to the current directory. Resolving it explicitly
    matters for callers that already have a directory in hand: asking git about
    the cwd instead would put their file in a *different* repo's git directory
    whenever the two disagree.

    Returns None when there is no repo. See the module docstring for why this
    location rather than one in the work tree.
    """
    args = ["git"]
    if repo_dir:
        args += ["-C", repo_dir]
    args += ["rev-parse", "--absolute-git-dir"]
    # Through gitsource's runner, not a second subprocess.run of our own: it
    # pins UTF-8 (rev-parse echoes the repo path back, and a non-ASCII one
    # decoded through cp1252 becomes a mojibake directory that this module then
    # creates as junk beside the real repo) and turns a missing git into a
    # non-zero result rather than an exception.
    result = gitsource._run(*args)
    git_dir = result.stdout.strip()
    if result.returncode != 0 or not git_dir:
        return None  # not a git repository
    return os.path.join(git_dir, "voicelog", relpath)


def state_path() -> str | None:
    """Path to this repo's state file, or None when not inside a repo.

    The path is relative to the current directory, as git reports it, so use it
    promptly rather than storing it.
    """
    return private_path("state.json")


def _read() -> dict:
    """Parsed state, or {} when absent or unreadable.

    Local state is disposable, so a truncated file is a miss rather than an
    error - the same discipline as ``cache._read``.
    """
    path = state_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def last_summarised_sha() -> str | None:
    """The commit voicelog last summarised here, or None if there is no record."""
    sha = _read().get("last_summarised_sha")
    return sha if isinstance(sha, str) and sha else None


def record_summarised(sha: str) -> bool:
    """Record ``sha`` as summarised. Returns whether it was stored.

    Written atomically (temp file + replace) so two concurrent runs are
    last-write-wins rather than a corrupt file. Never raises: the caller has
    already shown the user their changelog by this point.
    """
    path = state_path()
    if not path or not sha:
        return False

    payload = {
        "version": STATE_VERSION,
        "last_summarised_sha": sha,
        "last_summarised_at": datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
    }
    # Preserve anything a later version may have added alongside our keys.
    data = _read()
    data.update(payload)

    try:
        fileio.atomic_write_text(path, json.dumps(data))
    except OSError:
        # Silent: a watermark is a convenience, and the caller has already
        # shown the user their changelog by this point.
        return False
    return True
