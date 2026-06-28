;;; org-mcp.el --- Org/org-roam tool backend for the org-mcp MCP server -*- lexical-binding: t; -*-

;; Copyright (C) 2026 Kjetil Rohde Jakobsen
;;
;; SPDX-License-Identifier: GPL-3.0-or-later
;;
;; This program is free software: you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or (at
;; your option) any later version.  It is distributed WITHOUT ANY
;; WARRANTY; see the GNU General Public License (LICENSE in this repo, or
;; <https://www.gnu.org/licenses/>) for details.

;; This library is loaded into the user's running Emacs.  The companion
;; Python stdio server (org-mcp.py) calls a single entry point,
;; `org-mcp-dispatch', over `emacsclient -e', passing the tool name and a
;; JSON-encoded argument object.  All org/org-roam logic lives here; the
;; Python side is pure MCP/JSON plumbing.  See DESIGN.md.
;;
;; Boundary contract:
;;   in : (org-mcp-dispatch "TOOL" "JSON-ARGS-STRING")
;;   out: a JSON string (the only thing emacsclient prin1-prints back).
;; Every read/write is funnelled through `org-mcp--confine' so the server
;; can only ever touch files inside org-roam-directory / org-directory.

;;; Code:

(require 'json)
(require 'org)
(require 'org-roam)
(require 'org-roam-dailies nil t)
(require 'seq)
(require 'subr-x)

(defconst org-mcp--max-content 12000
  "Hard cap on returned note/body text, to bound Claude's context.")

;;;; ---------------------------------------------------------------------
;;;; Confinement gate
;;;; ---------------------------------------------------------------------

(defun org-mcp--roots ()
  "Canonical directory roots the server is allowed to touch."
  (delete-dups
   (mapcar (lambda (d) (file-name-as-directory (file-truename d)))
           (list org-roam-directory org-directory))))

(defun org-mcp--confine (path)
  "Return the canonical PATH if it lives inside an allowed root, else error.
Resolves `..', `~' and symlinks first, so neither path traversal nor a
symlink pointing outside the roots can escape."
  (unless (and path (stringp path) (> (length path) 0))
    (error "path required"))
  (let* ((true (file-truename path))
         (roots (org-mcp--roots)))
    (unless (seq-some (lambda (root) (string-prefix-p root true)) roots)
      (error "path outside org root: %s" path))
    true))

(defun org-mcp--confine-write (path)
  "Like `org-mcp--confine' but additionally require an .org file target."
  (let ((true (org-mcp--confine path)))
    (unless (string-suffix-p ".org" true)
      (error "refusing to write non-org file: %s" path))
    true))

;;;; ---------------------------------------------------------------------
;;;; Small helpers
;;;; ---------------------------------------------------------------------

(defun org-mcp--arg (args key &optional default)
  "Fetch string KEY from hash-table ARGS, or DEFAULT."
  (let ((v (gethash key args)))
    (if (or (null v) (eq v :null)) default v)))

(defun org-mcp--str-list (v)
  "Coerce V (a list, vector, or nil) into a list of strings."
  (cond ((null v) nil)
        ((vectorp v) (append v nil))
        ((listp v) v)
        (t (list v))))

(defun org-mcp--clean-tags (v)
  "Coerce V to a list of *legal* org tags.
Org only recognises [[:alnum:]_@#%] in tags, so any other character (a
hyphen, space, dot, …) silently turns the whole `:a:b:' run into plain
heading text that no agenda tag search can match.  Each tag is mapped to
that legal set (illegal chars -> `_'); empties are dropped.  Apply this
to every caller-supplied tag list *before* writing it to a file."
  (delq nil
        (mapcar (lambda (tag)
                  (let ((clean (replace-regexp-in-string
                                "[^[:alnum:]_@#%]" "_" (format "%s" tag))))
                    (and (> (length clean) 0) clean)))
                (org-mcp--str-list v))))

(defun org-mcp--node-link (node)
  (format "[[id:%s][%s]]" (org-roam-node-id node) (org-roam-node-title node)))

(defun org-mcp--node->alist (n)
  "Slim summary alist for org-roam node N."
  (list (cons "id" (org-roam-node-id n))
        (cons "title" (org-roam-node-title n))
        (cons "tags" (vconcat (org-roam-node-tags n)))
        (cons "file" (org-roam-node-file n))
        (cons "link" (org-mcp--node-link n))))

