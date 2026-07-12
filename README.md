# voicelog

**The changelog that talks to you.** At any git moment — after a `git pull`, before opening a PR, or just to catch up — voicelog gives you a short spoken + written rundown of what changed. Both the LLM and the voice are configurable (NVIDIA, OpenAI, ElevenLabs, and more). Run `voicelog`, and it prints a casual changelog and **reads a summary aloud**.

## Install

```bash
pip install voicelog            # core (text rundown; also all you need for OpenAI/ElevenLabs speech)
pip install "voicelog[tts]"     # + NVIDIA Riva TTS support (only needed for the riva provider)
```

Requires Python 3.10+. Speech playback works on Windows, macOS, and Linux (via `winsound`, `afplay`, and `paplay`/`aplay`/`ffplay` respectively). The `[tts]` extra pulls in NVIDIA's gRPC client — only needed if you use `tts_provider: riva` (the default). If you set `tts_provider: openai` or `elevenlabs`, plain `pip install voicelog` is enough since those just use HTTP.

Verify it's installed:

```bash
voicelog --help
```

<details>
<summary>Install from source (for development)</summary>

```bash
git clone https://github.com/Navaneethp007/voicelog.git
cd voicelog
pip install -e ".[tts,dev]"
pytest
```
</details>

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

### Orient yourself on a repo you just cloned

`--new` gives you a spoken + written overview of a project you're new to: what it *is* (from the README) plus what's been *happening lately* (the most recent commits, regardless of tags/releases):

```bash
voicelog --new
```

Reads `onboard_commits` recent commits (default 15 — tune it in `changelog.yml`) and the repo's README (`README.md`/`.rst`/`.txt`). Missing a README? It still orients you from the commits alone. Like the other modes above, `--new` is transient — it never writes `.changelog/voice.md`.

### Richer summaries with `--with-diff`

By default voicelog sends only commit messages, changed filenames, and a diffstat (lines added/removed) to the model — never your actual code. If commit messages are vague and you want the model to understand what really changed, opt in to sending the real code diff:

```bash
voicelog --pull --with-diff
```

This prints a privacy warning every time, since it's the one thing in voicelog that sends code to the LLM provider. Works with every mode (default, `--pull`, `--pr`, `--since`, `--new`).

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

## Using your own voice

The spoken part is just as configurable — set `tts_provider` in `changelog.yml`. All three work cross-platform (Windows/macOS/Linux) and are independent of your text LLM choice above (mix and match freely, e.g. Groq for text + ElevenLabs for voice).

| Provider | tts_provider | tts_voice | tts_api_key_env |
|----------|--------------|-----------|------------------|
| NVIDIA Riva (default) | `riva` | `Magpie-Multilingual.EN-US.Sofia` | `NVIDIA_API_KEY` |
| OpenAI TTS | `openai` | `alloy` (or echo/fable/onyx/nova/shimmer) | `OPENAI_API_KEY` |
| ElevenLabs | `elevenlabs` | your ElevenLabs voice id | `ELEVENLABS_API_KEY` |

No key for any of them? Set `speak: false` — the text changelog still works everywhere. See the comments in `changelog.yml` for the full field list per provider (`tts_model`, `tts_base_url`, etc).

## Configuration

Copy `changelog.yml` and edit it — all fields are optional, built-in defaults work out of the box. Key fields:

| Field | Purpose |
|-------|---------|
| `model` | Any model on build.nvidia.com (default mistral-medium) |
| `sections` | Section grouping for the changelog |
| `noise` | Regex commit subjects to drop (e.g. `^wip`) |
| `speak` | `true`/`false` — read aloud (also `--no-speak` per run) |
| `tts_provider` | `riva` / `openai` / `elevenlabs` |
| `tts_voice` | Voice id/name — meaning depends on the provider |
| `voice_md` | Path to the persistent changelog |
| `max_commits` | Cap on commits sent to the model for normal ranges |
| `onboard_commits` | Recent commits `--new` reads (ignores tags) |

Every secret is read from an environment variable named in the config (`api_key_env` for text, `tts_api_key_env` for speech) — never stored in the file itself.

## Graceful degradation

voicelog never blocks on audio. Text always prints first; if anything downstream fails it only warns to stderr:
- **LLM unavailable** → prints a plain bulleted commit list instead.
- **TTS misconfigured / key missing / no audio player / synthesis error** → prints text, warns, skips speech.

## Architecture

Pure data pipeline:

```
gitsource → filters → voice → prompt → llm → render → cli ─┬─ stdout
  (or readme +                                            ├─ voicefile (.changelog/voice.md, opt-in)
   read_recent_commits                                     └─ tts (speak aloud)
   for --new)
```

`llm` and `tts` are the only provider-aware modules. Swapping provider/model/voice is a config change, never a code change.

## Tests

```bash
pytest
```
