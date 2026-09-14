"""Tests for voicelog.fileio — the one file-read and file-write boundary.

Written before the implementation. These pin the two decisions the package used
to make ad hoc at seven read sites and three write sites.
"""
from __future__ import annotations

import os

import pytest

from voicelog import fileio


# ---------------------------------------------------------------------------
# read_text: content files must not die on a stray byte
# ---------------------------------------------------------------------------

def test_read_text_returns_the_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")

    assert fileio.read_text(str(path)) == "hello"


def test_read_text_survives_undecodable_bytes(tmp_path):
    """Six of seven read sites used a strict codec, so one stray byte in a
    voice sample or a hand-edited voice.md ended the run with a traceback.
    For content - as opposed to structure - a mangled character is better."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"caf" + bytes([0xE9]) + b" latte")

    out = fileio.read_text(str(path))

    assert out.startswith("caf")
    assert "latte" in out


def test_read_text_keeps_non_ascii_that_is_valid(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("café — ☕".encode("utf-8"))

    assert fileio.read_text(str(path)) == "café — ☕"


def test_read_text_propagates_a_missing_file(tmp_path):
    """Absent is not the same as unreadable; callers already handle OSError."""
    with pytest.raises(OSError):
        fileio.read_text(str(tmp_path / "nope.txt"))


# ---------------------------------------------------------------------------
# atomic_write_text: a write that fails must not destroy what was there
# ---------------------------------------------------------------------------

def test_atomic_write_creates_the_file(tmp_path):
    path = tmp_path / "out.txt"

    fileio.atomic_write_text(str(path), "content")

    assert path.read_text(encoding="utf-8") == "content"


def test_atomic_write_replaces_existing_content(tmp_path):
    path = tmp_path / "out.txt"
    path.write_text("old", encoding="utf-8")

    fileio.atomic_write_text(str(path), "new")

    assert path.read_text(encoding="utf-8") == "new"


def test_a_failed_write_leaves_the_original_intact(tmp_path, monkeypatch):
    """voice.md is the one accumulating, unrecoverable artifact voicelog
    produces, and it was the one writer that truncated its target first."""
    path = tmp_path / "out.txt"
    path.write_text("precious", encoding="utf-8")
    monkeypatch.setattr(fileio.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        fileio.atomic_write_text(str(path), "new content")

    assert path.read_text(encoding="utf-8") == "precious"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    path = tmp_path / "out.txt"
    path.write_text("precious", encoding="utf-8")
    monkeypatch.setattr(fileio.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        fileio.atomic_write_text(str(path), "new content")

    assert os.listdir(tmp_path) == ["out.txt"]


def test_the_temp_name_is_unique_per_call(tmp_path, monkeypatch):
    """All three writers used "<target>.tmp", so two runs in one repo collided:
    each could unlink the other's in-flight temp, and the documented
    last-write-wins guarantee did not hold."""
    path = tmp_path / "out.txt"
    seen = []
    real = fileio.os.replace
    monkeypatch.setattr(fileio.os, "replace",
                        lambda src, dst: seen.append(src) or real(src, dst))

    fileio.atomic_write_text(str(path), "one")
    fileio.atomic_write_text(str(path), "two")

    assert seen[0] != seen[1]
    assert str(path) + ".tmp" not in seen


def test_atomic_write_writes_utf8_with_lf_endings(tmp_path):
    """Every existing writer pinned both; a shared helper must not quietly
    change the bytes on disk."""
    path = tmp_path / "out.txt"

    fileio.atomic_write_text(str(path), "café\nsecond\n")

    assert path.read_bytes() == "café\nsecond\n".encode("utf-8")


def test_atomic_write_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "out.txt"

    fileio.atomic_write_text(str(path), "content")

    assert path.read_text(encoding="utf-8") == "content"


def test_atomic_write_applies_a_mode_when_asked(tmp_path):
    """save_user_config chmods 0600 because the file names env vars; the
    helper must keep that possible without every caller re-doing it."""
    path = tmp_path / "out.txt"

    fileio.atomic_write_text(str(path), "secret-ish", mode=0o600)

    assert path.exists()
    if os.name != "nt":
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_an_existing_files_permissions_are_preserved(tmp_path, monkeypatch):
    """mkstemp creates at 0600 and os.replace carries that inode's mode to the
    destination, so every rewrite quietly tightened the target - voice.md would
    drop from 0644 to owner-only, and a shared repo or a CI step running as
    another user could no longer read it."""
    path = tmp_path / "out.txt"
    path.write_text("old", encoding="utf-8")
    chmods = []
    real = fileio.os.chmod
    monkeypatch.setattr(fileio.os, "chmod",
                        lambda p, m: chmods.append(m) or real(p, m))

    fileio.atomic_write_text(str(path), "new")

    assert chmods, "no mode applied; the temp file's 0600 would survive"
    if os.name != "nt":
        assert chmods[-1] & 0o044        # still group/other readable


def test_a_new_file_is_not_owner_only(tmp_path, monkeypatch):
    """A file that did not exist gets the process default, not mkstemp's 0600."""
    path = tmp_path / "fresh.txt"
    chmods = []
    real = fileio.os.chmod
    monkeypatch.setattr(fileio.os, "chmod",
                        lambda p, m: chmods.append(m) or real(p, m))

    fileio.atomic_write_text(str(path), "new")

    assert chmods
    if os.name != "nt":
        assert chmods[-1] & 0o044


