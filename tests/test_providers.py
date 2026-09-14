"""Tests for voicelog.providers - registry data + provider HTTP, all mocked."""
from __future__ import annotations

from unittest import mock

import httpx
import pytest

from voicelog import providers
from voicelog.providers import ProviderError


def _resp(*, is_success=True, status_code=200, payload=None, text=""):
    """Response double exposing only what providers.py actually reads."""
    resp = mock.MagicMock()
    resp.is_success = is_success
    resp.status_code = status_code
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# Registry data
# ---------------------------------------------------------------------------

def test_nvidia_is_the_first_llm_provider_offered():
    assert list(providers.PROVIDERS)[0] == "nvidia"


def test_riva_is_first_and_none_is_last_tts_option():
    keys = list(providers.TTS_PROVIDERS)
    assert keys[0] == "riva"
    assert keys[-1] == providers.NO_TTS_KEY


def test_openrouter_preset_is_openai_compatible_v1():
    p = providers.PROVIDERS["openrouter"]
    assert p.base_url == "https://openrouter.ai/api/v1"
    assert p.api_key_env == "OPENROUTER_API_KEY"


def test_ollama_is_keyless_and_avoids_localhost_name_resolution():
    p = providers.PROVIDERS["ollama"]
    assert p.api_key_env == ""       # "" means this endpoint needs no key
    assert "127.0.0.1" in p.base_url  # not "localhost": may resolve to ::1


def test_every_preset_except_custom_ships_a_base_url():
    for key, p in providers.PROVIDERS.items():
        if key == providers.CUSTOM_PROVIDER_KEY:
            assert p.base_url == ""
        else:
            assert p.base_url.startswith("http"), key


def test_no_preset_pins_a_model_id():
    """A shipped model id is exactly the rot this feature exists to remove.

    Presets carry a catalog URL so the wizard can say where to find ids; they
    must never carry an id of their own, which would rot the same way
    mistral-medium-3.5-128b did.
    """
    for key, p in providers.PROVIDERS.items():
        assert not hasattr(p, "model")
        assert not hasattr(p, "model_hint")
        if key != providers.CUSTOM_PROVIDER_KEY:
            assert p.catalog_url.startswith("http"), key


def test_get_is_case_insensitive_and_returns_none_for_unknown():
    assert providers.get("NVIDIA").key == "nvidia"
    assert providers.get("nope") is None
    assert providers.get_tts("RIVA").key == "riva"
    assert providers.get_tts("nope") is None


# ---------------------------------------------------------------------------
# normalize_base_url
# ---------------------------------------------------------------------------

def test_normalize_strips_trailing_slashes_and_whitespace():
    assert providers.normalize_base_url("https://x/v1/") == "https://x/v1"
    assert providers.normalize_base_url("  https://x/v1//  ") == "https://x/v1"
    assert providers.normalize_base_url("") == ""


# ---------------------------------------------------------------------------
# fetch_models
# ---------------------------------------------------------------------------

def test_fetch_models_returns_sorted_unique_ids():
    payload = {"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=payload)):
        assert providers.fetch_models("https://x/v1", "k") == ["a", "b"]


def test_fetch_models_builds_a_clean_url_from_a_trailing_slash_base():
    """A trailing slash must not produce //models - the llm.py bug class."""
    payload = {"data": [{"id": "a"}]}
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=payload)) as get:
        providers.fetch_models("https://x/v1/", "k")

    assert get.call_args[0][0] == "https://x/v1/models"


def test_fetch_models_sends_bearer_key():
    payload = {"data": [{"id": "a"}]}
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=payload)) as get:
        providers.fetch_models("https://x/v1", "secret")

    assert get.call_args[1]["headers"]["Authorization"] == "Bearer secret"


def test_fetch_models_omits_auth_header_when_keyless():
    payload = {"data": [{"id": "a"}]}
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=payload)) as get:
        providers.fetch_models("http://127.0.0.1:11434/v1", None)

    assert "Authorization" not in get.call_args[1]["headers"]


def test_fetch_models_uses_a_short_timeout_not_the_llm_timeout():
    """120s (llm_timeout) in a wizard reads as a hang, so discovery caps low."""
    payload = {"data": [{"id": "a"}]}
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=payload)) as get:
        providers.fetch_models("https://x/v1", "k")

    timeout = get.call_args[1]["timeout"]
    assert timeout.read is not None and timeout.read <= 15.0
    assert timeout.connect is not None and timeout.connect <= 5.0


