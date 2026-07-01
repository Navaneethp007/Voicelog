"""Tests for voicelog.filters.drop_noise — written BEFORE implementation (TDD RED phase)."""
import pytest

from voicelog.models import Commit
from voicelog.filters import drop_noise


def make_commit(subject: str) -> Commit:
    return Commit(hash="abc123", subject=subject, body="", author="Test User", files=[])


class TestDropNoise:
    def test_matching_commit_is_dropped(self):
        """A commit whose subject matches a pattern is removed."""
        commits = [make_commit("wip: half-done feature")]
        result = drop_noise(commits, [r"^wip"])
        assert result == []

    def test_non_matching_commit_is_kept(self):
        """A commit whose subject matches no pattern is kept."""
        commits = [make_commit("feat: add login page")]
        result = drop_noise(commits, [r"^wip"])
        assert result == [make_commit("feat: add login page")]

    def test_empty_patterns_returns_all_commits(self):
        """When patterns list is empty, all commits are returned unchanged."""
        commits = [make_commit("wip: something"), make_commit("feat: real work")]
        result = drop_noise(commits, [])
        assert result == commits

    def test_case_insensitive_matching(self):
        """Matching is case-insensitive (e.g. 'WIP: foo' matches pattern '^wip')."""
        commits = [make_commit("WIP: uppercase subject")]
        result = drop_noise(commits, [r"^wip"])
        assert result == []

    def test_commit_matching_any_pattern_is_dropped(self):
        """A commit matching any one of multiple patterns is dropped."""
        commits = [
            make_commit("wip: partial"),
            make_commit("Merge branch 'main'"),
            make_commit("feat: real feature"),
        ]
        result = drop_noise(commits, [r"^wip", r"^merge"])
        assert result == [make_commit("feat: real feature")]

    def test_empty_commit_list_returns_empty(self):
        """An empty commit list produces an empty result regardless of patterns."""
        result = drop_noise([], [r"^wip", r"^merge"])
        assert result == []
