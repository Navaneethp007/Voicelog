"""Tests for voicelog.tts — all mocked, no real gRPC, no real audio."""
from __future__ import annotations

import dataclasses
import sys
import threading
import types
from unittest import mock

import pytest

from voicelog import config as config_module
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

def FakeConfig(**overrides):
    """A real Config, not a stand-in that omits most of it.

    This was a plain class carrying six attributes and omitting eighteen, which
    is what kept fourteen `getattr(config, "x", default)` guards alive in
    production: they were unreachable on every real path and only looked
    necessary because of this double. Building from defaults_config() means the
    fakes track the schema - add a field and these tests get it for free.
    """
    fields = dict(
        tts_function_id="fid-123",
        tts_voice="Some.Voice",
        tts_language="en-US",
        tts_sample_rate=44100,
        tts_timeout=90.0,
        tts_api_key_env="NVIDIA_API_KEY",
    )
    fields.update(overrides)
    return dataclasses.replace(config_module.defaults_config(), **fields)


def OpenAIConfig(**overrides):
    fields = dict(
        tts_provider="openai",
        tts_model="gpt-4o-mini-tts",
        tts_base_url="",
        tts_voice="alloy",
        tts_api_key_env="OPENAI_API_KEY",
    )
    fields.update(overrides)
    return FakeConfig(**fields)


def ElevenLabsConfig(**overrides):
    fields = dict(
        tts_provider="elevenlabs",
        tts_model="eleven_multilingual_v2",
        tts_voice="voice-id-123",
        tts_api_key_env="ELEVENLABS_API_KEY",
    )
    fields.update(overrides)
    return FakeConfig(**fields)


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

    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    tts.speak("Hello there.", FakeConfig(tts_api_key_env="RIVA_KEY"))

    # The configured key reached the auth metadata as a Bearer token.
    _, auth_kwargs = fake_riva.Auth.call_args
    meta = dict((k, v) for k, v in auth_kwargs["metadata_args"])
    assert meta["authorization"] == "Bearer riva-abc"


def test_speak_import_failure_raises_with_hint(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")

    def boom():
        raise ImportError("no riva")

    monkeypatch.setattr(tts, "_import_riva", boom)
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)
    with pytest.raises(TTSError) as exc:
        tts.speak("Hello world", FakeConfig())
    assert "pip install voicelog[tts]" in str(exc.value)


def test_speak_empty_speech_returns_without_synthesizing(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    result = tts.speak("```\ncode only\n```", FakeConfig())
    assert result is None
    service.synthesize.assert_not_called()


def test_speak_happy_path(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    played = []
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: played.append(path))

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

    def boom(path, seconds=0.0):
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

    def boom(path, seconds=0.0):
        raise FileNotFoundError("afplay: not found")

    monkeypatch.setattr(tts, "_play", boom)

    with pytest.raises(TTSError):
        tts.speak("Hello there.", FakeConfig())


def test_speak_synthesize_exception_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")
    fake_riva, fake_encoding, service = _make_fake_riva()
    service.synthesize.side_effect = RuntimeError("grpc boom")
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

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
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig())
    assert "timed out" in str(exc.value).lower()
    # The hung call is cancelled so the channel isn't left dangling.
    service.synthesize.return_value.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Provider dispatch — unknown provider, provider-specific key errors
# ---------------------------------------------------------------------------

def test_unknown_provider_raises_before_any_network_call(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key")

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig(tts_provider="not-a-real-provider"))
    assert "not-a-real-provider" in str(exc.value)


def test_openai_missing_key_names_configured_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", OpenAIConfig())
    assert "OPENAI_API_KEY" in str(exc.value)


def test_openai_happy_path_posts_and_plays(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    played = []
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: played.append(path))

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
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)


    mock_resp = mock.MagicMock()
    mock_resp.is_success = True
    mock_resp.content = b"\x01\x02"

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp) as mock_post:
        tts.speak("Hi", OpenAIConfig(tts_base_url="https://my-proxy.example.com/v1"))

    call_args, _ = mock_post.call_args
    assert call_args[0] == "https://my-proxy.example.com/v1/audio/speech"


def test_openai_http_failure_raises_ttserror(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

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
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: played.append(path))

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

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", ElevenLabsConfig(tts_voice=""))
    assert "tts_voice" in str(exc.value)


