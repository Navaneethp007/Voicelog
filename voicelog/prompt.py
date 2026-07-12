from __future__ import annotations

from voicelog.models import Commit


def build_prompt(
    commits: list[Commit],
    voice_text: str,
    sections: list[str],
) -> list[dict]:
    """Return an OpenAI-format message list for release-note generation.

    Returns:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    sections_list = "\n".join(f"- {s}" for s in sections)

    system_content = (
        "You are voicelog — a fun, friendly narrator who tells a developer what "
        "changed in their project, like a buddy catching them up over coffee. "
        "Your notes will be read ALOUD, so write in a warm, casual, conversational "
        "tone — upbeat and human, never stiff or corporate. A little personality and "
        "the occasional bit of wit is welcome; keep it concise and skimmable.\n\n"
        f"Still organise what changed into these sections:\n{sections_list}\n\n"
        "Conventional-commit prefixes (feat:, fix:, chore:, etc.) are a HINT to help "
        "you categorise commits, but they are not required and may be absent or wrong. "
        "Use your judgement.\n\n"
        "Output FINISHED Markdown only — no preamble, no explanation, no commentary. "
        "Use `## ` for the top heading and `### ` for each section. "
        "Omit any section that has nothing to show."
    )

    # Build the commit block
    commit_lines: list[str] = []
    for commit in commits:
        commit_lines.append(f"Subject: {commit.subject}")
        if commit.body:
            commit_lines.append(f"Body: {commit.body}")
        if commit.files:
            files_str = ", ".join(commit.files)
            commit_lines.append(f"Files: {files_str}")
        commit_lines.append("")  # blank separator between commits

    commits_block = "\n".join(commit_lines).rstrip()

    # Build user message
    user_parts: list[str] = []

    if voice_text:
        user_parts.append(
            f"The author's voice notes for this release:\n{voice_text}"
        )

    user_parts.append(f"Commits:\n{commits_block}")

    user_content = "\n\n".join(user_parts)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def build_summary_prompt(changelog_markdown: str, detail: bool = False) -> list[dict]:
    """Return messages that turn a full changelog into a SPOKEN digest.

    The result is read aloud by a text-to-speech engine, so it must be plain
    text — no markdown, no bullet lists, no headings. ``detail`` controls length:
    brief (a couple of sentences) or detailed (a fuller walkthrough).
    """
    common = (
        "You are voicelog's narrator. Turn the changelog below into a summary to "
        "be READ ALOUD by a text-to-speech engine.\n\n"
        "Always:\n"
        "- Plain text only — NO markdown, NO bullet points, NO headings, NO code.\n"
        "- Warm and casual, like telling a teammate what changed.\n"
        "- Output ONLY the sentences to be spoken — no preamble.\n"
    )
    if detail:
        length_rules = (
            "Length: give a DETAILED spoken rundown — walk through each notable "
            "change, roughly one sentence per item, grouped by area (features, "
            "fixes, and so on). Be thorough but keep every sentence speakable."
        )
    else:
        length_rules = (
            "Length: 2 to 3 short spoken sentences, never more. Lead with the shape "
            "of the release (e.g. how many features and fixes), then name one or two "
            "highlights."
        )
    system_content = common + length_rules
    user_content = f"Changelog:\n{changelog_markdown}"

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
