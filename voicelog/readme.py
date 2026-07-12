"""Load a project's README as onboarding context for `voicelog --new`."""
from __future__ import annotations

import os

_CANDIDATES = ["README.md", "README.rst", "README.txt", "README"]


def load_readme(dir_path: str, max_chars: int = 4000) -> str:
    """Return the repo's README text, truncated to max_chars.

    Tries common filenames in order (README.md preferred). Returns "" if the
    directory or none of the candidates exist — onboarding still proceeds
    using just the recent commits.
    """
    if not os.path.isdir(dir_path):
        return ""

    for name in _CANDIDATES:
        path = os.path.join(dir_path, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            text = text.strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n... (truncated)"
            return text

    return ""
