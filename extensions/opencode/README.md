# `@org-llm/opencode-plugin`

Soft-fork TypeScript plugins for opencode — first-class behavior code that
runs inside opencode's plugin loader rather than living in `org-llm`'s
Python config writer.

## Why a plugin instead of more JSON config?

The 2026-04-29 audit (Phase 15) showed opencode's JSON config has real
limits:

- **Provider config silently dropped** when fields are missing or schema
  drifts. Even after hitting the spec exactly, opencode's bundled
  providers can win the model-selection race.
- **Slash commands** as `.md` files are simple but limited — no UI, no
  state, no event hooks.
- **Theme JSON** is reasonably rich but the wordmark, splash animation,
  and inline panels (life-support, insights) need more than colors.

Plugins give us hooks into opencode's event stream, can render TUI
widgets, and can intercept model selection. That's the right surface
for the behavior we want.

## Phase 16.1 scope (first real plugin)

| Hook                      | What it does                                                       |
|---------------------------+--------------------------------------------------------------------|
| `session-start`           | Render the Phase 12 insight cards as a TUI widget instead of injecting via system prompt. Cleaner UX, doesn't bloat the prompt across turns. |
| `tool-call`               | Surface life-support vitals if any probe trips during a long-running operation. |
| `model-selection`         | Refuse opencode's bundled fallback (`big-pickle`) when an Ollama provider is configured. Fixes the Phase 15 audit bug structurally. |
| `theme-change`            | Re-skin our LCARS palette in response to knob changes without requiring a relaunch. |

## Layout

```
extensions/opencode/
├── package.json    bun project config
├── tsconfig.json   strict TS settings
├── src/
│   └── index.ts    plugin entry point (default export)
└── test/
    └── index.test.ts   bun:test sanity tests
```

## Build / dev

```bash
cd extensions/opencode
bun install
bun run typecheck
bun run test
bun run build           # → dist/index.js
```

The output gets registered with opencode via:

```json
"plugin": ["./dist/index.js"]
```

…in the workspace's `.opencode/opencode.json` (Phase 16.2 wires this into
`org-llm launch`'s config writer).

## Status

**Phase 16.0** (this commit): scaffolding only — build pipeline lands a
working no-op plugin. Real hooks land in Phase 16.1+.
