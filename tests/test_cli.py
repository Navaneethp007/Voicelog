"""CLI wiring tests â€” focus on the Phase 2 contract: text output is never
gated on TTS, and audio failure only warns."""
from __future__ import annotations

import os

import pytest

from voicelog import cli
from voicelog.config import Config, DEFAULTS
from voicelog.gitsource import GitResult, RefNotFound
from voicelog.models import Commit
from voicelog.tts import TTSError


def _config():
    return Config(
        provider=DEFAULTS["provider"],
        base_url=DEFAULTS["base_url"],
        model="test-model",  # never DEFAULTS["model"]: there is no default model
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


# ---------------------------------------------------------------------------
# CLI overrides: flags are the highest-precedence config layer
# ---------------------------------------------------------------------------

def _capture_cfg(monkeypatch):
    """Record the Config that actually reaches generation."""
    seen = {}

    def _generate(commits, voice_text, cfg, diff=None):
        seen["cfg"] = cfg
        return "## Unreleased\n\n### Features\n- A thing happened"

    monkeypatch.setattr(cli.generate, "generate", _generate)
    return seen


def test_model_flag_overrides_the_configured_model(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--model", "vendor/other", "--no-speak"])

    cli.main()

    assert seen["cfg"].model == "vendor/other"


def test_provider_flag_also_swaps_base_url_and_key_env(wired, monkeypatch):
    """Otherwise --provider openai sends an OpenAI model to NVIDIA's endpoint."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "openai", "--no-speak"])

    cli.main()

    assert seen["cfg"].provider == "openai"
    assert seen["cfg"].base_url == "https://api.openai.com/v1"
    assert seen["cfg"].api_key_env == "OPENAI_API_KEY"


def test_explicit_base_url_beats_the_provider_preset(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--provider", "openai", "--base-url", "https://proxy.test/v1/", "--no-speak",
    ])

    cli.main()

    assert seen["cfg"].base_url == "https://proxy.test/v1"  # normalized, preset overridden


def test_blank_api_key_env_flag_survives(wired, monkeypatch):
    """"" is meaningful - it says the endpoint needs no key at all."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--api-key-env", "", "--no-speak"])

    cli.main()

    assert seen["cfg"].api_key_env == ""


def test_voice_and_tts_provider_flags_apply(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--tts-provider", "openai", "--voice", "nova",
    ])

    cli.main()

    assert seen["cfg"].tts_provider == "openai"
    assert seen["cfg"].tts_voice == "nova"


def test_no_flags_leaves_the_config_untouched(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert seen["cfg"].model == "test-model"


# ---------------------------------------------------------------------------
# When the wizard may run
# ---------------------------------------------------------------------------

def test_not_interactive_when_stdin_is_not_a_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("VOICELOG_NO_SETUP", raising=False)

    assert cli._interactive() is False


def test_not_interactive_when_stdout_is_piped(monkeypatch):
    """Under some git hook runners stdin is the terminal but stdout is piped;
    prompting there hangs `git commit` and looks like git is broken."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    monkeypatch.delenv("VOICELOG_NO_SETUP", raising=False)

    assert cli._interactive() is False


@pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS", "GIT_INDEX_FILE", "VOICELOG_NO_SETUP"])
def test_not_interactive_in_ci_or_a_git_hook(monkeypatch, var):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    for name in ("CI", "GITHUB_ACTIONS", "GIT_INDEX_FILE", "GIT_DIR", "VOICELOG_NO_SETUP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, "1")

    assert cli._interactive() is False


def test_interactive_on_a_plain_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    for name in ("CI", "GITHUB_ACTIONS", "GIT_INDEX_FILE", "GIT_DIR", "VOICELOG_NO_SETUP"):
        monkeypatch.delenv(name, raising=False)

    assert cli._interactive() is True


def _args(**overrides):
    import argparse

    base = {"config": None, "model": None, "setup": False, "replay": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_needs_setup_on_a_genuinely_fresh_machine(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args()) is True


def test_no_setup_when_a_model_is_already_configured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    assert cli._needs_setup(_config(), _args()) is False


def test_no_setup_when_a_user_config_exists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    path = cli.config_module.user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("provider: groq\n")
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args()) is False


def test_setup_runs_when_the_project_configures_only_project_settings(monkeypatch, tmp_path):
    """A changelog.yml holding sections/noise says nothing about which model to
    call. Treating its mere existence as "configured" meant such a repo - the
    shape voicelog itself ships - printed a raw commit list forever."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    (tmp_path / "changelog.yml").write_text("sections: [Fixes]\n", encoding="utf-8")
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args()) is True


