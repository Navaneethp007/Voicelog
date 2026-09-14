"""Tests for voicelog.wizard - the only interactive module.

Prompts are driven by scripting builtins.input, so the real prompt helpers run.
Nothing here touches the network: providers.fetch_models is patched, and
conftest's guard would fail the test if it were not.
"""
from __future__ import annotations

import io
import os
from unittest import mock

import pytest

from voicelog import providers, wizard
from voicelog.providers import ProviderError
from voicelog.wizard import SetupAborted


def _answers(monkeypatch, *replies):
    """Script the prompts. Extra prompts beyond the script raise, so a test
    that provokes an unexpected question fails instead of hanging."""
    queue = list(replies)

    def _input(prompt=""):
        if not queue:
            raise AssertionError(f"unscripted prompt: {prompt!r}")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", _input)
    return queue


OPTIONS = [("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]

# Every shell metacharacter that matters, in one value.
NASTY_KEY = 'sk-$(whoami)-`id`-\\-"-\'-x'

# Captured at import, before the autouse stub below replaces the attribute,
# so the persistence tests can put the real implementation back.
_REAL_OFFER_TO_PERSIST = wizard._offer_to_persist


@pytest.fixture(autouse=True)
def _riva_present(monkeypatch):
    """Pretend the Riva client is installed unless a test says otherwise.

    Without this the riva path depends on whether the machine running the suite
    happens to have nvidia-riva-client, which is exactly the kind of hidden
    environment coupling that makes a suite pass here and fail in CI.
    """
    monkeypatch.setattr(wizard, "_riva_available", lambda: True)


@pytest.fixture(autouse=True)
def _no_key_persistence(monkeypatch):
    """Don't offer to persist the key unless a test is about that.

    ensure_key runs on every voicelog invocation, so the offer appears in many
    flows; tests not concerned with it would otherwise all have to script an
    extra answer. The tests that ARE about it patch this back.
    """
    monkeypatch.setattr(wizard, "_offer_to_persist", lambda env_name, key: None)


@pytest.fixture(autouse=True)
def _skip_model_verification(monkeypatch):
    """Most tests are not about the live model check, so let it pass by default.

    The tests that ARE about it patch providers.verify_model themselves. Without
    this, every choose_model test would trip conftest's network guard.
    """
    monkeypatch.setattr(providers, "verify_model", lambda *a, **k: None)


def _prompting_possible(monkeypatch):
    """Make prompting allowed: both streams a terminal, no CI/hook markers.

    ensure_key shares the wizard's gate, so a test that wants a prompt has to
    satisfy all of it - which is the point.
    """
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard.sys.stdout, "isatty", lambda: True)
    for name in wizard._NON_INTERACTIVE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TERM", raising=False)




# ---------------------------------------------------------------------------
# ask_text
# ---------------------------------------------------------------------------

def test_ask_text_returns_the_typed_value(monkeypatch):
    _answers(monkeypatch, "typed")
    assert wizard.ask_text("Name") == "typed"


def test_ask_text_empty_input_takes_the_default(monkeypatch):
    _answers(monkeypatch, "")
    assert wizard.ask_text("Name", default="fallback") == "fallback"


def test_ask_text_reprompts_when_empty_and_no_default(monkeypatch):
    _answers(monkeypatch, "", "   ", "finally")
    assert wizard.ask_text("Name") == "finally"


def test_ask_text_allows_empty_when_told_to(monkeypatch):
    _answers(monkeypatch, "")
    assert wizard.ask_text("Name", allow_empty=True) == ""


def test_ask_text_shows_the_default_in_the_prompt(monkeypatch, capsys):
    seen = {}

    def _input(prompt=""):
        seen["prompt"] = prompt
        return ""

    monkeypatch.setattr("builtins.input", _input)
    wizard.ask_text("Model", default="a/b")

    assert "a/b" in seen["prompt"]


def test_ask_text_aborts_on_eof(monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=EOFError))
    with pytest.raises(SetupAborted):
        wizard.ask_text("Name")


def test_ask_text_aborts_on_interrupt(monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(SetupAborted):
        wizard.ask_text("Name")


# ---------------------------------------------------------------------------
# ask_yes_no
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply, expected", [("y", True), ("Y", True), ("yes", True),
                                            ("n", False), ("N", False), ("no", False)])
def test_ask_yes_no_reads_the_answer(monkeypatch, reply, expected):
    _answers(monkeypatch, reply)
    assert wizard.ask_yes_no("Save?") is expected


def test_ask_yes_no_empty_takes_the_default(monkeypatch):
    _answers(monkeypatch, "")
    assert wizard.ask_yes_no("Save?", default=True) is True


def test_ask_yes_no_empty_takes_a_negative_default(monkeypatch):
    _answers(monkeypatch, "")
    assert wizard.ask_yes_no("Save?", default=False) is False


def test_ask_yes_no_reprompts_on_nonsense(monkeypatch):
    _answers(monkeypatch, "maybe", "y")
    assert wizard.ask_yes_no("Save?") is True


# ---------------------------------------------------------------------------
# ask_choice
# ---------------------------------------------------------------------------

def test_ask_choice_picks_by_number(monkeypatch):
    _answers(monkeypatch, "2")
    assert wizard.ask_choice("Fruit", OPTIONS) == "b"


def test_ask_choice_empty_takes_the_default(monkeypatch):
    _answers(monkeypatch, "")
    assert wizard.ask_choice("Fruit", OPTIONS, default="c") == "c"


def test_ask_choice_reprompts_when_empty_and_no_default(monkeypatch):
    _answers(monkeypatch, "", "1")
    assert wizard.ask_choice("Fruit", OPTIONS) == "a"


def test_ask_choice_reprompts_on_out_of_range_number(monkeypatch):
    _answers(monkeypatch, "9", "0", "-1", "1")
    assert wizard.ask_choice("Fruit", OPTIONS) == "a"


def test_ask_choice_filters_by_substring_then_picks(monkeypatch):
    """Typed text narrows the list; the number then indexes the narrowed list."""
    _answers(monkeypatch, "err", "1")
    assert wizard.ask_choice("Fruit", OPTIONS) == "c"


def test_ask_choice_filter_matches_the_value_not_only_the_label(monkeypatch):
    _answers(monkeypatch, "b", "1")
    assert wizard.ask_choice("Fruit", OPTIONS) == "b"


def test_ask_choice_filter_is_case_insensitive(monkeypatch):
    _answers(monkeypatch, "CHERRY", "1")
    assert wizard.ask_choice("Fruit", OPTIONS) == "c"


