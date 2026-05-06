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

(provide 'test_org_llm_chat)
;;; test_org_llm_chat.el ends here
