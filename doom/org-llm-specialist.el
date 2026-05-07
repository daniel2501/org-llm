;;; org-llm-specialist.el --- Spawn org-llm specialists from Emacs -*- lexical-binding: t; -*-
;;
;; Wires `org-llm specialist run` into the Doom Emacs surface (org buffer + chat).
;; Pairs with org_llm/specialist.py + org_llm/cli_specialist.py — bypasses
;; opencode entirely; uses direct FOSS API + tool-use file edits.
;;
;; Per the rule "Agor crew uses no Claude" + the round-12 finding that
;; opencode silently drops completions for non-default FOSS models, this
;; module gives users a way to spawn specialists directly from Emacs without
;; the opencode runtime.
;;
;;; Usage:
;;
;; In any org buffer, position point inside a subtree describing the task:
;;
;;   * @atoz — wrap canonical-page mentions
;;     :PROPERTIES:
;;     :HANDLE:    @atoz
;;     :MODEL:     qwen/qwen3-coder-30b-a3b-instruct
;;     :WORKDIR:   /home/daniel/repos/org-llm
;;     :TARGETS:   docs/wiki/literate-tools.org
;;     :END:
;;
;;     Wrap exactly 5 prose mentions of canonical wiki pages with
;;     `[[id:UUID][label]]` cross-link wrappers...
;;
;; Then `M-x org-llm-specialist-spawn-at-point' runs it.
;;
;; The result is inserted as a sibling subtree below the task heading.
;;
;;; Code:

(require 'json)
(require 'org)
(require 'subr-x)

(defgroup org-llm-specialist nil
  "Spawn org-llm specialists from Emacs."
  :group 'org-llm
  :prefix "org-llm-specialist-")

(defcustom org-llm-specialist-cli "org-llm"
  "Path or name of the org-llm CLI binary."
  :type 'string
  :group 'org-llm-specialist)

(defcustom org-llm-specialist-default-model
  "qwen/qwen3-coder-30b-a3b-instruct"
  "Default OpenRouter model id for specialist spawns."
  :type 'string
  :group 'org-llm-specialist)

(defcustom org-llm-specialist-default-workdir
  "/home/daniel/repos/org-llm"
  "Default workdir for specialist spawns."
  :type 'string
  :group 'org-llm-specialist)

(defcustom org-llm-specialist-default-handle "@data"
  "Default Bridge Crew handle for specialist spawns when not specified."
  :type 'string
  :group 'org-llm-specialist)

(defcustom org-llm-specialist-personas
  '(("@atoz"     . "You are @atoz — Bridge Crew wiki concept-graph specialist.")
    ("@data"     . "You are @data — Bridge Crew code + scribe specialist.")
    ("@spock"    . "You are @spock — Bridge Crew logic + canonical-source reviewer.")
    ("@geordi"   . "You are @geordi — Bridge Crew analytics + charts specialist.")
    ("@boothby"  . "You are @boothby — Bridge Crew ops + hygiene specialist.")
    ("@riker"    . "You are @riker — Bridge Crew process + scheduling specialist."))
  "Persona text per Bridge Crew handle."
  :type '(alist :key-type string :value-type string)
  :group 'org-llm-specialist)


(defun org-llm-specialist--persona-for (handle)
  "Return the persona text for HANDLE, falling back to a generic one."
  (or (cdr (assoc handle org-llm-specialist-personas))
      (format "You are %s, an org-llm Bridge Crew specialist." handle)))

(defun org-llm-specialist--build-task-spec ()
  "Build a task-spec plist from the current org subtree's properties + body."
  (unless (derived-mode-p 'org-mode)
    (user-error "org-llm-specialist: must be in org-mode"))
  (save-excursion
    (org-back-to-heading t)
    (let* ((props      (org-entry-properties))
           (handle     (or (cdr (assoc "HANDLE" props))
                            org-llm-specialist-default-handle))
           (model      (or (cdr (assoc "MODEL" props))
                            org-llm-specialist-default-model))
           (workdir    (or (cdr (assoc "WORKDIR" props))
                            org-llm-specialist-default-workdir))
           (targets    (cdr (assoc "TARGETS" props)))
           (max-iter   (cdr (assoc "MAX_ITER" props)))
           (max-budget (cdr (assoc "MAX_BUDGET" props)))
           (heading    (substring-no-properties (org-get-heading t t t t)))
           (body-start (progn (org-end-of-meta-data t) (point)))
           (body-end   (progn (org-end-of-subtree t t) (point)))
           (instruction (string-trim
                          (buffer-substring-no-properties body-start body-end))))
      (list :handle handle
            :persona (org-llm-specialist--persona-for handle)
            :instruction (concat heading "\n\n" instruction)
            :workdir workdir
            :target_files (when targets
                             (split-string targets "[,\n[:space:]]+" t))
            :model model
            :max_iterations (when max-iter (string-to-number max-iter))
            :max_budget_usd (when max-budget (string-to-number max-budget))))))

(defun org-llm-specialist--task-spec-to-json (spec)
  "Serialize SPEC plist as JSON string."
  (let ((json-encoding-pretty-print t)
        (json-object-type 'alist)
        (alist (cl-loop for (k v) on spec by #'cddr
                         when v
                         collect (cons (substring (symbol-name k) 1) v))))
    (json-encode alist)))

(defun org-llm-specialist--render-result (json-result)
  "Insert JSON-RESULT as an org sibling subtree below current heading."
  (save-excursion
    (org-back-to-heading t)
    (let* ((res (let ((json-object-type 'alist)
                       (json-array-type 'list))
                   (json-read-from-string json-result)))
           (handle      (cdr (assoc 'handle res)))
           (success     (cdr (assoc 'success res)))
           (iters       (cdr (assoc 'iterations res)))
           (cost        (cdr (assoc 'cost_usd res)))
           (duration    (cdr (assoc 'duration_seconds res)))
           (text-output (cdr (assoc 'text_output res)))
           (edits       (cdr (assoc 'edits_applied res)))
           (err         (cdr (assoc 'error res))))
      (org-end-of-subtree t t)
      (unless (bolp) (insert "\n"))
      (insert (format "** Result — %s (%s)\n"
                       handle (if success "ok" "FAILED")))
      (insert ":PROPERTIES:\n")
      (insert (format ":COST_USD:  %s\n" cost))
      (insert (format ":DURATION:  %ss\n" duration))
      (insert (format ":ITERATIONS: %s\n" iters))
      (when err (insert (format ":ERROR:    %s\n" err)))
      (insert ":END:\n\n")
      (when (and edits (> (length edits) 0))
        (insert (format "*Edits applied (%d):*\n" (length edits)))
        (dolist (e edits)
          (let ((tool (cdr (assoc 'tool e)))
                 (result (cdr (assoc 'result e))))
            (insert (format "- =%s= → %s\n" tool result))))
        (insert "\n"))
      (when (and text-output (not (string-empty-p text-output)))
        (insert "*Final summary:*\n\n")
        (insert text-output)
        (insert "\n")))))

;;;###autoload
(defun org-llm-specialist-spawn-at-point ()
  "Spawn an org-llm specialist using the current org subtree as the task spec.

Reads the subtree's properties (HANDLE, MODEL, WORKDIR, TARGETS,
MAX_ITER, MAX_BUDGET) and body as the instruction. Shells out to
`org-llm specialist run --json' and inserts the result as a sibling
subtree."
  (interactive)
  (let* ((spec (org-llm-specialist--build-task-spec))
         (json (org-llm-specialist--task-spec-to-json spec))
         (spec-file (make-temp-file "org-llm-specialist-" nil ".json"))
         (output-buffer (get-buffer-create "*org-llm-specialist*")))
    (with-temp-file spec-file (insert json))
    (with-current-buffer output-buffer
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (format ";; spec at: %s\n" spec-file))
        (insert (format ";; running %s specialist run %s --json\n\n"
                         org-llm-specialist-cli spec-file))))
    (display-buffer output-buffer)
    (let* ((proc-name (format "org-llm-specialist-%s" (plist-get spec :handle)))
           (proc (start-process proc-name output-buffer
                                 org-llm-specialist-cli
                                 "specialist" "run" spec-file "--json")))
      (set-process-sentinel
        proc
        (lambda (process _event)
          (when (memq (process-status process) '(exit signal))
            (let* ((status (process-exit-status process))
                   (output (with-current-buffer (process-buffer process)
                              (buffer-substring-no-properties (point-min) (point-max))))
                   (json-start (string-match "^{" output))
                   (json-text  (when json-start (substring output json-start))))
              (message "org-llm-specialist: exit %s" status)
              (when json-text
                (with-current-buffer (process-buffer process)
                  (goto-char (point-max))
                  (insert "\n\n;; --- result inserted into source buffer ---\n")
                  (insert json-text))
                (let ((src-buffer (current-buffer)))
                  (with-current-buffer src-buffer
                    (org-llm-specialist--render-result json-text)))))))))
    (message "org-llm-specialist: spawned %s (spec %s)"
              (plist-get spec :handle) spec-file)))