def test_an_explicit_mode_still_wins(tmp_path):
    """save_user_config asks for 0600 deliberately; that must not be widened."""
    path = tmp_path / "secretish.txt"
    path.write_text("old", encoding="utf-8")

    fileio.atomic_write_text(str(path), "new", mode=0o600)

    if os.name != "nt":
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_read_only_destination_leaves_no_scratch_file(tmp_path):
    """The mode was applied to the temp *before* the rename, so a read-only
    destination made the temp read-only too - the rename failed, and on Windows
    the cleanup unlink then failed as well because Windows will not delete a
    read-only file. Every attempt leaked a scratch file next to the target,
    holding the full changelog text, in the work tree, forever: the failure
    also holds the watermark back, so the same write is retried on every run."""
    import stat as _stat

    target = tmp_path / "voice.md"
    target.write_text("original", encoding="utf-8")
    os.chmod(target, _stat.S_IREAD)
    try:
        for i in range(3):
            try:
                fileio.atomic_write_text(str(target), f"attempt {i}")
            except OSError:
                pass

        leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".voicelog-")]
        assert leftovers == [], leftovers
        assert target.read_text(encoding="utf-8") == "original"
    finally:
        os.chmod(target, _stat.S_IWRITE)


def test_the_destination_mode_is_still_applied(tmp_path, monkeypatch):
    """Moving the chmod after the rename must not stop it happening."""
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    chmods = []
    real = fileio.os.chmod
    monkeypatch.setattr(fileio.os, "chmod",
                        lambda p, m: chmods.append((p, m)) or real(p, m))

    fileio.atomic_write_text(str(target), "new")

    assert chmods and chmods[-1][0] == str(target)   # applied to the destination


def test_a_write_never_widens_before_it_lands(tmp_path, monkeypatch):
    """mkstemp creates at 0600, the tightest mode we ever apply, so between the
    rename and the chmod the file is at its most restrictive - never briefly
    wider than intended."""
    target = tmp_path / "secret.txt"
    order = []
    real_chmod = fileio.os.chmod
    real_replace = fileio.os.replace
    monkeypatch.setattr(fileio.os, "chmod",
                        lambda p, m: order.append("chmod") or real_chmod(p, m))
    monkeypatch.setattr(fileio.os, "replace",
                        lambda s, d: order.append("replace") or real_replace(s, d))

    fileio.atomic_write_text(str(target), "x", mode=0o600)

    assert order == ["replace", "chmod"]
