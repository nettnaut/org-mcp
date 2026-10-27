# org-mcp

[![CI](https://github.com/nettnaut/org-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/nettnaut/org-mcp/actions/workflows/ci.yml)

An MCP server that gives Claude search, context, note creation, linking, and
org-agenda access over an Emacs **org-mode + org-roam** knowledge base — by
talking to a **live Emacs** over `emacsclient`, so everything runs through your
real org config (custom agenda commands, TODO keywords, capture templates,
org-roam IDs/backlinks, autosync) rather than a reimplementation.

- **`org-mcp.py`** — a zero-dependency, hand-rolled JSON-RPC server over
  **stdio** (default) or **Streamable HTTP** (`--http`). Pure MCP/JSON plumbing.
- **`org-mcp.el`** — the backend, loaded into your Emacs. All org logic lives
  here behind a single entry point, `org-mcp-dispatch`.

See `DESIGN.md` for the full design + decision log and `PLAN.md` for the build
sequence.

## Tools (13)

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
| `org_rename_heading` | rename a heading by id, keeping keyword, tags, planning and body |
| `org_edit_node_body` | append to (or replace) the **body text** of a heading by id; for notes/context (heading + meta-data untouched) |
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

## Remote access (HTTP mode)

To reach your notes from a client that isn't on this machine — e.g. a claude.ai
custom connector — run the server in HTTP mode behind an authenticating proxy:

```sh
ORG_MCP_PROFILE=remote python3 org-mcp.py --http 127.0.0.1:8765
# MCP endpoint: http://127.0.0.1:8765/mcp
```

- **Transport:** stateless MCP *Streamable HTTP* — `POST /mcp` with a JSON-RPC
  message (or batch), JSON response; notifications get `202`. No server-initiated
  stream, so `GET` is `405`. Requests are handled one at a time.
- **`ORG_MCP_PROFILE=remote`** limits the tools to `org_search`,
  `org_search_content`, `org_get_node`, `org_agenda`, `org_backlinks` and
  `org_capture_todo` — and capture always goes to the inbox (`target` is removed
  from its schema; any other target is refused). Other tools are hidden from
  `tools/list` *and* refused on `tools/call` (JSON-RPC `-32602`) before Emacs
  is touched. Default profile (`full`) is unchanged.
- **Bind to loopback** and put real authentication (e.g. an OAuth proxy) in
  front. The server warns on a non-loopback bind.
- **`ORG_MCP_HTTP_TOKEN`** — optional: require `Authorization: Bearer <token>`
  on every request, as a second lock behind the proxy.
- **`ORG_MCP_HTTP_TOKEN_HEADER`** — optional: carry the token in this header
  instead (e.g. `X-Upstream-Key: <token>`, raw value, no `Bearer`). For proxies
  that own `Authorization` themselves, such as Pomerium in MCP mode. A missing or
  wrong value gets `403` (not `401`, which would make an OAuth proxy try
  upstream OAuth).
- **`ORG_MCP_ALLOWED_ORIGINS`** — comma-separated; requests carrying any other
  `Origin` header get `403` (DNS-rebinding guard). Requests without `Origin`
  (server-to-server) are allowed.
- **Dedicated Emacs:** `emacsclient` honours `EMACS_SOCKET_NAME`, so e.g.
  `EMACS_SOCKET_NAME=org-mcp` points the server at an `emacs --daemon=org-mcp`.
- In HTTP mode every `tools/call` is logged to stderr (tool, argument *names*,
  ok, result size — never argument values or results).

## Tests

```sh
python3 tests/test_boundary.py     # pure-Python: prin1 unwrap, escaping, Emacs-down
python3 tests/test_transport.py    # pure-Python: JSON-RPC, remote profile, HTTP mode
bash    tests/smoke.sh             # end-to-end against the live Emacs
emacsclient -e '(progn (load-file "~/code/org-mcp/org-mcp.el") \
  (load-file "~/code/org-mcp/tests/org-mcp-tests.el") \
  (ert-run-tests-batch "org-mcp"))'   # ert: confinement gate + dispatch contract
```

## License

[GPL-3.0-or-later](LICENSE) © 2026 Kjetil Rohde Jakobsen. Contributions welcome.