def test_fetch_models_raises_with_status_on_http_error():
    with mock.patch("voicelog.providers.httpx.get",
                    return_value=_resp(is_success=False, status_code=401, text="bad key")):
        with pytest.raises(ProviderError) as exc:
            providers.fetch_models("https://x/v1", "k")

    assert "401" in str(exc.value)


def test_fetch_models_raises_on_transport_failure():
    with mock.patch("voicelog.providers.httpx.get",
                    side_effect=httpx.ConnectError("refused")):
        with pytest.raises(ProviderError):
            providers.fetch_models("https://x/v1", "k")


def test_fetch_models_raises_on_non_json_body():
    """A captive-portal/proxy login page returns 200 with HTML."""
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload=None)):
        with pytest.raises(ProviderError):
            providers.fetch_models("https://x/v1", "k")


def test_fetch_models_raises_on_empty_catalog():
    with mock.patch("voicelog.providers.httpx.get", return_value=_resp(payload={"data": []})):
        with pytest.raises(ProviderError):
            providers.fetch_models("https://x/v1", "k")


def test_fetch_models_never_echoes_a_response_body_into_the_error():
    """An HTML error page must not be dumped into the user's terminal."""
    html = "<html><body>corporate proxy login</body></html>" * 20
    with mock.patch("voicelog.providers.httpx.get",
                    return_value=_resp(is_success=False, status_code=403, text=html)):
        with pytest.raises(ProviderError) as exc:
            providers.fetch_models("https://x/v1", "k")

    assert "<html>" not in str(exc.value)


# ---------------------------------------------------------------------------
# _parse_models - tolerant by design, never raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"data": [{"id": "a"}]}, ["a"]),
        ({"models": [{"name": "a"}]}, ["a"]),      # Ollama native shape
        (["b", "a"], ["a", "b"]),                  # bare list of strings
        ({"data": [{"id": "a"}, {"nope": 1}]}, ["a"]),   # entry without an id
        ({"data": "not-a-list"}, []),
        ({"unexpected": 1}, []),
        ("<html>login</html>", []),
        (None, []),
        ([], []),
    ],
)
def test_parse_models_is_tolerant(payload, expected):
    assert providers._parse_models(payload) == expected


# ---------------------------------------------------------------------------
# verify_model - "listed" does not mean "callable on your account"
# ---------------------------------------------------------------------------

def test_verify_model_returns_none_when_the_model_answers():
    with mock.patch("voicelog.providers.httpx.post", return_value=_resp(payload={"ok": 1})):
        assert providers.verify_model("https://x/v1", "k", "a/one") is None


def test_verify_model_spends_exactly_one_token():
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(payload={"ok": 1})) as post:
        providers.verify_model("https://x/v1", "k", "a/one")

    body = post.call_args[1]["json"]
    assert body["max_tokens"] == 1
    assert body["model"] == "a/one"
    assert post.call_args[0][0] == "https://x/v1/chat/completions"


def test_verify_model_does_not_retry():
    """Setup must stay quick; a rejected id will not accept itself on retry."""
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=404)) as post:
        providers.verify_model("https://x/v1", "k", "a/one")

    assert post.call_count == 1


def test_verify_model_passes_on_an_unparseable_success_body():
    """We only care that the id was accepted, not what came back."""
    with mock.patch("voicelog.providers.httpx.post", return_value=_resp(payload=None)):
        assert providers.verify_model("https://x/v1", "k", "a/one") is None


def test_verify_model_builds_a_clean_url_from_a_trailing_slash_base():
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(payload={"ok": 1})) as post:
        providers.verify_model("https://x/v1/", "k", "a/one")

    assert post.call_args[0][0] == "https://x/v1/chat/completions"


def test_verify_model_omits_auth_header_when_keyless():
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(payload={"ok": 1})) as post:
        providers.verify_model("http://127.0.0.1:11434/v1", None, "llama3.1")

    assert "Authorization" not in post.call_args[1]["headers"]


def test_verify_model_uses_a_short_timeout():
    """A cold-starting model must not stall setup - NIM cold starts exceed 30s."""
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(payload={"ok": 1})) as post:
        providers.verify_model("https://x/v1", "k", "a/one")

    timeout = post.call_args[1]["timeout"]
    assert timeout.read is not None and timeout.read <= 15.0


@pytest.mark.parametrize("status", [401, 403])
def test_verify_model_blames_the_key_on_an_auth_failure(status):
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=status)):
        reason = providers.verify_model("https://x/v1", "k", "a/one")

    assert reason is not None
    assert "key" in reason.lower()