# ---------------------------------------------------------------------------
# Keys must not travel in error text
# ---------------------------------------------------------------------------

def test_openai_error_does_not_echo_the_key(monkeypatch):
    """tts_base_url is user-settable, so this adapter can be pointed at a
    proxy that quotes the request - headers included - back at us."""
    key = "sk-0123456789abcdefghij"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    mock_resp = mock.MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 401
    mock_resp.text = f'unauthorized: sent "Bearer {key}"'

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp):
        with pytest.raises(TTSError) as exc:
            tts.speak("Hello there.", OpenAIConfig())

    assert key not in str(exc.value)
    assert "401" in str(exc.value)  # still says what happened


def test_openai_error_redacts_before_truncating(monkeypatch):
    """The body is cut to 200 chars; cutting first would strand a key prefix."""
    key = "sk-0123456789abcdefghij"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    mock_resp = mock.MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 401
    mock_resp.text = ("x" * 190) + key + ("y" * 40)

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp):
        with pytest.raises(TTSError) as exc:
            tts.speak("Hello there.", OpenAIConfig())

    # Only the first 10 characters of the key fall inside the 200-char cut, so
    # that is the fragment a truncate-then-redact order would leak.
    assert "sk-0123456" not in str(exc.value)


def test_elevenlabs_error_does_not_echo_the_key(monkeypatch):
    key = "el-0123456789abcdefghij"
    monkeypatch.setenv("ELEVENLABS_API_KEY", key)
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    mock_resp = mock.MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 401
    mock_resp.text = f'unauthorized: xi-api-key was {key}'

    with mock.patch("voicelog.tts.httpx.post", return_value=mock_resp):
        with pytest.raises(TTSError) as exc:
            tts.speak("Hello there.", ElevenLabsConfig())

    assert key not in str(exc.value)
    assert "401" in str(exc.value)


def test_riva_error_does_not_echo_the_key(monkeypatch):
    """The gRPC branch catches Exception broadly and wraps the whole auth
    construction, which is handed the key."""
    key = "nvapi-0123456789abcdefghij"
    monkeypatch.setenv("NVIDIA_API_KEY", key)
    fake_riva, fake_encoding, service = _make_fake_riva()
    service.synthesize.side_effect = RuntimeError(f'bad metadata: Bearer {key}')
    monkeypatch.setattr(tts, "_import_riva", lambda: (fake_riva, fake_encoding))
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("Hello there.", FakeConfig())

    assert key not in str(exc.value)


# ---------------------------------------------------------------------------
# Windows playback must be interruptible
# ---------------------------------------------------------------------------

class _FakeWinsound:
    """Stands in for the winsound module, recording how it was called."""
    SND_FILENAME = 0x20000
    SND_ASYNC = 0x0001
    SND_PURGE = 0x0040

    def __init__(self):
        self.calls = []

    def PlaySound(self, sound, flags):
        self.calls.append((sound, flags))


def _wav(tmp_path, seconds=2.0, rate=8000):
    import wave
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(2 * int(rate * seconds)))  # silence
    return str(path)


def test_windows_playback_is_asynchronous(monkeypatch, tmp_path):
    """A synchronous PlaySound is a blocking call into the OS, so Python cannot
    deliver KeyboardInterrupt until it returns - Ctrl+C sat queued until the
    audio ended on its own, under a message promising it would skip."""
    fake = _FakeWinsound()
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)

    tts._play_windows(_wav(tmp_path, seconds=1.0), 1.0)

    _, flags = fake.calls[0]
    assert flags & fake.SND_ASYNC
    assert not any(f & fake.SND_PURGE for _, f in fake.calls)


