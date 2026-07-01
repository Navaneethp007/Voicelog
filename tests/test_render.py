"""Tests for voicelog/render.py — written BEFORE implementation (TDD Red phase)."""
from __future__ import annotations

import pytest

from voicelog.models import Commit
from voicelog.render import render, render_fallback


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_render_prepends_unreleased_header():
    result = render("Some content here.")
    assert result.startswith("## Unreleased\n\n")


def test_render_strips_single_line_think_block():
    markdown = "<think>internal reasoning</think>Actual changelog content."
    result = render(markdown)
    assert "<think>" not in result
    assert "internal reasoning" not in result
    assert "Actual changelog content." in result


def test_render_drops_empty_section_heading():
    """A heading with no content before the next heading is removed."""
    markdown = (
        "## What's new?\n\n"
        "## Features\n"
        "- Added a thing\n"
    )
    result = render(markdown)
    assert "What's new?" not in result
    assert "Features" in result
    assert "Added a thing" in result


def test_render_keeps_section_with_content():
    markdown = "## Features\n- Real item\n"
    result = render(markdown)
    assert "Features" in result
    assert "Real item" in result


def test_render_drops_trailing_empty_section():
    """An empty heading at the very end (no content after it) is removed."""
    markdown = "## Features\n- Item\n\n## Notes\n\n"
    result = render(markdown)
    assert "Notes" not in result
    assert "Features" in result
    assert "Item" in result


def test_render_strips_multiline_think_block():
    markdown = (
        "<think>\n"
        "line one of reasoning\n"
        "line two of reasoning\n"
        "</think>\n"
        "Real content after the block."
    )
    result = render(markdown)
    assert "<think>" not in result
    assert "line one of reasoning" not in result
    assert "Real content after the block." in result


def test_render_trims_surrounding_whitespace():
    markdown = "   \n  Some content.  \n  "
    result = render(markdown)
    # After the header the content should not start with blank lines
    body = result[len("## Unreleased\n\n"):]
    assert body == body.strip()


def test_render_passes_through_content_without_think_block():
    markdown = "### Added\n- Feature A\n- Feature B"
    result = render(markdown)
    assert "### Added" in result
    assert "- Feature A" in result
    assert "- Feature B" in result


def test_render_downgrades_double_hash_headings_to_triple():
    # Model-emitted ## headings should become ### so they nest under ## Unreleased
    markdown = "## Added\n- thing\n## Fixed\n- bug"
    result = render(markdown)
    assert "### Added" in result
    assert "### Fixed" in result
    # The prepended header must still be ## Unreleased (not ### Unreleased)
    assert result.startswith("## Unreleased")


def test_render_leaves_triple_hash_headings_alone():
    markdown = "### Added\n- thing\n### Fixed\n- bug"
    result = render(markdown)
    assert "### Added" in result
    assert "### Fixed" in result
    # No quadruple-hash headings should appear
    assert "####" not in result


# ---------------------------------------------------------------------------
# render_fallback()
# ---------------------------------------------------------------------------


def test_render_fallback_starts_with_unreleased_header():
    commits = [Commit(hash="abc", subject="Add feature", body="", author="A", files=[])]
    result = render_fallback(commits)
    assert result.startswith("## Unreleased\n\n")


def test_render_fallback_includes_all_commit_subjects():
    commits = [
        Commit(hash="a1b", subject="Fix crash", body="", author="A", files=[]),
        Commit(hash="c2d", subject="Add login page", body="", author="B", files=[]),
        Commit(hash="e3f", subject="Refactor auth", body="", author="C", files=[]),
    ]
    result = render_fallback(commits)
    assert "Fix crash" in result
    assert "Add login page" in result
    assert "Refactor auth" in result
    # Each subject should appear as a bullet
    for commit in commits:
        assert f"- {commit.subject}" in result


def test_render_fallback_empty_commits_returns_just_header():
    result = render_fallback([])
    assert result.strip() == "## Unreleased"
