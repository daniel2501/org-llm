<div align="center">

<img src="docs/img/01-banner.svg" alt="org-llm — queer, collective, free" />

# org-llm

**Your org-roam vault, augmented by FOSS LLMs.**

A self-hosted second brain that indexes your `~/org/` notes, embeds them with
[Ollama](https://ollama.com), exposes everything as MCP tools to
[opencode](https://opencode.ai) and [Claude Code](https://claude.com/code), and
falls back to multi-provider GPU clouds when your laptop runs out of VRAM.

</div>

---

## What you get

| | |
|---|---|
| 🔍 **Semantic search** over your full org-roam graph (`sqlite-vec`, no vector DB) | 💬 **RAG Q&A** grounded in your own notes (`org-llm ask "…"`) |
| 🧠 **Hardware-aware FOSS model catalog** — auto-pick the best fit for your VRAM | ☁️ **7+ cloud providers** when you outgrow local — RunPod, Vast, Lambda, Salad, OpenRouter, Groq, HF |
| 🛡️ **Encrypted credentials** via the standard Unix `pass` manager — never in plaintext | 🤖 **MCP server** — every capability exposed as a tool to opencode and Claude Code |
| 📓 **Org-babel skills** — define LLM workflows as `:skill:`-tagged source blocks | 🔬 **`doctor`** — deep health check + LLM-powered diagnosis of failures |
| 🚀 **`launch` / `claude`** — one-shot interactive workspaces with full vault context | 🎨 **FOSS tool installer** — `bat`, `eza`, `delta`, `zellij`, … with LCARS/Doom themes |
| 📊 **dbt analytics** — `stg_nodes`, `nodes_by_tag`, `recent_nodes`, `orphan_nodes` views | ⚡ **Fish/bash/zsh completions** + shortest-prefix command matching (`do` → `doctor`) |

---

## Quickstart

```sh
# Bootstrap everything: ollama, models, fonts, opencode, gh, claude, pass
org-llm install

# Initialize DB, index your vault, embed every node
org-llm init
org-llm index
org-llm embed

# Confirm the world is healthy
org-llm doctor

# Ask a question grounded in your notes
org-llm ask "what did I write about cooperative governance last month?"
```

<div align="center"><img src="docs/img/02-doctor.svg" alt="org-llm doctor" width="780" /></div>

---

## Architecture

```
                                ┌─────────────────────┐
   ~/org/*.org  ──── index ───▶ │  SQLite             │
                                │  + sqlite-vec       │ ◀── dbt views
                                │  + skills + history │     (analytics)
                                │  + config + pass    │
                                └──────────┬──────────┘
                                           │
        ┌──────────────────┬───────────────┼────────────────┬──────────────────┐
        ▼                  ▼               ▼                ▼                  ▼
   ┌─────────┐       ┌──────────┐    ┌──────────┐     ┌──────────┐      ┌────────────┐
   │ Ollama  │       │ MCP      │    │ opencode │     │ Claude   │      │ Cloud GPU  │
   │ (local) │       │ stdio    │ ─▶ │ workspace│     │ Code     │      │ (RunPod /  │
   │ chat    │ ◀──── │ 13 tools │    │          │     │ workspace│      │  Vast / …) │
   │ embed   │       └──────────┘    └──────────┘     └──────────┘      └────────────┘
   └─────────┘
```

The org file at `~/org/20260425230731-org_llm.org` is the literate-programming
source of truth — every Python/SQL/elisp file in this repo is tangled from it.

---

## Commands at a glance

Top-level commands accept the **shortest unique prefix** — `org-llm do` runs
`doctor`, `org-llm rev` runs `review-emacs`, ambiguous prefixes error with
candidates.

| Command | What it does |
|---|---|
| `org-llm init` | Create the SQLite DB and seed default config |
| `org-llm index` | Parse all `.org` files into the database (incremental by mtime) |
| `org-llm embed` | Generate embeddings for unembedded nodes (`embed_model`) |
| `org-llm code-index [PATHS]` | Index `~/repos` (or any tree) so `ask` answers across notes + code |
| `org-llm discover [DIRS]` | Probe filesystem for org/repo/dotfiles/Emacs roots; powers code-index auto-heal |
| `org-llm search "query"` | Semantic or keyword search (`-k` for keyword) |
| `org-llm ask "q"` | RAG Q&A; auto-detects time windows, path hints, and tag references |
| `org-llm capture` | Add a new note (LLM-polished by default; `--no-polish` to skip) |
| `org-llm tag` | Auto-tag untagged nodes (default dry-run; `--apply` to write) |
| `org-llm code "task"` | Generate code with retrieved org context (`--cloud` to route via OpenRouter) |
| `org-llm review-emacs` | LLM review of your Doom/vanilla Emacs config (`--cloud` for low-RAM hosts) |
| `org-llm models` | Discover, tune (catalog-based), assign, or pull FOSS LLMs |
| `org-llm performance` | Hardware-aware tuner — uses *free* RAM + measured tok/s (`--benchmark`) |
| `org-llm cloud` | Multi-provider GPU cloud — signup, configure, status, cost, `--quick-start` |
| `org-llm launch [-w WORKSPACE]` | Open themed opencode TUI: 30 MCP tools, LCARS theme, slash-commands |
| `org-llm claude` | Same, but Claude Code (`ANTHROPIC_API_KEY` from `pass`) |
| `org-llm doctor` | Deep health check + LLM diagnosis; `--install all` bulk-installs FOSS tools |
| `org-llm doctor -w` | LLM-driven self-test: 13 read-only probes + cloud-LLM judgement |
| `org-llm doctor -r PATH` | Append a structured org-mode report of any doctor run to PATH |
| `org-llm report` | Rich text reports — overview / tags / recent / orphans / daily |
| `org-llm tutor` | 31-step interactive tutorial — start with `tutor welcome` |
| `org-llm db` | Inspect schema, run SELECT queries, full data dictionary |
| `org-llm source <module>` | Print any module's source (with `--explain`) |
| `org-llm mcp` | Start the MCP stdio server (used by opencode and claude) |
| `org-llm grant <path>` / `revoke` / `grants` | Allow LLM file reads via MCP (with deny-list + auto-roots) |
| `org-llm grant-browser` / `revoke-browser` | Toggle LLM browser tools (`open_url`, `browser_command`) |
| `org-llm knob add <name>` | Define your own theme knob (e.g. `dinosaur`, `coffee`) |
| `org-llm completion fish --install` | Install shell completions |
| `org-llm install` | One-shot install Ollama, models, fonts, opencode, gh, claude, pass |

Every command has `--help`. The full setup walkthrough lives in `org-llm tutor`.

---

## FOSS model catalog

`org-llm models --discover` shows the entire FOSS catalog filtered by what
fits your hardware. `--tune` analyzes your current assignments and recommends
upgrades. Every catalog entry is FOSS-licensed (MIT / Apache 2.0 / BigCode
OpenRAIL / Meta Llama Community).

<div align="center"><img src="docs/img/05-models-discover.svg" alt="org-llm models --discover" width="780" /></div>

---

## Cloud GPUs

When `org-llm cloud --assess` says a model needs more VRAM than you have, pick
a provider and sign up — the CLI walks you through the rest:

```sh
org-llm cloud --providers          # compare prices and APIs
org-llm cloud --signup vast        # opens browser, prompts for API key
org-llm cloud --configure          # set endpoint URL + key (stored in pass)
org-llm cloud --test               # ping the configured endpoint
```

<div align="center">
  <img src="docs/img/03-cloud-providers.svg" alt="org-llm cloud --providers" width="780" />
  <img src="docs/img/04-cloud-cost.svg" alt="org-llm cloud --cost" width="600" />
</div>

Supported: **RunPod**, **Vast.ai**, **Lambda Labs**, **TensorDock**,
**Salad Cloud**, **Paperspace**, **CoreWeave**, plus per-token APIs
**OpenRouter** (free Llama 3.1 8B), **Groq** (free LPU tier), and
**Hugging Face Inference**.

---

## Credentials via `pass`

API keys are stored in the standard Unix
[password-store](https://www.passwordstore.org), GPG-encrypted at rest under
`~/.password-store/org-llm/`. Slug layout:

```
org-llm/
├── anthropic/api-key            # for `org-llm claude`
└── cloud/
    ├── runpod/api-key
    ├── vast/api-key
    └── openrouter/api-key
```

`org-llm cloud --configure` will offer to install `pass` if missing and will
**migrate** any existing SQLite-stored keys into pass and clear the plaintext
copy. See `org-llm tutor creds` for the full setup.

<div align="center"><img src="docs/img/08-creds.svg" alt="org-llm cloud --creds" width="640" /></div>

---

## MCP integration

`org-llm mcp` runs an MCP stdio server that exposes **30 tools** to any
MCP-aware client (opencode, Claude Code, …). The toolbox is designed
to give the LLM in opencode parity with the CLI, not a stripped-down
subset.

**Reading notes:**
`search_notes`, `ask_notes`, `get_node`, `list_nodes_by_tag`,
`list_recent_nodes`, `get_vault_stats`, `recent_files`

**Writing / acting:**
`capture_note`, `run_skill`, `tangle_file`, `index_vault`,
`embed_pending`, `set_config` *(allow-listed keys only)*

**Code corpus:**
`code_search` *(language-filterable)*

**Filesystem + ops:**
`discover_filesystem`, `doctor_health`, `performance_status`,
`list_models`, `list_grants`, `request_access`, `read_file`,
`list_directory`

**Browser (when granted):**
`open_url`, `browser_command`

**Skills + tutor + config:**
`list_skills`, `list_tutor_steps`, `get_tutor_step`, `get_config`,
`set_config`

`set_config` is allow-listed to safe keys only (`chat_model`,
`embed_model`, `code_model`, `temperature`, `code_dirs`, `trek_level`,
…). Credentials, grants, and theme-knob payloads stay inaccessible
from the LLM.

`org-llm launch` wires this up automatically for opencode (writes
`.opencode.json`, `.opencode/themes/`, `.opencode/command/`);
`org-llm claude` does the same for Claude Code (`.claude/settings.json`
+ `.claude/CLAUDE.md`). See [opencode workspace](#opencode-workspace).

---

## opencode workspace

`org-llm launch` is the *other face* of org-llm — a fully themed
opencode TUI with the full MCP toolbox, slash-command starter pack,
and a system prompt pre-loaded with vault stats, top tags, models,
hardware, filesystem inventory, and any active theme dials/knobs.

```sh
org-llm launch                       # default workspace: all
org-llm launch -w researcher         # read-heavy: search/ask/explore
org-llm launch -w scribe             # capture-heavy + skill workflows
org-llm launch -w engineer           # code-corpus + repo focus
org-llm launch --no-theme            # skip writing .opencode/themes/
org-llm launch --no-commands         # skip writing slash-commands
org-llm launch -n                    # dry-run: print config, don't launch
```

What gets written into your vault:

- `.opencode.json` — model, MCP server, instructions, theme reference.
- `.opencode/themes/org-llm-lcars.json` — LCARS palette (orange /
  purple / blue) matching the CLI, both light and dark variants.
- `.opencode/command/<name>.md` — slash-commands for instant action:
  `/discover`, `/recent`, `/health`, `/stats`, `/tags`, `/tutor`,
  `/code`, plus workspace-specific extras (`/explore` for researcher,
  `/capture` for scribe, `/repo` for engineer).

The system prompt also surfaces your **active theme knobs**
(`trek_level`, `commie_level`, `queer_level`, plus user-defined knobs
like `dinosaur`) so the in-opencode model matches the energy of your
CLI.

Rule of thumb: if you'd otherwise pipe four CLI commands together,
opencode is probably the right tool.

---

## Smart RAG retrieval

`org-llm ask` does more than pure vector search. It auto-detects three
intents in your query and adjusts retrieval accordingly:

- **Temporal phrases** — `"last week"`, `"past 6 months"`, `"yesterday"`,
  spelled-out numbers (`"last six months"`) — apply an `mtime` filter
- **Path keywords** — `"daily"`, `"journal"`, `"diary"` — surface the
  actual files in `~/org/<folder>/` regardless of similarity score
- **Tag references** — `"my politics tag"`, `tagged X`, `:foo:` — pull
  notes with that tag; if the tag doesn't exist you see *"closest
  existing: …"* up front rather than letting the LLM hallucinate

```sh
org-llm ask --cloud "summarize my last six months of daily notes in 6 bullets"
org-llm ask --cloud "what's in my :queer: tag?"
org-llm ask --cloud --days 30 "biggest themes recently"
```

A retrieval line prints before every answer so you can sanity-check what
the LLM actually saw:

```
▶ Retrieved 17 note(s) from the last 7 days + daily folder: 2026-04-12, …
```

---

## Code analysis (cross-corpus)

`org-llm code-index` walks `~/repos` (or any tree) and indexes source
files into the same DB so `ask` can answer about notes AND code in one
query. Skips `.git` / `node_modules` / `.venv` / `target` / `dist` etc.;
truncates per-file body at 24 KB; tags every code node `code:<lang>`.

```sh
org-llm code-index                                  # default: ~/repos
org-llm code-index ~/repos/dotfiles ~/.config/doom  # explicit paths
org-llm config code_dirs ~/repos,~/.config/doom     # persistent default
org-llm ask --cloud "how does cli.py wire up MCP?"
```

After indexing it auto-embeds new nodes so they're searchable
immediately. Pass `--no-embed` to skip.

If *every* path you pass is missing, the command no longer red-alerts
— it runs filesystem discovery and offers found code roots instead.
See [Filesystem discovery](#filesystem-discovery) below.

---

## Filesystem discovery

`org-llm discover` probes a small set of standard locations (`~/org`,
`~/repos`, `~/code`, `~/projects`, `~/work`, `~/dotfiles`,
`~/.config/doom`, `~/.doom.d`, `~/.config/emacs`, `~/.emacs.d`,
`~/.password-store`, `~/.local/share/ollama`) and reports what
actually exists with file counts and the most-frequent code language
per root.

```sh
org-llm discover                # standard probe
org-llm discover ~/extra/dir    # plus an extra dir
```

The same module powers two auto-heal flows:

- **`code-index <bad-paths>`** — if none of the paths exist, runs
  discovery and offers found code roots with `Index these? [Y/n]`
  instead of red-alerting.
- **`grants` empty-state** — surfaces concrete `grant-root`
  candidates pulled from disk instead of just printing "no grants".

The throughline: the LLM has access to the cloud and the local DB;
the *app* should also have access to the actual filesystem and feed
discoveries back into command flows.

---

## Performance tuning

`org-llm models --tune` uses the catalog's static VRAM × `0.55 × total_RAM`
heuristic. That passes models which OOM in practice — it can't see what
else your laptop is running. **`org-llm performance` reads free RAM at
runtime** and (with `--benchmark`) measures actual tokens-per-second per
pulled model.

```sh
org-llm performance              # read-only report
org-llm performance --benchmark  # measure tok/s per model (1-3 min)
org-llm performance --apply      # write recommended assignments to config
```

Severity icons in the recommendation table:

- ↓ **downgrade** — current model needs more RAM than you have
- ↑ **upgrade** — a higher-quality model fits
- + **missing** — role unassigned
- = **fit** — already optimal

---

## Self-healing

Every `red_alert + Exit` path first attempts a deterministic safe fix.
If the deterministic fix doesn't apply, the configured cloud LLM is
asked for a structured remediation, parsed against an allow-list of
safe `org-llm` subcommands, and executed.

What self-heals automatically:

| Failure | What we do |
|---|---|
| `no such table: config` | Run `init_db()` and continue |
| Ollama not reachable | Background `ollama serve`, wait, retry |
| Empty index but `~/org/` has `.org` files | Run `index` automatically |
| `ask` returns 0 hits and unembedded nodes exist | Run `embed` and retry the search |
| Model 404 at runtime (e.g. `llama3.2:latest` not pulled) | Pull and retry |
| Configured model is bogus (`llama99-doesnt-exist`) | LLM picks a safe alternative, swaps `chat_model`, retries |
| Out of memory | **Doesn't auto-fix** — RAM can't be conjured. Suggests `--cloud` / `performance --apply` |
| Path-traversal / sensitive-path requests | **Doesn't auto-fix** — security boundary |
| Missing API key for `--cloud` | **Doesn't auto-fix** — needs user paste |

Pick the best LLM for fix duty by benchmarking:

```sh
org-llm doctor --benchmark-fixers          # score 6 candidate models
org-llm doctor --benchmark-fixers --apply  # persist the winner as fixer_model
```

Then `_llm_assisted_fix` prefers `fixer_model` over `cloud_model` for
any fix call. Current measured leaderboard on 10 canonical scenarios:

| Rank | Model | Accuracy |
|---|---|---|
| 1 | `openai/gpt-oss-120b:free` | 80% |
| 2 | `openai/gpt-oss-20b:free` | 70% |
| 3 | `nvidia/nemotron-nano-9b-v2:free` | 60% |

---

## Doctor self-test

```sh
org-llm doctor -w                     # 13 read-only probes + LLM judgement
org-llm doctor -wr ~/org/dev-log.org  # also append a structured org report
```

Two LLM passes per run: one for qualitative assessment (✓ PASS / ⚠ NIT /
✗ ISSUE per probe + top-3 recommendations), one for a structured list
of executable fixes which the runner applies to allow-listed verbs only.

CI-friendly — wire it into a git pre-push hook to get a second pair of
eyes on every change without leaving your terminal.

---

## MCP file + browser grants

The MCP server exposes `read_file`, `list_directory`, `request_access`,
`open_url`, and `browser_command` to opencode/Claude — but only with
your explicit authorization. Three layers:

```sh
# Direct grants:
org-llm grant ~/projects/work
org-llm grants                       # see everything currently authorised

# Auto-grant roots (LLM may self-extend within these via request_access):
org-llm grant-root ~

# Browser:
org-llm grant-browser
org-llm doctor --install qutebrowser
```

Always-denied paths (sensitive deny-list, even with grants):
`~/.ssh`, `~/.gnupg`, `~/.password-store`, `~/.aws/credentials`,
`~/.kube/config`, `~/.netrc`, `/etc/shadow`, `/root/`, etc.

---

## Reports

```sh
org-llm report all     # everything below in one shot
org-llm report tags    # tag frequency leaderboard
org-llm report recent  # last 14 days of edits
org-llm report orphans # notes with no outgoing links
org-llm report daily   # daily/journal notes preview
```

<div align="center"><img src="docs/img/06-report-all.svg" alt="org-llm report all" width="700" /></div>

---

## Emacs config review

Auto-detects your Doom or vanilla config and asks `reason_model` for structured
advice — strengths, issues, prioritized improvements, optional polish.

```sh
org-llm review-emacs                       # full review
org-llm review-emacs --focus performance   # narrow scope
org-llm review-emacs --diff-only -o p.md   # emit patch hunks
```

<div align="center"><img src="docs/img/09-review-emacs.svg" alt="org-llm review-emacs" width="780" /></div>

Past reviews are written to the `history` table so you can find them later
with `org-llm db -q "SELECT timestamp, query FROM history WHERE command='review-emacs'"`.

---

## Theme knobs

`trek` / `commie` / `queer` are built-in dials (0–3) that shape the
completion-message pool. Levels are weights, not booleans:

```
0 = silent  |  1 = sparse (½×)  |  2 = normal  |  3 = max (2×)
```

Multi-tagged messages (e.g. `trek+commie`) use the MIN level — silence
any tag and dependent messages disappear. Persist via the config DB:

```sh
org-llm config queer_level 1     # half pride
org-llm config commie_level 3    # 2× solidarity
org-llm config trek_level 0      # silence Trek
```

Or per-command via `ORG_LLM_<NAME>_LEVEL`. Define your own dials:

```sh
org-llm knob add dinosaur \
  -m '◀ ROAR.|info' \
  -m '◀ Dino-mite work, comrade.|lcars1'

org-llm config dinosaur_level 2
org-llm knob list
```

User knobs live in the SQLite `user_theme_knobs` row; their levels
follow the same `<name>_level` pattern. Built-in knobs can't be removed
but can be set to 0.

---

## Theme: dark + light

Default is **dark** (LCARS-canonical bright orange/purple/blue on a dark
terminal). Switch persistently with `org-llm theme light`, or per-command with
`ORG_LLM_THEME=light org-llm …`. Every colour the app emits — Rich console,
banners, panels, progress bars, plus the `bat`/`delta`/`starship`/`fzf` theme
files written by `doctor --install` — switches in lockstep.

<table>
<tr>
  <th align="center">Dark mode (default)</th>
  <th align="center">Light mode</th>
</tr>
<tr>
  <td><img src="docs/img/02-doctor.svg" alt="doctor — dark" /></td>
  <td><img src="docs/img/02-doctor-light.svg" alt="doctor — light" /></td>
</tr>
<tr>
  <td><img src="docs/img/05-models-discover.svg" alt="models --discover — dark" /></td>
  <td><img src="docs/img/05-models-discover-light.svg" alt="models --discover — light" /></td>
</tr>
<tr>
  <td><img src="docs/img/06-report-all.svg" alt="report — dark" /></td>
  <td><img src="docs/img/06-report-all-light.svg" alt="report — light" /></td>
</tr>
</table>

```sh
org-llm theme show     # show stored + active mode
org-llm theme light    # persist as light
org-llm theme dark
org-llm theme toggle   # flip whatever is set
```

---

## Environment variables

| Variable | Effect |
|---|---|
| `ORG_LLM_DB` | Override the SQLite database path (default: `~/.local/share/org-llm/org-llm.db`) |
| `ORG_LLM_ORG_DIR` | Override the org-roam directory (default: `org_dir` config key, fallback `~/org`) |
| `ORG_LLM_OLLAMA_URL` | Override the Ollama endpoint (default: `ollama_url` config key, fallback `http://localhost:11434`) |
| `ORG_LLM_THEME` | `dark` or `light` (default: `dark`). Persistent: `org-llm theme {dark,light,toggle}`. |
| `ORG_LLM_NERD_FONTS` | Force-enable (`1`/`yes`) or disable (`0`/`no`) Nerd Font icons |
| `ORG_LLM_TREK_LEVEL` | Star Trek messaging weight, `0` (silent) / `1` (½×) / `2` (1×) / `3` (2×). Default `2`. Persistent via `org-llm config trek_level N`. |
| `ORG_LLM_COMMIE_LEVEL` | Solidarity messaging weight, `0`–`3`. Persistent via `config commie_level N`. |
| `ORG_LLM_QUEER_LEVEL` | Pride/trans messaging weight, `0`–`3`. Persistent via `config queer_level N`. |
| `ORG_LLM_<KNOB>_LEVEL` | Any user-defined knob from `org-llm knob add`, `0`–`3`. Persistent via `config <name>_level N`. |
| `PASSWORD_STORE_DIR` | Override the `pass` store location (default: `~/.password-store`) |
| `ANTHROPIC_API_KEY` | Used by `org-llm claude`. If unset, falls back to `pass` slug `org-llm/anthropic/api-key`. |

Resolution order is always: **env var → SQLite config → built-in default**, so
you can override any setting for a single command without touching the DB.

---

## Tutor

```sh
org-llm tutor          # show the welcome step
org-llm tutor <step>   # jump to one of the 24 steps
org-llm tutor --all    # read everything start-to-finish
```

<div align="center"><img src="docs/img/07-tutor-welcome.svg" alt="org-llm tutor welcome" width="640" /></div>

Steps:
`welcome → init → index → embed → search → ask → capture → tag → code →
config → skills → report → doctor → install → db → dbt → opencode →
source → review-emacs → creds → cloud → launch → emacs → claude → done`

---

## Recording GIFs (optional)

The static SVGs above are generated by `tools/gallery.py` (run from the repo
root with `uv run python tools/gallery.py`). For animated demos, install
[`vhs`](https://github.com/charmbracelet/vhs) plus its dependency `ttyd`:

```sh
# vhs binary
curl -sL https://github.com/charmbracelet/vhs/releases/latest/download/vhs_Linux_x86_64.tar.gz \
  | tar xz -C /tmp && mv /tmp/vhs_*_Linux_x86_64/vhs ~/.local/bin/

# ttyd (ubuntu/debian)
sudo apt install ttyd
```

Then write a `.tape` script under `docs/tape/` and run `vhs <script>.tape`.

---

## Development

```sh
git clone git@github.com:daniel2501/org-llm.git
cd org-llm
uv sync                 # install deps + dev tools
uv run pytest -q        # run the test suite (225+ tests)
uv run python tools/gallery.py   # regenerate README screenshots
```

The Python source is **tangled from the org file** at
`~/org/20260425230731-org_llm.org`. Edit there, `, b t` (`org-babel-tangle`),
commit both repos.

Project layout:

```
org_llm/
  cli.py          # the Typer app — every CLI command
  cli_skills.py   # skill / skills / skill-index / skill-new commands
  cloud.py        # multi-provider GPU registry + chat/embed/check
  creds.py        # `pass` wrapper for API keys
  db.py           # SQLAlchemy models + sqlite-vec extension load
  indexer.py      # parse org → files/nodes
  llm.py          # thin Ollama wrapper
  mcp_server.py   # FastMCP server with 13 tools
  models.py       # FOSS model catalog + tool registry + theming
  report.py       # rich text reports
  search.py       # vector_search + keyword_search
  skills.py       # :skill: org-babel block extractor + runner
  ui.py           # console, themes, banners, intensity levels
tests/            # 225 tests across 11 test files
dbt/              # analytics views (stg_nodes, recent_nodes, …)
doom/             # Doom Emacs integration (org-llm.el)
tools/            # gallery.py screenshot generator
```

---

## License

GPL-3.0 — same energy as the rest of the FOSS LLM stack this builds on.
The org-llm idea, like solidarity, is freely shared.

> *"From each according to ability, to each according to need."*
> — and `chmod +x` the revolution.
