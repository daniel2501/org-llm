;;; test_org_llm_chat.el --- ert tests for org-llm-chat -*- lexical-binding: t; -*-
;; Run with:
;;   emacs -batch -L doom/ -l ert -l tests/test_org_llm_chat.el \
;;         -f ert-run-tests-batch-and-exit

(require 'ert)
(require 'cl-lib)

;; Resolve doom/ relative to this test file so the test runs from any cwd.
(let* ((this-file (or load-file-name buffer-file-name))
       (root (file-name-directory
              (directory-file-name
               (file-name-directory (or this-file "."))))))
  (add-to-list 'load-path (expand-file-name "doom" root)))

(require 'org)
(require 'org-llm-chat)

;; ── @-prefix parsing ──────────────────────────────────────────────────────

(ert-deftest org-llm-chat/parse-at-prefix-basic ()
  (let ((p (org-llm-chat-parse-agent-prefix "@picard hello")))
    (should (equal (car p) "picard"))
    (should (equal (cdr p) "hello"))))

(ert-deftest org-llm-chat/parse-at-prefix-multiline ()
  (let ((p (org-llm-chat-parse-agent-prefix
            "@analyst summarise:\n  - point 1\n  - point 2")))
    (should (equal (car p) "analyst"))
    (should (string-match-p "point 1" (cdr p)))
    (should (string-match-p "point 2" (cdr p)))))

(ert-deftest org-llm-chat/parse-at-prefix-force-solo ()
  (let ((p (org-llm-chat-parse-agent-prefix "@spock! tell me logic")))
    (should (equal (car p) "spock"))
    (should (equal (cdr p) "tell me logic"))))

(ert-deftest org-llm-chat/parse-at-prefix-leading-ws ()
  (let ((p (org-llm-chat-parse-agent-prefix "   @data ping")))
    (should (equal (car p) "data"))
    (should (equal (cdr p) "ping"))))

(ert-deftest org-llm-chat/parse-at-prefix-none ()
  (should (null (org-llm-chat-parse-agent-prefix "no prefix here")))
  (should (null (org-llm-chat-parse-agent-prefix "")))
  (should (null (org-llm-chat-parse-agent-prefix "@ no-name")))
  ;; Prefix without trailing space + body shouldn't match either.
  (should (null (org-llm-chat-parse-agent-prefix "@picard"))))


;; ── filename generation ───────────────────────────────────────────────────

(ert-deftest org-llm-chat/session-file-shape ()
  (let* ((path (org-llm-chat--session-file "2026-05-06" "abcdef")))
    (should (string-suffix-p "/2026-05-06-abcdef.org" path))
    (should (string-prefix-p (expand-file-name org-llm-chat-sessions-dir)
                              path))))

(ert-deftest org-llm-chat/short-id-deterministic-shape ()
  (let ((sid (org-llm-chat--short-id)))
    (should (= (length sid) 6))
    (should (string-match-p "\\`[0-9a-f]\\{6\\}\\'" sid))))


;; ── prompt body extraction (the core C-c C-c parser) ──────────────────────

(defun org-llm-chat--with-buffer (text body-fn)
  "Run BODY-FN in a temp org-mode buffer pre-loaded with TEXT."
  (with-temp-buffer
    (insert text)
    (org-mode)
    (goto-char (point-min))
    (funcall body-fn)))

(ert-deftest org-llm-chat/extracts-me-body-simple ()
  (org-llm-chat--with-buffer
   "* Capture from chat\n** Me\nWhat is the recipe-ab status?\n** @analyst\nstatus: green.\n"
   (lambda ()
     (re-search-forward "^\\*\\* Me$")
     (let ((body (org-llm-chat--current-heading-body)))
       (should (equal body "What is the recipe-ab status?"))))))

(ert-deftest org-llm-chat/extracts-me-body-with-multiple-l2 ()
  (org-llm-chat--with-buffer
   (concat
    "* Capture\n"
    "** Me\nfirst question\n"
    "** @picard\nfirst answer\n"
    "** Me\nsecond question with @analyst inside body\n"
    "** @picard\nsecond answer\n")
   (lambda ()
     ;; Move to the second ** Me
     (re-search-forward "^\\*\\* Me$")
     (re-search-forward "^\\*\\* Me$")
     (let ((body (org-llm-chat--current-heading-body)))
       (should (equal body "second question with @analyst inside body"))))))

(ert-deftest org-llm-chat/strips-properties-drawer ()
  (org-llm-chat--with-buffer
   (concat
    "* Capture\n"
    "** Me\n"
    ":PROPERTIES:\n"
    ":CREATED: [2026-05-06]\n"
    ":END:\n"
    "actual prompt\n")
   (lambda ()
     (re-search-forward "^\\*\\* Me$")
     (let ((body (org-llm-chat--current-heading-body)))
       (should (equal body "actual prompt"))))))


;; ── markdown → org src-block conversion ───────────────────────────────────

(ert-deftest org-llm-chat/markdown-fence-to-org-src ()
  (let* ((md "Here is code:\n```python\nprint('hi')\n```\nDone.")
         (out (org-llm-chat--markdown->org md)))
    (should (string-match-p "#\\+begin_src python" out))
    (should (string-match-p "#\\+end_src" out))
    (should (string-match-p "print('hi')" out))))

(ert-deftest org-llm-chat/markdown-fence-no-lang ()
  (let* ((md "```\nplain code\n```")
         (out (org-llm-chat--markdown->org md)))
    (should (string-match-p "#\\+begin_src text" out))))


;; ── refile target resolution (file-based fallback path) ───────────────────

(ert-deftest org-llm-chat/refile-to-file-appends ()
  (let* ((tmp (make-temp-file "org-llm-chat-refile-" nil ".org"))
         (src (concat
               "* Capture\n** Me\nq?\n** @picard\nanswer body\n")))
    (unwind-protect
        (with-temp-buffer
          (insert src)
          (org-mode)
          (goto-char (point-min))
          (re-search-forward "^\\*\\* @picard$")
          (org-llm-chat--refile-to-file tmp nil)
          (with-temp-buffer
            (insert-file-contents tmp)
            (let ((content (buffer-string)))
              (should (string-match-p "@picard" content))
              (should (string-match-p "answer body" content)))))
      (ignore-errors (delete-file tmp)))))

;; ── DEC-015 v0.1 — proxy-port discovery ──────────────────────────────────

(ert-deftest org-llm-chat/proxy-port-discovery ()
  "Given a temp port file with a valid port, `--read-proxy-port' returns it."
  (let* ((tmp (make-temp-file "org-llm-proxy-port-")))
    (unwind-protect
        (progn
          (with-temp-file tmp (insert "54321\n"))
          (let ((org-llm-chat-proxy-port-file tmp))
            (should (equal (org-llm-chat--read-proxy-port) 54321))))
      (ignore-errors (delete-file tmp)))))

(ert-deftest org-llm-chat/proxy-port-missing-returns-nil ()
  "Missing port file → nil → fallback to shell-out path."
  (let ((org-llm-chat-proxy-port-file
         (expand-file-name "definitely-not-a-real-file"
                            temporary-file-directory)))
    (ignore-errors
      (delete-file (expand-file-name org-llm-chat-proxy-port-file)))
    (should (null (org-llm-chat--read-proxy-port)))))

(ert-deftest org-llm-chat/proxy-port-rejects-garbage ()
  "Non-integer file content → nil (graceful fallback, no error)."
  (let ((tmp (make-temp-file "org-llm-proxy-port-")))
    (unwind-protect
        (progn
          (with-temp-file tmp (insert "not-a-port\n"))
          (let ((org-llm-chat-proxy-port-file tmp))
            (should (null (org-llm-chat--read-proxy-port)))))
      (ignore-errors (delete-file tmp)))))

(ert-deftest org-llm-chat/proxy-port-rejects-out-of-range ()
  "Port outside 1..65535 → nil."
  (let ((tmp (make-temp-file "org-llm-proxy-port-")))
    (unwind-protect
        (progn
          (with-temp-file tmp (insert "70000\n"))
          (let ((org-llm-chat-proxy-port-file tmp))
            (should (null (org-llm-chat--read-proxy-port)))))
      (ignore-errors (delete-file tmp)))))

(ert-deftest org-llm-chat/build-proxy-payload-shape ()
  "Payload is JSON, includes messages array, agent field when set,
and the configured model. Sidebar injection disabled here so we
test the bare single-user-message form."
  (let* ((org-llm-chat-inject-sidebar nil)
         (org-llm-chat-default-model "claude-sonnet-4.6")
         (json (org-llm-chat--build-proxy-payload "spock" "hello"))
         (parsed (let ((json-object-type 'alist)
                       (json-array-type  'list)
                       (json-key-type    'string))
                   (json-read-from-string json))))
    (should (equal (cdr (assoc "agent" parsed)) "spock"))
    (should (equal (cdr (assoc "model" parsed)) "claude-sonnet-4.6"))
    (let* ((msgs (cdr (assoc "messages" parsed)))
           (first (car msgs)))
      (should (equal (cdr (assoc "role" first)) "user"))
      (should (string-match-p "@spock hello"
                              (cdr (assoc "content" first)))))
    ;; stream is JSON false (`:json-false' encodes to literal "false")
    (should (string-match-p "\"stream\":[ \t]*false" json))))

(ert-deftest org-llm-chat/build-proxy-payload-no-agent ()
  "Without an agent, no `agent' field is emitted and prompt is verbatim.
Sidebar injection disabled here so we test the bare form."
  (let* ((org-llm-chat-inject-sidebar nil)
         (json (org-llm-chat--build-proxy-payload nil "plain prompt"))
         (parsed (let ((json-object-type 'alist)
                       (json-array-type  'list)
                       (json-key-type    'string))
                   (json-read-from-string json))))
    (should (null (assoc "agent" parsed)))
    (let* ((msgs (cdr (assoc "messages" parsed)))
           (first (car msgs)))
      (should (equal (cdr (assoc "content" first)) "plain prompt")))))

(ert-deftest org-llm-chat/extract-openai-text-happy-path ()
  "Pull content from a typical OpenAI-compat JSON body."
  (let* ((json (concat
                "{\"choices\":[{\"message\":"
                "{\"role\":\"assistant\",\"content\":\"hi there\"}}]}"))
         (out (org-llm-chat--extract-openai-text json)))
    (should (equal out "hi there"))))

(ert-deftest org-llm-chat/extract-openai-text-bad-json ()
  "Malformed JSON → nil (caller falls back to raw body)."
  (should (null (org-llm-chat--extract-openai-text "not json {{{")))
  (should (null (org-llm-chat--extract-openai-text ""))))


;; ── DEC-015 v0.2 — SSE streaming ─────────────────────────────────────────

(ert-deftest org-llm-chat/sse-parse-data-chunk-extracts-delta ()
  "A typical OpenAI-compat SSE chunk yields the delta.content string."
  (let* ((line (concat
                "data: {\"id\":\"x\",\"choices\":[{\"index\":0,"
                "\"delta\":{\"content\":\"Hello\"}}]}"))
         (out (org-llm-chat--sse-parse-data-chunk line)))
    (should (equal out "Hello"))))

(ert-deftest org-llm-chat/sse-parse-data-chunk-role-marker-no-delta ()
  "First chunk often only carries `delta.role' — no content yet → nil."
  (let* ((line (concat
                "data: {\"choices\":[{\"index\":0,"
                "\"delta\":{\"role\":\"assistant\"}}]}"))
         (out (org-llm-chat--sse-parse-data-chunk line)))
    (should (null out))))

(ert-deftest org-llm-chat/sse-parse-data-chunk-malformed-returns-nil ()
  "Malformed JSON in a `data:' line returns nil — caller skips it."
  (should (null (org-llm-chat--sse-parse-data-chunk
                 "data: not-json {{{")))
  (should (null (org-llm-chat--sse-parse-data-chunk
                 "data: ")))
  (should (null (org-llm-chat--sse-parse-data-chunk
                 "not even a data line"))))

(ert-deftest org-llm-chat/sse-parse-data-chunk-done-returns-nil ()
  "`data: [DONE]' yields nil from the chunk parser; the done-marker
predicate handles it separately."
  (should (null (org-llm-chat--sse-parse-data-chunk "data: [DONE]")))
  (should (org-llm-chat--sse-done-marker-p "data: [DONE]"))
  (should (org-llm-chat--sse-done-marker-p "data:[DONE]"))
  (should (org-llm-chat--sse-done-marker-p "data:  [DONE]  "))
  (should (null (org-llm-chat--sse-done-marker-p
                 "data: {\"choices\":[]}"))))

(ert-deftest org-llm-chat/streaming-defcustom-toggle ()
  "When `org-llm-chat-streaming' is nil, the dispatcher must NOT
take the SSE branch — confirmed by checking the build payload's
`stream' field reflects the toggle."
  (let* ((org-llm-chat-inject-sidebar nil)
         (org-llm-chat-streaming nil)
         (json (org-llm-chat--build-proxy-payload "spock" "hi" nil)))
    (should (string-match-p "\"stream\":[ \t]*false" json)))
  (let* ((org-llm-chat-inject-sidebar nil)
         (org-llm-chat-streaming t)
         (json (org-llm-chat--build-proxy-payload "spock" "hi" t)))
    (should (string-match-p "\"stream\":[ \t]*true" json))))

(ert-deftest org-llm-chat/sse-process-buffer-multi-chunk ()
  "Drain a buffer of multiple SSE lines + leave a partial trailing
line in place for the next chunk."
  (with-temp-buffer
    (org-mode)
    (insert "* C\n** Me\nhello\n** @picard\n/thinking…/\n")
    (goto-char (point-min))
    (re-search-forward "^\\*\\* @picard$")
    (beginning-of-line)
    (let* ((marker (point-marker))
           (state (list :marker marker
                        :agent  "picard"
                        :buffer (current-buffer)
                        :inserted-pos nil
                        :raw-buffer (concat
                                     "data: {\"choices\":[{\"delta\":"
                                     "{\"role\":\"assistant\"}}]}\n"
                                     "data: {\"choices\":[{\"delta\":"
                                     "{\"content\":\"Hi \"}}]}\n"
                                     "data: {\"choices\":[{\"delta\":"
                                     "{\"content\":\"there\"}}]}\n"
                                     "data: {\"choices\":[{\"delta\":"  ; partial
                                     )
                        :header-done t
                        :content ""
                        :done nil
                        :rc 0
                        :error nil)))
      ;; Initialise rendering to drop the placeholder + set ins.
      (org-llm-chat--sse-init-render state)
      (org-llm-chat--sse-process-buffer state)
      (should (equal (plist-get state :content) "Hi there"))
      (should (string-match-p "Hi there" (buffer-string)))
      (should (not (plist-get state :done)))
      ;; Trailing partial line must still be in :raw-buffer
      (should (string-match-p "data: {\"choices\""
                              (plist-get state :raw-buffer))))))

(ert-deftest org-llm-chat/sse-process-buffer-handles-done ()
  "Encountering `data: [DONE]' flips :done."
  (with-temp-buffer
    (org-mode)
    (insert "* C\n** Me\nq\n** @picard\n/thinking…/\n")
    (goto-char (point-min))
    (re-search-forward "^\\*\\* @picard$")
    (beginning-of-line)
    (let* ((marker (point-marker))
           (state (list :marker marker
                        :agent  "picard"
                        :buffer (current-buffer)
                        :inserted-pos nil
                        :raw-buffer (concat
                                     "data: {\"choices\":[{\"delta\":"
                                     "{\"content\":\"ok\"}}]}\n"
                                     "data: [DONE]\n")
                        :header-done t
                        :content ""
                        :done nil
                        :rc 0
                        :error nil)))
      (org-llm-chat--sse-init-render state)
      (org-llm-chat--sse-process-buffer state)
      (should (plist-get state :done))
      (should (equal (plist-get state :content) "ok")))))

;; ── DEC-015 — sidebar injection ──────────────────────────────────────────

(ert-deftest org-llm-chat/format-sidebar-tight-shape ()
  "Given a small synthesised STATUS, the formatted message contains
the bridge-telemetry header, key sections, and the no-fabricate
guidance footer."
  (let* ((status
          '(("vault"   . (("n_files" . 312)
                          ("n_nodes" . 1487)
                          ("n_embedded" . 1402)
                          ("pct_embedded" . 94)
                          ("org_dir" . "~/org")))
            ("vitals"  . ((("label" . "vault")  ("status" . "nominal"))
                          (("label" . "proxy")  ("status" . "nominal"))
                          (("label" . "tracker") ("status" . "yellow"))))
            ("active"  . (("palette" . "bridge-night")
                          ("intent_agent" . "picard")))
            ("model"   . (("active" . "claude-sonnet-4.6")
                          ("provider" . "anthropic")
                          ("route" . "cloud")))))
         (out (org-llm-chat--format-sidebar-system-message status)))
    (should (stringp out))
    (should (string-match-p "LIVE BRIDGE TELEMETRY" out))
    (should (string-match-p "VAULT" out))
    (should (string-match-p "VITALS" out))
    (should (string-match-p "MODEL" out))
    (let ((case-fold-search t))
      (should (string-match-p "do not invent" out)))))

(ert-deftest org-llm-chat/format-sidebar-graceful-missing-keys ()
  "Empty alist still produces a well-formed string with header +
no-fabricate guidance footer; no error raised."
  (let ((out (org-llm-chat--format-sidebar-system-message '())))
    (should (stringp out))
    (should (> (length out) 0))
    (should (string-match-p "LIVE BRIDGE TELEMETRY" out))
    (let ((case-fold-search t))
      (should (string-match-p "do not invent" out)))))

(ert-deftest org-llm-chat/build-payload-injects-sidebar-when-present ()
  "With injection ON and a stubbed status, payload has 3 messages
ordered system/system/user. With injection OFF, payload has 1
message (user only)."
  (cl-letf (((symbol-function 'org-llm-chat--read-sidebar-status)
             (lambda ()
               '(("vault" . (("n_files" . 1) ("n_nodes" . 2)))
                 ("model" . (("active" . "test")))))))
    (let* ((org-llm-chat-inject-sidebar t)
           (json (org-llm-chat--build-proxy-payload "picard" "hi"))
           (parsed (let ((json-object-type 'alist)
                         (json-array-type  'list)
                         (json-key-type    'string))
                     (json-read-from-string json)))
           (msgs (cdr (assoc "messages" parsed))))
      (should (= (length msgs) 3))
      (should (equal (cdr (assoc "role" (nth 0 msgs))) "system"))
      (should (equal (cdr (assoc "role" (nth 1 msgs))) "system"))
      (should (equal (cdr (assoc "role" (nth 2 msgs))) "user"))
      (should (string-match-p "persona slot"
                              (cdr (assoc "content" (nth 0 msgs)))))
      (should (string-match-p "LIVE BRIDGE TELEMETRY"
                              (cdr (assoc "content" (nth 1 msgs)))))
      (should (string-match-p "@picard hi"
                              (cdr (assoc "content" (nth 2 msgs)))))))
  ;; Toggle off → `--read-sidebar-status' returns nil → 1 message.
  ;; The real `--read-sidebar-status' guards on `org-llm-chat-inject-sidebar',
  ;; so we stub it to nil here to model that guarded behaviour.
  (cl-letf (((symbol-function 'org-llm-chat--read-sidebar-status)
             (lambda () nil)))
    (let* ((org-llm-chat-inject-sidebar nil)
           (json (org-llm-chat--build-proxy-payload "picard" "hi"))
           (parsed (let ((json-object-type 'alist)
                         (json-array-type  'list)
                         (json-key-type    'string))
                     (json-read-from-string json)))
           (msgs (cdr (assoc "messages" parsed))))
      (should (= (length msgs) 1))
      (should (equal (cdr (assoc "role" (nth 0 msgs))) "user")))))


;; ── prompt-marker body strip ──────────────────────────────────────────────

(ert-deftest org-llm-chat/body-strips-prompt-marker ()
  "Leading `❯ ' (configured marker) is stripped from the body the LLM sees."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\n"
            "❯ system status?\n")
    (goto-char (point-min))
    (re-search-forward (org-llm-chat--user-heading-line-regex))
    (let ((body (org-llm-chat--current-heading-body)))
      (should (equal body "system status?")))))

(ert-deftest org-llm-chat/body-no-marker-still-clean ()
  "Body without the marker prefix is returned trimmed, unchanged."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\n"
            "  hello there  \n")
    (goto-char (point-min))
    (re-search-forward (org-llm-chat--user-heading-line-regex))
    (let ((body (org-llm-chat--current-heading-body)))
      (should (equal body "hello there")))))

;; ── per-agent glyphs ──────────────────────────────────────────────────────

(ert-deftest org-llm-chat/agent-heading-text-uses-configured-glyph ()
  "Configured handles render as `<glyph> @<name>'.
Test owns its fixture — chat module ships with NO default
roster (per the architectural rule that agent names must not be
hardcoded outside the core agent registry)."
  (let ((org-llm-chat-agent-glyphs '(("foo" . "★")
                                       ("bar" . "Δ"))))
    (should (equal (org-llm-chat--agent-heading-text "foo") "★ @foo"))
    (should (equal (org-llm-chat--agent-heading-text "bar") "Δ @bar"))))

(ert-deftest org-llm-chat/agent-heading-text-unknown-falls-back ()
  "Unconfigured agent names render as bare `@<name>' with no glyph."
  (let ((org-llm-chat-agent-glyphs '()))
    (should (equal (org-llm-chat--agent-heading-text "stranger")
                    "@stranger"))))

(ert-deftest org-llm-chat/agent-heading-text-empty-default ()
  "Default `org-llm-chat-agent-glyphs' is empty — chat surface
is roster-agnostic out of the box."
  (should (equal org-llm-chat-agent-glyphs '())))

(ert-deftest org-llm-chat/agent-heading-text-suffix ()
  "Optional SUFFIX is appended after the handle (used by ERROR path)."
  (let ((org-llm-chat-agent-glyphs '(("foo" . "★"))))
    (should (equal (org-llm-chat--agent-heading-text "foo" " ERROR")
                    "★ @foo ERROR"))))

;; ── user heading + auto-next-turn ─────────────────────────────────────────

(ert-deftest org-llm-chat/user-heading-regex-accepts-both-shapes ()
  "Regex must match the configured heading AND legacy `Me' for back-compat."
  (let ((rgx (org-llm-chat--user-heading-regex)))
    (should (string-match-p rgx (string-trim org-llm-chat-user-heading)))
    (should (string-match-p rgx "Me"))
    (should (string-match-p rgx "Me [Y]"))
    (should-not (string-match-p rgx "Captain Picard"))))

(ert-deftest org-llm-chat/append-next-turn-inserts-heading ()
  "After a response, the buffer ends with a fresh user heading + point on it."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\nq?\n** @picard\nans\n")
    (org-llm-chat--append-next-turn)
    (goto-char (point-min))
    (let ((count 0))
      (while (re-search-forward (org-llm-chat--user-heading-line-regex)
                                  nil t)
        (cl-incf count))
      (should (= count 2)))))

(ert-deftest org-llm-chat/append-next-turn-idempotent ()
  "Calling twice doesn't double-insert when buffer already ends with a heading."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\n")
    (org-llm-chat--append-next-turn)
    (org-llm-chat--append-next-turn)
    (goto-char (point-min))
    (let ((count 0))
      (while (re-search-forward (org-llm-chat--user-heading-line-regex)
                                  nil t)
        (cl-incf count))
      (should (= count 1)))))

;; ── telemetry source switch ───────────────────────────────────────────────

(ert-deftest org-llm-chat/telemetry-cli-respects-toggle ()
  "When `inject-sidebar' is nil, --read-sidebar-status returns nil."
  (let ((org-llm-chat-inject-sidebar nil))
    (should (null (org-llm-chat--read-sidebar-status)))))

(ert-deftest org-llm-chat/telemetry-cli-fallbacks-to-file ()
  "When CLI verb is missing, fall back to JSON file (legacy path)."
  (let* ((tmp (make-temp-file "telemetry-" nil ".json"
                                "{\"vault\":{\"n_files\":42}}"))
         (org-llm-chat-inject-sidebar t)
         (org-llm-chat-telemetry-source 'cli)
         (org-llm-chat-telemetry-cli "/nonexistent/no-such-bin")
         (org-llm-chat-sidebar-status-file tmp)
         (org-llm-chat--telemetry-cache nil))
    (unwind-protect
        (let ((data (org-llm-chat--read-sidebar-status)))
          (should data)
          (should (equal (cdr (assoc "n_files" (cdr (assoc "vault" data))))
                          42)))
      (ignore-errors (delete-file tmp)))))

;; ── LCARS prompt frame ────────────────────────────────────────────────────

(ert-deftest org-llm-chat/prompt-frame-strings-shape ()
  "Strings carry the configured face + the COMPOSE label."
  (let ((bounds (org-llm-chat--prompt-frame-strings)))
    (should (stringp (car bounds)))
    (should (stringp (cdr bounds)))
    (should (string-match-p "COMPOSE" (car bounds)))
    (should (eq (get-text-property 0 'face (car bounds))
                 'org-llm-chat-prompt-frame-face))))

(ert-deftest org-llm-chat/prompt-frame-draw-clear-cycle ()
  "Draw creates 2 overlays; clear removes them; idempotent."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\nq\n")
    (let ((org-llm-chat-prompt-frame t))
      (org-llm-chat--draw-prompt-frame)
      (should (= (length org-llm-chat--prompt-frame-overlays) 2))
      (org-llm-chat--clear-prompt-frame)
      (should (null org-llm-chat--prompt-frame-overlays))
      ;; double-clear is fine.
      (org-llm-chat--clear-prompt-frame)
      (should (null org-llm-chat--prompt-frame-overlays)))))

(ert-deftest org-llm-chat/prompt-frame-disabled-no-op ()
  "When defcustom is nil, draw inserts no overlays."
  (with-temp-buffer
    (org-mode)
    (insert "* Capture\n** " org-llm-chat-user-heading "\n")
    (let ((org-llm-chat-prompt-frame nil))
      (org-llm-chat--draw-prompt-frame)
      (should (null org-llm-chat--prompt-frame-overlays)))))


(provide 'test_org_llm_chat)
;;; test_org_llm_chat.el ends here
