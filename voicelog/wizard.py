"""Interactive setup - the only module in voicelog that asks questions.

`voicelog --setup`, and the first run on a machine with nothing configured, come
through here. The wizard collects answers in memory and hands them back; the
caller decides whether to write them. That split is what lets an abort leave
nothing behind.

Two constraints shape the code:

- **Output is ASCII only.** ``cli`` reconfigures the console to UTF-8 but
  swallows the failure, so a stray box-drawing character can raise
  UnicodeEncodeError halfway through setup on a cp1252 console.
- **A key is never written anywhere.** The wizard puts it in ``os.environ`` for
  this process and prints the command to persist it, with the value left as a
  placeholder so it stays out of scrollback and shell history.
"""
from __future__ import annotations

import getpass
import os
import re
import shlex
import subprocess
import sys
import warnings
from typing import Any, Sequence

from voicelog import config as config_module
from voicelog import fileio
from voicelog import providers
from voicelog.providers import (
    CUSTOM_PROVIDER_KEY,
    MANUAL_ENTRY,
    NO_TTS_KEY,
    ProviderError,
)


class SetupAborted(Exception):
    """The user backed out: Ctrl-C, EOF, or declining to save.

    ``interrupted`` distinguishes Ctrl-C from the other two, because they are
    different answers. EOF at the key prompt - or declining to save - answers
    that question and nothing more, so the run continues. Ctrl-C answers the
    whole command. One type for both meant whichever caller swallowed the
    refusal swallowed the interrupt with it, and the run ploughed on through a
    model call and a TTS attempt after the user had asked it to stop.
    """

    def __init__(self, *, interrupted: bool = False):
        super().__init__()
        self.interrupted = interrupted


# Option values for the "this model failed its check" prompt. Deliberately
# readable words rather than sentinels: ask_choice shows the default value back
# to the user, so "__pick_again__" would leak into the prompt.
_PICK_AGAIN = "different"
_KEEP_ANYWAY = "anyway"


# Environments where a prompt is never acceptable: it would hang a build or a
# git hook, and voicelog's rule is that it never blocks.
_NON_INTERACTIVE_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "VOICELOG_NO_SETUP",
)


def can_prompt() -> bool:
    """Whether it is safe to ask the user anything at all.

    Both streams must be a terminal. Checking stdin alone is not enough: under
    some git hook runners stdin is inherited from the terminal while stdout is
    piped, so a stdin-only check prints an invisible prompt and hangs
    `git commit` forever. Windows makes stdin-only worse still - the NUL device
    reports itself as a character device, so isatty() there is True.

    Every prompt in voicelog goes through this, the key prompt included -
    which is why the streams are touched defensively. Under pythonw.exe
    sys.stdin is None, and a detached or closed stream raises on isatty(); an
    AttributeError there used to take down the entire command rather than the
    one question it could not ask. textstyle.supports_style guards the same case
    for the same reason.
    """
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):  # None, detached, closed, or exotic
        return False
    if any(os.environ.get(name) for name in _NON_INTERACTIVE_VARS):
        return False
    return os.environ.get("TERM") != "dumb"


def _read_line(prompt: str) -> str:
    """Read one line, turning both ways out into SetupAborted.

    A thin seam over input() so every prompt handles EOF and Ctrl-C the same
    way, and so tests can script the whole flow.
    """
    try:
        return input(prompt)
    except EOFError:
        print()
        raise SetupAborted() from None
    except KeyboardInterrupt:
        print()
        raise SetupAborted(interrupted=True) from None


def ask_valid(key: str, prompt: str, default: str = "") -> str:
    """Prompt until the answer is one ``config`` accepts for ``key``.

    The wizard used to judge values by its own lights and hand them to a writer
    that judged them again, by different rules. Anything the two disagreed on
    was accepted, confirmed, "saved", and then silently discarded.
    """
    while True:
        answer = ask_text(prompt, default=default)
        try:
            return config_module.check_value(key, answer)
        except config_module.ConfigValueInvalid as exc:
            # reason is "<key>: <value> <what is wrong>"; the key is already in
            # the prompt the user is looking at.
            print(f"  {exc.reason.split(': ', 1)[-1]}")


