"""Configuration loading for voicelog."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class ConfigFileNotFound(Exception):
    """Raised when an explicitly-supplied config path does not exist."""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "provider": "nvidia",
    "base_url": "https://integrate.api.nvidia.com/v1",
    "model": "mistralai/mistral-medium-3.5-128b",
    "sections": ["Breaking", "Features", "Fixes", "Internal"],
    "noise": [r"^wip", r"^merge", r"^fmt", r"^chore\(deps\)"],
    "voice_samples": ".changelog/voice/",
    "fallback_commits": 50,
    # --- Phase 2: speech + persistent casual changelog ---
    "speak": True,  # always speak (degrades gracefully if TTS unavailable)
    "tts_function_id": "877104f7-e885-42b9-8de8-f6e4c6303969",  # magpie-tts-multilingual
    "tts_voice": "Magpie-Multilingual.EN-US.Sofia",
    "tts_language": "en-US",
    "tts_sample_rate": 44100,
    "voice_md": ".changelog/voice.md",
}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class Config:
    provider: str
    base_url: str
    model: str
    sections: list[str]
    noise: list[str]
    voice_samples: str
    fallback_commits: int
    speak: bool
    tts_function_id: str
    tts_voice: str
    tts_language: str
    tts_sample_rate: int
    voice_md: str


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load(path: str | None) -> Config:
    """Load configuration and return a Config instance.

    - path=None  → look for changelog.yml in cwd; if absent, use DEFAULTS silently.
    - path given → if file missing, raise ConfigFileNotFound; otherwise parse and
                   shallow-merge over DEFAULTS (file values win).
    """
    if path is None:
        candidate = os.path.join(os.getcwd(), "changelog.yml")
        if not os.path.isfile(candidate):
            return _build(DEFAULTS)
        path = candidate
    else:
        if not os.path.isfile(path):
            raise ConfigFileNotFound(path)

    with open(path, encoding="utf-8") as fh:
        file_data: dict[str, Any] = yaml.safe_load(fh) or {}

    merged = {**DEFAULTS, **file_data}
    return _build(merged)


def _build(data: dict[str, Any]) -> Config:
    return Config(
        provider=data["provider"],
        base_url=data["base_url"],
        model=data["model"],
        sections=list(data["sections"]),
        noise=list(data["noise"]),
        voice_samples=data["voice_samples"],
        fallback_commits=int(data["fallback_commits"]),
        speak=bool(data["speak"]),
        tts_function_id=data["tts_function_id"],
        tts_voice=data["tts_voice"],
        tts_language=data["tts_language"],
        tts_sample_rate=int(data["tts_sample_rate"]),
        voice_md=data["voice_md"],
    )
