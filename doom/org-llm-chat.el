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

(defun org-llm-chat--build-proxy-payload (agent prompt &optional streaming)
  "Build the OpenAI-compat JSON body sent to the proxy.
AGENT may be nil. The proxy's `intercept_agent_prefix' (DEC-008
— proxy-seam @-prefix swap) reads the `agent' field server-side
and rewrites the persona/model. We still forward the prefix in
the user message so non-proxy upstreams degrade gracefully.

When STREAMING is non-nil, sets `stream: true' in the payload so
the proxy emits SSE (DEC-015 v0.2)."
  (let* ((user-text (if (and agent (not (string-empty-p agent)))
                        (format "@%s %s" agent prompt)
                      prompt))
         (payload `(("messages" . [(("role" . "user")
                                    ("content" . ,user-text))])
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

(defun org-llm-chat--call-backend (agent prompt marker)
  "Call the backend ASYNC for AGENT + PROMPT, replacing MARKER on completion.
DEC-015 v0.1: prefer HTTP to the running llm-proxy when its port
file is present + reachable; fall back to shell-out when the
proxy isn't running (preserves v0 behaviour).

DEC-015 v0.2: when `org-llm-chat-streaming' is non-nil (default)
AND the proxy is reachable, request SSE streaming and rerender
the placeholder per-token. Setup failures fall back to the v0.1
non-streaming path automatically."
  (let ((port (org-llm-chat--read-proxy-port)))
    (cond
     ((and port (org-llm-chat--proxy-reachable-p port)
           org-llm-chat-streaming)
      ;; Try SSE; on setup error fall back to non-streaming proxy.
      (condition-case _err
          (org-llm-chat--call-backend-proxy-sse agent prompt marker port)
        (error
         (org-llm-chat--call-backend-proxy agent prompt marker port))))
     ((and port (org-llm-chat--proxy-reachable-p port))
      (org-llm-chat--call-backend-proxy agent prompt marker port))
     (t
      ;; fallback for when proxy not running — v0 shell-out path
      (org-llm-chat--call-backend-shell agent prompt marker)))))

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
empty body, and stores the running insertion marker on STATE."
  (let ((marker (plist-get state :marker))
        (agent  (plist-get state :agent))
        (buf    (plist-get state :buffer)))
    (when (and marker (marker-buffer marker) (buffer-live-p buf))
      (with-current-buffer buf
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
            (insert (format "** @%s\n" (or agent "agent")))
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
                              (re-search-backward "^\\*\\* @" nil t)
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
          (ignore-errors (save-buffer)))
         (t
          ;; Error path — render an ERROR heading + message.
          (when (and ins (marker-buffer ins))
            (save-excursion
              (goto-char ins)
              (insert (format "\nERROR: %s\n"
                              (or (plist-get state :error)
                                  "stream failed")))))))
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
