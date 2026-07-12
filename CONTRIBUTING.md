# Contributing to voicelog

Thanks for helping out! voicelog is a small, well-tested CLI. This guide covers
how to set up, the conventions that keep it clean, and how to ship a change.

## Dev setup

```bash
git clone https://github.com/Navaneethp007/voicelog.git
cd voicelog
pip install -e ".[tts,dev]"   # editable install + speech + pytest
pytest                        # should be all green
```

Requires Python 3.10+. The editable install means your changes take effect
immediately — no reinstall between edits.

## How the project is shaped

voicelog is a **pure data pipeline** — each stage takes data and returns data,
with no shared mutable state:

```
gitsource → filters → (voice) → prompt → llm → generate → render → cli ─┬─ stdout
                                                                        ├─ tts (speak)
                                                                        ├─ cache (.voicelog-cache.json)
                                                                        └─ voicefile (voice.md, opt-in)

--new takes a parallel path: readme + gitsource.read_recent_commits (ignores
tags) → prompt.build_onboarding_prompt → generate.onboard → render.render_onboarding
→ cli. Always transient — never cached, never writes voice.md.
```

Each module has one job and is testable in isolation:

| Module | Job |
|--------|-----|
| `gitsource` | git → `list[Commit]` for a range (since tag, `--since`, `--pull`, `--pr`, or `read_recent_commits` for `--new`) |
| `filters` | drop noise commits by regex |
| `voice` | load few-shot voice samples (optional) |
| `readme` | load a repo's README for `--new` (optional) |
| `prompt` | build the LLM messages (changelog, spoken summary, onboarding) |
| `llm` | **provider-aware** — one OpenAI-compatible HTTP call + retry |
| `generate` | orchestrate prompt + llm (`generate`, `summarize`, `onboard`) |
| `render` | clean model output → final markdown (changelog and onboarding variants) |
| `cache` | skip regeneration when the commit set is unchanged |
| `voicefile` | maintain the persistent `voice.md` (only with `--changelog`) |
| `tts` | **provider-aware** — dispatches to riva (gRPC) / openai / elevenlabs (httpx) |
| `cli` | wire it together; own the flags and the error table |

`Commit.insertions`/`Commit.deletions` (diffstat, from `git log --numstat`) are
always available and included in prompts — no code ever sent. The actual code
diff is a separate, opt-in field (`GitResult.diff`, populated when
`with_diff=True`) threaded through `generate`/`prompt` only when the user
passes `--with-diff`; `cli.py` prints a privacy warning whenever it does.

The only provider-aware modules are `llm` and `tts`. Swapping the text model or
speech engine is a config change, never a code change. `tts` picks its adapter
via `config.tts_provider`; each adapter takes chunked text + an API key and
returns `(pcm_bytes, sample_rate)` — the shared code stitches one WAV and plays
it cross-platform. Add a new speech provider by writing one `_synth_<name>`
function and registering it in `_ADAPTERS`.

## Conventions (please follow these)

**1. Test-driven.** Write the failing test first, watch it fail, then implement.
Tests live in `tests/test_<module>.py`. Pure stages get plain unit tests;
`gitsource` tests build real throwaway git repos in `tmp_path`; `llm`/`tts` tests
**mock** the network/audio and assert the request we build, not model output.

**2. Adding a config field.** Add it in three places in `voicelog/config.py`:
`DEFAULTS`, the `Config` dataclass, and `_build()`. Give **new** dataclass fields
a default value (e.g. `foo: int = 5`) so existing `Config(...)` call sites and
test fixtures keep working. Document it in `changelog.yml`.

**3. Never block on side-effects.** Text output always comes first and is never
gated on audio. Audio (`tts`) and the LLM degrade gracefully — a failure warns to
stderr and the run still produces useful output (see the error handling in
`cli.main`). Preserve this when you touch the pipeline.

**4. Match the surrounding style.** Small focused functions, typed signatures,
docstrings that say *what* and *why*. No new heavy dependencies without discussion
(the `tts` extra is deliberately isolated so the core stays light).

## Running the checks

```bash
pytest -q            # full suite (fast; network/audio are mocked)
voicelog --help      # sanity-check the CLI surface
```

There are also two throwaway smoke scripts you may create locally for manual
checks against real services (a one-shot LLM call, a one-sentence TTS play) —
keep those out of commits.

## Submitting a change

1. Branch off `main`.
2. Make the change **with tests**; keep the suite green.
3. Try it on your branch: `voicelog --pr` gives you a spoken/written rundown of
   what your PR contains — handy self-review.
4. Open a PR with a clear description of the behavior change.

## Releasing (maintainers)

PyPI versions are immutable, so every release is a version bump:

1. Bump `version` in `pyproject.toml` (semantic: patch for fixes, minor for
   features).
2. `python -m build`
3. `python -m twine check dist/*`
4. `python -m twine upload dist/*`  (username `__token__`, password = PyPI token)
5. `git tag vX.Y.Z && git push --tags`

`dist/` is gitignored — never commit built artifacts.
