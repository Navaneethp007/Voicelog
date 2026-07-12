"""CLI entry point for voicelog."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from voicelog import config as config_module
from voicelog import cache, filters, generate, gitsource, readme, render, tts, voice, voicefile
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


def _diff_for(git_result, commits: list, with_diff: bool):
    """Return the diff to send to the model, or None.

    ``git_result.diff`` was computed against the full range before noise
    filtering/capping. If those steps moved the oldest commit still in
    ``commits`` away from what that diff was scoped to, recompute it so the
    diff matches exactly what's being summarised — never a wider range.
    """
    if not with_diff or not commits:
        return None
    original = git_result.commits
    if not original or commits[-1].hash != original[-1].hash:
        return gitsource.diff_for_commits(commits)
    return git_result.diff


def _run_onboarding(args: argparse.Namespace, cfg) -> None:
    """The --new flow: orient a developer on a repo they just cloned.

    README + recent commit activity (ignoring tags entirely — this is "what's
    been happening lately", not "what shipped"). Always transient: never
    cached, never written to the persistent release changelog.
    """
    try:
        git_result = gitsource.read_recent_commits(cfg.onboard_commits, with_diff=args.with_diff)
    except NotAGitRepo:
        print("error: not a git repository (run voicelog from inside a git repo)", file=sys.stderr)
        sys.exit(1)

    cwd = os.getcwd()
    readme_text = readme.load_readme(cwd)
    commits = filters.drop_noise(git_result.commits, cfg.noise)

    if not commits and not readme_text:
        print("Nothing to onboard — empty repo, no README.", file=sys.stdout)
        sys.exit(0)

    diff = _diff_for(git_result, commits, args.with_diff)
    try:
        raw_markdown = generate.onboard(readme_text, commits, cfg, diff=diff)
    except MissingApiKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except LLMError as exc:
        print(
            f"warning: LLM unavailable ({exc}) — falling back to README + recent commits",
            file=sys.stderr,
        )
        print(render.render_onboarding_fallback(readme_text, commits))
        sys.exit(0)

    # --- Render and output (text first, always — never gated on TTS) ---
    rendered = render.render_onboarding(raw_markdown)
    print(rendered)

    # --new is always transient: no voice.md write, regardless of --changelog.

    # --- Speak a short summary aloud (failure only warns, never blocks) ---
    if cfg.speak and not args.no_speak:
        detail = args.detail or cfg.speech_detail == "detailed"
        try:
            spoken = generate.summarize(rendered, cfg, detail=detail)
        except (LLMError, MissingApiKey) as exc:
            print(f"warning: could not build spoken summary ({exc}) — skipping audio", file=sys.stderr)
        else:
            print("Speaking… (Ctrl+C to skip)", file=sys.stderr)
            try:
                tts.speak(spoken, cfg)
            except TTSError as exc:
                print(f"warning: could not speak ({exc})", file=sys.stderr)
            except KeyboardInterrupt:
                print("\nskipped audio.", file=sys.stderr)


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
    range_group.add_argument(
        "--new",
        action="store_true",
        help="Orient yourself on a repo you just cloned: README + recent commit "
        "activity (see onboard_commits in config), spoken + printed",
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
    parser.add_argument(
        "--with-diff",
        action="store_true",
        help="Send the actual code diff to the model for richer summaries "
        "(default sends only commit messages + a diffstat, never code)",
    )
    args = parser.parse_args()

    if args.with_diff:
        print(
            "warning: --with-diff sends your code changes to the LLM provider "
            "(privacy note — the default mode never does this).",
            file=sys.stderr,
        )

    # --- Load config ---
    try:
        cfg = config_module.load(args.config)
    except ConfigFileNotFound as exc:
        print(f"error: config file not found: {exc}", file=sys.stderr)
        sys.exit(1)

    # First run with no key set → offer to paste one (interactive terminals only).
    _maybe_prompt_for_key(cfg.api_key_env)

    # --new is a wholly different mode — README + recent commits (ignoring
    # tags), never a "since X" range — so it's handled entirely separately and
    # returns before touching the rest of the pipeline below.
    if args.new:
        _run_onboarding(args, cfg)
        return

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

    # --- Read commits ---
    try:
        git_result = gitsource.read_commits(cfg.fallback_commits, since=since, with_diff=args.with_diff)
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
    key = cache.cache_key(commits, cfg.model, cfg.sections, voice_text, with_diff=args.with_diff)
    raw_markdown = None if args.fresh else cache.get(cwd, key)

    if raw_markdown is None:
        diff = _diff_for(git_result, commits, args.with_diff)
        try:
            raw_markdown = generate.generate(commits, voice_text, cfg, diff=diff)
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
