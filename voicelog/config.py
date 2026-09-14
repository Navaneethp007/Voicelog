"""Configuration loading for voicelog.

Three layers, later winning: built-in ``DEFAULTS``, the user's own config
(``%APPDATA%\\voicelog\\config.yml`` / ``~/.config/voicelog/config.yml``), then
the project's ``./changelog.yml``. CLI flags are applied on top by ``cli``.

The split is deliberate. Which provider, model, key env var and voice you use is
a personal choice, so it lives once per machine and every repo inherits it.
Which sections a changelog has, what commit noise to drop and where the voice
samples live are properties of a project, so they live in a file that project
can commit. Neither file ever holds an API key - only the *name* of the
environment variable to read one from.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from voicelog import fileio, providers


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class ConfigFileNotFound(Exception):
    """Raised when an explicitly-supplied config path does not exist."""


class ConfigFileInvalid(Exception):
    """Raised when a config file exists but cannot be parsed.

    Worth its own type because the user config is global and rarely opened: a
    stray bracket in it would otherwise break voicelog in every repo on the
    machine, with nothing naming the file at fault.
    """


class ConfigFileUnreadable(ConfigFileInvalid):
    """Raised when a config file exists but could not be opened at all.

    Kept distinct from a parse error because the consequences are opposite: an
    unparseable file has nothing worth preserving, so `voicelog --setup` may
    replace it, while an unreadable one is *intact* - the content is fine and
    something transient (a lock, a permission) is in the way. Overwriting that
    would destroy a working config to fix a problem it does not have.

    A subclass so that every existing ``except ConfigFileInvalid`` keeps
    catching it; only the handlers that care need to look for it specifically.
    """


class ConfigValueInvalid(ConfigFileInvalid):
    """A config file parsed, but one of its values cannot be used.

    A subclass so every existing ``except ConfigFileInvalid`` keeps catching
    it - but a separate type, because the repair is the opposite of a parse
    error's. A parse error has nothing worth keeping, so ``voicelog --setup``
    may overwrite the file. A *value* error is one bad line in a file that is
    otherwise correct: rewriting it preserves the bad line (the wizard merges,
    and it owns only 10 of 24 keys), and when the line lives in a project's
    ``changelog.yml`` the wizard never writes that file at all.
    """

    def __init__(self, message: str, *, key: str = "", path: str = "", reason: str = ""):
        super().__init__(message)
        self.key = key
        self.path = path
        self.reason = reason or message


# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------

USER_CONFIG_ENV = "VOICELOG_CONFIG_HOME"  # absolute path to the *directory*
_warned_relative_home = False
USER_CONFIG_NAME = "config.yml"
PROJECT_CONFIG_NAME = "changelog.yml"


def user_config_dir() -> str | None:
    """Directory holding the user-level config, or None if there is no home.

    Honours ``$VOICELOG_CONFIG_HOME`` first, which is how the test suite stays
    off the developer's real config and how anyone can relocate it. Resolved on
    every call, never cached in a module constant - both of those depend on it.
    """
    override = os.environ.get(USER_CONFIG_ENV)
    if override:
        if os.path.isabs(override):
            return override
        # A relative value would resolve against the working directory, making
        # the machine-global config silently per-repo. Unlike XDG_CONFIG_HOME
        # this is voicelog's own variable, so say so rather than ignoring it in
        # silence - but only once, since this runs several times per invocation.
        global _warned_relative_home
        if not _warned_relative_home:
            _warned_relative_home = True
            print(
                f"warning: ignoring ${USER_CONFIG_ENV}={override!r} - it must be "
                "an absolute path, or the 'global' config would differ per "
                "directory.",
                file=sys.stderr,
            )

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            home = os.path.expanduser("~")
            if home == "~":
                return None
            base = os.path.join(home, "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        # The XDG spec says a relative value must be ignored.
        if not base or not os.path.isabs(base):
            home = os.path.expanduser("~")
            if home == "~":
                return None
            base = os.path.join(home, ".config")

    return os.path.join(base, "voicelog")


def user_config_path() -> str | None:
    """Path to the user-level config file, whether or not it exists."""
    directory = user_config_dir()
    return os.path.join(directory, USER_CONFIG_NAME) if directory else None


def project_config_path() -> str:
    """Path to the current project's config file, whether or not it exists."""
    return os.path.join(os.getcwd(), PROJECT_CONFIG_NAME)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # Read from the registries, never restated: these used to be literals here
    # too, so the preset table and the defaults table could disagree with
    # nothing able to notice.
    "provider": providers.PROVIDERS["nvidia"].key,
    "base_url": providers.PROVIDERS["nvidia"].base_url,
    # No default model, on purpose. Provider model ids get retired - NVIDIA
    # retired mistralai/mistral-medium-3.5-128b and every default-config run
    # silently degraded to a raw commit list. `voicelog --setup` picks one from
    # the provider's live catalog; --model overrides it for a single run.
    "model": "",
    "api_key_env": providers.PROVIDERS["nvidia"].api_key_env,  # any provider
    "llm_timeout": 120.0,  # seconds to wait for the model before failing over
    "sections": ["Breaking", "Features", "Fixes", "Internal"],
    "noise": [r"^wip", r"^merge", r"^fmt", r"^chore\(deps\)"],
    "voice_samples": ".changelog/voice/",
    "fallback_commits": 50,
    "max_commits": 50,  # cap sent to the model on large ranges (keeps it fast/cheap)
    "onboard_commits": 15,  # recent commits used by --new to orient a fresh clone
    # --- Phase 2: speech + persistent casual changelog ---
    "speak": True,  # always speak (degrades gracefully if TTS unavailable)
    "tts_function_id": providers.TTS_PROVIDERS["riva"].function_id,
    "tts_voice": providers.TTS_PROVIDERS["riva"].voices[0],
    "tts_language": providers.TTS_PROVIDERS["riva"].language,
    "tts_sample_rate": providers.TTS_PROVIDERS["riva"].sample_rate,
    "tts_timeout": 90.0,  # seconds per speech request before giving up
    "tts_provider": providers.TTS_PROVIDERS["riva"].key,
    "tts_model": "",  # openai/elevenlabs model id (blank → provider default)
    "tts_base_url": "",  # openai override (blank → provider default)
    "tts_api_key_env": providers.TTS_PROVIDERS["riva"].api_key_env,
    "speech_detail": "brief",  # "brief" (2-3 sentences) or "detailed" (fuller rundown)
    "voice_md": ".changelog/voice.md",
    # Default is a transient rundown (print + speak). Set true (or pass --changelog)
    # to also maintain the persistent release changelog at voice_md.
    "write_changelog": False,
}

