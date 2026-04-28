;;; org-llm.el --- Doom Emacs integration for org-llm -*- lexical-binding: t; -*-

;; Comprehensive Doom Emacs bindings under SPC l. Top-level keys are
;; the high-frequency actions; sub-prefixes group everything else by
;; intent. The whole org-llm CLI surface is reachable from the
;; keyboard — no need to memorise verbs.

(defvar org-llm-binary (expand-file-name "~/.local/bin/org-llm")
  "Path to the org-llm CLI binary.")

(defvar org-llm-ask-buffer      "*org-llm: ask*")
(defvar org-llm-search-buffer   "*org-llm: search*")
(defvar org-llm-report-buffer   "*org-llm: report*")
(defvar org-llm-launch-buffer   "*org-llm: opencode*")
(defvar org-llm-claude-buffer   "*org-llm: claude*")


;;; ── internal helpers ─────────────────────────────────────────────────────────

(defun org-llm--env ()
  "Process environment for org-llm: ensures PATH includes ~/.local/bin."
  (cons (format "PATH=%s:%s"
                (expand-file-name "~/.local/bin")
                (getenv "PATH"))
        process-environment))

(defun org-llm--run-display (cmd buf-name)
  "Run shell CMD, display ANSI-colored output in BUF-NAME side window."
  (let* ((buf (get-buffer-create buf-name))
         (process-environment (org-llm--env)))
    (with-current-buffer buf
      (setq buffer-read-only nil)
      (erase-buffer)
      (insert (shell-command-to-string cmd))
      (ansi-color-apply-on-region (point-min) (point-max))
      (setq buffer-read-only t)
      (goto-char (point-min))
      (special-mode))
    (display-buffer buf
                    '((display-buffer-in-side-window)
                      (side . right)
                      (window-width . 0.45)))))

