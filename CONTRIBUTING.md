# Contributing to org-mcp

Thanks for your interest! org-mcp is a small, dependency-light bridge between an
MCP client (e.g. Claude) and a **live Emacs** running org-mode + org-roam.
Contributions — bug reports, new tools, docs, tests — are welcome.

## Architecture in one minute

```
client → tools/call (JSON) → org-mcp.py
       → emacsclient -e "(org-mcp-dispatch \"TOOL\" JSON-ARGS)"
       → org-mcp.el: org-mcp-dispatch (json-parse → confine → tool → json-encode)
       → org-mcp.py unwraps prin1 quoting, json.loads → MCP text content
```

- **`org-mcp.py`** — zero-dependency, hand-rolled JSON-RPC **stdio** server.
  Pure MCP/JSON plumbing; it declares the tools and forwards calls.
- **`org-mcp.el`** — the backend loaded into your Emacs. **All** org/org-roam
  logic lives here, behind the single entry point `org-mcp-dispatch`.

See `DESIGN.md` for the full design + decision log.

## Adding or changing a tool

A tool touches **both** files. To add one:

1. **`org-mcp.el`** — write `org-mcp--your-tool` returning an alist/vector
   (whatever `json-encode` should serialise), then add a `pcase` branch for it
   in `org-mcp-dispatch`.
2. **`org-mcp.py`** — add an entry to the `TOOLS` list with `name`,
   `description`, and an `inputSchema`. Most tools dispatch 1:1 to elisp; only
   add to `_LOCAL` / `call_tool` if Python does the work itself (as
   `org_search_content` does with ripgrep).
3. Update the tool table and count in `README.md`.

### Non-negotiables

- **Confinement.** Every file a tool reads or writes **must** pass through
  `org-mcp--confine` (reads) or `org-mcp--confine-write` (writes, `.org` only).
  This is the security boundary that keeps the server inside
  `org-roam-directory` / `org-directory`. Don't bypass it.
- **No interactive prompts.** An `emacsclient` call must never block on a
  minibuffer `y/n`. Use immediate-finish captures and non-interactive defaults;
  return errors as data — `org-mcp-dispatch` already wraps everything as
  `{"error": …}`.
- **The JSON boundary.** The only thing Python ever interpolates into elisp is a
  JSON string (escaped by `_elisp_str`). Keep it that way — never build elisp
  from raw user text.

## Running the tests

```sh
python3 tests/test_boundary.py     # pure-Python: prin1 unwrap, escaping, Emacs-down
bash    tests/smoke.sh             # end-to-end against a live Emacs
emacsclient -e '(progn (load-file "~/code/org-mcp/org-mcp.el") \
  (load-file "~/code/org-mcp/tests/org-mcp-tests.el") \
  (ert-run-tests-batch "org-mcp"))'   # ert: confinement gate + dispatch contract
```

Please add or update tests for behaviour you change — at minimum cover the
confinement gate for any new write path.

## Style

- **Elisp:** prefix internal functions `org-mcp--`; keep public surface to
  `org-mcp-dispatch` / `org-mcp-roots`. `lexical-binding: t`. Match the existing
  docstring style.
- **Python:** standard library only — please don't add runtime dependencies.
- Keep commits focused; conventional-commit-style subjects (`feat:`, `fix:`,
  `docs:`) are appreciated but not required.

## Submitting

1. Fork, branch, commit.
2. Run the tests above.
3. Open a PR describing the change and how you verified it.

## License

By contributing, you agree your contributions are licensed under the project's
**GPL-3.0-or-later** license (see `LICENSE`).
