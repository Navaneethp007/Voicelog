"""Load voice samples from a directory of .md / .txt files."""
import os


def load_voice(dir_path: str) -> str:
    """Read up to 3 .md or .txt files from dir_path and return a few-shot style string.

    Returns "" if the directory does not exist or contains no matching files.
    """
    if not os.path.isdir(dir_path):
        return ""

    files = sorted(
        f for f in os.listdir(dir_path)
        if f.endswith(".md") or f.endswith(".txt")
    )

    if not files:
        return ""

    files = files[:3]

    parts = ["Here are past changelogs to imitate the voice of:"]
    for i, filename in enumerate(files, start=1):
        filepath = os.path.join(dir_path, filename)
        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()
        parts.append(f"\n--- sample {i} ---\n{content}")

    return "\n".join(parts)