def test_no_setup_when_the_project_pins_a_model(monkeypatch, tmp_path):
    """Driven through the real loader, so the cfg.model path is covered
    end-to-end rather than with a hand-built Config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    (tmp_path / "changelog.yml").write_text("model: vendor/pinned\n", encoding="utf-8")
    cfg = cli.config_module.load(None)

    assert cfg.model == "vendor/pinned"
    assert cli._needs_setup(cfg, _args()) is False


def test_no_setup_when_config_was_passed_explicitly(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args(config="somewhere.yml")) is False


def test_no_setup_when_not_interactive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args()) is False


def test_setup_flag_refuses_to_run_without_a_terminal(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "interactive" in capsys.readouterr().err.lower()


def test_setup_flag_writes_and_exits_without_running_a_changelog(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.wizard, "run_setup", lambda current, dest: {"model": "picked/one"})
    generated = {"ran": False}
    monkeypatch.setattr(
        cli.generate, "generate",
        lambda *a, **k: generated.__setitem__("ran", True) or "x",
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert generated["ran"] is False
    assert cli.config_module.read_user_config()["model"] == "picked/one"


def test_aborted_setup_writes_nothing(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    def _abort(current, dest):
        raise cli.wizard.SetupAborted()

    monkeypatch.setattr(cli.wizard, "run_setup", _abort)
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert cli.config_module.read_user_config() == {}
    assert "cancel" in capsys.readouterr().err.lower()


def test_unwritable_config_prints_the_yaml_to_save_by_hand(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.wizard, "run_setup", lambda current, dest: {"model": "picked/one"})

    def _boom(values, path=None):
        raise OSError("read-only file system")

    monkeypatch.setattr(cli.config_module, "save_user_config", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit):
        cli.main()

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "read-only file system" in combined
    assert "model: picked/one" in combined  # so the work is not lost


# ---------------------------------------------------------------------------
# Model misconfiguration stays non-fatal, but says what to do
# ---------------------------------------------------------------------------

def test_missing_model_prints_the_fix_and_still_prints_the_commit_list(wired, monkeypatch, capsys):
    """Exit 0 keeps pre-push hooks and CI working, as 0.2.0 did."""
    def _boom(commits, voice_text, cfg, diff=None):
        raise cli.MissingModel("no model configured - run `voicelog --setup`")

    monkeypatch.setattr(cli.generate, "generate", _boom)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr()
    assert "--setup" in out.err
    assert "feat: a thing" in out.out  # the commit-list fallback still ran


def test_retired_model_prints_the_fix_and_still_prints_the_commit_list(wired, monkeypatch, capsys):
    def _boom(commits, voice_text, cfg, diff=None):
        raise cli.ModelUnavailable("model 'x' was rejected - run `voicelog --setup`")

    monkeypatch.setattr(cli.generate, "generate", _boom)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr()
    assert "--setup" in out.err
    assert "feat: a thing" in out.out


def test_retired_model_during_the_spoken_summary_only_warns(wired, monkeypatch, capsys):
    """The changelog is already printed by then; audio is best-effort."""
    def _boom(md, cfg, detail=False):
        raise cli.ModelUnavailable("model 'x' was rejected - run `voicelog --setup`")

    monkeypatch.setattr(cli.generate, "summarize", _boom)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert "--setup" in out.err


def test_retired_model_on_the_new_path_still_prints_the_fallback(wired, monkeypatch, capsys):
    def _boom(readme_text, commits, cfg, diff=None):
        raise cli.ModelUnavailable("model 'x' was rejected - run `voicelog --setup`")

    monkeypatch.setattr(cli.gitsource, "read_recent_commits",
                        lambda n, with_diff=False: GitResult(
                            commits=[Commit("h", "feat: a thing", "", "Al", [])],
                            used_fallback=False, tag=None, diff=None))
    monkeypatch.setattr(cli.readme, "load_readme", lambda d: "# Project")
    monkeypatch.setattr(cli.generate, "onboard", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--new", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "--setup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Keys: text and speech can live in different env vars
# ---------------------------------------------------------------------------

def test_speech_key_is_requested_when_it_differs_from_the_text_key(wired, monkeypatch):
    """Mixing providers (OpenAI text + Riva voice) is the wizard's whole point,
    and 0.2.0 only ever prompted for the text key."""
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key",
                        lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "elevenlabs"])

    def _load(path):
        cfg = _config()
        cfg.tts_api_key_env = "ELEVENLABS_API_KEY"
        return cfg

    monkeypatch.setattr(cli.config_module, "load", _load)

    cli.main()

    assert asked == ["NVIDIA_API_KEY", "ELEVENLABS_API_KEY"]


def test_speech_key_is_not_requested_twice_when_it_is_the_same_var(wired, monkeypatch):
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key",
                        lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert asked == ["NVIDIA_API_KEY"]


def test_speech_key_is_not_requested_when_speech_is_off(wired, monkeypatch):
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key",
                        lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    def _load(path):
        cfg = _config()
        cfg.tts_api_key_env = "ELEVENLABS_API_KEY"
        return cfg

    monkeypatch.setattr(cli.config_module, "load", _load)

    cli.main()

    assert asked == ["NVIDIA_API_KEY"]


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------

def test_unparseable_config_exits_with_a_message(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("model: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["voicelog"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "config" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------

def test_cache_key_includes_the_provider_identity(wired, monkeypatch):
    """The same model id on two providers is not the same output."""
    seen = {}
    real_key = cli.cache.cache_key

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real_key(*args, **kwargs)

    monkeypatch.setattr(cli.cache, "cache_key", _spy)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert seen["provider"] == DEFAULTS["provider"]
    assert seen["base_url"] == DEFAULTS["base_url"]


# ---------------------------------------------------------------------------
# Flag validation at the CLI boundary
# ---------------------------------------------------------------------------

def test_unknown_provider_is_rejected_by_name(wired, monkeypatch, capsys):
    """A typo must not silently keep the old endpoint and surface later as a
    misleading "model was rejected" error."""
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "nvidai", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "nvidai" in err
    assert "nvidia" in err  # lists what is valid


def test_unknown_tts_provider_is_rejected_by_name(wired, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "elevenlabz"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "elevenlabz" in err
    assert "elevenlabs" in err


def test_custom_provider_is_accepted_and_leaves_the_endpoint_alone(wired, monkeypatch):
    """--provider custom is meaningful when paired with --base-url."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--provider", "custom", "--base-url", "https://proxy.test/v1", "--no-speak",
    ])

    cli.main()

    assert seen["cfg"].provider == "custom"
    assert seen["cfg"].base_url == "https://proxy.test/v1"