def test_ctrl_c_during_playback_stops_the_audio(monkeypatch, tmp_path):
    fake = _FakeWinsound()
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)
    monkeypatch.setattr(tts.time, "sleep",
                        lambda s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        tts._play_windows(_wav(tmp_path, seconds=30.0), 30.0)

    # Purged, so the sound stops now rather than playing out its 30 seconds.
    assert fake.calls[-1] == (None, fake.SND_PURGE)


def test_the_sound_is_started_on_the_calling_thread(monkeypatch, tmp_path):
    """SND_PURGE only acts on the thread that started the sound. Handing
    PlaySound to a worker thread and joining it reads better - the thread
    returning would BE the completion signal, with no duration to know - but
    measured on Windows, purging another thread's sound blocks for the
    remainder of the audio (9.44s of a 10s file) instead of stopping it, which
    turns Ctrl+C into a no-op that merely waits."""
    fake = _FakeWinsound()
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    played_on = []
    real = fake.PlaySound

    def record(sound, flags):
        played_on.append(threading.current_thread())
        return real(sound, flags)

    fake.PlaySound = record
    tts._play_windows(_wav(tmp_path, seconds=1.0), 1.0)

    assert played_on == [threading.current_thread()]


def test_an_interrupt_as_playback_starts_still_purges(monkeypatch, tmp_path):
    """The window between starting the sound and entering the wait loop."""
    fake = _FakeWinsound()
    real = fake.PlaySound

    def _play(sound, flags):
        real(sound, flags)
        if flags & fake.SND_ASYNC:
            raise KeyboardInterrupt      # the signal arrives as playback starts

    fake.PlaySound = _play
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)

    with pytest.raises(KeyboardInterrupt):
        tts._play_windows(_wav(tmp_path, seconds=30.0), 30.0)

    assert fake.calls[-1] == (None, fake.SND_PURGE)