def ask_text(prompt: str, default: str = "", *, allow_empty: bool = False) -> str:
    """Free-text prompt. Enter accepts ``default``.

    Re-prompts while the answer is empty and there is no default, unless
    ``allow_empty`` - some answers ("" means no key needed) are meaningful.
    """
    label = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        answer = _read_line(label).strip()
        if answer:
            return answer
        if default:
            return default
        if allow_empty:
            return ""
        print("  Please enter a value.")


def ask_secret(prompt: str) -> str:
    """Read a secret without echoing it. Empty means "skip".

    When the terminal cannot suppress echo, getpass does not fail - it warns and
    falls back to a plain visible ``input()``. Letting a key appear on screen
    unremarked is the bad outcome, so the warning is captured and reported: the
    user can then decide whether to rotate it.
    """
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
        if any(issubclass(w.category, getpass.GetPassWarning) for w in caught):
            print("  note: this terminal cannot hide input - the key was "
                  "visible as you typed it.")
        return value.strip().strip("'\"").strip()
    except EOFError:
        print()
        raise SetupAborted() from None
    except KeyboardInterrupt:
        print()
        raise SetupAborted(interrupted=True) from None


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Yes/no prompt. Enter accepts ``default``."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read_line(f"{prompt} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(
    prompt: str,
    options: Sequence[tuple[str, str]],
    *,
    default: str | None = None,
    page: int = 20,
    pinned: Sequence[tuple[str, str]] = (),
) -> str:
    """Numbered picker with substring filtering. Returns the chosen value.

    Type a number to pick, any other text to narrow the list (matched against
    both value and label), or Enter to take ``default``. At most ``page`` rows
    of ``options`` are printed at a time, which is what keeps a 300-model
    catalog usable.

    ``pinned`` rows always render, below the page and untouched by narrowing.
    An escape hatch appended to ``options`` is not one: NVIDIA lists 82 models,
    so "Type a model id manually" became option 83 - never printed and never
    reachable - and it is wanted most when the catalog offers a model the
    account turns out not to be able to call.
    """
    visible = list(options)
    pinned = list(pinned)
    while True:
        shown = visible[:page]
        rows = shown + pinned
        # Numbering covers this page; Enter covers everything still on
        # offer. A configured model sits beyond row 20 of an 82-model
        # catalog, and returning it needs no row to point at.
        selectable = visible + pinned
        print(f"\n{prompt}")
        for index, (_, label) in enumerate(rows, start=1):
            print(f"  {index}) {label}")
        if len(visible) > len(shown):
            # Counts the models, not our own pinned row.
            print(f"  ... {len(visible)} total; type text to narrow the list")

        default_label = ""
        if default is not None and any(value == default for value, _ in selectable):
            # "[nvidia]" reads like something to type; the input is a number.
            default_label = f" [Enter = {default}]"
        answer = _read_line(f"Choose 1-{len(rows)}{default_label}: ").strip()

        if not answer:
            if default is not None and any(value == default for value, _ in selectable):
                return default
            print("  Please choose one.")
            continue

        # isdecimal, not isdigit: isdigit accepts superscripts and other
        # numeric-looking characters that int() then rejects, so a stray
        # one raised ValueError mid-setup and lost every answer so far.
        if answer.isdecimal():
            index = int(answer)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
            print(f"  Pick a number between 1 and {len(rows)}.")
            continue

        needle = answer.lower()
        narrowed = [
            (value, label)
            for value, label in options
            if needle in value.lower() or needle in label.lower()
        ]
        if narrowed:
            visible = narrowed
        else:
            print(f"  No match for '{answer}'.")
            visible = list(options)


def _persist_hint(env_name: str) -> str:
    """The command that makes a key survive this terminal.

    The value stays a placeholder on purpose: printing the real key would put it
    in terminal scrollback, and in shell history once the user runs the line.
    """
    if os.name == "nt":
        return f'setx {env_name} "<your key>"     (then open a new terminal)'
    return f"export {env_name}=<your key>     (add it to your shell profile)"


