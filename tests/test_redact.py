"""Tests for voicelog.redact - keeping secrets out of error text."""
from __future__ import annotations

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
