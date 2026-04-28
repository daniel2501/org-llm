# pi-org-llm — bridge org-llm's MCP server into Pi

[Pi](https://pi.dev/) is a minimal, MIT-licensed terminal coding harness with a
deep TypeScript extension model. It doesn't ship with MCP support; this
extension adds it for org-llm specifically.

## What it does

Spawns `org-llm mcp` as a subprocess, speaks JSON-RPC 2.0 / MCP 2024-11-05
over stdio, then registers each MCP tool as a Pi tool prefixed with
`org_llm_`. So opencode's `search_notes` tool becomes Pi's
`org_llm_search_notes`, and Pi's typed tool-call narrowing works on it
the same way it does for built-in tools.

Beyond raw bridging, it also:

- **Per-turn system prompt injection** — the org-llm MCP server's
  `instructions` field (the search-first contract) gets prepended to
  the system prompt on EVERY turn, not just at session start. This is
  one of the main wins over opencode where the prompt is snapshotted
  at launch.
- **Streaming progress** — slow tools (`org_llm_index_vault`,
  `org_llm_embed_pending`, `org_llm_dbt_build`) emit MCP
  `notifications/progress` events that Pi exposes to the user via
  per-call progress updates.
- **Themed status line** — `setStatus` in LCARS orange shows
  `org-llm ⊳ N MCP tools` so the user knows the bridge is alive.
- **Slash commands** — `/org-llm-tools` lists every tool the bridge
  registered, grouped by family. `/org-llm <intent>` is a shortcut for
  `org_llm_org_llm_run` — feed any org-llm CLI verb as free text.
- **Clean shutdown** — sends `notifications/exit`, then SIGTERM, then
  SIGKILL on the MCP child so SQLite connections drain.

## Install

The bundled extension ships with org-llm. The CLI handles install for you:

```sh
org-llm pi --install         # install Pi if missing + copy extension
org-llm pi --launch          # install (idempotent) + launch Pi with extension
org-llm pi --show            # print path to bundled extension
```

`--install` will:
1. Detect whether `pi` is on PATH (or under `~/.bun/bin`, `~/.local/bin`, etc).
2. If not, run the official installer:
   `curl -fsSL https://pi.dev/install.sh | bash`
   OR `npm install -g @mariozechner/pi-coding-agent` if npm is preferred.
3. Copy `pi-org-llm.ts` to `~/.pi/extensions/pi-org-llm.ts`.
4. Append a one-liner to `~/.pi/config.json` so `pi` loads the
   extension by default. (Without this you'd need `pi -e ~/.pi/extensions/pi-org-llm.ts`
   every time.)

## Manual install

If you'd rather wire it yourself:

```sh
# 1. Install Pi however you like.
curl -fsSL https://pi.dev/install.sh | bash

# 2. Copy the bundled extension to a Pi-discovered location.
cp $(org-llm pi --show) ~/.pi/extensions/pi-org-llm.ts

# 3. Either load on every run...
pi -e ~/.pi/extensions/pi-org-llm.ts

# 4. ...or add it to ~/.pi/config.json:
{ "extensions": ["~/.pi/extensions/pi-org-llm.ts"] }
```

## Environment

| Variable | Effect |
|---|---|
| `ORG_LLM_BIN` | Path to the `org-llm` binary. Default: PATH lookup. |
| `ORG_LLM_DB` | Passed through to `org-llm mcp`. Override the DB. |
| `ORG_LLM_PI_DEBUG` | `1` = log every JSON-RPC frame to stderr. |

## Comparison vs opencode + Claude Code

| | opencode (`org-llm launch`) | Claude Code (`org-llm claude`) | Pi (`org-llm pi`) |
|---|---|---|---|
| MCP support | native | native | this extension |
| System prompt refresh | snapshot at launch | snapshot at launch | **per turn** |
| UI extensibility | JSON config + plugins | limited | full TypeScript hooks |
| Streaming progress | rendered in TUI | rendered in TUI | per-call onProgress callback |
| Slash command source | `.opencode/command/*.md` | `.claude/commands/*.md` | TypeScript code |
| Cost to ship | bundled ✓ | bundled ✓ | bundled ✓ |

## Limitations / caveats

- Pi's extension API is still evolving. The bridge uses `pi.events?.on?.()`
  and `ctx.ui?.setStatus?.()` defensively — any host method that doesn't
  exist in your Pi version no-ops cleanly rather than crashing.
- Tools that require an MCP `Context` parameter (Pi doesn't proxy this)
  still work, just without progress events. Use `ORG_LLM_PI_DEBUG=1` to
  confirm what's flowing.
- This bridge only handles tools. MCP `resources/` and `prompts/` aren't
  exposed yet — open an issue if you want them.
