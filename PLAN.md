# Plan — org-mcp implementation

Build bottom-up: elisp dispatch + confinement gate first (testable in isolation
via `emacsclient`), then the mu4e-style Python stdio server, then registration
and end-to-end smoke test. The `prin1` round-trip and the confinement gate are
locked earliest — everything depends on them. Design is fixed in `DESIGN.md`;
this plan sequences the build only.

## Scope

- **In:** `org-mcp.el` (dispatch + 10 tools + confine gate), `org-mcp.py`
  (stdio server), registration in `Emacs.org` + `~/.claude.json`, ert/python/
  smoke tests, README.
- **Out:** Forking either repo; caching layer; networked/remote transport; any
  tool beyond the locked 10; changes to existing GTD config behaviour.

## Action Items

- [ ] 1. Scaffold `~/code/org-mcp`: README stub, empty `org-mcp.el` /
      `org-mcp.py`, `tests/` (git already init'd).
- [ ] 2. Write `org-mcp.el` core: `org-mcp-dispatch` (`json-parse-string` in,
      `json-encode` out), `condition-case` wrapper, and `org-mcp--confine` gate
      reading `file-truename` roots from `org-roam-directory`/`org-directory`.
- [ ] 3. Implement the 4 read tools in elisp (`org_search`, `org_get_node`,
      `org_backlinks`; tag→files resolver for content search); verify each via
      `emacsclient -e "(org-mcp-dispatch ...)"`.
- [ ] 4. Implement the 3 write tools (`org_create_node`, `org_create_daily`,
      `org_insert_link`) using immediate-finish capture + non-interactive
      guards; verify node created, ID/backlink correct, autosync sees it.
- [ ] 5. Implement the 3 agenda/TODO tools (`org_agenda` by key,
      `org_capture_todo`, `org_update_todo`); verify against custom keys
      (`g`, `j`, `R`) returning structured items.
- [ ] 6. Write `org-mcp.py`: copy `mu4e-mcp.py` JSON-RPC loop; add emacsclient
      helper with prin1-unwrap + 10s timeout + Emacs-down clean error; add
      ripgrep-backed `org_search_content` (root pinned, `..` rejection);
      declare all 10 tools in `TOOLS`.
- [ ] 7. Add a load line for `org-mcp.el` and an `org_mcp` entry in
      `agent-shell-mcp-servers` in `Emacs.org`; re-tangle; add the same server
      to `~/.claude.json` `mcpServers`.
- [ ] 8. Write tests: ert (create→search→backlink, confine rejects
      `../escape`), python (prin1 unwrap on multi-line body, Emacs-down error,
      path pre-validation), `tests/smoke.sh` (pipe a real `tools/call`).
- [ ] 9. Validate end-to-end: run `smoke.sh`, then exercise from a live claude
      session (search → get_node → create_node → agenda) and confirm a
      confinement-escape attempt is rejected.
- [ ] 10. Write README (tool list, prin1 + confinement gotchas, registration
      steps); commit.

## Validation

Step 8 (ert + python unit + smoke) and Step 9 (manual end-to-end from a live
Claude session, including a deliberate `../` escape that must be rejected)
together cover correctness, bridge framing, and the security gate.

## Open Questions

- None blocking. Assumed default: `org_create_node` uses an immediate-finish
  capture template defined inside `org-mcp.el` (not the interactive dailies
  templates) so writes never block on the minibuffer.