# Keys `voicelog --setup` owns, in the order they are written. Everything absent
# from a user's file keeps tracking DEFAULTS, so later default improvements
# still reach people who set up long ago.
_USER_KEY_ORDER = (
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "speak",
    "tts_provider",
    "tts_voice",
    "tts_api_key_env",
    "tts_model",
    "tts_base_url",
)

_USER_HEADER = """\
# voicelog user config - written by `voicelog --setup`.
#
# Personal choices (provider, model, which env var holds your key, voice) live
# here, so every repo you work in inherits them. Per-project settings - sections,
# commit noise patterns, voice_samples - belong in that project's changelog.yml,
# which overrides anything here.
#
# Your API key is NEVER stored in this file. Only the *name* of the environment
# variable to read it from.
#
# Re-running `voicelog --setup` rewrites this file and does not keep comments.
"""


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------
#
# Every key is checked here, once, so that a bad value names itself instead of
# escaping as a bare ValueError (a traceback in every repo on the machine) or -
# worse - being absorbed silently. The silent cases were the dangerous ones:
# `noise: ^wip` became four one-character regexes that discarded every commit,
# and `max_commits: 0` reached a *paid* model call with no input at all.

# Strings are type-checked, never coerced: str(1.5) would turn `model: 1.5`
# into the model id "1.5" and earn a 404 from the provider instead of naming the
# config line. An empty string stays legal - `api_key_env: ""` means "this
# endpoint needs no key".
_STR_KEYS = (
    "api_key_env",
    "tts_api_key_env",
    "tts_function_id",
    "tts_voice",
    "tts_language",
    "tts_model",
)

