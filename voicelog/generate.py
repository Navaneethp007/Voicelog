"""High-level generation pipeline for voicelog."""
from __future__ import annotations

from voicelog.models import Commit
from voicelog.llm import LLMError, MissingApiKey
from voicelog import llm
from voicelog import prompt
from voicelog import render


def generate(commits: list[Commit], voice_text: str, config, diff: str | None = None) -> str:
    """Build a prompt, call the LLM, and return the raw Markdown release notes.

    Args:
        commits:    List of Commit objects to include in the release notes.
        voice_text: Transcribed voice notes from the author (may be empty).
        config:     voicelog.config.Config instance.
        diff:       Optional aggregate code diff for the range (opt-in, from
                    ``gitsource.read_commits(with_diff=True)``). Sends actual
                    code to the LLM when provided; omitted by default.

    Returns:
        Raw Markdown string from the model.

    Raises:
        LLMError:      Propagated from llm.complete, or if the response is empty.
        MissingApiKey: Propagated from llm.complete when API key is absent.
    """
    messages = prompt.build_prompt(commits, voice_text, config.sections, diff=diff)

    # MissingApiKey and LLMError are intentionally not caught — cli handles fallback.
    raw_markdown = llm.complete(messages, config)

    if not raw_markdown or not raw_markdown.strip():
        raise LLMError("Empty response from model")

    return raw_markdown


def summarize(changelog_markdown: str, config, detail: bool = False) -> str:
    """Return a spoken-word summary of a full changelog (for TTS).

    ``detail`` selects a fuller walkthrough instead of the brief 2-3 sentences.

    Raises:
        LLMError:      From llm.complete, or if the response is empty.
        MissingApiKey: From llm.complete when the API key is absent.
    """
    messages = prompt.build_summary_prompt(changelog_markdown, detail=detail)
    reply = llm.complete(messages, config)

    if not reply or not reply.strip():
        raise LLMError("Empty summary from model")

    # Strip any leading reasoning block and surrounding whitespace.
    return render.strip_reasoning(reply).strip()


def onboard(readme_text: str, commits: list[Commit], config, diff: str | None = None) -> str:
    """Build the onboarding prompt, call the LLM, and return raw Markdown.

    Orients a developer on a repo they just cloned: what it is (from the
    README) plus what's been happening lately (from recent commits).

    Raises:
        LLMError:      Propagated from llm.complete, or if the response is empty.
        MissingApiKey: Propagated from llm.complete when API key is absent.
    """
    messages = prompt.build_onboarding_prompt(readme_text, commits, diff=diff)

    raw_markdown = llm.complete(messages, config)

    if not raw_markdown or not raw_markdown.strip():
        raise LLMError("Empty response from model")

    return raw_markdown
