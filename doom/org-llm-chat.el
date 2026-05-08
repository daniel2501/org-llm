;;; org-llm-chat.el --- Org-mode-aware chat surface for org-llm -*- lexical-binding: t; -*-

;; v0 of DEC-015 — org-mode-aware chat surface (Path 1: native Emacs UI).
;;
;; The chat is a real org-mode file. Each turn is a level-2 heading
;; (`** Me` / `** @<agent>`). C-c C-c on `** Me` submits the prompt and
;; appends a sibling `** @<agent>` heading with the response. Code blocks,
;; tables, [[id:UUID]] links — all real org primitives, so `org-babel-tangle`,
;; `[[id:…]]` resolution, refile, agenda all work for free.
;;
;; Path 1 wiring (v0):
;;   - HTTP transport: shells out to `org-llm ask <prompt>` via `make-process`.
;;     This routes through the same cloud-or-local model selection as everywhere
;;     else and stays Emacs-async (UI never blocks).
;;   - `@<agent>` prefix detected client-side; the prompt is forwarded
;;     verbatim so the proxy's `intercept_agent_prefix` (when running) can
;;     swap personas. When the proxy isn't reachable we still surface the
;;     agent label in the response heading.
;;   - Streaming is not yet on the wire — v0 inserts a `/thinking…/`
;;     placeholder, replaces it on completion. SSE/chunked is a v0.1 swap
;;     against the same `org-llm-chat--call-backend` seam.
;;
;; Reuses from `org-llm.el`:
;;   - `org-llm-binary` — CLI binary path.
;;   - `org-llm--env` — PATH-augmented process environment.
;;   - `org-llm-org-dir-resolved` — vault root.
;;
;; Assumptions:
;;   - The user has org-llm installed (`org-llm doctor` passes).
;;   - The agent registry is seeded. The chat surface does NOT presume
;;     any specific agent roster — it dispatches whatever `@<name>'
;;     prefix the user types, and the proxy / backend resolves it
;;     against the core registry (`org_llm/agents/_builtins.py' +
;;     user customisations via `org-llm agents --tangle').
;;   - Optional: `org-llm launch` is running somewhere (for proxy-side
;;     @-prefix interception). v0 works without it; agent dispatch
;;     degrades gracefully to "ask" semantics.

(require 'org)
(require 'json)
(require 'cl-lib)
(require 'subr-x)
(require 'url)
(require 'url-http)

;; Soft-require the companion package so we share `org-llm-binary` etc.
;; If it's not loaded yet (e.g. tests), define minimal fallbacks.
(unless (featurep 'org-llm)
  (ignore-errors (require 'org-llm nil t)))

(defgroup org-llm-chat nil
  "Org-mode-aware chat surface for org-llm."
  :group 'org-llm)

(defcustom org-llm-chat-sessions-dir
  (expand-file-name "~/org/.opencode/sessions/")
  "Directory where chat sessions are persisted as .org files."
  :type 'directory
  :group 'org-llm-chat)

(defcustom org-llm-chat-pins-file
  (expand-file-name "~/org/chat-pins.org")
  "File where pinned chat turns are appended as reference cards."
  :type 'file
  :group 'org-llm-chat)

(defcustom org-llm-chat-default-agent nil
  "Agent name used when the prompt body has no `@<agent>' prefix.
