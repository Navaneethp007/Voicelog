"""Provider presets and provider-side discovery.

Two rules keep this module honest:

- **No interactive IO and no Config.** It runs mid-wizard, before a Config
  exists, so everything it needs arrives as plain arguments. Prompting lives in
  ``voicelog.wizard``; request shaping for generation lives in ``voicelog.llm``.
- **No per-provider code.** ``PROVIDERS`` is flat data. The first provider that
  needs a special case is the signal to drop that provider, not to add a branch.

Presets deliberately carry no model id. Shipping one is what broke voicelog
when NVIDIA retired ``mistralai/mistral-medium-3.5-128b`` - the catalog URL
points at the live list instead.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from voicelog.redact import detail as safe_detail

# Discovery must feel instant. llm_timeout (120s) in a wizard reads as a hang.
_DISCOVERY_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)

CUSTOM_PROVIDER_KEY = "custom"
NO_TTS_KEY = "none"
MANUAL_ENTRY = "__manual__"  # sentinel option value: "let me type it"


class ProviderError(Exception):
    """A provider request failed: transport, auth, or an unusable body."""


@dataclass(frozen=True)
class Provider:
    """An OpenAI-compatible LLM endpoint preset."""

    key: str
    label: str
    base_url: str  # "" for the custom entry - the wizard asks
    api_key_env: str  # "" means the endpoint needs no key at all
    signup_url: str = ""
    catalog_url: str = ""  # where a human finds valid model ids


@dataclass(frozen=True)
class TTSProvider:
    """A speech backend preset."""

    key: str
    label: str
    api_key_env: str
    voices: tuple[str, ...] = ()  # known ids offered as a picker; may be empty
    voice_hint: str = ""

    # Where the backend lives and what it defaults to. These used to sit in
    # tts.py as `or "gpt-4o-mini-tts"` fallbacks - a shadow default table for
    # exactly the keys config.DEFAULTS deliberately leaves blank, so "the
    # default speech model" had no single home and no way to be checked.
    base_url: str = ""
    model: str = ""
    sample_rate: int = 0          # Hz the adapter returns; 0 = caller decides

    # Riva-only, and still flat data rather than a branch: the NVCF function to
    # call and the language it synthesises.
    function_id: str = ""
    language: str = ""


PROVIDERS: dict[str, Provider] = {
    "nvidia": Provider(
        "nvidia",
        "NVIDIA  (free tier, no credit card)",
        "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY",
        "https://build.nvidia.com",
        "https://build.nvidia.com",
    ),
    "openai": Provider(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        "https://platform.openai.com/api-keys",
        "https://platform.openai.com/docs/models",
    ),
    "groq": Provider(
        "groq",
        "Groq  (fast, free tier)",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "https://console.groq.com/keys",
        "https://console.groq.com/docs/models",
    ),
    "openrouter": Provider(
        "openrouter",
        "OpenRouter  (one key, many providers)",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/keys",
        "https://openrouter.ai/models",
    ),
    "ollama": Provider(
        # 127.0.0.1, not "localhost": that can resolve to ::1 while Ollama
        # binds v4 only, which looks like a hung connection.
        "ollama",
        "Ollama  (local, no key needed)",
        "http://127.0.0.1:11434/v1",
        "",
        "https://ollama.com/download",
        "https://ollama.com/library",
    ),
    CUSTOM_PROVIDER_KEY: Provider(
        CUSTOM_PROVIDER_KEY,
        "Other OpenAI-compatible endpoint...",
        "",
        "",
    ),
}

TTS_PROVIDERS: dict[str, TTSProvider] = {
    "riva": TTSProvider(
        "riva",
        "NVIDIA Riva  (default; needs one extra package)",
        "NVIDIA_API_KEY",
        (
            "Magpie-Multilingual.EN-US.Sofia",
            "Magpie-Multilingual.EN-US.Mia",
            "Magpie-Multilingual.EN-US.Ray",
        ),
        "a Magpie voice name",
        base_url="grpc.nvcf.nvidia.com:443",   # gRPC, not HTTP
        sample_rate=44100,
        function_id="877104f7-e885-42b9-8de8-f6e4c6303969",
        language="en-US",
    ),
    "openai": TTSProvider(
        "openai",
        "OpenAI TTS",
        "OPENAI_API_KEY",
        ("alloy", "echo", "fable", "onyx", "nova", "shimmer"),
        "a voice name",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini-tts",
        sample_rate=24000,
    ),
    "elevenlabs": TTSProvider(
        "elevenlabs",
        "ElevenLabs",
        "ELEVENLABS_API_KEY",
        (),
        "your ElevenLabs voice id",
        base_url="https://api.elevenlabs.io/v1",
        model="eleven_multilingual_v2",
        sample_rate=24000,
    ),
    NO_TTS_KEY: TTSProvider(
        NO_TTS_KEY,
        "No voice - text only",
        "",
    ),
}


def get(key: str) -> Provider | None:
    """Look up an LLM preset by key, case-insensitively."""
    return PROVIDERS.get((key or "").strip().lower())


def get_tts(key: str) -> TTSProvider | None:
    """Look up a speech preset by key, case-insensitively."""
    return TTS_PROVIDERS.get((key or "").strip().lower())


def normalize_base_url(url: str) -> str:
    """Trim whitespace and trailing slashes from a base URL.

    Every caller that builds a path onto ``base_url`` must go through this, or a
    pasted ``https://host/v1/`` yields ``https://host/v1//models`` - a 404 on
    many gateways, and a confusing one because the URL looks right.
    """
    return (url or "").strip().rstrip("/")


def _parse_models(payload: object) -> list[str]:
    """Extract model ids from a /models body. Returns [] on anything odd.

    Tolerant on purpose: this sees OpenAI's ``{"data":[{"id":...}]}``, Ollama's
    native ``{"models":[{"name":...}]}``, bare lists, and HTML from proxies.
    Never raises - the caller turns an empty result into manual entry.
    """
    rows: object
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("models"))
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None

    if not isinstance(rows, list):
        return []

    ids: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            name = row
        elif isinstance(row, dict):
            name = row.get("id") or row.get("name") or ""
        else:
            continue
        if isinstance(name, str) and name.strip():
            ids.add(name.strip())
    return sorted(ids)


def headers(api_key: str | None) -> dict[str, str]:
    built = {"Accept": "application/json"}
    if api_key:
        built["Authorization"] = f"Bearer {api_key}"
    return built


# A model that has to cold-start can take well over 30s to answer. Setup is not
# the place to wait for that, so verification gives up early and says so rather
# than holding the wizard hostage.
_VERIFY_TIMEOUT = httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=3.0)

# The provider rejected the credential rather than the request. Shared with
# `llm` so the verifier and the generator cannot disagree about it.
AUTH_REJECTED = (401, 403)

# Statuses that mean "the model id itself is wrong", as opposed to "the service
# is having a bad minute" or "the request was malformed". Shared with `llm` so
# the two cannot drift: 400 is excluded on purpose, because it means "bad
# request", which includes "bad parameter" - reasoning models reject our probe's
# own parameters with 400 while being perfectly callable.
MODEL_REJECTED = (404, 410)

_MAX_DETAIL_CHARS = 200


# How to satisfy a provider that rejects one of our parameters. Renaming rather
# than dropping where an equivalent exists, so the request keeps its meaning.
_PARAM_FIXES = {
    "max_tokens": "max_completion_tokens",  # reasoning models renamed it
    "temperature": None,                    # o-series allows only the default
}

# Bounded so a provider that answers 400 to everything cannot loop.
MAX_PARAM_ADAPTATIONS = 3


def _rejected_param(response, payload: dict) -> str | None:
    """The parameter a 400 is complaining about, if it is one we can fix.

    Reads the provider's own structured error first (``error.param``), falling
    back to looking for our *own* parameter names in the message - never
    guessing at a name we did not send, which is what keeps this from turning
    into free-text error sniffing.
    """
    try:
        body = response.json()
    except (ValueError, AttributeError):
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None

    named = error.get("param")
    if isinstance(named, str) and named in _PARAM_FIXES and named in payload:
        return named

    message = error.get("message")
    if not isinstance(message, str):
        return None
    lowered = message.lower()
    if "unsupported" not in lowered and "not supported" not in lowered:
        return None
    for param in _PARAM_FIXES:
        if param in payload and param in lowered:
            return param
    return None


def adapt_payload(payload: dict, response) -> dict | None:
    """A payload retrying the request without the parameter the provider refused.

    Returns None when nothing can be adapted, which is most 400s.

    This is how reasoning models (o-series, gpt-5) become usable at all: they
    reject ``max_tokens`` in favour of ``max_completion_tokens``, and allow only
    the default ``temperature``. Negotiating with the provider rather than
    shipping a list of model ids is deliberate - a hardcoded list is exactly the
    rot that removing the default model was meant to end.
    """
    param = _rejected_param(response, payload)
    if param is None:
        return None
    adapted = dict(payload)
    replacement = _PARAM_FIXES[param]
    value = adapted.pop(param)
    if replacement:
        adapted[replacement] = value
    return adapted


def _error_detail(response, api_key: str | None) -> str:
    """A short, redacted reason from a provider's JSON error body, or "".

    Reads only ``error.message``, never raw body text: a corporate proxy answers
    200 or 400 with an HTML login page, and pasting that into a terminal is
    noise at best. Redacts before truncating, so a key cannot be cut in half.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        return ""
    return safe_detail(message.strip(), api_key, limit=_MAX_DETAIL_CHARS)


