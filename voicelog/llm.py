"""LLM provider integration — NVIDIA NIM / OpenAI-compatible endpoint."""
from __future__ import annotations

import os

import httpx


class MissingApiKey(Exception):
    """Raised when NVIDIA_API_KEY is not set in the environment."""


class LLMError(Exception):
    """Raised when an LLM request fails after all retries."""


def complete(messages: list[dict], config) -> str:
    """Send a chat-completion request and return the assistant content string.

    Args:
        messages: OpenAI-format message list.
        config:   voicelog.config.Config with .base_url and .model fields.

    Returns:
        The content string from choices[0].message.content.

    Raises:
        MissingApiKey: If NVIDIA_API_KEY is not set.
        LLMError:      If both request attempts fail.
    """
    env_name = getattr(config, "api_key_env", "NVIDIA_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise MissingApiKey(
            f"Set the {env_name} environment variable with your "
            f"{getattr(config, 'provider', 'LLM')} API key."
        )

    url = f"{config.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    timeout = getattr(config, "llm_timeout", 120.0)

    last_exc: Exception | None = None
    for attempt in range(2):  # try once, retry once on failure
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if not response.is_success:
                last_exc = Exception(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
                continue
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            # httpx.HTTPError: network/transport failures.
            # ValueError (JSONDecodeError) / KeyError / IndexError / TypeError:
            # a 2xx response whose body isn't the expected OpenAI shape (e.g. a
            # proxy error envelope returned as 200). Treat like any failure —
            # retry once, then LLMError so cli falls back to the commit list.
            last_exc = exc

    raise LLMError(str(last_exc))