# ---------------------------------------------------------------------------
# --tts-provider must swap the whole speech preset, like --provider does
# ---------------------------------------------------------------------------

def test_tts_provider_flag_swaps_the_key_env_and_voice(wired, monkeypatch):
    """Otherwise --tts-provider elevenlabs sends the NVIDIA key as xi-api-key
    with a Magpie voice name as the ElevenLabs voice id."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "elevenlabs"])

    cli.main()

    assert seen["cfg"].tts_api_key_env == "ELEVENLABS_API_KEY"
    # ElevenLabs voices are per-account ids, so there is no sensible default;
    # blank produces tts.py's actionable "needs tts_voice set to a voice id".
    assert seen["cfg"].tts_voice == ""


def test_tts_provider_flag_picks_the_new_backend_default_voice(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "openai"])

    cli.main()

    assert seen["cfg"].tts_api_key_env == "OPENAI_API_KEY"
    assert seen["cfg"].tts_voice == "alloy"


def test_explicit_voice_beats_the_tts_preset(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--tts-provider", "elevenlabs", "--voice", "my-voice-id",
    ])

    cli.main()

    assert seen["cfg"].tts_voice == "my-voice-id"


def test_tts_provider_none_disables_speech_instead_of_failing(wired, monkeypatch, capsys):
    """`none` is offered in the wizard, so it must mean the same thing here."""
    spoke = {"called": False}
    monkeypatch.setattr(cli.tts, "speak",
                        lambda text, config: spoke.__setitem__("called", True))
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "none"])

    cli.main()

    assert spoke["called"] is False
    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert "could not speak" not in out.err  # not a failure, a choice


# ---------------------------------------------------------------------------
# A corrupt config must not block the command that repairs it
# ---------------------------------------------------------------------------

def _write_broken_user_config():
    path = cli.config_module.user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("provider: [unclosed\n")
    return path


def test_setup_still_runs_when_the_user_config_is_corrupt(monkeypatch, tmp_path, capsys):
    """--setup is the documented repair, so it cannot be gated on a good config."""
    monkeypatch.chdir(tmp_path)
    _write_broken_user_config()
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.wizard, "run_setup", lambda current, dest: {"model": "fixed/model"})
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert cli.config_module.read_user_config()["model"] == "fixed/model"
    assert "warning" in capsys.readouterr().err.lower()


def test_corrupt_user_config_still_fails_a_normal_run(monkeypatch, tmp_path, capsys):
    """Only --setup gets the pass; a normal run must still say what is wrong."""
    monkeypatch.chdir(tmp_path)
    _write_broken_user_config()
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "config" in capsys.readouterr().err.lower()


def test_unreadable_target_still_prints_the_settings_to_save(monkeypatch, tmp_path, capsys):
    """save_user_config re-reads the target, so it can raise ConfigFileInvalid -
    which must not bypass the recovery print."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.wizard, "run_setup", lambda current, dest: {"model": "picked/one"})

    def _boom(values, path=None):
        raise cli.ConfigFileInvalid("config.yml: while parsing a flow sequence")

    monkeypatch.setattr(cli.config_module, "save_user_config", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "model: picked/one" in combined
    assert "flow sequence" in combined


# ---------------------------------------------------------------------------
# An unreadable config is intact: never "repair" it by overwriting
# ---------------------------------------------------------------------------

def _load_raises(monkeypatch, exc):
    monkeypatch.setattr(
        cli.config_module, "load", lambda path: (_ for _ in ()).throw(exc)
    )


def test_unreadable_config_exits_even_under_setup(monkeypatch, tmp_path, capsys):
    """--setup may replace a corrupt config, but not one it could not read."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    ran = {"wizard": False}
    monkeypatch.setattr(
        cli.wizard, "run_setup",
        lambda current, dest: ran.__setitem__("wizard", True) or {"model": "x"},
    )
    _load_raises(monkeypatch, cli.ConfigFileUnreadable("config.yml: locked"))
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert ran["wizard"] is False
    err = capsys.readouterr().err
    assert "locked" in err
    assert "fresh config" not in err  # must not promise to overwrite it


def test_unreadable_config_exits_on_a_normal_run(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _load_raises(monkeypatch, cli.ConfigFileUnreadable("config.yml: locked"))
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "locked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A preset is seeded only when the provider actually changes
# ---------------------------------------------------------------------------

def _load_config(monkeypatch, **fields):
    """Make main() load a Config with these fields overridden."""
    def _load(path):
        cfg = _config()
        for name, value in fields.items():
            setattr(cfg, name, value)
        return cfg

    monkeypatch.setattr(cli.config_module, "load", _load)


def test_restating_the_current_provider_keeps_a_customised_endpoint(wired, monkeypatch):
    """The wizard writes a custom base_url, then _apply_overrides runs AGAIN on
    the reloaded config - an unconditional reseed silently undid it."""
    seen = _capture_cfg(monkeypatch)
    _load_config(
        monkeypatch,
        provider="nvidia",
        base_url="https://my-proxy.test/v1",
        api_key_env="WORK_NVIDIA_KEY",
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "nvidia", "--no-speak"])

    cli.main()

    assert seen["cfg"].base_url == "https://my-proxy.test/v1"
    assert seen["cfg"].api_key_env == "WORK_NVIDIA_KEY"


def test_switching_provider_still_seeds_the_preset(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    _load_config(monkeypatch, provider="nvidia", base_url="https://my-proxy.test/v1")
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "groq", "--no-speak"])

    cli.main()

    assert seen["cfg"].base_url == "https://api.groq.com/openai/v1"
    assert seen["cfg"].api_key_env == "GROQ_API_KEY"


def test_restating_the_voice_backend_keeps_its_settings(wired, monkeypatch):
    """--tts-provider openai on a config already using openai must not downgrade
    a chosen voice, model or endpoint."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    _load_config(
        monkeypatch,
        tts_provider="openai",
        tts_voice="nova",
        tts_model="tts-1-hd",
        tts_base_url="https://speech-proxy.test/v1",
        tts_api_key_env="WORK_OPENAI_KEY",
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "openai"])

    cli.main()

    assert seen["cfg"].tts_voice == "nova"
    assert seen["cfg"].tts_model == "tts-1-hd"
    assert seen["cfg"].tts_base_url == "https://speech-proxy.test/v1"
    assert seen["cfg"].tts_api_key_env == "WORK_OPENAI_KEY"