def _mask(value: str) -> str:
    """Enough of a key to recognise, not enough to leak."""
    if len(value) <= 8:
        return "***"
    return f"{value[:6]}...{value[-2:]}"


def _shell_profile() -> str | None:
    """The first shell profile that already exists, or None.

    Only appends to a file the user already has: creating ~/.zshrc for someone
    who does not use zsh would be a file they never asked for and never read.
    """
    home = os.path.expanduser("~")
    if home == "~":
        return None
    for name in (".zshrc", ".bashrc", ".profile"):
        candidate = os.path.join(home, name)
        if os.path.isfile(candidate):
            return candidate
    return None


# setx silently truncates above this, which would store a key that looks set
# and does not work - worse than not storing it.
_SETX_MAX_CHARS = 1024

_MARKER = "# added by voicelog"


def _export_line(env_name: str, key: str) -> str:
    """A shell-safe `export NAME=value` line for a profile.

    shlex.quote is not decoration. This used to interpolate the key inside
    DOUBLE quotes, so a key containing a backtick or $( was executed by the
    shell at every startup - arbitrary code from a value the user pasted into a
    prompt. Single-quoted output is inert in sh, bash, zsh and ksh, which is
    every shell _shell_profile can return.
    """
    return f"export {env_name}={shlex.quote(key)}  {_MARKER}"


def _persist_key(env_name: str, key: str) -> bool:
    """Make a key outlive this terminal. Returns whether it worked."""
    if os.name == "nt":
        if len(key) > _SETX_MAX_CHARS:
            print(f"  setx truncates values longer than {_SETX_MAX_CHARS} characters, "
                  "which would store a broken key. Set it yourself instead.")
            return False
        # setx takes the value as a command-line argument, so for the life of
        # that process it is visible to anything that can list processes, and on
        # a machine with 4688 process-creation auditing enabled it is written to
        # the Security event log permanently. Said out loud because this module
        # promises a key is never written anywhere, and here it is written
        # somewhere - the user should get to decide with that in hand.
        print("  note: setx passes the key on a command line, so it is briefly "
              "visible to other processes on this machine (and recorded if "
              "process auditing is on).")
        try:
            result = subprocess.run(["setx", env_name, key], capture_output=True, text=True)
        except OSError as exc:
            print(f"  Could not run setx ({exc}).")
            return False
        if result.returncode != 0:
            print(f"  setx exited with {result.returncode}.")
            return False
        print("  Saved. New terminals will have it; this one already does.")
        return True

    profile = _shell_profile()
    if profile is None:
        print("  Could not find a shell profile (~/.zshrc, ~/.bashrc, ~/.profile).")
        return False

    line = _export_line(env_name, key)
    try:
        existing = fileio.read_text(profile)
    except OSError:
        existing = ""

    if line in existing.splitlines():
        print(f"  {profile} already sets it to this value; nothing to change.")
        return True
    if re.search(rf"^\s*export\s+{re.escape(env_name)}=", existing, re.MULTILINE):
        # Appending is still correct - the last export wins in every shell we
        # can be writing to - but the user should not have to work that out.
        print(f"  note: {profile} already sets ${env_name}; the line added at "
              "the end is the one that takes effect.")

    try:
        with open(profile, "a", encoding="utf-8") as fh:
            fh.write(f"\n{line}\n")
    except OSError as exc:
        print(f"  Could not write {profile} ({exc}).")
        return False
    print(f"  Added to {profile}. Open a new terminal, or run: source {profile}")
    return True


def _offer_to_persist(env_name: str, key: str) -> None:
    """Offer to keep a freshly-pasted key beyond this process.

    Without this, setup succeeds, the changelog works, and tomorrow's terminal
    fails with nothing on screen explaining why. Declining leaves the previous
    behaviour untouched: set for this run, with the command printed to copy.
    """
    if os.name == "nt":
        print(f'  Keeping it runs: setx {env_name} "<your key>"   '
              "(user-wide on this machine)")
    else:
        print("  Keeping it appends an export line to your shell profile.")

    if not ask_yes_no("  Keep this key for future terminals?", default=True):
        print(f"  Using it for this session only. To keep it: {_persist_hint(env_name)}")
        return
    if not _persist_key(env_name, key):
        print(f"  To do it yourself: {_persist_hint(env_name)}")