def verify_model(
    base_url: str, api_key: str | None, model: str, timeout=None
) -> str | None:
    """Check that ``model`` can actually be called. Returns None if it can.

    Being listed by ``/v1/models`` does not mean being callable: providers gate
    some models per account, so a catalogue entry can still answer 404 on
    ``/chat/completions``. One 1-token request settles it during setup instead
    of on the user's first real run.

    Returns:
        None when the model answered, otherwise a short human-readable reason.
        Never raises and never retries - a caller mid-wizard needs a verdict,
        not an exception, and a rejected id will not accept itself on a retry.
    """
    url = f"{normalize_base_url(base_url)}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        # max_tokens stays: it is what stops a cold reasoning model burning
        # hundreds of tokens and blowing the read timeout. temperature does not:
        # o-series models reject any non-default value with a 400.
        "max_tokens": 1,
    }
    # The same parameter negotiation `llm.complete` does, so the check cannot
    # condemn a model that generation is perfectly able to drive.
    for _ in range(MAX_PARAM_ADAPTATIONS + 1):
        try:
            response = httpx.post(
                url,
                headers=headers(api_key),
                json=payload,
                timeout=timeout or _VERIFY_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - verification never interrupts setup
            return f"could not verify ({safe_detail(str(exc), api_key)})"

        if response.status_code != 400:
            break
        adapted = adapt_payload(payload, response)
        if adapted is None:
            break
        payload = adapted

    # Any success counts. We asked for one token and do not read the reply, so a
    # reasoning model spending it on thinking, or an odd body shape, is fine.
    if response.is_success:
        return None
    if response.status_code in AUTH_REJECTED:
        return (
            f"the API key was rejected (HTTP {response.status_code}) - "
            "check the key you just entered"
        )
    if response.status_code == 400:
        detail = _error_detail(response, api_key)
        return (
            "the provider rejected the request (HTTP 400)"
            + (f": {detail}" if detail else "")
            + f" - that is about the request, not about '{model}'"
        )
    if response.status_code in MODEL_REJECTED:
        return (
            f"'{model}' is listed but not available to call on this account "
            f"(HTTP {response.status_code})"
        )
    return f"could not verify (HTTP {response.status_code})"


def fetch_models(base_url: str, api_key: str | None, timeout=None) -> list[str]:
    """List the model ids an OpenAI-compatible endpoint advertises.

    ``api_key=None`` (or "") sends no Authorization header, for keyless
    endpoints like a local Ollama.

    Raises:
        ProviderError: transport failure, non-2xx, an unreadable body, or an
            empty catalog. The message never includes the response body - a
            corporate proxy answers 200 with an HTML login page, and dumping
            that into a terminal is noise at best.
    """
    url = f"{normalize_base_url(base_url)}/models"
    try:
        response = httpx.get(
            url,
            headers=headers(api_key),
            timeout=timeout or _DISCOVERY_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - discovery is never fatal
        # Deliberately broad, matching verify_model: a failure here must always
        # fall back to typing a model id, never end setup. httpx.InvalidURL is
        # not an HTTPError, so a narrow catch let a pasted URL kill --setup.
        # Redacted, as verify_model already does for the identical case: httpx
        # echoes the request URL in several of its messages, so a key that
        # arrived inside a pasted base_url would otherwise reach the terminal.
        raise ProviderError(
            f"could not reach {url} ({safe_detail(str(exc), api_key)})"
        ) from exc

    if not response.is_success:
        raise ProviderError(f"{url} returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} did not return JSON") from exc

    ids = _parse_models(payload)
    if not ids:
        raise ProviderError(f"{url} listed no models")
    return ids