def test_switching_the_voice_backend_clears_the_old_ones_settings(wired, monkeypatch):
    """Otherwise an OpenAI tts_model is sent to ElevenLabs as its model_id, and
    an OpenAI proxy URL is reused for a different service."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    _load_config(
        monkeypatch,
        tts_provider="openai",
        tts_voice="nova",
        tts_model="tts-1-hd",
        tts_base_url="https://speech-proxy.test/v1",
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "elevenlabs"])

    cli.main()

    assert seen["cfg"].tts_model == ""
    assert seen["cfg"].tts_base_url == ""
    assert seen["cfg"].tts_api_key_env == "ELEVENLABS_API_KEY"


def test_provider_value_is_canonicalised(wired, monkeypatch):
    """A padded value used to be stored verbatim, polluting the cache key."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", " OpenAI ", "--no-speak"])

    cli.main()

    assert seen["cfg"].provider == "openai"


def test_padded_tts_none_still_disables_speech(wired, monkeypatch):
    """' none' used to miss the comparison and end as an unknown-provider error."""
    spoke = {"called": False}
    monkeypatch.setattr(cli.tts, "speak",
                        lambda text, config: spoke.__setitem__("called", True))
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", " None "])

    cli.main()

    assert spoke["called"] is False


def test_model_flag_is_stripped(wired, monkeypatch):
    """Otherwise ' m' and 'm' are two different cache entries."""
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--model", "  vendor/m  ", "--no-speak"])

    cli.main()

    assert seen["cfg"].model == "vendor/m"


# ---------------------------------------------------------------------------
# --provider custom is a promise to supply the endpoint
# ---------------------------------------------------------------------------

def test_bare_custom_provider_is_rejected(wired, monkeypatch, capsys):
    """Without --base-url there is nothing custom about it: it would keep the
    old endpoint and merely relabel, which also changes the cache key."""
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "custom", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--base-url" in err
    assert "--api-key-env" in err  # a custom endpoint often needs its own key


def test_empty_base_url_does_not_satisfy_custom(wired, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--provider", "custom", "--base-url", "", "--no-speak",
    ])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Switching provider while reusing the configured model
# ---------------------------------------------------------------------------

def test_switching_provider_warns_that_it_reuses_the_config_model(wired, monkeypatch, capsys):
    """Better than clearing the model: `voicelog --provider groq` is a
    documented example, and clearing would break it."""
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "groq", "--no-speak"])

    cli.main()

    err = capsys.readouterr().err
    assert "test-model" in err
    assert "--model" in err


def test_no_warning_when_the_model_was_given_explicitly(wired, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "voicelog", "--provider", "groq", "--model", "llama-3.1-8b-instant", "--no-speak",
    ])

    cli.main()

    assert "from your config" not in capsys.readouterr().err


def test_no_warning_when_the_provider_did_not_change(wired, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["voicelog", "--provider", "nvidia", "--no-speak"])

    cli.main()

    assert "from your config" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Where to get a key, outside the wizard too
# ---------------------------------------------------------------------------