def test_ask_choice_says_so_when_a_filter_matches_nothing(monkeypatch, capsys):
    _answers(monkeypatch, "zzz", "1")

    assert wizard.ask_choice("Fruit", OPTIONS) == "a"
    assert "no match" in capsys.readouterr().out.lower()


def test_ask_choice_caps_how_many_options_it_prints(monkeypatch, capsys):
    """A 300-model catalog must not scroll the prompt off the screen."""
    many = [(f"m{i}", f"model-{i}") for i in range(300)]
    _answers(monkeypatch, "1")

    wizard.ask_choice("Model", many, page=20)

    out = capsys.readouterr().out
    assert "model-19" in out
    assert "model-20" not in out
    assert "300" in out  # tells the user how many there are in total


def test_ask_choice_aborts_on_eof(monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=EOFError))
    with pytest.raises(SetupAborted):
        wizard.ask_choice("Fruit", OPTIONS)


def test_ask_choice_output_is_ascii_only(monkeypatch, capsys):
    """cli reconfigures the console to UTF-8 but swallows the failure, so a
    non-ASCII glyph can raise UnicodeEncodeError mid-setup on cp1252."""
    _answers(monkeypatch, "1")
    wizard.ask_choice("Fruit", OPTIONS)

    capsys.readouterr().out.encode("ascii")  # raises if we emitted anything else


# ---------------------------------------------------------------------------
# ensure_key
# ---------------------------------------------------------------------------

def test_ensure_key_sets_env_when_interactive(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-pasted")

    assert wizard.ensure_key("MY_KEY") is True
    assert os.environ.get("MY_KEY") == "nvapi-pasted"


def test_ensure_key_noop_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False)
    called = {"asked": False}
    monkeypatch.setattr(wizard.getpass, "getpass",
                        lambda prompt="": called.__setitem__("asked", True) or "x")

    assert wizard.ensure_key("MY_KEY") is False
    assert called["asked"] is False
    assert os.environ.get("MY_KEY") is None


def test_ensure_key_skips_when_already_set(monkeypatch):
    monkeypatch.setenv("MY_KEY", "already-here")
    called = {"asked": False}
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass",
                        lambda prompt="": called.__setitem__("asked", True) or "x")

    assert wizard.ensure_key("MY_KEY") is True
    assert called["asked"] is False
    assert os.environ.get("MY_KEY") == "already-here"


def test_ensure_key_empty_input_leaves_unset(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "   ")

    assert wizard.ensure_key("MY_KEY") is False
    assert os.environ.get("MY_KEY") is None


def test_ensure_key_is_a_noop_for_keyless_endpoints(monkeypatch):
    """Ollama needs no key at all; api_key_env == "" says so."""
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass",
                        lambda prompt="": pytest.fail("should not ask for a key"))

    assert wizard.ensure_key("") is True


def test_ensure_key_strips_quotes_and_newlines_from_a_paste(monkeypatch):
    """A Windows clipboard paste arrives wrapped in quotes or with a CR."""
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": '  "nvapi-x"\r ')

    wizard.ensure_key("MY_KEY")

    assert os.environ.get("MY_KEY") == "nvapi-x"


def test_ensure_key_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-secret")

    wizard.ensure_key("MY_KEY")

    captured = capsys.readouterr()
    assert "nvapi-secret" not in captured.out + captured.err


def test_ensure_key_can_confirm_reuse_of_an_existing_value(monkeypatch, capsys):
    """During setup, silently reusing a key set for another tool is a trap."""
    monkeypatch.setenv("MY_KEY", "nvapi-existing-value")
    _prompting_possible(monkeypatch)
    _answers(monkeypatch, "y")

    assert wizard.ensure_key("MY_KEY", confirm_existing=True) is True

    out = capsys.readouterr().out
    assert "nvapi-existing-value" not in out  # masked, not echoed


def test_ensure_key_replaces_an_existing_value_when_declined(monkeypatch):
    monkeypatch.setenv("MY_KEY", "old-value")
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "new-value")
    _answers(monkeypatch, "n")

    wizard.ensure_key("MY_KEY", confirm_existing=True)

    assert os.environ.get("MY_KEY") == "new-value"


# ---------------------------------------------------------------------------
# choose_model
# ---------------------------------------------------------------------------

def test_choose_model_picks_from_the_live_catalog(monkeypatch):
    _answers(monkeypatch, "1")
    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]):
        assert wizard.choose_model("https://x/v1", "k") == "a/one"


def test_choose_model_always_offers_manual_entry_even_on_success(monkeypatch):
    """Manual entry is a permanent option, not just a failure path."""
    _answers(monkeypatch, "3", "typed/model")
    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"


def test_choose_model_falls_back_to_typing_when_discovery_fails(monkeypatch, capsys):
    _answers(monkeypatch, "typed/model")
    with mock.patch.object(providers, "fetch_models",
                           side_effect=ProviderError("could not reach host")):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"

    assert "could not reach host" in capsys.readouterr().out


def test_choose_model_names_the_catalog_page_when_typing(monkeypatch, capsys):
    _answers(monkeypatch, "typed/model")
    with mock.patch.object(providers, "fetch_models", side_effect=ProviderError("down")):
        wizard.choose_model("https://x/v1", "k", catalog_url="https://models.example")

    assert "https://models.example" in capsys.readouterr().out


def test_choose_model_defaults_to_the_current_model(monkeypatch):
    _answers(monkeypatch, "")
    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]):
        assert wizard.choose_model("https://x/v1", "k", current="b/two") == "b/two"


# ---------------------------------------------------------------------------
# run_setup
# ---------------------------------------------------------------------------

def _current():
    """The effective config the wizard is handed, as cli passes it."""
    from voicelog.config import DEFAULTS
    return dict(DEFAULTS)


def _current_with(**overrides):
    """An effective config that is not the defaults.

    Needed because _current() is plain DEFAULTS, where tts_provider is already
    "riva" - so every existing test that picks another voice takes the
    *switched* branch, and nothing covers the not-switched half.
    """
    values = _current()
    values.update(overrides)
    return values


def test_run_setup_writes_the_chosen_llm_and_voice(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "gsk-test")
    _answers(
        monkeypatch,
        "3",       # provider: groq
        "",        # base_url: accept the preset
        "",        # api_key_env: accept GROQ_API_KEY
        "1",       # model: first from the catalog
        "2",       # voice provider: openai TTS
        "",        # tts_api_key_env: accept OPENAI_API_KEY
        "5",       # voice: nova
        "y",       # save
    )

    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["provider"] == "groq"
    assert values["base_url"] == "https://api.groq.com/openai/v1"
    assert values["model"] == "a/one"
    assert values["api_key_env"] == "GROQ_API_KEY"
    assert values["tts_provider"] == "openai"
    assert values["tts_voice"] == "nova"
    assert values["tts_api_key_env"] == "OPENAI_API_KEY"


