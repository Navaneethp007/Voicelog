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

--setup (and the first run on an unconfigured machine) takes a third path:
cli → wizard → providers (GET /v1/models) → config.save_user_config. It touches
none of the pipeline above; on a first run it then hands control back so the
command the user actually typed still completes.
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
| `redact` | keeps API keys out of error text; no imports, so every module can use it |
| `textstyle` | markdown → terminal styling, and the tty/NO_COLOR decision. Identity when styling is off |
| `state` | per-repo local state (the run watermark) in `.git/voicelog/`, plus the resolver `cache` uses |
| `generate` | orchestrate prompt + llm (`generate`, `summarize`, `onboard`) |
| `render` | clean model output → final markdown (changelog and onboarding variants) |
| `cache` | skip regeneration when the commit set is unchanged |
| `voicefile` | maintain the persistent `voice.md` (only with `--changelog`) |
| `tts` | **provider-aware** — dispatches to riva (gRPC) / openai / elevenlabs (httpx) |
| `providers` | **provider-aware** — the preset registry, `GET /v1/models` discovery, and a 1-token model check. No interactive IO, no `Config`, no per-provider branching |
| `wizard` | the only interactive module — prompt helpers, the one `can_prompt()` gate, the `--setup` / first-run flow, and the opt-in `pip install` of the Riva extra |
| `cli` | wire it together; own the flags and the error table |

`Commit.insertions`/`Commit.deletions` (diffstat, from `git log --numstat`) are
always available and included in prompts — no code ever sent. The actual code
diff is a separate, opt-in field (`GitResult.diff`, populated when
`with_diff=True`) threaded through `generate`/`prompt` only when the user
passes `--with-diff`; `cli.py` prints a privacy warning whenever it does.

The only provider-aware modules are `llm`, `tts` and `providers`, and `wizard`
is the only interactive one. `providers` holds the presets and the discovery
call; `llm`/`tts` hold the request shaping. Swapping the text model or
speech engine is a config change, never a code change. Adding an LLM provider is
one `PROVIDERS` entry and no code. `tts` picks its adapter
via `config.tts_provider`; each adapter takes chunked text + an API key and
returns `(pcm_bytes, sample_rate)` — the shared code stitches one WAV and plays
it cross-platform.

**One provider table.** Every fact about a backend — endpoint, key env var,
voices, default model, sample rate — lives in `providers.PROVIDERS` /
`TTS_PROVIDERS` and nowhere else. `config.DEFAULTS` reads NVIDIA's and Riva's
values from those presets rather than restating them, and `wizard._LLM_TO_TTS`
is derived by matching key variables. Adding a speech backend is one
`TTS_PROVIDERS` entry plus one `_synth_<name>` in `_ADAPTERS`; a test asserts
`set(_ADAPTERS) | {NO_TTS_KEY} == set(TTS_PROVIDERS)`, so the two cannot drift.
The adapter code stays in `tts` deliberately: putting a `synth` callable on the
preset would make `providers` import `tts` while `tts` imports `providers`.

## Conventions (please follow these)

**1. Test-driven.** Write the failing test first, watch it fail, then implement.
Tests live in `tests/test_<module>.py`. Pure stages get plain unit tests;
`gitsource` tests build real throwaway git repos in `tmp_path`; `llm`/`tts`/
`providers` tests **mock** the network/audio and assert the request we build, not
model output (`mock.patch("voicelog.<module>.httpx.<verb>")`). `wizard` tests
script `builtins.input`.

`tests/conftest.py` holds three autouse safety nets: it redirects the user config
directory (`VOICELOG_CONFIG_HOME`) so no test can read or clobber your real one,
sets `VOICELOG_NO_SETUP` so the wizard can never fire, and fails any unmocked
HTTP call. Do not work around them — an unmocked request would otherwise hit a
real provider with your real key.

**2. Adding a config field.** Add it in four places in `voicelog/config.py`:
`DEFAULTS`, the `Config` dataclass, `_build()`, and the validation group tuple it
belongs to (`_STR_KEYS`, `_POSITIVE_INT_KEYS`, `_URL_KEYS`, …). A key in no group is
accepted unvalidated, which is how `provider` used to take any string at all. Give **new** dataclass fields
a default value (e.g. `foo: int = 5`) so existing `Config(...)` call sites and
test fixtures keep working. Document it in `changelog.yml`.

Then decide *where the field belongs*. A personal choice (provider, model, key
env var, voice) should be written by `--setup`, which means adding it to
`config._USER_KEY_ORDER`. A per-project setting (sections, noise, paths) stays
documented in `changelog.yml` and is never written by the wizard.

**Never ship a default that names a third-party resource which can be retired.**
A model id, an NVCF function id, a hosted voice name: each one is a value that
works the day you write it and silently breaks later. `DEFAULTS["model"]` is `""`
for exactly this reason — the user picks from the provider's live catalogue.

