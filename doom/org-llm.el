;;; org-llm.el --- Doom Emacs integration for org-llm -*- lexical-binding: t; -*-

(defvar org-llm-binary (expand-file-name "~/.local/bin/org-llm")
  "Path to the org-llm CLI binary.")

(defvar org-llm-ask-buffer   "*org-llm: ask*")
(defvar org-llm-search-buffer "*org-llm: search*")
(defvar org-llm-report-buffer "*org-llm: report*")


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
  "Send CMD to a dedicated org-llm vterm buffer."
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
  "Show model assignments and available Ollama models."
  (interactive)
  (org-llm--run-display (format "%s models" org-llm-binary) "*org-llm: models*"))

;;;###autoload
(defun org-llm-ask-dwim ()
  "Ask about text at point or region."
  (interactive)
  (let ((text (if (use-region-p)
                  (buffer-substring-no-properties (region-beginning) (region-end))
                (thing-at-point 'sentence t))))
    (when text
      (org-llm-ask (string-trim text)))))


;;; ── Doom keybindings ─────────────────────────────────────────────────────────

(map! :leader
      (:prefix ("l" . "org-llm")
       :desc "Ask notes"          "a" #'org-llm-ask
       :desc "Ask (reason model)" "A" (cmd! (org-llm-ask (read-string "Ask (reason): ") t))
       :desc "Search notes"       "s" #'org-llm-search
       :desc "Keyword search"     "S" (cmd! (org-llm-search (read-string "Keyword: ") t))
       :desc "Report"             "r" #'org-llm-report
       :desc "Index"              "i" #'org-llm-index
       :desc "Embed"              "e" #'org-llm-embed
       :desc "Models"             "m" #'org-llm-models
       :desc "Ask dwim"           "." #'org-llm-ask-dwim))

(provide 'org-llm)
;;; org-llm.el ends here
