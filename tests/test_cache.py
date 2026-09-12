"""Tests for voicelog.cache — generation cache keyed on the commit set."""
from __future__ import annotations

import os
import subprocess

from voicelog import cache as cache_module
from voicelog.cache import cache_key, get, put, get_summary, put_summary, last
from voicelog.models import Commit


def _commit(h, subject="feat: x"):
    return Commit(hash=h, subject=subject, body="", author="A", files=[])


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------

def test_cache_key_is_deterministic():
    commits = [_commit("a"), _commit("b")]
    k1 = cache_key(commits, "model-x", ["Features", "Fixes"], "")
    k2 = cache_key(commits, "model-x", ["Features", "Fixes"], "")
    assert k1 == k2


def test_cache_key_changes_with_commits():
    base = [_commit("a")]
    other = [_commit("a"), _commit("b")]
    assert cache_key(base, "m", ["S"], "") != cache_key(other, "m", ["S"], "")


def test_cache_key_changes_with_model():
    commits = [_commit("a")]
    assert cache_key(commits, "m1", ["S"], "") != cache_key(commits, "m2", ["S"], "")


def test_cache_key_changes_with_sections():
    commits = [_commit("a")]
    assert cache_key(commits, "m", ["A"], "") != cache_key(commits, "m", ["A", "B"], "")


def test_cache_key_changes_with_voice_text():
    """Editing the voice samples (same commits) must invalidate the cache."""
    commits = [_commit("a")]
    assert cache_key(commits, "m", ["S"], "casual") != cache_key(commits, "m", ["S"], "formal")


def test_cache_key_independent_of_commit_order():
    """Same set of commits in any order yields the same key."""
    a, b = _commit("a"), _commit("b")
    assert cache_key([a, b], "m", ["S"], "") == cache_key([b, a], "m", ["S"], "")


def test_cache_key_changes_with_with_diff():
    """A --with-diff run must not reuse a no-diff run's cached output (and vice
    versa) — the prompt content, and therefore the expected output, differs."""
    commits = [_commit("a")]
    assert cache_key(commits, "m", ["S"], "") != cache_key(commits, "m", ["S"], "", with_diff=True)


def test_cache_key_with_diff_defaults_to_false():
    """Omitting with_diff is identical to passing with_diff=False."""
    commits = [_commit("a")]
    assert cache_key(commits, "m", ["S"], "") == cache_key(commits, "m", ["S"], "", with_diff=False)


# ---------------------------------------------------------------------------
# get / put
# ---------------------------------------------------------------------------

def test_get_missing_returns_none(tmp_path):
    assert get(str(tmp_path), "anykey") is None


def test_put_then_get_roundtrip(tmp_path):
    put(str(tmp_path), "key1", "## Unreleased\n\ncached text")
    assert get(str(tmp_path), "key1") == "## Unreleased\n\ncached text"


def test_get_different_key_returns_none(tmp_path):
    put(str(tmp_path), "key1", "text")
    assert get(str(tmp_path), "key2") is None


def test_put_overwrites_previous(tmp_path):
    put(str(tmp_path), "key1", "old")
    put(str(tmp_path), "key2", "new")
    # Single-entry cache: only the latest key is retained.
    assert get(str(tmp_path), "key2") == "new"
    assert get(str(tmp_path), "key1") is None


def test_cache_falls_back_under_the_given_dir_outside_a_repo(tmp_path):
    """Inside a repo the cache lives in the git directory (see below); this is
    the fallback that keeps the path resolvable when there is no repo."""
    put(str(tmp_path), "k", "v")
    assert (tmp_path / ".changelog" / ".voicelog-cache.json").exists()


# ---------------------------------------------------------------------------
# summary cache (keyed on the changelog key + detail level)
# ---------------------------------------------------------------------------

def test_get_summary_missing_returns_none(tmp_path):
    put(str(tmp_path), "k", "md")
    assert get_summary(str(tmp_path), "k", detail=False) is None


def test_put_then_get_summary_roundtrip(tmp_path):
    put(str(tmp_path), "k", "md")
    put_summary(str(tmp_path), "k", False, "brief spoken")
    assert get_summary(str(tmp_path), "k", detail=False) == "brief spoken"


def test_summary_is_keyed_on_detail(tmp_path):
    put(str(tmp_path), "k", "md")
    put_summary(str(tmp_path), "k", False, "brief one")
    put_summary(str(tmp_path), "k", True, "detailed one")
    assert get_summary(str(tmp_path), "k", detail=False) == "brief one"
    assert get_summary(str(tmp_path), "k", detail=True) == "detailed one"