Default is nil — every turn must specify `@<name>'. Set to a
specific handle (e.g. `\"crew\"' or `\"picard\"') in your local
config to route un-prefixed prompts somewhere by default. The
chat surface deliberately ships with no opinionated roster — the
agent registry is owned by the core (see
`org_llm/agents/_builtins.py' and `~/org/.opencode/opencode.json'
which `org-llm launch' regenerates) and is reconfigurable by the
user, so we don't hardcode handles here."
  :type '(choice (const :tag "No default (require @prefix)" nil)
                 (string :tag "Agent name"))
  :group 'org-llm-chat)

(defcustom org-llm-chat-known-agents '()
  "Optional hint list of known agent handles for `@<name>' prefix
detection (used only as a soft hint — unknown names still pass
through, and the proxy/backend reports invalid handles).

Default is empty: the chat surface does not presume any agent
roster. The authoritative agent list is the core registry
(`org_llm/agents/_builtins.py' + user customisations); set this
in your local config to mirror it if you want the hint behaviour."
  :type '(repeat string)
  :group 'org-llm-chat)

(defcustom org-llm-chat-binary
  (or (and (boundp 'org-llm-binary) org-llm-binary)
      (expand-file-name "~/.local/bin/org-llm"))
  "Path to the org-llm CLI binary."
  :type 'file
  :group 'org-llm-chat)

(defcustom org-llm-chat-proxy-port-file
  (expand-file-name "~/.local/share/org-llm/proxy-port")
  "File the running llm-proxy writes its bound port to.
DEC-015 v0.1 — when this file exists and the port is reachable,
the chat surface speaks OpenAI-compat HTTP directly to the proxy
via `url-retrieve' instead of shelling `org-llm ask'. When missing
or unreachable, we fall back to the v0 shell-out path."
  :type 'file
  :group 'org-llm-chat)

(defcustom org-llm-chat-default-model "claude-sonnet-4.6"
  "Model id forwarded to the proxy in the OpenAI-compat request.
The proxy's interceptors (DEC-008 — proxy-seam @-prefix swap) may
override this when an `@<agent>' prefix is detected; the value
here is the no-prefix default. Set to nil to omit the field."
  :type '(choice (const :tag "No model field" nil)
                 (string :tag "Model id"))
  :group 'org-llm-chat)

(defcustom org-llm-chat-proxy-timeout 1.0
  "Seconds to wait for a TCP connect to the proxy before falling
back to shell-out. Kept short — if the proxy isn't running the
fallback path needs to engage promptly."
  :type 'number
  :group 'org-llm-chat)

(defcustom org-llm-chat-sidebar-status-file
  (expand-file-name "~/org/.opencode/sidebar-status.json")
  "Path to the launch-time sidebar status JSON.
Used as a FALLBACK when the `org-llm telemetry' CLI verb isn't
reachable — see `org-llm-chat-telemetry-source'. By itself this
file's vitals are stale (launch-time only); the CLI verb is the
single source of truth for fresh data."
  :type '(choice (const :tag "Disabled" nil) (file :tag "Path"))
  :group 'org-llm-chat)

(defcustom org-llm-chat-telemetry-source 'cli
  "How to fetch live telemetry for system-message injection.

`cli'  — Shell out to `org-llm telemetry --pretty' (the
         single source of truth — fresh probes + DB queries +
         live proxy state). 1–2s startup overhead is masked by
         the cloud round-trip. Falls back to `json-file' on any
         CLI error.
`json-file' — Read `org-llm-chat-sidebar-status-file' directly
         (legacy path; vitals are stale)."
  :type '(choice (const :tag "CLI verb (recommended)" cli)
                 (const :tag "JSON file (legacy/fallback)" json-file))
  :group 'org-llm-chat)

(defcustom org-llm-chat-telemetry-cli
  (or (and (boundp 'org-llm-binary) org-llm-binary)
      (expand-file-name "~/.local/bin/org-llm"))
  "Path to the `org-llm' CLI binary used by the telemetry source.
Only consulted when `org-llm-chat-telemetry-source' is `cli'."
  :type 'file
  :group 'org-llm-chat)

(defcustom org-llm-chat-telemetry-cache-ttl 30
  "Seconds to cache the result of `org-llm telemetry' between calls.
Avoids paying CLI startup latency on every chat turn while still
keeping the data fresh (default 30s — values like memory + battery
move on a slower scale than that). Set to 0 to disable caching."
  :type 'number
  :group 'org-llm-chat)

(defvar org-llm-chat--telemetry-cache nil
  "Cons of (TIMESTAMP . PARSED-ALIST) for the last successful CLI fetch.")

(defcustom org-llm-chat-inject-sidebar t
  "When non-nil, prepend a system message with live sidebar telemetry
to every chat backend call. Combined with the proxy's persona-swap
(DEC-008 — proxy-seam @-prefix swap), this gives the agent ground
truth instead of letting it free-associate.

The injected message lands at `messages[1]' so the proxy's
`intercept_agent_prefix' (which overwrites `messages[0]') leaves
it intact. Toggle off if the extra context tokens hurt latency
on a small local model."
  :type 'boolean
  :group 'org-llm-chat)

(defcustom org-llm-chat-agent-glyphs '()
  "Per-agent glyph prepended to response headings.
Format is `((AGENT . GLYPH) …)' — keys are bare agent handles
(no `@'), values are strings (single chars, emoji, or any text;
may carry text properties like `:family' for font-faces).

Default is empty: the chat surface deliberately ships with NO
opinionated roster. The authoritative agent registry lives in
the core (`org_llm/agents/_builtins.py' + user customisations
via `org-llm agents --tangle'). Coupling the elisp-side
glyph map to specific handles (picard/spock/etc.) would lock
the chat surface against agent renames or user-added agents.

Configure this in your local Doom config, e.g.:

  (after! org-llm-chat
    (setq org-llm-chat-agent-glyphs
          '((\"picard\" . \"Δ\")
            (\"spock\"  . \"🖖\")
            ...)))

Unknown agents (no entry here) fall back to a bare `@<agent>'
heading — works without any glyph mapping at all."
  :type '(alist :key-type string :value-type string)
  :group 'org-llm-chat)

(defcustom org-llm-chat-user-heading "Me"
  "Heading text for the user's turn (after the leading `** ').
Default is the v0 plain `Me' — neutral, no theming, works in any
font. The shell-prompt-style line where you actually type lives
on a separate line below (see `org-llm-chat-prompt-marker').

Themed alternatives (Trek styling, etc.) belong in your personal
Doom config, e.g.:

    (after! org-llm-chat
      (setq org-llm-chat-user-heading \"🪪 Captain ●●●●\"))

Older sessions written under any historical default — `Me',
`🖖 Captain …', `🪪 Captain …', etc. — are still parsed
correctly by `--user-heading-regex' (permissive)."
  :type 'string
  :group 'org-llm-chat)

(defcustom org-llm-chat-prompt-marker "❯  "
  "Prefix on the line where the user types their prompt.
Inserted on a new line under the user-turn heading. The arrow
gives the active line a shell-prompt feel; the trailing whitespace
gives the cursor visual breathing room from the chevron. Stripped
(with any surrounding whitespace) from the body before the prompt
is sent to the backend so the LLM sees a clean message.

Set to nil or empty string to disable (the body is then read
from the line directly under the heading, no marker prefix)."
  :type '(choice (const :tag "Disabled" nil) (string :tag "Marker"))
  :group 'org-llm-chat)

(defcustom org-llm-chat-prompt-frame t
  "When non-nil, draw an LCARS-style outline around the active
user-prompt area (the trailing `** 🖖 Captain ❯❯❯❯' heading and
its body). The frame is purely visual — it renders through
overlay before/after strings, so the saved .org file is unchanged.

Lifecycle: drawn at chat-buffer open; cleared on submit so the
agent response renders cleanly under the heading; redrawn around
the auto-appended next-turn heading after the response lands."
  :type 'boolean
  :group 'org-llm-chat)

(defcustom org-llm-chat-heading-font nil
  "Font family applied to the agent + user turn headings.
When non-nil, `org-llm-chat-mode' buffer-locally remaps the
`org-level-2' face to use this family, giving the chat headings
a custom look without modifying the underlying .org file (the
file remains plain text — only rendering changes).

Default is nil — org-llm itself does NOT bundle Trek fonts (per
DEC-010 — Anti-capitalist FOSS, ambiguous-license assets are
excluded from the repo). To use a Trek font, install one
locally on your machine (your call, not the project's), e.g.:

  git clone https://github.com/leonawicz/trekfont /tmp/trekfont
  mkdir -p ~/.local/share/fonts/trek
  cp /tmp/trekfont/inst/fonts/*.ttf ~/.local/share/fonts/trek/
  fc-cache -f ~/.local/share/fonts/

Then `M-x customize-variable RET org-llm-chat-heading-font' and
set to e.g. \"Federation\", \"FederationDS9Title\", or \"Final
Frontier\". Verify with `fc-list | grep -i federation' that the
font is visible to fontconfig."
  :type '(choice (const :tag "Default (no remap)" nil)
                 (string :tag "Font family"))
  :group 'org-llm-chat)

(defface org-llm-chat-prompt-frame-face
  '((t :foreground "#ff9c00" :weight bold :family "monospace"
       :inherit nil))
  "Face for the LCARS-orange prompt frame.
Family pinned to `monospace' (and `:inherit nil') so the ▰ bars
render at fixed-width columns even when the heading face has
been remapped to a variable-width font (e.g. FederationDS9Title)
— without that, the top bar with `COMPOSE' label rendered at a
different visual width than the plain bottom bar, making the
frame look misaligned."
  :group 'org-llm-chat)

(defcustom org-llm-chat-shell-agents '()
  "Agent handles that route via `org-llm chat-dispatch' instead of
the normal LLM path. Used to send shell-capable workloads through
an Agor session (which has tool-calling + subprocess execution)
rather than a single-turn LLM call.

Default is empty — opt in by setting this in your local config:

  (after! org-llm-chat
    (setq org-llm-chat-shell-agents
          '(\"engineer\" \"ops\" \"agentsmith\" \"atoz\" \"riker\")))

The agent registry (`org_llm/agents/_builtins.py') is the
authoritative source of which agents have shell capability — this
list is your local opt-in mirror. The CLI verb double-checks
eligibility before actually dispatching, so a typo or stale entry
falls back to the normal path with a clear message."
  :type '(repeat string)
  :group 'org-llm-chat)

(defcustom org-llm-chat-streaming t
  "When non-nil, prefer SSE streaming for proxy backend calls.
DEC-015 v0.2 — when t, the chat surface sends `stream: true` to
the OpenAI-compat proxy and renders incoming `delta.content`
chunks at the placeholder location as they arrive. When nil,
falls back to v0.1 non-streaming `url-retrieve' behaviour.

If SSE setup fails (network-process spawn errors, no proxy port,
etc.) the call automatically degrades to the non-streaming path."
  :type 'boolean
  :group 'org-llm-chat)


;;; ── filename + buffer plumbing ─────────────────────────────────────────────

(defun org-llm-chat--short-id ()
  "Return a 6-char base32-ish session id derived from emacs-uptime + rand."
  (let ((rand (random (* 36 36 36 36 36 36))))
    (substring (format "%06x" rand) 0 6)))

(defun org-llm-chat--session-file (&optional date short-id)
  "Compute the session file path for DATE (YYYY-MM-DD) and SHORT-ID.
Both default to today + a fresh random id."
  (let* ((d  (or date (format-time-string "%Y-%m-%d")))
         (sid (or short-id (org-llm-chat--short-id))))
    (expand-file-name (format "%s-%s.org" d sid)
                      org-llm-chat-sessions-dir)))

(defun org-llm-chat--ensure-session-file (path)
  "Create PATH with the chat skeleton if it doesn't exist."
  (unless (file-directory-p (file-name-directory path))
    (make-directory (file-name-directory path) t))
  (unless (file-exists-p path)
    (with-temp-file path
      (insert "#+TITLE: org-llm chat — "
              (format-time-string "%Y-%m-%d %H:%M") "\n"
              "#+STARTUP: showeverything\n"
              "#+OPTIONS: toc:nil num:nil\n"
              "\n"
              "* Capture from chat — "
              (format-time-string "%Y-%m-%d") "\n"
              "\n"
              "** " org-llm-chat-user-heading "\n"
              (or org-llm-chat-prompt-marker "")))))


;;; ── prompt parsing ─────────────────────────────────────────────────────────

(defun org-llm-chat--apply-glyph-face-overlay (agent heading-start)
  "After inserting an agent heading at HEADING-START, look up AGENT's
glyph in `org-llm-chat-agent-glyphs'. If the glyph string carries
a text-property face (e.g. `:family \"Trekbats\"' for icon fonts),
create an overlay over the glyph characters in the buffer with
that face. Overlay faces win over text-property faces + font-lock-
applied faces — so the family override sticks even after org-mode
fontifies the heading line with `org-level-2'."
  (let ((glyph (cdr (assoc agent org-llm-chat-agent-glyphs))))
    (when (and glyph (stringp glyph) (not (string-empty-p glyph)))
      (let ((face (get-text-property 0 'face glyph)))
        (when face
          (let* ((glyph-start (+ heading-start 3))   ; skip "** "
                 (glyph-end   (+ glyph-start (length glyph)))
                 (ov (make-overlay glyph-start glyph-end)))
            (overlay-put ov 'face face)
            (overlay-put ov 'org-llm-chat-agent-glyph t)))))))

(defun org-llm-chat--agent-heading-text (agent &optional suffix)
  "Return the heading body (after `** ') for AGENT.
Looks up `org-llm-chat-agent-glyphs'; falls back to bare `@AGENT'.
Optional SUFFIX is appended (e.g. ` ERROR') for non-success paths."
  (let* ((name  (or agent "agent"))
         (glyph (or (cdr (assoc name org-llm-chat-agent-glyphs)) ""))
         (lead  (if (string-empty-p glyph) "" (concat glyph " "))))
    (concat lead "@" name (or suffix ""))))

;; ── LCARS prompt frame ─────────────────────────────────────────────────
;;
;; Pure-overlay outline drawn around the active `** 🖖 Captain ❯❯❯❯'
;; heading + its body, signalling to the user "this is the live compose
;; box". When the user submits, the frame is cleared (the agent will
;; render under that heading); after the response finalises and the
;; auto-next-turn heading lands, the frame is redrawn around the new
;; compose region. No buffer text is added — the saved .org file stays
;; identical regardless of whether the frame is on or off.

(defvar-local org-llm-chat--prompt-frame-overlays nil
  "List of overlays currently rendering the LCARS prompt frame.")

(defun org-llm-chat--prompt-frame-pad ()
  "Return leading padding for LCARS frame strings.
When `display-line-numbers-mode' is on, the buffer's line-number
gutter takes N columns to the left of the text area. Overlay
strings render INSIDE the text area, so without compensation the
LCARS top/bottom lines start N columns left of the heading text
they're framing — visually misaligned. Compute the gutter width
in columns and return that many spaces. Returns \"\" when no
gutter is active."
  (cond
   ((not (bound-and-true-p display-line-numbers-mode)) "")
   (t
    (let* ((win (or (get-buffer-window (current-buffer) t)
                    (selected-window)))
           (cols (when (and win (fboundp 'line-number-display-width))
                   (with-selected-window win
                     (line-number-display-width 'columns)))))
      (if (and cols (numberp cols) (> cols 0))
          (make-string (max 0 (round cols)) ?\s)
        "")))))

(defun org-llm-chat--prompt-frame-strings ()
  "Return (TOP . BOTTOM) propertised strings for the prompt frame.
The BOTTOM string includes trailing blank visual lines so the
cursor on the marker line has visual space below it. Without
this, the buffer ends right after the marker and Emacs's auto-
scroll glues the cursor to the visible window bottom — fighting
that with hooks is fragile (evil/Doom hooks keep undoing it).
Letting the visible region extend past the marker via overlay
solves the problem at the source: the cursor naturally lands
mid-window because there's room below."
  (let* ((face 'org-llm-chat-prompt-frame-face)
         (pad  (org-llm-chat--prompt-frame-pad))
         (bar-width 39)  ; total visible width of the LCARS bar
         (label " COMPOSE ")  ; 9 chars including padding spaces
         (lead 5)
         (trail (- bar-width (+ lead (length label))))
         (top-bar (concat (make-string lead ?▰) label (make-string trail ?▰)))
         (bot-bar (make-string bar-width ?▰))
         (top  (propertize (concat pad top-bar "\n") 'face face))
         ;; 12 trailing blank lines: gives cursor at marker enough
         ;; visual padding below that natural scroll keeps it mid-
         ;; window. These are overlay strings — buffer text + saved
         ;; file are unaffected.
         (bot  (concat
                 (propertize (concat "\n" pad bot-bar "\n") 'face face)
                 (make-string 12 ?\n))))
    (cons top bot)))

(defun org-llm-chat--clear-prompt-frame ()
  "Remove all prompt-frame overlays. Idempotent."
  (dolist (ov org-llm-chat--prompt-frame-overlays)
    (when (overlayp ov) (delete-overlay ov)))
  (setq-local org-llm-chat--prompt-frame-overlays nil))

(defun org-llm-chat--draw-prompt-frame ()
  "Outline the trailing user-turn heading + its body with overlays.
Idempotent — clears any prior frame first. No-op when
`org-llm-chat-prompt-frame' is nil or no user heading is found."
  (org-llm-chat--clear-prompt-frame)
  (when org-llm-chat-prompt-frame
    (save-excursion
      (goto-char (point-max))
      (when (re-search-backward
              (org-llm-chat--user-heading-line-regex) nil t)
        (let* ((bounds  (org-llm-chat--prompt-frame-strings))
               (top-pt  (line-beginning-position))
               (bot-pt  (point-max))
               (top-ov  (make-overlay top-pt top-pt))
               (bot-ov  (make-overlay bot-pt bot-pt)))
          (overlay-put top-ov 'before-string (car bounds))
          (overlay-put bot-ov 'after-string  (cdr bounds))
          (setq-local org-llm-chat--prompt-frame-overlays
                      (list top-ov bot-ov)))))))

(defun org-llm-chat--user-heading-regex ()
  "Regex matching a user-turn heading title (without the `** ' prefix).
Accepts the configured `org-llm-chat-user-heading', any past
`🖖 Captain …' / `🪪 Captain …' variant (so older sessions still
parse), and the legacy `Me'. An optional trailing tag like `[Y]'
(used for confirmation hints) is tolerated."
  (concat "\\`\\(?:"
          (regexp-quote org-llm-chat-user-heading)
          "\\|🖖 Captain[^\n]*"
          "\\|🪪 Captain[^\n]*"
          "\\|Me\\)\\(?:\\s-*\\[[A-Za-z0-9?]+\\]\\)?\\'"))

(defun org-llm-chat--user-heading-line-regex ()
  "Anchored line regex for a user-turn heading (for re-search).
Matches `^** <heading>\\s-*$' for the configured heading, any
historical `🖖 Captain …' / `🪪 Captain …' variant, or `Me'."
  (concat "^\\*\\* \\(?:"
          (regexp-quote org-llm-chat-user-heading)
          "\\|🖖 Captain[^\n]*"
          "\\|🪪 Captain[^\n]*"
          "\\|Me\\)\\s-*$"))

(defun org-llm-chat--at-me-heading-p ()
  "True iff point is inside a user-turn heading subtree."
  (save-excursion
    (and (not (org-before-first-heading-p))
         (progn (ignore-errors (org-back-to-heading t)) t)
         (let ((title (nth 4 (org-heading-components))))
           (and title
                (string-match-p (org-llm-chat--user-heading-regex)
                                (string-trim title)))))))

(defun org-llm-chat--current-heading-body ()
  "Return the body text under the current heading (no subheadings stripped).
Trims leading/trailing whitespace, the `:PROPERTIES:' drawer if
present, AND a leading `org-llm-chat-prompt-marker' (e.g. `❯ ')
so the LLM sees a clean prompt."
  (save-excursion
    (org-back-to-heading t)
    (let ((begin (progn (forward-line 1) (point)))
          (end   (save-excursion
                   (outline-next-heading)
                   (point))))
      (let ((raw (buffer-substring-no-properties begin end)))
        ;; Strip a leading PROPERTIES drawer if any
        (when (string-match
               "\\`[ \t]*:PROPERTIES:\\(.\\|\n\\)*?:END:[ \t]*\n?" raw)
          (setq raw (substring raw (match-end 0))))
        ;; Strip the prompt-marker prefix (e.g. `❯ ') so the LLM
        ;; doesn't see it. Tolerant of leading whitespace.
        (when (and org-llm-chat-prompt-marker
                    (not (string-empty-p org-llm-chat-prompt-marker)))
          (let ((mk (regexp-quote
                      (string-trim-right org-llm-chat-prompt-marker))))
            (when (string-match
                    (concat "\\`[ \t]*" mk "[ \t]*") raw)
              (setq raw (substring raw (match-end 0))))))
        (string-trim raw)))))

(defun org-llm-chat-parse-agent-prefix (body)
  "Return (AGENT . REST) from BODY when it starts with `@<agent> …`.
AGENT is the bare name (no `@`). REST is the prompt with the
`@<agent>` token stripped. Returns nil when no prefix matches.
Tolerates leading whitespace and a trailing `!` (force-solo marker
the proxy understands)."
  (when (and body (stringp body))
    (let ((trimmed (string-trim-left body)))
      (when (string-match "\\`@\\([A-Za-z][A-Za-z0-9_-]*\\)\\(!?\\)\\s-+\\(\\(?:.\\|\n\\)*\\)\\'"
                           trimmed)
        (cons (match-string 1 trimmed)
              (string-trim (match-string 3 trimmed)))))))


;;; ── backend call ───────────────────────────────────────────────────────────

(defun org-llm-chat--build-cli-args (agent prompt)
  "Build argv for `org-llm ask` (v0 backend).
AGENT may be nil; when set, we prefix the prompt with `@<agent>`
so a running llm-proxy intercepts. Either way the org-llm binary
runs `ask` with the (possibly prefixed) prompt as argument."
  (let ((q (if (and agent (not (string-empty-p agent)))
               (format "@%s %s" agent prompt)
             prompt)))
    (list org-llm-chat-binary "ask" q)))

(defvar-local org-llm-chat--pending-marker nil
  "Buffer-local marker pointing at the placeholder heading awaiting a response.")

(defvar-local org-llm-chat--pending-process nil
  "The async process producing the current response, if any.")

;;; ── thinking spinner ─────────────────────────────────────────────────────

(defconst org-llm-chat--spinner-frames
  '("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
  "Braille spinner frames cycled while waiting on a response.")

(defvar-local org-llm-chat--spinner-timer nil
  "Active spinner timer object; nil when no response is in flight.")

(defvar-local org-llm-chat--spinner-marker nil
  "Marker on the BOL of the placeholder line being animated.")

(defvar-local org-llm-chat--spinner-idx 0
  "Current index into `org-llm-chat--spinner-frames'.")

(defun org-llm-chat--spinner-tick (buf)
  "Replace the placeholder line in BUF with the next spinner frame."
  (when (buffer-live-p buf)
    (with-current-buffer buf
      (let ((mk org-llm-chat--spinner-marker))
        (when (and mk (marker-buffer mk))
          (save-excursion
            (goto-char mk)
            (let ((inhibit-read-only t)
                  (frame (nth (mod org-llm-chat--spinner-idx
                                    (length org-llm-chat--spinner-frames))
                              org-llm-chat--spinner-frames)))
              (delete-region (line-beginning-position)
                             (line-end-position))
              (insert frame " thinking…")))
          (setq-local org-llm-chat--spinner-idx
                      (1+ org-llm-chat--spinner-idx)))))))

(defun org-llm-chat--start-spinner (placeholder-marker)
  "Start the spinner animating the line after PLACEHOLDER-MARKER.
PLACEHOLDER-MARKER points at the `** @<agent>' heading line; the
spinner animates the line below it (the `/thinking…/' line that
`--insert-placeholder' just wrote)."
  (org-llm-chat--stop-spinner)
  (let ((line-marker (save-excursion
                       (goto-char placeholder-marker)
                       (forward-line 1)
                       (point-marker))))
    (set-marker-insertion-type line-marker nil)
    (setq-local org-llm-chat--spinner-marker line-marker)
    (setq-local org-llm-chat--spinner-idx 0)
    (let ((buf (current-buffer)))
      (setq-local org-llm-chat--spinner-timer
                  (run-at-time 0 0.1
                                #'org-llm-chat--spinner-tick buf)))))

(defun org-llm-chat--stop-spinner ()
  "Cancel the spinner timer + clear its state. Idempotent."
  (when (timerp org-llm-chat--spinner-timer)
    (cancel-timer org-llm-chat--spinner-timer))
  (setq-local org-llm-chat--spinner-timer nil)
  (when (markerp org-llm-chat--spinner-marker)
    (set-marker org-llm-chat--spinner-marker nil))
  (setq-local org-llm-chat--spinner-marker nil)
  (setq-local org-llm-chat--spinner-idx 0))

(defun org-llm-chat--insert-placeholder (agent)
  "Insert a `** @<agent>' heading + `/thinking…/' line after the current
user-turn subtree, start the spinner, and return a marker at the start
of the placeholder heading line."
  (save-excursion
    (org-back-to-heading t)
    (org-end-of-subtree t t)
    (unless (bolp) (insert "\n"))
    (let* ((start (point-marker))
           (heading-pos (point)))
      (insert (concat "** " (org-llm-chat--agent-heading-text agent)
                       "\n/thinking…/\n"))
      (org-llm-chat--apply-glyph-face-overlay agent heading-pos)
      (org-llm-chat--start-spinner start)
      start)))

(defun org-llm-chat--replace-placeholder (marker rendered)
  "Replace the placeholder heading at MARKER with RENDERED (string)."
  (when (and marker (marker-buffer marker))
    (with-current-buffer (marker-buffer marker)
      (save-excursion
        (goto-char marker)
        (let ((begin (point))
              (end   (save-excursion
                       (forward-line 1)         ; past heading
                       (if (re-search-forward "^\\*\\* " nil t)
                           (line-beginning-position)
                         (point-max)))))
          (delete-region begin end)
          (goto-char begin)
          (insert rendered)
          (unless (bolp) (insert "\n")))))))

(defun org-llm-chat--render-response (agent body)
  "Format BODY (raw text from the backend) as an org subtree under
`** @<agent>`. Plain text becomes paragraph; we DO NOT wrap text in
src-blocks. Code fences are left intact (the model is asked to
output org-flavoured src blocks; if it emits markdown ```…``` we
convert below)."
  (let* ((normalised (org-llm-chat--markdown->org body))
         (txt (string-trim (or normalised ""))))
    (concat "** " (org-llm-chat--agent-heading-text agent) "\n"
            (if (string-empty-p txt) "(empty response)" txt)
            "\n")))

(defun org-llm-chat--markdown->org (text)
  "Best-effort convert markdown ```fences``` to org-mode src blocks.
Leaves the rest of TEXT alone. Headings and links aren't touched —
opencode/agent prompts already encourage org output."
  (let ((case-fold-search nil)
        (out text))
    (when (and out (string-match-p "```" out))
      (setq out
            (replace-regexp-in-string
             "```\\([a-zA-Z0-9_+-]*\\)\n\\(\\(?:.\\|\n\\)*?\\)```"
             (lambda (m)
               ;; Re-match locally so we don't depend on outer match-data.
               (if (string-match
                    "\\````\\([a-zA-Z0-9_+-]*\\)\n\\(\\(?:.\\|\n\\)*?\\)```\\'"
                    m)
                   (let* ((lang (match-string 1 m))
                          (body (or (match-string 2 m) "")))
                     (format "#+begin_src %s\n%s#+end_src"
                             (if (and lang (not (string-empty-p lang)))
                                 lang
                               "text")
                             (if (and (> (length body) 0)
                                      (eq (aref body (1- (length body))) ?\n))
                                 body
                               (concat body "\n"))))
                 m))
             out t t)))
    out))


;;; ── submit + response handling ─────────────────────────────────────────────

;;;###autoload
(defun org-llm-chat ()
  "Open (or create + pop to) a chat buffer.
Buffer is a real org file under `org-llm-chat-sessions-dir' so
all org primitives (refile, tangle, links, agenda) work for free.
Bind `C-c C-c' under `** Me' to submit."
  (interactive)
  (let* ((path (org-llm-chat--session-file)))
    (org-llm-chat--ensure-session-file path)
    (let ((buf (find-file-noselect path)))
      (with-current-buffer buf
        (unless (derived-mode-p 'org-mode) (org-mode))
        (auto-save-mode 1)
        (org-llm-chat-mode 1))
      (pop-to-buffer buf)
      (with-current-buffer buf
        (org-llm-chat--goto-compose-position)
        (org-llm-chat--draw-prompt-frame)))))

;;;###autoload
(defun org-llm-chat-submit ()
  "Submit the prompt under the current `** Me' heading.
The body is sent to the backend; a sibling `** @<agent>' heading
is appended with the response. Auto-saves on completion."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (save-excursion
    (ignore-errors (org-back-to-heading t))
    (let* ((title (nth 4 (org-heading-components)))
           (is-me (and title
                       (string-match-p (org-llm-chat--user-heading-regex)
                                        (string-trim title)))))
      (unless is-me
        (user-error
         "Place point under a `** %s' heading to submit (got %S)"
         org-llm-chat-user-heading title))))
  (let* ((body  (org-llm-chat--current-heading-body))
         (parsed (org-llm-chat-parse-agent-prefix body))
         (agent  (or (and parsed (car parsed))
                     org-llm-chat-default-agent))
         (prompt (or (and parsed (cdr parsed)) body)))
    (when (string-empty-p (string-trim (or prompt "")))
      (user-error "Prompt body is empty — write your question under `** Me'"))
    (org-llm-chat--clear-prompt-frame)
    (let ((marker (org-llm-chat--insert-placeholder agent)))
      (setq-local org-llm-chat--pending-marker marker)
      (org-llm-chat--call-backend agent prompt marker))))

(defun org-llm-chat--read-proxy-port ()
  "Read the running proxy's port from `org-llm-chat-proxy-port-file'.
Returns an integer port or nil if the file is missing/empty/unreadable.
DEC-015 v0.1 — the file is written atomically by `start_proxy'."
  (let ((path (expand-file-name org-llm-chat-proxy-port-file)))
    (when (file-readable-p path)
      (condition-case _err
          (with-temp-buffer
            (insert-file-contents path)
            (let* ((raw (string-trim (buffer-string)))
                   (n (and (string-match-p "\\`[0-9]+\\'" raw)
                           (string-to-number raw))))
              (when (and n (> n 0) (< n 65536))
                n)))
        (error nil)))))

(defun org-llm-chat--proxy-reachable-p (port)
  "Return non-nil if a TCP connect to 127.0.0.1:PORT succeeds within
`org-llm-chat-proxy-timeout' seconds. Best-effort liveness probe."
  (when (and port (integerp port))
    (condition-case _err
        (let ((proc (make-network-process
                     :name "org-llm-chat-probe"
                     :host "127.0.0.1"
                     :service port
                     :nowait nil
                     :noquery t)))
          (when (process-live-p proc)
            (delete-process proc)
            t))
      (error nil))))

(defun org-llm-chat--parse-json-region ()
  "Parse the current buffer (point at start) as JSON → alist.
Uses string keys, alist objects, list arrays — matches what the
sidebar formatter expects."
  (let ((json-object-type 'alist)
        (json-array-type  'list)
        (json-key-type    'string))
    (json-read)))

(defun org-llm-chat--telemetry-from-cli ()
  "Run `org-llm telemetry' synchronously + parse stdout.
Returns the parsed alist or nil on any error (CLI missing, exit
≠0, JSON broken, etc.). Honors `org-llm-chat-telemetry-cache-ttl'
so successive calls within the TTL window reuse the prior result."
  (let* ((bin org-llm-chat-telemetry-cli)
         (now (float-time))
         (cache org-llm-chat--telemetry-cache)
         (cached-ts   (car-safe cache))
         (cached-data (cdr-safe cache)))
    (cond
     ;; Cache hit within TTL.
     ((and cached-data
           cached-ts
           (> org-llm-chat-telemetry-cache-ttl 0)
           (< (- now cached-ts) org-llm-chat-telemetry-cache-ttl))
      cached-data)
     ;; CLI missing or unreadable — caller falls back to JSON file.
     ((not (and bin (file-executable-p bin)))
      nil)
     (t
      (condition-case _err
          (with-temp-buffer
            (let ((rc (call-process bin nil t nil "telemetry")))
              (when (zerop rc)
                (goto-char (point-min))
                (let ((data (org-llm-chat--parse-json-region)))
                  (when data
                    (setq org-llm-chat--telemetry-cache
                          (cons now data))
                    data)))))
        (error nil))))))

(defun org-llm-chat--telemetry-from-file ()
  "Legacy path: parse the sidebar JSON file directly.
Used as a fallback when the CLI verb isn't reachable. The vitals
in this file are launch-time only — see `org-llm-chat-sidebar-
status-file' docstring."
  (when (and org-llm-chat-sidebar-status-file
             (file-readable-p org-llm-chat-sidebar-status-file))
    (condition-case _err
        (with-temp-buffer
          (insert-file-contents org-llm-chat-sidebar-status-file)
          (goto-char (point-min))
          (org-llm-chat--parse-json-region))
      (error nil))))

(defun org-llm-chat--read-sidebar-status ()
  "Return live telemetry as an alist, or nil.
Tries the CLI verb first (fresh probes, single source of truth);
falls back to the launch-time JSON file when CLI fails or is
unreachable. Returns nil when injection is disabled."
  (when org-llm-chat-inject-sidebar
    (or (and (eq org-llm-chat-telemetry-source 'cli)
             (org-llm-chat--telemetry-from-cli))
        (org-llm-chat--telemetry-from-file))))

(defun org-llm-chat--fmt-knobs (knobs)
  "Render KNOBS list as `name=level' pairs, comma-joined."
  (when (consp knobs)
    (mapconcat (lambda (k)
                 (let ((name  (cdr (assoc "name"  k)))
                       (level (cdr (assoc "level" k))))
                   (if level (format "%s=%s" name level) name)))
               knobs ", ")))

(defun org-llm-chat--fmt-vitals (vitals)
  "Render VITALS list as `label (status)' pieces, joined by ` | '."
  (when (consp vitals)
    (mapconcat (lambda (v)
                 (let ((label  (cdr (assoc "label"  v)))
                       (status (cdr (assoc "status" v))))
                   (if (and status (not (equal status "nominal")))
                       (format "%s (%s)" label status)
                     label)))
               vitals " | ")))

(defun org-llm-chat--format-sidebar-system-message (status)
  "Format STATUS alist as a tight system-message string for any agent.
Tolerant of missing keys — degrades gracefully so the message is
always well-formed even if the sidebar JSON drops a section."
  (let* ((vault   (cdr (assoc "vault"    status)))
         (act     (cdr (assoc "active"   status)))
         (model   (cdr (assoc "model"    status)))
         (mcp     (cdr (assoc "mcp"      status)))
         (hw      (cdr (assoc "hardware" status)))   ; kept for the future; not formatted here — see VITALS
         (vitals  (cdr (assoc "vitals"   status)))
         (sensors (cdr (assoc "sensors"  status)))
         (alerts  (and sensors (cdr (assoc "recent_alerts" sensors))))
         (activ   (cdr (assoc "activity" status)))
         (tags    (cdr (assoc "top_tags" status)))
         (sd      (cdr (assoc "stardate" status)))
         (n-files (and vault (cdr (assoc "n_files"      vault))))
         (n-nodes (and vault (cdr (assoc "n_nodes"      vault))))
         (n-embed (and vault (cdr (assoc "n_embedded"   vault))))
         (pct-emb (and vault (cdr (assoc "pct_embedded" vault))))
         (org-dir (and vault (cdr (assoc "org_dir"      vault))))
         (palette (and act   (cdr (assoc "palette"      act))))
         (intent  (and act   (cdr (assoc "intent_agent" act))))
         (knobs   (org-llm-chat--fmt-knobs
                    (and act (cdr (assoc "knobs" act)))))
         (m-active (and model (cdr (assoc "active"   model))))
         (m-prov   (and model (cdr (assoc "provider" model))))
         (m-route  (and model (cdr (assoc "route"    model))))
         (m-end    (and model (cdr (assoc "endpoint" model))))
         (mcp-srv  (and mcp   (cdr (assoc "server"     mcp))))
         (mcp-tn   (and mcp   (cdr (assoc "tool_count" mcp))))
         (mcp-cfg  (and mcp   (cdr (assoc "configured" mcp))))
         (ram      (and hw    (cdr (assoc "free_ram_gb" hw))))  ; deprecated alias; not surfaced — see VITALS line
         (act-n    (and activ (cdr (assoc "nodes"       activ))))
         (act-f    (and activ (cdr (assoc "files"       activ))))
         (act-w    (and activ (cdr (assoc "window_days" activ))))
         (vit-line (org-llm-chat--fmt-vitals vitals))
         (tag-line (when (consp tags)
                      (mapconcat (lambda (s) (format "%s" s)) tags ", "))))
    (mapconcat
     #'identity
     (delq nil
       (list
        "[LIVE BRIDGE TELEMETRY — read-only context. Do NOT fabricate beyond these facts.]"
        ""
        (when sd      (format "STARDATE: %s" sd))
        (when vault   (format "VAULT (%s): %s files · %s nodes · %s embedded (%s%%)"
                                (or org-dir "?") (or n-files "?") (or n-nodes "?")
                                (or n-embed "?") (or pct-emb "?")))
        (when activ   (format "ACTIVITY (last %sd): %s nodes / %s files"
                                (or act-w "?") (or act-n "?") (or act-f "?")))
        (when tags    (format "TOP TAGS: %s"
                                (if (and tag-line (not (string-empty-p tag-line)))
                                    tag-line "(none)")))
        (when act     (format "ACTIVE: palette=%s · intent=@%s%s"
                                (or palette "?") (or intent "?")
                                (if (and knobs (not (string-empty-p knobs)))
                                    (concat " · knobs=[" knobs "]") "")))
        (when model   (format "MODEL: %s (%s, %s%s)"
                                (or m-active "?") (or m-prov "?") (or m-route "?")
                                (if m-end (concat " @ " m-end) "")))
        (when mcp     (format "MCP: %s — %s tools%s"
                                (or mcp-srv "?") (or mcp-tn "?")
                                (if mcp-cfg ", configured" "")))
        ;; Note: HARDWARE: <total RAM> GB used to be surfaced here but
        ;; the field is total, not free, and got conflated with the
        ;; per-vital memory line. The VITALS line below carries the
        ;; accurate "X.X / Y.Y GB free" memory data — keep that as the
        ;; only memory signal the model sees.
        (when vit-line (concat "VITALS: " vit-line))
        (when sensors (format "RECENT ALERTS: %s"
                                (if (and alerts (consp alerts))
                                    (format "%d in last cycle" (length alerts))
                                  "(none)")))
        ""
        "When asked about state/status/activity: TRIAGE these facts — lead with the 1–2 items that warrant attention (alerts, anomalies, vault empty, low battery, memory pressure), dismiss the rest as 'otherwise nominal', and offer ONE concrete next action when useful. Do NOT just paraphrase this table. If a fact isn't here, say \"I don't have that in current telemetry\" — do not invent. Keep it tight: 4–6 lines."))
     "\n")))

(defun org-llm-chat--build-proxy-payload (agent prompt &optional streaming)
  "Build the OpenAI-compat JSON body sent to the proxy.
AGENT may be nil. The proxy's `intercept_agent_prefix' (DEC-008
— proxy-seam @-prefix swap) reads the `agent' field server-side
and rewrites the persona/model. We still forward the prefix in
the user message so non-proxy upstreams degrade gracefully.

When STREAMING is non-nil, sets `stream: true' in the payload so
the proxy emits SSE (DEC-015 v0.2).

When `org-llm-chat--read-sidebar-status' returns a non-nil status
alist, the messages array becomes a 3-entry vector:
  [0] persona-slot system message (proxy overwrites)
  [1] live bridge telemetry system message (survives the swap)
  [2] user message
Otherwise the array is the original single-user-message form."
  (let* ((user-text (if (and agent (not (string-empty-p agent)))
                        (format "@%s %s" agent prompt)
                      prompt))
         (status (org-llm-chat--read-sidebar-status))
         (messages
          (if status
              (vector
               '(("role" . "system")
                 ("content" . "(persona slot — proxy overwrites this)"))
               `(("role" . "system")
                 ("content" . ,(org-llm-chat--format-sidebar-system-message
                                 status)))
               `(("role" . "user")
                 ("content" . ,user-text)))
            (vector `(("role" . "user")
                      ("content" . ,user-text)))))
         (payload `(("messages" . ,messages)
                    ("stream" . ,(if streaming t :json-false)))))
    (when org-llm-chat-default-model
      (push `("model" . ,org-llm-chat-default-model) payload))
    (when (and agent (not (string-empty-p agent)))
      (push `("agent" . ,agent) payload))
    (json-encode payload)))

(defun org-llm-chat--extract-openai-text (json-text)
  "Pull the assistant message text from an OpenAI-compat response.
Returns the content string or nil if the shape doesn't match."
  (condition-case _err
      (let* ((parsed (let ((json-object-type 'alist)
                            (json-array-type  'list)
                            (json-key-type    'string))
                       (json-read-from-string json-text)))
             (choices (cdr (assoc "choices" parsed)))
             (first   (and choices (car choices)))
             (msg     (and first (cdr (assoc "message" first))))
             (content (and msg (cdr (assoc "content" msg)))))
        content)
    (error nil)))

(defun org-llm-chat--dispatch-via-agor (agent prompt marker)
  "Route AGENT + PROMPT to `org-llm chat-dispatch' (Agor path).
Shells out async, parses the JSON response, renders content at
MARKER. Falls back to the normal LLM path if the CLI returns
`eligible: false' (agent registry doesn't grant shell)."
  (let* ((cli (or org-llm-chat-telemetry-cli
                   (expand-file-name "~/.local/bin/org-llm")))
         (buf-name (format " *org-llm-chat-dispatch-%d*" (random)))
         (cb-buf (current-buffer))
         (proc-buf (generate-new-buffer buf-name))
         (proc (make-process
                 :name "org-llm-chat-dispatch"
                 :buffer proc-buf
                 :command (list cli "chat-dispatch" agent prompt)
                 :noquery t
                 :sentinel
                 (lambda (proc _event)
                   (when (memq (process-status proc) '(exit signal))
                     (let ((rc  (process-exit-status proc))
                           (out (with-current-buffer (process-buffer proc)
                                   (buffer-string))))
                       (kill-buffer (process-buffer proc))
                       (with-current-buffer cb-buf
                         (org-llm-chat--dispatch-finalise
                           agent prompt marker rc out))))))))
    (setq-local org-llm-chat--pending-process proc)))

(defun org-llm-chat--dispatch-finalise (agent prompt marker rc raw)
  "Render the chat-dispatch JSON response. On `eligible: false'
fall back to the normal `--call-backend' path so the user
doesn't see a routing failure as a dead end."
  (condition-case err
      (let* ((json-object-type 'alist)
             (json-array-type  'list)
             (json-key-type    'string)
             (parsed (json-read-from-string raw))
             (eligible   (cdr (assoc "eligible"   parsed)))
             (dispatched (cdr (assoc "dispatched" parsed)))
             (content    (or (cdr (assoc "content" parsed)) "")))
        (cond
         ((not (eq eligible t))
          ;; Ineligible — fall through to normal LLM path. Keep
          ;; same marker; --call-backend will refresh it.
          (org-llm-chat--call-backend-default agent prompt marker))
         (t
          (org-llm-chat--finalise-response
            marker agent content (if (eq dispatched t) 0 0)))))
    (error
     (org-llm-chat--finalise-response
       marker agent
       (format "ERROR parsing chat-dispatch JSON: %s\n\nRAW:\n%s"
               err raw)
       -1))))

(defun org-llm-chat--call-backend-default (agent prompt marker)
  "The non-Agor backend path — proxy SSE / non-streaming / shell.
Extracted so `--dispatch-via-agor' can fall back here when the
CLI deems an agent ineligible."
  (let ((port (org-llm-chat--read-proxy-port)))
    (cond
     ((and port (org-llm-chat--proxy-reachable-p port)
            org-llm-chat-streaming)
      (condition-case _err
          (org-llm-chat--call-backend-proxy-sse agent prompt marker port)
        (error
         (org-llm-chat--call-backend-proxy agent prompt marker port))))
     ((and port (org-llm-chat--proxy-reachable-p port))
      (org-llm-chat--call-backend-proxy agent prompt marker port))
     (t
      (org-llm-chat--call-backend-shell agent prompt marker)))))

(defun org-llm-chat--call-backend (agent prompt marker)
  "Call the backend ASYNC for AGENT + PROMPT, replacing MARKER on completion.
Routes to Agor (`--dispatch-via-agor') when AGENT is in
`org-llm-chat-shell-agents' — that path runs the agent in an
Agor session with tool-calling + shell access. Otherwise routes
to the normal LLM path (proxy SSE / non-streaming / shell-out
fallback) via `--call-backend-default'."
  (cond
   ((and org-llm-chat-shell-agents
         (member agent org-llm-chat-shell-agents))
    (org-llm-chat--dispatch-via-agor agent prompt marker))
   (t
    (org-llm-chat--call-backend-default agent prompt marker))))


(defun org-llm-chat--call-backend-proxy (agent prompt marker port)
  "Send the prompt to the local proxy at PORT via `url-retrieve'.
Async by design — the callback edits the chat buffer in-place,
replacing the `/thinking…/' placeholder under MARKER."
  (let* ((url (format "http://127.0.0.1:%d/v1/chat/completions" port))
         (url-request-method "POST")
         (url-request-extra-headers
          '(("Content-Type" . "application/json")))
         (url-request-data
          (encode-coding-string
           (org-llm-chat--build-proxy-payload agent prompt) 'utf-8))
         (buf (current-buffer)))
    (condition-case err
        (url-retrieve
         url
         (lambda (status &rest _)
           (let ((rc 0)
                 (raw ""))
             (cond
              ((plist-get status :error)
               (setq rc -1
                     raw (format "proxy error: %S"
                                 (plist-get status :error))))
              (t
               ;; Skip past HTTP headers to body
               (goto-char (point-min))
               (when (re-search-forward "\r?\n\r?\n" nil t)
                 (let* ((body (buffer-substring-no-properties
                               (point) (point-max)))
                        (text (org-llm-chat--extract-openai-text body)))
                   (setq raw (or text body))))))
             (let ((response-buf (current-buffer)))
               (when (buffer-live-p buf)
                 (with-current-buffer buf
                   (org-llm-chat--finalise-response
                    marker agent raw rc)))
               (when (buffer-live-p response-buf)
                 (let ((inhibit-message t)) (kill-buffer response-buf))))))
         nil t t)
      (error
       (org-llm-chat--finalise-response
        marker agent (format "ERROR calling proxy: %s" err) -1)))))

;;; ── SSE streaming (DEC-015 v0.2) ──────────────────────────────────────────

(defvar-local org-llm-chat--sse-process nil
  "The active SSE network process, if any.")

(defvar-local org-llm-chat--sse-state nil
  "Plist tracking the in-flight SSE stream:
  :marker        — placeholder marker to render under
  :agent         — agent name for the heading
  :buffer        — chat buffer (where to render)
  :inserted-pos  — marker for the running insertion point inside chat buffer
  :raw-buffer    — accumulating raw SSE bytes (string)
  :header-done   — t once we've consumed HTTP response headers
  :content       — accumulated content text rendered so far
  :done          — t once we've seen `data: [DONE]'
  :rc            — final exit code (0 ok, -1 error)
  :error         — error message if rc < 0")

(defun org-llm-chat--sse-parse-data-chunk (line)
  "Parse a single `data: {json}' SSE LINE.
Returns the assistant `delta.content' string when present, or
nil. Tolerant of `data: [DONE]', empty data, malformed JSON."
  (when (and (stringp line)
             (string-prefix-p "data:" line))
    (let ((payload (string-trim (substring line 5))))
      (cond
       ((string-empty-p payload) nil)
       ((string= payload "[DONE]") nil)
       (t
        (condition-case _err
            (let* ((json-object-type 'alist)
                   (json-array-type  'list)
                   (json-key-type    'string)
                   (obj (json-read-from-string payload))
                   (choices (cdr (assoc "choices" obj)))
                   (first   (and choices (car choices)))
                   (delta   (and first (cdr (assoc "delta" first))))
                   (content (and delta (cdr (assoc "content" delta)))))
              (and (stringp content) content))
          (error nil)))))))

(defun org-llm-chat--sse-done-marker-p (line)
  "Return non-nil iff LINE is the SSE terminator `data: [DONE]'."
  (and (stringp line)
       (string-match-p "\\`data:\\s-*\\[DONE\\]\\s-*\\'" line)))

(defun org-llm-chat--sse-init-render (state)
  "Initialise the chat buffer for SSE rendering.
Replaces the `/thinking…/' placeholder with the heading + an
empty body, and stores the running insertion marker on STATE.
Stops the thinking spinner — first byte arrived."
  (let ((marker (plist-get state :marker))
        (agent  (plist-get state :agent))
        (buf    (plist-get state :buffer)))
    (when (and marker (marker-buffer marker) (buffer-live-p buf))
      (with-current-buffer buf
        (org-llm-chat--stop-spinner)
        (save-excursion
          (goto-char marker)
          (let ((begin (point))
                (end   (save-excursion
                         (forward-line 1)
                         (if (re-search-forward "^\\*\\* " nil t)
                             (line-beginning-position)
                           (point-max)))))
            (delete-region begin end)
            (goto-char begin)
            (let ((heading-pos (point)))
              (insert (concat "** "
                               (org-llm-chat--agent-heading-text agent)
                               "\n"))
              (org-llm-chat--apply-glyph-face-overlay agent heading-pos))
            (let ((ins (point-marker)))
              (set-marker-insertion-type ins t)
              (plist-put state :inserted-pos ins))))))))

(defun org-llm-chat--sse-append-delta (state delta)
  "Append DELTA text to the live chat buffer at STATE's insertion point."
  (let ((ins (plist-get state :inserted-pos))
        (buf (plist-get state :buffer)))
    (when (and (stringp delta) (not (string-empty-p delta))
               ins (marker-buffer ins) (buffer-live-p buf))
      (with-current-buffer buf
        (save-excursion
          (goto-char ins)
          (insert delta)
          (set-marker ins (point))))
      (plist-put state :content
                 (concat (or (plist-get state :content) "") delta)))))

(defun org-llm-chat--sse-process-buffer (state)
  "Drain newline-terminated SSE lines from STATE's :raw-buffer.
For each `data:' line, append the parsed delta to the chat buffer.
Sets :done when `data: [DONE]' is observed. Leaves any partial
trailing line in the buffer for the next chunk."
  (let ((raw (or (plist-get state :raw-buffer) "")))
    (while (string-match "\\(.*\\)\n" raw)
      (let ((line (match-string 1 raw)))
        (setq raw (substring raw (match-end 0)))
        (cond
         ((org-llm-chat--sse-done-marker-p line)
          (plist-put state :done t))
         ((string-prefix-p "data:" (string-trim-left line))
          (let ((delta (org-llm-chat--sse-parse-data-chunk
                        (string-trim-left line))))
            (when delta
              (org-llm-chat--sse-append-delta state delta)))))))
    (plist-put state :raw-buffer raw)))

(defun org-llm-chat--sse-finalise (state)
  "Finalise an SSE stream: post-process the rendered body (markdown
fence → org src) and auto-save the chat buffer. Idempotent."
  (let* ((buf (plist-get state :buffer))
         (ins (plist-get state :inserted-pos))
         (rc  (or (plist-get state :rc) 0))
         (agent (plist-get state :agent))
         (content (or (plist-get state :content) "")))
    (when (buffer-live-p buf)
      (with-current-buffer buf
        (cond
         ((zerop rc)
          ;; Convert any markdown fences in the rendered body to
          ;; org src blocks. We replace the rendered region in
          ;; place — the streamed text was inserted verbatim.
          (when (and ins (marker-buffer ins))
            (save-excursion
              (let* ((normalised (org-llm-chat--markdown->org content))
                     (begin (save-excursion
                              (goto-char ins)
                              ;; Walk back to start of agent heading line.
                              ;; Heading is now `** [glyph ]@<agent>' so
                              ;; just match any `** ' line — going backward
                              ;; from inside the agent body, the FIRST
                              ;; `** ' we hit is the agent heading itself.
                              (re-search-backward "^\\*\\* " nil t)
                              (forward-line 1)
                              (point))))
                (when (and normalised
                           (not (string= normalised content))
                           (>= ins begin))
                  (delete-region begin ins)
                  (goto-char begin)
                  (insert normalised)
                  (set-marker ins (point))))))
          (when (string-empty-p (string-trim content))
            (when (and ins (marker-buffer ins))
              (save-excursion
                (goto-char ins)
                (insert "(empty response)\n"))))
          (org-llm-chat--append-next-turn)
          (ignore-errors (save-buffer)))
         (t
          ;; Error path — render an ERROR heading + message, then
          ;; STILL append a fresh user-turn heading + redraw the
          ;; prompt frame so the user can immediately retry. Without
          ;; this, the buffer dead-ends on a stream-drop with no
          ;; obvious way forward.
          (org-llm-chat--stop-spinner)
          (when (and ins (marker-buffer ins))
            (save-excursion
              (goto-char ins)
              (insert (format "\nERROR: %s\n"
                              (or (plist-get state :error)
                                  "stream failed")))))
          (org-llm-chat--append-next-turn)
          (ignore-errors (save-buffer))))
        (setq-local org-llm-chat--pending-marker nil)
        (setq-local org-llm-chat--pending-process nil)
        (setq-local org-llm-chat--sse-process nil)
        (setq-local org-llm-chat--sse-state nil)))))

(defun org-llm-chat--sse-filter (proc chunk)
  "Process filter for the SSE network process. Accumulates CHUNK
into the per-process state, strips HTTP headers on first chunk,
then drains SSE lines."
  (let ((state (process-get proc 'org-llm-chat-state)))
    (when state
      (let* ((existing (or (plist-get state :raw-buffer) ""))
             (combined (concat existing chunk)))
        (plist-put state :raw-buffer combined)
        ;; Strip HTTP response headers on first chunk: the first
        ;; blank line ("\r\n\r\n" or "\n\n") separates headers
        ;; from the SSE body.
        (unless (plist-get state :header-done)
          (when (string-match "\r?\n\r?\n" combined)
            (let ((body-start (match-end 0)))
              (plist-put state :raw-buffer
                         (substring combined body-start))
              (plist-put state :header-done t)
              ;; Initialise rendering now that we've got the
              ;; first byte (cleanest moment to drop the
              ;; placeholder).
              (org-llm-chat--sse-init-render state))))
        (when (plist-get state :header-done)
          (org-llm-chat--sse-process-buffer state)
          (when (plist-get state :done)
            (org-llm-chat--sse-finalise state)))))))

(defun org-llm-chat--sse-sentinel (proc event)
  "Process sentinel: when the network connection closes, finalise
the response (in case [DONE] never arrived)."
  (when (memq (process-status proc) '(closed exit signal failed))
    (let ((state (process-get proc 'org-llm-chat-state)))
      (when state
        (unless (plist-get state :done)
          ;; Closed without [DONE] — still render whatever we have
          ;; and call it ok if any content arrived; else mark error.
          (let ((content (or (plist-get state :content) "")))
            (if (string-empty-p (string-trim content))
                (progn
                  (plist-put state :rc -1)
                  (plist-put state :error
                             (format "stream closed early (%s)"
                                     (string-trim event))))
              (plist-put state :rc 0)))
          (org-llm-chat--sse-finalise state))))))

(defun org-llm-chat--call-backend-proxy-sse (agent prompt marker port)
  "Send PROMPT to the proxy at PORT with `stream: true' and render
SSE deltas as they arrive. Falls back (via condition-case in the
caller) to the v0.1 non-streaming path if `make-network-process'
or anything else here fails."
  (let* ((host "127.0.0.1")
         (body (encode-coding-string
                (org-llm-chat--build-proxy-payload agent prompt t)
                'utf-8))
         (req (format
               (concat "POST /v1/chat/completions HTTP/1.1\r\n"
                       "Host: %s:%d\r\n"
                       "Content-Type: application/json\r\n"
                       "Accept: text/event-stream\r\n"
                       "Content-Length: %d\r\n"
                       "Connection: close\r\n"
                       "\r\n")
               host port (length body)))
         (state (list :marker        marker
                      :agent         agent
                      :buffer        (current-buffer)
                      :inserted-pos  nil
                      :raw-buffer    ""
                      :header-done   nil
                      :content       ""
                      :done          nil
                      :rc            0
                      :error         nil))
         (proc (make-network-process
                :name     "org-llm-chat-sse"
                :host     host
                :service  port
                :nowait   nil
                :noquery  t
                :coding   '(no-conversion . no-conversion)
                :filter   #'org-llm-chat--sse-filter
                :sentinel #'org-llm-chat--sse-sentinel)))
    (process-put proc 'org-llm-chat-state state)
    (setq-local org-llm-chat--sse-process proc)
    (setq-local org-llm-chat--sse-state state)
    (setq-local org-llm-chat--pending-process proc)
    (process-send-string proc (concat req body))
    proc))


(defun org-llm-chat--call-backend-shell (agent prompt marker)
  "Shell-out backend (v0 path). Used when the proxy isn't running."
  (let* ((args (org-llm-chat--build-cli-args agent prompt))
         (buf (current-buffer))
         (output-buf (generate-new-buffer " *org-llm-chat-out*"))
         (process-environment
          (if (fboundp 'org-llm--env) (org-llm--env) process-environment)))
    (condition-case err
        (let ((proc (make-process
                     :name    "org-llm-chat"
                     :buffer  output-buf
                     :command args
                     :noquery t
                     :sentinel
                     (lambda (proc event)
                       (when (memq (process-status proc) '(exit signal))
                         (let* ((rc  (process-exit-status proc))
                                (raw (with-current-buffer (process-buffer proc)
                                       (buffer-string))))
                           (with-current-buffer (process-buffer proc)
                             (let ((inhibit-message t)) (kill-buffer)))
                           (when (buffer-live-p buf)
                             (with-current-buffer buf
                               (org-llm-chat--finalise-response
                                marker agent raw rc)))))))))
          (setq-local org-llm-chat--pending-process proc))
      (error
       (kill-buffer output-buf)
       (org-llm-chat--finalise-response
        marker agent (format "ERROR launching process: %s" err) -1)))))

(defun org-llm-chat--goto-compose-position ()
  "Move point + window scroll to the typing position of the active turn.
Walks back to the trailing user-turn heading, advances one line
into the body (the prompt-marker line), and lands at end-of-line —
right after `❯' (or wherever the marker ends).

ALSO scrolls every window showing this buffer so the prompt is
visible: `set-window-point' alone doesn't auto-scroll, so if the
user has scrolled up to read past responses, the cursor would
land off-screen at the buffer's natural bottom — making it look
like the cursor jumped 'somewhere weird'. `recenter' brings the
prompt into the visible region near the bottom of the window
(chat-style: prompt at bottom, prior context above)."
  (goto-char (point-max))
  (when (re-search-backward (org-llm-chat--user-heading-line-regex) nil t)
    (forward-line 1)
    (end-of-line))
  ;; Use `--force-prompt-window-start' which sets window-start
  ;; explicitly via `set-window-start' — far more reliable than
  ;; `recenter' under evil-mode + Doom's hook stack.
  (org-llm-chat--force-prompt-window-start)
  (redisplay t))

(defun org-llm-chat--has-valid-trailing-prompt-p ()
  "Return non-nil iff the buffer ends with a well-formed prompt.
Validation requires ALL of:
  (1) A trailing line matching `^** <CONFIGURED-HEADING>$' EXACTLY
      (no permissive matches — a heading missing dots or with
      typed-into chars fails here, triggering repair).
  (2) The heading is followed by at least one body line below
      (i.e. there's a place to type — the heading isn't the
      buffer's last line).
  (3) No further `** ' heading appears AFTER the user heading
      (catches stray empty `** ' from accidental
      M-RET-as-org-meta-return)."
  (save-excursion
    (goto-char (point-max))
    (let* ((strict-line-rgx
             (concat "^\\*\\* "
                      (regexp-quote org-llm-chat-user-heading)
                      "\\s-*$"))
           (heading-pos (re-search-backward strict-line-rgx nil t)))
      (when heading-pos
        (let ((heading-line (line-number-at-pos)))
          (forward-line 1)
          (and
           ;; (2) we moved off the heading line — body line exists
           (> (line-number-at-pos) heading-line)
           ;; (3) no further `** ' after the heading
           (save-excursion
             (not (re-search-forward "^\\*\\* " nil t)))))))))

(defun org-llm-chat--strip-stub-headings ()
  "Delete empty `** ' heading stubs from the buffer.
These are typically left over from accidental
M-RET-as-org-meta-return presses. Returns the count deleted."
  (save-excursion
    (goto-char (point-min))
    (let ((count 0))
      (while (re-search-forward "^\\*\\*\\s-*$" nil t)
        (delete-region (line-beginning-position)
                        (min (point-max)
                             (1+ (line-end-position))))
        (cl-incf count))
      count)))

(defun org-llm-chat-repair-prompt ()
  "Detect a broken trailing prompt and append a fresh one.
Idempotent: if the prompt is already well-formed AND there are
no stub headings, no-op. Otherwise:
  - Empty `** ' stub lines (left over from accidental
    M-RET-as-org-meta-return) are deleted.
  - If the trailing prompt is broken (missing marker, truncated
    heading, content past it), a fresh `** <heading>' + marker
    is appended at point-max."
  (interactive)
  (let ((stripped (org-llm-chat--strip-stub-headings)))
    (unless (org-llm-chat--has-valid-trailing-prompt-p)
      (org-llm-chat--append-next-turn))
    (org-llm-chat--clear-prompt-frame)
    (org-llm-chat--draw-prompt-frame)
    (ignore-errors (save-buffer))
    (when (or (> stripped 0)
              (not (org-llm-chat--has-valid-trailing-prompt-p)))
      (message "Prompt repaired — %d stub heading(s) stripped." stripped))))

;;;###autoload
(defun org-llm-chat-jump-to-prompt ()
  "Jump to the active typing position in the chat buffer.
Hotkey for the active compose box — useful when you've scrolled
up reading prior turns and want to start a new prompt without
fishing for the trailing heading manually.

Self-repairing: if the prompt structure was broken (e.g. by
accidental editing of the heading or marker line), a fresh
heading is appended automatically before the jump."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (org-llm-chat-repair-prompt)
  (org-llm-chat--goto-compose-position))

(defun org-llm-chat--at-prompt-position-p ()
  "Return non-nil iff point is in the prompt compose area.
The compose area is from the trailing user-turn heading line to
the end of the buffer, with NO other `** ' heading in between.
Generous on purpose: evil's `a' (append) command pushes point
past end-of-line into trailing whitespace / overlay padding,
and we still want recenter hooks to fire there."
  (and (derived-mode-p 'org-mode)
       (bound-and-true-p org-llm-chat-mode)
       (let ((p (point))
             (heading-pos (save-excursion
                            (goto-char (point-max))
                            (re-search-backward
                              (org-llm-chat--user-heading-line-regex)
                              nil t))))
         (and heading-pos
              (>= p heading-pos)
              ;; no `** ' heading appears between (heading + 1 line)
              ;; and point — i.e., we're inside this turn, not past it
              (save-excursion
                (goto-char heading-pos)
                (forward-line 1)
                (not (re-search-forward "^\\*\\* " p t)))))))

(defun org-llm-chat--force-prompt-window-start ()
  "Set window-start so cursor lands roughly mid-window.
Computes the target as ~half the window height in logical lines
above point. Larger displacements (e.g. fixed `-8') broke when
the buffer had many tiny stub lines above the prompt — the
cursor still ended up near the visible bottom because 8 short
lines barely fill a few visual rows."
  (when-let ((win (get-buffer-window (current-buffer))))
    (let* ((p (point))
           (h (max 4 (/ (window-height win) 2)))
           (start (save-excursion
                    (goto-char p)
                    (forward-line (- h))
                    (line-beginning-position))))
      (set-window-start win start nil)
      (set-window-point win p)
      (org-llm-chat--scroll-log
        "force-window-start: p=%d h=%d start=%d delta=%d"
        p h start (- p start)))))

(defun org-llm-chat--maybe-reassert-prompt-scroll ()
  "Re-assert window-start when entering insert state at the prompt.
evil-mode's state-change machinery + various Doom hooks can
re-scroll the window after we've placed the cursor. This hook
fires after insert state is fully entered, ONLY when point is
on the prompt-marker line."
  (when (org-llm-chat--at-prompt-position-p)
    (org-llm-chat--scroll-log "insert-state-entry: forcing window-start")
    (org-llm-chat--force-prompt-window-start)))

(defvar-local org-llm-chat--last-prompt-line nil
  "Cache of the line number where the prompt was last asserted.
The post-command hook only force-recenters when point is on the
prompt line AND that line was changed since last assertion — keeps
us from fighting the user's deliberate scrolling on the same line.")

(defcustom org-llm-chat-debug-scroll nil
  "When non-nil, the chat surface logs scroll-related decisions to
the `*Messages*' buffer. Useful for diagnosing why the visible
cursor lands somewhere unexpected after state changes. Set to
`t' temporarily, reproduce the issue, then check `*Messages*'."
  :type 'boolean
  :group 'org-llm-chat)

(defun org-llm-chat--scroll-log (fmt &rest args)
  (when org-llm-chat-debug-scroll
    (apply #'message (concat "[chat-scroll] " fmt) args)))

(defun org-llm-chat--post-command-keep-prompt-visible ()
  "Buffer-local `post-command-hook' for chat buffers.
After every command, if point is on the prompt-marker line AND
either (a) point's screen position is at/near the visual bottom,
or (b) we haven't yet asserted scroll on this prompt line, force
a recenter. Cache prevents fighting the user's deliberate scroll."
  (when (and (bound-and-true-p org-llm-chat-mode)
             (org-llm-chat--at-prompt-position-p))
    (when-let ((win (get-buffer-window (current-buffer))))
      (let* ((p     (point))
             (line  (line-number-at-pos p))
             (start (window-start win))
             (end   (window-end win t))
             (first-time (not (eq org-llm-chat--last-prompt-line line)))
             (offscreen  (or (< p start) (> p end)))
             (near-bottom (< (- end p) 3))
             (will-recenter (or first-time offscreen near-bottom)))
        (org-llm-chat--scroll-log
          "post-cmd cmd=%S p=%d start=%d end=%d near-bot=%s recenter=%s"
          this-command p start end near-bottom will-recenter)
        (when will-recenter
          (org-llm-chat--force-prompt-window-start)
          (setq-local org-llm-chat--last-prompt-line line))))))

;;;###autoload
(defun org-llm-chat-submit-from-anywhere ()
  "Submit the active prompt from anywhere in the chat buffer.
Convenience wrapper: jumps to the compose position first so the
caller doesn't need point on the user heading. Identical end
state to typing under `** 🪪 Captain ●●●●' and pressing
`C-c C-c' — useful from a quick localleader chord (e.g. `, RET'
in evil normal state)."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (org-llm-chat--goto-compose-position)
  (org-llm-chat-submit))

(defun org-llm-chat--append-next-turn ()
  "Append a fresh user-turn heading at end of buffer + move point
under it, ready for the next prompt.

Idempotent via `--has-valid-trailing-prompt-p': if the buffer
already ends with a well-formed prompt (strict heading match,
body line exists, no junk between), this is a no-op. Otherwise
a fresh `** <heading>' + marker is appended — including the
case where user editing has broken the trailing structure."
  (save-restriction
    (widen)
    (goto-char (point-max))
    (unless (org-llm-chat--has-valid-trailing-prompt-p)
      (unless (bolp) (insert "\n"))
      (insert "\n** " org-llm-chat-user-heading "\n"
               (or org-llm-chat-prompt-marker "")))
    (org-llm-chat--goto-compose-position)
    (org-llm-chat--draw-prompt-frame)))

(defun org-llm-chat--finalise-response (marker agent raw rc)
  "Render RAW as the response under MARKER for AGENT; auto-save on success."
  (let* ((cleaned (org-llm-chat--strip-ansi (or raw "")))
         ;; concat (not format) so propertized agent glyphs preserve
         ;; their text properties (e.g. :family for icon-font glyphs).
         (heading-prefix (if (zerop rc)
                             (concat "** "
                                     (org-llm-chat--agent-heading-text agent))
                           (concat "** "
                                   (org-llm-chat--agent-heading-text
                                    agent " ERROR"))))
         (rendered
          (concat heading-prefix "\n"
                  (let ((md (org-llm-chat--markdown->org cleaned)))
                    (if (string-empty-p (string-trim (or md "")))
                        (format "(no response, exit %d)" rc)
                      md))
                  "\n")))
    (org-llm-chat--replace-placeholder marker rendered)
    ;; Apply the glyph overlay to the freshly-inserted heading —
    ;; `marker' points at heading start. Wins over org-mode's
    ;; font-lock + the `org-level-2' face remap.
    (org-llm-chat--apply-glyph-face-overlay agent marker)
    (org-llm-chat--stop-spinner)
    ;; Always append the next turn + save, even on error, so a stream
    ;; drop or non-zero exit isn't a dead-end. The ERROR line stays
    ;; in the buffer so the user knows what happened.
    (org-llm-chat--append-next-turn)
    (ignore-errors (save-buffer))
    (setq-local org-llm-chat--pending-marker nil)
    (setq-local org-llm-chat--pending-process nil)))

(defun org-llm-chat--strip-ansi (s)
  "Strip ANSI color escape sequences from S."
  (when s
    (replace-regexp-in-string "\x1b\\[[0-9;]*[A-Za-z]" "" s)))


;;; ── export / refile / pin ──────────────────────────────────────────────────

;;;###autoload
(defun org-llm-chat-refile ()
  "Refile the current chat subtree to an org-roam node.
Subtree retains its `[[id:…]]' so back-refs continue to resolve."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (cond
   ((featurep 'org-roam)
    (require 'org-roam)
    (let ((node (org-roam-node-read nil nil nil t "Refile to node: ")))
      (unless node (user-error "No node selected"))
      (let ((target-file (org-roam-node-file node)))
        (org-llm-chat--refile-to-file target-file (org-roam-node-title node)))))
   (t
    (org-llm-chat--refile-to-file
     (read-file-name "Refile to org file: " "~/org/" nil t)
     nil))))

(defun org-llm-chat--refile-to-file (target-file _title)
  "Append the current subtree at point to TARGET-FILE."
  (org-back-to-heading t)
  (let ((subtree (buffer-substring-no-properties
                  (point)
                  (save-excursion (org-end-of-subtree t t) (point)))))
    (with-current-buffer (find-file-noselect target-file)
      (save-excursion
        (goto-char (point-max))
        (unless (bolp) (insert "\n"))
        (insert subtree)
        (unless (bolp) (insert "\n"))
        (save-buffer)))
    (message "Refiled subtree → %s" target-file)))

;;;###autoload
(defun org-llm-chat-export-subtree (path)
  "Write the current chat subtree to PATH as a standalone .org file."
  (interactive
   (list (read-file-name "Export subtree to: "
                         (expand-file-name "~/org/exports/")
                         nil nil
                         (format "%s-chat.org"
                                 (format-time-string "%Y-%m-%d")))))
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (org-back-to-heading t)
  (let ((subtree (buffer-substring-no-properties
                  (point)
                  (save-excursion (org-end-of-subtree t t) (point)))))
    (unless (file-directory-p (file-name-directory path))
      (make-directory (file-name-directory path) t))
    (with-temp-file path
      (insert "#+TITLE: chat-export — "
              (format-time-string "%Y-%m-%d %H:%M") "\n\n")
      (insert subtree))
    (message "Exported subtree → %s" path)))

;;;###autoload
(defun org-llm-chat-pin ()
  "Append the current chat turn (subtree at point) to the pins file
as a reference card. Pins are append-only — useful for capturing
a one-shot answer you want to come back to later."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "Not in an org-mode buffer"))
  (org-back-to-heading t)
  (let ((subtree (buffer-substring-no-properties
                  (point)
                  (save-excursion (org-end-of-subtree t t) (point)))))
    (unless (file-exists-p org-llm-chat-pins-file)
      (with-temp-file org-llm-chat-pins-file
        (insert "#+TITLE: org-llm chat pins\n"
                "#+STARTUP: overview\n\n")))
    (with-current-buffer (find-file-noselect org-llm-chat-pins-file)
      (save-excursion
        (goto-char (point-max))
        (unless (bolp) (insert "\n"))
        (insert (format "* Pinned %s\n"
                        (format-time-string "%Y-%m-%d %H:%M")))
        (insert subtree)
        (unless (bolp) (insert "\n"))
        (save-buffer)))
    (message "Pinned → %s" org-llm-chat-pins-file)))


;;; ── minor mode + keymap ────────────────────────────────────────────────────

(defvar org-llm-chat-mode-map
  (let ((m (make-sparse-keymap)))
    ;; Don't shadow C-c C-c globally; we install a local override at
    ;; the heading level via `org-ctrl-c-ctrl-c-hook'.
    ;; Plain-Emacs / non-evil bindings — always available.
    (define-key m (kbd "C-c C-l c") #'org-llm-chat-jump-to-prompt)
    (define-key m (kbd "C-c C-l p") #'org-llm-chat-pin)
    (define-key m (kbd "C-c C-l r") #'org-llm-chat-refile)
    (define-key m (kbd "C-c C-l e") #'org-llm-chat-export-subtree)
    ;; Universal chords — work in all states, mirror standard chat-UI
    ;; conventions (M-RET = jump to input, C-RET = submit).
    ;; Bind BOTH ASCII (`M-RET') and function-key (`M-<return>') forms
    ;; — different terminals + Emacs builds emit one or the other,
    ;; and `org-mode-map' binds `M-RET' to `org-meta-return' which
    ;; would otherwise shadow our binding via key-translation.
    (define-key m (kbd "M-RET")      #'org-llm-chat-jump-to-prompt)
    (define-key m (kbd "M-<return>") #'org-llm-chat-jump-to-prompt)
    (define-key m (kbd "C-RET")      #'org-llm-chat-submit-from-anywhere)
    (define-key m (kbd "C-<return>") #'org-llm-chat-submit-from-anywhere)
    m)
  "Keymap for `org-llm-chat-mode'.")

;; ── evil / Doom ergonomics ──────────────────────────────────────────────
;;
;; Loaded only when `evil' is on the system. In normal/visual/motion
;; states, `g RET' jumps to the active prompt — mnemonic "go to input".
;; Doom's localleader (`SPC m' / `,') is also wired so `SPC m c'
;; (compose), `SPC m p' (pin), `SPC m r' (refile), `SPC m e' (export)
;; all work the way a Doom user expects from a major-mode-flavoured
;; minor mode.
(with-eval-after-load 'evil
  (when (fboundp 'evil-define-key*)
    (evil-define-key* '(normal visual motion) org-llm-chat-mode-map
                       (kbd "g RET")      #'org-llm-chat-jump-to-prompt
                       (kbd "g <return>") #'org-llm-chat-jump-to-prompt
                       (kbd "g s")        #'org-llm-chat-submit-from-anywhere)
    (evil-define-key* '(normal visual motion insert emacs) org-llm-chat-mode-map
                       (kbd "M-RET")      #'org-llm-chat-jump-to-prompt
                       (kbd "M-<return>") #'org-llm-chat-jump-to-prompt
                       (kbd "C-RET")      #'org-llm-chat-submit-from-anywhere
                       (kbd "C-<return>") #'org-llm-chat-submit-from-anywhere))
  ;; Force chat-mode-map's bindings to win over evil's state keymaps
  ;; AND over the underlying major-mode (org-mode) bindings. Without
  ;; this, `M-RET' falls through to `org-meta-return' in normal state
  ;; because org-mode's binding lives in `evil-normal-state-map's
  ;; auxiliary table at higher precedence than the minor-mode map.
  (when (fboundp 'evil-make-overriding-map)
    (evil-make-overriding-map org-llm-chat-mode-map 'normal)
    (evil-make-overriding-map org-llm-chat-mode-map 'insert)
    (evil-make-overriding-map org-llm-chat-mode-map 'visual)
    (evil-make-overriding-map org-llm-chat-mode-map 'motion))
  (when (fboundp 'evil-normalize-keymaps)
    (evil-normalize-keymaps))
  ;; Re-assert cursor placement when entering insert state at the
  ;; prompt. APPEND=t so we run AFTER evil-mc, evil-snipe, etc.
  (when (boundp 'evil-insert-state-entry-hook)
    (add-hook 'evil-insert-state-entry-hook
               #'org-llm-chat--maybe-reassert-prompt-scroll t))
  ;; Same on state EXIT (covers normal→insert transitions where the
  ;; exit hook fires for normal state before the entry hook fires for
  ;; insert state — some Doom configs do scroll work on either edge).
  (when (boundp 'evil-normal-state-exit-hook)
    (add-hook 'evil-normal-state-exit-hook
               #'org-llm-chat--maybe-reassert-prompt-scroll t)))
  ;; Doom localleader — only registers when Doom's `general' is
  ;; around. `general-define-key' is safe outside Doom too (general.el
  ;; is a regular MELPA package).
  ;;
  ;; CRITICAL: `:states' MUST exclude `insert' and `emacs'. Doom's
  ;; `doom-localleader-key' is `SPC m' by default — including SPC as
  ;; a prefix in insert mode breaks the spacebar (every space typed
  ;; would start a prefix sequence). Localleader bindings only fire
  ;; in normal/visual/motion. For typing-time access, the alt-leader
  ;; (`,') is added separately for insert mode below.
  (with-eval-after-load 'general
    (when (fboundp 'general-define-key)
      (let ((leader     (or (and (boundp 'doom-localleader-key)
                                  doom-localleader-key)
                            ",")))
        ;; Primary localleader (SPC m / ,) — normal/visual/motion only.
        (general-define-key
          :keymaps 'org-llm-chat-mode-map
          :states '(normal visual motion)
          :prefix leader
          "c"   '(org-llm-chat-jump-to-prompt       :which-key "compose / jump to prompt")
          "RET" '(org-llm-chat-submit-from-anywhere :which-key "submit prompt")
          "s"   '(org-llm-chat-submit-from-anywhere :which-key "submit prompt")
          "p"   '(org-llm-chat-pin                  :which-key "pin subtree")
          "r"   '(org-llm-chat-refile               :which-key "refile to vault")
          "e"   '(org-llm-chat-export-subtree       :which-key "export subtree"))
        ;; Insert/emacs states: NO leader prefixes. Bound prefixes in
        ;; insert mode break ordinary typing of the prefix character
        ;; (e.g. `,foo' would consume the comma). The universal
        ;; chords (`C-RET' submit, `M-RET' jump) and the standard
        ;; `C-c C-l <key>' bindings cover insert-mode needs without
        ;; the prefix hazard.
        )))   ; closes let + when + with-eval-after-load

(defun org-llm-chat--ctrl-c-ctrl-c ()
  "Hook fn: when point is on a `** Me' heading subtree, submit and
return non-nil so `org-ctrl-c-ctrl-c' stops here."
  (when (and (bound-and-true-p org-llm-chat-mode)
             (org-llm-chat--at-me-heading-p))
    (org-llm-chat-submit)
    t))

;;;###autoload
(define-minor-mode org-llm-chat-mode
  "Minor mode: org buffer behaves as an org-llm chat surface.
`C-c C-c' on a `** Me' heading submits the prompt; the response
lands as a sibling `** @<agent>' heading. All other org keys
continue to work.

Bindings (plain Emacs):
  C-c C-c        submit (when point is on/under user heading)
  C-<return>     submit from anywhere in the buffer
  M-<return>     jump to active prompt (from anywhere)
  C-c C-l c      jump to active prompt
  C-c C-l p      pin current subtree to ~/org/chat-pins.org
  C-c C-l r      refile current subtree to an org-roam node
  C-c C-l e      export current subtree as standalone .org

Bindings (evil + Doom localleader, when those packages are loaded):
  g RET          jump to active prompt          (normal/visual/motion)
  g s            submit from anywhere           (normal/visual/motion)
  SPC m c / ,c   compose / jump to prompt       (Doom localleader)
  SPC m RET      submit                         (Doom localleader)
  SPC m s        submit (alt)                   (Doom localleader)
  SPC m p / ,p   pin subtree
  SPC m r / ,r   refile subtree
  SPC m e / ,e   export subtree"
  :init-value nil
  :lighter " ✱chat"
  :keymap org-llm-chat-mode-map
  (cond
   (org-llm-chat-mode
    (add-hook 'org-ctrl-c-ctrl-c-hook
               #'org-llm-chat--ctrl-c-ctrl-c nil t)
    ;; APPEND=t so our hook runs LAST in the chain — after evil and
    ;; Doom hooks have done their state-change re-scrolling. We're the
    ;; "settle the dust" pass.
    (add-hook 'post-command-hook
               #'org-llm-chat--post-command-keep-prompt-visible
               t t)
    (org-llm-chat--apply-heading-font))
   (t
    (remove-hook 'org-ctrl-c-ctrl-c-hook
                  #'org-llm-chat--ctrl-c-ctrl-c t)
    (remove-hook 'post-command-hook
                  #'org-llm-chat--post-command-keep-prompt-visible t)
    (org-llm-chat--remove-heading-font))))

(defvar-local org-llm-chat--heading-font-cookies nil
  "List of cookies returned by `face-remap-add-relative' for the
heading-font remaps. Multiple faces are remapped (org-level-2 plus
any theme-specific overlay faces like `doom-themes-org-at-tag' that
otherwise override the family on `@<agent>' tokens).")

(defcustom org-llm-chat-heading-font-faces
  '(org-level-2
    doom-themes-org-at-tag
    org-tag)
  "Faces to remap to `org-llm-chat-heading-font' when chat-mode
turns on. `org-level-2' carries the heading body; theme overlays
like `doom-themes-org-at-tag' (Doom Emacs) and `org-tag' apply
narrower coverage to `@<agent>' tokens or trailing :tags: and
override the family unless we remap them too."
  :type '(repeat face)
  :group 'org-llm-chat)

(defun org-llm-chat--apply-heading-font ()
  "Buffer-locally remap heading faces to `org-llm-chat-heading-font'.
No-op when the defcustom is nil. The remap is purely visual; the
underlying .org file is unchanged."
  (when (and org-llm-chat-heading-font
             (not (string-empty-p org-llm-chat-heading-font)))
    (org-llm-chat--remove-heading-font)
    (let (cookies)
      (dolist (face org-llm-chat-heading-font-faces)
        (when (facep face)
          (push (face-remap-add-relative
                  face :family org-llm-chat-heading-font)
                cookies)))
      (setq-local org-llm-chat--heading-font-cookies cookies))))

(defun org-llm-chat--remove-heading-font ()
  "Revert all heading-font remaps. Idempotent."
  (dolist (c org-llm-chat--heading-font-cookies)
    (when c (face-remap-remove-relative c)))
  (setq-local org-llm-chat--heading-font-cookies nil))

(provide 'org-llm-chat)
;;; org-llm-chat.el ends here