(defun org-llm--vterm (cmd)
  "Send CMD to a dedicated org-llm vterm buffer (bottom side window)."
  (require 'vterm)
  (let ((buf (get-buffer-create "*org-llm: vterm*")))
    (with-current-buffer buf
      (unless (derived-mode-p 'vterm-mode)
        (vterm-mode)))
    (display-buffer buf
                    '((display-buffer-in-side-window)
                      (side . bottom)
                      (window-height . 0.35)))
    (with-current-buffer buf
      (vterm-send-string (concat cmd "\n")))))

(defun org-llm--vterm-fullscreen (cmd buf-name)
  "Open BUF-NAME as a full vterm window running CMD (for interactive sessions)."
  (require 'vterm)
  (let ((buf (get-buffer-create buf-name)))
    (with-current-buffer buf
      (unless (derived-mode-p 'vterm-mode)
        (vterm-mode)))
    (switch-to-buffer buf)
    (with-current-buffer buf
      (vterm-send-string (concat cmd "\n")))))

(defun org-llm--shell (cmd buf-name)
  "Convenience wrapper: run CMD in side-window with ANSI rendering."
  (org-llm--run-display (format "%s %s" org-llm-binary cmd) buf-name))


;;; ── interactive commands ─────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-ask (query &optional reason)
  "Ask a question answered from org notes.
With prefix arg, use the reason model (deepseek-r1)."
  (interactive "sAsk your notes: \nP")
  (let* ((flag (if reason " --reason" ""))
         (cmd  (format "%s ask %s%s" org-llm-binary
                       (shell-quote-argument query) flag)))
    (org-llm--run-display cmd org-llm-ask-buffer)))

;;;###autoload
(defun org-llm-search (query &optional keyword)
  "Search org notes.  With prefix arg, use keyword search."
  (interactive "sSearch notes: \nP")
  (let* ((flag (if keyword " --keyword" ""))
         (cmd  (format "%s search %s%s" org-llm-binary
                       (shell-quote-argument query) flag)))
    (org-llm--run-display cmd org-llm-search-buffer)))

;;;###autoload
(defun org-llm-report (&optional section)
  "Show knowledge base report.  SECTION defaults to 'all'."
  (interactive (list (completing-read "Section: "
                       '("all" "overview" "tags" "recent" "orphans" "daily")
                       nil t nil nil "all")))
  (org-llm--vterm (format "%s report %s" org-llm-binary (or section "all"))))

;;;###autoload
(defun org-llm-index (&optional force)
  "Re-index org files.  With prefix arg, force full re-index."
  (interactive "P")
  (org-llm--vterm (format "%s index%s" org-llm-binary
                           (if force " --force" ""))))

;;;###autoload
(defun org-llm-embed (&optional force)
  "Generate embeddings.  With prefix arg, re-embed everything."
  (interactive "P")
  (org-llm--vterm (format "%s embed%s" org-llm-binary
                           (if force " --force" ""))))

;;;###autoload
(defun org-llm-models ()
  "Show model assignments + auto-suggestions dashboard."
  (interactive)
  (org-llm--shell "models" "*org-llm: models*"))

;;;###autoload
(defun org-llm-models-set (assignment)
  "One-shot model assignment.  ASSIGNMENT is role=tag (e.g. chat=gemma3)."
  (interactive "sAssign (role=tag): ")
  (org-llm--shell (format "models --set %s" (shell-quote-argument assignment))
                   "*org-llm: models*"))

;;;###autoload
(defun org-llm-ask-dwim ()
  "Ask about text at point or region."
  (interactive)
  (let ((text (if (use-region-p)
                  (buffer-substring-no-properties (region-beginning) (region-end))
                (thing-at-point 'sentence t))))
    (when text
      (org-llm-ask (string-trim text)))))

;;;###autoload
(defun org-llm-launch (&optional model)
  "Launch opencode as the org-llm workspace in a full vterm buffer.
With prefix arg, prompt for model override."
  (interactive (list (when current-prefix-arg
                       (read-string "Model override (empty = default): "))))
  (let* ((flag (if (and model (not (string-empty-p model)))
                   (format " --model %s" (shell-quote-argument model))
                 ""))
         (cmd  (format "%s launch%s" org-llm-binary flag)))
    (org-llm--vterm-fullscreen cmd org-llm-launch-buffer)))

;;;###autoload
(defun org-llm-launch-cloud ()
  "Launch opencode forced to cloud routing (overrides auto-detect)."
  (interactive)
  (org-llm--vterm-fullscreen
   (format "%s launch --cloud" org-llm-binary) org-llm-launch-buffer))

;;;###autoload
(defun org-llm-launch-local ()
  "Launch opencode forced to local Ollama (privacy-first)."
  (interactive)
  (org-llm--vterm-fullscreen
   (format "%s launch --local" org-llm-binary) org-llm-launch-buffer))

;;;###autoload
(defun org-llm-claude ()
  "Launch Claude Code as the org-llm workspace in a full vterm buffer."
  (interactive)
  (org-llm--vterm-fullscreen
   (format "%s claude" org-llm-binary) org-llm-claude-buffer))

;;;###autoload
(defun org-llm-pi (&optional install)
  "Launch Pi (with the org-llm bridge extension) in a full vterm buffer.
With prefix arg, run --install instead (idempotent install + wire into
~/.pi/config.json so plain pi auto-loads the bridge)."
  (interactive "P")
  (org-llm--vterm-fullscreen
   (format "%s pi%s" org-llm-binary
           (if install " --install" " --launch"))
   "*org-llm: pi*"))

;;;###autoload
(defun org-llm-splash ()
  "Open the org-llm splash menu (LCARS logo + linked verb shortcuts)."
  (interactive)
  (org-llm--shell "splash" "*org-llm: splash*"))

;;;###autoload
(defun org-llm-askbook ()
  "Show the askbook table (multi-model Q/A scratchpad)."
  (interactive)
  (org-llm--shell "askbook show" "*org-llm: askbook*"))

;;;###autoload
(defun org-llm-askbook-add (question backend)
  "Add a QUESTION to the askbook with chosen BACKEND (chat/reason/cloud/etc)."
  (interactive
   (list (read-string "Question: ")
         (completing-read "Backend: "
            '("chat" "reason" "fast" "code" "text" "cloud" "claude" "pi")
            nil t nil nil "chat")))
  (org-llm--vterm
   (format "%s askbook add %s --backend %s --run"
           org-llm-binary
           (shell-quote-argument question)
           backend)))

;;;###autoload
(defun org-llm-askbook-run ()
  "Process every pending askbook entry."
  (interactive)
  (org-llm--vterm (format "%s askbook run" org-llm-binary)))

;;;###autoload
(defun org-llm-askbook-open ()
  "Open ~/org/llm-askbook.org for direct editing."
  (interactive)
  (find-file (expand-file-name "~/org/llm-askbook.org")))

;;;###autoload
(defun org-llm-doctor ()
  "Run org-llm doctor health check in vterm."
  (interactive)
  (org-llm--vterm (format "%s doctor" org-llm-binary)))

;;;###autoload
(defun org-llm-doctor-power-boost (&optional apply)
  "Run doctor's power-boost probe.  Prefix arg = --apply."
  (interactive "P")
  (org-llm--vterm
   (format "%s doctor --power-boost%s" org-llm-binary (if apply " --apply" ""))))

;;;###autoload
(defun org-llm-capture (title body)
  "Capture a new note into the org vault."
  (interactive (list (read-string "Note title: ")
                     (read-string "Content: ")))
  (org-llm--run-display
   (format "%s capture --title %s --body %s --no-polish"
           org-llm-binary
           (shell-quote-argument title)
           (shell-quote-argument body))
   "*org-llm: capture*"))

;;;###autoload
(defun org-llm-tag (&optional mode)
  "Auto-tag nodes.  MODE = default | redo | repair | force."
  (interactive (list (completing-read "tag mode: "
                       '("default" "redo" "repair" "force")
                       nil t nil nil "default")))
  (org-llm--vterm
   (format "%s tag%s" org-llm-binary
           (pcase mode
             ("redo"   " --redo")
             ("repair" " --repair")
             ("force"  " --force")
             (_        "")))))

;;;###autoload
(defun org-llm-discover ()
  "Probe the filesystem for vaults / repos / dotfiles."
  (interactive)
  (org-llm--shell "discover" "*org-llm: discover*"))


;;; ── Captain's Log ────────────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-log (&optional limit)
  "Show Captain's Log entries.  Prefix arg = ask for --limit."
  (interactive "P")
  (let ((arg (if limit (format " --limit %d" (read-number "Limit: " 50)) "")))
    (org-llm--shell (concat "log" arg) "*org-llm: log*")))

;;;###autoload
(defun org-llm-log-grep (pattern)
  "Search Captain's Log entries for PATTERN."
  (interactive "sgrep: ")
  (org-llm--shell (format "log --grep %s" (shell-quote-argument pattern))
                   "*org-llm: log*"))

;;;###autoload
(defun org-llm-log-kind (kind)
  "Filter Captain's Log to KIND (cli | llm | mcp | config | doctor | dbt | embed)."
  (interactive (list (completing-read "kind: "
                       '("cli" "llm" "mcp" "config" "doctor" "dbt" "embed")
                       nil t)))
  (org-llm--shell (format "log --kind %s" kind) "*org-llm: log*"))

;;;###autoload
(defun org-llm-log-reflect ()
  "LLM reflection on recent Captain's Log entries."
  (interactive)
  (org-llm--vterm (format "%s log --reflect" org-llm-binary)))

;;;###autoload
(defun org-llm-log-open ()
  "Open ~/org/captains-log.org in this buffer."
  (interactive)
  (find-file (expand-file-name "~/org/captains-log.org")))


;;; ── dbt ──────────────────────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-dbt-status ()
  "dbt analytics layer: project paths + per-model row counts."
  (interactive)
  (org-llm--shell "dbt status" "*org-llm: dbt*"))

;;;###autoload
(defun org-llm-dbt-build ()
  "Run + test dbt models in dependency order."
  (interactive)
  (org-llm--vterm (format "%s dbt build" org-llm-binary)))

;;;###autoload
(defun org-llm-dbt-doctor ()
  "dbt health check (binary, project, DB, raw tables, compile)."
  (interactive)
  (org-llm--shell "dbt doctor" "*org-llm: dbt*"))

;;;###autoload
(defun org-llm-dbt-models ()
  "List dbt models with materialization."
  (interactive)
  (org-llm--shell "dbt models" "*org-llm: dbt*"))

;;;###autoload
(defun org-llm-dbt-walkthrough (&optional model)
  "LLM walks through one (or all) dbt models with commentary."
  (interactive "sModel (blank = all): ")
  (let ((arg (if (string-empty-p model) "" (concat " " model))))
    (org-llm--vterm (format "%s dbt walkthrough%s" org-llm-binary arg))))

;;;###autoload
(defun org-llm-dbt-design (&optional intent)
  "LLM-driven dbt model designer.  INTENT blank = proposals."
  (interactive "sDesign intent (blank for proposals): ")
  (let ((arg (if (string-empty-p intent) "" (format " %s" (shell-quote-argument intent)))))
    (org-llm--vterm (format "%s dbt design%s" org-llm-binary arg))))

;;;###autoload
(defun org-llm-dbt-lessons (level)
  "LLM-instructed dbt lessons at LEVEL (intro|intermediate|advanced)."
  (interactive (list (completing-read "level: "
                       '("intro" "intermediate" "advanced") nil t nil nil "intro")))
  (org-llm--vterm (format "%s dbt lessons --level %s" org-llm-binary level)))


;;; ── Auto-embed watcher ──────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-watch ()
  "Foreground auto-embedder watcher (Ctrl-C to stop)."
  (interactive)
  (org-llm--vterm-fullscreen
   (format "%s watch" org-llm-binary) "*org-llm: watch*"))

;;;###autoload
(defun org-llm-watch-daemon ()
  "Show daemonization options for the auto-embed watcher."
  (interactive)
  (org-llm--shell "watch --daemon" "*org-llm: watch*"))


;;; ── Context, history, self ──────────────────────────────────────────────────

;;;###autoload
(defun org-llm-context-open ()
  "Open ~/org/org-llm-context.org for direct editing."
  (interactive)
  (find-file (expand-file-name "~/org/org-llm-context.org")))

;;;###autoload
(defun org-llm-context-add (fact)
  "Add a fact to org-llm context."
  (interactive "sFact: ")
  (org-llm--vterm (format "%s context add %s"
                           org-llm-binary (shell-quote-argument fact))))

;;;###autoload
(defun org-llm-history-open ()
  "Open ~/org/llm-history.org for review."
  (interactive)
  (find-file (expand-file-name "~/org/llm-history.org")))

;;;###autoload
(defun org-llm-history-build ()
  "Rebuild the LLM-history narrative."
  (interactive)
  (org-llm--vterm (format "%s history build" org-llm-binary)))

;;;###autoload
(defun org-llm-self-snapshot (label)
  "Take a self-mod snapshot with optional LABEL."
  (interactive "sLabel (optional): ")
  (let ((arg (if (string-empty-p label) ""
               (format " -l %s" (shell-quote-argument label)))))
    (org-llm--vterm (format "%s self snapshot%s" org-llm-binary arg))))

;;;###autoload
(defun org-llm-self-rollback ()
  "Roll back to most recent self-mod snapshot (interactive prompt)."
  (interactive)
  (org-llm--vterm (format "%s self rollback" org-llm-binary)))

;;;###autoload
(defun org-llm-self-log ()
  "Open the self-mod activity log."
  (interactive)
  (find-file (expand-file-name "~/org/org-llm-self-mod.org")))


;;; ── Skills ──────────────────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-skills ()
  "List registered org-babel :skill: blocks."
  (interactive)
  (org-llm--shell "skills" "*org-llm: skills*"))

;;;###autoload
(defun org-llm-skill-run (name)
  "Run a registered skill by NAME."
  (interactive "sSkill name: ")
  (org-llm--vterm (format "%s skill %s" org-llm-binary
                           (shell-quote-argument name))))

;;;###autoload
(defun org-llm-skill-new (name)
  "Scaffold a new :skill: block by NAME."
  (interactive "sNew skill name: ")
  (org-llm--vterm (format "%s skill-new %s" org-llm-binary
                           (shell-quote-argument name))))


;;; ── Config (incl. literate config round-trip) ───────────────────────────────

;;;###autoload
(defun org-llm-config-show ()
  "Show all config keys."
  (interactive)
  (org-llm--shell "config" "*org-llm: config*"))

;;;###autoload
(defun org-llm-config-set (key value)
  "Set a config KEY to VALUE."
  (interactive "sKey: \nsValue: ")
  (org-llm--vterm (format "%s config %s %s" org-llm-binary
                           (shell-quote-argument key)
                           (shell-quote-argument value))))

;;;###autoload
(defun org-llm-config-tangle ()
  "Write ~/org/org-llm-config.org from current DB state."
  (interactive)
  (org-llm--vterm (format "%s config --tangle" org-llm-binary)))

;;;###autoload
(defun org-llm-config-apply-from-org ()
  "Apply edits from ~/org/org-llm-config.org back to the DB."
  (interactive)
  (org-llm--vterm (format "%s config --apply-from-org" org-llm-binary)))

;;;###autoload
(defun org-llm-config-diff-org ()
  "Show pending diffs between DB and the literate config file."
  (interactive)
  (org-llm--shell "config --diff-org" "*org-llm: config*"))

;;;###autoload
(defun org-llm-config-open ()
  "Open ~/org/org-llm-config.org for direct editing."
  (interactive)
  (find-file (expand-file-name "~/org/org-llm-config.org")))


;;; ── Theme + dials ───────────────────────────────────────────────────────────

;;;###autoload
(defun org-llm-theme (mode)
  "Set UI theme MODE: dark | light."
  (interactive (list (completing-read "theme: " '("dark" "light") nil t)))
  (org-llm--vterm (format "%s theme %s" org-llm-binary mode)))

;;;###autoload
(defun org-llm-dial (which level)
  "Set a theme dial: WHICH = trek|commie|queer; LEVEL = 0..3."
  (interactive
   (list (completing-read "dial: " '("trek" "commie" "queer") nil t)
         (completing-read "level: " '("0" "1" "2" "3") nil t nil nil "2")))
  (org-llm--vterm (format "%s config %s_level %s" org-llm-binary which level)))

;;;###autoload
(defun org-llm-knob ()
  "Open knob management subcommand (interactive)."
  (interactive)
  (org-llm--vterm (format "%s knob" org-llm-binary)))


;;; ── Tutor + cloud + source ──────────────────────────────────────────────────

;;;###autoload
(defun org-llm-init ()
  "One-time DB init."
  (interactive)
  (org-llm--vterm (format "%s init" org-llm-binary)))

;;;###autoload
(defun org-llm-code (task)
  "LLM-generate code for a TASK using the code model."
  (interactive "sCode task: ")
  (org-llm--vterm (format "%s code %s" org-llm-binary
                           (shell-quote-argument task))))

;;;###autoload
(defun org-llm-code-index ()
  "Re-scan source-code repos so /code can search them."
  (interactive)
  (org-llm--vterm (format "%s code-index" org-llm-binary)))

;;;###autoload
(defun org-llm-db (&optional flag)
  "DB inspection.  FLAG = nil | dict | schema (interactive prompt)."
  (interactive (list (completing-read "db view: "
                       '("default" "schema" "dict") nil t)))
  (org-llm--shell
   (format "db%s" (pcase flag
                    ("dict"   " --dict")
                    ("schema" " --schema")
                    (_        "")))
   "*org-llm: db*"))

;;;###autoload
(defun org-llm-performance ()
  "Show / tune org-llm performance to your hardware."
  (interactive)
  (org-llm--shell "performance" "*org-llm: performance*"))

;;;###autoload
(defun org-llm-personalize ()
  "Auto-create theme knobs from your vault + filesystem content."
  (interactive)
  (org-llm--vterm (format "%s personalize" org-llm-binary)))

;;;###autoload
(defun org-llm-review-emacs ()
  "LLM review of your Doom / vanilla Emacs config."
  (interactive)
  (org-llm--vterm (format "%s review-emacs" org-llm-binary)))

;;;###autoload
(defun org-llm-skill-index ()
  "Scan org files and re-register skill blocks."
  (interactive)
  (org-llm--vterm (format "%s skill-index" org-llm-binary)))

;;;###autoload
(defun org-llm-stale ()
  "LLM-driven stale-content sweep."
  (interactive)
  (org-llm--vterm (format "%s stale" org-llm-binary)))

;;;###autoload
(defun org-llm-grants ()
  "List paths the LLM can read via MCP grants."
  (interactive)
  (org-llm--shell "grants" "*org-llm: grants*"))

;;;###autoload
(defun org-llm-man (&optional install-only)
  "Install + open the org-llm man page.  Prefix arg = install only (no open)."
  (interactive "P")
  (org-llm--vterm
   (format "%s man%s" org-llm-binary (if install-only " --install" ""))))

;;;###autoload
(defun org-llm-tutor (step)
  "Open a tutor STEP (e.g. welcome, embed, dbt)."
  (interactive (list (read-string "Tutor step (welcome / embed / dbt / …): "
                                     "welcome")))
  (org-llm--shell (format "tutor %s" step) "*org-llm: tutor*"))

;;;###autoload
(defun org-llm-cloud ()
  "Show cloud provider status."
  (interactive)
  (org-llm--shell "cloud --status" "*org-llm: cloud*"))

;;;###autoload
(defun org-llm-source (module)
  "Show org-llm source for MODULE (cli / db / search / …)."
  (interactive (list (read-string "module: " "cli")))
  (org-llm--shell (format "source %s" module) "*org-llm: source*"))


;;; ── Doom keybindings ─────────────────────────────────────────────────────────
;;
;; Top-level keys under SPC l = high-frequency actions.
;; Sub-prefixes = grouped specialised functionality.
;;
;; Whole CLI is reachable; SPC l ? (not bound) opens which-key for anything
;; you forget.

(map! :leader
      (:prefix ("l" . "org-llm")
       ;; ─── Top-level frequent actions ─────────────────────────────────
       :desc "Launch opencode"           "o" #'org-llm-launch
       :desc "Launch opencode --cloud"   "O" #'org-llm-launch-cloud
       :desc "Launch Claude Code"        "C" #'org-llm-claude
       :desc "Launch Pi (bridge)"        "p" #'org-llm-pi
       :desc "Ask notes"                 "a" #'org-llm-ask
       :desc "Ask (reason model)"        "A" (cmd! (org-llm-ask (read-string "Ask (reason): ") t))
       :desc "Search notes"              "s" #'org-llm-search
       :desc "Keyword search"            "S" (cmd! (org-llm-search (read-string "Keyword: ") t))
       :desc "Capture note"              "c" #'org-llm-capture
       :desc "Report"                    "r" #'org-llm-report
       :desc "Index"                     "i" #'org-llm-index
       :desc "Embed"                     "e" #'org-llm-embed
       :desc "Models dashboard"          "m" #'org-llm-models
       :desc "Doctor"                    "d" #'org-llm-doctor
       :desc "Doctor power-boost"        "?" #'org-llm-doctor-power-boost
       :desc "Discover"                  "v" #'org-llm-discover
       :desc "Tag (auto)"                "t" #'org-llm-tag
       :desc "Code generate"             "g" #'org-llm-code
       :desc "Stale-content sweep"       "z" #'org-llm-stale
       :desc "Review Emacs config"       "x" #'org-llm-review-emacs
       :desc "Splash menu"               "SPC" #'org-llm-splash
       :desc "Askbook (multi-model Q/A)" "k" #'org-llm-askbook
       :desc "Ask dwim"                  "." #'org-llm-ask-dwim

       ;; ─── Captain's Log ──────────────────────────────────────────────
       (:prefix ("L" . "log")
        :desc "Recent entries"           "L" #'org-llm-log
        :desc "Grep"                     "g" #'org-llm-log-grep
        :desc "Filter by kind"           "k" #'org-llm-log-kind
        :desc "LLM reflect"              "r" #'org-llm-log-reflect
        :desc "Open captains-log.org"    "o" #'org-llm-log-open)

       ;; ─── dbt ────────────────────────────────────────────────────────
       (:prefix ("D" . "dbt")
        :desc "Status"                   "s" #'org-llm-dbt-status
        :desc "Build"                    "b" #'org-llm-dbt-build
        :desc "Doctor"                   "d" #'org-llm-dbt-doctor
        :desc "Models"                   "m" #'org-llm-dbt-models
        :desc "Walkthrough"              "w" #'org-llm-dbt-walkthrough
        :desc "Design"                   "D" #'org-llm-dbt-design
        :desc "Lessons"                  "l" #'org-llm-dbt-lessons)

       ;; ─── Auto-embed watcher ─────────────────────────────────────────
       (:prefix ("W" . "watch")
        :desc "Foreground watcher"       "w" #'org-llm-watch
        :desc "Daemon setup hints"       "d" #'org-llm-watch-daemon)

       ;; ─── Context, history, self-mod ─────────────────────────────────
       (:prefix ("X" . "context+history")
        :desc "Open context.org"         "c" #'org-llm-context-open
        :desc "Add fact"                 "a" #'org-llm-context-add
        :desc "Open llm-history.org"     "h" #'org-llm-history-open
        :desc "Build history"            "b" #'org-llm-history-build
        :desc "Self snapshot"            "s" #'org-llm-self-snapshot
        :desc "Self rollback"            "r" #'org-llm-self-rollback
        :desc "Self-mod log"             "l" #'org-llm-self-log)

       ;; ─── Skills ─────────────────────────────────────────────────────
       (:prefix ("K" . "skills")
        :desc "List skills"              "K" #'org-llm-skills
        :desc "Run skill"                "r" #'org-llm-skill-run
        :desc "New skill"                "n" #'org-llm-skill-new
        :desc "Re-index skills"          "i" #'org-llm-skill-index)

       ;; ─── Askbook ────────────────────────────────────────────────────
       (:prefix ("B" . "askbook")
        :desc "Show table"               "B" #'org-llm-askbook
        :desc "Add question"             "a" #'org-llm-askbook-add
        :desc "Run pending"              "r" #'org-llm-askbook-run
        :desc "Open askbook.org"         "o" #'org-llm-askbook-open)

       ;; ─── Config (incl. literate round-trip) ─────────────────────────
       (:prefix ("G" . "config")
        :desc "Show all"                 "G" #'org-llm-config-show
        :desc "Set key=value"            "s" #'org-llm-config-set
        :desc "Tangle to org"            "t" #'org-llm-config-tangle
        :desc "Apply from org"           "a" #'org-llm-config-apply-from-org
        :desc "Diff DB ↔ org"            "d" #'org-llm-config-diff-org
        :desc "Open config.org"          "o" #'org-llm-config-open)

       ;; ─── Themes + dials ─────────────────────────────────────────────
       (:prefix ("T" . "theme")
        :desc "Set theme (dark/light)"   "T" #'org-llm-theme
        :desc "Trek dial"                "t" (cmd! (org-llm-dial "trek"   (read-string "Trek 0-3: ")))
        :desc "Commie dial"              "c" (cmd! (org-llm-dial "commie" (read-string "Commie 0-3: ")))
        :desc "Queer dial"               "q" (cmd! (org-llm-dial "queer"  (read-string "Queer 0-3: ")))
        :desc "Knob (interactive)"       "k" #'org-llm-knob)

       ;; ─── Models management ──────────────────────────────────────────
       (:prefix ("M" . "models")
        :desc "Dashboard"                "M" #'org-llm-models
        :desc "Set role=tag"             "s" #'org-llm-models-set
        :desc "Power-boost"              "p" #'org-llm-doctor-power-boost
        :desc "Cloud status"             "c" #'org-llm-cloud)

       ;; ─── Tutor + source + advanced ──────────────────────────────────
       (:prefix ("H" . "help/tutor")
        :desc "Tutor step"               "H" #'org-llm-tutor
        :desc "Man page"                 "m" #'org-llm-man
        :desc "Source (module)"          "s" #'org-llm-source
        :desc "Performance"              "p" #'org-llm-performance
        :desc "Personalize"              "P" #'org-llm-personalize
        :desc "DB inspect"               "d" #'org-llm-db
        :desc "Code-index repos"         "c" #'org-llm-code-index
        :desc "Init DB"                  "i" #'org-llm-init
        :desc "List grants"              "g" #'org-llm-grants)))

(provide 'org-llm)
;;; org-llm.el ends here