def test_put_summary_preserves_markdown(tmp_path):
    put(str(tmp_path), "k", "the markdown")
    put_summary(str(tmp_path), "k", False, "spoken")
    assert get(str(tmp_path), "k") == "the markdown"


def test_new_markdown_invalidates_summaries(tmp_path):
    put(str(tmp_path), "k", "md1")
    put_summary(str(tmp_path), "k", False, "spoken for md1")
    put(str(tmp_path), "k2", "md2")  # new commit set → new key
    assert get_summary(str(tmp_path), "k2", detail=False) is None
    assert get_summary(str(tmp_path), "k", detail=False) is None


def test_get_summary_wrong_key_returns_none(tmp_path):
    put(str(tmp_path), "k", "md")
    put_summary(str(tmp_path), "k", False, "spoken")
    assert get_summary(str(tmp_path), "other", detail=False) is None


# ---------------------------------------------------------------------------
# Provider identity participates in the key
# ---------------------------------------------------------------------------

def _commits():
    return [Commit("abc123", "feat: thing", "", "Al", [])]


def test_key_changes_with_the_provider():
    """The same model id served by two providers is not the same output."""
    a = cache_key(_commits(), "m", ["Features"], "", provider="openai",
                  base_url="https://api.openai.com/v1")
    b = cache_key(_commits(), "m", ["Features"], "", provider="openrouter",
                  base_url="https://api.openai.com/v1")

    assert a != b


def test_key_changes_with_the_base_url():
    a = cache_key(_commits(), "m", ["Features"], "", base_url="https://one.test/v1")
    b = cache_key(_commits(), "m", ["Features"], "", base_url="https://two.test/v1")

    assert a != b


def test_omitting_the_provider_matches_passing_blanks():
    """Keeps the existing positional call sites meaningful."""
    assert cache_key(_commits(), "m", ["Features"], "") == cache_key(
        _commits(), "m", ["Features"], "", provider="", base_url=""
    )


# ---------------------------------------------------------------------------
# Where the cache lives
# ---------------------------------------------------------------------------

def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True, env=env)
    run("git", "init")
    (repo / "f.txt").write_text("x")
    run("git", "add", "f.txt")
    run("git", "commit", "-m", "first")
    return repo, run


