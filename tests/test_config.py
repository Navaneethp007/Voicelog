"""Tests for voicelog/config.py — written BEFORE any implementation (TDD RED phase)."""
from __future__ import annotations

import os
import textwrap
from unittest import mock

import pytest
import yaml

from voicelog import config
from voicelog.config import Config, ConfigFileNotFound, DEFAULTS, load


# ---------------------------------------------------------------------------
# 1. load(None) with no changelog.yml present → returns defaults
# ---------------------------------------------------------------------------

def test_load_none_no_file_returns_defaults(tmp_path, monkeypatch):
    """When path=None and no changelog.yml exists in cwd, silently use DEFAULTS."""
    monkeypatch.chdir(tmp_path)  # empty dir — no changelog.yml
    cfg = load(None)
    assert isinstance(cfg, Config)
    assert cfg.provider == DEFAULTS["provider"]
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.model == DEFAULTS["model"]
    assert cfg.sections == DEFAULTS["sections"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]
    assert cfg.fallback_commits == DEFAULTS["fallback_commits"]


# ---------------------------------------------------------------------------
# 2. load(path) with a real YAML file → file values override defaults,
#    absent keys fall back to defaults
# ---------------------------------------------------------------------------

def test_load_path_with_partial_yaml_merges_over_defaults(tmp_path):
    """File values win; keys absent from the file fall back to DEFAULTS."""
    config_file = tmp_path / "my_changelog.yml"
    config_file.write_text(
        textwrap.dedent("""\
            provider: openai
            model: gpt-4o
        """),
        encoding="utf-8",
    )

    cfg = load(str(config_file))

    # overridden by file
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"

    # not in file → fall back to defaults
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.sections == DEFAULTS["sections"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]
    assert cfg.fallback_commits == DEFAULTS["fallback_commits"]


# ---------------------------------------------------------------------------
# 3. load("missing.yml") → raises ConfigFileNotFound
# ---------------------------------------------------------------------------

def test_load_explicit_missing_path_raises(tmp_path):
    """Passing a path that does not exist must raise ConfigFileNotFound."""
    missing = str(tmp_path / "does_not_exist.yml")
    with pytest.raises(ConfigFileNotFound):
        load(missing)


# ---------------------------------------------------------------------------
# 4. YAML file that sets sections and fallback_commits → those land in Config,
#    others stay default
# ---------------------------------------------------------------------------

def test_load_yaml_sections_and_fallback_commits(tmp_path):
    """sections list and fallback_commits integer are correctly parsed and stored."""
    config_file = tmp_path / "changelog.yml"
    config_file.write_text(
        textwrap.dedent("""\
            sections:
              - Breaking
              - Features
              - Deprecations
            fallback_commits: 100
        """),
        encoding="utf-8",
    )

    cfg = load(str(config_file))

    assert cfg.sections == ["Breaking", "Features", "Deprecations"]
    assert cfg.fallback_commits == 100

    # keys not mentioned → still defaults
    assert cfg.provider == DEFAULTS["provider"]
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.model == DEFAULTS["model"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]


# ---------------------------------------------------------------------------
# No shipped model default
# ---------------------------------------------------------------------------

def test_no_model_ships_as_a_default():
    """Standing guard: a shipped model id gets retired out from under us.

    NVIDIA retired mistralai/mistral-medium-3.5-128b and every default-config
    run silently degraded to a commit list. The user picks a model instead.
    """
    assert DEFAULTS["model"] == ""


# ---------------------------------------------------------------------------
# User-config discovery
# ---------------------------------------------------------------------------

def test_user_config_home_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    assert config.user_config_dir() == str(tmp_path / "elsewhere")


def test_relative_user_config_home_is_ignored(monkeypatch, tmp_path):
    """A relative value would make the machine-global config per-directory."""
    monkeypatch.setattr(config, "_warned_relative_home", False)
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", "relative/path")
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: str(tmp_path / "home"))

    assert config.user_config_dir() == os.path.join(
        str(tmp_path / "home"), ".config", "voicelog"
    )


