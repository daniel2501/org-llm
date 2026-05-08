;;; verify_cell.el --- Headless cell verifier (R26 P2-2) -*- lexical-binding: t; -*-

;; Copyright (C) 2026  org-llm

;; Loaded by `emacs --batch -Q -l elisp/verify_cell.el -f verify-cell-main'.
;; Reads the path of the file under test from the VERIFY_FILE environment
;; variable, dispatches a semantic check by extension, and prints a JSON
;; diagnostics document of the form:
;;
;;     {"file": "...", "kind": "org|elisp|other",
;;      "errors": [...], "warnings": [...]}
;;
;; to stdout.  Exit status of the Emacs process is intentionally left at 0;
;; the bash wrapper inspects the JSON and decides 0/1 for the gate.

;;; Code:

(require 'json)
(require 'subr-x)
(require 'cl-lib)

(defvar verify-cell--errors   nil "Accumulated error strings.")
(defvar verify-cell--warnings nil "Accumulated warning strings.")

(defun verify-cell--push-error (fmt &rest args)
  (push (apply #'format fmt args) verify-cell--errors))

(defun verify-cell--push-warning (fmt &rest args)
  (push (apply #'format fmt args) verify-cell--warnings))

;; ---------------------------------------------------------------------------
;; Org verifier

(defun verify-cell--check-balanced-links ()
  "Walk the buffer counting [[ vs ]] in non-src regions.
`org-element-parse-buffer' is permissive about unclosed link brackets,
so we add a textual check that catches `[[id:abc][unclosed' style breakage."
  (save-excursion
    (goto-char (point-min))
    (let ((open 0) (close 0))
      (while (re-search-forward "\\[\\[" nil t)
        (cl-incf open))
      (goto-char (point-min))
      (while (re-search-forward "\\]\\]" nil t)
        (cl-incf close))
      (unless (= open close)
        (verify-cell--push-error
         "unbalanced link brackets: %d [[ vs %d ]]" open close)))))

(defun verify-cell-org (file)
  "Verify FILE as an org document."
  (condition-case err
      (with-temp-buffer
        (insert-file-contents file)
        ;; Avoid heavy org init; we just need the parser.
        (let ((inhibit-message t)
              (message-log-max nil))
          (require 'org)
          (require 'org-element)
          (let ((delay-mode-hooks t))
            (org-mode))
          ;; Parse; org-element raises on hard structural errors.
          (condition-case parse-err
              (org-element-parse-buffer)
            (error
             (verify-cell--push-error
              "org-element-parse-buffer: %s"
              (error-message-string parse-err))))
          ;; Best-effort id refresh — never let it fatal the run.
          (when (require 'org-id nil t)
            (condition-case id-err
                (let ((org-id-locations-file
                       (make-temp-file "verify-cell-id-")))
                  (org-id-update-id-locations (list file) t))
              (error
               (verify-cell--push-warning
                "org-id-update-id-locations: %s"
                (error-message-string id-err)))))
          (verify-cell--check-balanced-links)))
    (error
     (verify-cell--push-error "org verify crashed: %s"
                              (error-message-string err)))))

;; ---------------------------------------------------------------------------
;; Elisp verifier

(defun verify-cell--collect-checkdoc (file)
  "Run `checkdoc-file' on FILE, capturing diagnostics as warnings."
  (when (require 'checkdoc nil t)
    (let ((buf (get-buffer-create " *verify-cell-checkdoc*"))
          (inhibit-message t)
          (message-log-max nil))
      (unwind-protect
          (with-current-buffer buf
            (erase-buffer)
            (let ((standard-output buf))
              (condition-case cd-err
                  (checkdoc-file file)
                (error
                 (verify-cell--push-warning
                  "checkdoc-file: %s"
                  (error-message-string cd-err)))))
            (let ((out (string-trim (buffer-string))))
              (unless (string-empty-p out)
                (dolist (line (split-string out "\n" t))
                  (verify-cell--push-warning "checkdoc: %s"
                                             (string-trim line))))))
        (kill-buffer buf)))))

(defun verify-cell-elisp (file)
  "Verify FILE as an elisp source file."
  (condition-case err
      (let ((byte-compile-log-buffer " *verify-cell-bcl*")
            (inhibit-message t)
            (message-log-max nil)
            (byte-compile-verbose nil)
            (byte-compile-warnings t))
        ;; byte-compile-file returns nil on hard error, t / 'no-byte-compile
        ;; otherwise; warnings land in the log buffer.
        (let ((result
               (condition-case bc-err
                   (byte-compile-file file)
                 (error
                  (verify-cell--push-error
                   "byte-compile-file: %s"
                   (error-message-string bc-err))
                  nil))))
          (when (null result)
            (verify-cell--push-error
             "byte-compile-file returned nil for %s" file))
          (when (get-buffer byte-compile-log-buffer)
            (with-current-buffer byte-compile-log-buffer
              (let ((log (string-trim (buffer-string))))
                (unless (string-empty-p log)
                  (dolist (line (split-string log "\n" t))
                    (let ((trimmed (string-trim line)))
                      (cond
                       ((string-match-p ":Error:" trimmed)
                        (verify-cell--push-error "bytec: %s" trimmed))
                       ((string-match-p ":Warning:" trimmed)
                        (verify-cell--push-warning "bytec: %s" trimmed)))))))))
          ;; Clean up the .elc artifact so we don't pollute the worktree.
          (let ((elc (concat (file-name-sans-extension file) ".elc")))
            (when (file-exists-p elc) (ignore-errors (delete-file elc)))))
        (verify-cell--collect-checkdoc file))
    (error
     (verify-cell--push-error "elisp verify crashed: %s"
                              (error-message-string err)))))

;; ---------------------------------------------------------------------------
;; Entry point

(defun verify-cell--kind-for (file)
  (let ((ext (downcase (or (file-name-extension file) ""))))
    (cond
     ((string= ext "org") "org")
     ((member ext '("el" "elisp")) "elisp")
     (t "other"))))

(defun verify-cell-main ()
  "Read $VERIFY_FILE; dispatch by extension; print JSON to stdout."
  (let ((file (getenv "VERIFY_FILE"))
        (verify-cell--errors   nil)
        (verify-cell--warnings nil))
    (cond
     ((null file)
      (verify-cell--push-error "VERIFY_FILE env var not set"))
     ((not (file-readable-p file))
      (verify-cell--push-error "file not readable: %s" file))
     (t
      (let ((kind (verify-cell--kind-for file)))
        (cond
         ((string= kind "org")   (verify-cell-org file))
         ((string= kind "elisp") (verify-cell-elisp file))
         (t (verify-cell--push-warning
             "no verifier for extension: %s"
             (file-name-extension file)))))))
    (let* ((kind (if file (verify-cell--kind-for file) "unknown"))
           (payload `((file     . ,(or file ""))
                      (kind     . ,kind)
                      (errors   . ,(vconcat (nreverse verify-cell--errors)))
                      (warnings . ,(vconcat (nreverse verify-cell--warnings))))))
      (princ (json-encode payload))
      (princ "\n"))))

(provide 'verify_cell)
;;; verify_cell.el ends here