def test_run_setup_never_returns_the_api_key(monkeypatch, tmp_path):
    """The whole no-secrets-on-disk guarantee reduces to this assertion."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-supersecret")
    _answers(monkeypatch, "1", "", "", "1", "4", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    import yaml
    assert "nvapi-supersecret" not in yaml.safe_dump(values)
    assert os.environ.get("NVIDIA_API_KEY") == "nvapi-supersecret"  # session only


def test_run_setup_marks_a_keyless_provider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    _answers(monkeypatch, "5", "", "1", "4", "y")  # ollama, preset url, model, no voice, save

    with mock.patch.object(providers, "fetch_models", return_value=["llama3.1"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["provider"] == "ollama"
    assert values["api_key_env"] == ""


def test_run_setup_turns_speech_off_when_no_voice_is_chosen(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "4", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["speak"] is False
    assert not [key for key in values if key.startswith("tts_")]


def test_run_setup_asks_for_a_custom_base_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(
        monkeypatch,
        "6",                          # provider: other
        "https://proxy.test/v1/",     # base_url, with a trailing slash
        "MY_PROXY_KEY",               # api_key_env
        "1",                          # model
        "4",                          # no voice
        "y",
    )

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["base_url"] == "https://proxy.test/v1"  # normalized
    assert values["api_key_env"] == "MY_PROXY_KEY"


def test_run_setup_aborts_when_the_save_is_declined(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "4", "n")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        with pytest.raises(SetupAborted):
            wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))


def test_run_setup_warns_when_the_project_config_pins_a_model(monkeypatch, tmp_path, capsys):
    """Otherwise you pick a live model, still get the 410, and blame the wizard."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("model: pinned/old\n", encoding="utf-8")
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "4", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    out = capsys.readouterr().out
    assert "changelog.yml" in out
    assert "pinned/old" in out


def test_run_setup_reuses_one_key_for_text_and_voice(monkeypatch, tmp_path):
    """NVIDIA text + Riva voice share NVIDIA_API_KEY; don't ask twice."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    asked = {"count": 0}

    def _getpass(prompt=""):
        asked["count"] += 1
        return "nvapi-once"

    monkeypatch.setattr(wizard.getpass, "getpass", _getpass)
    _answers(monkeypatch, "1", "", "", "1", "1", "1", "y")  # nvidia, riva, first voice

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["tts_api_key_env"] == "NVIDIA_API_KEY"
    assert asked["count"] == 1


def test_run_setup_output_is_ascii_only(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "4", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    capsys.readouterr().out.encode("ascii")


def test_run_setup_does_not_pin_the_riva_function_id(monkeypatch, tmp_path):
    """tts_function_id is an NVCF deployment id - the same rot as a model id.

    Leaving it out of the user's file means a future default improvement still
    reaches them.
    """
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "1", "1", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert "tts_function_id" not in values
    assert "tts_sample_rate" not in values
    assert "tts_language" not in values


def test_run_setup_leaves_project_scoped_keys_alone(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    _answers(monkeypatch, "1", "", "", "1", "4", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    for key in ("sections", "noise", "voice_samples", "max_commits", "voice_md"):
        assert key not in values


# ---------------------------------------------------------------------------
# choose_model: the live check
# ---------------------------------------------------------------------------

def test_choose_model_confirms_a_model_that_verifies(monkeypatch, capsys):
    _answers(monkeypatch, "1")
    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]), \
         mock.patch.object(providers, "verify_model", return_value=None) as verify:
        assert wizard.choose_model("https://x/v1", "k") == "a/one"

    verify.assert_called_once()
    assert "a/one" in verify.call_args[0]
    assert "ok" in capsys.readouterr().out.lower()


def test_choose_model_offers_another_pick_when_verification_fails(monkeypatch, capsys):
    """The real case: listed by /v1/models, 404 on /chat/completions."""
    _answers(monkeypatch, "1", "1", "2")  # pick a/one, "choose a different one", pick b/two
    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]), \
         mock.patch.object(providers, "verify_model",
                           side_effect=["'a/one' is listed but not available", None]):
        assert wizard.choose_model("https://x/v1", "k") == "b/two"

    assert "not available" in capsys.readouterr().out


def test_choose_model_keeps_a_failing_model_only_when_explicitly_told_to(monkeypatch, capsys):
    """Verification is advisory - a provider having a bad minute must not trap
    anyone - but keeping a model that just failed has to be a decision."""
    _answers(monkeypatch, "1", "2")  # pick a/one, then "use it anyway"
    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]), \
         mock.patch.object(providers, "verify_model", return_value="not available"):
        assert wizard.choose_model("https://x/v1", "k") == "a/one"

    assert "anyway" in capsys.readouterr().out.lower()


def test_choose_model_never_silently_accepts_a_failing_model(monkeypatch):
    """It used to offer exactly one re-pick and then keep whatever it had, so
    setup saved a config it had just proved was broken."""
    # Every answer is "choose a different one", so the only way this can end is
    # the script running dry - which proves it never gives up on its own.
    _answers(monkeypatch, "1", "1", "2", "1", "1")
    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]), \
         mock.patch.object(providers, "verify_model", return_value="not available"):
        with pytest.raises(AssertionError, match="unscripted prompt"):
            wizard.choose_model("https://x/v1", "k")


def test_choose_model_verifies_a_manually_typed_id_too(monkeypatch):
    _answers(monkeypatch, "2", "typed/model", "2")  # manual entry, then keep anyway
    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]), \
         mock.patch.object(providers, "verify_model", return_value="not available") as verify:
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"

    assert "typed/model" in verify.call_args[0]


def test_choose_model_verification_failure_never_blocks_setup(monkeypatch):
    _answers(monkeypatch, "1", "2")  # pick a/one, then keep anyway
    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]), \
         mock.patch.object(providers, "verify_model", return_value="could not verify (timeout)"):
        assert wizard.choose_model("https://x/v1", "k") == "a/one"


# ---------------------------------------------------------------------------
# The voice menu follows the text provider, without hiding the alternatives
# ---------------------------------------------------------------------------

def _tts_labels(llm_provider):
    return [value for value, _ in wizard._tts_options(llm_provider)]


def test_voice_menu_puts_the_matching_service_first():
    assert _tts_labels("openai")[0] == "openai"
    assert _tts_labels("nvidia")[0] == "riva"


def test_voice_menu_keeps_riva_first_for_providers_with_no_speech_service():
    """Groq, OpenRouter and Ollama have no TTS of their own."""
    for provider in ("groq", "openrouter", "ollama", "custom"):
        assert _tts_labels(provider)[0] == "riva", provider


def test_voice_menu_always_offers_every_service():
    """Mixing is the point: Groq text with ElevenLabs speech must stay possible."""
    for provider in ("nvidia", "openai", "groq", "openrouter", "ollama", "custom"):
        assert set(_tts_labels(provider)) == set(providers.TTS_PROVIDERS), provider


def test_voice_menu_keeps_no_voice_last():
    for provider in ("nvidia", "openai", "groq"):
        assert _tts_labels(provider)[-1] == providers.NO_TTS_KEY, provider


def test_run_setup_defaults_the_voice_to_the_text_provider(monkeypatch, tmp_path):
    """Picking OpenAI for text makes OpenAI TTS one Enter away."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "sk-test")
    _answers(
        monkeypatch,
        "2",   # provider: OpenAI
        "",    # base URL
        "",    # key env var
        "1",   # model
        "",    # voice service -> Enter must land on OpenAI TTS, not Riva
        "1",   # voice
        "y",
    )

    with mock.patch.object(providers, "fetch_models", return_value=["gpt-x"]), \
         mock.patch.object(providers, "verify_model", return_value=None):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["tts_provider"] == "openai"
    assert values["tts_api_key_env"] == "OPENAI_API_KEY"


