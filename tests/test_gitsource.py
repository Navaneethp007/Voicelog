"""Tests for voicelog.gitsource — written BEFORE implementation (TDD RED phase).

All tests use real throwaway git repos via tmp_path + monkeypatch.chdir.
No mocking of git.
"""
import os
import subprocess

import pytest

from voicelog.gitsource import (
    NotAGitRepo,
    NoCommitsFound,
    RefNotFound,
    GitResult,
    read_commits,
    read_recent_commits,
    detect_base_branch,
)
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


class TestSinceRef:
    def test_since_returns_commits_after_ref(self, tmp_path, monkeypatch):
        """read_commits(since=REF) returns only commits in REF..HEAD."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, filename="a.txt", message="feat: first")
        # capture this point as the 'since' ref
        rev = run("git", "rev-parse", "HEAD").stdout.strip()
        add_commit(run, repo, filename="b.txt", message="feat: second")
        add_commit(run, repo, filename="c.txt", message="fix: third")
        monkeypatch.chdir(repo)

        result = read_commits(since=rev)

        subjects = [c.subject for c in result.commits]
        assert subjects == ["fix: third", "feat: second"]  # newest first, first excluded
        assert result.used_fallback is False

    def test_since_ignores_tags(self, tmp_path, monkeypatch):
        """since range is independent of the last tag."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, filename="a.txt", message="feat: base")
        run("git", "tag", "v9.9.9")
        rev = run("git", "rev-parse", "HEAD").stdout.strip()
        add_commit(run, repo, filename="b.txt", message="feat: after ref")
        monkeypatch.chdir(repo)

        result = read_commits(since=rev)

        assert [c.subject for c in result.commits] == ["feat: after ref"]

    def test_since_invalid_ref_raises(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        monkeypatch.chdir(repo)

        with pytest.raises(RefNotFound):
            read_commits(since="no-such-ref-xyz")

    def test_since_no_new_commits_returns_empty(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        rev = run("git", "rev-parse", "HEAD").stdout.strip()
        monkeypatch.chdir(repo)

        result = read_commits(since=rev)  # nothing after HEAD

        assert result.commits == []


class TestDetectBaseBranch:
    def test_detects_main_when_present(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: base")
        run("git", "branch", "-M", "main")
        run("git", "checkout", "-q", "-b", "feature")
        add_commit(run, repo, filename="f.txt", message="feat: work")
        monkeypatch.chdir(repo)

        assert detect_base_branch() == "main"

    def test_detects_master_when_that_is_the_trunk(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: base")
        run("git", "branch", "-M", "master")
        run("git", "checkout", "-q", "-b", "feature")
        monkeypatch.chdir(repo)

        assert detect_base_branch() == "master"

    def test_returns_none_when_no_conventional_trunk(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: base")
        run("git", "branch", "-M", "trunk")  # neither main nor master
        monkeypatch.chdir(repo)

        assert detect_base_branch() is None


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


class TestDiffstat:
    def test_insertions_counted_for_new_file(self, tmp_path, monkeypatch):
        """A commit adding a 3-line file reports insertions=3, deletions=0."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: baseline")
        run("git", "tag", "v0.0.1")

        (repo / "new.txt").write_text("line1\nline2\nline3\n")
        run("git", "add", "new.txt")
        run("git", "commit", "-m", "feat: add three lines")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert result.commits[0].insertions == 3
        assert result.commits[0].deletions == 0

    def test_deletions_counted_when_lines_removed(self, tmp_path, monkeypatch):
        """A commit that shrinks a file reports deletions > 0."""
        repo, run = make_repo(tmp_path)
        (repo / "shrink.txt").write_text("a\nb\nc\nd\n")
        run("git", "add", "shrink.txt")
        run("git", "commit", "-m", "chore: baseline")
        run("git", "tag", "v0.0.1")

        (repo / "shrink.txt").write_text("a\n")
        run("git", "add", "shrink.txt")
        run("git", "commit", "-m", "fix: trim file")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert result.commits[0].deletions == 3

    def test_insertions_sum_across_multiple_files(self, tmp_path, monkeypatch):
        """A commit touching two files sums insertions/deletions across both."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: baseline")
        run("git", "tag", "v0.0.1")

        (repo / "alpha.txt").write_text("a1\na2\n")   # 2 lines
        (repo / "beta.txt").write_text("b1\nb2\nb3\n")  # 3 lines
        run("git", "add", "alpha.txt", "beta.txt")
        run("git", "commit", "-m", "feat: add alpha and beta")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert result.commits[0].insertions == 5
        assert result.commits[0].deletions == 0

    def test_empty_commit_has_zero_stats(self, tmp_path, monkeypatch):
        """An --allow-empty commit has insertions=0, deletions=0 (no crash)."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: baseline")
        run("git", "tag", "v0.0.1")
        run("git", "commit", "--allow-empty", "-m", "chore: nothing changed")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert len(result.commits) == 1
        assert result.commits[0].insertions == 0
        assert result.commits[0].deletions == 0

    def test_files_list_still_populated_alongside_stats(self, tmp_path, monkeypatch):
        """Switching to --numstat must not lose the Commit.files list."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "init.txt", "i", "chore: baseline")
        run("git", "tag", "v0.0.1")
        add_commit(run, repo, "tracked.txt", "x\ny\n", "feat: add tracked file")

        monkeypatch.chdir(repo)
        result = read_commits()

        assert "tracked.txt" in result.commits[0].files


class TestWithDiff:
    def test_diff_is_none_when_not_requested(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: baseline")
        run("git", "tag", "v0.0.1")
        add_commit(run, repo, "x.txt", "hi", "feat: x")
        monkeypatch.chdir(repo)

        result = read_commits()

        assert result.diff is None

    def test_diff_contains_added_content(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: baseline")
        run("git", "tag", "v0.0.1")
        (repo / "new.py").write_text("def hello():\n    return 'unique_marker_xyz'\n")
        run("git", "add", "new.py")
        run("git", "commit", "-m", "feat: add hello")
        monkeypatch.chdir(repo)

        result = read_commits(with_diff=True)

        assert result.diff is not None
        assert "unique_marker_xyz" in result.diff
        assert "+" in result.diff  # unified diff marks additions

    def test_diff_since_ref_scopes_to_the_range(self, tmp_path, monkeypatch):
        """The diff only covers the requested range, not the whole repo history."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "old.py", "old content marker", "feat: old stuff")
        rev = run("git", "rev-parse", "HEAD").stdout.strip()
        (repo / "new.py").write_text("new content marker")
        run("git", "add", "new.py")
        run("git", "commit", "-m", "feat: new stuff")
        monkeypatch.chdir(repo)

        result = read_commits(since=rev, with_diff=True)

        assert "new content marker" in result.diff
        assert "old content marker" not in result.diff

    def test_diff_works_on_fallback_range_with_root_commit(self, tmp_path, monkeypatch):
        """with_diff works even when the range includes the repo's very first
        commit (no parent to diff against)."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "root.py", "root content marker", "feat: root commit")
        monkeypatch.chdir(repo)

        result = read_commits(with_diff=True)  # no tags → fallback mode

        assert result.used_fallback is True
        assert "root content marker" in result.diff

    def test_diff_none_when_no_commits_in_range(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        run("git", "tag", "v0.0.1")
        monkeypatch.chdir(repo)

        result = read_commits(with_diff=True)  # nothing since the tag

        assert result.commits == []
        assert result.diff is None


class TestReadRecentCommits:
    """read_recent_commits (used by --new) ignores tags entirely."""

    def test_raises_when_not_a_git_repo(self, tmp_path, monkeypatch):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        monkeypatch.chdir(plain_dir)

        with pytest.raises(NotAGitRepo):
            read_recent_commits()

    def test_returns_commits_even_when_a_tag_covers_everything(self, tmp_path, monkeypatch):
        """Unlike read_commits, a tag on HEAD does not make this return empty."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, "a.txt", "a", "feat: a")
        add_commit(run, repo, "b.txt", "b", "feat: b")
        run("git", "tag", "v1.0.0")  # tag is on HEAD — read_commits() would return []
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=15)

        subjects = [c.subject for c in result.commits]
        assert "feat: a" in subjects
        assert "feat: b" in subjects

    def test_respects_the_n_limit(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        for i in range(5):
            add_commit(run, repo, f"f{i}.txt", str(i), f"feat: commit {i}")
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=3)

        assert len(result.commits) == 3
        # Newest-first: the last 3 commits made.
        subjects = [c.subject for c in result.commits]
        assert subjects == ["feat: commit 4", "feat: commit 3", "feat: commit 2"]

    def test_empty_repo_returns_empty_list(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=15)

        assert result.commits == []

    def test_used_fallback_is_always_false(self, tmp_path, monkeypatch):
        """used_fallback describes tag-based fallback, which is irrelevant here."""
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=15)

        assert result.used_fallback is False

    def test_with_diff_populates_diff(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        (repo / "new.py").write_text("marker_unique_onboard_diff")
        run("git", "add", "new.py")
        run("git", "commit", "-m", "feat: add new file")
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=15, with_diff=True)

        assert result.diff is not None
        assert "marker_unique_onboard_diff" in result.diff

    def test_diff_is_none_by_default(self, tmp_path, monkeypatch):
        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        monkeypatch.chdir(repo)

        result = read_recent_commits(n=15)

        assert result.diff is None


class TestParseNumstatBlock:
    def test_malformed_line_is_skipped_not_raised(self):
        """A line with fewer than 2 tabs (unexpected git output) is skipped
        rather than raising ValueError from an unpacking mismatch."""
        from voicelog.gitsource import _parse_numstat_block

        files, insertions, deletions = _parse_numstat_block("not-a-numstat-line\n3\t1\treal.txt\n")

        assert files == ["real.txt"]
        assert insertions == 3
        assert deletions == 1

    def test_all_malformed_lines_returns_empty(self):
        from voicelog.gitsource import _parse_numstat_block

        files, insertions, deletions = _parse_numstat_block("garbage\nmore garbage\n")

        assert files == []
        assert insertions == 0
        assert deletions == 0


class TestDiffForCommits:
    def test_returns_none_for_empty_list(self, tmp_path, monkeypatch):
        from voicelog.gitsource import diff_for_commits

        repo, run = make_repo(tmp_path)
        add_commit(run, repo, message="feat: only")
        monkeypatch.chdir(repo)

        assert diff_for_commits([]) is None

    def test_matches_the_oldest_commit_in_the_given_list(self, tmp_path, monkeypatch):
        """diff_for_commits scopes to the LAST commit in the given list (the
        list's oldest, since commits are newest-first) — not the repo's actual
        first commit. This lets callers recompute after filtering/capping."""
        from voicelog.gitsource import diff_for_commits, read_recent_commits

        repo, run = make_repo(tmp_path)
        (repo / "old.py").write_text("old_marker_content")
        run("git", "add", "old.py")
        run("git", "commit", "-m", "feat: old")
        (repo / "new.py").write_text("new_marker_content")
        run("git", "add", "new.py")
        run("git", "commit", "-m", "feat: new")
        monkeypatch.chdir(repo)

        all_commits = read_recent_commits(n=15).commits
        # Only the newest commit — diff should be scoped to just that one.
        newest_only = [all_commits[0]]

        diff = diff_for_commits(newest_only)

        assert "new_marker_content" in diff
        assert "old_marker_content" not in diff


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
