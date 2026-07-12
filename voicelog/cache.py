"""Generation cache — avoid re-calling the LLM (and re-churning voice.md) when
the commit set hasn't changed.

The cache is a single-entry JSON file at ``.changelog/.voicelog-cache.json``
holding the key of the last generation, its raw model output, and the spoken
summaries derived from it (per detail level). Re-running with the same commits +
model + sections returns the cached text — and the cached spoken summary —
instead of making fresh (nondeterministic, paid) API calls.
"""
from __future__ import annotations

import hashlib
import json
import os

from voicelog.models import Commit


def _cache_path(repo_dir: str) -> str:
    return os.path.join(repo_dir, ".changelog", ".voicelog-cache.json")


def _read(repo_dir: str) -> dict:
    try:
        with open(_cache_path(repo_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(repo_dir: str, data: dict) -> None:
    path = _cache_path(repo_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh)


def _slot(detail: bool) -> str:
    return "detailed" if detail else "brief"


def cache_key(
    commits: list[Commit],
    model: str,
    sections: list[str],
    voice_text: str,
    with_diff: bool = False,
) -> str:
    """Stable key for a generation request.

    Keyed on the SET of commit hashes (order-independent), the model, the ordered
    section list, the voice-sample text, and whether a code diff was included
    (``with_diff`` — different prompt content, so a diff run and a no-diff run
    for the same commits must never share cached output). Changing any of these
    invalidates the cache.
    """
    hashes = sorted(c.hash for c in commits)
    payload = json.dumps(
        {
            "commits": hashes,
            "model": model,
            "sections": list(sections),
            "voice": voice_text,
            "with_diff": with_diff,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(repo_dir: str, key: str) -> str | None:
    """Return the cached changelog markdown for ``key``, or None on miss."""
    data = _read(repo_dir)
    if data.get("key") == key:
        return data.get("markdown")
    return None


def put(repo_dir: str, key: str, markdown: str) -> None:
    """Store ``markdown`` under ``key`` (single-entry; resets cached summaries)."""
    _write(repo_dir, {"key": key, "markdown": markdown, "summaries": {}})


def get_summary(repo_dir: str, key: str, detail: bool) -> str | None:
    """Return the cached spoken summary for ``key``+``detail``, or None on miss."""
    data = _read(repo_dir)
    if data.get("key") != key:
        return None
    return data.get("summaries", {}).get(_slot(detail))


def put_summary(repo_dir: str, key: str, detail: bool, text: str) -> None:
    """Cache a spoken summary. No-op if the stored entry is for a different key."""
    data = _read(repo_dir)
    if data.get("key") != key:
        return  # markdown for this key isn't cached; nothing to attach to
    data.setdefault("summaries", {})[_slot(detail)] = text
    _write(repo_dir, data)
