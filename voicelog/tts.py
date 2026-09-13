"""Text-to-speech — speak a changelog aloud.

Provider-aware: ``tts_provider`` selects the engine (NVIDIA Riva, OpenAI, or
ElevenLabs). Each adapter returns raw 16-bit PCM; shared code stitches it into
one WAV and plays it cross-platform (Windows/macOS/Linux).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import wave

import httpx

from voicelog.redact import redact


class TTSError(Exception):
    """Raised when speech synthesis or playback fails."""


# ---------------------------------------------------------------------------
# Cross-platform playback
# ---------------------------------------------------------------------------

def _linux_player() -> list[str] | None:
    for cmd in (["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


def _check_playback() -> None:
    """Verify an audio player is available before spending an API call."""
    system = platform.system()
    if system == "Windows":
        try:
            import winsound  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise TTSError("winsound is unavailable on this Windows build") from exc
    elif system == "Darwin":
        if not shutil.which("afplay"):  # pragma: no cover - macOS only
            raise TTSError("no audio player found ('afplay' missing)")
    else:
        if _linux_player() is None:  # pragma: no cover - linux only
            raise TTSError(
                "no audio player found — install one of: pulseaudio (paplay), "
                "alsa-utils (aplay), or ffmpeg (ffplay)"
            )


# How long to wait between checks while asynchronous playback runs. Short
# enough that Ctrl+C feels immediate, long enough not to spin the CPU.
_PLAY_POLL_SECONDS = 0.1

# Audio may still be draining when the computed duration elapses; this much
# grace keeps the last syllable from being cut by the temp file's deletion.
_PLAY_GRACE_SECONDS = 0.25


def _import_winsound():
    """Import winsound. Kept thin so tests can substitute a fake player."""
    import winsound

    return winsound


def _wav_seconds(path: str) -> float:
    """How long a WAV runs for, or 0.0 when that cannot be determined."""
    try:
        with wave.open(path, "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error, EOFError):
        # EOFError is what a truncated file raises, and it is not an OSError.
        return 0.0


def _play_windows(path: str) -> None:
    """Play a WAV on Windows, interruptibly.

    PlaySound is a blocking call into the OS, and Python can only deliver
    KeyboardInterrupt between bytecode instructions - so a synchronous play
    queued the user's Ctrl+C until the audio had finished on its own, and the
    handler then reported "skipped audio" for something fully played. The CLI
    prints "Ctrl+C to skip" before every utterance, so that was a promise the
    code did not keep.

    Playing asynchronously and waiting in slices puts the interrupt back within
    reach; SND_PURGE stops sound that is already playing, which is what makes it
    a skip rather than a delayed acknowledgement. POSIX needs none of this: Ctrl+C
    reaches the whole process group, so afplay/aplay are signalled directly.
    """
    winsound = _import_winsound()
    seconds = _wav_seconds(path)
    if not seconds:
        # No duration to wait out. A plain synchronous play is still better than
        # not playing at all - it just cannot be interrupted.
        winsound.PlaySound(path, winsound.SND_FILENAME)
        return

    deadline = time.monotonic() + seconds + _PLAY_GRACE_SECONDS
    try:
        # Inside the try: an interrupt arriving between starting the sound
        # and reaching the loop would otherwise escape the purge, leaving
        # audio playing under a 'skipped audio' message and a temp WAV that
        # speak() cannot delete while it is still open.
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_PLAY_POLL_SECONDS, remaining))
    except KeyboardInterrupt:
        winsound.PlaySound(None, winsound.SND_PURGE)
        raise


def _play(path: str) -> None:
    """Play a WAV file synchronously on the current OS. Patchable in tests."""
    system = platform.system()
    if system == "Windows":
        _play_windows(path)
        return
    if system == "Darwin":
        subprocess.run(["afplay", path], check=True)
        return
    player = _linux_player()
    if player is None:  # pragma: no cover
        raise TTSError("no audio player found")
    subprocess.run([*player, path], check=True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _speech_text(markdown: str) -> str:
    """Convert markdown to clean spoken text."""
    text = markdown

    # Remove fenced code blocks entirely.
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)

    # Links: [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)

    lines: list[str] = []
    for line in text.splitlines():
        # Strip heading markers.
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        # Strip leading bullet markers.
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        # Strip inline backticks and emphasis markers, keeping inner words.
        line = line.replace("`", "")
        line = re.sub(r"[*_]", "", line)
        lines.append(line.rstrip())

    text = "\n".join(lines)
    # Collapse excess blank lines.
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def _chunk_text(text: str, max_len: int = 400) -> list[str]:
    """Split text into chunks <= max_len, breaking at sentence/word boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        split_at = -1
        # Prefer a sentence boundary.
        for sep in (". ", "! ", "? ", "\n"):
            idx = window.rfind(sep)
            if idx > split_at:
                split_at = idx + len(sep)
        if split_at <= 0:
            # Fall back to the last whitespace before max_len.
            idx = window.rfind(" ")
            split_at = idx + 1 if idx > 0 else max_len
        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Provider adapters — each returns (pcm_bytes, sample_rate_hz)
# ---------------------------------------------------------------------------

def _import_riva():
    """Import riva.client and AudioEncoding. Kept thin so tests can patch it."""
    import riva.client
    from riva.client.proto.riva_audio_pb2 import AudioEncoding

    return riva.client, AudioEncoding


