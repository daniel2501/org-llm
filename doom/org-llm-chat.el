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
;;   - The Bridge Crew agents (@picard, @spock, @data, @boothby,
;;     @geordi, @atoz, @riker) are seeded in the agents table.
;;     `org-llm agents --tangle` writes ~/org/org-llm-agents.org if not.
;;   - Optional: `org-llm launch` is running somewhere (for proxy-side
;;     @-prefix interception). v0 works without it; agent dispatch
;;     degrades gracefully to "ask" semantics.

(require 'org)
(require 'json)
(require 'cl-lib)
(require 'subr-x)

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

(defcustom org-llm-chat-default-agent "picard"
  "Agent name used when the prompt body has no `@<agent>` prefix.
The default `picard` is the manager — it dispatches to specialists
internally. To force user-supplied routing on every turn, set this
to nil."
  :type '(choice (const :tag "No default (require @prefix)" nil)
                 (string :tag "Agent name"))
  :group 'org-llm-chat)

(defcustom org-llm-chat-known-agents
  '("picard" "spock" "data" "gardener" "analyst" "curator" "tracker"
    "crew" "scribe" "researcher" "reviewer" "classifier")
  "Known agent names for `@<name>` prefix detection. Treated as a
hint set — unknown names still pass through; the proxy or backend
will tell the user if the agent is unknown."
  :type '(repeat string)
  :group 'org-llm-chat)

(defcustom org-llm-chat-binary
  (or (and (boundp 'org-llm-binary) org-llm-binary)
      (expand-file-name "~/.local/bin/org-llm"))
  "Path to the org-llm CLI binary."
  :type 'file
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
              "** Me\n"
              ""))))


;;; ── prompt parsing ─────────────────────────────────────────────────────────

(defun org-llm-chat--at-me-heading-p ()
  "True iff point is inside a `** Me` heading subtree."
  (save-excursion
    (and (not (org-before-first-heading-p))
         (progn (ignore-errors (org-back-to-heading t)) t)
         (let ((title (nth 4 (org-heading-components))))
           (and title
                (string-match-p
                 "\\`Me\\(\\s-*\\(\\[[A-Za-z0-9?]+\\]\\)\\)?\\'"
                 (string-trim title)))))))

(defun org-llm-chat--current-heading-body ()
  "Return the body text under the current heading (no subheadings stripped).
Trims leading/trailing whitespace + `:PROPERTIES:` drawer if present."
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

(defun org-llm-chat--insert-placeholder (agent)
  "Insert a `** @<agent> /thinking…/` heading after the current `** Me` subtree.
Returns a marker pointing at the start of the placeholder line."
  (save-excursion
    (org-back-to-heading t)
    (org-end-of-subtree t t)
    (unless (bolp) (insert "\n"))
    (let ((start (point-marker)))
      (insert (format "** @%s\n/thinking…/\n" (or agent "agent")))
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
    (concat (format "** @%s\n" (or agent "agent"))
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
      (goto-char (point-max))
      (when (re-search-backward "^\\*\\* Me\\s-*$" nil t)
        (forward-line 1)
        (end-of-line)))))

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
                       (string-match-p "\\`Me\\(\\s-*\\[[A-Za-z0-9?]+\\]\\)?\\'"
                                        (string-trim title)))))
      (unless is-me
        (user-error
         "Place point under a `** Me' heading to submit (got %S)"
         title))))
  (let* ((body  (org-llm-chat--current-heading-body))
         (parsed (org-llm-chat-parse-agent-prefix body))
         (agent  (or (and parsed (car parsed))
                     org-llm-chat-default-agent))
         (prompt (or (and parsed (cdr parsed)) body)))
    (when (string-empty-p (string-trim (or prompt "")))
      (user-error "Prompt body is empty — write your question under `** Me'"))
    (let ((marker (org-llm-chat--insert-placeholder agent)))
      (setq-local org-llm-chat--pending-marker marker)
      (org-llm-chat--call-backend agent prompt marker))))

(defun org-llm-chat--call-backend (agent prompt marker)
  "Call the backend ASYNC for AGENT + PROMPT, replacing MARKER on completion."
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

(defun org-llm-chat--finalise-response (marker agent raw rc)
  "Render RAW as the response under MARKER for AGENT; auto-save on success."
  (let* ((cleaned (org-llm-chat--strip-ansi (or raw "")))
         (heading-prefix (if (zerop rc)
                             (format "** @%s" agent)
                           (format "** @%s ERROR" agent)))
         (rendered
          (concat heading-prefix "\n"
                  (let ((md (org-llm-chat--markdown->org cleaned)))
                    (if (string-empty-p (string-trim (or md "")))
                        (format "(no response, exit %d)" rc)
                      md))
                  "\n")))
    (org-llm-chat--replace-placeholder marker rendered)
    (when (zerop rc)
      (ignore-errors (save-buffer)))
    (setq-local org-llm-chat--pending-marker nil)
    (setq-local org-llm-chat--pending-process nil)
    (when (re-search-forward "^\\*\\* @" nil t)
      (goto-char (line-end-position)))))

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
    (define-key m (kbd "C-c C-l p") #'org-llm-chat-pin)
    (define-key m (kbd "C-c C-l r") #'org-llm-chat-refile)
    (define-key m (kbd "C-c C-l e") #'org-llm-chat-export-subtree)
    m)
  "Keymap for `org-llm-chat-mode'.")

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

Export keys:
  C-c C-l p — pin current subtree to ~/org/chat-pins.org
  C-c C-l r — refile current subtree to an org-roam node
  C-c C-l e — export current subtree as standalone .org"
  :init-value nil
  :lighter " ✱chat"
  :keymap org-llm-chat-mode-map
  (if org-llm-chat-mode
      (add-hook 'org-ctrl-c-ctrl-c-hook
                #'org-llm-chat--ctrl-c-ctrl-c nil t)
    (remove-hook 'org-ctrl-c-ctrl-c-hook
                 #'org-llm-chat--ctrl-c-ctrl-c t)))

(provide 'org-llm-chat)
;;; org-llm-chat.el ends here
