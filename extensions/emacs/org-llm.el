;;; org-llm.el --- Emacs integration for org-llm  -*- lexical-binding: t; -*-

;; Copyright (C) 2026 Daniel Benedict
;; URL: https://github.com/daniel2501/org-llm
;; Version: 0.1.0
;; Package-Requires: ((emacs "28.1") (org "9.6"))
;; Keywords: convenience, org-roam, llm

;; This file is licensed under the same terms as the org-llm project.

;;; Commentary:

;; First-class Emacs integration for the org-llm Python tool.  The CLI
;; verbs continue to work as-is — this package adds editor-side
;; conveniences that the README has documented for a while but were
;; never actually packaged: SPC-l keybinds for Doom, capture from any
;; buffer, jump-to-id, and a thin `org-llm-call' MCP wrapper for
;; reaching into org-llm tools without leaving Emacs.
;;
;; Phase 16.0 (this commit) ships the scaffolding only.  Real behavior
;; lands in Phase 16.3+.

;;; Code:

(require 'org)

(defgroup org-llm nil
  "Emacs-side integration with the org-llm Python tool."
  :group 'org
  :prefix "org-llm-")

(defcustom org-llm-binary "org-llm"
  "Path to the `org-llm' executable.
Defaults to whichever is on `exec-path'.  Override if you keep
multiple installations and want this Emacs to bind a specific one
\(useful for the daily-walkthrough corpus pattern\)."
  :type 'string
  :group 'org-llm)

(defcustom org-llm-org-dir nil
  "Override `org-llm''s `ORG_LLM_ORG_DIR' env var.
nil means inherit from the calling shell.  Set to a string to
pin a workspace from inside Emacs (the equivalent of using the
`./walk' wrapper script)."
  :type '(choice (const :tag "Inherit from shell" nil) directory)
  :group 'org-llm)

(defcustom org-llm-db nil
  "Override `org-llm''s `ORG_LLM_DB' env var.
nil means inherit.  Pair with `org-llm-org-dir' to switch
workspaces from Emacs without leaving the editor."
  :type '(choice (const :tag "Inherit" nil) file)
  :group 'org-llm)

;;;###autoload
(defun org-llm-version ()
  "Print the org-llm CLI version this Emacs is wired to."
  (interactive)
  (let ((bin (executable-find org-llm-binary)))
    (if bin
        (message "org-llm: %s (binary: %s)" (org-llm--shell "--version") bin)
      (user-error "org-llm binary %S not found on `exec-path'" org-llm-binary))))

(defun org-llm--shell (&rest args)
  "Run `org-llm' with ARGS, return stdout as a string."
  (let ((process-environment
         (append (when org-llm-org-dir
                   (list (format "ORG_LLM_ORG_DIR=%s" org-llm-org-dir)))
                 (when org-llm-db
                   (list (format "ORG_LLM_DB=%s" org-llm-db)))
                 process-environment)))
    (with-temp-buffer
      (apply #'call-process org-llm-binary nil t nil args)
      (string-trim (buffer-string)))))

(provide 'org-llm)
;;; org-llm.el ends here
