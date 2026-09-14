# voicelog

**The changelog that talks to you.** At any git moment — after a `git pull`, before opening a PR, or just to catch up — voicelog gives you a short spoken + written rundown of what changed. Both the LLM and the voice are configurable (NVIDIA, OpenAI, ElevenLabs, and more). Run `voicelog`, and it prints a casual changelog and **reads a summary aloud**.

## Install

```bash
pip install voicelog            # core (text rundown; also all you need for OpenAI/ElevenLabs speech)
pip install "voicelog[tts]"     # + NVIDIA Riva TTS support (setup can also install this for you)
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

## Setup — run it once

**Just run `voicelog`.** On a machine with nothing configured it walks you through setup: pick a provider, paste your API key, pick a model from that provider's live catalog (searchable, or type an id), pick a voice. Then it carries on and prints the changelog you asked for.

Most steps take the default, so setup is mostly pressing Enter. The model you choose is checked with one tiny request before setup finishes — being listed in a provider's catalog doesn't guarantee your account may *call* it, and finding that out during setup beats finding out on your first real run.

Re-run it any time with `voicelog --setup`.

Your answers are saved per-machine, so **every repo you work in inherits them** — you configure once, not once per project:

| Platform | User config |
|----------|-------------|
| Windows | `%APPDATA%\voicelog\config.yml` |
| macOS / Linux | `~/.config/voicelog/config.yml` (honours `$XDG_CONFIG_HOME`) |

Set `VOICELOG_CONFIG_HOME` to put it somewhere else.

**Your API key is never written to that file** — only the *name* of the environment variable it lives in. Setup puts the key in the current session and offers to keep it for future terminals (`setx` on Windows, an `export` line in your shell profile on macOS/Linux). Decline and it just prints the command to run yourself:

```bash
export NVIDIA_API_KEY=nvapi-...     # bash / macOS / Linux
set NVIDIA_API_KEY=nvapi-...        # Windows cmd (this session)
$env:NVIDIA_API_KEY = "nvapi-..."   # PowerShell (this session)
setx NVIDIA_API_KEY "nvapi-..."     # Windows, every future terminal
```

Keeping it has a cost worth knowing: on Windows `setx` passes the value on a command line, so it is briefly visible to other processes (and recorded if process-creation auditing is on) — voicelog says so before it runs. On macOS/Linux the key lands in a shell profile that is typically world-readable, so think twice if you keep your dotfiles in git.

A free key from [build.nvidia.com](https://build.nvidia.com) needs no credit card, but any provider below works.

**voicelog ships no default model.** You pick one during setup, because provider model ids get retired — NVIDIA retired the model voicelog used to default to, and every run on a default config quietly fell back to a plain commit list. Nothing voicelog ships can go stale that way now.

Non-interactive environments (CI, git hooks, pipes) never see a prompt: setup is skipped, and a run with no model configured prints how to fix it and falls back to the commit list rather than failing your build.

## Usage

```bash
# From inside your git repo: prints the changelog AND speaks it
voicelog

# Print only, no audio
voicelog --no-speak

# Ignore the cache and regenerate fresh prose from the model
voicelog --fresh

# Use an alternate config file (ignores your user config, for reproducible runs)
voicelog --config path/to/changelog.yml

# Summarise only what arrived since voicelog last looked
voicelog --since-last

# Missed the audio? Re-play the last run - free, offline, no model call
voicelog --replay

# Re-run setup
voicelog --setup

# Override provider/model/voice for a single run, without touching your config
voicelog --model meta/llama-3.3-70b-instruct
voicelog --provider groq
voicelog --provider ollama --model llama3.1     # local, no key needed
voicelog --tts-provider openai --voice nova
voicelog --provider custom --base-url https://my-proxy/v1 --api-key-env MY_KEY
```

Switching provider takes that provider's endpoint and key env var with it; *restating*
the provider you already use changes nothing, so a hand-edited `base_url` survives
`--provider nvidia`. A switch keeps whatever `model` your config names and says so —
pass `--model` to change it too.

The same rule applies wherever the name is written, not just on the command line: putting
`provider: openai` in a config file brings OpenAI's endpoint and key variable with it, and
anything you set yourself in that file wins over the preset. A flag and a config entry mean
the same thing.

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

The generated changelog is cached on the **set of commits**. Re-running with the same commits returns the cached text instead of calling the model again — so repeat runs are free, fast, and don't churn `voice.md`. Add a new commit (or pass `--fresh`) to regenerate.

The cache and the run watermark live in your repo's git directory (`.git/voicelog/`), not in the work tree — so there is nothing to git-ignore, nothing to commit by accident, and the same file is used whichever subdirectory you run from. **voicelog writes nothing into your repo unless you ask for `--changelog`.** Upgrading from 0.2.x leaves a stale `.changelog/.voicelog-cache.json` behind; it is safe to delete.

By default, each run is a **transient rundown** — it prints the casual changelog and reads a short spoken summary aloud, but doesn't write anything. That's the everyday use: quick "what changed" at a git moment.

Each run:
1. Prints the full casual changelog (under `## Unreleased`) to stdout.
2. Reads a **short spoken summary** aloud through whichever voice you configured (2-3 sentences — not the whole changelog, so audio stays quick even on huge repos). Ctrl+C during playback skips the audio; the text has already printed.