def test_key_prompt_offers_the_signup_link(wired, monkeypatch):
    """The wizard passes signup_url; a plain run had no reason not to."""
    seen = {}
    monkeypatch.setattr(
        cli.wizard, "ensure_key",
        lambda env, **kw: seen.update(kw) or True,
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert seen.get("signup_url") == "https://build.nvidia.com"


# ---------------------------------------------------------------------------
# A rejected key says so, and still prints something useful
# ---------------------------------------------------------------------------

def test_rejected_key_prints_the_fix_and_the_commit_list(wired, monkeypatch, capsys):
    def _boom(commits, voice_text, cfg, diff=None):
        raise cli.InvalidApiKey("NVIDIA_API_KEY was rejected - the key may have expired")

    monkeypatch.setattr(cli.generate, "generate", _boom)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0  # never breaks a hook or a CI step
    out = capsys.readouterr()
    assert "expired" in out.err
    assert "feat: a thing" in out.out


def test_rejected_key_during_the_spoken_summary_only_warns(wired, monkeypatch, capsys):
    def _boom(md, cfg, detail=False):
        raise cli.InvalidApiKey("NVIDIA_API_KEY was rejected - the key may have expired")

    monkeypatch.setattr(cli.generate, "summarize", _boom)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert "expired" in out.err


# ---------------------------------------------------------------------------
# Terminal styling must reach stdout and nothing else
# ---------------------------------------------------------------------------

def test_stdout_styling_never_reaches_voice_md_or_the_summariser(
    wired, monkeypatch, tmp_path, capsys
):
    """The rendered markdown is ONE string object shared by three consumers:
    print, voicefile (which matches ^## Unreleased and byte-compares the file)
    and the spoken-summary prompt. Styling may only happen at the print
    boundary. Nothing guarded this before."""
    monkeypatch.setattr(cli.textstyle, "style", lambda t, **kw: "STYLEDMARKER" + t)
    seen = {}

    def _summarize(md, cfg, detail=False):
        seen["md"] = md
        return "A short spoken summary."

    monkeypatch.setattr(cli.generate, "summarize", _summarize)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--changelog"])

    cli.main()

    assert "STYLEDMARKER" in capsys.readouterr().out
    content = (tmp_path / ".changelog" / "voice.md").read_text(encoding="utf-8")
    assert "STYLEDMARKER" not in content
    assert "\033" not in content
    # The marker line is first, then the block voicefile must be able to find.
    assert content.splitlines()[1].startswith("## Unreleased")
    assert "STYLEDMARKER" not in seen["md"]


def test_styling_is_off_when_stdout_is_not_a_terminal(wired, monkeypatch, capsys):
    """The default in the suite, and the thing that keeps piped output pure."""
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert "\033" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The watermark: how far we have summarised
# ---------------------------------------------------------------------------

def _watermark(monkeypatch, head="a" * 40):
    """Spy the watermark write and pin HEAD. Returns the list of recorded shas."""
    recorded = []
    monkeypatch.setattr(cli.gitsource, "head_sha", lambda: head)
    monkeypatch.setattr(cli.state, "record_summarised",
                        lambda sha: recorded.append(sha) or True)
    return recorded


def _since_spy(monkeypatch, commits=None):
    """Replace read_commits, recording the range it was asked for."""
    seen = {}

    def _read(fallback_commits=50, since=None, with_diff=False):
        seen["since"] = since
        return GitResult(
            commits=commits if commits is not None
            else [Commit("h", "feat: a thing", "", "Al", [])],
            used_fallback=False,
            tag="v1.0.0",
            diff=None,
        )

    monkeypatch.setattr(cli.gitsource, "read_commits", _read)
    return seen


def test_default_run_records_the_watermark(wired, monkeypatch):
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert recorded == ["a" * 40]


def test_a_cache_hit_still_records(wired, monkeypatch, tmp_path):
    """The watermark records what the user was shown, not what we paid for."""
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])
    cli.main()
    recorded.clear()

    cli.main()  # second run: served from cache

    assert recorded == ["a" * 40]


def test_a_speech_failure_still_records(wired, monkeypatch):
    """Audio never blocks, so a broken sound device must not freeze the
    watermark forever."""
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak",
                        lambda text, config: (_ for _ in ()).throw(TTSError("no device")))

    cli.main()

    assert recorded == ["a" * 40]


