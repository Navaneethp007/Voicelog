"""Suite-wide safety net.

Three things must be true of every test in this suite, and none of them were
guaranteed before user-level config existed:

1. Config discovery reads a machine-global path (``%APPDATA%\voicelog`` /
   ``~/.config/voicelog``). Without redirection, tests would read — and the
   wizard tests could overwrite — the developer's real config, making results
   depend on whose machine they run on.
2. The setup wizard must never trigger. Under ``pytest -s`` capture is off and
   ``sys.stdin.isatty()`` is True, which would otherwise hang the suite on a
   prompt.
3. No test may touch the network. ``voicelog.providers`` is the first module
   whose happy path is a live HTTP call, and a forgotten patch would otherwise
   fire a real authenticated request at a real provider.
"""
from __future__ import annotations

import httpx
import pytest

_PROVIDER_KEY_VARS = (
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
)


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path, monkeypatch):
    """Point user-config discovery at a throwaway dir, and disarm the wizard."""
    monkeypatch.setenv("VOICELOG_CONFIG_HOME", str(tmp_path / "voicelog-config"))
    monkeypatch.setenv("VOICELOG_NO_SETUP", "1")
    # Keep terminal styling off: under `pytest -s` stdout IS a real tty,
    # which would otherwise flip ANSI on and break stdout assertions.
    monkeypatch.setenv("NO_COLOR", "1")
    for name in _PROVIDER_KEY_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on an unmocked HTTP call.

    Per-test ``mock.patch("voicelog.<mod>.httpx.<verb>", ...)`` targets the same
    module attribute, so it shadows this guard and restores it on exit — the
    house pattern keeps working unchanged.
    """
    def _boom(*args, **kwargs):
        raise AssertionError(
            "unmocked HTTP call — patch voicelog.<module>.httpx.<verb> in this test"
        )

    for verb in ("get", "post", "request", "stream"):
        monkeypatch.setattr(httpx, verb, _boom)
