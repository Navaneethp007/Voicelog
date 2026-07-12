"""CLI wiring tests — focus on the Phase 2 contract: text output is never
gated on TTS, and audio failure only warns."""
from __future__ import annotations

import pytest

from voicelog import cli
from voicelog.config import Config, DEFAULTS
from voicelog.gitsource import GitResult, RefNotFound
from voicelog.models import Commit
from voicelog.tts import TTSError


# ---------------------------------------------------------------------------
# First-run interactive key prompt
# ---------------------------------------------------------------------------

def test_prompt_for_key_sets_env_when_interactive(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "nvapi-pasted")

    cli._maybe_prompt_for_key("MY_KEY")

    assert cli.os.environ.get("MY_KEY") == "nvapi-pasted"


def test_prompt_for_key_noop_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    called = {"asked": False}
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": called.__setitem__("asked", True) or "x")

    cli._maybe_prompt_for_key("MY_KEY")

    assert called["asked"] is False
    assert cli.os.environ.get("MY_KEY") is None


def test_prompt_for_key_skips_when_already_set(monkeypatch):
    monkeypatch.setenv("MY_KEY", "already-here")
    called = {"asked": False}
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": called.__setitem__("asked", True) or "x")

    cli._maybe_prompt_for_key("MY_KEY")

    assert called["asked"] is False
    assert cli.os.environ.get("MY_KEY") == "already-here"


def test_prompt_for_key_empty_input_leaves_unset(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "   ")

    cli._maybe_prompt_for_key("MY_KEY")

    assert cli.os.environ.get("MY_KEY") is None


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
        lambda fallback_commits=50, since=None: GitResult(
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
    # Spoken summary is a separate LLM call — stub it so tests stay offline.
    monkeypatch.setattr(cli.generate, "summarize", lambda md, cfg, detail=False: "A short spoken summary.")
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


def test_default_run_does_not_write_voice_md(wired, monkeypatch, tmp_path):
    """The default run is a transient rundown — no persistent changelog."""
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_changelog_flag_writes_voice_md(wired, monkeypatch, tmp_path):
    """--changelog opts into the persistent release changelog."""
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--changelog"])

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


def test_speaks_summary_not_full_changelog(wired, monkeypatch):
    """Audio uses the short spoken summary, never the full rendered changelog."""
    spoken = {}
    monkeypatch.setattr(cli.generate, "summarize", lambda md, cfg, detail=False: "SUMMARY LINE")
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoken.setdefault("text", text))

    cli.main()

    assert spoken["text"] == "SUMMARY LINE"
    assert "A thing happened" not in spoken["text"]


def test_detail_flag_requests_detailed_summary(wired, monkeypatch):
    seen = {}

    def rec_summarize(md, cfg, detail=False):
        seen["detail"] = detail
        return "detailed spoken summary"

    monkeypatch.setattr(cli.generate, "summarize", rec_summarize)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--detail"])

    cli.main()

    assert seen["detail"] is True


def test_summary_is_cached_across_runs(wired, monkeypatch):
    """A repeat run with the same commits must NOT call summarize again."""
    calls = {"n": 0}

    def counting_summarize(md, cfg, detail=False):
        calls["n"] += 1
        return "spoken summary"

    monkeypatch.setattr(cli.generate, "summarize", counting_summarize)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()  # generates + caches summary
    cli.main()  # should reuse cached summary

    assert calls["n"] == 1


def test_default_summary_is_brief(wired, monkeypatch):
    seen = {}

    def rec_summarize(md, cfg, detail=False):
        seen["detail"] = detail
        return "brief"

    monkeypatch.setattr(cli.generate, "summarize", rec_summarize)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert seen["detail"] is False


def test_summary_failure_skips_audio_but_still_prints(wired, monkeypatch, capsys):
    """If the spoken summary can't be produced, text still prints and audio is skipped."""
    def boom(md, cfg, detail=False):
        raise cli.LLMError("summary model down")

    spoke = {"called": False}
    monkeypatch.setattr(cli.generate, "summarize", boom)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.__setitem__("called", True))

    cli.main()

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert spoke["called"] is False
    assert "warning" in out.err.lower()


def test_caps_commits_for_large_repo(wired, monkeypatch, capsys):
    """More than max_commits → only the most recent max_commits go to the model."""
    cap = _config().max_commits
    many = [Commit(f"h{i}", f"feat: thing {i}", "", "Al", []) for i in range(cap + 60)]
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None: GitResult(commits=many, used_fallback=False, tag="v1.0.0"),
    )
    seen = {}

    def recording_generate(commits, voice_text, cfg):
        seen["n"] = len(commits)
        return "## Unreleased\n\n- x"

    monkeypatch.setattr(cli.generate, "generate", recording_generate)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert seen["n"] == cap  # capped at the default max_commits
    assert "warning" in capsys.readouterr().err.lower()


def test_pull_flag_reads_since_orig_head(wired, monkeypatch):
    """--pull asks gitsource for commits since ORIG_HEAD."""
    seen = {}

    def rec(fallback_commits=50, since=None):
        seen["since"] = since
        return GitResult(
            commits=[Commit("h", "feat: pulled thing", "", "Al", [])],
            used_fallback=False,
            tag=None,
        )

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pull", "--no-speak"])

    cli.main()

    assert seen["since"] == "ORIG_HEAD"


def test_pr_flag_uses_detected_base(wired, monkeypatch):
    """--pr summarises commits since the auto-detected base branch."""
    seen = {}

    def rec(fallback_commits=50, since=None):
        seen["since"] = since
        return GitResult(commits=[Commit("h", "feat: pr work", "", "Al", [])], used_fallback=False, tag=None)

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.gitsource, "detect_base_branch", lambda: "origin/main")
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pr", "--no-speak"])

    cli.main()

    assert seen["since"] == "origin/main"


def test_pr_with_explicit_base_uses_it(wired, monkeypatch):
    """--pr develop compares against the given branch, skipping auto-detect."""
    seen = {}

    def rec(fallback_commits=50, since=None):
        seen["since"] = since
        return GitResult(commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None)

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    # Auto-detect must NOT be consulted when a base is given.
    monkeypatch.setattr(
        cli.gitsource, "detect_base_branch",
        lambda: (_ for _ in ()).throw(AssertionError("should not auto-detect")),
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pr", "develop", "--no-speak"])

    cli.main()

    assert seen["since"] == "develop"


def test_pr_no_base_exits_with_error(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli.gitsource, "detect_base_branch", lambda: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pr", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "base branch" in capsys.readouterr().err.lower()


def test_pr_mode_does_not_write_voice_md(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.gitsource, "detect_base_branch", lambda: "main")
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    # Even with --changelog, PR mode is transient and must not persist.
    monkeypatch.setattr("sys.argv", ["voicelog", "--pr", "--changelog", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_since_flag_passes_ref(wired, monkeypatch):
    seen = {}

    def rec(fallback_commits=50, since=None):
        seen["since"] = since
        return GitResult(commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None)

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since", "main", "--no-speak"])

    cli.main()

    assert seen["since"] == "main"


def test_pull_mode_does_not_write_voice_md(wired, monkeypatch, tmp_path):
    """Diff/pull mode is transient — it must not touch the release changelog."""
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None: GitResult(
            commits=[Commit("h", "feat: pulled", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pull", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_invalid_since_ref_exits_with_error(wired, monkeypatch, capsys):
    def rec(fallback_commits=50, since=None):
        raise RefNotFound(since)

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since", "bogusref", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "bogusref" in capsys.readouterr().err


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
