from __future__ import annotations

from voicelog.models import Commit


def build_prompt(
    commits: list[Commit],
    voice_text: str,
    sections: list[str],
    diff: str | None = None,
) -> list[dict]:
    """Return an OpenAI-format message list for release-note generation.

    ``diff`` is opt-in (see ``gitsource.read_commits(with_diff=True)``): when
    provided (non-empty), the actual code diff for the range is included so the
    model can write richer notes even when commit messages are vague. Omitted by
    default — commit messages + diffstat only, no code sent anywhere.

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
    if diff:
        system_content += (
            "\n\nA code diff for this range is included below. Use it to understand "
            "what actually changed when commit messages are vague — but describe the "
            "changes in plain language; never quote raw diff syntax or code."
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
        if commit.insertions or commit.deletions:
            commit_lines.append(
                f"Changes: +{commit.insertions}/-{commit.deletions} lines"
            )
        commit_lines.append("")  # blank separator between commits

    commits_block = "\n".join(commit_lines).rstrip()

    # Build user message
    user_parts: list[str] = []

    if voice_text:
        user_parts.append(
            f"The author's voice notes for this release:\n{voice_text}"
        )

    user_parts.append(f"Commits:\n{commits_block}")

    if diff:
        user_parts.append(f"Code diff for this range:\n{diff}")

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


def build_onboarding_prompt(
    readme_text: str,
    commits: list[Commit],
    diff: str | None = None,
) -> list[dict]:
    """Return messages that orient a developer who just cloned this repo.

    Combines the README (what the project IS) with recent commit activity
    (what's actively being worked on) into a casual, spoken-friendly overview.
    ``diff`` is opt-in — see :func:`build_prompt` for the same convention.

    Returns:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    system_content = (
        "You are voicelog — a fun, friendly guide orienting a developer who just "
        "cloned this repository for the first time. Your notes will be read ALOUD, "
        "so write in a warm, casual, conversational tone.\n\n"
        "Cover two things, in order:\n"
        "1. What this project IS — summarise the README in plain terms: what it "
        "does, who it's for.\n"
        "2. What's been happening lately — summarise the recent commit activity: "
        "what's actively being worked on.\n\n"
        "If the README is missing or unhelpful, infer the project's purpose from "
        "the commit subjects and file paths instead — say so if you're guessing.\n\n"
        "Output FINISHED Markdown only — no preamble, no explanation. Use `## ` "
        "for each of the two sections (e.g. `## What this is`, `## Recent activity`)."
    )
    if diff:
        system_content += (
            "\n\nA code diff for the recent commits is included below. Use it for "
            "extra context if commit messages are vague — describe changes in plain "
            "language; never quote raw diff syntax or code."
        )

    readme_block = readme_text.strip() if readme_text else "(no README found)"

    commit_lines: list[str] = []
    for commit in commits:
        commit_lines.append(f"Subject: {commit.subject}")
        if commit.body:
            commit_lines.append(f"Body: {commit.body}")
        if commit.files:
            commit_lines.append(f"Files: {', '.join(commit.files)}")
        if commit.insertions or commit.deletions:
            commit_lines.append(
                f"Changes: +{commit.insertions}/-{commit.deletions} lines"
            )
        commit_lines.append("")
    commits_block = "\n".join(commit_lines).rstrip() or "(no recent commits)"

    user_parts = [f"README:\n{readme_block}", f"Recent commits:\n{commits_block}"]
    if diff:
        user_parts.append(f"Code diff for these commits:\n{diff}")
    user_content = "\n\n".join(user_parts)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
