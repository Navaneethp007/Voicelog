"""Tests for voicelog.cache — generation cache keyed on the commit set."""
from __future__ import annotations

from voicelog.cache import cache_key, get, put
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


def test_cache_file_lives_under_changelog_dir(tmp_path):
    put(str(tmp_path), "k", "v")
    assert (tmp_path / ".changelog" / ".voicelog-cache.json").exists()
