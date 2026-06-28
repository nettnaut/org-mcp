;;; org-mcp-tests.el --- ert tests for org-mcp.el -*- lexical-binding: t; -*-

;; Run inside the live Emacs (org-roam already loaded), e.g.:
;;   emacsclient -e '(progn (load-file "~/code/org-mcp/org-mcp.el")
;;                          (load-file "~/code/org-mcp/tests/org-mcp-tests.el")
;;                          (ert-run-tests-batch "org-mcp"))'
;;
;; These focus on the confinement gate and the dispatch error contract — the
;; parts that must never regress for safety. Tool round-trips (create -> search
;; -> backlink) are exercised live against the real KB via tests/smoke.sh.

(require 'ert)
(require 'org-mcp)

(ert-deftest org-mcp-confine-accepts-inside-root ()
  (let ((f (expand-file-name "inbox.org" org-directory)))
    (should (org-mcp--confine f))))

(ert-deftest org-mcp-confine-rejects-traversal ()
  (should-error
   (org-mcp--confine (expand-file-name "../../etc/passwd" org-roam-directory))))

(ert-deftest org-mcp-confine-rejects-absolute-outside ()
  (should-error (org-mcp--confine "/etc/passwd")))

(ert-deftest org-mcp-confine-write-rejects-non-org ()
  (should-error
   (org-mcp--confine-write (expand-file-name "notes.txt" org-directory))))

(ert-deftest org-mcp-dispatch-unknown-tool-returns-error-json ()
  (let ((out (json-parse-string (org-mcp-dispatch "bogus" "{}")
                                :object-type 'alist)))
    (should (alist-get 'error out))))

(ert-deftest org-mcp-edit-node-body-requires-content ()
  ;; Content is validated before the id lookup, so this needs no real node.
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_edit_node_body" "{\"id\":\"x\"}")
              :object-type 'alist)))
    (should (alist-get 'error out))))

(ert-deftest org-mcp-edit-node-body-rejects-bad-operation ()
  ;; Operation is validated before the id lookup, so a dummy id is fine.
  (let ((out (json-parse-string
              (org-mcp-dispatch
               "org_edit_node_body"
               "{\"id\":\"x\",\"content\":\"hi\",\"operation\":\"frobnicate\"}")
              :object-type 'alist)))
    (should (string-match-p "operation" (alist-get 'error out)))))

(ert-deftest org-mcp-edit-node-body-rejects-whitespace-content ()
  ;; Whitespace-only content must not pass the guard and silently empty a body.
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_edit_node_body"
                                "{\"id\":\"x\",\"content\":\"   \\n\\n\"}")
              :object-type 'alist)))
    (should (string-match-p "content" (alist-get 'error out)))))

(ert-deftest org-mcp-edit-node-body-rejects-heading-content ()
  ;; Content that would parse as a heading must be refused, not silently inserted.
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_edit_node_body"
                                "{\"id\":\"x\",\"content\":\"note\\n* sneaky\"}")
              :object-type 'alist)))
    (should (string-match-p "heading" (alist-get 'error out)))))

(ert-deftest org-mcp-find-tasks-heading-locates-exact-title ()
  ;; Drives child-placement in org_capture_todo: a new TODO is filed under the
  ;; "Tasks" heading only when this finds it, with the right outline level.
  (with-temp-buffer
    (org-mode)
    (insert "* Tasks\n** TODO existing\n* Archive  :ARCHIVE:\n")
    (let ((hit (org-mcp--find-tasks-heading)))
      (should hit)
      (should (= 1 (nth 1 hit)))             ; level of "* Tasks"
      (should (= (point-min) (nth 0 hit))))))

(ert-deftest org-mcp-find-tasks-heading-nil-without-tasks ()
  ;; No "Tasks" heading -> capture falls back to appending at end of file.
  (with-temp-buffer
    (org-mode)
    (insert "* Project\n** TODO existing\n* Tasks for later\n")  ; substring, not exact
    (should-not (org-mcp--find-tasks-heading))))

(ert-deftest org-mcp-clean-tags-sanitizes-illegal-chars ()
  ;; Hyphens/spaces/dots in a tag silently break org tag parsing, so the
  ;; server must map them to the legal set before writing them to a file.
  (should (equal '("levive" "rag" "d_008" "D_008")
                 (org-mcp--clean-tags '("levive" "rag" "d-008" "D-008"))))
  (should (equal '("a_b" "c_d") (org-mcp--clean-tags '("a b" "c.d"))))
  ;; Empties (incl. an all-illegal tag collapsing to "") are dropped.
  (should (equal '("ok") (org-mcp--clean-tags '("ok" "")))))

(ert-deftest org-mcp-rename-heading-requires-title ()
  ;; Title is validated before the id lookup, so this needs no real node.
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_rename_heading" "{\"id\":\"x\",\"title\":\"   \"}")
              :object-type 'alist)))
    (should (string-match-p "title" (alist-get 'error out)))))

(ert-deftest org-mcp-rename-heading-rejects-multiline ()
  ;; A heading is one line; a newline in the title must be refused, not written.
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_rename_heading"
                                "{\"id\":\"x\",\"title\":\"two\\nlines\"}")
              :object-type 'alist)))
    (should (string-match-p "single line" (alist-get 'error out)))))

(ert-deftest org-mcp-dispatch-search-returns-json-array ()
  ;; Empty query matches everything; result must be valid JSON (a vector).
  (let ((out (json-parse-string
              (org-mcp-dispatch "org_search" "{\"query\":\"\",\"max_results\":1}")
              :array-type 'list)))
    (should (listp out))))

(provide 'org-mcp-tests)
;;; org-mcp-tests.el ends here
