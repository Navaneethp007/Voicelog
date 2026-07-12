"""CLI entry point for voicelog."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from voicelog import config as config_module
from voicelog import cache, filters, generate, gitsource, render, tts, voice, voicefile
from voicelog.config import ConfigFileNotFound
from voicelog.gitsource import NotAGitRepo, RefNotFound
from voicelog.llm import LLMError, MissingApiKey
from voicelog.tts import TTSError


def _maybe_prompt_for_key(env_name: str) -> None:
    """First-run helper: if the key env var is empty and we're in an interactive
    terminal, ask the user to paste it and use it for this session."""
    if os.environ.get(env_name):
        return
    if not sys.stdin.isatty():
        return  # non-interactive (CI, pipe) — don't hang; let the normal error fire

    print(f"No API key found in ${env_name}.", file=sys.stderr)
    try:
        key = getpass.getpass("Paste your API key (or press Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return
    if not key:
        return
    os.environ[env_name] = key
    if os.name == "nt":
        tip = f'setx {env_name} "<your key>"  (then open a new terminal)'
    else:
        tip = f'export {env_name}=<your key>  (add to your shell profile)'
    print(f"Using it for this session. To avoid this next time: {tip}", file=sys.stderr)


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
    range_group = parser.add_mutually_exclusive_group()
    range_group.add_argument(
        "--pull",
        action="store_true",
        help="Summarise what your last git pull/merge brought in (commits since ORIG_HEAD)",
    )
    range_group.add_argument(
        "--pr",
        nargs="?",
        const=True,
        default=False,
        metavar="BASE",
        help="Summarise what your branch would put in a PR (commits since BASE, "
        "or an auto-detected base branch if BASE is omitted)",
    )
    range_group.add_argument(
        "--since",
        metavar="REF",
        default=None,
        help="Summarise commits since REF (a branch, tag, or commit), instead of since the last tag",
    )
    parser.add_argument(
        "--changelog",
        action="store_true",
        help="Also maintain the persistent release changelog at .changelog/voice.md",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Speak a fuller, more detailed rundown instead of the brief summary",
    )
    args = parser.parse_args()

    # --pull, --pr and --since all put voicelog in a transient "what changed"
    # mode that never touches the persistent release changelog.
    if args.pull:
        since = "ORIG_HEAD"
    elif args.pr is not False:
        # --pr BASE → use BASE; bare --pr → auto-detect the base branch.
        since = args.pr if isinstance(args.pr, str) else gitsource.detect_base_branch()
        if since is None:
            print(
                "error: could not detect a base branch to compare against "
                "(looked for origin/HEAD, main, master). Pass one explicitly: --pr <branch>",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Comparing the current branch against {since}…", file=sys.stderr)
    else:
        since = args.since
    diff_mode = since is not None

    # --- Load config ---
    try:
        cfg = config_module.load(args.config)
    except ConfigFileNotFound as exc:
        print(f"error: config file not found: {exc}", file=sys.stderr)
        sys.exit(1)

    # First run with no key set → offer to paste one (interactive terminals only).
    _maybe_prompt_for_key(cfg.api_key_env)

    # --- Read commits ---
    try:
        git_result = gitsource.read_commits(cfg.fallback_commits, since=since)
    except NotAGitRepo:
        print("error: not a git repository (run voicelog from inside a git repo)", file=sys.stderr)
        sys.exit(1)
    except RefNotFound as exc:
        if args.pull:
            print(
                "error: no recent pull/merge/rebase found (ORIG_HEAD is not set)",
                file=sys.stderr,
            )
        else:
            print(f"error: unknown git ref: {exc}", file=sys.stderr)
        sys.exit(1)

    if git_result.used_fallback:
        print(
            f"warning: no tags found — using last {cfg.fallback_commits} commits",
            file=sys.stderr,
        )

    # --- Filter noise ---
    commits = filters.drop_noise(git_result.commits, cfg.noise)

    if not commits:
        msg = f"No new commits since {since}." if diff_mode else "Nothing to release."
        print(msg, file=sys.stdout)
        sys.exit(0)

    # --- Cap very large ranges (keeps generation fast/cheap on big forks) ---
    if len(commits) > cfg.max_commits:
        print(
            f"warning: {len(commits)} commits — using the most recent {cfg.max_commits}",
            file=sys.stderr,
        )
        commits = commits[: cfg.max_commits]

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

    # --- Persist the casual changelog to .changelog/voice.md (opt-in) ---
    # The default run is a transient rundown. The persistent release changelog is
    # written only with --changelog (or write_changelog: true), and never in
    # diff/pull mode ("what did I just pull" is not a release).
    if (args.changelog or cfg.write_changelog) and not diff_mode:
        try:
            voicefile.update_voice_md(cwd, rendered, git_result.tag)
        except OSError as exc:
            print(f"warning: could not update voice.md ({exc})", file=sys.stderr)

    # --- Speak a short summary aloud (failure only warns, never blocks) ---
    # We speak a 2-3 sentence digest, NOT the whole changelog: reading a large
    # changelog aloud is impractical and, chunk-by-chunk, extremely slow.
    if cfg.speak and not args.no_speak:
        detail = args.detail or cfg.speech_detail == "detailed"
        # The spoken summary is cached too (keyed on the same commit set + detail),
        # so repeat runs make no LLM call at all.
        spoken = None if args.fresh else cache.get_summary(cwd, key, detail)
        if spoken is None:
            try:
                spoken = generate.summarize(rendered, cfg, detail=detail)
            except (LLMError, MissingApiKey) as exc:
                print(f"warning: could not build spoken summary ({exc}) — skipping audio", file=sys.stderr)
            else:
                cache.put_summary(cwd, key, detail, spoken)

        if spoken is not None:
            print("Speaking… (Ctrl+C to skip)", file=sys.stderr)
            try:
                tts.speak(spoken, cfg)
            except TTSError as exc:
                print(f"warning: could not speak ({exc})", file=sys.stderr)
            except KeyboardInterrupt:
                # The changelog is already printed and saved; just skip the audio.
                print("\nskipped audio.", file=sys.stderr)


if __name__ == "__main__":
    main()