# Keys whose value must be stripped and lowercased before anything compares it.
# Only the flag path did this, so " NVIDIA " from a config file behaved the
# same as "nvidia" while hashing to a different cache key.
# What `origin` says for a value that came from a flag rather than a file.
_FLAG_SOURCE = "command line"

_CANONICAL_KEYS = ("provider", "tts_provider")

# Keys that are trimmed but keep their case - a model id is case-sensitive.
_STRIPPED_KEYS = ("model",)

# Paths get expanduser applied here, once, rather than in each consumer - which
# is why `voice_samples: ~/voice/` silently loaded nothing before.
_PATH_KEYS = ("voice_samples", "voice_md")

# Checked at load, so a URL httpx cannot even build never reaches a request:
# httpx raises InvalidURL for these, which is not an HTTPError, so it escaped
# the retry loop as a traceback rather than the documented commit-list fallback.
_URL_KEYS = ("base_url", "tts_base_url")

_POSITIVE_INT_KEYS = (
    "fallback_commits",
    "max_commits",
    "onboard_commits",
    "tts_sample_rate",
)
_POSITIVE_FLOAT_KEYS = ("llm_timeout", "tts_timeout")
_BOOL_KEYS = ("speak", "write_changelog")
_STRING_LIST_KEYS = ("sections", "noise")
_PATTERN_LIST_KEYS = ("noise",)
# Membership comes from the provider registries rather than a second list, so
# adding a provider cannot leave the config validator behind. This is what the
# flag path has always done via _validate_flags; a config file skipped it
# entirely, and an unknown name only surfaced later as a confusing failure.
_CHOICE_KEYS = {
    "speech_detail": ("brief", "detailed"),
    "provider": tuple(providers.PROVIDERS),
    "tts_provider": tuple(providers.TTS_PROVIDERS),
}

# bool("false") is True, so a quoted value used to mean the opposite of what it
# said. A dict lookup, deliberately not a parser.
_BOOL_WORDS = {"true": True, "false": False, "yes": True, "no": False}


def _invalid(key: str, value: object, expected: str, origin: dict[str, str] | None):
    """Build a ConfigValueInvalid that says where to go and what to change."""
    where = (origin or {}).get(key)
    if where and where.startswith("--"):
        # A flag, not a file: there is no line to edit and nothing for --setup
        # to rewrite. Naming the flag and the valid values is the whole fix.
        lines = [f"{where}: {value!r} {expected}."]
    else:
        lines = [
            f"{where or 'built-in defaults'}: {key}: {value!r} {expected}.",
            f"Edit that line (remove it to use the default, "
            f"{DEFAULTS[key]!r}), then run voicelog again.",
        ]
        if key in _USER_KEY_ORDER:
            # True only for the keys the wizard actually writes.
            lines.append("Or run `voicelog --setup`, which rewrites that setting.")
    return ConfigValueInvalid(
        " ".join(lines),
        key=key,
        path=where or "",
        reason=f"{key}: {value!r} {expected}",
    )


def _as_bool(key, value, origin) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _BOOL_WORDS:
        return _BOOL_WORDS[value.strip().lower()]
    raise _invalid(key, value, "is not true or false", origin)


