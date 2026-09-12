"""Render a little markdown for a terminal, or get out of the way entirely.

The model writes markdown, which is right for `.changelog/voice.md` and wrong
for a screen: nobody wants to read `- **Repo onboarding with \\`--new\\`!**`.

Two rules shape this module:

- **When styling is off, this is the identity function.** Piped output, a
  redirect into a file, `NO_COLOR`, a dumb terminal - all get back exactly the
  markdown they got before, byte for byte. That keeps `voicelog > notes.md`
  honest, and it is why styling may only be applied at a print boundary: the
  same string also reaches `voicefile` (which matches `^## Unreleased` and
  byte-compares against the file) and the spoken-summary prompt.
- **Three constructs, done properly, rather than eight done badly.** Headings,
  inline code and bold. Everything else is left alone, each for a reason
  recorded below, because a false positive in a changelog is worse than an
  unstyled one.

Bold, dim and underline only - no colour. No palette means nothing to negotiate
about 8 vs 256 colours, and no "is grey readable on a light background".
"""
from __future__ import annotations

import os
import re
import sys

BOLD = "\033[1m"
DIM = "\033[2m"
HEADING = "\033[1;4m"  # bold + underline, for h1/h2
RESET = "\033[0m"

# A fence toggles verbatim mode. This single bit of state is what makes the
# --new fallback safe: it embeds the target repo's entire README, which will
# contain code blocks full of asterisks and backticks.
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")

# ATX headings only, and the space after the hashes is required - so "#hashtag"
# is not a heading. A trailing run of hashes is closing syntax, not content.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*#*$")

# One backtick, no newline, not doubled. Matched before bold so that a `**`
# inside a code span survives.
_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

# Bounded to a single line with no interior asterisk, and requiring non-space
# just inside the markers, so "** stray" and "** spaced **" match nothing. An
# unmatched ** is therefore left as literal text: stripping orphans would
# corrupt the sentence. Backticks ARE allowed inside, because real model output
# nests them: "**Repo onboarding with `--new`!**".
_BOLD_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")

# One scan, both constructs, leftmost wins. Scanning for code spans first and
# styling only the gaps between them meant a bold span *containing* a code span
# was never matched as a whole - which is the shape LLM changelogs emit.
_INLINE_RE = re.compile(
    r"(?<!`)`(?P<code>[^`\n]+)`(?!`)"
    r"|\*\*(?=\S)(?P<bold>[^*\n]+?)(?<=\S)\*\*"
)

# Deliberately unhandled, with the reason each would misfire:
#   *italic* / _italic_  - collides with "* " bullets and with identifiers like
#                          max_chars, voice_md, --no-speak
#   __bold__             - collides with __init__.py, __pycache__
#   [text](url), images  - stripping the markers would lose the URL
#   tables, blockquotes, raw HTML, setext headings, strikethrough, task lists
#   4-space indented code - would swallow nested bullets, which changelogs emit
#                           constantly


def _enable_windows_vt() -> bool:
    """Turn on ANSI interpretation for the Windows console, if it is one.

    CPython never sets this itself - PEP 528 gave us UTF-8 console output, not
    escape-sequence handling. Asking the OS beats guessing from WT_SESSION or
    ANSICON, which fail closed on legacy conhost and anywhere those are unset;
    and GetConsoleMode fails for a non-console handle, so this fails closed for
    free on a pipe.
    """
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    STD_OUTPUT_HANDLE = -11
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a real console
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True  # Windows Terminal already has it on
        if not kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        ):
            return False
        # Re-read rather than trust the call: older builds accept it and drop
        # the unknown flag, which would leave us printing literal escapes.
        check = ctypes.c_uint32()
        return bool(
            kernel32.GetConsoleMode(handle, ctypes.byref(check))
            and check.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )
    except (AttributeError, OSError, ValueError):
        return False


def supports_style(stream=None) -> bool:
    """Whether ``stream`` can be expected to render ANSI escapes.

    Deliberately not ``wizard.can_prompt()``: that asks whether it is safe to
    *ask a question*, which depends on stdin and on CI/hook markers. Neither has
    anything to do with whether the thing we are writing to can render bold.
    """
    stream = sys.stdout if stream is None else stream
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):  # detached, closed, or exotic
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return _enable_windows_vt() if sys.platform == "win32" else True


def _code_spans(text: str, resume: str = "") -> str:
    """Dim every code span.

    ``resume`` is re-emitted after each RESET, so a code span nested inside bold
    does not close the bold that surrounds it.
    """
    return _CODE_RE.sub(lambda m: DIM + m.group(1) + RESET + resume, text)


def _inline(text: str) -> str:
    """Style bold and inline code on one line.

    A single left-to-right scan over both constructs, so whichever opens first
    wins: ``**x**`` stays literal inside a code span, while ``**a `b` c**`` is
    bold with a dim ``b`` inside it.
    """
    parts: list[str] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        parts.append(_code_spans(text[position:match.start()]))
        code = match.group("code")
        if code is not None:
            parts.append(DIM + code + RESET)
        else:
            parts.append(BOLD + _code_spans(match.group("bold"), resume=BOLD) + RESET)
        position = match.end()
    parts.append(_code_spans(text[position:]))
    return "".join(parts)


def style(text: str, *, enable: bool | None = None) -> str:
    """Return ``text`` styled for a terminal, or unchanged when styling is off.

    ``enable=None`` asks ``supports_style()``. Pass it explicitly to force
    either branch - which is how this gets tested, since a test suite never has
    a real terminal.
    """
    if enable is None:
        enable = supports_style()
    if not enable:
        return text

    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            # Markers inside the heading are removed rather than styled: a
            # nested RESET would end the heading's own bold mid-line.
            body = _BOLD_RE.sub(r"\1", _CODE_RE.sub(r"\1", heading.group(2)))
            prefix = HEADING if len(heading.group(1)) <= 2 else BOLD
            out.append(prefix + body + RESET)
            continue
        out.append(_inline(line))
    return "\n".join(out)
