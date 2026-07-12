# voicelog

**The changelog that talks to you.** At any git moment — after a `git pull`, before opening a PR, or just to catch up — voicelog gives you a short spoken + written rundown of what changed, using a free LLM (NVIDIA NIM by default). Run `voicelog`, and it prints a casual changelog and **reads a summary aloud**.

## Install

> 📦 **PyPI release coming soon** — `pip install voicelog` isn't live yet. For now, install from source:

```bash
git clone https://github.com/Navaneethp007/voicelog.git
cd voicelog

pip install -e ".[tts]"   # core + speech (NVIDIA Riva TTS)
# or, text only (works on any OS, no audio):
pip install -e .
```

Requires Python 3.10+. Speech playback currently uses Windows audio (`winsound`); on macOS/Linux the text rundown works and the spoken step is skipped with a warning.

Verify it's installed:

```bash
voicelog --help
```

## Setup — one API key

voicelog needs a free API key from [build.nvidia.com](https://build.nvidia.com) (no credit card). You can either let voicelog ask you on first run, or set it yourself.

**Easiest:** just run `voicelog` in a repo — if no key is set, it prompts you to paste one and uses it for that session.

**Or set it manually** (the same key powers text and speech):

```bash
export NVIDIA_API_KEY=nvapi-...     # bash / macOS / Linux
set NVIDIA_API_KEY=nvapi-...        # Windows cmd (this session)
$env:NVIDIA_API_KEY = "nvapi-..."   # PowerShell (this session)
```

To persist it on Windows so every terminal has it: `setx NVIDIA_API_KEY "nvapi-..."` (then open a new terminal).

## Usage

```bash
# From inside your git repo: prints the changelog AND speaks it
voicelog

# Print only, no audio
voicelog --no-speak

# Ignore the cache and regenerate fresh prose from the model
voicelog --fresh

# Use an alternate config file
voicelog --config path/to/changelog.yml
```

### Catch up on a `git pull`

Just pulled a branch and want to know what landed? `--pull` summarizes exactly the commits the pull brought in (everything since `ORIG_HEAD`) — text + voice:

```bash
git pull
voicelog --pull          # "here's what you just pulled", spoken + printed
```

### Preview a PR before you open it

On a feature branch, `--pr` summarizes what your branch would put in a pull request — the commits since the base branch. The base is auto-detected (`origin/HEAD`, falling back to `main`/`master`), or you can name it:

```bash
voicelog --pr           # against the auto-detected base
voicelog --pr develop   # against a base branch you choose
```

### Diff against any ref

Or compare against any ref with `--since`:

```bash
voicelog --since main              # commits since main
voicelog --since v1.2.0            # commits since a tag
voicelog --since HEAD~10           # last 10 commits
```

These are transient "what changed" views — they print and speak, but do **not** touch the persistent `.changelog/voice.md` (that's reserved for releases).

### More (or less) spoken detail

By default the spoken summary is brief (2-3 sentences). For a fuller walkthrough of each change, add `--detail`:

```bash
voicelog --pull --detail
```

Set `speech_detail: detailed` in `changelog.yml` to make it the default. (The printed text is always the full changelog — this only changes the *spoken* part.)

### Caching

The generated changelog is cached on the **set of commits** (in `.changelog/.voicelog-cache.json`). Re-running with the same commits returns the cached text instead of calling the model again — so repeat runs are free, fast, and don't churn `voice.md`. Add a new commit (or pass `--fresh`) to regenerate. You'll usually want to git-ignore the cache file.

By default, each run is a **transient rundown** — it prints the casual changelog and reads a short spoken summary aloud, but doesn't write anything. That's the everyday use: quick "what changed" at a git moment.

Each run:
1. Prints the full casual changelog (under `## Unreleased`) to stdout.
2. Reads a **short spoken summary** aloud via NVIDIA Riva TTS (2-3 sentences — not the whole changelog, so audio stays quick even on huge repos).

Add `--changelog` (or set `write_changelog: true`) to *also* maintain a persistent release changelog at `.changelog/voice.md` that accumulates across releases (with automatic promotion when you tag). That's opt-in — most runs don't need it.

### Working on large repos

For big histories (e.g. a fork with hundreds of commits since the last tag), voicelog caps how many commits it sends to the model — the most recent `max_commits` (default 50) — so generation stays fast and cheap. The spoken part is always a short summary, so it never gets stuck reading a giant changelog aloud. Tune `max_commits` in `changelog.yml`, or use `--no-speak` for instant text-only output.

## Persistent changelog (`.changelog/voice.md`)

voicelog maintains this file for you:
- Keeps the current `## Unreleased` block at the top, refreshed each run (idempotent — running twice with no new commits changes nothing).
- When you `git tag` a release, the previous `## Unreleased` is **promoted** to `## <tag>` and a fresh `## Unreleased` starts above it. Your history accumulates; nothing is lost.

## Using your own LLM

voicelog talks to any **OpenAI-compatible** endpoint. Set three things in `changelog.yml` — `base_url`, `model`, and `api_key_env` (the name of the env var your key lives in) — then export that key. The key is never stored in the file.

| Provider | base_url | model | api_key_env |
|----------|----------|-------|-------------|
| NVIDIA (default) | `https://integrate.api.nvidia.com/v1` | `mistralai/mistral-medium-3.5-128b` | `NVIDIA_API_KEY` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| Groq (fast, free tier) | `https://api.groq.com/openai/v1` | `llama-3.1-8b-instant` | `GROQ_API_KEY` |
| Ollama (local, no key) | `http://localhost:11434/v1` | `llama3.1` | any name |

**Speech is NVIDIA Riva only**, so the spoken step always needs an NVIDIA key (`tts_api_key_env`), even if your text model is OpenAI or Groq. No NVIDIA key? Set `speak: false` — the text changelog still works everywhere.

## Configuration

Copy `changelog.yml` and edit it — all fields are optional, built-in defaults work out of the box. Key fields:

| Field | Purpose |
|-------|---------|
| `model` | Any model on build.nvidia.com (default mistral-medium) |
| `sections` | Section grouping for the changelog |
| `noise` | Regex commit subjects to drop (e.g. `^wip`) |
| `speak` | `true`/`false` — read aloud (also `--no-speak` per run) |
| `tts_voice` / `tts_language` | Riva voice + language |
| `voice_md` | Path to the persistent changelog |

The only secret is `NVIDIA_API_KEY` — read from the environment, never stored in config.

## Graceful degradation

voicelog never blocks on audio. Text always prints first; if anything downstream fails it only warns to stderr:
- **LLM unavailable** → prints a plain bulleted commit list instead.
- **TTS not installed / non-Windows / audio error** → prints text, warns, skips speech.

## Architecture

Pure data pipeline:

```
gitsource → filters → voice → prompt → llm → render → cli ─┬─ stdout
                                                           ├─ voicefile (.changelog/voice.md)
                                                           └─ tts (speak aloud)
```

`llm` and `tts` are the only provider-aware modules. Swapping provider/model/voice is a config change, never a code change.

## Tests

```bash
pytest
```
