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


;;; ── opencode action bridge (Phase 18.4-iter8) ──────────────────────────────
;; Drives a running opencode TUI by writing a JSON action file that
;; the plugin polls on a 250ms tick. Sidesteps every vterm key
;; conflict AND opencode's slash registry validator (which rejected
;; vterm-injected /sys* commands as "Unknown command" even when the
;; same names worked when typed manually).
;;
;; The plugin reads `<org_dir>/.opencode/sidebar-action.json` and
;; dispatches when `ts` exceeds the last-seen timestamp.

(defcustom org-llm-org-dir-resolved
  (or (and (boundp 'org-llm-org-dir) org-llm-org-dir)
      (expand-file-name "~/org"))
  "Directory the opencode plugin reads its action file from."
  :type 'directory
  :group 'org-llm)

(defun org-llm--opencode-action-file ()
  (expand-file-name ".opencode/sidebar-action.json"
                     org-llm-org-dir-resolved))

(defun org-llm--opencode-write-action (alist)
  "Write ALIST as JSON into the plugin's action file with a fresh `ts'.
Includes schema-version `v: 1' so future plugin upgrades can branch
by version. Best-effort — silent on filesystem errors."
  (require 'json)
  (let* ((path (org-llm--opencode-action-file))
         (dir  (file-name-directory path))
         (ts   (truncate (* 1000 (float-time))))
         (data (append `((v . 1) (ts . ,ts)) alist)))
    (condition-case _err
        (progn
          (unless (file-directory-p dir) (make-directory dir t))
          (with-temp-file path
            (insert (json-encode data))))
      (error nil))))

(defun org-llm--read-proc-comm (pid)
  "Return /proc/PID/comm trimmed, or nil if unreadable."
  (condition-case nil
      (with-temp-buffer
        (insert-file-contents (format "/proc/%d/comm" pid))
        (string-trim (buffer-string)))
    (error nil)))

(defun org-llm--read-proc-children (pid)
  "Return list of PID children from /proc/PID/task/PID/children, or nil."
  (condition-case nil
      (with-temp-buffer
        (insert-file-contents (format "/proc/%d/task/%d/children" pid pid))
        (mapcar #'string-to-number
                (split-string (buffer-string) "[ \t\n]+" t)))
    (error nil)))

(defun org-llm--proc-has-descendant-named (pid name &optional depth)
  "True iff any descendant of PID has comm == NAME (within DEPTH=8)."
  (let ((depth (or depth 8)))
    (when (and pid (> depth 0))
      (or (equal (org-llm--read-proc-comm pid) name)
          (cl-some (lambda (child)
                     (org-llm--proc-has-descendant-named child name (1- depth)))
                   (org-llm--read-proc-children pid))))))

