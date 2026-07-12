"""Render LLM output or fallback commit list as markdown."""
from __future__ import annotations

import re

from voicelog.models import Commit

_HEADER = "## Unreleased\n\n"

# Matches a <think>...</think> block at the very start of the string (multiline).
_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Remove a leading <think>...</think> block (emitted by reasoning models)."""
    return _THINK_RE.sub("", text)

# Matches ## headings (but NOT ### or more).
_H2_RE = re.compile(r"^## (.+)$", re.MULTILINE)

# Matches any markdown heading line (#, ##, ###, ...).
_HEADING_RE = re.compile(r"^#{1,6}\s")


def _strip_empty_sections(text: str) -> str:
    """Drop heading lines that have no content before the next heading or EOF."""
    lines = text.splitlines()
    keep: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _HEADING_RE.match(line):
            # Look ahead: is there any non-blank, non-heading content before the
            # next heading?
            has_content = False
            j = i + 1
            while j < len(lines) and not _HEADING_RE.match(lines[j]):
                if lines[j].strip():
                    has_content = True
                j += 1
            if not has_content:
                i += 1  # drop this empty heading
                continue
        keep.append(line)
        i += 1
    return "\n".join(keep).strip()


def render(markdown: str) -> str:
    """Strip reasoning block, normalise headings, prepend ## Unreleased header."""
    # Strip leading <think>...</think> block if present.
    text = strip_reasoning(markdown)

    # Trim surrounding whitespace.
    text = text.strip()

    # Downgrade ## headings emitted by the model to ### so they nest under
    # the ## Unreleased header we prepend.
    text = _H2_RE.sub(r"### \1", text)

    # Remove sections the model emitted with no content under them.
    text = _strip_empty_sections(text)

    return _HEADER + text


def render_fallback(commits: list[Commit]) -> str:
    """Return a plain bulleted changelog when the LLM is unavailable."""
    if not commits:
        return "## Unreleased"

    bullets = "\n".join(f"- {c.subject}" for c in commits)
    return _HEADER + bullets


_ONBOARD_HEADER = "# Project Overview\n\n"


def render_onboarding(markdown: str) -> str:
    """Clean model output for the ``--new`` onboarding rundown.

    Distinct top-level header from render()'s ``## Unreleased`` — this isn't a
    changelog, so it gets its own ``# Project Overview`` framing. The model's
    ``## `` section headings (What this is / Recent activity) already nest
    correctly under a single-``#`` wrapper, so — unlike render() — they are
    NOT downgraded.
    """
    text = strip_reasoning(markdown)
    text = text.strip()
    text = _strip_empty_sections(text)
    return _ONBOARD_HEADER + text


def render_onboarding_fallback(readme_text: str, commits: list[Commit]) -> str:
    """Plain fallback for ``--new`` when the LLM is unavailable — the raw
    README plus a bulleted list of recent commit subjects, so onboarding is
    never fully blocked."""
    readme_block = readme_text.strip() if readme_text else "(no README found)"
    if commits:
        commits_block = "\n".join(f"- {c.subject}" for c in commits)
    else:
        commits_block = "(no recent commits)"
    return (
        f"{_ONBOARD_HEADER}## What this is\n\n{readme_block}\n\n"
        f"## Recent activity\n\n{commits_block}"
    )