;;;###autoload
(defun org-llm-specialist-spawn-from-region (start end &optional handle model)
  "Spawn a specialist with the region between START and END as the task.

When called interactively with prefix arg, prompts for HANDLE and MODEL."
  (interactive
    (if current-prefix-arg
        (list (region-beginning) (region-end)
              (completing-read "Handle: "
                                (mapcar #'car org-llm-specialist-personas)
                                nil nil org-llm-specialist-default-handle)
              (read-string "Model: " org-llm-specialist-default-model))
      (list (region-beginning) (region-end) nil nil)))
  (let* ((handle (or handle org-llm-specialist-default-handle))
         (model  (or model org-llm-specialist-default-model))
         (instruction (buffer-substring-no-properties start end))
         (spec (list :handle handle
                      :persona (org-llm-specialist--persona-for handle)
                      :instruction instruction
                      :workdir org-llm-specialist-default-workdir
                      :model model))
         (json (org-llm-specialist--task-spec-to-json spec))
         (spec-file (make-temp-file "org-llm-specialist-" nil ".json"))
         (output-buffer (get-buffer-create "*org-llm-specialist*")))
    (with-temp-file spec-file (insert json))
    (with-current-buffer output-buffer
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (format ";; running %s specialist run %s\n"
                         org-llm-specialist-cli spec-file))))
    (display-buffer output-buffer)
    (start-process "org-llm-specialist" output-buffer
                    org-llm-specialist-cli
                    "specialist" "run" spec-file)
    (message "spawned %s; output in *org-llm-specialist*" handle)))


(provide 'org-llm-specialist)
;;; org-llm-specialist.el ends here
