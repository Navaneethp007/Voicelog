"""Tests for voicelog.voice.load_voice — written BEFORE implementation (TDD)."""
import pytest
from voicelog.voice import load_voice


# 1. Missing directory → returns ""
def test_missing_directory_returns_empty_string(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    assert load_voice(missing) == ""


# 2. Empty directory (no .md/.txt files) → returns ""
def test_empty_directory_returns_empty_string(tmp_path):
    assert load_voice(str(tmp_path)) == ""


# 3. One .md file → returned text contains the file's content
def test_single_md_file_content_present(tmp_path):
    (tmp_path / "changelog.md").write_text("My markdown changelog")
    result = load_voice(str(tmp_path))
    assert "My markdown changelog" in result


# 4. One .txt file → returned text contains the file's content
def test_single_txt_file_content_present(tmp_path):
    (tmp_path / "notes.txt").write_text("My text notes")
    result = load_voice(str(tmp_path))
    assert "My text notes" in result


# 5. Three files → all three included, in sorted order
def test_three_files_all_included_in_sorted_order(tmp_path):
    (tmp_path / "a_first.md").write_text("Content A")
    (tmp_path / "b_second.md").write_text("Content B")
    (tmp_path / "c_third.md").write_text("Content C")
    result = load_voice(str(tmp_path))
    assert "Content A" in result
    assert "Content B" in result
    assert "Content C" in result
    # sorted order: A before B before C
    assert result.index("Content A") < result.index("Content B") < result.index("Content C")


# 6. Four files → only first three (sorted) are included
def test_four_files_only_first_three_included(tmp_path):
    (tmp_path / "a.md").write_text("Alpha")
    (tmp_path / "b.md").write_text("Beta")
    (tmp_path / "c.md").write_text("Gamma")
    (tmp_path / "d.md").write_text("Delta")
    result = load_voice(str(tmp_path))
    assert "Alpha" in result
    assert "Beta" in result
    assert "Gamma" in result
    assert "Delta" not in result


# 7. Non-.md/.txt files are ignored
def test_non_md_txt_files_are_ignored(tmp_path):
    (tmp_path / "data.json").write_text('{"key": "value"}')
    (tmp_path / "script.py").write_text("print('hello')")
    result = load_voice(str(tmp_path))
    assert result == ""


# 8. Framing text appears in result when files are present
def test_framing_text_appears_when_files_present(tmp_path):
    (tmp_path / "sample.md").write_text("Some changelog")
    result = load_voice(str(tmp_path))
    assert "Here are past changelogs" in result
