"""Tests for voicelog.redact - keeping secrets out of error text."""
from __future__ import annotations

from voicelog import redact as redact_module
from voicelog.redact import redact


def test_replaces_a_secret_wherever_it_appears():
    assert redact("key=nvapi-x and again nvapi-x", "nvapi-x") == "key=*** and again ***"


def test_replaces_several_secrets():
    text = "llm nvapi-a speech el-b"
    assert redact(text, "nvapi-a", "el-b") == "llm *** speech ***"


def test_ignores_blank_and_missing_secrets():
    assert redact("nothing to hide", None, "", "absent") == "nothing to hide"


def test_leaves_no_fragment_of_the_secret_behind():
    secret = "nvapi-0123456789abcdef"
    assert secret not in redact(f"rejected: {secret}", secret)
    assert "0123456789" not in redact(f"rejected: {secret}", secret)


def test_returns_text_unchanged_when_no_secrets_are_given():
    assert redact("plain") == "plain"


# ---------------------------------------------------------------------------
# detail(): redact-then-truncate, written once
# ---------------------------------------------------------------------------

def test_detail_redacts_before_truncating():
    """The order is the whole point: truncating first can cut a key in half,
    leaving a prefix no later replace() can find. Four call sites each did both
    steps by hand, with three different limits and three copies of the comment
    explaining why the order matters."""
    key = "nvapi-" + "x" * 40
    text = "prefix " + key + " suffix"

    out = redact_module.detail(text, key, limit=20)

    assert key not in out
    assert "nvapi" not in out


def test_detail_truncates_to_the_limit():
    assert len(redact_module.detail("y" * 500, limit=200)) <= 200 + 3


def test_detail_marks_that_it_truncated():
    """A silently cut body reads as a complete one."""
    out = redact_module.detail("y" * 500, limit=50)

    assert out.endswith("...")


def test_detail_leaves_a_short_string_alone():
    assert redact_module.detail("short", limit=200) == "short"


def test_detail_ignores_blank_secrets():
    assert redact_module.detail("body", None, "", limit=200) == "body"
