"""Keeping API keys out of text that gets printed.

Deliberately its own module with no imports: `llm`, `tts` and `providers` all
need it, and `tts` is otherwise a leaf that imports nothing from the package.
Two copies of a two-line function would have been tolerable; a third is where a
module becomes cheaper than the duplication.
"""
from __future__ import annotations


def redact(text: str, *secrets: str | None) -> str:
    """Blank out secrets a provider may have echoed back to us.

    Some gateways quote the offending request - Authorization header included -
    in their 401 bodies, and those bodies end up in error messages, terminals
    and CI logs.

    **Always call this before truncating.** Truncating first can cut a key in
    half, leaving a prefix that no later replace() can find; that was a real bug
    in this codebase. Blank and missing secrets are ignored, so callers can pass
    an optional key straight through without guarding it.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# What a provider's error body is worth quoting. Long enough to carry a real
# message, short enough not to paste an HTML login page into a terminal.
DEFAULT_LIMIT = 200


def detail(text: str, *secrets: str | None, limit: int = DEFAULT_LIMIT) -> str:
    """A provider's message, safe to print: redacted, then truncated.

    Both steps, in this order, in one place. Four call sites did them by hand
    with three different limits and three copies of the comment explaining why
    the order matters - which is three chances to get it wrong, and the
    truncate-first bug had already happened once.
    """
    out = redact(text, *secrets)
    return out if len(out) <= limit else out[:limit] + "..."
