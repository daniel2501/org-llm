# `extensions/` — non-Python first-class code

org-llm's primary surfaces aren't Python. The TUI is opencode (TypeScript +
SolidJS via OpenTUI). The editor is Emacs (org-mode is the substrate).
This directory holds first-class code for those surfaces, alongside the
Python core in `org_llm/`.

## Layout

```
extensions/
├── opencode/                 — TypeScript plugins for opencode (soft-fork pattern)
│   ├── src/                    .ts source for plugin entry points
│   ├── test/                   bun:test suites
│   ├── package.json            bun project config
│   └── README.md
├── emacs/                    — Emacs Lisp package
│   ├── org-llm.el              top-level entry; auto-tangled or hand-written
│   ├── org-llm-mcp.el          MCP-tools-from-emacs bridge
│   ├── org-llm-doom.el         Doom-Emacs SPC l keybinds
│   └── README.md
└── .shared/                  — JSON schemas, fixtures, code generators that
                                produce TS types from the Python schema
```

## Phase 2026-04.10 — extensions spec

See `~/org/org-llm-test-session/phase-16-extensions-codebase-expansion.org`
for the implementation plan. This directory's scaffolding lands ahead of the
real plugin/package work so the layout is clear.

## Why these surfaces matter

- **opencode**: the headline workspace. Every Phase-12 insight card, Phase-13
  walk-and-teach turn, and Phase-14 self-walk session happens through
  opencode's TUI. The Python `org-llm launch` command writes config + spawns
  opencode, but the *behavior* the user sees is opencode-side.
- **Emacs**: where org files actually get written, edited, and re-tangled.
  The Doom binds in our README are doc-only today; this dir holds the real
  package that backs them.
