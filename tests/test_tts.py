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


def _make_fake_riva():
    """Return a fake riva.client module + AudioEncoding."""
    fake_riva = types.SimpleNamespace()
    fake_riva.Auth = mock.MagicMock(name="Auth")

    resp = types.SimpleNamespace(audio=b"\x01\x02\x03\x04")
    service = mock.MagicMock(name="Service")
    service.synthesize.return_value = resp
    fake_riva.SpeechSynthesisService = mock.MagicMock(return_value=service)

    fake_encoding = types.SimpleNamespace(LINEAR_PCM=1)
    return fake_riva, fake_encoding, service


def test_speak_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(TTSError):
        tts.speak("Hello world", FakeConfig())


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
    assert len(played) == 1


def test_speak_synthesize_exception_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    service.synthesize.side_effect = RuntimeError("grpc boom")
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig())
    assert "grpc boom" in str(exc.value)