Add `--changelog` (or set `write_changelog: true`) to *also* maintain a persistent release changelog at `.changelog/voice.md` that accumulates across releases (with automatic promotion when you tag). That's opt-in — most runs don't need it.

### Working on large repos

For big histories (e.g. a fork with hundreds of commits since the last tag), voicelog caps how many commits it sends to the model — the most recent `max_commits` (default 50) — so generation stays fast and cheap. The spoken part is always a short summary, so it never gets stuck reading a giant changelog aloud. Tune `max_commits` in `changelog.yml`, or use `--no-speak` for instant text-only output.

### Picking up where you left off

```bash
voicelog --since-last
```

Summarises only what has arrived since voicelog last summarised this repo. Useful in a project with
no tags, where the default range is "the last 50 commits" and every run therefore says the same
thing.

The watermark advances only when a run actually printed a summary — not when it fell back to a raw
commit list, and not for the one-off ranges (`--pull`, `--pr`, `--since`), since those show a slice
the watermark never covered. If the stored commit is no longer in your branch's history (a rebase, a
reset, a shallow clone), voicelog says so and shows the default range instead of failing. Like the
other range modes it is transient: it never writes the release changelog.

### Missed the audio?

```bash
voicelog --replay
```

Re-prints the last run and re-speaks its summary, straight from the cache: no model call, no git
range, nothing recorded. It exists because the watermark records that a summary was *printed*, not
that it was *heard* — if the audio failed, or you stepped away, `--since-last` correctly reports
nothing new and the run you missed would otherwise be gone.

Because it replays rather than regenerates, you get the exact words you missed, not a paraphrase.
**Only the most recent run is kept**, so the next `voicelog` run in that repo replaces it.

## Persistent changelog (`.changelog/voice.md`)

voicelog maintains this file for you:
- Keeps the current `## Unreleased` block at the top, refreshed each run (idempotent — running twice with no new commits changes nothing).
- When you `git tag` a release, the previous `## Unreleased` is **promoted** to `## <tag>` and a fresh `## Unreleased` starts above it. Your history accumulates; nothing is lost.

## Using your own LLM

voicelog talks to any **OpenAI-compatible** endpoint. `voicelog --setup` offers these presets, lists each one's live models for you to choose from, and checks that your pick actually answers; you can also set `base_url`, `model` and `api_key_env` by hand. The key is never stored in either config file.

