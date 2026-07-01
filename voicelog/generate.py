"""High-level generation pipeline for voicelog."""
from __future__ import annotations

from voicelog.models import Commit
from voicelog.llm import LLMError, MissingApiKey
from voicelog import llm
from voicelog import prompt


def generate(commits: list[Commit], voice_text: str, config) -> str:
    """Build a prompt, call the LLM, and return the raw Markdown release notes.

    Args:
        commits:    List of Commit objects to include in the release notes.
        voice_text: Transcribed voice notes from the author (may be empty).
        config:     voicelog.config.Config instance.

    Returns:
        Raw Markdown string from the model.

    Raises:
        LLMError:      Propagated from llm.complete, or if the response is empty.
        MissingApiKey: Propagated from llm.complete when API key is absent.
    """
    messages = prompt.build_prompt(commits, voice_text, config.sections)

    # MissingApiKey and LLMError are intentionally not caught — cli handles fallback.
    raw_markdown = llm.complete(messages, config)

    if not raw_markdown or not raw_markdown.strip():
        raise LLMError("Empty response from model")

    return raw_markdown
