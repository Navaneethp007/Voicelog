from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Commit:
    hash: str
    subject: str
    body: str
    author: str
    files: list[str] = field(default_factory=list)