def test_cache_lives_in_the_git_directory_inside_a_repo(tmp_path, monkeypatch):
    """So it needs no .gitignore entry in anyone's project."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    put(str(repo), "key1", "## Unreleased\n\n- a thing")

    assert not (repo / ".changelog").exists()
    assert get(str(repo), "key1") == "## Unreleased\n\n- a thing"


def test_caching_leaves_the_work_tree_clean(tmp_path, monkeypatch):
    repo, run = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    put(str(repo), "key1", "markdown")

    assert run("git", "status", "--porcelain").stdout.strip() == ""


def test_the_same_cache_is_used_from_a_subdirectory(tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    (repo / "src").mkdir()
    monkeypatch.chdir(repo)
    put(str(repo), "key1", "markdown")

    monkeypatch.chdir(repo / "src")

    assert get(str(repo / "src"), "key1") == "markdown"


def test_outside_a_repo_it_falls_back_to_the_given_directory(tmp_path, monkeypatch):
    """Keeps the function total - it always has somewhere to write - so no
    caller has to handle a missing location. voicelog requires a repo anyway."""
    monkeypatch.chdir(tmp_path)

    put(str(tmp_path), "key1", "markdown")

    assert get(str(tmp_path), "key1") == "markdown"


# ---------------------------------------------------------------------------
# A cache failure must never cost a generation
# ---------------------------------------------------------------------------

def test_put_reports_failure_instead_of_raising(tmp_path, monkeypatch, capsys):
    """put() runs after a paid LLM call and before the changelog is printed, so
    an unwritable location used to turn a completed call into a traceback with
    no output at all."""
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(cache_module.os, "makedirs", _boom)

    assert put(str(tmp_path), "k", "markdown") is False

    err = capsys.readouterr().err
    assert "cache" in err.lower()
    assert "read-only file system" in err


def test_put_says_what_the_failure_costs(tmp_path, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(cache_module.os, "makedirs", _boom)
    put(str(tmp_path), "k", "markdown")

    # The user needs the consequence, not just the cause: this is what connects
    # an unwritable cache to tomorrow's API bill.
    assert "not be reused" in capsys.readouterr().err


def test_put_summary_reports_failure_instead_of_raising(tmp_path, monkeypatch):
    put(str(tmp_path), "k", "markdown")

    def _boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(cache_module.os, "makedirs", _boom)

    assert put_summary(str(tmp_path), "k", False, "spoken") is False


def test_a_successful_put_returns_true(tmp_path):
    assert put(str(tmp_path), "k", "markdown") is True
    assert put_summary(str(tmp_path), "k", False, "spoken") is True


def test_the_write_is_atomic(tmp_path, monkeypatch):
    """put_summary rewrites the entry INCLUDING the markdown, so an interrupt
    mid-write would truncate a generation already paid for."""
    put(str(tmp_path), "k", "markdown")
    real_replace = cache_module.os.replace
    seen = {}

    def _spy(src, dst):
        seen["src"], seen["dst"] = src, dst
        return real_replace(src, dst)

    monkeypatch.setattr(cache_module.os, "replace", _spy)
    put_summary(str(tmp_path), "k", False, "spoken")

    assert seen["src"].endswith(".tmp")          # written aside, then renamed
    assert not seen["dst"].endswith(".tmp")
    assert get_summary(str(tmp_path), "k", False) == "spoken"


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    def _boom(src, dst):
        raise OSError("nope")

    monkeypatch.setattr(cache_module.os, "replace", _boom)
    put(str(tmp_path), "k", "markdown")

    directory = tmp_path / ".changelog"
    leftovers = [n for n in os.listdir(directory)] if directory.exists() else []
    assert not [n for n in leftovers if n.endswith(".tmp")]


# ---------------------------------------------------------------------------
# cache.last - the one reader that is not key-gated
# ---------------------------------------------------------------------------

def test_last_is_none_when_nothing_is_cached(tmp_path):
    assert last(str(tmp_path)) is None


def test_last_returns_the_stored_markdown(tmp_path):
    put(str(tmp_path), "k", "## Unreleased\n\n- a thing")

    entry = last(str(tmp_path))

    assert entry.markdown == "## Unreleased\n\n- a thing"
    assert entry.summary(False) is None


def test_last_ignores_the_key_on_purpose(tmp_path):
    """This is the point of it, not a bug: a replay deliberately never computes
    a key, because doing so would mean reading commits, filtering, capping and
    loading voice samples - the entire pipeline a replay exists to skip."""
    put(str(tmp_path), "key-one", "markdown")

    assert get(str(tmp_path), "key-two") is None      # the gated reader misses
    assert last(str(tmp_path)).markdown == "markdown"  # this one does not


def test_last_carries_the_spoken_summary_per_detail(tmp_path):
    put(str(tmp_path), "k", "markdown")
    put_summary(str(tmp_path), "k", False, "the brief one")

    entry = last(str(tmp_path))

    assert entry.summary(False) == "the brief one"
    assert entry.summary(True) is None


def test_last_carries_both_detail_slots(tmp_path):
    put(str(tmp_path), "k", "markdown")
    put_summary(str(tmp_path), "k", False, "brief text")
    put_summary(str(tmp_path), "k", True, "detailed text")

    entry = last(str(tmp_path))

    assert entry.summary(False) == "brief text"
    assert entry.summary(True) == "detailed text"


def test_last_returns_the_newest_entry(tmp_path):
    put(str(tmp_path), "k1", "old markdown")
    put(str(tmp_path), "k2", "new markdown")

    assert last(str(tmp_path)).markdown == "new markdown"


def _write_raw(tmp_path, text):
    path = tmp_path / ".changelog" / ".voicelog-cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_last_is_none_for_a_corrupt_cache(tmp_path):
    _write_raw(tmp_path, "{not json")

    assert last(str(tmp_path)) is None


def test_last_is_none_for_a_non_dict_cache(tmp_path):
    _write_raw(tmp_path, "[]")

    assert last(str(tmp_path)) is None


def test_last_is_none_for_blank_markdown(tmp_path):
    _write_raw(tmp_path, '{"key": "k", "markdown": "   ", "summaries": {}}')

    assert last(str(tmp_path)) is None


def test_last_survives_a_summaries_field_that_is_not_a_dict(tmp_path):
    """There is no key gate to fail first, and --replay's whole promise is that
    it never fails - so the shape has to be checked."""
    _write_raw(tmp_path, '{"key": "k", "markdown": "m", "summaries": "oops"}')

    entry = last(str(tmp_path))

    assert entry.markdown == "m"
    assert entry.summary(False) is None


def test_last_does_not_expose_the_key(tmp_path):
    """Anyone holding a key must use the gated reader instead."""
    put(str(tmp_path), "k", "markdown")

    entry = last(str(tmp_path))

    assert not hasattr(entry, "key")
