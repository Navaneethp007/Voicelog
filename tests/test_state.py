"""Tests for voicelog.state - per-repo local state.

Real throwaway git repos, no mocking of git, matching tests/test_gitsource.py.
The acceptance test for "never committed by accident" is the one asserting
`git status --porcelain` is empty after a write.
"""
from __future__ import annotations

import json
import os
import subprocess

from voicelog import gitsource, state


def make_repo(tmp_path):
    """Create a minimal git repo with one commit; return (repo_path, run_fn)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    run = lambda *args: subprocess.run(
        args, cwd=repo, capture_output=True, text=True, env=env
    )
    run("git", "init")
    run("git", "config", "user.email", "t@t.com")
    run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("data")
    run("git", "add", "file.txt")
    run("git", "commit", "-m", "chore: first")
    return repo, run


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------

def test_state_lives_inside_the_git_directory(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    path = state.state_path()

    assert path is not None
    assert "voicelog" in path
    assert ".git" in path.replace(os.sep, "/")


def test_the_same_file_is_used_from_a_subdirectory(tmp_path, monkeypatch):
    """A location derived from the working directory would mean a run from a
    subdirectory quietly forgot where it had got to."""
    repo, _ = make_repo(tmp_path)
    (repo / "src").mkdir()

    monkeypatch.chdir(repo)
    state.record_summarised("a" * 40)
    top = state.last_summarised_sha()

    monkeypatch.chdir(repo / "src")
    assert state.last_summarised_sha() == top == "a" * 40


def test_no_state_path_outside_a_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert state.state_path() is None


def test_recording_outside_a_repo_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert state.record_summarised("a" * 40) is False
    assert state.last_summarised_sha() is None


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def test_round_trip(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert state.record_summarised("b" * 40) is True

    assert state.last_summarised_sha() == "b" * 40


def test_missing_state_reads_as_none(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert state.last_summarised_sha() is None


def test_corrupt_state_reads_as_none(tmp_path, monkeypatch):
    """Same discipline as the cache: local state is disposable, so a truncated
    file is a miss, never a crash."""
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    path = state.state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")

    assert state.last_summarised_sha() is None


def test_a_later_write_replaces_the_earlier_sha(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    state.record_summarised("c" * 40)
    state.record_summarised("d" * 40)

    assert state.last_summarised_sha() == "d" * 40


def test_state_records_a_version_for_future_readers(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    state.record_summarised("e" * 40)

    with open(state.state_path(), encoding="utf-8") as fh:
        stored = json.load(fh)
    assert stored["version"] >= 1
    assert stored["last_summarised_sha"] == "e" * 40
    assert stored["last_summarised_at"]


def test_write_leaves_no_temp_file(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    state.record_summarised("f" * 40)

    directory = os.path.dirname(state.state_path())
    assert not [n for n in os.listdir(directory) if n.endswith(".tmp")]


def test_an_unwritable_location_returns_false_rather_than_raising(tmp_path, monkeypatch):
    """A watermark is a convenience; failing to store it must never take down a
    run that already printed its changelog."""
    repo, _ = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(state.os, "makedirs", _boom)

    assert state.record_summarised("a" * 40) is False


# ---------------------------------------------------------------------------
# The point of putting it in .git
# ---------------------------------------------------------------------------

def test_writing_state_leaves_the_work_tree_clean(tmp_path, monkeypatch):
    """"Gitignored by default" achieved by not being in the work tree at all -
    no .gitignore entry to add, nothing to commit by accident."""
    repo, run = make_repo(tmp_path)
    monkeypatch.chdir(repo)

    state.record_summarised("a" * 40)

    assert run("git", "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# state shares gitsource's git boundary
# ---------------------------------------------------------------------------

def test_private_path_decodes_a_non_ascii_repo_path(tmp_path, monkeypatch):
    """rev-parse echoes the repo path back. Decoded through cp1252 that becomes
    a mojibake directory, which cache._write then creates as junk beside the
    real repo - two caches, neither found by the other."""
    repo = tmp_path / "репо"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)

    path = state.private_path("cache.json", str(repo))

    assert path is not None
    assert "репо" in path


def test_private_path_uses_the_shared_git_runner(tmp_path, monkeypatch):
    """One git boundary: state had its own subprocess.run, so every fix to the
    runner had to be remembered twice."""
    calls = []
    real = gitsource._run          # captured before patching, or the spy calls itself
    monkeypatch.setattr(state.gitsource, "_run",
                        lambda *a: calls.append(a) or real(*a))

    state.private_path("cache.json", str(tmp_path))

    assert calls, "private_path did not go through gitsource._run"
