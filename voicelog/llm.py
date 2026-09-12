"""LLM provider integration — any OpenAI-compatible chat-completions endpoint."""
from __future__ import annotations

import os

import httpx

from voicelog import providers
from voicelog.providers import (
    AUTH_REJECTED as _AUTH_REJECTED,
    MAX_PARAM_ADAPTATIONS,
    MODEL_REJECTED as _MODEL_REJECTED,
    normalize_base_url,
)
from voicelog.redact import redact

# Which statuses mean "wrong model" lives in `providers` so the verifier and
# the generator can never disagree about it. Everything else keeps the
# documented retry-then-soft-fallback behaviour.

_MAX_BODY_CHARS = 300


class MissingApiKey(Exception):
    """Raised when the configured API-key environment variable is not set."""


class LLMError(Exception):
    """Raised when an LLM request fails after all retries."""


class MissingModel(LLMError):
    """Raised when no model is configured anywhere.

    A subclass of LLMError so the callers that already catch that keep working —
    notably the spoken-summary path, which runs *after* the changelog has been
    printed and must not turn a misconfiguration into a traceback.
    """


class InvalidApiKey(LLMError):
    """Raised when the provider rejects the credential itself (401/403).

    Distinct from `MissingApiKey`, which means no key was configured at all.
    This one means a key was sent and refused - most often because it expired
    after setup. Without its own type it was retried and then reported as
    "LLM unavailable", so an expired key looked like a provider outage.
    """


class ModelUnavailable(LLMError):
    """Raised when the provider rejects the configured model id (404/410).

    Its own type because it is a configuration problem wearing an HTTP error's
    clothes. Treating it as a generic outage is what let a retired default model
    sit behind a "falling back to commit list" warning unnoticed.
    """


def complete(messages: list[dict], config) -> str:
    """Send a chat-completion request and return the assistant content string.

    Args:
        messages: OpenAI-format message list.
        config:   voicelog.config.Config with .base_url and .model fields.

    Returns:
        The content string from choices[0].message.content.

    Raises:
        MissingModel:      If no model is configured.
        MissingApiKey:     If the configured key env var is set but empty.
        InvalidApiKey:     If the provider rejects the key (401/403).
        ModelUnavailable:  If the provider rejects the model id (404/410).
        LLMError:          If both request attempts fail.
    """
    model = (getattr(config, "model", "") or "").strip()
    if not model:
        raise MissingModel(
            "no model configured — voicelog ships no default model on purpose, "
            "because provider model ids get retired. "
            "Run `voicelog --setup` to pick one from your provider's catalog, "
            "or pass `--model <model-id>` for a single run."
        )

    # A blank api_key_env means the endpoint needs no key at all (a local
    # Ollama, an unauthenticated proxy) — not that we forgot to configure one.
    env_name = getattr(config, "api_key_env", "NVIDIA_API_KEY")
    api_key = os.environ.get(env_name) if env_name else None
    if env_name and not api_key:
        raise MissingApiKey(
            f"Set the {env_name} environment variable with your "
            f"{getattr(config, 'provider', 'LLM')} API key."
        )

    base_url = normalize_base_url(config.base_url)
    url = f"{base_url}/chat/completions"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    timeout = getattr(config, "llm_timeout", 120.0)

    last_exc: Exception | None = None
    attempts_left = 2  # try once, retry once on failure
    adaptations_left = MAX_PARAM_ADAPTATIONS
    while attempts_left:
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if response.status_code == 400 and adaptations_left:
                # The provider may be refusing a parameter rather than the
                # request: reasoning models reject max_tokens and a non-default
                # temperature. Adapting and retrying is what makes them usable
                # at all, and it does not spend the ordinary retry budget - the
                # request was never really attempted on its own terms.
                adapted = providers.adapt_payload(payload, response)
                if adapted is not None:
                    payload = adapted
                    adaptations_left -= 1
                    continue
            if response.status_code in _AUTH_REJECTED:
                # No retry: a refused credential will not be accepted on a
                # second attempt, and the fix is a new key, not a wait.
                raise InvalidApiKey(
                    f"{env_name or 'the API key'} was rejected by "
                    f"{getattr(config, 'provider', 'the provider')} "
                    f"(HTTP {response.status_code}) - the key may have expired, "
                    f"been revoked, or belong to a different provider. "
                    f"Set a new one, or run `voicelog --setup`."
                )
            if response.status_code in _MODEL_REJECTED:
                # Raised immediately: a wrong model id will not fix itself on a
                # second attempt, and the fix is a config change, not a wait.
                raise ModelUnavailable(
                    f"model '{model}' was rejected by "
                    f"{getattr(config, 'provider', 'the provider')} "
                    f"(HTTP {response.status_code}) — it may have been renamed or "
                    f"retired, or {base_url} may be the wrong endpoint. "
                    f"Run `voicelog --setup` to pick a current model, "
                    f"or pass `--model <model-id>`."
                )
            if not response.is_success:
                last_exc = Exception(
                    f"HTTP {response.status_code}: "
                    # Redact first: truncating first would cut a key in half and
                    # leave a prefix that replace() can no longer match.
                    f"{redact(response.text, api_key)[:_MAX_BODY_CHARS]}"
                )
                attempts_left -= 1
                continue
            return response.json()["choices"][0]["message"]["content"]
        except (
            httpx.HTTPError,
            httpx.InvalidURL,  # not an HTTPError, despite the name
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            # httpx.HTTPError: network/transport failures.
            # ValueError (JSONDecodeError) / KeyError / IndexError / TypeError:
            # a 2xx response whose body isn't the expected OpenAI shape (e.g. a
            # proxy error envelope returned as 200). Treat like any failure —
            # retry once, then LLMError so cli falls back to the commit list.
            last_exc = exc
        attempts_left -= 1

    raise LLMError(redact(str(last_exc), api_key))