def ensure_key(
    env_name: str,
    *,
    label: str = "",
    signup_url: str = "",
    confirm_existing: bool = False,
) -> bool:
    """Make sure a key is available in ``os.environ[env_name]``.

    Returns whether one is available afterwards. No-ops - returning True - when
    ``env_name`` is blank, which is how a keyless endpoint like a local Ollama
    says it needs nothing.

    Only prompts on an interactive terminal, so CI and pipes fall through to the
    normal missing-key error instead of hanging. ``confirm_existing`` is for the
    wizard: outside it, an already-set variable is reused silently, because this
    runs on every single voicelog invocation.
    """
    if not env_name:
        return True

    existing = os.environ.get(env_name)
    if existing and not confirm_existing:
        return True

    if not can_prompt():
        return bool(existing)

    who = f" for {label}" if label else ""
    declined = False
    if existing:
        print(f"\n${env_name} is already set to {_mask(existing)}.")
        if ask_yes_no("Use that value?", default=True):
            return True
        declined = True
    else:
        print(f"\nNo API key found in ${env_name}{who}.")
        if signup_url:
            print(f"  Get one at: {signup_url}")

    # "Skip" means "keep what is there", which is not on offer once the user
    # has refused what is there - so the prompt has to say what Enter does.
    key = ask_secret(
        "Paste a different API key (or press Enter to continue without one): "
        if declined
        else "Paste your API key (or press Enter to skip): "
    )
    if not key:
        if declined:
            # Refused, and nothing offered in its place. Returning True here
            # meant the caller went on to authenticate discovery, model
            # verification and generation with the very key the user had
            # just rejected. Dropping it from this process only - never from
            # the shell it came from - turns that into the ordinary,
            # actionable missing-key error.
            os.environ.pop(env_name, None)
            print(f"  Not using the existing ${env_name} for this run.")
            return False
        return bool(existing)

    os.environ[env_name] = key
    if existing and existing != key:
        # setx and a profile export are user-wide: say what would be replaced.
        print(f"  note: keeping this will replace the existing ${env_name} "
              f"({_mask(existing)}) for every tool on this machine.")
    _offer_to_persist(env_name, key)
    return True


def _pick_model(
    models: list[str], current: str, catalog_url: str
) -> str:
    """One pass of the model picker: choose from the catalog, or type an id."""
    chosen = ask_choice(
        "Which model?",
        [(model, model) for model in models],
        default=current if current in models else None,
        pinned=[(MANUAL_ENTRY, "Type a model id manually")],
    )
    if chosen == MANUAL_ENTRY:
        if catalog_url:
            print(f"  Catalog: {catalog_url}")
        return ask_text("Model id", default=current)
    return chosen


def choose_model(
    base_url: str,
    api_key: str | None,
    *,
    current: str = "",
    catalog_url: str = "",
) -> str:
    """Pick a model id, from the provider's live catalog where possible.

    Discovery keeps the list current, so nothing voicelog ships can go stale.
    Typing an id is offered even when discovery succeeds: someone who knows what
    they want should not have to scroll, and a brand-new model can be callable
    before it shows up in a catalog listing.

    The chosen id is then checked with one 1-token call, because appearing in
    ``/v1/models`` does not prove an account may call it. A failed check warns
    and offers one more pick; it never overrides the user and never blocks.
    """
    # Bound before the try: the retry path below needs to know whether a
    # catalog exists, and discovery failing must not leave it unbound.
    models: list[str] = []
    print("\nFetching available models...", flush=True)
    try:
        models = providers.fetch_models(base_url, api_key)
    except ProviderError as exc:
        print(f"  Could not list models: {exc}")
        if catalog_url:
            print(f"  Find a model id at: {catalog_url}")
        model = ask_text("Model id", default=current)
    else:
        print(f"  Found {len(models)}.")
        model = _pick_model(models, current, catalog_url)

    while True:
        print("Checking that it answers...", flush=True)
        reason = providers.verify_model(base_url, api_key, model)
        if reason is None:
            print(f"  ok - {model} is callable.")
            return model

        # Keeping a model that just failed stays possible - the check is
        # advisory, and a provider having a bad minute must not trap anyone -
        # but it has to be a decision. Accepting it silently is how setup came
        # to save a model it had already proved was broken.
        print(f"  Warning: {reason}")
        if ask_choice(
            "What would you like to do?",
            [(_PICK_AGAIN, "Choose a different model"),
             (_KEEP_ANYWAY, f"Use '{model}' anyway")],
            default=_PICK_AGAIN,
        ) == _KEEP_ANYWAY:
            print(f"  Keeping {model} anyway - it may not work.")
            return model

        # With no catalog (discovery failed) there is nothing to pick from, so
        # the retry is another chance to type an id.
        model = (
            _pick_model(models, current, catalog_url)
            if models
            else ask_text("Model id", default=model)
        )


