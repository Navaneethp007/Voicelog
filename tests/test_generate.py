"""Tests for voicelog.generate and voicelog.llm modules.

All tests mock llm.complete or httpx.post — no real HTTP calls are made.
"""
from __future__ import annotations

import dataclasses
import os
from unittest.mock import MagicMock, patch, call

import pytest

from voicelog import config as config_module
from voicelog.models import Commit
from voicelog.llm import (
    complete, MissingApiKey, LLMError, MissingModel, ModelUnavailable, InvalidApiKey,
)
from voicelog.generate import generate, summarize, onboard


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def config():
    # defaults_config() rather than thirteen hand-passed fields: a fake that
    # lists them itself drifts from the schema, and drifting fakes are what
    # kept fourteen unreachable getattr guards alive in production.
    return dataclasses.replace(
        config_module.defaults_config(),
        model="test-model",   # there is deliberately no default model
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


# 5b. diff is threaded through to prompt.build_prompt when provided
def test_generate_passes_diff_to_prompt_when_provided(sample_commits, config):
    markdown = "## Features\n- x\n"
    fake_messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    with patch("voicelog.generate.llm.complete", return_value=markdown):
        with patch("voicelog.generate.prompt.build_prompt", return_value=fake_messages) as mock_build:
            generate(sample_commits, "", config, diff="unique_diff_marker")

    mock_build.assert_called_once()
    _, kwargs = mock_build.call_args
    assert kwargs.get("diff") == "unique_diff_marker"


# 5c. diff defaults to None when not passed
def test_generate_diff_defaults_to_none(sample_commits, config):
    markdown = "## Features\n- x\n"
    with patch("voicelog.generate.llm.complete", return_value=markdown):
        with patch("voicelog.generate.prompt.build_prompt", return_value=[
            {"role": "system", "content": "s"}, {"role": "user", "content": "u"},
        ]) as mock_build:
            generate(sample_commits, "", config)

    _, kwargs = mock_build.call_args
    assert kwargs.get("diff") is None


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
# onboard() — orient a developer on a freshly cloned repo
# ===========================================================================

def test_onboard_returns_markdown_on_success(sample_commits, config):
    markdown = "## What this is\nA tool.\n\n## Recent activity\n- did stuff"
    with patch("voicelog.generate.llm.complete", return_value=markdown) as mock_complete:
        result = onboard("some readme", sample_commits, config)
    assert result == markdown
    mock_complete.assert_called_once()


def test_onboard_raises_on_empty_response(sample_commits, config):
    with patch("voicelog.generate.llm.complete", return_value=""):
        with pytest.raises(LLMError):
            onboard("some readme", sample_commits, config)


def test_onboard_passes_diff_to_prompt_when_provided(sample_commits, config):
    markdown = "## What this is\nA tool.\n"
    fake_messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    with patch("voicelog.generate.llm.complete", return_value=markdown):
        with patch(
            "voicelog.generate.prompt.build_onboarding_prompt", return_value=fake_messages
        ) as mock_build:
            onboard("readme text", sample_commits, config, diff="unique_onboard_diff")

    mock_build.assert_called_once()
    _, kwargs = mock_build.call_args
    assert kwargs.get("diff") == "unique_onboard_diff"


def test_onboard_reraises_llm_error(sample_commits, config):
    original_error = LLMError("upstream failure")
    with patch("voicelog.generate.llm.complete", side_effect=original_error):
        with pytest.raises(LLMError) as exc_info:
            onboard("readme text", sample_commits, config)
    assert exc_info.value is original_error


def test_onboard_reraises_missing_api_key(sample_commits, config):
    original_error = MissingApiKey("Set NVIDIA_API_KEY env var — see https://build.nvidia.com")
    with patch("voicelog.generate.llm.complete", side_effect=original_error):
        with pytest.raises(MissingApiKey) as exc_info:
            onboard("readme text", sample_commits, config)
    assert exc_info.value is original_error


def test_onboard_works_with_empty_commits(config):
    markdown = "## What this is\nA tool.\n"
    with patch("voicelog.generate.llm.complete", return_value=markdown):
        result = onboard("readme text", [], config)
    assert result == markdown


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


# ---------------------------------------------------------------------------
# No model configured
# ---------------------------------------------------------------------------

def test_complete_raises_missing_model_when_model_is_blank(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    config.model = ""

    with patch("voicelog.llm.httpx.post") as post:
        with pytest.raises(MissingModel):
            complete([{"role": "user", "content": "x"}], config)

    post.assert_not_called()  # never spend a request on a config error


def test_missing_model_message_points_at_setup(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    config.model = "   "  # whitespace is not a model

    with pytest.raises(MissingModel) as exc:
        complete([{"role": "user", "content": "x"}], config)

    assert "--setup" in str(exc.value)


def test_missing_model_is_an_llm_error(config, monkeypatch):
    """cli's summarize path catches LLMError; an uncaught raise there would
    traceback *after* the changelog already printed."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    config.model = ""

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "x"}], config)


# ---------------------------------------------------------------------------
# Retired / unknown model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [404, 410])
def test_retired_model_raises_model_unavailable(config, monkeypatch, status):
    """HTTP 410 is exactly what NVIDIA returned for the retired default."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = status
    resp.text = "model has reached its end of life"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(ModelUnavailable):
            complete([{"role": "user", "content": "x"}], config)


def test_retired_model_does_not_consume_the_retry(config, monkeypatch):
    """Retrying a dead model id only doubles the wait before the same answer."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 410
    resp.text = "gone"

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        with pytest.raises(ModelUnavailable):
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count == 1


def test_model_unavailable_message_names_model_and_setup(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    config.model = "vendor/retired-model"
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 410
    resp.text = "gone"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(ModelUnavailable) as exc:
            complete([{"role": "user", "content": "x"}], config)

    message = str(exc.value)
    assert "vendor/retired-model" in message
    assert "--setup" in message
    assert config.base_url in message  # a 404 can equally mean a wrong base_url


def test_model_unavailable_is_an_llm_error(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 404
    resp.text = "nope"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError):
            complete([{"role": "user", "content": "x"}], config)


def test_server_error_still_retries_and_stays_a_plain_llm_error(config, monkeypatch):
    """Real outages keep the documented retry-then-soft-fallback behaviour."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 500
    resp.text = "boom"

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        with pytest.raises(LLMError) as exc:
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count == 2
    assert not isinstance(exc.value, ModelUnavailable)


# ---------------------------------------------------------------------------
# base_url handling
# ---------------------------------------------------------------------------

def test_trailing_slash_base_url_does_not_double_the_separator(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    config.base_url = "https://example.test/v1/"
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        complete([{"role": "user", "content": "x"}], config)

    assert post.call_args[0][0] == "https://example.test/v1/chat/completions"


# ---------------------------------------------------------------------------
# Keyless endpoints (a local Ollama)
# ---------------------------------------------------------------------------

def test_blank_api_key_env_means_no_key_is_needed(config, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    config.api_key_env = ""
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        assert complete([{"role": "user", "content": "x"}], config) == "ok"

    assert "Authorization" not in post.call_args[1]["headers"]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_error_text_does_not_echo_the_api_key(config, monkeypatch):
    """Some gateways quote the offending request - including its key - in a 401."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-supersecret")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = 'unauthorized: header was "Bearer nvapi-supersecret"'

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError) as exc:
            complete([{"role": "user", "content": "x"}], config)

    assert "nvapi-supersecret" not in str(exc.value)


def test_a_key_straddling_the_truncation_point_is_still_redacted(config, monkeypatch):
    """Truncating before redacting cuts the key in half, and then no replace()
    can find it - the prefix ships to stderr and into CI logs."""
    key = "nvapi-0123456789abcdefghijklmnop"
    monkeypatch.setenv("NVIDIA_API_KEY", key)
    # Place the key so that it spans the 300-char cut.
    body = ("x" * 290) + key + ("y" * 50)
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = body

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError) as exc:
            complete([{"role": "user", "content": "x"}], config)

    message = str(exc.value)
    assert key not in message
    assert "nvapi-0123456789" not in message  # not even the prefix


# ---------------------------------------------------------------------------
# A rejected key is a configuration problem, not an outage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_raises_invalid_api_key(config, monkeypatch, status):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-expired")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = status
    resp.text = "unauthorized"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(InvalidApiKey):
            complete([{"role": "user", "content": "x"}], config)


def test_rejected_key_does_not_consume_the_retry(config, monkeypatch):
    """A wrong key will not become right on a second attempt."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-expired")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = "unauthorized"

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        with pytest.raises(InvalidApiKey):
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count == 1


def test_rejected_key_message_names_the_env_var_and_expiry(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-expired")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = "unauthorized"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(InvalidApiKey) as exc:
            complete([{"role": "user", "content": "x"}], config)

    message = str(exc.value)
    assert "NVIDIA_API_KEY" in message
    assert "expire" in message.lower()


def test_invalid_api_key_is_an_llm_error(config, monkeypatch):
    """So the spoken-summary path, which runs after the changelog has printed,
    keeps catching it instead of tracebacking."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-expired")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = "unauthorized"

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError):
            complete([{"role": "user", "content": "x"}], config)


def test_rejected_key_error_does_not_echo_the_key(config, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-supersecret")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = 'unauthorized: "Bearer nvapi-supersecret"'

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(InvalidApiKey) as exc:
            complete([{"role": "user", "content": "x"}], config)

    assert "nvapi-supersecret" not in str(exc.value)


def test_auth_statuses_are_shared_with_providers():
    from voicelog import llm, providers

    assert providers.AUTH_REJECTED == (401, 403)
    assert llm._AUTH_REJECTED is providers.AUTH_REJECTED


# ---------------------------------------------------------------------------
# Parameter negotiation - reasoning models reject our defaults
# ---------------------------------------------------------------------------

def _unsupported(param, message=None):
    """A provider 400 for an unsupported parameter, OpenAI's shape."""
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 400
    resp.json.return_value = {
        "error": {
            "message": message or f"Unsupported parameter: '{param}' is not supported with this model.",
            "param": param,
            "code": "unsupported_parameter",
        }
    }
    resp.text = "400"
    return resp


def _ok(content="generated text"):
    resp = MagicMock()
    resp.is_success = True
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def test_max_tokens_is_renamed_when_the_model_rejects_it(config, monkeypatch):
    """o-series and gpt-5 want max_completion_tokens. Hardcoding a model list
    would be the same rot we removed for model ids, so ask the provider."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    responses = [_unsupported("max_tokens"), _ok()]

    with patch("voicelog.llm.httpx.post", side_effect=responses) as post:
        assert complete([{"role": "user", "content": "x"}], config) == "generated text"

    body = post.call_args[1]["json"]
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 4096


def test_temperature_is_dropped_when_the_model_rejects_it(config, monkeypatch):
    """o-series only supports the default temperature."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    responses = [_unsupported("temperature"), _ok()]

    with patch("voicelog.llm.httpx.post", side_effect=responses) as post:
        complete([{"role": "user", "content": "x"}], config)

    assert "temperature" not in post.call_args[1]["json"]


def test_both_parameters_can_be_negotiated_in_one_run(config, monkeypatch):
    """A reasoning model rejects both, one at a time."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    responses = [_unsupported("max_tokens"), _unsupported("temperature"), _ok()]

    with patch("voicelog.llm.httpx.post", side_effect=responses) as post:
        assert complete([{"role": "user", "content": "x"}], config) == "generated text"

    body = post.call_args[1]["json"]
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert body["max_completion_tokens"] == 4096
    assert post.call_count == 3


def test_negotiation_reads_the_message_when_there_is_no_param_field(config, monkeypatch):
    """Not every provider fills in error.param."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    bare = MagicMock()
    bare.is_success = False
    bare.status_code = 400
    bare.json.return_value = {
        "error": {"message": "Unsupported parameter: 'max_tokens' is not supported"}
    }
    bare.text = "400"

    with patch("voicelog.llm.httpx.post", side_effect=[bare, _ok()]) as post:
        complete([{"role": "user", "content": "x"}], config)

    assert "max_completion_tokens" in post.call_args[1]["json"]


def test_an_unrelated_400_is_not_negotiated(config, monkeypatch):
    """Only parameters we actually sent, and only ones we know how to change."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 400
    resp.json.return_value = {"error": {"message": "your prompt is too long"}}
    resp.text = "too long"

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        with pytest.raises(LLMError):
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count == 2      # the normal retry, no adaptation loop


def test_negotiation_is_bounded(config, monkeypatch):
    """A provider that always says 400 must not loop forever."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")

    with patch("voicelog.llm.httpx.post", return_value=_unsupported("max_tokens")) as post:
        with pytest.raises(LLMError):
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count <= 6


def test_a_server_error_still_retries_exactly_twice(config, monkeypatch):
    """Negotiation must not disturb the ordinary retry budget."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 500
    resp.text = "boom"

    with patch("voicelog.llm.httpx.post", return_value=resp) as post:
        with pytest.raises(LLMError):
            complete([{"role": "user", "content": "x"}], config)

    assert post.call_count == 2


def test_the_verifier_negotiates_too(config, monkeypatch):
    """Otherwise the check would condemn a model generation can now drive."""
    from voicelog import providers

    with patch("voicelog.providers.httpx.post",
               side_effect=[_unsupported("max_tokens"), _ok()]):
        assert providers.verify_model("https://x/v1", "k", "openai/o4-mini") is None


def test_a_provider_error_body_keeps_its_whole_budget(config):
    """The body is deliberately budgeted at _MAX_BODY_CHARS. Wrapping the
    finished message in detail() again re-truncated it to the 200-char default,
    silently discarding a third of the diagnostic that budget exists to keep."""
    import os
    from voicelog.llm import _MAX_BODY_CHARS

    os.environ["NVIDIA_API_KEY"] = "k"
    body = "E" * 600
    resp = MagicMock(status_code=400, is_success=False, text=body)

    with patch("voicelog.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError) as exc:
            complete("prompt", config)

    kept = str(exc.value).count("E")
    assert kept >= _MAX_BODY_CHARS - 20, f"only {kept} of {_MAX_BODY_CHARS} kept"