def _as_positive_int(key, value, origin) -> int:
    """A whole number >= 1, accepting the spellings YAML users actually write.

    `max_commits: "50"` and `tts_sample_rate: 44100.0` mean exactly what they
    look like. 0.1/0.2 coerced with int(); replacing that with a strict
    isinstance check made both fatal on upgrade, while `speak: "false"` kept
    working - an asymmetry that was an oversight, not a policy. Spelling is
    relaxed here; meaning is not, so 0, -1 and 1.5 are still errors.
    """
    # bool is an int subclass, so `max_commits: true` would silently cap to one.
    if isinstance(value, bool):
        raise _invalid(key, value, "is not a whole number", origin)
    if isinstance(value, float):
        if not value.is_integer():
            raise _invalid(key, value, "is not a whole number", origin)
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise _invalid(key, value, "is not a whole number", origin) from None
    elif not isinstance(value, int):
        raise _invalid(key, value, "is not a whole number", origin)
    if value < 1:
        raise _invalid(key, value, "must be at least 1", origin)
    return value


def _as_positive_float(key, value, origin) -> float:
    """A number > 0, accepting a quoted one for the same reason as above."""
    if isinstance(value, bool):
        raise _invalid(key, value, "is not a number", origin)
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise _invalid(key, value, "is not a number", origin) from None
    elif not isinstance(value, (int, float)):
        raise _invalid(key, value, "is not a number", origin)
    if value <= 0:
        raise _invalid(key, value, "must be greater than 0", origin)
    return float(value)


def _as_string_list(key, value, origin) -> list[str]:
    # A YAML scalar can only mean one item, so `noise: ^wip` is unambiguous -
    # and reading it as a list of characters was silent data loss.
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise _invalid(key, value, "is not a list of strings", origin)
    for item in value:
        if not isinstance(item, str):
            raise _invalid(key, item, "is not a string", origin)
    return list(value)


def check_value(key: str, value: Any) -> Any:
    """Validate one value exactly as ``load`` would, returning it normalised.

    Public so the wizard can accept only what the writer will keep. Those were
    two separate judgements, and they disagreed: typing `localhost:11434/v1` at
    the base-URL prompt was accepted, echoed back in the Summary and reported
    "Saved", then dropped by save_user_config's repair loop - leaving the
    effective base_url silently falling back to NVIDIA's.
    """
    return _check_key(key, value)


def _check_key(
    key: str, value: Any, origin: dict[str, str] | None = None, *, normalize: bool = True
) -> Any:
    """Validate one key and return the value to use. Raises ConfigValueInvalid.

    Per-key rather than whole-dict so the writer can reuse it: ``--setup`` needs
    to drop the values it cannot use, one at a time, rather than give up on the
    whole file.

    ``normalize=False`` checks without transforming, which is what writing back
    needs: expanding ``~`` is right for *using* a path and wrong for *storing*
    one, since it bakes a machine-specific absolute path into a config the user
    deliberately wrote portably.
    """
    if key in _STR_KEYS:
        if not isinstance(value, str):
            raise _invalid(key, value, "is not text", origin)
        return value

    if key in _STRIPPED_KEYS:
        if not isinstance(value, str):
            raise _invalid(key, value, "is not text", origin)
        # Stripped here rather than at the one flag that happened to do it, so
        # " m " and "m" cannot be two cache entries for one model.
        return value.strip()

    if key in _CANONICAL_KEYS:
        if not isinstance(value, str):
            raise _invalid(key, value, "is not text", origin)
        value = value.strip().lower()

    if key in _PATH_KEYS:
        if not isinstance(value, str):
            raise _invalid(key, value, "is not a path", origin)
        return os.path.expanduser(value) if normalize else value

    if key in _URL_KEYS:
        if not isinstance(value, str):
            raise _invalid(key, value, "is not a URL", origin)
        if value:  # blank means "use the provider default"
            try:
                # httpx itself is the authority on what it can build a request
                # from: urlsplit silently *strips* the stray newline a paste
                # leaves behind and accepts a non-ASCII host, both of which
                # httpx rejects. Its own scheme/host check is what catches a
                # bare "api.example.com", which httpx accepts as a path.
                parsed = urllib.parse.urlsplit(value)
                if not parsed.scheme or not parsed.netloc:
                    raise ValueError("missing a scheme or host")
                httpx.URL(value)
            except (httpx.InvalidURL, ValueError, UnicodeError, TypeError) as exc:
                raise _invalid(key, value, f"is not a usable URL ({exc})", origin)
        # Normalised here, not only at --base-url: the same endpoint written
        # with a trailing slash was a second cache key and a second bill.
        return providers.normalize_base_url(value)

    if key in _POSITIVE_INT_KEYS:
        return _as_positive_int(key, value, origin)
    if key in _POSITIVE_FLOAT_KEYS:
        return _as_positive_float(key, value, origin)
    if key in _BOOL_KEYS:
        return _as_bool(key, value, origin)

    if key in _STRING_LIST_KEYS:
        value = _as_string_list(key, value, origin)
        if key in _PATTERN_LIST_KEYS:
            for pattern in value:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    # Caught here so the message names the file, rather than
                    # letting re.error escape from filters.drop_noise mid-run.
                    raise _invalid(key, pattern, f"is not a valid regex ({exc})", origin)
        return value

    if key in _CHOICE_KEYS:
        allowed = _CHOICE_KEYS[key]
        if value not in allowed:
            raise _invalid(key, value, f"is not one of {', '.join(allowed)}", origin)
        return value

    return value  # not a key we own; not ours to judge


