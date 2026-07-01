"""Tests for voicelog/config.py — written BEFORE any implementation (TDD RED phase)."""
from __future__ import annotations

import os
import textwrap

import pytest

from voicelog.config import Config, ConfigFileNotFound, DEFAULTS, load


# ---------------------------------------------------------------------------
# 1. load(None) with no changelog.yml present → returns defaults
# ---------------------------------------------------------------------------

def test_load_none_no_file_returns_defaults(tmp_path, monkeypatch):
    """When path=None and no changelog.yml exists in cwd, silently use DEFAULTS."""
    monkeypatch.chdir(tmp_path)  # empty dir — no changelog.yml
    cfg = load(None)
    assert isinstance(cfg, Config)
    assert cfg.provider == DEFAULTS["provider"]
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.model == DEFAULTS["model"]
    assert cfg.sections == DEFAULTS["sections"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]
    assert cfg.fallback_commits == DEFAULTS["fallback_commits"]


# ---------------------------------------------------------------------------
# 2. load(path) with a real YAML file → file values override defaults,
#    absent keys fall back to defaults
# ---------------------------------------------------------------------------

def test_load_path_with_partial_yaml_merges_over_defaults(tmp_path):
    """File values win; keys absent from the file fall back to DEFAULTS."""
    config_file = tmp_path / "my_changelog.yml"
    config_file.write_text(
        textwrap.dedent("""\
            provider: openai
            model: gpt-4o
        """),
        encoding="utf-8",
    )

    cfg = load(str(config_file))

    # overridden by file
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"

    # not in file → fall back to defaults
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.sections == DEFAULTS["sections"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]
    assert cfg.fallback_commits == DEFAULTS["fallback_commits"]


# ---------------------------------------------------------------------------
# 3. load("missing.yml") → raises ConfigFileNotFound
# ---------------------------------------------------------------------------

def test_load_explicit_missing_path_raises(tmp_path):
    """Passing a path that does not exist must raise ConfigFileNotFound."""
    missing = str(tmp_path / "does_not_exist.yml")
    with pytest.raises(ConfigFileNotFound):
        load(missing)


# ---------------------------------------------------------------------------
# 4. YAML file that sets sections and fallback_commits → those land in Config,
#    others stay default
# ---------------------------------------------------------------------------

def test_load_yaml_sections_and_fallback_commits(tmp_path):
    """sections list and fallback_commits integer are correctly parsed and stored."""
    config_file = tmp_path / "changelog.yml"
    config_file.write_text(
        textwrap.dedent("""\
            sections:
              - Breaking
              - Features
              - Deprecations
            fallback_commits: 100
        """),
        encoding="utf-8",
    )

    cfg = load(str(config_file))

    assert cfg.sections == ["Breaking", "Features", "Deprecations"]
    assert cfg.fallback_commits == 100

    # keys not mentioned → still defaults
    assert cfg.provider == DEFAULTS["provider"]
    assert cfg.base_url == DEFAULTS["base_url"]
    assert cfg.model == DEFAULTS["model"]
    assert cfg.noise == DEFAULTS["noise"]
    assert cfg.voice_samples == DEFAULTS["voice_samples"]
