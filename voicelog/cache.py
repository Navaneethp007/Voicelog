"""Generation cache — avoid re-calling the LLM (and re-churning voice.md) when
the commit set hasn't changed.

The cache is a single-entry JSON file in the repo's git directory
(``.git/voicelog/cache.json``; see ``_cache_path``), holding the key of the last
generation, its raw model output, and the spoken
summaries derived from it (per detail level). Re-running with the same commits +
model + sections returns the cached text — and the cached spoken summary —
instead of making fresh (nondeterministic, paid) API calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass

from voicelog import state
from voicelog.models import Commit


# Kept for repos created by older versions, and as the fallback when there is
# no git directory to write into.
_LEGACY_RELPATH = os.path.join(".changelog", ".voicelog-cache.json")


def _cache_path(repo_dir: str) -> str:
    """Where this repo's generation cache lives.

    Inside a git repo: the repo's own git directory, so the cache needs no
    .gitignore entry in anybody's project, cannot be committed by accident, and
    is the same file from any subdirectory. ``repo_dir`` is then unused.

    Outside one: the historical location under ``repo_dir``. That keeps this
    function total - every caller always has somewhere to write - and voicelog
    requires a git repo in practice, so it is a fallback rather than a path
    anyone takes.
    """
    private = state.private_path("cache.json", repo_dir)
    return private if private else os.path.join(repo_dir, _LEGACY_RELPATH)


def _read(repo_dir: str) -> dict:
    try:
        with open(_cache_path(repo_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(repo_dir: str, data: dict) -> bool:
    """Persist the cache. Returns whether it worked; never raises.

    Two properties, both learned the hard way:

    - **Never raises.** ``put`` runs after a paid model call and before the
      changelog is printed, so an unwritable location used to turn a completed
      generation into a traceback with no output whatsoever.
    - **Atomic.** ``put_summary`` rewrites the entry *including* the markdown,
      so an interrupt mid-write would truncate a generation already paid for.
    """
    path = _cache_path(repo_dir)
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(
            f"warning: could not write the generation cache ({exc}); "
            "this run will not be reused.",
            file=sys.stderr,
        )
        return False
    return True


def _slot(detail: bool) -> str:
    return "detailed" if detail else "brief"


def cache_key(
    commits: list[Commit],
    model: str,
    sections: list[str],
    voice_text: str,
    with_diff: bool = False,
    *,
    provider: str = "",
    base_url: str = "",
) -> str:
    """Stable key for a generation request.

    Keyed on the SET of commit hashes (order-independent), the model, the ordered
    section list, the voice-sample text, and whether a code diff was included
    (``with_diff`` — different prompt content, so a diff run and a no-diff run
    for the same commits must never share cached output). Changing any of these
    invalidates the cache.

    The provider identity counts too: the same model id can be served by two
    different endpoints (``gpt-4o-mini`` direct versus through OpenRouter, or a
    local proxy), and those are not interchangeable outputs. Both are
    keyword-only with blank defaults so existing positional call sites keep
    working.
    """
    hashes = sorted(c.hash for c in commits)
    payload = json.dumps(
        {
            "commits": hashes,
            "model": model,
            "provider": provider,
            "base_url": base_url,
            "sections": list(sections),
            "voice": voice_text,
            "with_diff": with_diff,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LastEntry:
    """The last cached generation, whatever key it was stored under."""

    markdown: str
    summaries: dict[str, str]

    def summary(self, detail: bool) -> str | None:
        """The cached spoken text for this detail level, or None."""
        return self.summaries.get(_slot(detail))


def last(repo_dir: str) -> LastEntry | None:
    """The most recent cached entry, REGARDLESS of key - for ``--replay`` only.

    Every other reader is key-gated on purpose: ``get``/``get_summary`` answer
    "may I skip generation for *these* commits", and the key is the whole
    answer. This one answers a different question - "what did I last cache
    here" - which has no key to check against, because a replay deliberately
    never computes one: doing so would mean reading commits, filtering them,
    applying the cap and loading the voice samples, i.e. the entire pipeline a
    replay exists to skip.

    **Never use this to decide whether generation can be skipped.** It will
    hand you output for a different commit set, a different model and a
    different provider without complaint. (It returns a LastEntry rather than a
    string partly for that reason: it cannot be dropped in where ``get`` was
    required without failing immediately.)

    One read, so the markdown and its summary always come from the same
    snapshot - ``put_summary`` is a read-modify-write, so two separate key-less
    reads could disagree and speak a summary belonging to text never printed.
    """
    data = _read(repo_dir)
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return None
    raw = data.get("summaries")
    # Shape-checked because there is no key gate to fail first, and this
    # command's whole promise is that it never fails, it just re-prints.
    summaries = (
        {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw, dict)
        else {}
    )
    return LastEntry(markdown=markdown, summaries=summaries)


def get(repo_dir: str, key: str) -> str | None:
    """Return the cached changelog markdown for ``key``, or None on miss."""
    data = _read(repo_dir)
    if data.get("key") == key:
        return data.get("markdown")
    return None


def put(repo_dir: str, key: str, markdown: str) -> bool:
    """Store ``markdown`` under ``key`` (single-entry; resets cached summaries).

    Returns whether it was stored. Callers may ignore that - a cache miss next
    time is the only consequence - but it must never raise; see ``_write``.
    """
    return _write(repo_dir, {"key": key, "markdown": markdown, "summaries": {}})


def get_summary(repo_dir: str, key: str, detail: bool) -> str | None:
    """Return the cached spoken summary for ``key``+``detail``, or None on miss."""
    data = _read(repo_dir)
    if data.get("key") != key:
        return None
    return data.get("summaries", {}).get(_slot(detail))


def put_summary(repo_dir: str, key: str, detail: bool, text: str) -> bool:
    """Cache a spoken summary. No-op if the stored entry is for a different key."""
    data = _read(repo_dir)
    if data.get("key") != key:
        return False  # markdown for this key isn't cached; nothing to attach to
    data.setdefault("summaries", {})[_slot(detail)] = text
    return _write(repo_dir, data)
