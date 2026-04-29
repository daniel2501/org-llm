# `org-llm.el` — Emacs integration

First-class Emacs Lisp package alongside the Python `org_llm/` core. The
README's Doom keybinds and SPC-l workflow have been documentation-only;
this directory holds the real package that backs them.

## Install

### Doom Emacs

```elisp
;; packages.el
(package! org-llm
  :recipe (:host github
           :repo "daniel2501/org-llm"
           :files ("extensions/emacs/*.el")))
```

```elisp
;; config.el
(use-package! org-llm
  :after org
  :config
  (setq org-llm-binary (executable-find "org-llm"))
  ;; Optional per-vault overrides:
  ;; (setq org-llm-org-dir "~/org/")
  ;; (setq org-llm-db "~/.local/share/org-llm/org-llm.db")
  )
```

### Vanilla Emacs (`use-package` + `straight.el`)

```elisp
(use-package org-llm
  :straight (:host github :repo "daniel2501/org-llm"
             :files ("extensions/emacs/*.el"))
  :after org)
```

## Phase 16.3+ scope (the actual workflow)

| Command                  | Binding (Doom) | What it does                                          |
|--------------------------+----------------+-------------------------------------------------------|
| `org-llm-launch-opencode`| `SPC l o`      | Run `org-llm launch` with this Emacs's pinned workspace |
| `org-llm-capture`        | `SPC l c`      | `capture-from-region` if region active, else prompt   |
| `org-llm-search`         | `SPC l s`      | Run `search`, open buffer with hits                   |
| `org-llm-ask`            | `SPC l a`      | Run `ask`, render result in a new buffer              |
| `org-llm-jump-to-id`     | `SPC l j`      | Resolve `[[id:UUID]]` at point via the DB             |
| `org-llm-walk`           | `SPC l w`      | Trigger the walk-and-teach flow on the current note   |
| `org-llm-doctor`         | `SPC l d`      | Run doctor + show panel                               |
| `org-llm-life-support`   | `SPC l L`      | Inline life-support readout                           |

## Layout

```
extensions/emacs/
├── org-llm.el          — top-level: defcustom + helpers + entrypoints
├── org-llm-mcp.el      — talk to the org-llm MCP stdio server from elisp
├── org-llm-doom.el     — SPC-l keybinds for Doom Emacs
└── README.md
```

## Status

**Phase 16.0** (this commit): scaffolding only.  `org-llm.el` ships with
`defcustom` for the binary path, ORG_LLM_ORG_DIR / ORG_LLM_DB overrides,
and a `org-llm-version` smoke command.  The interactive verbs above
land in Phase 16.3+.
