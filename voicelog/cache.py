"""Generation cache — avoid re-calling the LLM (and re-churning voice.md) when
the commit set hasn't changed.

The cache is a single-entry JSON file at ``.changelog/.voicelog-cache.json``
holding the key of the last generation and its raw model output. Re-running with
the same commits + model + sections returns the cached text instead of making a
fresh (nondeterministic, paid) API call.
"""
from __future__ import annotations

import hashlib
import json
import os

from voicelog.models import Commit


def _cache_path(repo_dir: str) -> str:
    return os.path.join(repo_dir, ".changelog", ".voicelog-cache.json")


def cache_key(commits: list[Commit], model: str, sections: list[str], voice_text: str) -> str:
    """Stable key for a generation request.

    Keyed on the SET of commit hashes (order-independent), the model, the ordered
    section list, and the voice-sample text (which is injected into the prompt).
    Changing any of these invalidates the cache.
    """
    hashes = sorted(c.hash for c in commits)
    payload = json.dumps(
        {
            "commits": hashes,
            "model": model,
            "sections": list(sections),
            "voice": voice_text,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(repo_dir: str, key: str) -> str | None:
    """Return the cached markdown for ``key``, or None on miss / unreadable cache."""
    path = _cache_path(repo_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("key") == key:
        return data.get("markdown")
    return None


def put(repo_dir: str, key: str, markdown: str) -> None:
    """Store ``markdown`` under ``key`` (single-entry; overwrites any previous)."""
    path = _cache_path(repo_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"key": key, "markdown": markdown}, fh)