def _choose_llm(current: dict[str, Any]) -> dict[str, Any]:
    """The provider / endpoint / key / model half of setup."""
    options = [(key, preset.label) for key, preset in providers.PROVIDERS.items()]
    provider_key = ask_choice(
        "Which LLM provider?", options, default=current.get("provider")
    )
    preset = providers.PROVIDERS[provider_key]

    if provider_key == CUSTOM_PROVIDER_KEY:
        base_url = ask_valid(
            "base_url", "Enter the base URL (OpenAI-compatible, usually ends in /v1)")
        api_key_env = ask_text(
            "Enter the name of the environment variable holding your key "
            "(blank if none needed)",
            allow_empty=True,
        )
    else:
        default_url = (
            current.get("base_url")
            if current.get("provider") == provider_key and current.get("base_url")
            else preset.base_url
        )
        base_url = ask_valid("base_url", "Enter the base URL", default=default_url)
        if preset.api_key_env:
            api_key_env = ask_text(
                "Enter the name of the environment variable holding your key",
                default=preset.api_key_env,
            )
        else:
            api_key_env = ""
            print(f"  {preset.label.split('(')[0].strip()} needs no API key.")

    base_url = providers.normalize_base_url(base_url)

    ensure_key(
        api_key_env,
        label=provider_key,
        signup_url=preset.signup_url,
        confirm_existing=True,
    )
    api_key = os.environ.get(api_key_env) if api_key_env else None

    # Offer the configured model only when it belongs to the provider being
    # set up. Switching NVIDIA -> OpenAI used to hand OpenAI's picker
    # "deepseek-ai/deepseek-coder-6.7b-instruct" as its default, so pressing
    # Enter stored a guaranteed 404 - and it bites hardest on the path where
    # discovery failed, which is the one place the user has no list to correct
    # it from. Exactly the guard default_url above already applies.
    current_model = (
        current.get("model", "") if current.get("provider") == provider_key else ""
    )

    model = choose_model(
        base_url,
        api_key,
        current=current_model,
        catalog_url=preset.catalog_url,
    )

    return {
        "provider": provider_key,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
    }


# Text providers that also offer speech. Everything else (Groq, OpenRouter,
# Ollama) has no TTS of its own, which is why the menu reorders rather than
# filters - filtering would leave those users with "no voice" as the only choice.
# Derived rather than typed: a text provider pairs with the speech backend that
# reads the same key variable, which is exactly what "you already have a key for
# this" means. Written by hand it was a third provider table to keep in step.
_LLM_TO_TTS = {
    llm.key: speech.key
    for llm in providers.PROVIDERS.values()
    for speech in providers.TTS_PROVIDERS.values()
    if llm.api_key_env and llm.api_key_env == speech.api_key_env
}


# Riva speaks gRPC, so it needs NVIDIA's client library - the one part of
# voicelog that is not a plain HTTP call and therefore not a core dependency.
_RIVA_KEY = "riva"
_RIVA_FALLBACK_REQUIREMENT = "voicelog[tts]"


def _riva_available() -> bool:
    """Whether the Riva client can be imported right now."""
    try:
        from voicelog.tts import _import_riva

        _import_riva()
    except Exception:  # noqa: BLE001 - any import problem means unavailable
        return False
    return True