@pytest.mark.parametrize("error", ["MissingModel", "ModelUnavailable", "LLMError"])
def test_a_commit_list_fallback_does_not_record(wired, monkeypatch, error):
    """A raw commit list is not a summary. Advancing here would skip those
    commits permanently once the model worked again."""
    recorded = _watermark(monkeypatch)
    exc = getattr(cli, error)("boom")
    monkeypatch.setattr(cli.generate, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    with pytest.raises(SystemExit):
        cli.main()

    assert recorded == []


def test_the_no_commits_exit_does_not_record(wired, monkeypatch):
    """Nothing was summarised; and `commits` is empty AFTER noise filtering, so
    consuming them would lose them if the user later loosened `noise`."""
    recorded = _watermark(monkeypatch)
    _since_spy(monkeypatch, commits=[])
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert recorded == []


@pytest.mark.parametrize("argv", [
    ["voicelog", "--pull", "--no-speak"],
    ["voicelog", "--since", "HEAD~3", "--no-speak"],
    ["voicelog", "--pr", "main", "--no-speak"],
])
def test_one_off_ranges_do_not_record(wired, monkeypatch, argv):
    """Commits between the watermark and that range were never shown."""
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr("sys.argv", argv)

    cli.main()

    assert recorded == []


def test_a_failed_changelog_write_does_not_record(wired, monkeypatch):
    """With --changelog the release changelog IS the artifact; advancing past
    commits that never landed in it would drop them from the notes for good."""
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr(cli.voicefile, "update_voice_md",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr("sys.argv", ["voicelog", "--changelog", "--no-speak"])

    cli.main()

    assert recorded == []


def test_no_head_sha_is_not_an_error(wired, monkeypatch):
    """An empty repo has no HEAD; there is simply nothing to record."""
    recorded = []
    monkeypatch.setattr(cli.gitsource, "head_sha", lambda: None)
    monkeypatch.setattr(cli.state, "record_summarised",
                        lambda sha: recorded.append(sha) or True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert recorded == []


def test_the_commit_cap_warns_that_older_commits_will_be_skipped(wired, monkeypatch, capsys):
    """Capping at max_commits then advancing to HEAD silently drops the rest.
    There is no way to express "all but the newest 50" as a range, so say so."""
    _watermark(monkeypatch)
    many = [Commit(f"h{i}", f"feat: thing {i}", "", "Al", []) for i in range(120)]
    _since_spy(monkeypatch, commits=many)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    err = capsys.readouterr().err
    assert "120 commits" in err
    assert "--since-last" in err


# ---------------------------------------------------------------------------
# --since-last
# ---------------------------------------------------------------------------

def test_since_last_uses_the_stored_sha(wired, monkeypatch, capsys):
    seen = _since_spy(monkeypatch)
    _watermark(monkeypatch)
    monkeypatch.setattr(cli.state, "last_summarised_sha", lambda: "b" * 40)
    monkeypatch.setattr(cli.gitsource, "is_ancestor", lambda sha, *a: True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--no-speak"])

    cli.main()

    assert seen["since"] == "b" * 40
    assert "bbbbbbbb" in capsys.readouterr().err  # short form, not 40 chars


def test_since_last_with_no_stored_sha_uses_the_default_range(wired, monkeypatch, capsys):
    seen = _since_spy(monkeypatch)
    _watermark(monkeypatch)
    monkeypatch.setattr(cli.state, "last_summarised_sha", lambda: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--no-speak"])

    cli.main()

    assert seen["since"] is None
    assert "no record" in capsys.readouterr().err.lower()


def test_a_stale_sha_warns_and_does_not_end_the_run(wired, monkeypatch, capsys):
    """After a rebase or reset the sha no longer resolves, which used to reach
    read_commits and exit 1 with "unknown git ref"."""
    seen = _since_spy(monkeypatch)
    _watermark(monkeypatch)
    monkeypatch.setattr(cli.state, "last_summarised_sha", lambda: "c" * 40)
    monkeypatch.setattr(cli.gitsource, "is_ancestor", lambda sha, *a: False)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--no-speak"])

    cli.main()  # must not raise SystemExit

    assert seen["since"] is None
    err = capsys.readouterr().err.lower()
    assert "history" in err or "rebase" in err


def test_since_last_records_the_watermark(wired, monkeypatch):
    recorded = _watermark(monkeypatch)
    _since_spy(monkeypatch)
    monkeypatch.setattr(cli.state, "last_summarised_sha", lambda: "b" * 40)
    monkeypatch.setattr(cli.gitsource, "is_ancestor", lambda sha, *a: True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--no-speak"])

    cli.main()

    assert recorded == ["a" * 40]


def test_since_last_never_writes_the_release_changelog(wired, monkeypatch, tmp_path):
    """Even on its first run, where it falls back to the default range: a
    partial range must not reach voicefile, which REPLACES the Unreleased
    block."""
    _watermark(monkeypatch)
    monkeypatch.setattr(cli.state, "last_summarised_sha", lambda: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--changelog", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()


def test_since_last_is_mutually_exclusive_with_other_ranges(wired, monkeypatch):
    monkeypatch.setattr("sys.argv", ["voicelog", "--since-last", "--pull"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_an_unwritable_cache_still_prints_the_changelog(wired, monkeypatch, capsys):
    """cache.put runs after a paid model call and before the changelog prints."""
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    # Patch the underlying failure, not _write itself - patching _write would
    # bypass the guard this test exists to check.
    monkeypatch.setattr(cli.cache.os, "makedirs", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()          # must not raise

    out = capsys.readouterr()
    assert "## Unreleased" in out.out
    assert "cache" in out.err.lower()


# ---------------------------------------------------------------------------
# --replay: get back the run whose audio you missed
# ---------------------------------------------------------------------------

def _cached(tmp_path, markdown="## Unreleased\n\n- a cached thing", brief=None, detailed=None):
    """Seed the cache the way a previous run would have left it."""
    cli.cache.put(str(tmp_path), "old-key", markdown)
    if brief is not None:
        cli.cache.put_summary(str(tmp_path), "old-key", False, brief)
    if detailed is not None:
        cli.cache.put_summary(str(tmp_path), "old-key", True, detailed)


def _no_model_calls(monkeypatch):
    """Any model call at all is a failure for a replay."""
    def _boom(*args, **kwargs):
        raise AssertionError("a replay must make no model call")

    monkeypatch.setattr(cli.generate, "generate", _boom)
    monkeypatch.setattr(cli.generate, "summarize", _boom)


def test_replay_reprints_the_cached_changelog(wired, monkeypatch, tmp_path, capsys):
    _cached(tmp_path)
    _no_model_calls(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()

    assert "a cached thing" in capsys.readouterr().out


def test_replay_makes_no_model_call(wired, monkeypatch, tmp_path):
    """The load-bearing test: the whole point is that it costs nothing."""
    _cached(tmp_path, brief="the spoken words")
    _no_model_calls(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()          # _no_model_calls raises if either is touched


def test_replay_reads_no_commits(wired, monkeypatch, tmp_path):
    _cached(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("a replay must not read commits")

    monkeypatch.setattr(cli.gitsource, "read_commits", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()


def test_replay_does_not_advance_the_watermark(wired, monkeypatch, tmp_path):
    """It re-shows what you already saw, so there is nothing new to record."""
    _cached(tmp_path)

    def _boom(sha):
        raise AssertionError("a replay must not touch the watermark")

    monkeypatch.setattr(cli.state, "record_summarised", _boom)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()


def test_replay_speaks_the_cached_summary_verbatim(wired, monkeypatch, tmp_path):
    _cached(tmp_path, brief="exactly these spoken words")
    _no_model_calls(monkeypatch)
    spoken = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoken.append(text))
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()

    assert spoken == ["exactly these spoken words"]


def test_replay_with_no_cache_says_so_and_exits_cleanly(wired, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()          # no SystemExit: never breaks a caller

    assert "nothing to replay" in capsys.readouterr().out.lower()


def test_replay_without_a_cached_summary_prints_and_explains(wired, monkeypatch, tmp_path, capsys):
    """The --no-speak case: there is text to re-print but nothing to say."""
    _cached(tmp_path)                      # no summaries at all
    _no_model_calls(monkeypatch)
    spoke = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.append(text))
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()

    out = capsys.readouterr()
    assert "a cached thing" in out.out
    assert spoke == []
    assert "no cached spoken summary" in out.err.lower()


def test_replay_falls_back_to_the_other_detail_level(wired, monkeypatch, tmp_path, capsys):
    """Refusing would mean a model call the flag promises not to make."""
    _cached(tmp_path, brief="the brief one")
    _no_model_calls(monkeypatch)
    spoken = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoken.append(text))
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--detail"])

    cli.main()

    assert spoken == ["the brief one"]
    assert "brief" in capsys.readouterr().err.lower()


def test_replay_prefers_the_requested_detail_level(wired, monkeypatch, tmp_path):
    _cached(tmp_path, brief="the brief one", detailed="the detailed one")
    _no_model_calls(monkeypatch)
    spoken = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoken.append(text))
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--detail"])

    cli.main()

    assert spoken == ["the detailed one"]


def test_replay_with_no_speak_prints_only_and_stays_quiet(wired, monkeypatch, tmp_path, capsys):
    _cached(tmp_path)                      # no summary, but --no-speak was asked for
    spoke = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.append(text))
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()

    out = capsys.readouterr()
    assert "a cached thing" in out.out
    assert spoke == []
    assert "no cached spoken summary" not in out.err.lower()


def test_replay_survives_a_speech_failure(wired, monkeypatch, tmp_path, capsys):
    _cached(tmp_path, brief="words")
    _no_model_calls(monkeypatch)
    monkeypatch.setattr(
        cli.tts, "speak",
        lambda text, config: (_ for _ in ()).throw(TTSError("no device")),
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()

    out = capsys.readouterr()
    assert "a cached thing" in out.out
    assert "could not speak" in out.err


def test_replay_never_writes_the_release_changelog(wired, monkeypatch, tmp_path, capsys):
    """There is no tag to pass, and update_voice_md REPLACES the Unreleased
    block - so replaying a --pull entry would overwrite a real changelog."""
    _cached(tmp_path)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--changelog", "--no-speak"])

    cli.main()

    assert not (tmp_path / ".changelog" / "voice.md").exists()
    assert "--changelog" in capsys.readouterr().err


def test_replay_does_not_prompt_for_the_text_provider_key(wired, monkeypatch, tmp_path):
    """It makes no model call, so asking for that secret would be asking for a
    key to run a command that cannot use it."""
    _cached(tmp_path)
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()

    assert "NVIDIA_API_KEY" not in asked


def test_replay_and_fresh_are_rejected(wired, monkeypatch, capsys):
    """"read only the cache" and "ignore the cache" cannot both be honoured."""
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--fresh"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "--fresh" in capsys.readouterr().err


def test_replay_is_mutually_exclusive_with_a_range(wired, monkeypatch):
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--since-last"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_replay_never_opens_the_first_run_wizard(monkeypatch, tmp_path):
    """A fresh machine has no cache, so the only outcome is the miss message -
    running a questionnaire to reach it would be a poor trade."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    cfg = _config()
    cfg.model = ""

    assert cli._needs_setup(cfg, _args(replay=True)) is False


# ---------------------------------------------------------------------------
# A bad config VALUE is not a bad config FILE
# ---------------------------------------------------------------------------

def test_a_bad_value_exits_one_and_does_not_claim_the_file_is_unreadable(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("max_commits: many\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "max_commits" in err
    assert "could not read config" not in err   # it read fine; one line is wrong
    assert "could not open config" not in err


def test_setup_cannot_repair_a_bad_value_in_a_project_config(monkeypatch, tmp_path, capsys):
    """The wizard writes the USER config and never a project's changelog.yml,
    so it cannot fix this line - and saying it will would be a lie."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changelog.yml").write_text("max_commits: many\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    ran = {"wizard": False}
    monkeypatch.setattr(
        cli.wizard, "run_setup",
        lambda current, dest: ran.__setitem__("wizard", True) or {"model": "x"},
    )
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert ran["wizard"] is False
    err = capsys.readouterr().err
    assert "max_commits" in err
    assert "fresh config" not in err


def test_setup_repairs_a_bad_value_in_the_user_config(monkeypatch, tmp_path, capsys):
    """Now that saving drops what it cannot use, --setup IS a repair here - so
    refusing would be needlessly unhelpful."""
    monkeypatch.chdir(tmp_path)
    path = cli.config_module.user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("max_commits: many\n")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.wizard, "run_setup", lambda current, dest: {"model": "picked/one"})
    monkeypatch.setattr("sys.argv", ["voicelog", "--setup"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    stored = cli.config_module.read_user_config()
    assert stored["model"] == "picked/one"
    assert "max_commits" not in stored      # the bad line is gone, not preserved


# ---------------------------------------------------------------------------
# Replay must be able to ask for the key it needs
# ---------------------------------------------------------------------------

def test_replay_offers_the_speech_key_even_when_it_is_the_shared_one(
    wired, monkeypatch, tmp_path
):
    """Replay skips the text-key prompt by design, and the speech prompt only
    fired when the two env vars differed - which they do not in the default
    config. So a replay in a fresh terminal could only warn "could not speak"."""
    _cached(tmp_path, brief="words")
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay"])

    cli.main()

    assert asked == ["NVIDIA_API_KEY"]


def test_replay_does_not_ask_for_a_key_it_will_not_use(wired, monkeypatch, tmp_path):
    """--no-speak means nothing to play, so nothing to unlock."""
    _cached(tmp_path, brief="words")
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr("sys.argv", ["voicelog", "--replay", "--no-speak"])

    cli.main()

    assert asked == []


def test_the_normal_run_still_asks_once_for_a_shared_key(wired, monkeypatch):
    """The guard existed to avoid asking twice for the same variable."""
    asked = []
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: asked.append(env) or True)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)

    cli.main()

    assert asked == ["NVIDIA_API_KEY"]


# ---------------------------------------------------------------------------
# Asking for a voice means you want to hear it
# ---------------------------------------------------------------------------

def test_choosing_a_voice_backend_turns_speech_on_for_that_run(wired, monkeypatch):
    """With speak: false in config, --tts-provider was a silent no-op: nothing
    played and nothing said why."""
    seen = _capture_cfg(monkeypatch)
    spoke = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.append(text))
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    _load_config(monkeypatch, speak=False)
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "openai"])

    cli.main()

    assert seen["cfg"].speak is True
    assert spoke


def test_choosing_a_voice_turns_speech_on_for_that_run(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr(cli.wizard, "ensure_key", lambda env, **kw: True)
    _load_config(monkeypatch, speak=False)
    monkeypatch.setattr("sys.argv", ["voicelog", "--voice", "nova"])

    cli.main()

    assert seen["cfg"].speak is True


def test_no_speak_still_beats_a_voice_flag(wired, monkeypatch):
    spoke = []
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: spoke.append(text))
    _load_config(monkeypatch, speak=False)
    monkeypatch.setattr("sys.argv", ["voicelog", "--voice", "nova", "--no-speak"])

    cli.main()

    assert spoke == []


def test_tts_provider_none_still_turns_speech_off(wired, monkeypatch):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setattr(cli.tts, "speak", lambda text, config: None)
    monkeypatch.setattr("sys.argv", ["voicelog", "--tts-provider", "none"])

    cli.main()

    assert seen["cfg"].speak is False


# ---------------------------------------------------------------------------
# The watermark must not advance past a generation nothing kept
# ---------------------------------------------------------------------------

def test_a_failed_cache_write_does_not_record(wired, monkeypatch, capsys):
    """Advancing here loses the generation from both directions: --replay has
    nothing cached and --since-last sees nothing new, so the only copy of a paid
    summary is terminal scrollback. Re-generating next run is the lesser evil.

    Reachable because state.record_summarised and the cache write can fail
    independently: if the whole .git/voicelog directory is unwritable the
    watermark write fails too, but a read-only cache.json alone leaves it fine.
    """
    recorded = _watermark(monkeypatch)
    # Patch the underlying failure, not _write, so the real guard runs.
    monkeypatch.setattr(cli.cache.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert recorded == []
    out = capsys.readouterr()
    assert "## Unreleased" in out.out     # the text still printed
    assert "cache" in out.err.lower()


def test_a_successful_cache_write_still_records(wired, monkeypatch):
    """The gate must not freeze the watermark on the ordinary path."""
    recorded = _watermark(monkeypatch)
    monkeypatch.setattr("sys.argv", ["voicelog", "--no-speak"])

    cli.main()

    assert recorded == ["a" * 40]
