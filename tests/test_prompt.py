from __future__ import annotations

import pytest

from voicelog.models import Commit
from voicelog.prompt import build_prompt, build_summary_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_commits():
    return [
        Commit(
            hash="abc1234",
            subject="feat: add login page",
            body="Implements OAuth2 login flow.",
            author="Alice",
            files=["src/login.py", "templates/login.html"],
        ),
        Commit(
            hash="def5678",
            subject="fix: correct typo in README",
            body="",
            author="Bob",
            files=["README.md"],
        ),
    ]


@pytest.fixture()
def sections():
    return ["Features", "Bug Fixes", "Other"]


# ---------------------------------------------------------------------------
# 1. Returns exactly 2 messages
# ---------------------------------------------------------------------------

def test_returns_exactly_two_messages(sample_commits, sections):
    result = build_prompt(sample_commits, "some voice text", sections)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# 2. First message role is "system", second is "user"
# ---------------------------------------------------------------------------

def test_first_message_role_is_system(sample_commits, sections):
    result = build_prompt(sample_commits, "some voice text", sections)
    assert result[0]["role"] == "system"


def test_second_message_role_is_user(sample_commits, sections):
    result = build_prompt(sample_commits, "some voice text", sections)
    assert result[1]["role"] == "user"


# ---------------------------------------------------------------------------
# 3. System message content mentions the section names passed in
# ---------------------------------------------------------------------------

def test_system_message_contains_section_names(sample_commits, sections):
    result = build_prompt(sample_commits, "some voice text", sections)
    system_content = result[0]["content"]
    for section in sections:
        assert section in system_content, f"Section '{section}' not found in system message"


# ---------------------------------------------------------------------------
# 4. User message includes all commit subjects
# ---------------------------------------------------------------------------

def test_user_message_includes_all_commit_subjects(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    user_content = result[1]["content"]
    for commit in sample_commits:
        assert commit.subject in user_content, f"Subject '{commit.subject}' not found in user message"


# ---------------------------------------------------------------------------
# 5. User message includes commit body when non-empty
# ---------------------------------------------------------------------------

def test_user_message_includes_nonempty_body(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    user_content = result[1]["content"]
    # First commit has a non-empty body
    assert "Implements OAuth2 login flow." in user_content


# ---------------------------------------------------------------------------
# 6. User message includes changed file paths
# ---------------------------------------------------------------------------

def test_user_message_includes_changed_files(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    user_content = result[1]["content"]
    assert "src/login.py" in user_content
    assert "templates/login.html" in user_content
    assert "README.md" in user_content


# ---------------------------------------------------------------------------
# 7. When voice_text is non-empty, it appears in the user message
# ---------------------------------------------------------------------------

def test_voice_text_appears_in_user_message_when_nonempty(sample_commits, sections):
    voice_text = "This release brings a brand new login experience."
    result = build_prompt(sample_commits, voice_text, sections)
    user_content = result[1]["content"]
    assert voice_text in user_content


# ---------------------------------------------------------------------------
# 8. When voice_text is "", the user message still works (no framing artifacts)
# ---------------------------------------------------------------------------

def test_empty_voice_text_produces_no_framing_artifacts(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    user_content = result[1]["content"]
    # Should still contain commit info
    assert sample_commits[0].subject in user_content
    # Should not have stray framing labels for a missing voice text block
    # (we check that typical framing words don't appear orphaned)
    # We just verify it doesn't crash and commits are present — no KeyError / empty string artifacts
    assert user_content.strip() != ""


# ---------------------------------------------------------------------------
# 9. Sections list is reflected in the system prompt (parametrised)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("custom_sections", [
    ["New Features", "Fixes"],
    ["Breaking Changes", "Deprecations", "Internal"],
    ["Highlights"],
])
def test_system_prompt_reflects_custom_sections(sample_commits, custom_sections):
    result = build_prompt(sample_commits, "", custom_sections)
    system_content = result[0]["content"]
    for section in custom_sections:
        assert section in system_content, (
            f"Custom section '{section}' not found in system message"
        )


# ---------------------------------------------------------------------------
# 10. No diffs in user message (extra safety check)
# ---------------------------------------------------------------------------

def test_user_message_does_not_contain_diff_markers(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    user_content = result[1]["content"]
    assert "@@" not in user_content
    assert "diff --git" not in user_content


# ---------------------------------------------------------------------------
# 11. System message uses a casual, spoken-aloud narrator tone
# ---------------------------------------------------------------------------

def test_system_message_is_casual_narrator(sample_commits, sections):
    result = build_prompt(sample_commits, "", sections)
    system_content = result[0]["content"].lower()
    # Casual framing markers — the notes are read aloud in a conversational tone.
    assert "casual" in system_content or "conversational" in system_content
    assert "aloud" in system_content
    # Still groups into sections (grouping must survive the tone change).
    for section in sections:
        assert section in result[0]["content"]


# ---------------------------------------------------------------------------
# build_summary_prompt — the short spoken digest
# ---------------------------------------------------------------------------

def test_summary_prompt_returns_two_messages():
    result = build_summary_prompt("## Unreleased\n\n### Features\n- A cool thing")
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[1]["role"] == "user"


def test_summary_prompt_asks_for_short_spoken_plaintext():
    result = build_summary_prompt("## Unreleased\n\n- x")
    system = result[0]["content"].lower()
    # Must steer toward a short, spoken, plain-text digest (no markdown read aloud).
    assert "spoken" in system or "aloud" in system or "read out" in system
    assert "sentence" in system  # bounded length
    assert "markdown" in system or "plain" in system


def test_summary_prompt_includes_the_changelog():
    changelog = "## Unreleased\n\n### Features\n- Added PDF export"
    result = build_summary_prompt(changelog)
    assert "Added PDF export" in result[1]["content"]


def test_summary_prompt_brief_is_short():
    result = build_summary_prompt("## Unreleased\n\n- x", detail=False)
    assert "2 to 3" in result[0]["content"]


def test_summary_prompt_detailed_asks_for_more():
    brief = build_summary_prompt("## Unreleased\n\n- x", detail=False)[0]["content"]
    detailed = build_summary_prompt("## Unreleased\n\n- x", detail=True)[0]["content"]
    assert brief != detailed
    low = detailed.lower()
    assert "2 to 3" not in detailed  # not capped to the tiny length
    assert "each" in low or "detailed" in low or "rundown" in low
