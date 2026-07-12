"""Tests for voicelog.tts — all mocked, no real gRPC, no real audio."""
from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from voicelog import tts
from voicelog.tts import TTSError, _chunk_text, _speech_text


# ---------------------------------------------------------------------------
# Pure helper: _speech_text
# ---------------------------------------------------------------------------

def test_speech_text_strips_heading_hashes():
    out = _speech_text("## Unreleased\n### Features")
    assert "Unreleased" in out
    assert "Features" in out
    assert "#" not in out


def test_speech_text_removes_fenced_code_blocks():
    md = "Here is code:\n```python\nprint('secret')\n```\nDone."
    out = _speech_text(md)
    assert "print" not in out
    assert "secret" not in out
    assert "Here is code:" in out
    assert "Done." in out


def test_speech_text_turns_bullet_into_plain():
    out = _speech_text("- did a thing")
    assert out == "did a thing"


def test_speech_text_turns_link_into_text():
    out = _speech_text("See [the docs](http://example.com/page) now")
    assert "the docs" in out
    assert "http" not in out
    assert "example.com" not in out


def test_speech_text_strips_inline_markers_keeps_words():
    out = _speech_text("This is `code` and *bold* and _italic_ words")
    assert "code" in out
    assert "bold" in out
    assert "italic" in out
    assert "`" not in out
    assert "*" not in out
    assert "_" not in out


def test_speech_text_empty_for_only_code():
    assert _speech_text("```\ncode here\n```").strip() == ""


# ---------------------------------------------------------------------------
# Pure helper: _chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_single_chunk_under_max():
    text = "Short sentence."
    assert _chunk_text(text, max_len=400) == ["Short sentence."]


def test_chunk_text_splits_without_cutting_midword():
    words = ["word%d" % i for i in range(300)]
    text = " ".join(words)
    chunks = _chunk_text(text, max_len=400)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 400
    rejoined = " ".join(chunks).split()
    assert rejoined == words


def test_chunk_text_prefers_sentence_boundaries():
    first = "A" * 300 + "."
    second = "B" * 200 + "."
    text = first + " " + second
    chunks = _chunk_text(text, max_len=400)
    # Splitting at the sentence boundary keeps each sentence intact.
    assert any(c.endswith(".") for c in chunks)
    assert chunks[0].rstrip() == first


def test_chunk_text_drops_empty_chunks():
    chunks = _chunk_text("   ", max_len=400)
    assert chunks == []


# ---------------------------------------------------------------------------
# speak() — mocked
# ---------------------------------------------------------------------------

class FakeConfig:
    tts_function_id = "fid-123"
    tts_voice = "Some.Voice"
    tts_language = "en-US"
    tts_sample_rate = 44100
    tts_timeout = 90.0
    tts_api_key_env = "NVIDIA_API_KEY"


def _make_fake_riva():
    """Return a fake riva.client module + AudioEncoding.

    synthesize(future=True) returns a call object whose .result(timeout=...)
    yields the response — mirroring the real gRPC future interface.
    """
    fake_riva = types.SimpleNamespace()
    fake_riva.Auth = mock.MagicMock(name="Auth")

    resp = types.SimpleNamespace(audio=b"\x01\x02\x03\x04")
    call = mock.MagicMock(name="Call")
    call.result.return_value = resp
    service = mock.MagicMock(name="Service")
    service.synthesize.return_value = call
    fake_riva.SpeechSynthesisService = mock.MagicMock(return_value=service)

    fake_encoding = types.SimpleNamespace(LINEAR_PCM=1)
    return fake_riva, fake_encoding, service


def test_speak_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(TTSError):
        tts.speak("Hello world", FakeConfig())


def test_speak_reads_configured_key_env(monkeypatch):
    """TTS reads the key from the env var named in config.tts_api_key_env."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("RIVA_KEY", "riva-abc")

    class Cfg(FakeConfig):
        tts_api_key_env = "RIVA_KEY"

    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path: None)

    tts.speak("Hello there.", Cfg())

    # The configured key reached the auth metadata as a Bearer token.
    _, auth_kwargs = fake_riva.Auth.call_args
    meta = dict((k, v) for k, v in auth_kwargs["metadata_args"])
    assert meta["authorization"] == "Bearer riva-abc"


def test_speak_import_failure_raises_with_hint(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")

    def boom():
        raise ImportError("no riva")

    monkeypatch.setattr(tts, "_import_riva", boom)
    monkeypatch.setattr(tts, "_play", lambda path: None)
    with pytest.raises(TTSError) as exc:
        tts.speak("Hello world", FakeConfig())
    assert "pip install voicelog[tts]" in str(exc.value)


def test_speak_empty_speech_returns_without_synthesizing(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path: None)

    result = tts.speak("```\ncode only\n```", FakeConfig())
    assert result is None
    service.synthesize.assert_not_called()


def test_speak_happy_path(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    played = []
    monkeypatch.setattr(tts, "_play", lambda path: played.append(path))

    cfg = FakeConfig()
    tts.speak("Hello there.", cfg)

    service.synthesize.assert_called_once()
    _, kwargs = service.synthesize.call_args
    assert kwargs["voice_name"] == cfg.tts_voice
    assert kwargs["language_code"] == cfg.tts_language
    assert kwargs["sample_rate_hz"] == cfg.tts_sample_rate
    # Uses the async future path so a deadline can be enforced.
    assert kwargs["future"] is True
    call = service.synthesize.return_value
    _, result_kwargs = call.result.call_args
    assert result_kwargs["timeout"] == cfg.tts_timeout
    assert len(played) == 1


def test_speak_playback_failure_raises_ttserror(monkeypatch):
    """A playback failure (busy/absent audio device on macOS/Linux) must become
    a TTSError so the CLI's 'audio only warns, never blocks' contract holds."""
    import subprocess

    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))

    def boom(path):
        raise subprocess.CalledProcessError(1, ["afplay", path])

    monkeypatch.setattr(tts, "_play", boom)

    with pytest.raises(TTSError):
        tts.speak("Hello there.", FakeConfig())


