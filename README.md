# org-mcp

[![CI](https://github.com/nettnaut/org-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/nettnaut/org-mcp/actions/workflows/ci.yml)

An MCP server that gives Claude search, context, note creation, linking, and
org-agenda access over an Emacs **org-mode + org-roam** knowledge base — by
talking to a **live Emacs** over `emacsclient`, so everything runs through your
real org config (custom agenda commands, TODO keywords, capture templates,
org-roam IDs/backlinks, autosync) rather than a reimplementation.

- **`org-mcp.py`** — a zero-dependency, hand-rolled JSON-RPC **stdio** server
  (same shape as the in-house `mu4e-mcp.py`). Pure MCP/JSON plumbing.
- **`org-mcp.el`** — the backend, loaded into your Emacs. All org logic lives
  here behind a single entry point, `org-mcp-dispatch`.

See `DESIGN.md` for the full design + decision log and `PLAN.md` for the build
sequence.

## Tools (11)

| Tool | What it does |
|------|--------------|
| `org_search` | org-roam search by **title/tags/aliases** |
| `org_search_content` | **full-text body** search (ripgrep), optional `tags` filter |
| `org_get_node` | full content + properties of a node (by id/title/alias) |
| `org_backlinks` | nodes linking **to** a node |
| `org_create_node` | create a new roam node (real capture, returns id + link) |
| `org_create_daily` | append a timestamped entry to a daily file |
| `org_insert_link` | insert an `[[id:]]` link inside an existing node |
| `org_agenda` | run a **custom agenda command** by key (`g`, `j`, `R`, …) |
| `org_capture_todo` | create a TODO (returns its id) |
| `org_update_todo` | change state / schedule / deadline / refile a TODO by id |
| `org_ensure_todo_ids` | backfill `:ID:`s onto TODO headings (default `org-agenda-files`) so the by-id tools work; `dry_run` to just count |

## Usage

New here? **[USAGE.md](USAGE.md)** is a plain-language guide to *asking* an MCP
client (e.g. Claude) to search, read, create, link, and schedule across your
notes and tasks — with example prompts. You never call the tools directly.

## How it works

```
Claude → tools/call (JSON) → org-mcp.py
       → emacsclient -e "(org-mcp-dispatch \"TOOL\" JSON-ARGS)"
       → Emacs: org-mcp-dispatch  (json-parse → confine gate → tool → json-encode)
       → org-mcp.py unwraps prin1 quoting, json.loads → MCP text content
```

The only thing Python ever puts into elisp is a **JSON string** escaped into an
elisp string literal — so multi-line note bodies, quotes, and commas never
touch elisp syntax. Content search runs ripgrep directly in Python (fast),
calling Emacs only to resolve tag→files and line→node-id.

## Safety: confinement gate

Every read/write is funnelled through `org-mcp--confine`, which canonicalises
the target with `file-truename` (resolving `..`, `~`, and symlinks) and requires
it to live inside `org-roam-directory` / `org-directory`. Path traversal,
absolute-path escapes, and symlink escapes are all rejected. Writes additionally
require an `.org` target. ripgrep is pinned to the roam root.

## Two gotchas (read before debugging)

1. **`prin1` quoting.** `emacsclient -e` prints the return value with `prin1`,
   so a returned string comes back wrapped in `"…"` with `"` and `\` escaped.
   `org-mcp.py:_unwrap_elisp_string` reverses this before `json.loads`. If output
   looks double-escaped, that's where to look.
2. **No interactive prompts.** Writes use immediate-finish capture and force
   non-interactive defaults, because an `emacsclient` call must never block on a
   minibuffer `y/n` prompt. Returning errors as `{"error": …}` keeps the channel
   clean.

## Install

**Requirements:** a running Emacs **server** (`M-x server-start`, or an Emacs
daemon), [`org-roam`](https://www.orgroam.com/), and
[`ripgrep`](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH`. The Python
side is stdlib-only — nothing to `pip install`.

1. **Clone** this repo, e.g. to `~/code/org-mcp`.
2. **Load the backend** into your Emacs so `org-mcp-dispatch` is defined. Add to
   your init (the `org-roam` `:config` block is a natural spot):
   ```elisp
   (load (expand-file-name "~/code/org-mcp/org-mcp.el") t)
   ```
   If you skip this, the Python server auto-loads it from its own directory on
   the first call.
3. **Register the MCP server** with your client, pointing at `org-mcp.py` with
   an absolute path. For the `claude` CLI:
   ```sh
   claude mcp add org-mcp --transport stdio -- python3 /path/to/org-mcp/org-mcp.py
   ```
   Or, inside Emacs (e.g. agent-shell's `agent-shell-mcp-servers`):
   ```elisp
   ((name . "org_mcp")
    (command . "python3")
    (args . ("/path/to/org-mcp/org-mcp.py")))
   ```

> First-run tip: if you have existing org tasks created before installing this,
> run the `org_ensure_todo_ids` tool once (try `dry_run` first) to backfill the
> `:ID:`s that `org_update_todo` / `org_get_node` rely on.

## Tests

```sh
python3 tests/test_boundary.py     # pure-Python: prin1 unwrap, escaping, Emacs-down
bash    tests/smoke.sh             # end-to-end against the live Emacs
emacsclient -e '(progn (load-file "~/code/org-mcp/org-mcp.el") \
  (load-file "~/code/org-mcp/tests/org-mcp-tests.el") \
  (ert-run-tests-batch "org-mcp"))'   # ert: confinement gate + dispatch contract
```

## License

[GPL-3.0-or-later](LICENSE) © 2026 Kjetil Rohde Jakobsen. Contributions welcome.