| Provider | base_url | api_key_env | Model ids |
|----------|----------|-------------|-----------|
| NVIDIA (default) | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com) |
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/docs/models) |
| Groq (fast, free tier) | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/docs/models) |
| OpenRouter (one key, many providers) | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | [openrouter.ai/models](https://openrouter.ai/models) |
| Ollama (local, no key) | `http://127.0.0.1:11434/v1` | *(leave blank — none needed)* | whatever you have pulled |

The model column is deliberately a link rather than a value: setup reads each provider's `/v1/models` at the moment you run it, so the list is never out of date. Anything else OpenAI-compatible works too — pick "Other" and paste its base URL.

Reasoning models (OpenAI's o-series, gpt-5) work too: they reject the parameters most tools send, so voicelog asks the provider what it wants and retries — no model allowlist to go stale.

## Using your own voice

The spoken part is just as configurable — `voicelog --setup` asks, or set `tts_provider` in a config file, or pass `--tts-provider` / `--voice` for one run. Riva is the only backend needing an extra package (it speaks gRPC, not HTTP); if you pick it and don't have it, setup offers to `pip install` it for you and carries on either way. Setup puts the speech service matching your text provider first, but every service stays selectable: pairing Groq for text with ElevenLabs for speech is supported, and Groq, OpenRouter and Ollama have no speech service of their own. All three work cross-platform (Windows/macOS/Linux) and are independent of your text LLM choice above (mix and match freely, e.g. Groq for text + ElevenLabs for voice).

| Provider | tts_provider | tts_voice | tts_api_key_env |
|----------|--------------|-----------|------------------|
| NVIDIA Riva (default) | `riva` | `Magpie-Multilingual.EN-US.Sofia` | `NVIDIA_API_KEY` |
| OpenAI TTS | `openai` | `alloy` (or echo/fable/onyx/nova/shimmer) | `OPENAI_API_KEY` |
| ElevenLabs | `elevenlabs` | your ElevenLabs voice id | `ELEVENLABS_API_KEY` |
| No voice | `none` | — | — |

No key for any of them? Set `speak: false` — the text changelog still works everywhere. See the comments in `changelog.yml` for the full field list per provider (`tts_model`, `tts_base_url`, etc).

## Configuration

Settings come from four layers, each overriding the one before:

```
built-in defaults  <  your user config  <  the project's ./changelog.yml  <  CLI flags
```

That split is the point: **which provider, model, key and voice you use is personal**, so it lives in your user config and follows you between repos. **Which sections a changelog has, what commit noise to drop** and where voice samples live are properties of a project, so they belong in a `changelog.yml` that project can commit — without carrying anyone's model choice or key.

`--config PATH` replaces both discovered files, so a pinned config means the same thing on every machine.

All fields are optional. Key ones:

| Field | Purpose |
|-------|---------|
| `provider` | Preset name: `nvidia`, `openai`, `groq`, `openrouter`, `ollama`, `custom` |
| `base_url` | OpenAI-compatible endpoint |
| `api_key_env` | Name of the env var holding your key (blank = endpoint needs none) |
| `model` | Model id at your provider — **no default**; set by `voicelog --setup` or `--model` |
| `sections` | Section grouping for the changelog |
| `noise` | Regex commit subjects to drop (e.g. `^wip`) |
| `speak` | `true`/`false` — read aloud (also `--no-speak` per run) |
| `tts_provider` | `riva` / `openai` / `elevenlabs` / `none` (no audio) |
| `tts_voice` | Voice id/name — meaning depends on the provider |
| `voice_md` | Path to the persistent changelog, relative to the repo |
| `max_commits` | Cap on commits sent to the model for normal ranges |
| `onboard_commits` | Recent commits `--new` reads (ignores tags) |

Config values are checked when they load, so a typo names itself — the file, the setting and the
value — instead of failing somewhere downstream. When the bad value is in *your* config, `voicelog
--setup` repairs it by dropping the unusable setting; when it is in a project's `changelog.yml`,
voicelog says so rather than pretending the wizard can fix a file it never writes. That matters most for the quiet ones: `noise: ^wip`
written as a bare string used to become four one-character patterns that discarded every commit, and
`voice_samples: ~/voice/` silently loaded nothing at all.

Every secret is read from an environment variable named in the config (`api_key_env` for text, `tts_api_key_env` for speech) — never stored in either config file, including the one `voicelog --setup` writes. If text and speech use different providers, setup asks for both keys.

### Output styling

On a terminal, headings, `**bold**` and `` `code` `` are rendered with bold, underline and dim
rather than printed as markdown source. Piped or redirected output is left exactly as it was — plain
markdown — so `voicelog > NOTES.md` still produces a file you can commit. Set `NO_COLOR` to turn
styling off, or `FORCE_COLOR` to keep it through a pipe.

## Graceful degradation

voicelog never blocks on audio, and never fails your build. Text always prints first; if anything downstream fails it only warns to stderr:
- **LLM unavailable** (timeout, 5xx, rate limit) → retries once, then prints a plain bulleted commit list instead.
- **No model configured** → says to run `voicelog --setup`, then prints the commit list. Exit code stays 0, so pre-push hooks and CI keep working.
- **Model retired or unknown** (HTTP 404/410) → names the model, the endpoint and `voicelog --setup`, then prints the commit list. Reported as a configuration problem rather than an outage, because that is what it is — a retired model hiding behind a generic "LLM unavailable" warning is how the old default model stayed broken unnoticed.
- **TTS misconfigured / key missing / no audio player / synthesis error** → prints text, warns, skips speech.

## Architecture

Pure data pipeline:

```
gitsource → filters → voice → prompt → llm → render → cli ─┬─ stdout
  (or readme +                                            ├─ voicefile (.changelog/voice.md, opt-in)
   read_recent_commits                                     └─ tts (speak aloud)
   for --new)

cli --setup → wizard → providers (GET /v1/models) → config.save_user_config
```

`llm`, `tts` and `providers` are the only provider-aware modules, and `wizard` is the only interactive one. Swapping provider/model/voice is a config change, never a code change.

## Tests

```bash
pytest
```
