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
from urllib.parse import quote

import httpx

from voicelog import providers
from voicelog.providers import normalize_base_url
from voicelog.redact import detail


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


# How long to wait between checks while playback runs. Short enough that Ctrl+C
# feels immediate, long enough not to spin the CPU.
_PLAY_POLL_SECONDS = 0.1

# Slack added to the known duration before we consider playback finished.
# PlaySound tells us when a sound is *queued*, never when it is audible, and a
# Bluetooth device can take a noticeable moment to start.
_PLAY_GRACE_SECONDS = 1.0


def _import_winsound():
    """Import winsound. Kept thin so tests can substitute a fake player."""
    import winsound

    return winsound


def _play_windows(path: str, seconds: float) -> None:
    """Play a WAV on Windows, interruptibly. ``seconds`` is its known duration.

    A synchronous PlaySound is a blocking call into the OS, and Python can only
    deliver KeyboardInterrupt between bytecode instructions - so it queued the
    user's Ctrl+C until the audio had finished, while the CLI printed
    "Ctrl+C to skip" before every utterance. Playing asynchronously and waiting
    in slices puts the interrupt back within reach, and SND_PURGE stops sound
    that is already playing.

    **The sound must be started on this thread.** Handing PlaySound to a worker
    thread and joining it looks tidier - the thread returning would *be* the
    completion signal, with no duration to know - but SND_PURGE only acts on
    the thread that started the sound. Measured: purging another thread's sound
    blocks for the remainder of the audio (9.44s of a 10s file) instead of
    stopping it, so Ctrl+C became a no-op that merely waited. From this thread
    the same call returns in 0.01s and actually stops.

    The duration is passed in rather than read back from the header: speak()
    wrote the PCM and knows it exactly. Re-reading meant a file whose header we
    could not parse fell through to a synchronous play - reinstating the very
    bug this exists to fix.
    """
    winsound = _import_winsound()
    try:
        # Inside the try: an interrupt arriving between starting the sound and
        # reaching the loop would otherwise escape the purge, leaving audio
        # playing under a 'skipped audio' message and a temp WAV that speak()
        # cannot delete while it is still open.
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        # Timed from after the call, not before: whatever was spent queueing
        # the sound would otherwise count against the audio's own length.
        deadline = time.monotonic() + seconds + _PLAY_GRACE_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_PLAY_POLL_SECONDS, remaining))
    except KeyboardInterrupt:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001
            # A failing purge must not replace the interrupt: raising here would
            # discard the KeyboardInterrupt and cli._speak would never print
            # "skipped audio". The worst case is audio that plays on, which is
            # exactly where we started.
            pass
        raise


def _play(path: str, seconds: float = 0.0) -> None:
    """Play a WAV file on the current OS, blocking until it ends.

    ``seconds`` is the audio's known duration; only Windows needs it, because
    only there do we have to decide for ourselves when playback is over.
    """
    system = platform.system()
    if system == "Windows":
        _play_windows(path, seconds)
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

    timeout = config.tts_timeout
    rate = config.tts_sample_rate
    try:
        auth = riva_client.Auth(
            uri=providers.TTS_PROVIDERS["riva"].base_url,
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
        raise TTSError(detail(str(exc), api_key)) from exc

    return bytes(pcm), rate


def _synth_openai(chunks: list[str], api_key: str, config) -> tuple[bytes, int]:
    # Defaults come from the preset, not from literals here: these are facts
    # about the backend, and a second copy of them in this module is how "the
    # default speech model" ended up with no single home.
    preset = providers.TTS_PROVIDERS["openai"]
    base = normalize_base_url(config.tts_base_url or preset.base_url)
    model = config.tts_model or preset.model
    voice = config.tts_voice or preset.voices[0]
    timeout = config.tts_timeout
    url = f"{base}/audio/speech"
    # Omitted entirely when there is no key, rather than sent empty: a blank
    # tts_api_key_env means the endpoint needs none, and "Authorization: Bearer "
    # is a malformed header a local server may reject outright. Not
    # providers.headers(), which also sets Accept: application/json - correct
    # for the JSON APIs it serves, wrong to start sending to an audio endpoint.
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

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
                    f"{detail(resp.text, api_key)}"
                )
            pcm.extend(resp.content)
        except TTSError:
            raise
        except httpx.HTTPError as exc:
            raise TTSError(f"OpenAI TTS request failed: {exc}") from exc

    # OpenAI 'pcm' is 24 kHz, 16-bit, mono.
    return bytes(pcm), preset.sample_rate


def _synth_elevenlabs(chunks: list[str], api_key: str, config) -> tuple[bytes, int]:
    voice_id = config.tts_voice
    if not voice_id:
        raise TTSError("ElevenLabs needs tts_voice set to a voice id")
    preset = providers.TTS_PROVIDERS["elevenlabs"]
    model = config.tts_model or preset.model
    timeout = config.tts_timeout
    base = normalize_base_url(config.tts_base_url or preset.base_url)
    url = (
        f"{base}/text-to-speech/{quote(voice_id, safe='')}"
        f"?output_format=pcm_{preset.sample_rate}"
    )
    headers = {"xi-api-key": api_key} if api_key else {}

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
                    f"{detail(resp.text, api_key)}"
                )
            pcm.extend(resp.content)
        except TTSError:
            raise
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs request failed: {exc}") from exc

    return bytes(pcm), preset.sample_rate


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

    provider = config.tts_provider.lower()
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        # The list comes from the registry, so it can name every value the
        # wizard and --help offer. Built from _ADAPTERS it could not even
        # mention `none`, which is a legitimate answer it has no adapter for.
        raise TTSError(
            f"unknown tts_provider '{provider}' — use one of: "
            f"{', '.join(sorted(providers.TTS_PROVIDERS))}"
        )

    # Fail before spending an API call if we can't play audio here.
    _check_playback()

    # A blank name means the endpoint needs no key - a local OpenAI-compatible
    # server, say. llm.complete has always read it that way; this did not, and
    # raised "Set the  environment variable" with an empty name. The wizard can
    # produce a blank value, so it was reachable.
    env_name = config.tts_api_key_env
    api_key = os.environ.get(env_name, "") if env_name else ""
    if env_name and not api_key:
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
        # 16-bit mono, so two bytes per frame. speak() wrote it, so this is
        # exact - no header to re-read and no unparseable case to fall back
        # from.
        _play(path, len(pcm) / 2 / rate if rate else 0.0)
    except (
        OSError,
        subprocess.CalledProcessError,   # POSIX players, run with check=True
        RuntimeError,                    # winsound.PlaySound: "Failed to play sound"
        ImportError,                     # a Windows build without winsound
        wave.Error,                      # e.g. setframerate() on a non-positive rate
    ) as exc:
        # Playback failure (busy/absent audio device, player vanished, etc.) —
        # never let this crash the CLI; the text output already succeeded.
        #
        # The tuple used to be OSError + CalledProcessError, which is shaped for
        # the POSIX branch and covers almost nothing the Windows branch raises:
        # none of RuntimeError, ImportError or wave.Error is an OSError, so each
        # escaped here, escaped cli._speak's `except TTSError`, and tracebacked
        # out of main() after the changelog had printed and been paid for.
        raise TTSError(f"audio playback failed: {exc}") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
