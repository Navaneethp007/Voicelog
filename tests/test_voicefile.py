"""Tests for voicelog/voicefile.py — written BEFORE implementation (TDD Red phase)."""
from __future__ import annotations

from pathlib import Path

import pytest

from voicelog.voicefile import update_voice_md


def _voice_path(repo_dir: Path) -> Path:
    return repo_dir / ".changelog" / "voice.md"


# ---------------------------------------------------------------------------
# Case 1 / 2: file does not exist yet
# ---------------------------------------------------------------------------


def test_creates_file_with_marker_and_content(tmp_path):
    new_md = "## Unreleased\n\n### Features\n- Did a cool thing\n"
    changed = update_voice_md(str(tmp_path), new_md, "v1.0.0")

    assert changed is True
    path = _voice_path(tmp_path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert content.startswith("<!-- voicelog:last-tag=v1.0.0 -->\n")
    assert "## Unreleased" in content
    assert "- Did a cool thing" in content


def test_creates_changelog_directory_if_missing(tmp_path):
    assert not (tmp_path / ".changelog").exists()
    update_voice_md(str(tmp_path), "## Unreleased\n\n- x\n", "v1.0.0")
    assert (tmp_path / ".changelog").is_dir()
    assert _voice_path(tmp_path).exists()


def test_first_creation_with_none_tag_marker(tmp_path):
    update_voice_md(str(tmp_path), "## Unreleased\n\n- x\n", None)
    content = _voice_path(tmp_path).read_text(encoding="utf-8")
    assert content.splitlines()[0] == "<!-- voicelog:last-tag=none -->"


# ---------------------------------------------------------------------------
# Case 2: tag unchanged → replace top Unreleased block
# ---------------------------------------------------------------------------


def test_tag_unchanged_replaces_unreleased_block_returns_true(tmp_path):
    update_voice_md(str(tmp_path), "## Unreleased\n\n- old item\n", "v1.0.0")
    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- new item\n", "v1.0.0")

    assert changed is True
    content = _voice_path(tmp_path).read_text(encoding="utf-8")
    assert "- new item" in content
    assert "- old item" not in content
    # marker unchanged
    assert content.splitlines()[0] == "<!-- voicelog:last-tag=v1.0.0 -->"


def test_tag_unchanged_identical_content_returns_false(tmp_path):
    new_md = "## Unreleased\n\n- same item\n"
    update_voice_md(str(tmp_path), new_md, "v1.0.0")
    before = _voice_path(tmp_path).read_text(encoding="utf-8")

    changed = update_voice_md(str(tmp_path), new_md, "v1.0.0")

    assert changed is False
    after = _voice_path(tmp_path).read_text(encoding="utf-8")
    assert after == before


def test_tag_unchanged_preserves_content_below(tmp_path):
    # Build a file that has history below the Unreleased block.
    seed = (
        "<!-- voicelog:last-tag=v1.0.0 -->\n"
        "## Unreleased\n\n"
        "- old unreleased\n\n"
        "## v0.9.0\n\n"
        "- shipped earlier\n"
    )
    path = _voice_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(seed, encoding="utf-8")

    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- brand new\n", "v1.0.0")

    assert changed is True
    content = path.read_text(encoding="utf-8")
    assert "- brand new" in content
    assert "- old unreleased" not in content
    # history preserved
    assert "## v0.9.0" in content
    assert "- shipped earlier" in content


def test_tag_unchanged_subsections_are_not_block_boundaries(tmp_path):
    seed = (
        "<!-- voicelog:last-tag=v1.0.0 -->\n"
        "## Unreleased\n\n"
        "### Features\n"
        "- feat one\n\n"
        "### Fixes\n"
        "- fix one\n\n"
        "## v0.9.0\n\n"
        "- earlier\n"
    )
    path = _voice_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(seed, encoding="utf-8")

    new_md = "## Unreleased\n\n### Features\n- replaced feat\n"
    changed = update_voice_md(str(tmp_path), new_md, "v1.0.0")

    assert changed is True
    content = path.read_text(encoding="utf-8")
    # whole block (including ### subsections) replaced
    assert "- feat one" not in content
    assert "- fix one" not in content
    assert "- replaced feat" in content
    # history below the ## boundary preserved
    assert "## v0.9.0" in content
    assert "- earlier" in content


# ---------------------------------------------------------------------------
# Case 3: tag changed → promote
# ---------------------------------------------------------------------------


def test_tag_changed_promotes_old_unreleased_under_new_tag(tmp_path):
    # The unreleased commits were made AFTER v1.0.0 and ship in v1.1.0, so the
    # promoted heading must be the release they shipped in (current_tag), not the
    # tag that was current when the notes were written.
    seed = (
        "<!-- voicelog:last-tag=v1.0.0 -->\n"
        "## Unreleased\n\n"
        "- the work that shipped in v1.1.0\n"
    )
    path = _voice_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(seed, encoding="utf-8")

    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- fresh work\n", "v1.1.0")

    assert changed is True
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "<!-- voicelog:last-tag=v1.1.0 -->"
    # fresh Unreleased on top
    assert lines[1] == "## Unreleased"
    assert "- fresh work" in content
    # promoted block heading is the NEW release, not the old stored tag
    assert "## v1.1.0" in content
    assert "## v1.0.0" not in content
    assert "- the work that shipped in v1.1.0" in content
    # fresh block appears above the promoted one
    assert content.index("## Unreleased") < content.index("## v1.1.0")
    assert content.index("- fresh work") < content.index("- the work that shipped in v1.1.0")


def test_tag_changed_from_none_promotes_under_first_tag(tmp_path):
    # Notes written with no tags (fallback mode) become the content of the first
    # release once it is cut — promote them under that first tag (current_tag).
    seed = (
        "<!-- voicelog:last-tag=none -->\n"
        "## Unreleased\n\n"
        "- pre-release work\n"
    )
    path = _voice_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(seed, encoding="utf-8")

    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- fresh\n", "v1.0.0")

    assert changed is True
    content = path.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "<!-- voicelog:last-tag=v1.0.0 -->"
    assert "## v1.0.0" in content
    assert "- pre-release work" in content
    assert "- fresh" in content
    assert content.index("- fresh") < content.index("- pre-release work")


def test_tag_removed_uses_archived_fallback(tmp_path):
    # Edge case: a tag existed but is now gone (current_tag is None). With no
    # release to name the block, fall back to an "archived" heading.
    seed = (
        "<!-- voicelog:last-tag=v1.0.0 -->\n"
        "## Unreleased\n\n"
        "- orphaned work\n"
    )
    path = _voice_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(seed, encoding="utf-8")

    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- fresh\n", None)

    assert changed is True
    content = path.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "<!-- voicelog:last-tag=none -->"
    assert "## Unreleased (archived)" in content
    assert "- orphaned work" in content


# ---------------------------------------------------------------------------
# The configured path is honoured
# ---------------------------------------------------------------------------

def test_rel_path_overrides_the_default_location(tmp_path):
    """`voice_md` was loaded and documented but never read - the path was
    hardcoded, so setting it did nothing."""
    changed = update_voice_md(str(tmp_path), "## Unreleased\n\n- a thing\n", "v1.0.0",
                              rel_path="docs/NOTES.md")

    assert changed is True
    content = (tmp_path / "docs" / "NOTES.md").read_text(encoding="utf-8")
    assert "- a thing" in content
    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_a_blank_rel_path_falls_back_to_the_default(tmp_path):
    update_voice_md(str(tmp_path), "## Unreleased\n\n- a thing\n", None, rel_path="")

    assert (tmp_path / ".changelog" / "voice.md").exists()


def test_the_default_location_is_unchanged(tmp_path):
    """19 existing call sites pass three positional arguments; they must work."""
    update_voice_md(str(tmp_path), "## Unreleased\n\n- a thing\n", None)

    assert (tmp_path / ".changelog" / "voice.md").exists()


# ---------------------------------------------------------------------------
# voice.md is the one artifact that cannot be regenerated
# ---------------------------------------------------------------------------

def test_a_failed_write_does_not_destroy_the_existing_changelog(tmp_path, monkeypatch):
    """open(path, "w") truncates before writing, so an interrupt or a full disk
    left voice.md empty. The cache and the state file - both explicitly
    disposable - were the ones written atomically; this one was not."""
    from voicelog import fileio
    path = tmp_path / ".changelog" / "voice.md"
    path.parent.mkdir(parents=True)
    original = chr(10).join([
        "<!-- voicelog:last-tag=v1.0.0 -->", "## Unreleased", "", "- months of history", ""
    ])
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(fileio.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        update_voice_md(str(tmp_path), chr(10).join(["## Unreleased", "", "- a new thing", ""]),
                        "v1.0.0")

    assert path.read_text(encoding="utf-8") == original


def test_undecodable_history_is_not_rewritten_as_replacement_chars(tmp_path):
    """errors="replace" is right for input you only read and wrong for a
    read-modify-write: decoding then writing the result back turns every
    undecodable byte into U+FFFD *permanently*, in the one file this package
    calls unrecoverable. Refusing leaves it exactly as it was."""
    path = tmp_path / ".changelog" / "voice.md"
    path.parent.mkdir(parents=True)
    raw = (("<!-- voicelog:last-tag=v1 -->" + chr(10) + "## Unreleased" + chr(10) * 2
            + "- old" + chr(10) * 2 + "## v0.9" + chr(10) * 2 + "- caf").encode("utf-8")
           + bytes([0xE9]) + (" notes" + chr(10)).encode("utf-8"))
    path.write_bytes(raw)

    with pytest.raises(UnicodeDecodeError):
        update_voice_md(str(tmp_path), "## Unreleased" + chr(10) * 2 + "- new" + chr(10),
                        "v1")

    assert path.read_bytes() == raw


def test_valid_non_ascii_history_is_preserved(tmp_path):
    """Refusing on undecodable bytes must not refuse ordinary UTF-8."""
    path = tmp_path / ".changelog" / "voice.md"
    path.parent.mkdir(parents=True)
    path.write_text("<!-- voicelog:last-tag=v1 -->" + chr(10) + "## Unreleased" + chr(10) * 2
                    + "- old" + chr(10) * 2 + "## v0.9" + chr(10) * 2 + "- café ☕" + chr(10),
                    encoding="utf-8")

    update_voice_md(str(tmp_path), "## Unreleased" + chr(10) * 2 + "- new" + chr(10), "v1")

    assert "café ☕" in path.read_text(encoding="utf-8")
