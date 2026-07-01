"""CLI wiring tests — focus on the Phase 2 contract: text output is never
gated on TTS, and audio failure only warns."""
from __future__ import annotations

import pytest

from voicelog import cli
from voicelog.config import Config, DEFAULTS
from voicelog.gitsource import GitResult
from voicelog.models import Commit
from voicelog.tts import TTSError


def _config():
    return Config(
        provider=DEFAULTS["provider"],
        base_url=DEFAULTS["base_url"],
        model=DEFAULTS["model"],
        sections=list(DEFAULTS["sections"]),
        noise=list(DEFAULTS["noise"]),
        voice_samples=DEFAULTS["voice_samples"],
        fallback_commits=int(DEFAULTS["fallback_commits"]),
        speak=DEFAULTS["speak"],
        tts_function_id=DEFAULTS["tts_function_id"],
        tts_voice=DEFAULTS["tts_voice"],
        tts_language=DEFAULTS["tts_language"],
        tts_sample_rate=DEFAULTS["tts_sample_rate"],
        voice_md=DEFAULTS["voice_md"],
    )


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Patch the whole pipeline so main() runs offline; cwd is a temp dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.config_module, "load", lambda path: _config())
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda n: GitResult(
            commits=[Commit("h", "feat: a thing", "", "Al", [])],
            used_fallback=False,
            tag="v1.0.0",
        ),
    )
    monkeypatch.setattr(cli.voice, "load_voice", lambda d: "")
    monkeypatch.setattr(
        cli.generate,
        "generate",
        lambda commits, voice_text, cfg: "## Unreleased\n\n### Features\n- A thing happened",
    )
    monkeypatch.setattr("sys.argv", ["voicelog"])


def test_text_prints_even_when_tts_fails(wired, monkeypatch, capsys):
    def boom(text, config):
        raise TTSError("no audio device")

    monkeypatch.setattr(cli.tts, "speak", boom)

    cli.main()  # must NOT raise

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert "A thing happened" in out.out
    # TTS failure surfaces only as a stderr warning.
    assert "warning" in out.err.lower()
    assert "no audio device" in out.err


def test_no_speak_flag_skips_tts(wired, monkeypatch, capsys):
    called = {"spoke": False}

    def spy(text, config):
        called["spoke"] = True

    monkeypatch.setattr(cli.tts, "speak", spy)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert called["spoke"] is False


def test_speaks_on_normal_run(wired, monkeypatch, capsys):
    called = {"spoke": False}

    def spy(text, config):
        called["spoke"] = True

    monkeypatch.setattr(cli.tts, "speak", spy)

    cli.main()

    assert called["spoke"] is True


def test_voice_md_written(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    voice_md = tmp_path / ".changelog" / "voice.md"
    assert voice_md.exists()
    content = voice_md.read_text(encoding="utf-8")
    assert "## Unreleased" in content
    assert "A thing happened" in content


def test_second_run_uses_cache_and_skips_generate(wired, monkeypatch):
    """A repeat run with the same commits must NOT call the model again."""
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    calls = {"n": 0}

    def counting_generate(commits, voice_text, cfg):
        calls["n"] += 1
        return "## Unreleased\n\n### Features\n- A thing happened"

    monkeypatch.setattr(cli.generate, "generate", counting_generate)

    cli.main()  # generates + caches
    cli.main()  # should hit cache

    assert calls["n"] == 1


def test_fresh_flag_bypasses_cache(wired, monkeypatch):
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    calls = {"n": 0}

    def counting_generate(commits, voice_text, cfg):
        calls["n"] += 1
        return "## Unreleased\n\n### Features\n- A thing happened"

    monkeypatch.setattr(cli.generate, "generate", counting_generate)

    cli.main()  # generates + caches
    monkeypatch.setattr("sys.argv", ["voicelog", "--fresh"])
    cli.main()  # --fresh ignores cache → regenerates

    assert calls["n"] == 2