(defun org-mcp--file-id (file)
  "Top-level :ID: of FILE, or nil."
  (with-temp-buffer
    (insert-file-contents file)
    (delay-mode-hooks (org-mode))
    (goto-char (point-min))
    (org-entry-get (point) "ID")))

;;;; ---------------------------------------------------------------------
;;;; Read tools
;;;; ---------------------------------------------------------------------

(defun org-mcp--search (query max)
  "Metadata search over org-roam nodes (title/tags/aliases)."
  (let* ((needle (downcase (or query "")))
         (hits '()))
    (dolist (n (org-roam-node-list))
      (let ((hay (downcase
                  (string-join
                   (append (list (or (org-roam-node-title n) ""))
                           (org-mcp--str-list (org-roam-node-tags n))
                           (org-mcp--str-list (org-roam-node-aliases n)))
                   " "))))
        (when (string-search needle hay)
          (push n hits))))
    (setq hits (nreverse hits))
    (when (and max (> (length hits) max))
      (setq hits (seq-take hits max)))
    (vconcat (mapcar #'org-mcp--node->alist hits))))

(defun org-mcp--node-content (file point)
  "Text of the node at POINT in FILE (subtree, or whole file for file nodes)."
  (with-temp-buffer
    (insert-file-contents file)
    (delay-mode-hooks (org-mode))
    (goto-char (min point (point-max)))
    (let ((text (if (org-before-first-heading-p)
                    (buffer-string)
                  (save-restriction
                    (org-narrow-to-subtree)
                    (buffer-string)))))
      (if (> (length text) org-mcp--max-content)
          (substring text 0 org-mcp--max-content)
        text))))

(defun org-mcp--get-node (key)
  "Full content + metadata for a node looked up by id, title or alias."
  (let ((n (or (ignore-errors (org-roam-node-from-id key))
               (ignore-errors (org-roam-node-from-title-or-alias key)))))
    (unless n (error "node not found: %s" key))
    (let ((file (org-mcp--confine (org-roam-node-file n))))
      (list (cons "id" (org-roam-node-id n))
            (cons "title" (org-roam-node-title n))
            (cons "tags" (vconcat (org-roam-node-tags n)))
            (cons "file" file)
            (cons "olp" (vconcat (org-mcp--str-list (org-roam-node-olp n))))
            (cons "link" (org-mcp--node-link n))
            (cons "content" (org-mcp--node-content file (org-roam-node-point n)))))))

(defun org-mcp--backlinks (id)
  "Nodes that link TO the node with ID."
  (let ((n (ignore-errors (org-roam-node-from-id id))))
    (unless n (error "node not found: %s" id))
    (vconcat
     (mapcar (lambda (bl)
               (org-mcp--node->alist (org-roam-backlink-source-node bl)))
             (org-roam-backlinks-get n)))))

(defun org-mcp--tag-files (tags)
  "Files of nodes carrying any of TAGS (for ripgrep restriction)."
  (let ((want (mapcar #'downcase (org-mcp--str-list tags)))
        (files '()))
    (when want
      (dolist (n (org-roam-node-list))
        (let ((have (mapcar #'downcase (org-mcp--str-list (org-roam-node-tags n)))))
          (when (seq-intersection want have)
            (push (org-roam-node-file n) files)))))
    (vconcat (delete-dups files))))

(defun org-mcp--node-at (file line)
  "Resolve FILE:LINE to its enclosing node id + heading (for content hits)."
  (setq file (org-mcp--confine file))
  (with-temp-buffer
    (insert-file-contents file)
    (delay-mode-hooks (org-mode))
    (goto-char (point-min))
    (forward-line (1- (max 1 (or line 1))))
    (let ((id (org-entry-get (point) "ID" t))
          (heading (save-excursion
                     (ignore-errors
                       (org-back-to-heading t)
                       (org-get-heading t t t t)))))
      (list (cons "id" (or id ""))
            (cons "heading" (or heading ""))
            (cons "link" (if id (format "[[id:%s]]" id) ""))))))

;;;; ---------------------------------------------------------------------
;;;; Write tools  (non-interactive; see DESIGN.md D11)
;;;; ---------------------------------------------------------------------

(defun org-mcp--create-node (title content tags)
  "Create a new org-roam node via an immediate-finish capture.
A pre-generated ID is written into the head so the new id is returned
deterministically."
  (unless (and title (stringp title) (> (length title) 0))
    (error "title required"))
  (let* ((id (org-id-new))
         (tags (org-mcp--clean-tags tags))
         (node (org-roam-node-create :id id :title title))
         (filetags (if tags
                       (format "#+filetags: :%s:\n" (string-join tags ":"))
                     ""))
         (head (concat ":PROPERTIES:\n:ID:       " id "\n:END:\n"
                       "#+title: ${title}\n" filetags))
         (org-roam-capture-templates
          `(("d" "mcp" plain ,(concat "\n" (or content ""))
             :target (file+head "%<%Y%m%d%H%M%S>-${slug}.org" ,head)
             :immediate-finish t :unnarrowed t))))
    (org-roam-capture- :node node :keys "d")
    (org-roam-db-sync)
    (let ((n (ignore-errors (org-roam-node-from-id id))))
      (list (cons "id" id)
            (cons "title" title)
            (cons "file" (and n (org-roam-node-file n)))
            (cons "link" (format "[[id:%s][%s]]" id title))))))

(defun org-mcp--create-daily (content date)
  "Append a timestamped CONTENT entry to the daily file for DATE (or today)."
  (require 'org-roam-dailies)
  (let* ((time (if (or (null date) (string= date "today"))
                   (current-time)
                 (org-read-date nil t date)))
         (day (format-time-string "%Y-%m-%d" time))
         (dir (expand-file-name org-roam-dailies-directory org-roam-directory))
         (file (org-mcp--confine-write (expand-file-name (concat day ".org") dir)))
         (fresh (not (file-exists-p file))))
    (make-directory dir t)
    (with-current-buffer (find-file-noselect file)
      (when fresh
        (goto-char (point-min))
        (insert ":PROPERTIES:\n:ID:       " (org-id-new) "\n:END:\n"
                "#+title: " day "\n#+filetags: :daily:\n\n"))
      (goto-char (point-max))
      (unless (bolp) (insert "\n"))
      (insert (format "* %s %s\n" (format-time-string "%H:%M") (or content "")))
      (save-buffer))
    (org-roam-db-sync)
    (list (cons "file" file)
          (cons "date" day)
          (cons "created" fresh)
          (cons "id" (or (org-mcp--file-id file) "")))))

(defun org-mcp--insert-link (source-id target-id description)
  "Insert an [[id:]] link to TARGET-ID inside the node SOURCE-ID."
  (let ((src (ignore-errors (org-roam-node-from-id source-id)))
        (tgt (ignore-errors (org-roam-node-from-id target-id))))
    (unless src (error "source node not found: %s" source-id))
    (unless tgt (error "target node not found: %s" target-id))
    (let ((file (org-mcp--confine-write (org-roam-node-file src)))
          (point (org-roam-node-point src))
          (desc (or description (org-roam-node-title tgt))))
      (with-current-buffer (find-file-noselect file)
        (goto-char (min point (point-max)))
        (if (org-before-first-heading-p)
            (goto-char (point-max))
          (org-end-of-subtree t t))
        (unless (bolp) (insert "\n"))
        (insert (format "[[id:%s][%s]]\n" target-id desc))
        (save-buffer))
      (org-roam-db-sync)
      (list (cons "source" source-id)
            (cons "target" target-id)
            (cons "inserted" (format "[[id:%s][%s]]" target-id desc))))))

;;;; ---------------------------------------------------------------------
;;;; Agenda / TODO tools
;;;; ---------------------------------------------------------------------

(defun org-mcp--agenda-keys ()
  (mapcar #'car org-agenda-custom-commands))

(defun org-mcp--agenda (key)
  "Run custom agenda command KEY; return structured items + rendered text."
  (unless (assoc key org-agenda-custom-commands)
    (error "no such agenda key: %s (available: %s)"
           key (string-join (org-mcp--agenda-keys) ", ")))
  (save-window-excursion
    (let ((org-agenda-sticky nil)
          (org-agenda-window-setup 'current-window)
          (items '()))
      (org-agenda nil key)
      (with-current-buffer org-agenda-buffer-name
        (goto-char (point-min))
        (while (not (eobp))
          (let ((m (or (get-text-property (point) 'org-marker)
                       (get-text-property (point) 'org-hd-marker))))
            (when m
              (push (list (cons "text" (string-trim
                                        (buffer-substring-no-properties
                                         (line-beginning-position)
                                         (line-end-position))))
                          (cons "todo" (or (get-text-property (point) 'todo-state) ""))
                          (cons "id" (or (org-with-point-at m (org-entry-get (point) "ID")) ""))
                          (cons "file" (or (org-with-point-at m
                                             (buffer-file-name (marker-buffer m))) "")))
                    items)))
          (forward-line 1))
        (let ((text (buffer-substring-no-properties (point-min) (point-max))))
          (list (cons "key" key)
                (cons "items" (vconcat (nreverse items)))
                (cons "text" text)))))))

(defun org-mcp--target-file (target)
  "Resolve a friendly TARGET name (or filename) to a confined org file."
  (let* ((fname (pcase (or target "inbox")
                  ("inbox" "inbox.org")
                  ("agenda" "agenda.org")
                  ("notes" "notes.org")
                  ("someday" "someday.org")
                  (other (if (string-suffix-p ".org" other) other
                           (concat other ".org"))))))
    (org-mcp--confine-write (expand-file-name fname org-directory))))

(defun org-mcp--find-tasks-heading ()
  "Return (POS LEVEL) of the first \"Tasks\" heading in the current buffer.
POS is the start of the heading line and LEVEL its outline depth (number
of leading stars).  Matches a heading whose title is exactly \"Tasks\"
(trailing tags allowed).  Returns nil when no such heading exists."
  (save-excursion
    (goto-char (point-min))
    (when (re-search-forward
           "^\\(\\*+\\)[ \t]+Tasks[ \t]*\\(?::[[:alnum:]_@#%:]+:\\)?[ \t]*$"
           nil t)
      (list (match-beginning 0) (length (match-string 1))))))

(defun org-mcp--capture-todo (text target tags body)
  "Create a new TODO in TARGET (default inbox.org).
TEXT is a short, succinct action used verbatim as the heading — keep it to
one line.  Optional BODY holds any longer details/context and is placed in
the entry body (after the property drawer), so headings stay readable in
agenda and project views.  In a file that has a \"Tasks\" heading the TODO
is inserted as its last child; otherwise it is appended at end of file."
  (unless (and text (> (length text) 0)) (error "text required"))
  (let ((file (org-mcp--target-file target))
        (tags (org-mcp--clean-tags tags))
        (id nil))
    (with-current-buffer (find-file-noselect file)
      (let* ((tasks (org-mcp--find-tasks-heading))
             (level (if tasks (1+ (nth 1 tasks)) 1)))
        (if tasks
            ;; Land at the end of the Tasks subtree so the new entry becomes
            ;; its last child rather than a sibling further down the file.
            (progn (goto-char (nth 0 tasks))
                   (org-end-of-subtree t t))
          (goto-char (point-max)))
        (unless (bolp) (insert "\n"))
        (insert (format "%s TODO %s%s\n"
                        (make-string level ?*) text
                        (if tags (format "  :%s:" (string-join tags ":")) "")))
        (forward-line -1)
        (setq id (org-id-get-create))   ; stable handle for org_update_todo
        (when (and body (> (length (string-trim body)) 0))
          (org-end-of-meta-data t)      ; skip planning + property drawer
          (insert (string-trim-right body) "\n"))
        (save-buffer)))
    (org-roam-db-sync)
    (list (cons "file" file) (cons "id" id)
          (cons "text" text) (cons "state" "TODO"))))

(defun org-mcp--update-todo (id state schedule deadline refile)
  "Modify the TODO entry identified by ID."
  (let ((m (org-id-find id t)))
    (unless m (error "id not found: %s" id))
    (org-with-point-at m
      (org-mcp--confine-write (buffer-file-name))
      (when state (org-todo state))
      (when schedule (org-schedule nil schedule))
      (when deadline (org-deadline nil deadline))
      (when refile
        (org-refile nil nil (list nil (org-mcp--target-file refile) nil nil)))
      (save-buffer))
    (org-roam-db-sync)
    (list (cons "id" id)
          (cons "state" state)
          (cons "scheduled" (or schedule :false))
          (cons "deadline" (or deadline :false))
          (cons "updated" t))))

(defun org-mcp--rename-heading (id title)
  "Set the headline TEXT of the entry identified by ID to TITLE.
Only the title is replaced; the outline level, TODO keyword, priority,
tags, planning lines, property drawer and body are all preserved.  TITLE
must be a single, non-empty line — use this to shorten a bloated heading
after moving its detail into the body with `org-mcp--edit-node-body'."
  (unless (and title (> (length (string-trim title)) 0))
    (error "title required"))
  (when (string-match-p "[\n\r]" title)
    (error "title must be a single line"))
  (let ((title (string-trim title))
        (m (org-id-find id t)))
    (unless m (error "id not found: %s" id))
    (org-with-point-at m
      (org-mcp--confine-write (buffer-file-name))
      (org-back-to-heading t)
      (org-edit-headline title)
      (save-buffer))
    (org-roam-db-sync)
    (list (cons "id" id) (cons "title" title) (cons "renamed" t))))

(defun org-mcp--edit-node-body (id content operation)
  "Append CONTENT to, or replace, the body of the heading identified by ID.
OPERATION is \"append\" (default) — add CONTENT after any existing body,
before child headings — or \"replace\", which overwrites the body while
keeping the heading, planning lines and property drawer intact.  CONTENT may
span multiple lines, but a line that would parse as a heading (starts with
`*') is refused — this edits body text, not outline structure.  The heading
and its meta-data (planning, property/logbook drawers, clocks) are never
touched; for state/schedule/deadline use `org-mcp--update-todo'."
  (unless (and content (not (string-empty-p (string-trim content))))
    (error "content required"))
  (when (string-match-p "^\\*+ " content)
    (error "content would create a heading (line starts with `*'); \
this tool edits body text only"))
  (let ((op (or operation "append")))
    (unless (member op '("append" "replace"))
      (error "operation must be \"append\" or \"replace\": %s" op))
    (let ((m (org-id-find id t)))
      (unless m (error "id not found: %s" id))
      (org-with-point-at m
        (org-mcp--confine-write (buffer-file-name))
        (org-back-to-heading t)
        ;; End of this entry's own content = the next heading (a child or the
        ;; following sibling), computed from the heading so we never absorb it.
        (let ((end (save-excursion (outline-next-heading) (point))))
          ;; FULL=t so we skip planning, the property drawer, AND logbook/clock
          ;; lines — otherwise a `replace' would delete clock history / state log.
          (org-end-of-meta-data t)
          (let* ((start (min (point) end))
                 (old (string-trim (buffer-substring-no-properties start end)))
                 (add (string-trim content))
                 (body (if (and (equal op "append") (> (length old) 0))
                           (concat old "\n\n" add)
                         add)))
            (delete-region start end)
            (goto-char start)
            ;; Blank line after the meta-data, the body, then a blank line
            ;; before whatever follows (the next heading or end of file).
            (insert "\n" body "\n\n")))
        (save-buffer))
      (org-roam-db-sync)
      (list (cons "id" id)
            (cons "operation" op)
            (cons "updated" t)))))

(defun org-mcp--ensure-todo-ids (files dry-run)
  "Ensure every TODO-state heading in FILES has an :ID:.

FILES defaults to `org-agenda-files' (the natural set of files holding
real tasks).  A heading counts if it has any todo keyword (NEXT, TODO,
WAITING, DONE, …); headings without a keyword are left alone.  Files
outside the confined roots, or that do not exist, are skipped and
reported.  When DRY-RUN is non-nil, nothing is written — only counted.

Org tasks created before this server has run lack the per-heading :ID:
that `org_update_todo' (and `org_get_node') locate by, so a content-search
on such a heading resolves to the *file* node instead.  Running this once
backfills them; `org_capture_todo' adds an :ID' to every new task."
  (let ((targets (or (org-mcp--str-list files) (org-agenda-files)))
        (todos 0) (missing 0) (added 0) (per-file '()) (skipped '()))
    (dolist (f targets)
      (let ((true (ignore-errors (org-mcp--confine-write f))))
        (if (or (null true) (not (file-exists-p true)))
            (push f skipped)
          (let ((n 0))
            (with-current-buffer (find-file-noselect true)
              (org-with-wide-buffer
               (goto-char (point-min))
               (org-map-entries
                (lambda ()
                  (when (org-get-todo-state)
                    (setq todos (1+ todos))
                    (unless (org-entry-get (point) "ID")
                      (setq missing (1+ missing) n (1+ n))
                      (unless dry-run
                        (org-id-get-create)
                        (setq added (1+ added))))))
                t 'file))
              (when (and (> n 0) (not dry-run)) (save-buffer)))
            (when (> n 0)
              (push (list (cons "file" true) (cons "missing" n)) per-file))))))
    (unless dry-run
      (org-id-update-id-locations)
      (when (fboundp 'org-roam-db-sync) (org-roam-db-sync)))
    (list (cons "dry_run" (if dry-run t :false))
          (cons "todos" todos)
          (cons "missing" missing)
          (cons "added" added)
          (cons "files" (vconcat (nreverse per-file)))
          (cons "skipped" (vconcat (nreverse skipped))))))

;;;; ---------------------------------------------------------------------
;;;; Dispatch
;;;; ---------------------------------------------------------------------

(defun org-mcp-roots ()
  "Public: return the confined roots as JSON (used by the Python side)."
  (json-encode (list (cons "roam" (file-truename org-roam-directory))
                     (cons "org" (file-truename org-directory))
                     (cons "dailies" (expand-file-name
                                      (or (bound-and-true-p org-roam-dailies-directory) "")
                                      org-roam-directory)))))

(defun org-mcp-dispatch (tool args-json)
  "Run TOOL with ARGS-JSON (a JSON object string); return a JSON string.
Single entry point for the Python server.  Every error is caught and
returned as {\"error\": ...} so emacsclient never blocks or leaks a trace."
  (condition-case err
      (let* ((args (if (and args-json (> (length args-json) 0))
                       (json-parse-string args-json :object-type 'hash-table
                                          :array-type 'list
                                          :null-object :null :false-object :false)
                     (make-hash-table :test 'equal)))
             (a (lambda (k &optional d) (org-mcp--arg args k d)))
             (result
              (pcase tool
                ("org_search"
                 (org-mcp--search (funcall a "query")
                                  (funcall a "max_results" 20)))
                ("org_get_node"
                 (org-mcp--get-node (funcall a "id_or_title")))
                ("org_backlinks"
                 (org-mcp--backlinks (funcall a "id")))
                ("tag_files"
                 (org-mcp--tag-files (funcall a "tags")))
                ("node_at"
                 (org-mcp--node-at (funcall a "file") (funcall a "line")))
                ("org_create_node"
                 (org-mcp--create-node (funcall a "title")
                                       (funcall a "content")
                                       (funcall a "tags")))
                ("org_create_daily"
                 (org-mcp--create-daily (funcall a "content")
                                        (funcall a "date" "today")))
                ("org_insert_link"
                 (org-mcp--insert-link (funcall a "source_id")
                                       (funcall a "target_id")
                                       (funcall a "description")))
                ("org_agenda"
                 (org-mcp--agenda (funcall a "key" "g")))
                ("org_capture_todo"
                 (org-mcp--capture-todo (funcall a "text")
                                        (funcall a "target" "inbox")
                                        (funcall a "tags")
                                        (funcall a "body")))
                ("org_update_todo"
                 (org-mcp--update-todo (funcall a "id")
                                       (funcall a "state")
                                       (funcall a "schedule")
                                       (funcall a "deadline")
                                       (funcall a "refile")))
                ("org_rename_heading"
                 (org-mcp--rename-heading (funcall a "id")
                                          (funcall a "title")))
                ("org_edit_node_body"
                 (org-mcp--edit-node-body (funcall a "id")
                                          (funcall a "content")
                                          (funcall a "operation" "append")))
                ("org_ensure_todo_ids"
                 (org-mcp--ensure-todo-ids (funcall a "files")
                                           (eq (funcall a "dry_run") t)))
                (_ (error "unknown tool: %s" tool)))))
        (json-encode result))
    (error
     (json-encode (list (cons "error" (error-message-string err)))))))

(provide 'org-mcp)
;;; org-mcp.el ends here