**3. Never block on side-effects.** Text output always comes first and is never
gated on audio. Audio (`tts`) and the LLM degrade gracefully — a failure warns to
stderr and the run still produces useful output (see the error handling in
`cli.main`). A missing or retired model is reported with the fix (`voicelog
--setup`) but still exits 0 with the commit-list fallback, so nobody's pre-push
hook or CI job starts failing. Preserve this when you touch the pipeline.

**One config boundary: flags are a layer, not a second pass.** `config.load` merges
`DEFAULTS < user config < ./changelog.yml < flags`, and `cli._flag_overrides` only turns
`args` into a dict. Validation, canonicalisation, URL normalisation and preset
reconciliation all happen once, inside `load`, so a setting means the same thing however
it arrived. Flags used to be applied *after* load by `_apply_overrides`, which therefore
had to re-implement all of that — and the file path simply went without it. Roughly a
third of one review's findings reduced to "the flag path handles this, the file path
doesn't": `provider: openai` in a file kept NVIDIA's endpoint and key var while every
error said "openai".

**A provider name seeds its preset only when it changes something.** The rule is
per-layer: when a layer sets `provider` to something other than what the layers beneath
it resolved to, every preset-owned key that layer does not set itself comes from the
preset. Firing only on a *change* is what lets the wizard write a hand-typed `base_url`
next to `provider: nvidia` and keep it — and it matters because the flag layer is applied
twice, once on the main path and again on the config the wizard just wrote.
`_validate_flags` now holds only what no validator downstream can see (mutually exclusive
flags, and `--provider custom` needing `--base-url`); a value that is merely wrong is
config's business. A flag-origin failure exits 2, a file-origin one exits 1.

**A bad config *value* is not a bad config *file*.** `ConfigValueInvalid` is separate from
`ConfigFileInvalid` because the repair is opposite: a parse error has nothing worth keeping, so
`--setup` may overwrite the file, while a value error is one bad line in a file that is otherwise
correct — `save_user_config` merges, so rewriting *preserves* the bad line, and it never writes a
project's `changelog.yml` at all. Validate in `config._validated`, one row per key; type-check the
string keys rather than coercing them, so `model: 1.5` names the config line instead of earning a 404.

**Negotiate parameters with the provider; never ship a list of model ids.** Reasoning models
reject `max_tokens` (they want `max_completion_tokens`) and any non-default `temperature`. On a 400,
`providers.adapt_payload` reads the provider's own `error.param`/`error.code` and retries once with
the parameter renamed or dropped — bounded by `MAX_PARAM_ADAPTATIONS`, and only for parameters we
actually sent. A hardcoded "these models are special" list is the same rot that removing the default
model was meant to end. `verify_model` uses the same helper, so the check can never condemn a model
generation is able to drive.

**A cache write must never cost a generation.** `cache._write` runs after a paid model call and
before the changelog is printed, so it is atomic and never raises — it warns and returns False.
Apply the same rule to anything else that writes after the model has been called.

**`cache.last` is the only key-agnostic reader, and it is for `--replay` alone.** Every other reader
compares the key because the key *is* the answer to "may I skip generation". Never use `last` for
that: it will hand you output for a different commit set without complaint.

**Anything written into a shell profile goes through `shlex.quote`.** The export line was once
double-quoted, which meant a pasted key containing a backtick was executed at every shell startup.

**Styling happens at the print boundary only** (`cli._emit`). The rendered markdown is one
string shared with `voicefile.update_voice_md` — which matches `^## Unreleased`, splits blocks on
`^## ` and byte-compares against the file — and with the spoken-summary prompt. An escape sequence
reaching either breaks block replacement or gets read aloud. `textstyle.style` is the identity
function when styling is off, which is what keeps piped output byte-identical markdown.

**Local state goes in `.git/voicelog/`, never the work tree.** That is how "gitignored by default"
is achieved without writing a `.gitignore` into someone's project, and it is the same file from any
subdirectory. Use `state.private_path(name, repo_dir)` rather than building a path from the working
directory.

**Redact before truncating.** Cutting a body to length first can slice a key in half,
leaving a prefix that `replace()` can no longer match. `redact(text, key)[:N]`, never
`redact(text[:N], key)`.

**No prompt may ever escape `wizard.can_prompt()`** — the wizard's questions and
the API-key prompt alike. It requires stdin *and* stdout to be a terminal and no
CI/git-hook markers in the environment. Checking stdin alone is not enough: hook
runners hand you a terminal stdin with a piped stdout, and on Windows the NUL
device reports `isatty()` as true.

**4. Match the surrounding style.** Small focused functions, typed signatures,
docstrings that say *what* and *why*. No new heavy dependencies without discussion
(the `tts` extra is deliberately isolated so the core stays light).

## Running the checks

```bash
pytest -q            # full suite (fast; network/audio are mocked)
pytest -q -s         # capture off, so isatty() is true: MUST NOT hang or prompt
voicelog --help      # sanity-check the CLI surface
```

The `-s` run is not redundant. Disabling capture makes the streams look
interactive, which is the one condition under which a badly-gated prompt blocks
forever — it is how the `can_prompt()` gate above was found to be missing from
the key prompt.

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