def _riva_requirement() -> str:
    """What to install for Riva speech, read from our own package metadata.

    Resolving it from the installed ``[tts]`` extra keeps pyproject.toml the
    single source of truth, so the version pin cannot drift out of sync with a
    string hardcoded here. Installing that dependency directly, rather than
    ``voicelog[tts]``, also leaves an editable/source checkout of voicelog
    untouched.
    """
    try:
        from importlib.metadata import requires

        for requirement in requires("voicelog") or []:
            if 'extra == "tts"' in requirement:
                return requirement.split(";")[0].strip()
    except Exception:  # noqa: BLE001 - no metadata (running from source)
        pass
    return _RIVA_FALLBACK_REQUIREMENT


def _install_riva() -> bool:
    """Install the Riva client with pip. Returns whether it worked.

    Runs pip in this interpreter so the package lands in the environment
    voicelog itself is running from - a global `pip` can easily be a different
    one. Output is left unredirected: an install is slow enough that silence
    looks like a hang.
    """
    requirement = _riva_requirement()
    print(f"  Running: {sys.executable} -m pip install {requirement}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", requirement],
            check=False,
        )
    except (OSError, ValueError) as exc:
        print(f"  Could not run pip ({exc}).")
        return False
    if result.returncode != 0:
        print(f"  pip exited with {result.returncode}.")
        return False
    return _riva_available()


def _offer_riva_install() -> None:
    """Ask to install the Riva client when it is missing.

    Never blocks: whatever happens, setup finishes and the written changelog
    keeps working. Only speech waits on the package.
    """
    if _riva_available():
        return

    requirement = _riva_requirement()
    print("  Riva speech needs an extra package (NVIDIA's gRPC client).")
    if not ask_yes_no("  Install it now?", default=True):
        print(f'  Skipped. Run `pip install "{_RIVA_FALLBACK_REQUIREMENT}"` '
              "when you want speech.")
        return

    if _install_riva():
        print("  Installed - speech is ready.")
    else:
        print(f'  Install it yourself with: pip install "{requirement}"')
        print("  Saving your settings anyway; the written changelog is unaffected.")


def _tts_options(llm_provider: str) -> list[tuple[str, str]]:
    """Speech options, with the one matching the text provider first.

    Every service stays on the list: pairing Groq text with ElevenLabs speech is
    a supported combination, not an accident. "No voice" stays last.
    """
    preferred = _LLM_TO_TTS.get((llm_provider or "").lower())
    keys = [key for key in providers.TTS_PROVIDERS if key != NO_TTS_KEY]
    if preferred in keys:
        keys.remove(preferred)
        keys.insert(0, preferred)
    keys.append(NO_TTS_KEY)
    return [(key, providers.TTS_PROVIDERS[key].label) for key in keys]


