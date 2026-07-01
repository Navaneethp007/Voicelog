"""Noise-filtering utilities for voicelog."""
from __future__ import annotations

import re

from voicelog.models import Commit


def drop_noise(commits: list[Commit], patterns: list[str]) -> list[Commit]:
    """Return commits whose subject does NOT match any of the given regex patterns.

    Matching is case-insensitive. If patterns is empty, all commits are returned.
    """
    if not patterns:
        return list(commits)

    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    return [
        commit
        for commit in commits
        if not any(pattern.search(commit.subject) for pattern in compiled)
    ]
