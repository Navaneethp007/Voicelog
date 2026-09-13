"""CLI entry point for voicelog."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

import yaml

from voicelog import config as config_module
from voicelog import (
    cache, filters, generate, gitsource, providers, readme, render, state, textstyle,
    tts, voice, voicefile, wizard,
)
from voicelog.config import (
    ConfigFileInvalid,
    ConfigFileNotFound,
    ConfigFileUnreadable,
    ConfigValueInvalid,
)
from voicelog.gitsource import NotAGitRepo, RefNotFound
from voicelog.llm import (
    InvalidApiKey,
    LLMError,
    MissingApiKey,
    MissingModel,
    ModelUnavailable,
)
from voicelog.tts import TTSError


def _interactive() -> bool:
    """Whether it is safe to ask the user a question.

    One gate for the whole program, defined in `wizard` next to the prompts it
    protects. Wrapped here so the CLI reads in its own terms.
    """
    return wizard.can_prompt()


def _reject_choice(flag: str, value: str, valid) -> None:
    """Fail a bad preset name loudly, naming what would have worked."""
    print(
        f"error: unknown {flag} '{value}' - choose one of: {', '.join(valid)}",
        file=sys.stderr,
    )
    sys.exit(2)


def _validate_flags(args: argparse.Namespace) -> None:
    """Reject flag values and combinations that cannot mean anything.

    A typo'd --provider would otherwise keep the previous endpoint and surface
    much later as "model was rejected", which blames the wrong thing. Runs
    before the config is even read, so a typo fails fast either way.
    """
    # --fresh means "ignore the cache", --replay means "read only the cache".
    # No run satisfies both, and silently picking one would hand the user the
    # opposite of what they asked for. argparse cannot express this, because
    # --fresh must stay combinable with every range flag.
    if args.replay and args.fresh:
        print(
            "error: --replay re-reads the cache and --fresh ignores it - pick one",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.provider and providers.get(args.provider) is None:
        _reject_choice("--provider", args.provider, providers.PROVIDERS)
    if args.tts_provider and providers.get_tts(args.tts_provider) is None:
        _reject_choice("--tts-provider", args.tts_provider, providers.TTS_PROVIDERS)

    # "custom" is not a provider, it is a promise to name the endpoint. Without
    # one it would keep whatever endpoint was already configured and just
    # relabel it - which silently changes the cache key and every error label.
    custom = providers.get(args.provider or "")
    if custom and custom.key == providers.CUSTOM_PROVIDER_KEY and not args.base_url:
        print(
            "error: --provider custom needs --base-url <OpenAI-compatible endpoint> "
            "(and --api-key-env NAME if it uses a different key)",
            file=sys.stderr,
        )
        sys.exit(2)


def _apply_overrides(cfg, args: argparse.Namespace):
    """Apply CLI flags as the highest-precedence config layer.

    The only place voicelog changes a loaded Config, and it does it by
    replacement, so cfg stays immutable everywhere downstream.

    **A preset is seeded only when the provider actually changes.** Restating
    the provider you already use is a no-op, which matters because this runs
    twice - once on the main path and again on the config the wizard just wrote
    (see `_run_setup`). Reseeding unconditionally meant `--provider openai`
    silently reset a base_url and key env var the user had typed into the wizard
    seconds earlier. The visible trade: `--provider nvidia` on a hand-pointed
    base_url keeps that URL rather than resetting it to NVIDIA's.
    """
    changes: dict = {}
    silenced = False

    if args.provider:
        # _validate_flags has already rejected unknown names, so the preset
        # exists; its .key is the canonical spelling (stripped, lowercased).
        # Storing args.provider raw would leak padding into the cache key.
        preset = providers.get(args.provider)
        key = preset.key if preset else args.provider.strip().lower()
        if key != cfg.provider and preset and preset.base_url:
            changes["base_url"] = preset.base_url
            changes["api_key_env"] = preset.api_key_env
        changes["provider"] = key

    # ...then anything explicit overrides the preset.
    if args.base_url:
        changes["base_url"] = providers.normalize_base_url(args.base_url)
    if args.api_key_env is not None:  # "" means "this endpoint needs no key"
        changes["api_key_env"] = args.api_key_env
    if args.model:
        changes["model"] = args.model.strip()

    if args.tts_provider:
        tts_preset = providers.get_tts(args.tts_provider)
        tts_key = tts_preset.key if tts_preset else args.tts_provider.strip().lower()
        if tts_key == providers.NO_TTS_KEY:
            # `none` is offered in the wizard, so it means the same thing here,
            # and it beats the implied "on" from naming a voice below.
            changes["speak"] = False
            silenced = True
        elif tts_key != cfg.tts_provider:
            # A real switch: take the whole preset, because every one of these
            # is backend-specific. Carrying them over would send an NVIDIA key
            # as an ElevenLabs header, a Magpie voice name as an ElevenLabs
            # voice id, an OpenAI tts_model as ElevenLabs' model_id, and an
            # OpenAI proxy URL to a different service entirely.
            changes["tts_provider"] = tts_key
            changes["tts_api_key_env"] = tts_preset.api_key_env
            # Blank where there is no sensible default (ElevenLabs ids are
            # per-account) so tts.py gives its actionable "needs tts_voice set
            # to a voice id".
            changes["tts_voice"] = tts_preset.voices[0] if tts_preset.voices else ""
            changes["tts_model"] = ""
            changes["tts_base_url"] = ""
        else:
            changes["tts_provider"] = tts_key

        if not silenced:
            # Naming a backend means wanting audio: with `speak: false` in the
            # config this flag was otherwise a silent no-op - nothing played,
            # and nothing said why. --no-speak still wins, because that is
            # applied where speech is attempted, not here.
            changes["speak"] = True

    # ...and an explicit --voice wins over whatever the preset chose. Naming a
    # voice means wanting to hear it, for the same reason.
    if args.voice:
        changes["tts_voice"] = args.voice
        if not silenced:
            changes.setdefault("speak", True)

    return dataclasses.replace(cfg, **changes) if changes else cfg


def _needs_setup(cfg, args: argparse.Namespace) -> bool:
    """Whether this is a genuine first run: nothing to call, nothing configured.

    Deliberately does NOT consider whether ``./changelog.yml`` exists. If that
    file sets a model, ``cfg.model`` is already truthy and we have returned
    above; if it does not, its existence says nothing about whether a model is
    configured - and a repo that commits one holding only `sections`/`noise`
    (the shape voicelog itself ships) would otherwise never see the wizard and
    print a raw commit list forever, with no hint that a one-time setup fixes it.

    ``--config`` still suppresses it, because ``_run_setup(resume=True)``
    reloads from that path and would ignore the user config the wizard just
    wrote - so the wizard could not affect the run that triggered it.
    """
    if args.replay:
        return False  # a replay calls nothing, so there is nothing to configure
    if cfg.model or args.config:
        return False
    user_path = config_module.user_config_path()
    if user_path and os.path.isfile(user_path):
        return False
    return _interactive()


def _run_setup(args: argparse.Namespace, cfg, *, resume: bool = False):
    """Run the wizard and save its answers.

    Args:
        resume: True on an implicit first run - return a freshly loaded Config so
            the command the user actually typed carries on in the same
            invocation. False for an explicit --setup, which is a mode: write,
            report, exit.

    Returns:
        A reloaded Config when resuming, else None. None also means "setup did
        not happen", and the caller keeps the config it already had.
    """
    if not _interactive():
        if not resume:
            print(
                "error: `voicelog --setup` needs an interactive terminal.",
                file=sys.stderr,
            )
            sys.exit(1)
        return None

    dest = config_module.user_config_path()
    if not dest:
        print(
            "error: could not find a home directory to store your config in. "
            "Set VOICELOG_CONFIG_HOME to a writable directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    if resume:
        print("First run - let's set up voicelog.", file=sys.stderr)

    try:
        values = wizard.run_setup(dataclasses.asdict(cfg), dest=dest)
    except wizard.SetupAborted:
        print("Setup cancelled - nothing was written.", file=sys.stderr)
        if resume:
            return None
        sys.exit(1)

    try:
        written = config_module.save_user_config(values)
    except (OSError, ConfigFileInvalid) as exc:
        # ConfigFileInvalid too: saving re-reads the target to preserve keys the
        # user added by hand, so an unparseable existing file lands here.
        # Do not lose the answers the user just gave us.
        print(f"error: could not write {dest} ({exc})", file=sys.stderr)
        print("Save this yourself to keep the settings:", file=sys.stderr)
        print(yaml.safe_dump(values, sort_keys=False), file=sys.stderr)
        sys.exit(1)

    print(f"Saved to {written}", file=sys.stderr)

    if not resume:
        print("Setup complete. Run `voicelog` inside a git repo.", file=sys.stderr)
        sys.exit(0)

    # Reload rather than building a Config from `values`: one merge path, and it
    # re-asserts flag precedence over what was just written.
    return _apply_overrides(config_module.load(args.config), args)


def _resolve_since_last() -> str | None:
    """The stored watermark to summarise from, or None to use the default range.

    Every "cannot use it" case is handled here rather than being left to
    `read_commits`, which raises RefNotFound for an unresolvable ref and would
    end the run with "unknown git ref" - a stale watermark after a rebase, a
    reset or a shallow clone is not the user's fault and must not be fatal.
    """
    sha = state.last_summarised_sha()
    if not sha:
        print(
            "note: no record of a previous voicelog run in this repo - showing the "
            "default range. The next --since-last will start from here.",
            file=sys.stderr,
        )
        return None
    if not gitsource.is_ancestor(sha):
        print(
            f"warning: the last commit voicelog summarised ({sha[:9]}) is not in this "
            "branch's history - a rebase, a reset, or a different branch. Showing the "
            "default range.",
            file=sys.stderr,
        )
        return None
    print(f"Picking up from {sha[:9]}...", file=sys.stderr)
    return sha


def _emit(markdown: str) -> None:
    """Print user-facing markdown, styled for a terminal.

    Styling happens HERE and nowhere else. The rendered string is shared with
    `voicefile.update_voice_md` - which matches `^## Unreleased`, splits blocks
    on `^## ` and byte-compares against the file - and with the spoken-summary
    prompt. An escape sequence reaching either would break block replacement or
    end up read aloud.
    """
    styled = textstyle.style(markdown)
    try:
        print(styled)
    except UnicodeEncodeError:
        # Only reachable when the UTF-8 reconfigure in main() could not be
        # applied. Losing the run to an emoji would be a poor trade.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(styled.encode(encoding, "backslashreplace").decode(encoding, "replace"))


def _speak(text: str, cfg) -> None:
    """Read ``text`` aloud. Never raises: audio is best-effort, always.

    Shared by the changelog path, the --new path and --replay, so a change to
    how speech failures are reported lands in one place instead of three.
    """
    print("Speaking... (Ctrl+C to skip)", file=sys.stderr)
    try:
        tts.speak(text, cfg)
    except TTSError as exc:
        print(f"warning: could not speak ({exc})", file=sys.stderr)
    except KeyboardInterrupt:
        # The changelog is already printed and saved; just skip the audio.
        print("\nskipped audio.", file=sys.stderr)


def _ensure_speech_key(cfg, args: argparse.Namespace, *, already_asked: str = "") -> None:
    """Offer to paste the key speech needs, if we are going to speak.

    ``already_asked`` names an env var this run has already prompted for, so the
    normal path does not ask twice for a shared variable. It must be passed
    explicitly rather than assumed to be ``cfg.api_key_env``: --replay skips the
    text-provider prompt entirely (it makes no model call), so assuming left a
    replay unable to ask for the very key it needs to play anything - which in
    the default config, where both vars are NVIDIA_API_KEY, meant every replay
    in a fresh terminal could only warn "could not speak".
    """
    if (
        cfg.speak
        and not args.no_speak
        and cfg.tts_api_key_env
        and cfg.tts_api_key_env != already_asked
    ):
        wizard.ensure_key(cfg.tts_api_key_env, label=cfg.tts_provider)


def _run_replay(args: argparse.Namespace, cfg) -> None:
    """The --replay flow: re-print and re-speak the last cached run.

    Exists because the watermark records that a summary was *printed*, not that
    it was *heard*, and audio is best-effort: a run whose speech failed - or
    that you walked away from - advances the watermark all the same, and
    --since-last then shows nothing. This is how to get that run back.

    Makes no model call, reads no commits and never touches the watermark, so it
    is also the one cache reader that is not key-gated (see cache.last).
    """
    if args.changelog:
        print(
            "note: --changelog is ignored with --replay - a replay re-prints what "
            "you already saw; it never rewrites the release changelog.",
            file=sys.stderr,
        )

    entry = cache.last(os.getcwd())
    if entry is None:
        # One message for absent, corrupt and outside-a-repo alike: the remedy
        # for all three is the same, and a disposable file does not deserve a
        # diagnosis that reads like a bug report.
        print(
            "Nothing to replay - no cached run in this repo yet. Run `voicelog` "
            "inside a git repo first; --replay then re-plays that run."
        )
        return

    # Rendered here rather than at cache time: the cache holds raw model output,
    # so a replay picks up today's rendering rules.
    _emit(render.render(entry.markdown))

    if not cfg.speak or args.no_speak:
        return

    detail = args.detail or cfg.speech_detail == "detailed"
    spoken = entry.summary(detail)
    if spoken is None:
        spoken = entry.summary(not detail)
        if spoken is None:
            print(
                "note: this entry has no cached spoken summary - the run that "
                "produced it did not speak. Printed only; a fresh run would "
                "build one, which calls the model.",
                file=sys.stderr,
            )
            return
        asked, got = ("detailed", "brief") if detail else ("brief", "detailed")
        print(
            f"note: no cached {asked} summary for this entry - speaking the "
            f"cached {got} one.",
            file=sys.stderr,
        )

    _ensure_speech_key(cfg, args)
    _speak(spoken, cfg)


def _model_error(exc: Exception) -> None:
    """Report a model misconfiguration.

    Deliberately not fatal. voicelog promises to always print something useful,
    and a hard exit here would start failing every pre-push hook and CI step
    that 0.2.0 ran fine. What changes is that the message names the fix instead
    of dumping a provider's JSON.
    """
    print(f"error: {exc}", file=sys.stderr)


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
    except (MissingModel, ModelUnavailable, InvalidApiKey) as exc:
        _model_error(exc)
        _emit(render.render_onboarding_fallback(readme_text, commits))
        sys.exit(0)
    except MissingApiKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except LLMError as exc:
        print(
            f"warning: LLM unavailable ({exc}) — falling back to README + recent commits",
            file=sys.stderr,
        )
        _emit(render.render_onboarding_fallback(readme_text, commits))
        sys.exit(0)

    # --- Render and output (text first, always — never gated on TTS) ---
    rendered = render.render_onboarding(raw_markdown)
    _emit(rendered)

    # --new is always transient: no voice.md write, regardless of --changelog.

    # --- Speak a short summary aloud (failure only warns, never blocks) ---
    if cfg.speak and not args.no_speak:
        detail = args.detail or cfg.speech_detail == "detailed"
        try:
            spoken = generate.summarize(rendered, cfg, detail=detail)
        except (MissingModel, ModelUnavailable, InvalidApiKey) as exc:
            # Its own branch so the message names the fix; on a cache hit this
            # is the first LLM call of the run.
            print(f"error: {exc}", file=sys.stderr)
            print("warning: skipping audio.", file=sys.stderr)
        except (LLMError, MissingApiKey) as exc:
            print(f"warning: could not build spoken summary ({exc}) — skipping audio", file=sys.stderr)
        else:
            _speak(spoken, cfg)


def main() -> None:
    # Casual changelogs contain em-dashes/emoji; make sure the console can show
    # them (Windows defaults to cp1252). The voice.md file is always UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            # errors= is not optional: reconfigure(encoding=...) resets the
            # error handler to "strict", so omitting it can make a stream
            # LESS forgiving than it already was.
            stream.reconfigure(  # type: ignore[union-attr]
                encoding="utf-8", errors="backslashreplace"
            )
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
        help="Use only this config file, ignoring your user config and "
        "./changelog.yml (for reproducible or CI runs)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Configure provider, API key env var, model and voice, then exit "
        "(re-runnable; writes your user config)",
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
        "--since-last",
        action="store_true",
        help="Summarise only what arrived since voicelog last summarised this repo "
        "(ignores tags; transient - never writes the release changelog)",
    )
    range_group.add_argument(
        "--replay",
        action="store_true",
        help="Re-print and re-speak the last cached run - no model call, no git "
        "range, nothing recorded (for when you missed the audio). Only the most "
        "recent run is kept, so the next voicelog run replaces it",
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
    # Per-run overrides of the configured provider/model/voice. All default to
    # None so that "not passed" stays distinguishable from an empty string.
    override_group = parser.add_argument_group("per-run overrides")
    override_group.add_argument(
        "--model",
        metavar="ID",
        default=None,
        help="Model id to use for this run (voicelog ships no default model)",
    )
    override_group.add_argument(
        "--provider",
        metavar="NAME",
        default=None,
        help=f"Provider preset: {', '.join(providers.PROVIDERS)} "
        "(also sets its base URL and key env var)",
    )
    override_group.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="OpenAI-compatible endpoint to use for this run",
    )
    override_group.add_argument(
        "--api-key-env",
        metavar="NAME",
        default=None,
        help="Environment variable holding the API key (empty means none needed)",
    )
    override_group.add_argument(
        "--tts-provider",
        metavar="NAME",
        default=None,
        help=f"Speech backend: {', '.join(k for k in providers.TTS_PROVIDERS if k != providers.NO_TTS_KEY)}",
    )
    override_group.add_argument(
        "--voice",
        metavar="NAME",
        default=None,
        help="Voice id/name for this run (meaning depends on the provider)",
    )
    args = parser.parse_args()
    _validate_flags(args)

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
    except ConfigValueInvalid as exc:
        # The file parsed; one line in it is unusable. `--setup` gets a pass
        # only when it can actually fix that line - i.e. when the value came
        # from the user config, which saving rewrites and which drops values it
        # cannot use. A bad value in a project's changelog.yml is untouchable by
        # the wizard, so promising a repair there would be a lie.
        user_path = config_module.user_config_path()
        repairable = bool(args.setup and user_path and exc.path == user_path)
        if not repairable:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"warning: ignoring an unusable setting ({exc})", file=sys.stderr)
        print("Setup will drop it.", file=sys.stderr)
        cfg = config_module.defaults_config()
    except ConfigFileUnreadable as exc:
        # Intact, just unavailable - so unlike a corrupt file, --setup gets no
        # pass here. Rewriting it would destroy content we never managed to
        # read, to fix a problem the file does not have.
        print(f"error: could not open config ({exc})", file=sys.stderr)
        print(
            "Close whatever has it open, or fix its permissions, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ConfigFileInvalid as exc:
        if not args.setup:
            print(f"error: could not read config ({exc})", file=sys.stderr)
            sys.exit(1)
        # --setup is the documented way to fix a broken config, so refusing to
        # run it on a broken config would leave hand-editing as the only repair.
        print(f"warning: ignoring unreadable config ({exc})", file=sys.stderr)
        print("Continuing with built-in defaults; setup will write a fresh config.",
              file=sys.stderr)
        cfg = config_module.defaults_config()

    # Flags sit above every file layer. Applied before the setup gate so that
    # `--model X` on a fresh machine just works instead of opening a wizard.
    previous_provider = cfg.provider
    cfg = _apply_overrides(cfg, args)

    # A switch keeps whatever model the config named, because clearing it would
    # break `voicelog --provider groq` for everyone who has one configured. Say
    # so here, while the user can still act on it, rather than letting it
    # surface later as a rejected-model error.
    if args.provider and not args.model and cfg.model and cfg.provider != previous_provider:
        print(
            f"warning: using model '{cfg.model}' from your config against "
            f"--provider {cfg.provider}; pass --model to change it.",
            file=sys.stderr,
        )

    if args.setup:
        _run_setup(args, cfg)
    elif _needs_setup(cfg, args):
        cfg = _run_setup(args, cfg, resume=True) or cfg

    # Before the key prompts on purpose: a replay makes no model call, so
    # asking for the text-provider key would be asking for a secret to run a
    # command that cannot use it. The speech key is asked for inside
    # _run_replay, and only once there is something to speak.
    if args.replay:
        _run_replay(args, cfg)
        return

    # No key set → offer to paste one (interactive terminals only).
    preset = providers.get(cfg.provider)
    wizard.ensure_key(
        cfg.api_key_env,
        label=cfg.provider,
        signup_url=preset.signup_url if preset else "",
    )
    _ensure_speech_key(cfg, args, already_asked=cfg.api_key_env)

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
    elif args.since_last:
        since = _resolve_since_last()
    else:
        since = args.since
    # --since-last is transient even on its first run, where it has no stored
    # sha and falls back to the default range: a partial range must never reach
    # voicefile, which REPLACES the Unreleased block rather than merging it.
    diff_mode = since is not None or args.since_last

    # Only a range that starts at or before the watermark may advance it. A
    # one-off query (--pull/--pr/--since) shows a slice the watermark never
    # covered, so advancing would silently skip everything in between.
    watermark_mode = since is None or bool(args.since_last)

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
        where = since[:9] if since and len(since) == 40 else since
        msg = f"No new commits since {where}." if since else "Nothing to release."
        print(msg, file=sys.stdout)
        sys.exit(0)

    # --- Cap very large ranges (keeps generation fast/cheap on big forks) ---
    if len(commits) > cfg.max_commits:
        # The watermark can only advance to HEAD, and there is no way to express
        # "all but the newest N" as a <sha>..HEAD range. Refusing to advance
        # would livelock on the same N forever, so advance and say what is lost.
        extra = (
            "; the older ones will not appear in a later --since-last run"
            if watermark_mode
            else ""
        )
        print(
            f"warning: {len(commits)} commits — using the most recent "
            f"{cfg.max_commits}{extra}",
            file=sys.stderr,
        )
        commits = commits[: cfg.max_commits]

    # --- Load voice samples ---
    voice_text = voice.load_voice(cfg.voice_samples)

    # --- Generate changelog (cached on the commit set to avoid churn + API cost) ---
    cwd = os.getcwd()
    key = cache.cache_key(
        commits,
        cfg.model,
        cfg.sections,
        voice_text,
        with_diff=args.with_diff,
        provider=cfg.provider,
        base_url=cfg.base_url,
    )
    raw_markdown = None if args.fresh else cache.get(cwd, key)
    # A cache hit needs no write, so there is nothing that can fail.
    cached_ok = True

    if raw_markdown is None:
        diff = _diff_for(git_result, commits, args.with_diff)
        try:
            raw_markdown = generate.generate(commits, voice_text, cfg, diff=diff)
        except (MissingModel, ModelUnavailable, InvalidApiKey) as exc:
            _model_error(exc)
            _emit(render.render_fallback(commits))
            sys.exit(0)
        except MissingApiKey as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except LLMError as exc:
            print(f"warning: LLM unavailable ({exc}) — falling back to commit list", file=sys.stderr)
            _emit(render.render_fallback(commits))
            sys.exit(0)
        cached_ok = cache.put(cwd, key, raw_markdown)

    # --- Render and output (text first, always — never gated on TTS) ---
    rendered = render.render(raw_markdown)
    _emit(rendered)

    # --- Persist the casual changelog to .changelog/voice.md (opt-in) ---
    # The default run is a transient rundown. The persistent release changelog is
    # written only with --changelog (or write_changelog: true), and never in
    # diff/pull mode ("what did I just pull" is not a release).
    persisted_ok = True
    if (args.changelog or cfg.write_changelog) and not diff_mode:
        try:
            voicefile.update_voice_md(cwd, rendered, git_result.tag, rel_path=cfg.voice_md)
        except OSError as exc:
            print(f"warning: could not update voice.md ({exc})", file=sys.stderr)
            persisted_ok = False

    # --- Record how far we have summarised -------------------------------
    # Reaching here means a summary was PRINTED, fresh or cached: every failure
    # path above exits first, including the two that print a raw commit list.
    # Placed before the speech block because generate.summarize does not catch
    # KeyboardInterrupt, and a Ctrl+C there would skip a write placed lower -
    # leaving the user to re-read the identical range next time.
    # cached_ok joins the gate for the same reason persisted_ok is in it: if
    # nothing kept this generation, advancing hides it from both directions -
    # --replay finds no cache and --since-last sees no new commits - leaving a
    # paid summary in terminal scrollback and nowhere else. Re-generating it
    # next run costs another call; losing it costs the call AND the text.
    #
    # This does not risk a permanently frozen watermark: record_summarised
    # writes into the same .git/voicelog directory, so anything that makes that
    # location unwritable already stops the watermark by itself. The gate only
    # bites when the cache file alone is unwritable, which the next run can fix.
    if watermark_mode and persisted_ok and cached_ok:
        head = gitsource.head_sha()
        if head:
            state.record_summarised(head)

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
            except (MissingModel, ModelUnavailable, InvalidApiKey) as exc:
                print(f"error: {exc}", file=sys.stderr)
                print("warning: skipping audio.", file=sys.stderr)
            except (LLMError, MissingApiKey) as exc:
                print(f"warning: could not build spoken summary ({exc}) — skipping audio", file=sys.stderr)
            else:
                cache.put_summary(cwd, key, detail, spoken)

        if spoken is not None:
            _speak(spoken, cfg)


if __name__ == "__main__":
    main()