def test_a_failing_purge_does_not_replace_the_interrupt(monkeypatch, tmp_path):
    """SND_PURGE can raise RuntimeError of its own. If it did, it would discard
    the KeyboardInterrupt and cli._speak would never print "skipped audio"."""
    class _BadPurge(_FakeWinsound):
        def PlaySound(self, sound, flags):
            self.calls.append((sound, flags))
            if flags & self.SND_PURGE:
                raise RuntimeError("purge failed")

    fake = _BadPurge()
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)
    monkeypatch.setattr(tts.time, "sleep",
                        lambda s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        tts._play_windows(_wav(tmp_path, seconds=30.0), 30.0)


def test_the_duration_comes_from_the_caller_not_the_header(monkeypatch, tmp_path):
    """speak() wrote the PCM, so it knows the length exactly. Re-reading the
    header meant a file we could not parse fell through to a synchronous play,
    reinstating the uninterruptible bug this exists to fix."""
    fake = _FakeWinsound()
    monkeypatch.setattr(tts, "_import_winsound", lambda: fake)
    slept = []
    monkeypatch.setattr(tts.time, "sleep", lambda s: slept.append(s))
    bad = tmp_path / "not.wav"
    bad.write_bytes(b"nope")          # no readable header at all

    tts._play_windows(str(bad), 0.3)

    assert fake.calls[0][1] & fake.SND_ASYNC      # still asynchronous
    assert slept                                  # still waited


# ---------------------------------------------------------------------------
# speak()'s guard was shaped for POSIX and applied to both platforms
# ---------------------------------------------------------------------------

def _speakable(monkeypatch, exc):
    """A config that reaches playback, with _play raising ``exc``."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    monkeypatch.setattr(tts, "_check_playback", lambda: None)
    monkeypatch.setattr(tts, "_synth_riva", lambda chunks, key, cfg: (b"pcm-data", 44100))
    monkeypatch.setattr(tts, "_ADAPTERS", {"riva": tts._synth_riva})
    monkeypatch.setattr(tts, "_play",
                        lambda path, seconds=0.0: (_ for _ in ()).throw(exc))


def test_a_winsound_runtime_error_becomes_a_tts_error(monkeypatch):
    """winsound.PlaySound raises RuntimeError("Failed to play sound"), which is
    not an OSError - so it escaped this guard, escaped cli._speak's except
    TTSError, and tracebacked out of main() after the changelog had printed and
    been paid for."""
    _speakable(monkeypatch, RuntimeError("Failed to play sound"))

    with pytest.raises(TTSError):
        tts.speak("hello", FakeConfig())


def test_a_missing_winsound_becomes_a_tts_error(monkeypatch):
    """_import_winsound raises ImportError on a Windows build without it."""
    _speakable(monkeypatch, ImportError("no winsound"))

    with pytest.raises(TTSError):
        tts.speak("hello", FakeConfig())


def test_a_wave_error_becomes_a_tts_error(monkeypatch):
    """wave.Error is not an OSError either, and setframerate raises it for a
    non-positive rate. _wav_seconds already lists it; speak() did not."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    monkeypatch.setattr(tts, "_check_playback", lambda: None)
    monkeypatch.setattr(tts, "_synth_riva", lambda chunks, key, cfg: (b"pcm", 0))
    monkeypatch.setattr(tts, "_ADAPTERS", {"riva": tts._synth_riva})
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    with pytest.raises(TTSError):
        tts.speak("hello", FakeConfig())


def test_a_keyless_speech_endpoint_is_allowed(monkeypatch):
    """A blank tts_api_key_env means "needs no key", exactly as llm.complete
    already treats a blank api_key_env. It raised with an empty variable name
    in the message instead - and the wizard can produce this value."""
    seen = {}
    monkeypatch.setattr(tts, "_check_playback", lambda: None)
    monkeypatch.setattr(tts, "_synth_openai",
                        lambda chunks, key, cfg: seen.update(key=key) or (b"pcm", 24000))
    monkeypatch.setattr(tts, "_ADAPTERS", {"openai": tts._synth_openai})
    monkeypatch.setattr(tts, "_play", lambda path, seconds=0.0: None)

    tts.speak("hello", OpenAIConfig(tts_api_key_env="",
                                   tts_base_url="http://127.0.0.1:8080/v1",
                                   tts_model="local"))

    assert seen["key"] == ""


def test_a_named_but_unset_key_still_raises(monkeypatch):
    """The keyless case must not swallow a genuine missing key."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(tts, "_check_playback", lambda: None)

    with pytest.raises(TTSError) as exc:
        tts.speak("hello", FakeConfig())

    assert "NVIDIA_API_KEY" in str(exc.value)


def test_an_elevenlabs_voice_id_is_percent_encoded(monkeypatch):
    """A voice id is an opaque per-account string. Unencoded, a "#" in it made
    everything after it a URL fragment - dropping output_format=pcm_24000, so
    ElevenLabs returned MP3, which we wrote into a WAV header as 24 kHz PCM and
    played as loud garbage."""
    seen = {}
    resp = mock.Mock(status_code=200, content=b"pcm")
    resp.raise_for_status = lambda: None
    with mock.patch("voicelog.tts.httpx.post",
                    side_effect=lambda url, **kw: seen.update(url=url) or resp):
        tts._synth_elevenlabs(["hi"], "k", ElevenLabsConfig(tts_voice="voice#1 with spaces"))

    assert "#" not in seen["url"].split("?")[0].split("/text-to-speech/")[1]
    assert "output_format=pcm_24000" in seen["url"]


def test_a_keyless_openai_endpoint_sends_no_auth_header(monkeypatch):
    """A blank tts_api_key_env means the endpoint needs no key. Sending
    "Authorization: Bearer " is not the same as sending nothing - a local
    server can reject a malformed header outright. llm.complete omits it."""
    seen = {}
    resp = mock.Mock(status_code=200, content=b"pcm", is_success=True)
    resp.raise_for_status = lambda: None
    cfg = OpenAIConfig(tts_api_key_env="", tts_base_url="http://127.0.0.1:8080/v1")

    with mock.patch("voicelog.tts.httpx.post",
                    side_effect=lambda url, **kw: seen.update(kw) or resp):
        tts._synth_openai(["hi"], "", cfg)

    assert "Authorization" not in seen["headers"]


def test_a_keyed_openai_endpoint_still_sends_the_header(monkeypatch):
    seen = {}
    resp = mock.Mock(status_code=200, content=b"pcm", is_success=True)
    resp.raise_for_status = lambda: None

    with mock.patch("voicelog.tts.httpx.post",
                    side_effect=lambda url, **kw: seen.update(kw) or resp):
        tts._synth_openai(["hi"], "sk-real", OpenAIConfig())

    assert seen["headers"]["Authorization"] == "Bearer sk-real"


def test_a_keyless_elevenlabs_endpoint_sends_no_key_header(monkeypatch):
    seen = {}
    resp = mock.Mock(status_code=200, content=b"pcm", is_success=True)
    resp.raise_for_status = lambda: None

    with mock.patch("voicelog.tts.httpx.post",
                    side_effect=lambda url, **kw: seen.update(kw) or resp):
        tts._synth_elevenlabs(["hi"], "", ElevenLabsConfig())

    assert "xi-api-key" not in seen["headers"]
