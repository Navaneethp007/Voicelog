"""Tests for voicelog.gitsource — written BEFORE implementation (TDD RED phase).

All tests use real throwaway git repos via tmp_path + monkeypatch.chdir.
No mocking of git.
"""
import os
import subprocess

import pytest

from voicelog.gitsource import NotAGitRepo, NoCommitsFound, GitResult, read_commits
from voicelog.models import Commit


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_repo(tmp_path):
    """Create a minimal git repo; return (repo_path, run_fn)."""
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
    return repo, run


def add_commit(run, repo, filename="file.txt", content="data", message="chore: add file"):
    """Write a file and commit it."""
    filepath = repo / filename
    filepath.write_text(content)
    run("git", "add", filename)
    run("git", "commit", "-m", message)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotAGitRepo:
    def test_raises_when_not_inside_a_git_repo(self, tmp_path, monkeypatch):
        """read_commits raises NotAGitRepo when cwd is not a git repository."""
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        monkeypatch.chdir(plain_dir)

        with pytest.raises(NotAGitRepo):
            read_commits()


class TestEmptyCommits:
    def test_filaless_empty_commits_are_parsed(self, tmp_path, monkeypatch):
        """Commits with no file changes (git commit --allow-empty) still parse."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: first")  # baseline with a file
        run("git", "tag", "v0.1.0")
        run("git", "commit", "--allow-empty", "-m", "feat: empty one")
        run("git", "commit", "--allow-empty", "-m", "fix: another empty")
        monkeypatch.chdir(repo)

        result = read_commits()

        subjects = [c.subject for c in result.commits]
        assert "feat: empty one" in subjects
        assert "fix: another empty" in subjects
        assert len(result.commits) == 2

    def test_mixed_empty_and_file_commits(self, tmp_path, monkeypatch):
        """A mix of file-changing and empty commits all parse, files intact."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: base")
        run("git", "tag", "v0.1.0")
        run("git", "commit", "--allow-empty", "-m", "chore: empty")
        add_commit(run, repo, filename="new.txt", message="feat: with file")
        monkeypatch.chdir(repo)

        result = read_commits()

        by_subject = {c.subject: c for c in result.commits}
        assert "chore: empty" in by_subject
        assert by_subject["chore: empty"].files == []
        assert "new.txt" in by_subject["feat: with file"].files


class TestTagExists:
    def test_commits_after_tag_are_returned(self, tmp_path, monkeypatch):
        """When a tag exists, only commits after that tag are returned."""
        repo, run = make_repo(tmp_path)

        # Commit before the tag
        add_commit(run, repo, "before.txt", "v0", "chore: before tag")
        run("git", "tag", "v0.1.0")

        # Two commits after the tag
        add_commit(run, repo, "after1.txt", "a1", "feat: after tag 1")
        add_commit(run, repo, "after2.txt", "a2", "feat: after tag 2")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert isinstance(result, GitResult)
        assert result.used_fallback is False
        assert len(result.commits) == 2

    def test_used_fallback_is_false_when_tag_exists(self, tmp_path, monkeypatch):
        """used_fallback is False when a tag is found."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "a.txt", "x", "chore: initial")
        run("git", "tag", "v1.0.0")
        add_commit(run, repo, "b.txt", "y", "feat: post-tag")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert result.used_fallback is False


class TestNoTags:
    def test_returns_last_n_commits_when_no_tags(self, tmp_path, monkeypatch):
        """When no tags exist, returns last fallback_commits commits with used_fallback=True."""
        repo, run = make_repo(tmp_path)

        # Create 5 commits
        for i in range(5):
            add_commit(run, repo, f"f{i}.txt", str(i), f"chore: commit {i}")

        monkeypatch.chdir(repo)
        result = read_commits(fallback_commits=3)

        assert result.used_fallback is True
        # Should get at most 3 commits (fallback_commits)
        assert len(result.commits) <= 3

    def test_used_fallback_is_true_when_no_tags(self, tmp_path, monkeypatch):
        """used_fallback is True when there are no tags in the repo."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "x.txt", "x", "chore: only commit")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert result.used_fallback is True


class TestNoCommitsSinceTag:
    def test_empty_commits_list_when_nothing_after_tag(self, tmp_path, monkeypatch):
        """When there are no commits after the latest tag, commits list is empty."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: initial")
        run("git", "tag", "v1.0.0")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert result.commits == []
        assert result.used_fallback is False


class TestCommitSubject:
    def test_commit_subject_matches_message(self, tmp_path, monkeypatch):
        """Commit.subject matches the commit message used when committing."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: baseline")
        run("git", "tag", "v0.0.1")

        expected_subject = "feat: the real deal"
        add_commit(run, repo, "real.txt", "r", expected_subject)

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert result.commits[0].subject == expected_subject


class TestChangedFilesCaptured:
    def test_files_list_contains_committed_filename(self, tmp_path, monkeypatch):
        """Commit.files contains the name of the file that was changed in the commit."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "baseline.txt", "b", "chore: baseline")
        run("git", "tag", "v0.0.1")

        add_commit(run, repo, "special.txt", "s", "feat: add special file")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert "special.txt" in result.commits[0].files

    def test_multiple_files_in_single_commit(self, tmp_path, monkeypatch):
        """Commit.files contains all files changed in a multi-file commit."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: init")
        run("git", "tag", "v0.0.1")

        # Commit two files at once
        (repo / "alpha.txt").write_text("a")
        (repo / "beta.txt").write_text("b")
        run("git", "add", "alpha.txt", "beta.txt")
        run("git", "commit", "-m", "feat: add alpha and beta")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert "alpha.txt" in result.commits[0].files
        assert "beta.txt" in result.commits[0].files


class TestCommitFields:
    def test_commit_hash_is_populated(self, tmp_path, monkeypatch):
        """Commit.hash is a non-empty string."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: init")
        run("git", "tag", "v0.0.1")
        add_commit(run, repo, "next.txt", "n", "feat: next")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert len(result.commits[0].hash) == 40  # full SHA

    def test_commit_author_is_populated(self, tmp_path, monkeypatch):
        """Commit.author matches the configured git author name."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: init")
        run("git", "tag", "v0.0.1")
        add_commit(run, repo, "next.txt", "n", "feat: next")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert result.commits[0].author == "Test"
