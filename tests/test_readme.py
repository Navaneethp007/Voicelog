"""Tests for voicelog.readme — loads a project's README for --new onboarding."""
from __future__ import annotations

from voicelog.readme import load_readme


def test_missing_dir_returns_empty_string(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_readme(str(missing)) == ""


def test_no_readme_file_returns_empty_string(tmp_path):
    (tmp_path / "not-a-readme.txt").write_text("irrelevant")
    assert load_readme(str(tmp_path)) == ""


def test_readme_md_is_loaded(tmp_path):
    (tmp_path / "README.md").write_text("# My Project\n\nDoes cool things.")
    result = load_readme(str(tmp_path))
    assert "My Project" in result
    assert "Does cool things." in result


def test_readme_md_preferred_over_other_variants(tmp_path):
    (tmp_path / "README.md").write_text("markdown version")
    (tmp_path / "README.rst").write_text("rst version")
    (tmp_path / "README.txt").write_text("txt version")
    result = load_readme(str(tmp_path))
    assert "markdown version" in result
    assert "rst version" not in result


def test_readme_rst_used_when_no_md(tmp_path):
    (tmp_path / "README.rst").write_text("rst content here")
    result = load_readme(str(tmp_path))
    assert "rst content here" in result


def test_readme_plain_file_used_as_last_resort(tmp_path):
    (tmp_path / "README").write_text("plain readme, no extension")
    result = load_readme(str(tmp_path))
    assert "plain readme, no extension" in result


def test_truncates_long_readme(tmp_path):
    long_text = "x" * 10_000
    (tmp_path / "README.md").write_text(long_text)
    result = load_readme(str(tmp_path), max_chars=100)
    assert len(result) < 10_000
    assert "truncated" in result.lower()


def test_short_readme_not_truncated(tmp_path):
    (tmp_path / "README.md").write_text("short and sweet")
    result = load_readme(str(tmp_path), max_chars=4000)
    assert "truncated" not in result.lower()
    assert result == "short and sweet"


def test_result_is_stripped(tmp_path):
    (tmp_path / "README.md").write_text("\n\n  padded content  \n\n")
    result = load_readme(str(tmp_path))
    assert result == "padded content"