@pytest.mark.parametrize("status", [404, 410])
def test_verify_model_blames_the_model_when_rejected(status):
    """The real case: /v1/models lists it, /chat/completions 404s on it.

    400 is deliberately NOT here - see the dedicated test below. It means "bad
    request", which includes "bad parameter", and llm.py excludes it for the
    same reason.
    """
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=status)):
        reason = providers.verify_model("https://x/v1", "k", "a/one")

    assert reason is not None
    assert "account" in reason.lower() or "not available" in reason.lower()
    assert "key" not in reason.lower()  # do not misdirect: the key was fine


def test_verify_model_is_inconclusive_on_a_server_error():
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=503)):
        reason = providers.verify_model("https://x/v1", "k", "a/one")

    assert reason is not None
    assert "could not verify" in reason.lower()


def test_verify_model_is_inconclusive_on_a_timeout():
    with mock.patch("voicelog.providers.httpx.post",
                    side_effect=httpx.ReadTimeout("too slow")):
        reason = providers.verify_model("https://x/v1", "k", "a/one")

    assert reason is not None
    assert "could not verify" in reason.lower()


def test_verify_model_never_raises():
    """It reports; it never interrupts setup."""
    with mock.patch("voicelog.providers.httpx.post", side_effect=RuntimeError("boom")):
        assert providers.verify_model("https://x/v1", "k", "a/one") is not None


def test_verify_model_never_echoes_the_key():
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=401,
                                       text="bad token nvapi-secret")):
        reason = providers.verify_model("https://x/v1", "nvapi-secret", "a/one")

    assert "nvapi-secret" not in reason


# ---------------------------------------------------------------------------
# HTTP 400 is about the request, not about the model
# ---------------------------------------------------------------------------

def test_400_does_not_accuse_the_model():
    """Reasoning models reject our own probe parameters with 400. Reporting
    that as "not available on this account" blames a callable model."""
    payload = {"error": {"message": "Unsupported parameter: 'max_tokens'"}}
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=400, payload=payload)):
        reason = providers.verify_model("https://x/v1", "k", "openai/o4-mini")

    assert reason is not None
    assert "400" in reason
    assert "not available" not in reason.lower()
    assert "account" not in reason.lower()


def test_400_surfaces_the_provider_reason():
    """A realistic key on purpose: redaction is a blunt substring replace, so a
    one-character "key" would scrub every k in the provider's message."""
    payload = {"error": {"message": "Unsupported parameter: 'max_tokens'"}}
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=400, payload=payload)):
        reason = providers.verify_model("https://x/v1", "nvapi-realistic-key", "openai/o4-mini")

    assert "max_tokens" in reason


def test_400_says_the_model_itself_is_not_the_problem():
    payload = {"error": {"message": "Unsupported parameter: 'max_tokens'"}}
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=400, payload=payload)):
        reason = providers.verify_model("https://x/v1", "k", "openai/o4-mini")

    assert "openai/o4-mini" in reason


def test_400_detail_is_redacted():
    key = "nvapi-0123456789abcdefghij"
    payload = {"error": {"message": f"bad request, key was {key}"}}
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=400, payload=payload)):
        reason = providers.verify_model("https://x/v1", key, "a/one")

    assert key not in reason


def test_400_with_an_unreadable_body_still_reports_cleanly():
    """A proxy's HTML page must not be pasted into the terminal."""
    html = "<html><body>corporate proxy login</body></html>"
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(is_success=False, status_code=400, text=html)):
        reason = providers.verify_model("https://x/v1", "k", "a/one")

    assert "400" in reason
    assert "<html>" not in reason


def test_probe_sends_no_temperature():
    """o-series models reject any non-default temperature with 400."""
    with mock.patch("voicelog.providers.httpx.post",
                    return_value=_resp(payload={"ok": 1})) as post:
        providers.verify_model("https://x/v1", "k", "a/one")

    assert "temperature" not in post.call_args[1]["json"]


def test_model_rejected_statuses_are_shared_and_exclude_400():
    from voicelog import llm

    assert providers.MODEL_REJECTED == (404, 410)
    assert llm._MODEL_REJECTED is providers.MODEL_REJECTED


# ---------------------------------------------------------------------------
# Preset endpoint shapes
# ---------------------------------------------------------------------------

def test_hosted_presets_point_at_a_versioned_api_path():
    """Probed live during planning: nvidia 200, openai 401, groq 401,
    openrouter 200 - all correct. A wrong URL gives 404 or a DNS failure, so
    pin the shape to stop an edit quietly breaking one. Not a network test."""
    for key, p in providers.PROVIDERS.items():
        if key in (providers.CUSTOM_PROVIDER_KEY, "ollama"):
            continue
        assert p.base_url.startswith("https://"), key
        assert p.base_url.rstrip("/").endswith("/v1"), key
        assert not p.base_url.endswith("/"), key