def test_speak_playback_missing_player_raises_ttserror(monkeypatch):
    """If the player binary vanished after the availability check, that OSError
    is also converted to TTSError rather than crashing."""
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))

    def boom(path):
        raise FileNotFoundError("afplay: not found")

    monkeypatch.setattr(tts, "_play", boom)

    with pytest.raises(TTSError):
        tts.speak("Hello there.", FakeConfig())


def test_speak_synthesize_exception_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    service.synthesize.side_effect = RuntimeError("grpc boom")
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig())
    assert "grpc boom" in str(exc.value)


def test_speak_timeout_raises_clean_error(monkeypatch):
    """A hung synthesis call is bounded by tts_timeout and raises TTSError."""
    import grpc

    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    service.synthesize.return_value.result.side_effect = grpc.FutureTimeoutError()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig())
    assert "timed out" in str(exc.value).lower()
    # The hung call is cancelled so the channel isn't left dangling.
    service.synthesize.return_value.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Provider dispatch — unknown provider, provider-specific key errors
# ---------------------------------------------------------------------------

class OpenAIConfig(FakeConfig):
    tts_provider = "openai"
    tts_model = "gpt-4o-mini-tts"
    tts_base_url = ""
    tts_voice = "alloy"
    tts_api_key_env = "OPENAI_API_KEY"


class ElevenLabsConfig(FakeConfig):
    tts_provider = "elevenlabs"
    tts_model = "eleven_multilingual_v2"
    tts_voice = "voice-id-123"
    tts_api_key_env = "ELEVENLABS_API_KEY"


def test_unknown_provider_raises_before_any_network_call(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")

    class BadConfig(FakeConfig):
        tts_provider = "not-a-real-provider"

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", BadConfig())
    assert "not-a-real-provider" in str(exc.value)


def test_openai_missing_key_names_configured_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", OpenAIConfig())
    assert "OPENAI_API_KEY" in str(exc.value)


def test_openai_happy_path_posts_and_plays(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    played = []
    monkeypatch.setattr(tts, "_play", lambda path: played.append(path))

    mock_resp = mock.MagicMock()
    mock_resp.is_success = True
    mock_resp.content = b"\x01\x02\x03\x04"

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp) as mock_post:
        tts.speak("Hello there.", OpenAIConfig())

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://api.openai.com/v1/audio/speech"
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk-test"
    body = call_kwargs["json"]
    assert body["model"] == "gpt-4o-mini-tts"
    assert body["voice"] == "alloy"
    assert body["response_format"] == "pcm"
    assert len(played) == 1


def test_openai_custom_base_url_is_used(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tts, "_play", lambda path: None)

    class CustomConfig(OpenAIConfig):
        tts_base_url = "https://my-proxy.example.com/v1"

    mock_resp = mock.MagicMock()
    mock_resp.is_success = True
    mock_resp.content = b"\x01\x02"

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp) as mock_post:
        tts.speak("Hi", CustomConfig())

    call_args, _ = mock_post.call_args
    assert call_args[0] == "https://my-proxy.example.com/v1/audio/speech"


def test_openai_http_failure_raises_ttserror(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tts, "_play", lambda path: None)

    mock_resp = mock.MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 401
    mock_resp.text = "unauthorized"

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp):
        with pytest.raises(TTSError) as exc:
            tts.speak("Hello there.", OpenAIConfig())
    assert "401" in str(exc.value)


def test_elevenlabs_missing_key_names_configured_env(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", ElevenLabsConfig())
    assert "ELEVENLABS_API_KEY" in str(exc.value)


def test_elevenlabs_happy_path_posts_and_plays(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    played = []
    monkeypatch.setattr(tts, "_play", lambda path: played.append(path))

    mock_resp = mock.MagicMock()
    mock_resp.is_success = True
    mock_resp.content = b"\x05\x06\x07\x08"

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp) as mock_post:
        tts.speak("Hello there.", ElevenLabsConfig())

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert "voice-id-123" in call_args[0]
    assert call_kwargs["headers"]["xi-api-key"] == "el-test"
    assert call_kwargs["json"]["model_id"] == "eleven_multilingual_v2"
    assert len(played) == 1


def test_elevenlabs_missing_voice_id_raises(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")

    class NoVoiceConfig(ElevenLabsConfig):
        tts_voice = ""

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", NoVoiceConfig())
    assert "tts_voice" in str(exc.value)
