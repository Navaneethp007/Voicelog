"""Tests for voicelog.generate and voicelog.llm modules.

All tests mock llm.complete or httpx.post — no real HTTP calls are made.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch, call

import pytest

from voicelog.models import Commit
from voicelog.config import Config, DEFAULTS
from voicelog.llm import complete, MissingApiKey, LLMError
from voicelog.generate import generate, summarize


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def config():
    return Config(
        provider=DEFAULTS["provider"],
        base_url=DEFAULTS["base_url"],
        model=DEFAULTS["model"],
        sections=list(DEFAULTS["sections"]),
        noise=list(DEFAULTS["noise"]),
        voice_samples=DEFAULTS["voice_samples"],
        fallback_commits=int(DEFAULTS["fallback_commits"]),
        speak=DEFAULTS["speak"],
        tts_function_id=DEFAULTS["tts_function_id"],
        tts_voice=DEFAULTS["tts_voice"],
        tts_language=DEFAULTS["tts_language"],
        tts_sample_rate=DEFAULTS["tts_sample_rate"],
        voice_md=DEFAULTS["voice_md"],
    )


@pytest.fixture()
def sample_commits():
    return [
        Commit(
            hash="abc1234",
            subject="feat: add OAuth login",
            body="Implements login flow.",
            author="Alice",
            files=["src/auth.py"],
        ),
        Commit(
            hash="def5678",
            subject="fix: handle null session",
            body="",
            author="Bob",
            files=[],
        ),
    ]


# ===========================================================================
# generate.py behaviour tests  (tests 1–5)
# ===========================================================================

# 1. Happy path: llm.complete returns markdown → generate returns it unchanged
def test_generate_returns_markdown_on_success(sample_commits, config):
    markdown = "## Features\n- Add OAuth login\n"
    with patch("voicelog.generate.llm.complete", return_value=markdown) as mock_complete:
        result = generate(sample_commits, "Some voice notes.", config)
    assert result == markdown
    mock_complete.assert_called_once()


# 2. Empty string response from llm.complete → raises LLMError
def test_generate_raises_on_empty_string_response(sample_commits, config):
    with patch("voicelog.generate.llm.complete", return_value=""):
        with pytest.raises(LLMError):
            generate(sample_commits, "voice notes", config)


# 3. Whitespace-only response → raises LLMError
def test_generate_raises_on_whitespace_only_response(sample_commits, config):
    with patch("voicelog.generate.llm.complete", return_value="   \n\t  "):
        with pytest.raises(LLMError):
            generate(sample_commits, "voice notes", config)


# 4. llm.complete raises LLMError → generate re-raises it (does not swallow)
def test_generate_reraises_llm_error(sample_commits, config):
    original_error = LLMError("upstream failure")
    with patch("voicelog.generate.llm.complete", side_effect=original_error):
        with pytest.raises(LLMError) as exc_info:
            generate(sample_commits, "voice notes", config)
    assert exc_info.value is original_error


# 5. llm.complete raises MissingApiKey → generate re-raises it (does not swallow)
def test_generate_reraises_missing_api_key(sample_commits, config):
    original_error = MissingApiKey("Set NVIDIA_API_KEY env var — see https://build.nvidia.com")
    with patch("voicelog.generate.llm.complete", side_effect=original_error):
        with pytest.raises(MissingApiKey) as exc_info:
            generate(sample_commits, "voice notes", config)
    assert exc_info.value is original_error


# ===========================================================================
# summarize() — the short spoken digest
# ===========================================================================

def test_summarize_returns_spoken_text(config):
    changelog = "## Unreleased\n\n### Features\n- Added PDF export"
    with patch("voicelog.generate.llm.complete", return_value="Three features and two fixes.") as mock:
        result = summarize(changelog, config)
    assert result == "Three features and two fixes."
    mock.assert_called_once()


def test_summarize_empty_raises_llmerror(config):
    with patch("voicelog.generate.llm.complete", return_value="   "):
        with pytest.raises(LLMError):
            summarize("## Unreleased\n\n- x", config)


def test_summarize_strips_think_block(config):
    reply = "<think>let me reason</think>Two fixes and a new export option."
    with patch("voicelog.generate.llm.complete", return_value=reply):
        result = summarize("## Unreleased\n\n- x", config)
    assert "<think>" not in result
    assert "let me reason" not in result
    assert "Two fixes and a new export option." in result


# ===========================================================================
# llm.py behaviour tests  (tests 6–10)
# ===========================================================================

# 6. NVIDIA_API_KEY unset → raises MissingApiKey with helpful message
def test_complete_raises_missing_api_key_when_env_var_absent(config):
    messages = [{"role": "user", "content": "Hello"}]
    env_without_key = {k: v for k, v in os.environ.items() if k != "NVIDIA_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises(MissingApiKey) as exc_info:
            complete(messages, config)
    assert "NVIDIA_API_KEY" in str(exc_info.value)


# 6b. Reads the API key from the env var named in config.api_key_env
def test_complete_uses_configured_key_env(config):
    import dataclasses
    cfg = dataclasses.replace(config, api_key_env="MY_PROVIDER_KEY")
    messages = [{"role": "user", "content": "Hello"}]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.is_success = True
    mock_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    env = {"MY_PROVIDER_KEY": "abc123"}  # NVIDIA_API_KEY intentionally absent
    with patch.dict(os.environ, env, clear=True):
        with patch("voicelog.llm.httpx.post", return_value=mock_response) as mock_post:
            result = complete(messages, cfg)

    assert result == "ok"
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer abc123"


# 6c. MissingApiKey names the configured env var
def test_missing_key_message_names_configured_env(config):
    import dataclasses
    cfg = dataclasses.replace(config, api_key_env="OPENAI_API_KEY")
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(MissingApiKey) as exc:
            complete([{"role": "user", "content": "x"}], cfg)
    assert "OPENAI_API_KEY" in str(exc.value)


# 7. httpx returns success → complete returns the content string
def test_complete_returns_content_on_success(config):
    messages = [{"role": "user", "content": "Hello"}]
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()  # does not raise
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "## Release notes\n- Feature A\n"}}]
    }

    with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123"}):
        with patch("voicelog.llm.httpx.post", return_value=mock_response) as mock_post:
            result = complete(messages, config)

    assert result == "## Release notes\n- Feature A\n"
    mock_post.assert_called_once()


# 8. httpx fails first call then succeeds on retry → complete returns content
def test_complete_retries_once_on_failure_then_succeeds(config):
    messages = [{"role": "user", "content": "Generate notes"}]

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()
    success_response.json.return_value = {
        "choices": [{"message": {"content": "Retry worked!"}}]
    }

    import httpx as httpx_module
    with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123"}):
        with patch(
            "voicelog.llm.httpx.post",
            side_effect=[httpx_module.RequestError("connection reset"), success_response],
        ) as mock_post:
            result = complete(messages, config)

    assert result == "Retry worked!"
    assert mock_post.call_count == 2


# 9. httpx fails both attempts → raises LLMError
def test_complete_raises_llm_error_when_both_attempts_fail(config):
    messages = [{"role": "user", "content": "Generate notes"}]

    import httpx as httpx_module
    with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123"}):
        with patch(
            "voicelog.llm.httpx.post",
            side_effect=[
                httpx_module.RequestError("timeout"),
                httpx_module.RequestError("timeout again"),
            ],
        ):
            with pytest.raises(LLMError):
                complete(messages, config)


# 10. Request body sent to httpx contains required fields
def test_complete_sends_correct_request_body(config):
    messages = [{"role": "user", "content": "Write changelog"}]
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "changelog"}}]
    }

    with patch.dict(os.environ, {"NVIDIA_API_KEY": "secret-key"}):
        with patch("voicelog.llm.httpx.post", return_value=mock_response) as mock_post:
            complete(messages, config)

    _, call_kwargs = mock_post.call_args
    body = call_kwargs.get("json", {})

    assert body.get("model") == config.model
    assert body.get("messages") == messages
    assert "temperature" in body
    assert "max_tokens" in body
    # The request deadline comes from config, not a hardcoded value.
    assert call_kwargs.get("timeout") == config.llm_timeout


# 11. A 200 with a non-OpenAI body falls back to LLMError instead of crashing.
def test_complete_falls_back_on_malformed_200(config):
    messages = [{"role": "user", "content": "Write changelog"}]

    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.is_success = True
    bad_response.json.return_value = {}  # no "choices" — a gateway error envelope

    with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123"}):
        with patch(
            "voicelog.llm.httpx.post", return_value=bad_response
        ) as mock_post:
            with pytest.raises(LLMError):
                complete(messages, config)

    # Malformed body is treated like any failure: one retry, then LLMError.
    assert mock_post.call_count == 2