def test_a_relative_user_config_home_warns_once(monkeypatch, tmp_path, capsys):
    """user_config_dir runs several times per invocation."""
    monkeypatch.setattr(config, "_warned_relative_home", False)
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", "relative/path")
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: str(tmp_path / "home"))

    config.user_config_dir()
    config.user_config_dir()
    config.user_config_dir()

    assert capsys.readouterr().err.lower().count("ignoring") == 1


def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("VOICELOG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    assert config.user_config_dir() == os.path.join(str(tmp_path / "Roaming"), "voicelog")


def test_posix_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("VOICELOG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert config.user_config_dir() == os.path.join(str(tmp_path / "xdg"), "voicelog")


def test_relative_xdg_config_home_is_ignored(monkeypatch, tmp_path):
    """The XDG spec says a relative XDG_CONFIG_HOME must be ignored."""
    monkeypatch.delenv("VOICELOG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: str(tmp_path / "home"))

    assert config.user_config_dir() == os.path.join(
        str(tmp_path / "home"), ".config", "voicelog"
    )


def test_posix_falls_back_to_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("VOICELOG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: str(tmp_path / "home"))

    assert config.user_config_dir() == os.path.join(
        str(tmp_path / "home"), ".config", "voicelog"
    )


def test_user_config_dir_is_none_when_home_is_unresolvable(monkeypatch):
    """No resolvable home (service account, odd container) must not crash."""
    monkeypatch.delenv("VOICELOG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: "~")

    assert config.user_config_dir() is None
    assert config.user_config_path() is None


def test_user_config_dir_resolved_at_call_time(monkeypatch, tmp_path):
    """Not a module constant - test isolation and --setup both depend on this."""
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", str(tmp_path / "a"))
    first = config.user_config_dir()
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", str(tmp_path / "b"))

    assert config.user_config_dir() != first


# ---------------------------------------------------------------------------
# Precedence: DEFAULTS < user config < project changelog.yml
# ---------------------------------------------------------------------------

def _write_user_config(body):
    path = config.user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def test_user_config_overrides_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_user_config("model: user/model\nprovider: groq\n")

    cfg = load(None)

    assert cfg.model == "user/model"
    assert cfg.provider == "groq"
    assert cfg.sections == DEFAULTS["sections"]  # untouched keys still track DEFAULTS


def test_project_config_overrides_user_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_user_config("model: user/model\nprovider: groq\n")
    (tmp_path / "changelog.yml").write_text("model: project/model\n", encoding="utf-8")

    cfg = load(None)

    assert cfg.model == "project/model"
    assert cfg.provider == "groq"  # the project file only overrode what it set


def test_explicit_config_path_ignores_both_discovered_layers(monkeypatch, tmp_path):
    """--config PATH must stay hermetic, or CI inherits personal settings."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("model: user/model\nprovider: groq\n")
    (tmp_path / "changelog.yml").write_text("model: project/model\n", encoding="utf-8")
    explicit = tmp_path / "pinned.yml"
    explicit.write_text("model: pinned/model\n", encoding="utf-8")

    cfg = load(str(explicit))

    assert cfg.model == "pinned/model"
    assert cfg.provider == DEFAULTS["provider"]  # NOT groq


def test_single_key_user_config_does_not_break_build(monkeypatch, tmp_path):
    """_build subscripts all 24 keys, so DEFAULTS must stay the base layer."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("model: only/this\n")

    cfg = load(None)

    assert cfg.model == "only/this"
    assert cfg.tts_sample_rate == DEFAULTS["tts_sample_rate"]


def test_valueless_key_means_unset_not_null(monkeypatch, tmp_path):
    """A bare `model:` used to beat the default and crash _build on int(None)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text(
        "model:\nfallback_commits:\nsections:\n", encoding="utf-8"
    )

    cfg = load(None)

    assert cfg.model == DEFAULTS["model"]
    assert cfg.fallback_commits == DEFAULTS["fallback_commits"]
    assert cfg.sections == DEFAULTS["sections"]


def test_non_mapping_config_file_is_ignored(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("- a\n- b\n", encoding="utf-8")

    assert load(None).sections == DEFAULTS["sections"]


def test_malformed_yaml_raises_config_file_invalid(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("model: [unclosed\n", encoding="utf-8")

    with pytest.raises(config.ConfigFileInvalid) as exc:
        load(None)

    assert "changelog.yml" in str(exc.value)


def test_malformed_user_config_names_its_path(monkeypatch, tmp_path):
    """A global file nobody remembers must say which file is broken."""
    monkeypatch.chdir(tmp_path)
    path = _write_user_config("provider: [oops\n")

    with pytest.raises(config.ConfigFileInvalid) as exc:
        load(None)

    assert os.path.basename(path) in str(exc.value)


# ---------------------------------------------------------------------------
# save_user_config
# ---------------------------------------------------------------------------

def test_save_user_config_round_trips(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    written = config.save_user_config({"model": "a/b", "provider": "groq"})

    assert os.path.isfile(written)
    assert load(None).model == "a/b"


def test_save_user_config_writes_only_the_given_keys(monkeypatch, tmp_path):
    """Untouched keys must keep tracking DEFAULTS so improvements reach users."""
    monkeypatch.chdir(tmp_path)

    config.save_user_config({"model": "a/b"})

    assert set(config.read_user_config()) == {"model"}


def test_save_user_config_preserves_hand_added_keys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_user_config("max_commits: 7\nmodel: old/model\n")

    config.save_user_config({"model": "new/model"})

    stored = config.read_user_config()
    assert stored["model"] == "new/model"
    assert stored["max_commits"] == 7


def test_save_user_config_leaves_no_temp_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    written = config.save_user_config({"model": "a/b"})

    siblings = os.listdir(os.path.dirname(written))
    assert not [name for name in siblings if name.endswith(".tmp")]


def test_save_user_config_header_says_keys_are_not_stored(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    written = config.save_user_config({"model": "a/b"})

    with open(written, encoding="utf-8") as fh:
        head = fh.read()
    assert head.lstrip().startswith("#")
    assert "key" in head.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_save_user_config_is_owner_only_on_posix(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    written = config.save_user_config({"model": "a/b"})

    assert (os.stat(written).st_mode & 0o777) == 0o600


def test_read_user_config_is_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert config.read_user_config() == {}


def test_save_over_a_corrupt_config_backs_it_up_instead_of_failing(monkeypatch, tmp_path):
    """`voicelog --setup` is the repair for a broken config, so saving cannot
    be blocked by the very file it is replacing - but the old content is kept."""
    monkeypatch.chdir(tmp_path)
    broken = _write_user_config("provider: [unclosed\n")

    written = config.save_user_config({"model": "fixed/model"})

    assert config.read_user_config()["model"] == "fixed/model"
    with open(written + ".bak", encoding="utf-8") as fh:
        assert "unclosed" in fh.read()
    assert broken == written


# ---------------------------------------------------------------------------
# Unreadable is not the same as unparseable
# ---------------------------------------------------------------------------

def test_unreadable_file_raises_the_unreadable_subclass(monkeypatch, tmp_path):
    """A locked or permission-denied file is intact, not corrupt."""
    path = tmp_path / "changelog.yml"
    path.write_text("model: fine/model\n", encoding="utf-8")

    with mock.patch("builtins.open", side_effect=PermissionError("locked by another process")):
        with pytest.raises(config.ConfigFileUnreadable) as exc:
            config._read_yaml(str(path))

    assert "locked" in str(exc.value)


def test_unreadable_is_a_kind_of_invalid():
    """Existing `except ConfigFileInvalid` handlers must keep catching it."""
    assert issubclass(config.ConfigFileUnreadable, config.ConfigFileInvalid)


def test_parse_error_raises_the_base_class_not_the_subclass(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("model: [unclosed\n", encoding="utf-8")

    with pytest.raises(config.ConfigFileInvalid) as exc:
        load(None)

    assert not isinstance(exc.value, config.ConfigFileUnreadable)


def test_save_refuses_to_touch_a_file_it_could_not_read(monkeypatch, tmp_path):
    """The repair for "can't read" is not "overwrite"; the keys are still there."""
    monkeypatch.chdir(tmp_path)
    original = "max_commits: 7\nmodel: precious/model\n"
    path = _write_user_config(original)

    def _unreadable(_path):
        raise config.ConfigFileUnreadable(f"{_path}: locked")

    monkeypatch.setattr(config, "_read_yaml", _unreadable)

    with pytest.raises(config.ConfigFileUnreadable):
        config.save_user_config({"model": "new/model"})

    with open(path, encoding="utf-8") as fh:
        assert fh.read() == original       # untouched
    assert not os.path.exists(path + ".bak")
    assert not os.path.exists(path + ".tmp")


def test_a_second_corruption_does_not_clobber_the_first_backup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    _write_user_config("provider: [first\n")
    written = config.save_user_config({"model": "one"})
    _write_user_config("provider: [second\n")
    config.save_user_config({"model": "two"})

    with open(written + ".bak", encoding="utf-8") as fh:
        assert "first" in fh.read()
    with open(written + ".bak.1", encoding="utf-8") as fh:
        assert "second" in fh.read()


# ---------------------------------------------------------------------------
# Value validation: a bad value must name itself, never be absorbed
# ---------------------------------------------------------------------------

def _load_with_user_config(monkeypatch, tmp_path, body):
    monkeypatch.chdir(tmp_path)
    _write_user_config(body)
    return load(None)


@pytest.mark.parametrize("body, key", [
    ("max_commits: many\n", "max_commits"),
    ("max_commits: 0\n", "max_commits"),
    ("max_commits: -1\n", "max_commits"),
    ("max_commits: true\n", "max_commits"),
    ("fallback_commits: 0\n", "fallback_commits"),
    ("onboard_commits: lots\n", "onboard_commits"),
    ("tts_sample_rate: 44.1k\n", "tts_sample_rate"),
    ("llm_timeout: soon\n", "llm_timeout"),
    ("llm_timeout: 0\n", "llm_timeout"),
    ("tts_timeout: -5\n", "tts_timeout"),
    ("speech_detail: detaild\n", "speech_detail"),
    ("provider: 1\n", "provider"),
    ("model: 1.5\n", "model"),
    ("sections: 5\n", "sections"),
    ("sections: [1, 2]\n", "sections"),
    ("noise: ['^wip(']\n", "noise"),
])
def test_a_bad_value_raises_and_names_the_key(monkeypatch, tmp_path, body, key):
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, body)

    assert key in str(exc.value)


def test_the_message_names_the_file_it_came_from(monkeypatch, tmp_path):
    """The user config is global and rarely opened - an error that doesn't name
    it sends people hunting."""
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, "max_commits: many\n")

    assert config.USER_CONFIG_NAME in str(exc.value)


def test_the_message_names_the_project_file_when_that_is_the_source(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("max_commits: many\n", encoding="utf-8")

    with pytest.raises(config.ConfigValueInvalid) as exc:
        load(None)

    assert "changelog.yml" in str(exc.value)


def test_the_message_shows_the_offending_value(monkeypatch, tmp_path):
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, "max_commits: many\n")

    assert "many" in str(exc.value)


def test_a_wizard_owned_key_offers_setup(monkeypatch, tmp_path):
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, "provider: 1\n")

    assert "--setup" in str(exc.value)


def test_a_project_only_key_does_not_offer_setup(monkeypatch, tmp_path):
    """The wizard owns 10 of 24 keys; suggesting it for the others is a lie."""
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, "max_commits: many\n")

    assert "--setup" not in str(exc.value)


def test_a_value_error_is_a_kind_of_config_file_invalid():
    """So existing `except ConfigFileInvalid` handlers keep catching it."""
    assert issubclass(config.ConfigValueInvalid, config.ConfigFileInvalid)


def test_a_value_error_is_not_an_unreadable_error():
    """They need opposite advice: one says "unlock the file", the other
    "edit this line"."""
    assert not issubclass(config.ConfigValueInvalid, config.ConfigFileUnreadable)


# ---------------------------------------------------------------------------
# Values that must be accepted, or quietly repaired
# ---------------------------------------------------------------------------

def test_a_bare_string_becomes_a_one_element_list(monkeypatch, tmp_path):
    """`noise: ^wip` used to become four one-character regexes that matched
    nearly every subject, so every commit was filtered out as noise - total
    silent data loss. A YAML scalar can only mean one item."""
    cfg = _load_with_user_config(monkeypatch, tmp_path, "noise: ^wip\n")

    assert cfg.noise == ["^wip"]


def test_a_bare_section_string_becomes_a_one_element_list(monkeypatch, tmp_path):
    cfg = _load_with_user_config(monkeypatch, tmp_path, "sections: Features\n")

    assert cfg.sections == ["Features"]


@pytest.mark.parametrize("written, expected", [
    ("speak: false\n", False),
    ('speak: "false"\n', False),
    ('speak: "no"\n', False),
    ('speak: "FALSE"\n', False),
    ("speak: true\n", True),
    ('speak: "yes"\n', True),
])
def test_bool_spellings(monkeypatch, tmp_path, written, expected):
    """bool("false") is True, so a quoted value used to mean the opposite."""
    cfg = _load_with_user_config(monkeypatch, tmp_path, written)

    assert cfg.speak is expected


def test_a_nonsense_bool_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(config.ConfigValueInvalid):
        _load_with_user_config(monkeypatch, tmp_path, 'speak: "sometimes"\n')


def test_an_empty_api_key_env_stays_legal(monkeypatch, tmp_path):
    """"" is not a missing value - it means this endpoint needs no key."""
    cfg = _load_with_user_config(monkeypatch, tmp_path, 'api_key_env: ""\n')

    assert cfg.api_key_env == ""


def test_paths_are_expanded(monkeypatch, tmp_path):
    """`voice_samples: ~/voice/` used to load nothing at all, silently, and
    `voice_md: ~/notes.md` created a literal ~ directory in the repo."""
    cfg = _load_with_user_config(
        monkeypatch, tmp_path, "voice_samples: ~/voice/\nvoice_md: ~/notes.md\n"
    )

    home = os.path.expanduser("~")
    assert "~" not in cfg.voice_samples
    assert "~" not in cfg.voice_md
    assert cfg.voice_samples.startswith(home)
    assert cfg.voice_md.startswith(home)
    assert cfg.voice_samples.rstrip("/" + os.sep).endswith("voice")


def test_a_malformed_noise_regex_names_the_file(monkeypatch, tmp_path):
    """Otherwise re.error escapes from filters.drop_noise mid-run."""
    with pytest.raises(config.ConfigValueInvalid) as exc:
        _load_with_user_config(monkeypatch, tmp_path, "noise: ['^wip(']\n")

    assert config.USER_CONFIG_NAME in str(exc.value)


def test_the_builtin_defaults_pass_their_own_validation():
    """If DEFAULTS ever fails the table, every run breaks - so pin it."""
    cfg = config.defaults_config()

    assert cfg.max_commits >= 1
    assert cfg.sections == config.DEFAULTS["sections"]


def test_valueless_keys_still_fall_through_to_defaults(monkeypatch, tmp_path):
    """A bare `max_commits:` is unset, not invalid - it must not now raise."""
    cfg = _load_with_user_config(monkeypatch, tmp_path, "max_commits:\nsections:\n")

    assert cfg.max_commits == config.DEFAULTS["max_commits"]
    assert cfg.sections == config.DEFAULTS["sections"]


# ---------------------------------------------------------------------------
# --setup as a real repair: drop what cannot be used
# ---------------------------------------------------------------------------

def test_saving_drops_a_value_it_cannot_use(monkeypatch, tmp_path, capsys):
    """Without this, --setup preserved the bad line and the next run died
    identically - so it could only ever refuse, never repair."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("max_commits: many\nmodel: old/model\n")

    config.save_user_config({"model": "new/model"})

    stored = config.read_user_config()
    assert "max_commits" not in stored          # dropped, so the default applies
    assert stored["model"] == "new/model"
    assert "max_commits" in capsys.readouterr().err


def test_saving_keeps_hand_added_values_that_are_valid(monkeypatch, tmp_path):
    """Only the unusable keys go; everything else the user wrote survives."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("max_commits: 7\nmax_commits_typo: nonsense\n")

    config.save_user_config({"model": "new/model"})

    stored = config.read_user_config()
    assert stored["max_commits"] == 7
    assert stored["max_commits_typo"] == "nonsense"   # unknown keys are not ours to judge


def test_a_config_repaired_by_saving_then_loads(monkeypatch, tmp_path):
    """The end-to-end point of the repair."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("max_commits: many\n")

    config.save_user_config({"model": "m"})

    assert load(None).max_commits == config.DEFAULTS["max_commits"]


def test_the_drop_note_does_not_blame_the_built_in_defaults(monkeypatch, tmp_path, capsys):
    """The value came from the user's file; saying "built-in defaults" sends
    them looking in the wrong place - and repeating "edit that line, then run
    voicelog again" is advice for a problem just fixed."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("max_commits: many\n")

    config.save_user_config({"model": "m"})

    err = capsys.readouterr().err
    assert "max_commits" in err
    assert "built-in defaults" not in err
    assert "run voicelog again" not in err


# ---------------------------------------------------------------------------
# A base URL that cannot be requested must be caught at load
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "http://a" + chr(10) + "b@x/v1",     # a stray newline from a paste
    "http://" + chr(128) + "bad/v1",     # a control character
    "api.example.com/v1",                # no scheme: httpx reads it as a path
])
def test_an_unusable_base_url_is_rejected_at_load(monkeypatch, tmp_path, value):
    """httpx raises InvalidURL for the first two, which is NOT an HTTPError - so
    it used to escape the request loop as a traceback instead of the documented
    fall-back to the commit list."""
    monkeypatch.chdir(tmp_path)
    # yaml.safe_dump, not repr: inside YAML single quotes a backslash is
    # literal, so a repr would write the two characters \ and n rather than the
    # newline this test is about.
    _write_user_config(yaml.safe_dump({"base_url": value}))

    with pytest.raises(config.ConfigValueInvalid) as exc:
        load(None)

    assert "base_url" in str(exc.value)


def test_a_normal_base_url_is_accepted(monkeypatch, tmp_path):
    cfg = _load_with_user_config(
        monkeypatch, tmp_path, "base_url: https://api.example.com/v1" + chr(10)
    )

    assert cfg.base_url == "https://api.example.com/v1"


def test_an_empty_tts_base_url_stays_legal(monkeypatch, tmp_path):
    """Blank means "use the provider default"."""
    cfg = _load_with_user_config(monkeypatch, tmp_path, 'tts_base_url: ""' + chr(10))

    assert cfg.tts_base_url == ""


# ---------------------------------------------------------------------------
# Saving must not rewrite what the user wrote
# ---------------------------------------------------------------------------

def test_saving_leaves_a_home_relative_path_as_written(monkeypatch, tmp_path):
    """Expanding ~ is right for USING a path and wrong for STORING one: it bakes
    a machine-specific absolute path into a config the user wrote portably."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("voice_samples: ~/notes/voice" + chr(10))

    config.save_user_config({"model": "m"})

    assert config.read_user_config()["voice_samples"] == "~/notes/voice"


def test_loading_still_expands_that_path(monkeypatch, tmp_path):
    """Stored as written, expanded when used."""
    monkeypatch.chdir(tmp_path)
    _write_user_config("voice_samples: ~/notes/voice" + chr(10))

    assert "~" not in load(None).voice_samples