def _synth_riva(chunks: list[str], api_key: str, config) -> tuple[bytes, int]:
    try:
        riva_client, AudioEncoding = _import_riva()
    except ImportError as exc:
        raise TTSError(
            "Riva TTS not installed — run: pip install voicelog[tts]"
        ) from exc

    import grpc  # present whenever riva.client imported

    timeout = getattr(config, "tts_timeout", 90.0)
    rate = config.tts_sample_rate
    try:
        auth = riva_client.Auth(
            uri="grpc.nvcf.nvidia.com:443",
            use_ssl=True,
            metadata_args=[
                ["function-id", config.tts_function_id],
                ["authorization", f"Bearer {api_key}"],
            ],
        )
        service = riva_client.SpeechSynthesisService(auth)

        pcm = bytearray()
        for chunk in chunks:
            # Async future path so we can enforce a client-side deadline — riva's
            # synchronous synthesize() has no timeout and can block forever.
            call = service.synthesize(
                chunk,
                voice_name=config.tts_voice,
                language_code=config.tts_language,
                sample_rate_hz=rate,
                encoding=AudioEncoding.LINEAR_PCM,
                future=True,
            )
            try:
                resp = call.result(timeout=timeout)
            except grpc.FutureTimeoutError as exc:
                call.cancel()
                raise TTSError(
                    f"speech synthesis timed out after {timeout:.0f}s — the TTS "
                    f"service was too slow. Try again, raise tts_timeout, or use "
                    f"--no-speak."
                ) from exc
            pcm.extend(resp.audio)
    except TTSError:
        raise
    except Exception as exc:
        # Broad by design, and it wraps the Auth construction that is handed the
        # key - so scrub before the message escapes.
        raise TTSError(redact(str(exc), api_key)) from exc

    return bytes(pcm), rate


def _synth_openai(chunks: list[str], api_key: str, config) -> tuple[bytes, int]:
    base = (getattr(config, "tts_base_url", "") or "https://api.openai.com/v1").rstrip("/")
    model = getattr(config, "tts_model", "") or "gpt-4o-mini-tts"
    voice = config.tts_voice or "alloy"
    timeout = getattr(config, "tts_timeout", 90.0)
    url = f"{base}/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}"}

    pcm = bytearray()
    for chunk in chunks:
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json={"model": model, "voice": voice, "input": chunk, "response_format": "pcm"},
                timeout=timeout,
            )
            if not resp.is_success:
                # Redact before truncating: cutting first strands a key
                # prefix that no later replace() can match.
                raise TTSError(
                    f"OpenAI TTS HTTP {resp.status_code}: "
                    f"{redact(resp.text, api_key)[:200]}"
                )
            pcm.extend(resp.content)
        except TTSError:
            raise
        except httpx.HTTPError as exc:
            raise TTSError(f"OpenAI TTS request failed: {exc}") from exc

    # OpenAI 'pcm' is 24 kHz, 16-bit, mono.
    return bytes(pcm), 24000


def _synth_elevenlabs(chunks: list[str], api_key: str, config) -> tuple[bytes, int]:
    voice_id = config.tts_voice
    if not voice_id:
        raise TTSError("ElevenLabs needs tts_voice set to a voice id")
    model = getattr(config, "tts_model", "") or "eleven_multilingual_v2"
    timeout = getattr(config, "tts_timeout", 90.0)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
    headers = {"xi-api-key": api_key}

    pcm = bytearray()
    for chunk in chunks:
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json={"text": chunk, "model_id": model},
                timeout=timeout,
            )
            if not resp.is_success:
                raise TTSError(
                    f"ElevenLabs HTTP {resp.status_code}: "
                    f"{redact(resp.text, api_key)[:200]}"
                )
            pcm.extend(resp.content)
        except TTSError:
            raise
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs request failed: {exc}") from exc

    return bytes(pcm), 24000


_ADAPTERS = {
    "riva": _synth_riva,
    "openai": _synth_openai,
    "elevenlabs": _synth_elevenlabs,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def speak(text: str, config) -> None:
    """Speak ``text`` aloud using the configured TTS provider.

    Raises:
        TTSError: unknown provider, missing key, no audio player, or synthesis
                  failure. (Callers treat this as best-effort — audio never
                  blocks the text output.)
    """
    speech = _speech_text(text)
    if not speech.strip():
        return
    chunks = _chunk_text(speech, max_len=400)
    if not chunks:
        return

    provider = getattr(config, "tts_provider", "riva").lower()
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise TTSError(
            f"unknown tts_provider '{provider}' — use one of: "
            f"{', '.join(sorted(_ADAPTERS))}"
        )

    # Fail before spending an API call if we can't play audio here.
    _check_playback()

    env_name = getattr(config, "tts_api_key_env", "NVIDIA_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise TTSError(
            f"Set the {env_name} environment variable with your {provider} TTS key."
        )

    pcm, rate = adapter(chunks, api_key, config)
    if not pcm:
        return

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    path = tmp.name
    tmp.close()
    try:
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit PCM
            wav.setframerate(rate)
            wav.writeframes(pcm)
        _play(path)
    except (OSError, subprocess.CalledProcessError) as exc:
        # Playback failure (busy/absent audio device, player vanished, etc.) —
        # never let this crash the CLI; the text output already succeeded.
        raise TTSError(f"audio playback failed: {exc}") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
