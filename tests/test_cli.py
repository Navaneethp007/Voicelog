"""CLI wiring tests â€” focus on the Phase 2 contract: text output is never
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
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(
            commits=[Commit("h", "feat: a thing", "", "Al", [])],
            used_fallback=False,
            tag="v1.0.0",
            diff="+fake diff line" if with_diff else None,
        ),
    )
    monkeypatch.setattr(cli.voice, "load_voice", lambda d: "")
    monkeypatch.setattr(
        cli.generate,
        "generate",
        lambda commits, voice_text, cfg, diff=None: "## Unreleased\n\n### Features\n- A thing happened",
    )
    # Spoken summary is a separate LLM call â€” stub it so tests stay offline.
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
    """The default run is a transient rundown â€” no persistent changelog."""
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

    def counting_generate(commits, voice_text, cfg, diff=None):
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
    """More than max_commits â†’ only the most recent max_commits go to the model."""
    cap = _config().max_commits
    many = [Commit(f"h{i}", f"feat: thing {i}", "", "Al", []) for i in range(cap + 60)]
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(commits=many, used_fallback=False, tag="v1.0.0"),
    )
    seen = {}

    def recording_generate(commits, voice_text, cfg, diff=None):
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

    def rec(fallback_commits=50, since=None, with_diff=False):
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

    def rec(fallback_commits=50, since=None, with_diff=False):
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

    def rec(fallback_commits=50, since=None, with_diff=False):
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
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(
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

    def rec(fallback_commits=50, since=None, with_diff=False):
        seen["since"] = since
        return GitResult(commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None)

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since", "main", "--no-speak"])

    cli.main()

    assert seen["since"] == "main"


# ---------------------------------------------------------------------------
# --with-diff — opt-in code diff for richer summaries
# ---------------------------------------------------------------------------

def test_with_diff_requests_diff_from_gitsource(wired, monkeypatch):
    seen = {}

    def rec(fallback_commits=50, since=None, with_diff=False):
        seen["with_diff"] = with_diff
        return GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])],
            used_fallback=False, tag="v1.0.0", diff="+added_line",
        )

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--with-diff", "--no-speak"])

    cli.main()

    assert seen["with_diff"] is True


def test_without_with_diff_flag_defaults_to_false(wired, monkeypatch):
    seen = {}

    def rec(fallback_commits=50, since=None, with_diff=False):
        seen["with_diff"] = with_diff
        return GitResult(commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag="v1.0.0")

    monkeypatch.setattr(cli.gitsource, "read_commits", rec)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert seen["with_diff"] is False


def test_with_diff_recomputed_when_capping_changes_oldest_commit(wired, monkeypatch):
    """On a large range, the diff must be rescoped to the CAPPED commit list,
    not the diff GitResult computed over the full (uncapped) range."""
    cap = _config().max_commits
    many = [Commit(f"h{i}", f"feat: thing {i}", "", "Al", []) for i in range(cap + 20)]
    # many[0] is newest, many[-1] is oldest — capping keeps many[:cap], so the
    # new oldest-in-range is many[cap - 1], which differs from many[-1].
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(
            commits=many, used_fallback=False, tag="v1.0.0",
            diff="stale_full_range_diff" if with_diff else None,
        ),
    )
    monkeypatch.setattr(
        cli.gitsource, "diff_for_commits",
        lambda commits, max_chars=20_000: "rescoped_capped_diff",
    )
    seen = {}

    def recording_generate(commits, voice_text, cfg, diff=None):
        seen["diff"] = diff
        return "## Unreleased\n\n- x"

    monkeypatch.setattr(cli.generate, "generate", recording_generate)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--with-diff", "--no-speak"])

    cli.main()

    assert seen["diff"] == "rescoped_capped_diff"


def test_with_diff_threads_diff_into_generate(wired, monkeypatch):
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])],
            used_fallback=False, tag="v1.0.0",
            diff="unique_diff_content" if with_diff else None,
        ),
    )
    seen = {}

    def recording_generate(commits, voice_text, cfg, diff=None):
        seen["diff"] = diff
        return "## Unreleased\n\n- x"

    monkeypatch.setattr(cli.generate, "generate", recording_generate)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--with-diff", "--no-speak"])

    cli.main()

    assert seen["diff"] == "unique_diff_content"


def test_without_with_diff_generate_receives_no_diff(wired, monkeypatch):
    seen = {}

    def recording_generate(commits, voice_text, cfg, diff=None):
        seen["diff"] = diff
        return "## Unreleased\n\n- x"

    monkeypatch.setattr(cli.generate, "generate", recording_generate)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert seen["diff"] is None


def test_with_diff_prints_privacy_warning(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--with-diff", "--no-speak"])

    cli.main()

    err = capsys.readouterr().err.lower()
    assert "diff" in err and ("sent" in err or "sends" in err or "privacy" in err)


def test_no_privacy_warning_without_with_diff(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    err = capsys.readouterr().err.lower()
    assert "privacy" not in err


# ---------------------------------------------------------------------------
# --new — orient a developer on a repo they just cloned
# ---------------------------------------------------------------------------

def test_new_reads_recent_commits_and_readme(wired, monkeypatch):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: recent work", "", "Al", [])],
            used_fallback=False, tag=None,
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "This project does X.")
    seen = {}

    def rec_onboard(readme_text, commits, cfg, diff=None):
        seen["readme"] = readme_text
        seen["commits"] = commits
        return "## What this is\nDoes X.\n\n## Recent activity\n- recent work"

    monkeypatch.setattr(cli.generate, "onboard", rec_onboard)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    cli.main()

    assert seen["readme"] == "This project does X."
    assert len(seen["commits"]) == 1


def test_new_prints_project_overview_header(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme text")
    monkeypatch.setattr(
        cli.generate, "onboard",
        lambda readme_text, commits, cfg, diff=None: "## What this is\nA tool.",
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    cli.main()

    out = capsys.readouterr().out
    assert "# Project Overview" in out
    assert "A tool." in out


def test_new_never_writes_voice_md(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme")
    monkeypatch.setattr(
        cli.generate, "onboard",
        lambda readme_text, commits, cfg, diff=None: "## What this is\nStuff.",
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    # Even with --changelog, --new is transient and must not persist.
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--changelog", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_new_uses_configured_onboard_commits(wired, monkeypatch):
    seen = {}

    def rec(n=15, with_diff=False):
        seen["n"] = n
        return GitResult(commits=[], used_fallback=False, tag=None)

    monkeypatch.setattr(cli.gitsource, "read_recent_commits", rec)
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme text")
    monkeypatch.setattr(
        cli.generate, "onboard",
        lambda readme_text, commits, cfg, diff=None: "## What this is\nStuff.",
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    cli.main()

    assert seen["n"] == _config().onboard_commits


def test_new_empty_repo_and_no_readme_exits_cleanly(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(commits=[], used_fallback=False, tag=None),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "")
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "nothing" in out.lower()


def test_new_speaks_a_summary(wired, monkeypatch):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme text")
    monkeypatch.setattr(
        cli.generate, "onboard",
        lambda readme_text, commits, cfg, diff=None: "## What this is\nStuff.",
    )
    monkeypatch.setattr(cli.generate, "summarize", lambda md, cfg, detail=False: "Spoken overview.")
    spoke = {"called": False}
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.__setitem__("called", True))
    monkeypatch.setattr("sys.argv", ["voicelog", "--new"])

    cli.main()

    assert spoke["called"] is True


def test_new_no_speak_skips_audio(wired, monkeypatch):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme text")
    monkeypatch.setattr(
        cli.generate, "onboard",
        lambda readme_text, commits, cfg, diff=None: "## What this is\nStuff.",
    )
    spoke = {"called": False}
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.__setitem__("called", True))
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    cli.main()

    assert spoke["called"] is False


def test_new_llm_failure_falls_back_to_readme_and_commits(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: unique_fallback_commit", "", "Al", [])],
            used_fallback=False, tag=None,
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "unique_fallback_readme_text")

    def boom(readme_text, commits, cfg, diff=None):
        raise cli.LLMError("model down")

    monkeypatch.setattr(cli.generate, "onboard", boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "unique_fallback_readme_text" in out
    assert "unique_fallback_commit" in out


def test_new_missing_api_key_exits_with_error(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme")

    def boom(readme_text, commits, cfg, diff=None):
        raise cli.MissingApiKey("Set NVIDIA_API_KEY env var — see https://build.nvidia.com")

    monkeypatch.setattr(cli.generate, "onboard", boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "NVIDIA_API_KEY" in capsys.readouterr().err


def test_new_with_diff_threads_diff_into_onboard(wired, monkeypatch):
    monkeypatch.setattr(
        cli.gitsource,
        "read_recent_commits",
        lambda n=15, with_diff=False: GitResult(
            commits=[Commit("h", "feat: x", "", "Al", [])],
            used_fallback=False, tag=None,
            diff="unique_onboard_diff_content" if with_diff else None,
        ),
    )
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "readme")
    seen = {}

    def rec_onboard(readme_text, commits, cfg, diff=None):
        seen["diff"] = diff
        return "## What this is\nStuff."

    monkeypatch.setattr(cli.generate, "onboard", rec_onboard)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--with-diff", "--no-speak"])

    cli.main()

    assert seen["diff"] == "unique_onboard_diff_content"


def test_new_mutually_exclusive_with_pull(wired, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--pull"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse's usage-error exit code


def test_pull_mode_does_not_write_voice_md(wired, monkeypatch, tmp_path):
    """Diff/pull mode is transient â€” it must not touch the release changelog."""
    monkeypatch.setattr(
        cli.gitsource,
        "read_commits",
        lambda fallback_commits=50, since=None, with_diff=False: GitResult(
            commits=[Commit("h", "feat: pulled", "", "Al", [])], used_fallback=False, tag=None
        ),
    )
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--pull", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_invalid_since_ref_exits_with_error(wired, monkeypatch, capsys):
    def rec(fallback_commits=50, since=None, with_diff=False):
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

    def counting_generate(commits, voice_text, cfg, diff=None):
        calls["n"] += 1
        return "## Unreleased\n\n### Features\n- A thing happened"

    monkeypatch.setattr(cli.generate, "generate", counting_generate)

    cli.main()  # generates + caches
    monkeypatch.setattr("sys.argv", ["voicelog", "--fresh"])
    cli.main()  # --fresh ignores cache â†’ regenerates

    assert calls["n"] == 2
