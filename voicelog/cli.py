"""CLI entry point for voicelog."""
from __future__ import annotations

import argparse
import os
import sys

from voicelog import config as config_module
from voicelog import cache, filters, generate, gitsource, render, tts, voice, voicefile
from voicelog.config import ConfigFileNotFound
from voicelog.gitsource import NotAGitRepo
from voicelog.llm import LLMError, MissingApiKey
from voicelog.tts import TTSError


def main() -> None:
    # Casual changelogs contain em-dashes/emoji; make sure the console can show
    # them (Windows defaults to cp1252). The voice.md file is always UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="voicelog",
        description="Generate a polished release changelog from commits since the last git tag.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to changelog.yml (default: ./changelog.yml if present, else built-in defaults)",
    )
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Print the changelog but do not read it aloud",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore the cache and regenerate the changelog from the model",
    )
    args = parser.parse_args()

    # --- Load config ---
    try:
        cfg = config_module.load(args.config)
    except ConfigFileNotFound as exc:
        print(f"error: config file not found: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Read commits ---
    try:
        git_result = gitsource.read_commits(cfg.fallback_commits)
    except NotAGitRepo:
        print("error: not a git repository (run voicelog from inside a git repo)", file=sys.stderr)
        sys.exit(1)

    if git_result.used_fallback:
        print(
            f"warning: no tags found — using last {cfg.fallback_commits} commits",
            file=sys.stderr,
        )

    # --- Filter noise ---
    commits = filters.drop_noise(git_result.commits, cfg.noise)

    if not commits:
        print("Nothing to release.", file=sys.stdout)
        sys.exit(0)

    # --- Load voice samples ---
    voice_text = voice.load_voice(cfg.voice_samples)

    # --- Generate changelog (cached on the commit set to avoid churn + API cost) ---
    cwd = os.getcwd()
    key = cache.cache_key(commits, cfg.model, cfg.sections, voice_text)
    raw_markdown = None if args.fresh else cache.get(cwd, key)

    if raw_markdown is None:
        try:
            raw_markdown = generate.generate(commits, voice_text, cfg)
        except MissingApiKey as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except LLMError as exc:
            print(f"warning: LLM unavailable ({exc}) — falling back to commit list", file=sys.stderr)
            print(render.render_fallback(commits))
            sys.exit(0)
        cache.put(cwd, key, raw_markdown)

    # --- Render and output (text first, always — never gated on TTS) ---
    rendered = render.render(raw_markdown)
    print(rendered)

    # --- Persist the casual changelog to .changelog/voice.md ---
    try:
        voicefile.update_voice_md(cwd, rendered, git_result.tag)
    except OSError as exc:
        print(f"warning: could not update voice.md ({exc})", file=sys.stderr)

    # --- Speak it aloud (failure only warns, never blocks) ---
    if cfg.speak and not args.no_speak:
        try:
            tts.speak(rendered, cfg)
        except TTSError as exc:
            print(f"warning: could not speak ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