def test_ollama_preset_is_loopback_with_a_port():
    p = providers.PROVIDERS["ollama"]

    assert p.base_url == "http://127.0.0.1:11434/v1"  # not "localhost": may be ::1


def test_fetch_models_never_lets_an_unexpected_error_escape():
    """Discovery failing must always fall back to manual entry - it used to
    catch only HTTPError, so httpx.InvalidURL (not an HTTPError) killed --setup
    with a traceback. verify_model has always caught broadly; this matches it."""
    with mock.patch("voicelog.providers.httpx.get", side_effect=httpx.InvalidURL("bad")):
        with pytest.raises(ProviderError):
            providers.fetch_models("https://x/v1", "k")


def test_fetch_models_survives_a_unicode_error():
    with mock.patch("voicelog.providers.httpx.get", side_effect=UnicodeError("bad host")):
        with pytest.raises(ProviderError):
            providers.fetch_models("https://x/v1", "k")


def test_fetch_models_never_echoes_the_key_in_its_error():
    """verify_model redacts the identical case. httpx puts the request URL in
    several exception messages, and a key pasted into a base_url - or echoed by
    a proxy - would otherwise land in a terminal and a bug report."""
    key = "nvapi-supersecret-value"
    with mock.patch("voicelog.providers.httpx.get",
                    side_effect=httpx.ConnectError(f"failed for {key}")):
        with pytest.raises(ProviderError) as exc:
            providers.fetch_models("https://x/v1", key)

    assert key not in str(exc.value)


# ---------------------------------------------------------------------------
# One table: the registry is the only place a provider fact is written
# ---------------------------------------------------------------------------

def test_every_speech_backend_has_an_adapter():
    """_ADAPTERS was a second registry that could drift from TTS_PROVIDERS, and
    had: `none` is a first-class TTS_PROVIDERS entry with no adapter, so
    tts_provider: none reached speak() and was called "unknown"."""
    from voicelog import tts
    assert set(tts._ADAPTERS) | {providers.NO_TTS_KEY} == set(providers.TTS_PROVIDERS)


def test_no_adapter_exists_for_a_backend_not_in_the_registry():
    from voicelog import tts
    assert set(tts._ADAPTERS) <= set(providers.TTS_PROVIDERS)


def test_the_defaults_table_does_not_restate_the_presets():
    """DEFAULTS spelled NVIDIA's URL, key env var and Magpie voice as literals,
    so the registry and the defaults could disagree with nothing to catch it."""
    from voicelog.config import DEFAULTS
    nvidia = providers.PROVIDERS["nvidia"]
    riva = providers.TTS_PROVIDERS["riva"]

    assert DEFAULTS["provider"] == nvidia.key
    assert DEFAULTS["base_url"] == nvidia.base_url
    assert DEFAULTS["api_key_env"] == nvidia.api_key_env
    assert DEFAULTS["tts_provider"] == riva.key
    assert DEFAULTS["tts_api_key_env"] == riva.api_key_env
    assert DEFAULTS["tts_voice"] == riva.voices[0]
    assert DEFAULTS["tts_function_id"] == riva.function_id
    assert DEFAULTS["tts_language"] == riva.language
    assert DEFAULTS["tts_sample_rate"] == riva.sample_rate


def test_speech_presets_carry_their_own_endpoint_and_model():
    """These lived in tts.py as `or "gpt-4o-mini-tts"` fallbacks - a shadow
    default table for keys DEFAULTS deliberately leaves blank."""
    assert providers.TTS_PROVIDERS["openai"].base_url == "https://api.openai.com/v1"
    assert providers.TTS_PROVIDERS["openai"].model == "gpt-4o-mini-tts"
    assert providers.TTS_PROVIDERS["openai"].sample_rate == 24000
    assert providers.TTS_PROVIDERS["elevenlabs"].model == "eleven_multilingual_v2"
    assert providers.TTS_PROVIDERS["elevenlabs"].sample_rate == 24000


def test_the_llm_to_tts_affinity_is_derived_not_typed():
    """_LLM_TO_TTS was a third hand-written table; it is exactly "the backends
    that share a key variable with a text provider"."""
    from voicelog import wizard
    for llm_key, tts_key in wizard._LLM_TO_TTS.items():
        assert providers.PROVIDERS[llm_key].api_key_env == (
            providers.TTS_PROVIDERS[tts_key].api_key_env
        )


def test_llm_and_providers_build_the_same_headers():
    """llm.py re-implemented providers._headers byte for byte, while already
    importing four constants from that module to stop exactly this drift."""
    from voicelog import llm
    assert llm._headers is providers.headers
