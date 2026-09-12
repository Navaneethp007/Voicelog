"""Maintain a persistent casual changelog at ``.changelog/voice.md``.

The file stores a "last-seen tag" marker as its very first line:

    <!-- voicelog:last-tag=<tag> -->

followed by a ``## Unreleased`` block and any older history below it.
"""
from __future__ import annotations

import os
import re

MARKER_RE = re.compile(r"^<!-- voicelog:last-tag=(.*?) -->\s*$")
UNRELEASED_RE = re.compile(r"^## Unreleased\b")
BLOCK_BOUNDARY_RE = re.compile(r"^## ", re.MULTILINE)


DEFAULT_VOICE_MD = os.path.join(".changelog", "voice.md")


def _voice_path(repo_dir: str, rel_path: str | None = None) -> str:
    """Where the persistent changelog lives, honouring the configured path.

    os.path.join lets an absolute ``rel_path`` win outright, which is intended:
    someone who configures an absolute path means it.
    """
    return os.path.join(repo_dir, rel_path or DEFAULT_VOICE_MD)


def _marker_line(tag: str | None) -> str:
    return f"<!-- voicelog:last-tag={tag if tag is not None else 'none'} -->"


def _parse_marker(content: str) -> tuple[str, str]:
    """Split content into (stored_tag, body).

    ``stored_tag`` is the literal string from the marker (e.g. ``"v1.0.0"`` or
    ``"none"``). If no marker is present, returns ``("none", content)`` and
    leaves the body untouched.
    """
    lines = content.splitlines(keepends=True)
    if lines:
        m = MARKER_RE.match(lines[0].rstrip("\n"))
        if m:
            return m.group(1), "".join(lines[1:])
    return "none", content


def _split_top_unreleased(body: str) -> tuple[str, str]:
    """Split ``body`` into (unreleased_block, rest).

    The top Unreleased block runs from the first ``^## Unreleased`` line up to
    (but not including) the next ``^## `` line, or EOF. ``### `` subsections do
    not act as boundaries. If no Unreleased heading exists, returns
    ``("", body)``.
    """
    lines = body.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if UNRELEASED_RE.match(line):
            start = i
            break
    if start is None:
        return "", body

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    block = "".join(lines[start:end])
    rest = "".join(lines[:start]) + "".join(lines[end:])
    return block, rest


def _normalize_block(md: str) -> str:
    """Ensure a block ends with exactly one trailing newline."""
    return md.rstrip("\n") + "\n"


def update_voice_md(
    repo_dir: str,
    new_unreleased_md: str,
    current_tag: str | None,
    *,
    rel_path: str | None = None,
) -> bool:
    """Update the persistent voice changelog.

    Returns True if the file content changed on disk, False if it was already
    identical.
    """
    path = _voice_path(repo_dir, rel_path)
    marker = _marker_line(current_tag)
    fresh = _normalize_block(new_unreleased_md)

    if not os.path.exists(path):
        new_content = f"{marker}\n{fresh}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
        return True

    with open(path, "r", encoding="utf-8") as f:
        existing = f.read()

    stored_tag, body = _parse_marker(existing)
    current_str = current_tag if current_tag is not None else "none"

    if stored_tag == current_str:
        # No new release: replace the top Unreleased block, preserve the rest.
        _, rest = _split_top_unreleased(body)
        new_body = fresh + rest
        new_content = f"{marker}\n{new_body}"
    else:
        # A release happened: promote the old Unreleased block. Those commits were
        # made after ``stored_tag`` and ship in ``current_tag``, so label the block
        # with the release it shipped in — not the tag that was current when the
        # notes were written. Only when no tag exists (a tag was removed) do we
        # fall back to an "archived" heading.
        old_block, rest = _split_top_unreleased(body)
        if current_tag:
            promoted_heading = f"## {current_tag}"
        else:
            promoted_heading = "## Unreleased (archived)"

        if old_block:
            promoted = re.sub(
                r"^## Unreleased\b.*$",
                promoted_heading,
                old_block,
                count=1,
                flags=re.MULTILINE,
            )
            promoted = _normalize_block(promoted)
            new_body = f"{fresh}\n{promoted}"
        else:
            new_body = fresh
        if rest.strip():
            new_body = f"{new_body}\n{rest.lstrip(chr(10))}"
        new_content = f"{marker}\n{new_body}"

    if new_content == existing:
        return False

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_content)
    return True
