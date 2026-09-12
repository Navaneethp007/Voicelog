"""Tests for voicelog.textstyle - markdown to terminal styling.

The suite never has a real tty, so the enabled path is driven through the
explicit `enable=` parameter. That is also the contract that matters: when
styling is OFF the function is the identity, so piped output stays exactly the
markdown it always was.
"""
from __future__ import annotations

import pytest

from voicelog.textstyle import style, supports_style

BOLD = "\033[1m"
DIM = "\033[2m"
HEAD = "\033[1;4m"
RESET = "\033[0m"


def _styled(text):
    return style(text, enable=True)


# ---------------------------------------------------------------------------
# The identity contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "## Unreleased\n\n### Features\n- **bold** and `code`",
    "# Project Overview\n\nplain",
    "",
    "no markup at all",
])
def test_disabled_is_the_identity(text):
    """Piped output must be byte-identical to today's markdown - it is what
    voice.md holds and what `voicelog > notes.md` produces."""
    assert style(text, enable=False) is text


# ---------------------------------------------------------------------------
# What it does style
# ---------------------------------------------------------------------------

def test_bold_becomes_bold():
    assert _styled("a **word** b") == f"a {BOLD}word{RESET} b"


def test_inline_code_becomes_dim():
    assert _styled("run `voicelog --new` now") == f"run {DIM}voicelog --new{RESET} now"


def test_h1_and_h2_get_bold_underline():
    assert _styled("# Title") == f"{HEAD}Title{RESET}"
    assert _styled("## Unreleased") == f"{HEAD}Unreleased{RESET}"


def test_h3_and_deeper_get_bold_only():
    assert _styled("### Features") == f"{BOLD}Features{RESET}"
    assert _styled("###### Deep") == f"{BOLD}Deep{RESET}"


def test_heading_markers_inside_the_text_are_stripped_not_restyled():
    """A nested RESET would close the heading's bold for the rest of the line."""
    result = _styled("### The `--new` flag and **bold**")

    assert result == f"{BOLD}The --new flag and bold{RESET}"
    assert result.count(RESET) == 1


def test_multiple_constructs_on_one_line():
    assert _styled("**a** and `b`") == f"{BOLD}a{RESET} and {DIM}b{RESET}"


# ---------------------------------------------------------------------------
# What it deliberately leaves alone - each of these would be a false positive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, why", [
    ("touch __init__.py and __pycache__", "dunder names, not bold"),
    ("set max_chars and voice_md", "underscores in identifiers, not italics"),
    ("* a bullet item", "list marker, not italics"),
    ("2 * 3 = 6", "arithmetic, not italics"),
    ("*emphasis* stays literal", "italics are ambiguous with bullets"),
    ("[![badge](a.svg)](b)", "stripping a link would lose the URL"),
    ("| a | b |", "table row"),
    ("> quoted", "blockquote"),
    ("<div>html</div>", "raw html"),
    ("Title\n=====", "setext heading"),
    ("a ** stray marker", "unmatched ** stays literal rather than corrupting text"),
    ("emoji line \U0001F389 stays", "emoji pass through"),
    ("#hashtag not a heading", "a heading needs a space after the hashes"),
])
def test_left_alone(text, why):
    result = _styled(text)

    assert result == text, why
    assert "\033" not in result, why


def test_bold_inside_a_code_span_is_not_styled():
    """Code spans are matched first and bold only runs in the gaps."""
    result = _styled("literal `**not bold**` here")

    assert result == f"literal {DIM}**not bold**{RESET} here"


def test_fenced_block_contents_are_verbatim():
    """The --new fallback embeds a whole README, which is full of fences."""
    text = "before\n```python\nx = **1**  # `not code`\n```\nafter"

    result = _styled(text)

    assert "x = **1**  # `not code`" in result
    assert "```python" in result


def test_a_heading_inside_a_fence_is_not_a_heading():
    text = "```\n## not a heading\n```"

    assert _styled(text) == text


def test_tilde_fences_count_too():
    text = "~~~\n**verbatim**\n~~~"

    assert _styled(text) == text


# ---------------------------------------------------------------------------
# supports_style
# ---------------------------------------------------------------------------

class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _plain_env(monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)


def test_not_a_tty_means_no_styling(monkeypatch):
    _plain_env(monkeypatch)
    assert supports_style(_Stream(tty=False)) is False


def test_no_color_disables_even_on_a_tty(monkeypatch):
    _plain_env(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_style(_Stream(tty=True)) is False


def test_empty_no_color_does_not_disable(monkeypatch):
    """no-color.org: the variable must be non-empty to count."""
    _plain_env(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setattr("voicelog.textstyle._enable_windows_vt", lambda: True)
    assert supports_style(_Stream(tty=True)) is True


def test_force_color_beats_no_color_and_a_pipe(monkeypatch):
    _plain_env(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert supports_style(_Stream(tty=False)) is True


def test_dumb_terminal_disables(monkeypatch):
    _plain_env(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    assert supports_style(_Stream(tty=True)) is False


def test_an_exotic_stream_without_isatty_disables(monkeypatch):
    _plain_env(monkeypatch)

    class Odd:
        pass

    assert supports_style(Odd()) is False


# ---------------------------------------------------------------------------
# Nesting - what real model output actually looks like
# ---------------------------------------------------------------------------

def test_bold_containing_a_code_span_is_styled():
    """Straight from a real run: `- **Repo onboarding with `--new`!**`.
    Styling the gaps between code spans only, this never matched at all."""
    result = _styled("**Repo onboarding with `--new`!**")

    assert "**" not in result
    assert result.startswith(BOLD)
    assert result.endswith(RESET)
    assert "--new" in result
    assert "`" not in result


def test_bold_resumes_after_a_nested_code_span():
    """The code span's RESET would otherwise end the surrounding bold early."""
    result = _styled("**before `mid` after**")

    assert result == f"{BOLD}before {DIM}mid{RESET}{BOLD} after{RESET}"


def test_a_code_span_still_wins_when_it_comes_first():
    """Leftmost construct wins, so ** inside a code span stays literal."""
    result = _styled("literal `**not bold**` here")

    assert result == f"literal {DIM}**not bold**{RESET} here"


def test_bold_and_a_separate_code_span_on_one_line():
    result = _styled("**bold** then `code`")

    assert result == f"{BOLD}bold{RESET} then {DIM}code{RESET}"