def test_voice_defaults_to_the_first_voice_of_a_newly_chosen_service(monkeypatch, tmp_path):
    """Switching voice service must not break the all-Enter path.

    The configured voice belongs to the old service, so it cannot be the
    default; without a fallback the user presses Enter and gets told off.
    """
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "sk-test")
    _answers(
        monkeypatch,
        "2",   # provider: OpenAI (current config still says riva for voice)
        "",    # base URL
        "",    # key env var
        "1",   # model
        "",    # voice service -> OpenAI TTS
        "",    # voice -> Enter must land on OpenAI's first voice
        "y",
    )

    with mock.patch.object(providers, "fetch_models", return_value=["gpt-x"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["tts_voice"] == providers.TTS_PROVIDERS["openai"].voices[0]


def test_choose_model_survives_discovery_failure_plus_verification_failure(monkeypatch):
    """The two failure paths crossing must not lose the user's answers.

    Discovery failing means there is no catalog to re-pick from, so the retry
    has to fall back to typing - not to a name that was never bound.
    """
    _answers(monkeypatch, "typed/model", "2")  # type an id, then keep anyway
    with mock.patch.object(providers, "fetch_models",
                           side_effect=ProviderError("host unreachable")), \
         mock.patch.object(providers, "verify_model", return_value="not available"):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"


def test_choose_model_offers_retyping_when_there_is_no_catalog(monkeypatch):
    _answers(monkeypatch, "first/try", "1", "second/try")
    with mock.patch.object(providers, "fetch_models",
                           side_effect=ProviderError("host unreachable")), \
         mock.patch.object(providers, "verify_model",
                           side_effect=["not available", None]):
        assert wizard.choose_model("https://x/v1", "k") == "second/try"


# ---------------------------------------------------------------------------
# Offering to install the Riva extra
# ---------------------------------------------------------------------------

def _riva_answers(monkeypatch, *extra):
    """Walk to the voice step with NVIDIA + Riva chosen, then the given answers."""
    return _answers(monkeypatch, "1", "", "", "1", "1", *extra)


def test_riva_requirement_comes_from_package_metadata():
    """One source of truth: the [tts] extra in pyproject, read back at runtime."""
    requirement = wizard._riva_requirement()

    assert "riva" in requirement
    assert "extra" not in requirement  # the marker is stripped
    assert ";" not in requirement


def test_no_install_prompt_when_riva_is_already_present(monkeypatch, tmp_path):
    """Nobody who already has it should be asked."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    run = mock.Mock()
    monkeypatch.setattr(wizard.subprocess, "run", run)
    _riva_answers(monkeypatch, "1", "y")  # voice, save - no install question

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    run.assert_not_called()
    assert values["tts_provider"] == "riva"


def test_offers_to_install_riva_and_runs_pip_on_yes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    monkeypatch.setattr(wizard, "_riva_available", lambda: False)
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr(wizard.subprocess, "run", run)
    _riva_answers(monkeypatch, "y", "1", "y")  # install? yes, voice, save

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    run.assert_called_once()
    argv = run.call_args[0][0]
    assert argv[:4] == [wizard.sys.executable, "-m", "pip", "install"]
    assert "riva" in argv[4]
    assert values["tts_provider"] == "riva"


def test_declining_the_install_keeps_riva_and_names_the_command(monkeypatch, tmp_path, capsys):
    """Speech just stays unavailable until they install it; text still works."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    monkeypatch.setattr(wizard, "_riva_available", lambda: False)
    run = mock.Mock()
    monkeypatch.setattr(wizard.subprocess, "run", run)
    _riva_answers(monkeypatch, "n", "1", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    run.assert_not_called()
    assert values["tts_provider"] == "riva"
    assert "pip install" in capsys.readouterr().out


def test_a_failed_install_warns_but_completes_setup(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    monkeypatch.setattr(wizard, "_riva_available", lambda: False)
    monkeypatch.setattr(wizard.subprocess, "run",
                        mock.Mock(side_effect=OSError("pip not found")))
    _riva_answers(monkeypatch, "y", "1", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    assert values["tts_provider"] == "riva"
    out = capsys.readouterr().out
    assert "pip install" in out  # the manual command to fall back on


def test_install_is_not_offered_for_http_backends(monkeypatch, tmp_path):
    """Only Riva needs a package; OpenAI TTS and ElevenLabs are plain HTTP."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    monkeypatch.setattr(wizard, "_riva_available", lambda: False)
    run = mock.Mock()
    monkeypatch.setattr(wizard.subprocess, "run", run)
    _answers(monkeypatch, "2", "", "", "1", "", "1", "y")  # OpenAI text + OpenAI TTS

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        wizard.run_setup(_current(), dest=str(tmp_path / "config.yml"))

    run.assert_not_called()


# ---------------------------------------------------------------------------
# When the terminal cannot hide input, say so
# ---------------------------------------------------------------------------

def test_says_so_when_the_key_could_not_be_hidden(monkeypatch, capsys):
    """getpass warns and falls back to a visible input() when it cannot
    suppress echo. Silently letting a key appear on screen is the bad case."""
    import warnings

    def _echoing_getpass(prompt=""):
        warnings.warn("Can not control echo on the terminal.",
                      wizard.getpass.GetPassWarning, stacklevel=2)
        return "nvapi-visible"

    monkeypatch.setattr(wizard.getpass, "getpass", _echoing_getpass)

    assert wizard.ask_secret("Paste your key: ") == "nvapi-visible"

    out = capsys.readouterr().out.lower()
    assert "visible" in out


def test_no_note_when_the_key_was_hidden(monkeypatch, capsys):
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-hidden")

    wizard.ask_secret("Paste your key: ")

    assert "visible" not in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Prompt wording
# ---------------------------------------------------------------------------

def test_no_internal_sentinel_leaks_into_a_prompt(monkeypatch):
    """ask_choice echoes the default VALUE, so option values are user-visible."""
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        return "2"      # "use it anyway", so the loop ends

    monkeypatch.setattr("builtins.input", _input)
    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]),          mock.patch.object(providers, "verify_model", return_value="not available"):
        wizard.choose_model("https://x/v1", "k")

    joined = " ".join(prompts)
    assert "__" not in joined, joined


def test_choice_default_says_how_to_accept_it(monkeypatch):
    """`Choose 1-3 [c]` shows a value you cannot type; the input is a number."""
    seen = {}

    def _input(prompt=""):
        seen["prompt"] = prompt
        return "1"

    monkeypatch.setattr("builtins.input", _input)
    wizard.ask_choice("Fruit", OPTIONS, default="c")

    assert "Enter = c" in seen["prompt"]


# ---------------------------------------------------------------------------
# Offering to keep the key for future terminals
# ---------------------------------------------------------------------------

def test_offers_to_persist_after_a_successful_paste(monkeypatch, capsys):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-pasted")
    monkeypatch.setattr(wizard, "_offer_to_persist", _REAL_OFFER_TO_PERSIST)
    persisted = {}
    monkeypatch.setattr(wizard, "_persist_key",
                        lambda env, key: persisted.update(env=env, key=key) or True)
    _answers(monkeypatch, "y")

    wizard.ensure_key("MY_KEY")

    assert persisted == {"env": "MY_KEY", "key": "nvapi-pasted"}


def test_declining_persistence_keeps_the_session_only_behaviour(monkeypatch, capsys):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-pasted")
    monkeypatch.setattr(wizard, "_offer_to_persist", _REAL_OFFER_TO_PERSIST)
    called = {"ran": False}
    monkeypatch.setattr(wizard, "_persist_key",
                        lambda env, key: called.__setitem__("ran", True) or True)
    _answers(monkeypatch, "n")

    wizard.ensure_key("MY_KEY")

    assert called["ran"] is False
    assert os.environ.get("MY_KEY") == "nvapi-pasted"  # still set for this run
    out = capsys.readouterr().out
    assert "setx" in out or "export" in out            # the manual command, as before


def test_persistence_offer_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.delenv("MY_KEY", raising=False)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-supersecret")
    monkeypatch.setattr(wizard, "_offer_to_persist", _REAL_OFFER_TO_PERSIST)
    monkeypatch.setattr(wizard, "_persist_key", lambda env, key: True)
    _answers(monkeypatch, "y")

    wizard.ensure_key("MY_KEY")

    captured = capsys.readouterr()
    assert "nvapi-supersecret" not in captured.out + captured.err


def test_persistence_warns_when_it_would_replace_a_different_value(monkeypatch, capsys):
    """setx overwrites a user-wide variable other tools may rely on."""
    monkeypatch.setenv("MY_KEY", "belongs-to-another-tool")
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "nvapi-new")
    monkeypatch.setattr(wizard, "_offer_to_persist", _REAL_OFFER_TO_PERSIST)
    monkeypatch.setattr(wizard, "_persist_key", lambda env, key: True)
    _answers(monkeypatch, "n", "y")  # don't reuse the existing value; then persist

    wizard.ensure_key("MY_KEY", confirm_existing=True)

    out = capsys.readouterr().out
    assert "replace" in out.lower() or "overwrit" in out.lower()
    assert "belongs-to-another-tool" not in out  # masked, never echoed


@pytest.mark.skipif(os.name != "nt", reason="setx is Windows-only")
def test_windows_persistence_runs_setx(monkeypatch):
    calls = []
    monkeypatch.setattr(wizard.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or mock.Mock(returncode=0))

    assert wizard._persist_key("MY_KEY", "nvapi-x") is True

    assert calls and calls[0][0] == "setx"
    assert calls[0][1] == "MY_KEY"


@pytest.mark.skipif(os.name != "nt", reason="setx is Windows-only")
def test_windows_persistence_refuses_an_over_long_key(monkeypatch, capsys):
    """setx truncates above 1024 characters, which would store a broken key."""
    calls = []
    monkeypatch.setattr(wizard.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or mock.Mock(returncode=0))

    assert wizard._persist_key("MY_KEY", "x" * 1100) is False

    assert calls == []
    assert "1024" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="shell profiles are POSIX")
def test_posix_persistence_appends_to_a_profile(monkeypatch, tmp_path):
    profile = tmp_path / ".bashrc"
    profile.write_text("# existing" + chr(10), encoding="utf-8")
    monkeypatch.setattr(wizard, "_shell_profile", lambda: str(profile))

    assert wizard._persist_key("MY_KEY", "nvapi-x") is True

    content = profile.read_text(encoding="utf-8")
    assert "export MY_KEY=" in content
    assert "voicelog" in content       # a marker, so it can be found later
    assert content.startswith("# existing")  # appended, never rewritten


@pytest.mark.skipif(os.name == "nt", reason="shell profiles are POSIX")
def test_posix_persistence_reports_failure_when_no_profile_is_found(monkeypatch):
    monkeypatch.setattr(wizard, "_shell_profile", lambda: None)

    assert wizard._persist_key("MY_KEY", "nvapi-x") is False


def test_export_line_quotes_shell_metacharacters():
    """The line used to be double-quoted, so a key containing a backtick or $(
    was EXECUTED at every shell startup - from a value pasted into a prompt.

    Not POSIX-skipped: the quoting is pure string logic, and the platform that
    cannot run the file-append tests is the one most likely to be developing on.
    """
    import shlex

    nasty = NASTY_KEY
    line = wizard._export_line("MY_KEY", nasty)

    # Parse it the way a shell would; the value must come back byte-identical.
    parsed = shlex.split(line)
    assert parsed[0] == "export"
    assert parsed[1] == f"MY_KEY={nasty}"


def test_export_line_carries_the_voicelog_marker():
    assert "voicelog" in wizard._export_line("MY_KEY", "k")


@pytest.mark.skipif(os.name == "nt", reason="shell profiles are POSIX")
def test_posix_persistence_reports_an_existing_export(monkeypatch, tmp_path, capsys):
    """The last export wins, so appending is correct - but say so rather than
    letting the user wonder which line is in effect."""
    profile = tmp_path / ".bashrc"
    profile.write_text("export MY_KEY=old-value" + chr(10), encoding="utf-8")
    monkeypatch.setattr(wizard, "_shell_profile", lambda: str(profile))

    assert wizard._persist_key("MY_KEY", "new-value") is True

    out = capsys.readouterr().out.lower()
    assert "already" in out
    assert "new-value" not in out          # never echo the key
    assert "old-value" not in out


@pytest.mark.skipif(os.name == "nt", reason="shell profiles are POSIX")
def test_posix_persistence_skips_an_identical_existing_line(monkeypatch, tmp_path):
    """Re-pasting the same key must not grow the profile a line at a time."""
    profile = tmp_path / ".bashrc"
    monkeypatch.setattr(wizard, "_shell_profile", lambda: str(profile))
    profile.write_text("", encoding="utf-8")

    assert wizard._persist_key("MY_KEY", "same-key") is True
    once = profile.read_text(encoding="utf-8")
    assert wizard._persist_key("MY_KEY", "same-key") is True

    assert profile.read_text(encoding="utf-8") == once


# ---------------------------------------------------------------------------
# Switching the speech backend must not carry the old one's settings
# ---------------------------------------------------------------------------

def test_switching_the_voice_backend_clears_the_old_ones_model(monkeypatch, tmp_path):
    """save_user_config merges, so a stored OpenAI tts_model would survive into
    an ElevenLabs config and go out as its model_id."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    current = _current_with(
        tts_provider="openai",
        tts_voice="nova",
        tts_model="tts-1-hd",
        tts_base_url="https://speech-proxy.test/v1",
    )
    # nvidia, base, key env, model, voice service = ElevenLabs, its key env,
    # voice id (ElevenLabs has no preset voices, so this is a text prompt), save
    _answers(monkeypatch, "1", "", "", "1", "3", "", "my-voice-id", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(current, dest=str(tmp_path / "config.yml"))

    assert values["tts_provider"] == "elevenlabs"
    assert values["tts_model"] == ""
    assert values["tts_base_url"] == ""


def test_restating_the_same_voice_backend_keeps_its_settings(monkeypatch, tmp_path):
    """The half nothing covered: a restatement must not wipe a configured
    tts_model or a speech proxy URL."""
    monkeypatch.chdir(tmp_path)
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "k")
    current = _current_with(
        tts_provider="openai",
        tts_voice="nova",
        tts_model="tts-1-hd",
        tts_base_url="https://speech-proxy.test/v1",
    )
    # voice service "2" = OpenAI TTS, i.e. the one already configured
    _answers(monkeypatch, "1", "", "", "1", "2", "", "1", "y")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        values = wizard.run_setup(current, dest=str(tmp_path / "config.yml"))

    assert values["tts_provider"] == "openai"
    assert "tts_model" not in values      # untouched, so the stored value survives
    assert "tts_base_url" not in values


# ---------------------------------------------------------------------------
# can_prompt must survive a process with no usable streams
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stream", [None, "closed"])
@pytest.mark.parametrize("name", ["stdin", "stdout"])
def test_can_prompt_is_false_when_a_stream_is_unusable(monkeypatch, name, stream):
    """Under pythonw.exe sys.stdin is None, and a detached stream can be closed.
    can_prompt is reached on EVERY invocation via _interactive and ensure_key,
    so raising here took down the whole command, not just setup."""
    if stream == "closed":
        stream = io.StringIO()
        stream.close()
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.sys, name, stream)

    assert wizard.can_prompt() is False


def test_can_prompt_is_still_true_with_two_real_terminals(monkeypatch):
    """The guard must not swallow the working case."""
    _prompting_possible(monkeypatch)

    assert wizard.can_prompt() is True


# ---------------------------------------------------------------------------
# A declined key must not be used
# ---------------------------------------------------------------------------

def test_a_declined_key_is_not_used_when_no_replacement_is_given(monkeypatch):
    """Answering no to "Use that value?" and then pressing Enter used to return
    True with the refused key still in os.environ - so the key the user had just
    rejected was silently used for discovery, verification and generation."""
    monkeypatch.setenv("MY_KEY", "sk-the-one-i-refused")
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "")
    _answers(monkeypatch, "n")

    assert wizard.ensure_key("MY_KEY", confirm_existing=True) is False
    assert "MY_KEY" not in os.environ


def test_declining_says_what_enter_now_means(monkeypatch):
    """Once the existing value is declined, Enter cannot mean "skip and keep
    it" - there is nothing left to keep."""
    monkeypatch.setenv("MY_KEY", "old-value")
    _prompting_possible(monkeypatch)
    prompts = []
    monkeypatch.setattr(wizard.getpass, "getpass",
                        lambda prompt="": prompts.append(prompt) or "")
    _answers(monkeypatch, "n")

    wizard.ensure_key("MY_KEY", confirm_existing=True)

    assert "skip" not in prompts[0].lower()
    assert "without" in prompts[0].lower()


def test_a_confirmed_key_is_left_alone(monkeypatch):
    """The decline path must not disturb the ordinary yes."""
    monkeypatch.setenv("MY_KEY", "keep-me")
    _prompting_possible(monkeypatch)
    _answers(monkeypatch, "y")

    assert wizard.ensure_key("MY_KEY", confirm_existing=True) is True
    assert os.environ["MY_KEY"] == "keep-me"


# ---------------------------------------------------------------------------
# A long catalog must not push the escape hatch off the page
# ---------------------------------------------------------------------------

# What integrate.api.nvidia.com actually returns from /v1/models, against a
# 20-row page: "Type a model id manually" was option 83.
_NVIDIA_CATALOG_SIZE = 82


def test_manual_entry_survives_a_catalog_longer_than_one_page(monkeypatch):
    """The manual row was appended to the options, so paging cut it off - the
    one option a user needs when the catalog lists a model their account cannot
    call, which is exactly the situation that sends them looking for it."""
    models = [f"m/{i}" for i in range(_NVIDIA_CATALOG_SIZE)]
    _answers(monkeypatch, "21", "typed/model")   # 21 = the row after the page

    with mock.patch.object(providers, "fetch_models", return_value=models):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"


def test_the_manual_row_is_printed_and_numbered(monkeypatch, capsys):
    models = [f"m/{i}" for i in range(_NVIDIA_CATALOG_SIZE)]
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        return ["21", "typed/model"][len(prompts) - 1]

    monkeypatch.setattr("builtins.input", _input)
    with mock.patch.object(providers, "fetch_models", return_value=models):
        wizard.choose_model("https://x/v1", "k")

    assert "Type a model id manually" in capsys.readouterr().out
    # The prompt goes to input(), not stdout: 20 paged rows plus the pinned one.
    assert "Choose 1-21" in prompts[0]


def test_the_total_counts_models_not_the_manual_row(monkeypatch, capsys):
    """It used to say "21 total" for 20 models, counting its own escape hatch."""
    models = [f"m/{i}" for i in range(_NVIDIA_CATALOG_SIZE)]
    _answers(monkeypatch, "1")

    with mock.patch.object(providers, "fetch_models", return_value=models):
        wizard.choose_model("https://x/v1", "k")

    assert f"{_NVIDIA_CATALOG_SIZE} total" in capsys.readouterr().out


def test_narrowing_keeps_the_manual_row_reachable(monkeypatch):
    """Filtering replaces the visible list, which must not drop the pinned row."""
    models = [f"m/{i}" for i in range(_NVIDIA_CATALOG_SIZE)] + ["zeta/one"]
    _answers(monkeypatch, "zeta", "2", "typed/model")  # narrow, pick manual, type

    with mock.patch.object(providers, "fetch_models", return_value=models):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"


def test_a_short_catalog_still_numbers_manual_entry_last(monkeypatch):
    """The fix must not renumber the ordinary case."""
    _answers(monkeypatch, "3", "typed/model")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one", "b/two"]):
        assert wizard.choose_model("https://x/v1", "k") == "typed/model"


# ---------------------------------------------------------------------------
# A model id belongs to the provider it came from
# ---------------------------------------------------------------------------

def _llm_answers(monkeypatch, provider_choice):
    _prompting_possible(monkeypatch)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "sk-pasted")
    _answers(monkeypatch, provider_choice, "", "")  # provider, base_url, env var


def test_the_model_default_does_not_cross_a_provider_switch(monkeypatch):
    """Switching NVIDIA -> OpenAI offered 'deepseek-ai/deepseek-coder-6.7b-instruct'
    as the model-id default, so pressing Enter stored a guaranteed 404. The
    base_url default next to it has always been guarded this way."""
    seen = {}
    monkeypatch.setattr(wizard, "choose_model",
                        lambda base_url, api_key, **kw: seen.update(kw) or "gpt-4o-mini")
    _llm_answers(monkeypatch, "2")   # openai

    wizard._choose_llm(_current_with(
        provider="nvidia", model="deepseek-ai/deepseek-coder-6.7b-instruct"))

    assert seen["current"] == ""


def test_the_model_default_survives_restating_the_same_provider(monkeypatch):
    """Re-running setup without changing provider must still offer the model
    you already use - otherwise every run retypes it."""
    seen = {}
    monkeypatch.setattr(wizard, "choose_model",
                        lambda base_url, api_key, **kw: seen.update(kw) or "kept/model")
    _llm_answers(monkeypatch, "1")   # nvidia, same as current

    wizard._choose_llm(_current_with(provider="nvidia", model="keep/this"))

    assert seen["current"] == "keep/this"


def _scripted(monkeypatch, *replies, limit=4):
    """input() that fails loudly instead of looping forever on a bad default."""
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        if len(prompts) > limit:
            raise AssertionError(f"prompt repeated {len(prompts)}x: {prompt!r}")
        return replies[len(prompts) - 1] if len(prompts) <= len(replies) else ""

    monkeypatch.setattr("builtins.input", _input)
    return prompts


def test_a_default_beyond_the_first_page_is_still_offered(monkeypatch):
    """Re-running --setup against NVIDIA's 82 models: the configured model is
    usually not in the first 20 rows, and keeping it with Enter is the common
    path. Checking membership against the printed page instead of the whole
    list hid the hint and rejected Enter with "Please choose one."."""
    options = [(f"m/{i}", f"m/{i}") for i in range(_NVIDIA_CATALOG_SIZE)]
    prompts = _scripted(monkeypatch, "")

    chosen = wizard.ask_choice(
        "Which model?", options, default="m/50",
        pinned=[(wizard.MANUAL_ENTRY, "Type a model id manually")],
    )

    assert chosen == "m/50"
    assert "Enter = m/50" in prompts[0]


def test_a_pinned_value_can_still_be_the_default(monkeypatch):
    options = [(f"m/{i}", f"m/{i}") for i in range(_NVIDIA_CATALOG_SIZE)]
    prompts = _scripted(monkeypatch, "")

    chosen = wizard.ask_choice(
        "Which model?", options, default=wizard.MANUAL_ENTRY,
        pinned=[(wizard.MANUAL_ENTRY, "Type a model id manually")],
    )

    assert chosen == wizard.MANUAL_ENTRY
    assert f"Enter = {wizard.MANUAL_ENTRY}" in prompts[0]


def test_a_default_that_is_not_on_offer_is_not_advertised(monkeypatch):
    """The guard that stops a stale config value being accepted silently."""
    options = [(f"m/{i}", f"m/{i}") for i in range(_NVIDIA_CATALOG_SIZE)]
    prompts = _scripted(monkeypatch, "", "1")

    chosen = wizard.ask_choice("Which model?", options, default="gone/model")

    assert chosen == "m/0"                      # Enter refused, then picked 1
    assert "Enter =" not in prompts[0]


def test_a_non_decimal_digit_does_not_crash_the_picker(monkeypatch):
    """"2".isdigit() is True for a superscript two, but int() rejects it - so a
    stray character raised ValueError mid-setup and lost every answer already
    given. isdecimal() is the predicate that matches int()."""
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        assert len(prompts) < 4, "picker did not recover"
        return chr(178) if len(prompts) == 1 else "1"      # superscript two

    monkeypatch.setattr("builtins.input", _input)

    assert wizard.ask_choice("Pick?", OPTIONS) == "a"


def test_a_non_decimal_digit_is_treated_as_a_filter(monkeypatch):
    """It is text, so it narrows the list - and matches nothing here."""
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        assert len(prompts) < 4
        return chr(178) if len(prompts) == 1 else "1"

    monkeypatch.setattr("builtins.input", _input)
    wizard.ask_choice("Pick?", OPTIONS)


# ---------------------------------------------------------------------------
# The wizard must not accept what the writer will drop
# ---------------------------------------------------------------------------

def _first_prompt_only(monkeypatch, *replies):
    """Script the opening prompts, then abort - for tests about a default."""
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        if len(prompts) > len(replies):
            raise EOFError          # becomes SetupAborted
        return replies[len(prompts) - 1]

    monkeypatch.setattr("builtins.input", _input)
    return prompts


def test_a_base_url_without_a_scheme_is_re_prompted(monkeypatch, capsys):
    """Typing localhost:11434/v1 - the standard Ollama paste - was accepted,
    echoed in the Summary and reported "Saved", then silently dropped by
    save_user_config's repair loop. The effective base_url fell back to
    NVIDIA's, so a local model id and whatever key was in the environment
    went there."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(
        monkeypatch,
        "5",                          # provider: ollama (needs no key)
        "localhost:11434/v1",         # no scheme - must be refused
        "http://localhost:11434/v1",  # corrected
    )

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        with pytest.raises(SetupAborted):
            wizard._choose_llm(_current())

    # The URL prompt appeared twice: refused once, accepted once.
    url_prompts = [p for p in prompts if "base URL" in p]
    assert len(url_prompts) == 2
    assert "scheme" in capsys.readouterr().out.lower()


def test_a_valid_base_url_is_accepted_first_time(monkeypatch):
    """The guard must not re-prompt on a good value."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(monkeypatch, "5", "http://localhost:11434/v1")

    with mock.patch.object(providers, "fetch_models", return_value=["a/one"]):
        with pytest.raises(SetupAborted):
            wizard._choose_llm(_current())

    assert len([p for p in prompts if "base URL" in p]) == 1


def test_whatever_the_wizard_accepts_the_writer_keeps(monkeypatch, tmp_path):
    """The property that was broken: two validators with two opinions."""
    from voicelog import config as config_module
    monkeypatch.chdir(tmp_path)
    accepted = config_module.check_value("base_url", "http://localhost:11434/v1")

    config_module.save_user_config({"base_url": accepted, "model": "m"})

    assert config_module.read_user_config()["base_url"] == accepted


# ---------------------------------------------------------------------------
# Re-running setup must not switch a voice backend behind your back
# ---------------------------------------------------------------------------

def test_a_configured_voice_backend_is_the_default(monkeypatch):
    """_LLM_TO_TTS[llm_provider] was consulted first and "riva" is truthy, so an
    ElevenLabs user re-running --setup to change only the model was switched
    back to Riva by pressing Enter - blanking voice, model and base_url."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(monkeypatch)

    with pytest.raises(SetupAborted):
        wizard._choose_tts(
            _current_with(tts_provider="elevenlabs",
                          tts_api_key_env="ELEVENLABS_API_KEY",
                          tts_voice="my-voice-id"),
            "NVIDIA_API_KEY", "nvidia")

    assert "Enter = elevenlabs" in prompts[0]


def test_a_fresh_machine_still_gets_the_llm_affinity(monkeypatch):
    """With nothing configured, choosing OpenAI for text should suggest OpenAI
    for speech - that is what the affinity table is for, and it must survive."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(monkeypatch)

    with pytest.raises(SetupAborted):
        wizard._choose_tts(_current(), "OPENAI_API_KEY", "openai")

    assert "Enter = openai" in prompts[0]


def test_setx_warns_about_the_exposure_before_running(monkeypatch, capsys):
    """setx passes the key as a command-line argument: readable from any
    process listing for the child's lifetime, and written permanently to the
    Security event log where 4688 auditing is on. Worth a sentence before we
    do it, since the module docstring promises a key is never written down."""
    monkeypatch.setattr(wizard.os, "name", "nt")
    monkeypatch.setattr(wizard.subprocess, "run",
                        lambda *a, **k: mock.Mock(returncode=0))

    assert wizard._persist_key("MY_KEY", "sk-secret") is True

    out = capsys.readouterr().out
    assert "process" in out.lower() or "visible" in out.lower()
    assert "sk-secret" not in out          # still never echoes the key itself


# ---------------------------------------------------------------------------
# SetupAborted records which way out the user took
# ---------------------------------------------------------------------------

def test_eof_at_a_prompt_is_not_an_interrupt(monkeypatch):
    """Callers need to tell them apart: EOF means "skip this question", Ctrl-C
    means "stop the command". One exception type for both meant whoever
    swallowed it swallowed the interrupt too."""
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": (_ for _ in ()).throw(EOFError))

    with pytest.raises(SetupAborted) as exc:
        wizard.ask_text("Anything?")

    assert exc.value.interrupted is False


def test_ctrl_c_at_a_prompt_is_an_interrupt(monkeypatch):
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(SetupAborted) as exc:
        wizard.ask_text("Anything?")

    assert exc.value.interrupted is True


def test_ctrl_c_at_a_secret_prompt_is_an_interrupt(monkeypatch):
    """ask_secret has its own reader, so it needs the same distinction."""
    monkeypatch.setattr(wizard.getpass, "getpass",
                        lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(SetupAborted) as exc:
        wizard.ask_secret("Paste: ")

    assert exc.value.interrupted is True


def test_declining_to_save_is_not_an_interrupt():
    """run_setup raises this itself when the user answers no."""
    assert SetupAborted().interrupted is False


# ---------------------------------------------------------------------------
# Pressing Enter must never dead-end on a prompt with no default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("llm_provider",
                         ["nvidia", "openai", "groq", "openrouter", "ollama", "custom"])
def test_the_voice_prompt_always_offers_a_default(monkeypatch, llm_provider):
    """Blanking a stored tts_provider that equals the built-in default fixed
    the ElevenLabs switch, but left the providers with no speech affinity -
    Groq, OpenRouter, Ollama, custom - with no default at all, contradicting
    the invariant stated ten lines below it."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(monkeypatch)

    with pytest.raises(SetupAborted):
        wizard._choose_tts(_current(), "SOME_KEY", llm_provider)

    assert "Enter = " in prompts[0], prompts[0]


def test_a_configured_backend_still_beats_the_affinity(monkeypatch):
    """The ElevenLabs fix must survive the dead-end fix."""
    _prompting_possible(monkeypatch)
    prompts = _first_prompt_only(monkeypatch)

    with pytest.raises(SetupAborted):
        wizard._choose_tts(_current_with(tts_provider="elevenlabs"),
                           "NVIDIA_API_KEY", "nvidia")

    assert "Enter = elevenlabs" in prompts[0]
