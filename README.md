# voicelog

**The changelog that talks to you.** voicelog reads your git commits since the last tag, writes them up as a fun, casual changelog using a free LLM (NVIDIA NIM), prints it, **reads it aloud**, and keeps a running `.changelog/voice.md` across releases.

## Install

```bash
pip install -e .          # core (text changelog only)
pip install -e ".[tts]"   # + speech (NVIDIA Riva TTS)
```

Requires Python 3.10+. Speech playback currently uses Windows audio (`winsound`).

## Setup

Get a free API key at [build.nvidia.com](https://build.nvidia.com) and set it. The same key powers both the changelog text and the speech.

```bash
# bash / cmd / PowerShell respectively
export NVIDIA_API_KEY=nvapi-...
set NVIDIA_API_KEY=nvapi-...
$env:NVIDIA_API_KEY = "nvapi-..."
```

To persist it on Windows: `setx NVIDIA_API_KEY "nvapi-..."` (then open a new terminal).

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

### Caching

The generated changelog is cached on the **set of commits** (in `.changelog/.voicelog-cache.json`). Re-running with the same commits returns the cached text instead of calling the model again — so repeat runs are free, fast, and don't churn `voice.md`. Add a new commit (or pass `--fresh`) to regenerate. You'll usually want to git-ignore the cache file.

Each run:
1. Prints the casual changelog (under `## Unreleased`) to stdout.
2. Reads it aloud via NVIDIA Riva TTS.
3. Updates `.changelog/voice.md` — a persistent changelog that accumulates across releases.

## Persistent changelog (`.changelog/voice.md`)

voicelog maintains this file for you:
- Keeps the current `## Unreleased` block at the top, refreshed each run (idempotent — running twice with no new commits changes nothing).
- When you `git tag` a release, the previous `## Unreleased` is **promoted** to `## <tag>` and a fresh `## Unreleased` starts above it. Your history accumulates; nothing is lost.

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