def _choose_tts(
    current: dict[str, Any], llm_key_env: str, llm_provider: str = ""
) -> dict[str, Any]:
    """The voice half of setup. Returns {"speak": False} if voice is declined."""
    options = _tts_options(llm_provider)
    # A backend the user actually configured wins over the affinity table. The
    # affinity was consulted first and "riva" is truthy, so an ElevenLabs user
    # re-running --setup to change only the model was switched back to Riva by
    # pressing Enter, blanking their voice, model and base_url with it. A stored
    # value equal to the built-in default is not a choice, so a fresh machine
    # still gets the affinity and the common case is still one Enter.
    configured = current.get("tts_provider") or ""
    if configured == config_module.DEFAULTS["tts_provider"]:
        configured = ""
    # Three tiers, and the last one is what keeps the invariant below true:
    # blanking a stored value that equals the built-in default left Groq,
    # OpenRouter, Ollama and custom - the providers with no speech affinity -
    # with no default at all, so Enter had nowhere to go.
    default = (
        configured
        or _LLM_TO_TTS.get((llm_provider or "").lower())
        or config_module.DEFAULTS["tts_provider"]
    )
    tts_key = ask_choice(
        "Read changelogs aloud with which voice?",
        options,
        default=default,
    )
    if tts_key == NO_TTS_KEY:
        print("  Voice off. The written changelog still works everywhere.")
        return {"speak": False}

    preset = providers.TTS_PROVIDERS[tts_key]

    # Asked here, immediately after choosing Riva, because it is a fact about
    # the backend rather than about the voice.
    if tts_key == _RIVA_KEY:
        _offer_riva_install()

    if preset.api_key_env == llm_key_env and os.environ.get(llm_key_env):
        tts_api_key_env = llm_key_env
        print(f"  Reusing ${llm_key_env} for speech.")
    else:
        tts_api_key_env = ask_text(
            "Enter the name of the environment variable holding your speech key",
            default=preset.api_key_env,
        )
        ensure_key(tts_api_key_env, label=tts_key, confirm_existing=True)

    if preset.voices:
        voice_options = [(voice, voice) for voice in preset.voices]
        # Keep the configured voice only if it belongs to this service;
        # otherwise offer that service's first voice, so pressing Enter all the
        # way through setup never dead-ends on a prompt with no default.
        if current.get("tts_provider") == tts_key and current.get("tts_voice"):
            voice_default = current["tts_voice"]
        else:
            voice_default = preset.voices[0]
        # Pinned for the same reason as the model picker: no preset ships 20
        # voices today, but an escape hatch that paging can hide is not one.
        voice = ask_choice(
            "Which voice?",
            voice_options,
            default=voice_default,
            pinned=[(MANUAL_ENTRY, f"Type {preset.voice_hint}")],
        )
        if voice == MANUAL_ENTRY:
            voice = ask_text("Voice")
    else:
        voice = ask_text(f"Voice ({preset.voice_hint})")

    # tts_function_id / tts_language / tts_sample_rate are deliberately not
    # written: they name an NVIDIA deployment, which can be retired the same way
    # a model id can. Leaving them out means a future default still reaches you.
    values = {
        "speak": True,
        "tts_provider": tts_key,
        "tts_voice": voice,
        "tts_api_key_env": tts_api_key_env,
    }

    if current.get("tts_provider") != tts_key:
        # A real switch, so clear the previous backend's own settings - the same
        # reasoning as cli._apply_overrides. save_user_config MERGES, so a
        # stored `tts_model: gpt-4o-mini-tts` would otherwise survive into an
        # ElevenLabs config and be sent as its model_id, and an OpenAI proxy URL
        # would be reused for a different service entirely.
        values["tts_model"] = ""
        values["tts_base_url"] = ""

    return values


def _warn_if_project_pins_model() -> None:
    """Say so when ./changelog.yml will override what we are about to save.

    Without this, someone picks a live model, still gets the retired-model
    error, and reasonably concludes that setup does nothing.
    """
    path = config_module.project_config_path()
    if not os.path.isfile(path):
        return
    try:
        project = config_module._read_yaml(path)
    except config_module.ConfigFileInvalid:
        return
    pinned = project.get("model")
    if pinned:
        print(
            f"\nNote: {path} sets `model: {pinned}`, which overrides the model "
            "you pick here for this repo. Remove it there to use your own."
        )


def run_setup(current: dict[str, Any], *, dest: str) -> dict[str, Any]:
    """Ask everything, and return only the keys the user chose.

    Args:
        current: the effective config as a dict, used for bracketed defaults so
            re-running ``--setup`` offers the current answers.
        dest: where the caller will write; shown so the user knows what changes.

    Returns:
        The config keys to persist. Never contains an API key.

    Raises:
        SetupAborted: the user backed out; the caller must not write anything.
    """
    print("\nvoicelog setup")
    print(f"  Writes your choices to: {dest}")
    print("  Your API key is NOT written there - only the name of the")
    print("  environment variable it lives in.")

    values = _choose_llm(current)
    values.update(
        _choose_tts(current, values["api_key_env"], values["provider"])
    )

    print("\nSummary")
    for key in ("provider", "base_url", "model", "api_key_env"):
        print(f"  {key}: {values[key] or '(none needed)'}")
    if values.get("speak"):
        print(f"  voice: {values['tts_voice']} via {values['tts_provider']}")
    else:
        print("  voice: off")

    _warn_if_project_pins_model()

    if not ask_yes_no(f"\nSave to {dest}?", default=True):
        raise SetupAborted()
    return values