(defun org-llm--opencode-vterm-buffer ()
  "Return the vterm buffer hosting an `opencode' process, or nil.
Walks the vterm subprocess's process tree (via /proc on Linux)
looking for a descendant whose comm equals `opencode'. Falls
back to a buffer-name regex (`*org-llm: opencode/claude/vterm*')
when /proc isn't readable.

The buffer-name approach alone wasn't enough: users who launch
opencode from a generic vterm (not via `org-llm-launch') end up
with buffers like `*vterm*<2>'. Process-tree inspection finds
opencode regardless of buffer name."
  (or
   ;; Path 1: process-tree match (Linux). Most reliable.
   (cl-find-if
    (lambda (b)
      (and (buffer-live-p b)
           (with-current-buffer b (derived-mode-p 'vterm-mode))
           (let ((p (get-buffer-process b)))
             (and p (org-llm--proc-has-descendant-named
                     (process-id p) "opencode")))))
    (buffer-list))
   ;; Path 2: anchored buffer-name match (cross-platform fallback).
   (cl-find-if
    (lambda (b)
      (and (buffer-live-p b)
           (with-current-buffer b (derived-mode-p 'vterm-mode))
           (string-match-p "\\`\\*org-llm: \\(opencode\\|claude\\|vterm\\)"
                            (buffer-name b))))
    (buffer-list))))

;;;###autoload
(defun org-llm-sys-scroll-up    () "Sidebar scroll up by 1." (interactive)
       (org-llm--opencode-write-action '((action . "scroll") (direction . -1) (unit . "step"))))
;;;###autoload
(defun org-llm-sys-scroll-down  () "Sidebar scroll down by 1." (interactive)
       (org-llm--opencode-write-action '((action . "scroll") (direction . 1)  (unit . "step"))))
;;;###autoload
(defun org-llm-sys-page-up      () "Sidebar page up." (interactive)
       (org-llm--opencode-write-action '((action . "scroll") (direction . -1) (unit . "viewport"))))
;;;###autoload
(defun org-llm-sys-page-down    () "Sidebar page down." (interactive)
       (org-llm--opencode-write-action '((action . "scroll") (direction . 1)  (unit . "viewport"))))
;;;###autoload
(defun org-llm-sys-doctor       () "Run /sysdoctor." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/sysdoctor"))))
;;;###autoload
(defun org-llm-sys-stats        () "Run /sysstats." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/sysstats"))))
;;;###autoload
(defun org-llm-sys-recent       () "Run /sysrecent." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/sysrecent"))))
;;;###autoload
(defun org-llm-sys-models       () "Run /sysmodels." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/sysmodels"))))
;;;###autoload
(defun org-llm-sys-cloud        () "Run /syscloud." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/syscloud"))))
;;;###autoload
(defun org-llm-sys-menu         () "Run /sysmenu." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/sysmenu"))))
;;;###autoload
(defun org-llm-sys-insights     () "Open /insights dialog." (interactive)
       (org-llm--opencode-write-action '((action . "slash") (slash . "/insights"))))

;;; ── Action-bridge: prompt + status + notification ──────────────────────────
;; Tier-1 actions beyond /sys* dispatch. All use the same JSON
;; bridge — the plugin's 250 ms tick reads sidebar-action.json,
;; dispatches based on `action` field.

;;;###autoload
(defun org-llm-sys-refresh-status ()
  "Force the running opencode to re-read sidebar-status.json now.
Useful after writing the JSON externally (e.g. from a daemon
that just updated vault counts) to skip the 15s tick."
  (interactive)
  (org-llm--opencode-write-action '((action . "refresh-status"))))

;;;###autoload
(defun org-llm-sys-toast (message &optional title variant)
  "Surface MESSAGE as a toast in the running opencode TUI.
Optional TITLE defaults to `org-llm'; VARIANT is one of `info'
\(default), `success', `warning', `error'."
  (interactive "sToast message: ")
  (org-llm--opencode-write-action
   `((action . "toast")
     (title  . ,(or title "org-llm"))
     (message . ,message)
     (variant . ,(or variant "info")))))

;;;###autoload
(defun org-llm-sys-prompt-fill (text)
  "Pre-fill the running opencode's prompt with TEXT (no submit).
Workflow: highlight text in any Emacs buffer, invoke this — the
opencode prompt populates, you tweak + submit manually."
  (interactive
   (list (if (use-region-p)
             (buffer-substring-no-properties (region-beginning) (region-end))
           (read-string "Prefill prompt: "))))
  (org-llm--opencode-write-action `((action . "prompt-fill") (text . ,text))))

;;;###autoload
(defun org-llm-sys-prompt-submit (text)
  "Fill AND submit TEXT to the running opencode.
Skips the user-review step — the answer arrives in chat without
a round-trip through the prompt. Workflow: org-babel `:llm'
blocks, `send region as a question' macros."
  (interactive
   (list (if (use-region-p)
             (buffer-substring-no-properties (region-beginning) (region-end))
           (read-string "Ask org-llm: "))))
  (org-llm--opencode-write-action `((action . "prompt-submit") (text . ,text))))

;;;###autoload
(defun org-llm-sys-send-region ()
  "Send the active region (or current paragraph) to opencode as a question.
Calls `org-llm-sys-prompt-submit' under the hood."
  (interactive)
  (let* ((bounds (if (use-region-p)
                     (cons (region-beginning) (region-end))
                   (let ((p (bounds-of-thing-at-point 'paragraph)))
                     (or p (cons (line-beginning-position)
                                  (line-end-position))))))
         (text (buffer-substring-no-properties (car bounds) (cdr bounds))))
    (org-llm-sys-prompt-submit text)))

(defun org-llm--vterm-send-key-multi (key-name escape-seq)
  "Send a special key (like PgUp/PgDn) to the active opencode vterm.
KEY-NAME is the Emacs key string (e.g. \"<prior>\"); ESCAPE-SEQ
is the raw terminal escape sequence (e.g. \"\\e[5~\"). Tries
three primitives in order — vterm-send-key, then a programmatic
keypress dispatch in the vterm window, then process-send-string
direct to the pty — because vterm forks differ on which one
actually reaches opencode's keypress handler."
  (let* ((buf (or (org-llm--opencode-vterm-buffer)
                  (user-error "No opencode vterm buffer found")))
         (win (get-buffer-window buf)))
    (with-current-buffer buf
      (cond
       ;; Path 1: vterm-send-key (modern emacs-libvterm).
       ((fboundp 'vterm-send-key)
        (vterm-send-key key-name nil nil nil))
       ;; Path 2: dispatch the key in the vterm window's local map.
       ;; Hits whatever vterm's mode-map binds <prior>/<next> to —
       ;; usually vterm--self-insert which forwards to libvterm.
       (win
        (with-selected-window win
          (let ((cmd (or (lookup-key vterm-mode-map (kbd key-name))
                          (lookup-key (current-local-map) (kbd key-name)))))
            (if (commandp cmd)
                (call-interactively cmd)
              (vterm-send-string escape-seq)))))
       ;; Path 3: write the raw escape sequence directly to the
       ;; vterm subprocess's stdin. Bypasses vterm's send-string
       ;; abstraction, but only works when there's a live process.
       ((get-buffer-process buf)
        (process-send-string (get-buffer-process buf) escape-seq))
       ;; Path 4: last-resort vterm-send-string with the escape.
       (t (vterm-send-string escape-seq))))))

;;;###autoload
(defun org-llm-chat-page-up ()
  "Scroll opencode chat up one page.
Chat scroll is opencode's NATIVE keypress handler, not the
slash-validation path that rejected the /sys* injections — so
this is one of the rare places vterm-side dispatch works at all."
  (interactive)
  (org-llm--vterm-send-key-multi "<prior>" "\e[5~"))

;;;###autoload
(defun org-llm-chat-page-down ()
  "Scroll opencode chat down one page."
  (interactive)
  (org-llm--vterm-send-key-multi "<next>" "\e[6~"))

;;;###autoload
(defun org-llm-opencode-quit ()
  "Cleanly close the running opencode vterm (Ctrl-c twice).
This one DOES use vterm-send-string because the action bridge is
inside opencode — once opencode dies, the plugin can't dispatch
its own shutdown. Quit signals must reach the process directly."
  (interactive)
  (let ((buf (or (org-llm--opencode-vterm-buffer)
                 (user-error "No opencode vterm buffer found"))))
    (with-current-buffer buf
      (vterm-send-string "\C-c")
      (sit-for 0.1)
      (vterm-send-string "\C-c"))))


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

;;;###autoload
(defun org-llm-log-export (path &optional kind grep)
  "Append filtered Captain's Log rows to PATH (any org file).
With \\[universal-argument] also prompt for KIND filter; double prefix
also prompts for GREP."
  (interactive
   (list (read-file-name "Export to org file: " "~/org/" nil nil)
         (when current-prefix-arg
           (completing-read "kind (blank = all): "
                            '("" "cli" "llm" "mcp" "config" "doctor" "dbt" "embed")
                            nil t))
         (when (equal current-prefix-arg '(16))
           (read-string "grep (blank = none): "))))
  (let ((args (concat (format " --export %s" (shell-quote-argument path))
                      (and kind (not (string-empty-p kind))
                           (format " --kind %s" kind))
                      (and grep (not (string-empty-p grep))
                           (format " --grep %s" (shell-quote-argument grep))))))
    (org-llm--shell (concat "log" args) "*org-llm: log-export*")))


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

;;;###autoload
(defun org-llm-skill-examples ()
  "Install the bundled starter-skills bundle into ~/org."
  (interactive)
  (org-llm--shell "skill-examples" "*org-llm: skill-examples*"))

;;;###autoload
(defun org-llm-code-gen (task)
  "Top-level alias for `org-llm code generate TASK` (back-compat).
Use TASK as the prompt for the code-generation model."
  (interactive "sCode-gen task: ")
  (org-llm--vterm (format "%s code-gen %s" org-llm-binary
                           (shell-quote-argument task))))

;;;###autoload
(defun org-llm-onboarding ()
  "Crew commissioning sequence — alias for `org-llm setup`."
  (interactive)
  (org-llm--vterm (format "%s onboarding" org-llm-binary)))


;;; ── Themes: LCARS palette + theme-studio surface registry ───────────────────

;;;###autoload
(defun org-llm-palette (&optional name)
  "Pick an LCARS palette by NAME (classic | red | green | gold | violet).
With no arg, opens the text-based picker showing all palettes + the
current selection."
  (interactive (list (completing-read "Palette (blank = picker): "
                                       '("" "classic" "red" "green"
                                         "gold" "violet" "reset")
                                       nil nil)))
  (org-llm--shell (concat "palette" (and name (not (string-empty-p name))
                                            (concat " " name)))
                   "*org-llm: palette*"))

;;;###autoload
(defun org-llm-theme-studio-show ()
  "Show every themed surface + its currently-active value."
  (interactive)
  (org-llm--shell "theme-studio show" "*org-llm: theme-studio*"))

;;;###autoload
(defun org-llm-theme-studio-regenerate ()
  "Regenerate the LLM-driven theme cache for active dials."
  (interactive)
  (org-llm--vterm (format "%s theme-studio regenerate" org-llm-binary)))

;;;###autoload
(defun org-llm-theme-studio-verify ()
  "Re-run the quality gate over every cached themed variant."
  (interactive)
  (org-llm--shell "theme-studio verify" "*org-llm: theme-studio verify*"))


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

       ;; ─── Opencode-vterm bridge (Phase 18.4) ─────────────────────────
       ;; Drives a running opencode TUI from outside vterm by injecting
       ;; /sys* slash commands. Works in Emacs vterm where ctrl-prefix
       ;; would otherwise be eaten by Emacs commands. Locator pattern
       ;; matches any vterm buffer with "org-llm" in its name (set by
       ;; `org-llm-launch' / `org-llm-claude').
       (:prefix ("y" . "opencode (in-session)")
        :desc "Sidebar scroll ↓"         "j" #'org-llm-sys-scroll-down
        :desc "Sidebar scroll ↑"         "k" #'org-llm-sys-scroll-up
        :desc "Sidebar page ↓"           "J" #'org-llm-sys-page-down
        :desc "Sidebar page ↑"           "K" #'org-llm-sys-page-up
        :desc "Chat scroll ↑ (PgUp)"     "H" #'org-llm-chat-page-up
        :desc "Chat scroll ↓ (PgDn)"     "L" #'org-llm-chat-page-down
        :desc "/sysdoctor"               "d" #'org-llm-sys-doctor
        :desc "/sysstats"                "s" #'org-llm-sys-stats
        :desc "/sysrecent"               "r" #'org-llm-sys-recent
        :desc "/sysmodels"               "m" #'org-llm-sys-models
        :desc "/syscloud"                "c" #'org-llm-sys-cloud
        :desc "/sysmenu"                 "M" #'org-llm-sys-menu
        :desc "/insights"                "i" #'org-llm-sys-insights
        :desc "Quit opencode (Ctrl-c x2)" "q" #'org-llm-opencode-quit

        ;; Phase 18.4-iter9: action-bridge extensions. Same JSON
        ;; channel as scroll/slash; `p`/`P` for prompt fill/submit,
        ;; `R` for region-as-question (most common workflow), `=`
        ;; for force-refresh, `~` for raw toast injection.
        :desc "Prompt fill (no submit)"  "p" #'org-llm-sys-prompt-fill
        :desc "Prompt submit (auto)"     "P" #'org-llm-sys-prompt-submit
        :desc "Send region as question"  "R" #'org-llm-sys-send-region
        :desc "Refresh sidebar status"   "=" #'org-llm-sys-refresh-status
        :desc "Toast (notify)"           "~" #'org-llm-sys-toast)

       ;; ─── Captain's Log ──────────────────────────────────────────────
       (:prefix ("L" . "log")
        :desc "Recent entries"           "L" #'org-llm-log
        :desc "Grep"                     "g" #'org-llm-log-grep
        :desc "Filter by kind"           "k" #'org-llm-log-kind
        :desc "LLM reflect"              "r" #'org-llm-log-reflect
        :desc "Export → org file"        "e" #'org-llm-log-export
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
        :desc "Install starter bundle"   "e" #'org-llm-skill-examples
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
        :desc "Knob (interactive)"       "k" #'org-llm-knob
        :desc "LCARS palette picker"     "p" #'org-llm-palette
        :desc "Theme-studio show"        "s" #'org-llm-theme-studio-show
        :desc "Theme-studio regenerate"  "g" #'org-llm-theme-studio-regenerate
        :desc "Theme-studio verify"      "v" #'org-llm-theme-studio-verify)

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
