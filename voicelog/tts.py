"""Text-to-speech — speak a changelog aloud via NVIDIA Riva TTS (cloud gRPC)."""
from __future__ import annotations

import os
import re
import tempfile
import wave


class TTSError(Exception):
    """Raised when speech synthesis or playback fails."""


# ---------------------------------------------------------------------------
# Lazy / patchable import + playback helpers
# ---------------------------------------------------------------------------

def _import_riva():
    """Import riva.client and AudioEncoding. Kept thin so tests can patch it."""
    import riva.client
    from riva.client.proto.riva_audio_pb2 import AudioEncoding

    return riva.client, AudioEncoding


def _check_playback() -> None:
    """Ensure audio playback is available (Windows-only) before synthesizing."""
    try:
        import winsound  # noqa: F401
    except ImportError as exc:  # pragma: no cover - non-Windows only
        raise TTSError("Audio playback only supported on Windows") from exc


def _play(path: str) -> None:
    """Play a WAV file synchronously. Kept thin so tests can patch it."""
    import winsound

    winsound.PlaySound(path, winsound.SND_FILENAME)


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
# Public API
# ---------------------------------------------------------------------------

def speak(text: str, config) -> None:
    """Speak ``text`` aloud via NVIDIA Riva TTS.

    Raises:
        TTSError: If Riva is unavailable, playback is unsupported, the API key
                  is missing, or synthesis fails.
    """
    try:
        riva_client, AudioEncoding = _import_riva()
    except ImportError as exc:
        raise TTSError(
            "Riva TTS not installed — run: pip install voicelog[tts]"
        ) from exc

    # Check playback availability before spending an API call.
    _check_playback()

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise TTSError(
            "Set NVIDIA_API_KEY env var — see https://build.nvidia.com"
        )

    speech = _speech_text(text)
    if not speech.strip():
        return

    chunks = _chunk_text(speech, max_len=400)
    if not chunks:
        return

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
            resp = service.synthesize(
                chunk,
                voice_name=config.tts_voice,
                language_code=config.tts_language,
                sample_rate_hz=config.tts_sample_rate,
                encoding=AudioEncoding.LINEAR_PCM,
            )
            pcm.extend(resp.audio)
    except Exception as exc:
        raise TTSError(str(exc)) from exc

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    path = tmp.name
    tmp.close()
    try:
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(config.tts_sample_rate)
            wav.writeframes(bytes(pcm))
        _play(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