def _validated(data: dict[str, Any], origin: dict[str, str] | None) -> dict[str, Any]:
    """Return ``data`` with every value checked, and paths expanded."""
    return {key: _check_key(key, value, origin) for key, value in data.items()}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class Config:
    provider: str
    base_url: str
    model: str
    sections: list[str]
    noise: list[str]
    voice_samples: str
    fallback_commits: int
    speak: bool
    tts_function_id: str
    tts_voice: str
    tts_language: str
    tts_sample_rate: int
    voice_md: str
    tts_timeout: float = 90.0  # default keeps existing Config(...) call sites valid
    max_commits: int = 50
    llm_timeout: float = 120.0
    api_key_env: str = "NVIDIA_API_KEY"
    tts_api_key_env: str = "NVIDIA_API_KEY"
    write_changelog: bool = False
    speech_detail: str = "brief"
    tts_provider: str = "riva"
    tts_model: str = ""
    tts_base_url: str = ""
    onboard_commits: int = 15


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _read_yaml(path: str) -> dict[str, Any]:
    """Parse a config file into a dict.

    A file whose top level is not a mapping (someone hand-edits it into a list)
    yields {} rather than exploding later inside the merge.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigFileUnreadable(f"{path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # PyYAML raises this from inside safe_load, and it is a ValueError -
        # so it matched neither handler and escaped load(), read_user_config()
        # and save_user_config() as a traceback. The base class, deliberately:
        # ConfigFileUnreadable promises an intact file behind a transient
        # obstacle that --setup must not overwrite, while a mis-encoded file
        # will never decode and has nothing worth preserving. Naming the
        # encoding matters - "could not read" sends people to file permissions.
        # No byte offset: PyYAML decodes in chunks, so exc.start is relative to
        # a chunk rather than the file and would point at the wrong place.
        raise ConfigFileInvalid(
            f"{path}: not valid UTF-8 ({exc.reason}). "
            "Re-save the file as UTF-8, or run `voicelog --setup` to rewrite it."
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigFileInvalid(f"{path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def read_user_config() -> dict[str, Any]:
    """Parsed user-level config, or {} when there is none."""
    path = user_config_path()
    if not path or not os.path.isfile(path):
        return {}
    return _read_yaml(path)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

# Keys a provider preset owns. When a layer switches provider without naming
# one of these itself, the preset supplies it - otherwise an NVIDIA key goes to
# an OpenAI endpoint, or a Magpie voice name to ElevenLabs.
_PROVIDER_DERIVED = ("base_url", "api_key_env")
_TTS_DERIVED = ("tts_api_key_env", "tts_voice", "tts_model", "tts_base_url")


def _preset_values(tts_key: str) -> dict[str, Any]:
    """What a TTS preset contributes when a layer switches to it."""
    preset = providers.get_tts(tts_key)
    if preset is None:
        return {}
    return {
        "tts_api_key_env": preset.api_key_env,
        # Blank where there is no sensible default - ElevenLabs ids are
        # per-account - so tts.py gives its actionable "needs tts_voice set".
        "tts_voice": preset.voices[0] if preset.voices else "",
        # Backend-specific, and meaningless to carry across a switch.
        "tts_model": "",
        "tts_base_url": "",
    }


def _canonical(value: Any) -> str | None:
    """A provider name for comparison, or None if it is not even a string."""
    return value.strip().lower() if isinstance(value, str) else None


def _reconcile(
    merged: dict, layer: dict, source: str, origin: dict, derived: set[str]
) -> None:
    """Fill preset-owned keys that *this layer* switched provider without naming.

    The rule is per layer and fires only on a change:

        when a layer sets `provider` to something other than the provider the
        layers beneath it resolved to, every preset-owned key that layer does
        not itself set is taken from the preset.

    Firing only on a change is what lets the wizard write a hand-typed base_url
    next to `provider: nvidia` and keep it - an unconditional reseed silently
    undid exactly that. cli._apply_overrides has always applied this rule to
    flags; running it for every layer is what makes `provider: openai` in a
    file mean what `--provider openai` means.
    """
    def take(key: str, value: str) -> None:
        """Take ``value`` from the preset for a key this layer did not set."""
        if key in layer:
            return
        merged[key] = value
        origin[key] = source
        derived.add(key)

    def take_unless_named(key: str, value: str) -> None:
        """As ``take``, but a *blank* preset value never overrules the user.

        The two blanks in this file mean opposite things, and conflating them
        broke one case each way.

        A blank from a *provider* preset is an absence of opinion: `custom`
        exists precisely because voicelog knows nothing about the endpoint.
        Treating its empty api_key_env as an instruction blanked a variable the
        user had named for their own endpoint, turning a working setup into a
        401 reported as "the API key was rejected by custom".

        A blank from a *speech* preset is an instruction - see _preset_values,
        which writes "" deliberately so a backend-specific model id or voice
        cannot survive a switch. Those go through ``take``.

        "Named by the user" means a layer wrote it and no preset supplied it;
        inheriting NVIDIA's default and carrying it to an arbitrary --base-url
        host is the leak this still prevents.
        """
        named_here = key in origin and key not in derived
        if not value and named_here:
            return
        take(key, value)

    new_provider = _canonical(layer.get("provider"))
    if new_provider and new_provider != _canonical(merged.get("provider")):
        preset = providers.get(new_provider)
        if preset is not None:
            take_unless_named("base_url", preset.base_url)
            take_unless_named("api_key_env", preset.api_key_env)

    new_tts = _canonical(layer.get("tts_provider"))
    if new_tts and new_tts != _canonical(merged.get("tts_provider")):
        for key, value in _preset_values(new_tts).items():
            take(key, value)



def _assemble(
    layers: list[tuple[str, dict[str, Any]]],
    overrides: dict[str, Any] | None = None,
    overrides_origin: dict[str, str] | None = None,
) -> Config:
    """Merge every layer over ``DEFAULTS`` and build the Config.

    Shared by ``load`` and ``defaults_config`` so the two cannot diverge:
    the recovery path used to build straight from DEFAULTS and so silently
    lost the flag layer, the preset reconciliation and the `none` rule.
    """
    layers = list(layers)
    if overrides:
        layers.append((_FLAG_SOURCE, overrides))

    # Track which file each value came from, so a bad value can name its source.
    merged: dict[str, Any] = dict(DEFAULTS)
    origin: dict[str, str] = {}
    # Keys whose current value came from a preset rather than from anything the
    # user wrote. Only these may be overwritten by a preset that has nothing to
    # say - see seed() in _reconcile.
    derived: set[str] = set()
    for source, layer in layers:
        layer = {k: v for k, v in layer.items() if v is not None}
        _reconcile(merged, layer, source, origin, derived)
        for key, value in layer.items():
            merged[key] = value
            derived.discard(key)   # written by hand, so no longer inherited
            if source == _FLAG_SOURCE:
                origin[key] = (overrides_origin or {}).get(key, source)
            else:
                origin[key] = source

    # Resolved once, after every layer, because it is a fact about the final
    # value rather than about any one layer. `none` is not a backend - it is
    # the absence of one - so "speak" and "there is nothing to speak with"
    # cannot both hold, whichever layer said what. Deciding it per layer left
    # `--tts-provider none --voice X` (and a file setting both) with speak on
    # and no adapter, so every run printed "Speaking..." and then raised
    # unknown tts_provider 'none'.
    if _canonical(merged.get("tts_provider")) == providers.NO_TTS_KEY:
        merged["speak"] = False
        origin["speak"] = origin.get("tts_provider", origin.get("speak", ""))

    return _build(merged, origin)


def load(
    path: str | None,
    overrides: dict[str, Any] | None = None,
    overrides_origin: dict[str, str] | None = None,
) -> Config:
    """Load configuration and return a Config instance.

    Precedence, later winning: ``DEFAULTS`` < user config < ``./changelog.yml``
    < ``overrides`` (the CLI flags, built by ``cli._flag_overrides``).

    Flags are a layer here rather than a second pass applied afterwards, so
    every setting means the same thing however it arrived: one validator, one
    set of preset rules, one normalisation. ``overrides_origin`` names the flag
    each override came from, so a bad value can point at ``--provider`` rather
    than at a file.

    - ``path=None`` → discover the user config, then ``./changelog.yml`` if present.
    - ``path`` given → that file *replaces both discovered layers*, so
      ``--config ci.yml`` means the same thing on any machine. A missing file
      raises ``ConfigFileNotFound``.

    ``DEFAULTS`` always underpins the merge, which is what lets ``_build``
    subscript every key: however partial the files are, no key can be absent.
    A key present but valueless (``model:``) counts as unset rather than null -
    otherwise it would beat the default and reach ``int(None)``.
    """
    if path is not None:
        if not os.path.isfile(path):
            raise ConfigFileNotFound(path)
        layers = [(path, _read_yaml(path))]
    else:
        user_path = user_config_path()
        layers = [(user_path or USER_CONFIG_NAME, read_user_config())]
        candidate = project_config_path()
        if os.path.isfile(candidate):
            layers.append((candidate, _read_yaml(candidate)))

    return _assemble(layers, overrides, overrides_origin)


def _build(data: dict[str, Any], origin: dict[str, str] | None = None) -> Config:
    """Validate a fully-merged dict and build a Config from it.

    Subscripts every key deliberately: ``load`` guarantees ``DEFAULTS`` is the
    base layer, so a missing key here means that guarantee broke.

    ``origin`` maps a key to the file it came from, so a bad value can name its
    source. It is optional because ``defaults_config()`` has no files involved.
    """
    data = _validated(data, origin)
    return Config(
        provider=data["provider"],
        base_url=data["base_url"],
        model=data["model"],
        sections=data["sections"],
        noise=data["noise"],
        voice_samples=data["voice_samples"],
        fallback_commits=data["fallback_commits"],
        speak=data["speak"],
        tts_function_id=data["tts_function_id"],
        tts_voice=data["tts_voice"],
        tts_language=data["tts_language"],
        tts_sample_rate=data["tts_sample_rate"],
        voice_md=data["voice_md"],
        tts_timeout=data["tts_timeout"],
        max_commits=data["max_commits"],
        llm_timeout=data["llm_timeout"],
        api_key_env=data["api_key_env"],
        tts_api_key_env=data["tts_api_key_env"],
        write_changelog=data["write_changelog"],
        speech_detail=data["speech_detail"],
        tts_provider=data["tts_provider"],
        tts_model=data["tts_model"],
        tts_base_url=data["tts_base_url"],
        onboard_commits=data["onboard_commits"],
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def defaults_config(
    overrides: dict[str, Any] | None = None,
    overrides_origin: dict[str, str] | None = None,
) -> Config:
    """A Config from ``DEFAULTS`` and the flags, ignoring every file.

    For the one case that needs it: a config file is unusable and the user is
    running ``voicelog --setup``, which is the command that rewrites it.

    It still takes ``overrides`` because flags are a layer, not a second pass -
    dropping them here meant `--setup --provider groq` against a broken config
    opened the wizard preselected on the default provider.
    """
    return _assemble([], overrides, overrides_origin)


def _backup_path(target: str, limit: int = 20) -> str:
    """First free ``<target>.bak``, ``.bak.1``, ... so no backup is overwritten.

    The first one keeps the plain ``.bak`` name, which is what anyone looking
    for it would guess. After ``limit`` the oldest numbered slot is reused - a
    bounded loop matters more here than keeping every generation of a file that
    was already unparseable.
    """
    for suffix in ("", *(f".{n}" for n in range(1, limit))):
        candidate = f"{target}.bak{suffix}"
        if not os.path.exists(candidate):
            return candidate
    return f"{target}.bak.{limit}"


def save_user_config(values: dict[str, Any], path: str | None = None) -> str:
    """Merge ``values`` into the user config file and write it atomically.

    Only the given keys are written or replaced; keys the user added by hand
    survive, and every key left out keeps tracking ``DEFAULTS``.

    The write goes to a temp file in the same directory and is then renamed, so
    a Ctrl-C can never leave a half-written global config that breaks voicelog
    in every repo. Never pass a secret in ``values``.

    Returns:
        The path written.

    Raises:
        OSError: the directory or file could not be written. Callers are
            expected to print the config for the user to save by hand.
    """
    target = path or user_config_path()
    if not target:
        raise OSError("no writable user config location (no home directory found)")

    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)

    data: dict[str, Any] = {}
    if os.path.isfile(target):
        try:
            data = _read_yaml(target)
        except ConfigFileUnreadable:
            # Intact but unavailable. Writing would replace content we were
            # never able to look at, so refuse and let the caller say why.
            raise
        except ConfigFileInvalid:
            # `voicelog --setup` is how a broken config gets fixed, so an
            # unparseable file must not block the write. There are no keys to
            # preserve from it, but it is kept alongside rather than destroyed.
            backup = _backup_path(target)
            try:
                os.replace(target, backup)
                print(f"note: kept the unreadable config as {backup}", file=sys.stderr)
            except OSError:
                pass  # cannot preserve it; proceed rather than block the repair
    data.update(values)

    # Drop values this version cannot use, so `voicelog --setup` genuinely
    # repairs a broken config instead of preserving the bad line and exiting 0.
    # Only keys we own are judged; anything hand-added that we do not recognise
    # is left exactly as written.
    for key in [k for k in data if k in DEFAULTS]:
        try:
            data[key] = _check_key(key, data[key], {key: target}, normalize=False)
        except ConfigValueInvalid as exc:
            print(f"note: dropped unusable {exc.reason}", file=sys.stderr)
            del data[key]

    ordered = {key: data[key] for key in _USER_KEY_ORDER if key in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})

    body = _USER_HEADER + yaml.safe_dump(
        ordered, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    # 0600: a personal file naming the env vars that hold keys - cheap even
    # with no secret in it. Raises on failure, so --setup can print the YAML
    # for the user to save by hand rather than claim it saved.
    fileio.atomic_write_text(target, body, mode=0o600)
    return target
