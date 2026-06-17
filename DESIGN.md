# org-mcp — Design & Decision Log

An MCP server giving Claude search, context, note creation, linking, and
org-agenda access over Kjetil's `~/Notes` org-mode + org-roam knowledge base.

Status: **design locked, pre-implementation.** Authored via the brainstorming
process (Understanding Lock confirmed). Implementation plan to follow via
concise-planning.

---

## Understanding Summary

- **What:** A bespoke, single-file Python MCP server (`org-mcp.py`) plus a
  companion elisp library (`org-mcp.el`) that gives Claude search, context
  retrieval, note creation, linking, and org-agenda access over the `~/Notes`
  org-roam KB.
- **Why:** Neither candidate repo fits. `aserranoni/org-roam-mcp` (Python, reads
  org-roam SQLite, can write notes/links) has **no agenda**.
  `szaffarano/org-mcp-server` (Rust, file parser) has **agenda but is
  read-only, no roam/backlinks/linking**. Both reimplement org logic *outside*
  Emacs, which would drift from Kjetil's heavily customized GTD config.
- **Who:** Single user (Kjetil), via Claude in agent-shell (ACP) inside Emacs,
  and via bare `claude` CLI outside agent-shell.
- **Architecture:** Thin bridge — every tool builds an elisp form and calls
  `emacsclient -e`; the live Emacs does the real work (real custom agenda
  commands, real `org-roam-capture`/`org-id`, autosync). **All writes** go
  through Emacs.
- **Pattern:** Mirror the in-house `~/.local/mcp/mu4e-mcp.py` — zero
  dependencies, hand-rolled JSON-RPC stdio loop.
- **Non-goals:** No standalone file/SQLite parser; no direct file writes behind
  Emacs's back; not forking either repo (tool *shapes* lifted only); no
  multi-user/remote/networked operation.

## Assumptions

- Emacs server is always up in Kjetil's workflow; if down, tools return a clean
  error rather than falling back.
- ~224 notes, single user, interactive use. `emacsclient` latency (tens of ms)
  is fine. No caching layer.
- Local-only, stdio, no network. Server inherits the user's permissions.
- Low concurrency; no locking beyond what Emacs already provides.
- Slim JSON output, capped body sizes, return ready-to-paste `[[id:…]]` links.

---

## Decision Log

| # | Decision | Alternatives considered | Why |
|---|----------|------------------------|-----|
| D1 | **Build bespoke, do not fork** | Fork aserranoni (Python, roam, write, no agenda); fork szaffarano (Rust, agenda, read-only, no roam) | Neither covers the full ask (search+context+create+link+agenda); both reimplement org logic outside Emacs and would drift from the custom GTD config. |
| D2 | **Bridge to the live Emacs via `emacsclient`** (option A) | (B) standalone DB+file parser; (C) hybrid reads-direct/writes-via-Emacs | Reuses the *actual* config: custom agenda commands, TODO keywords, capture templates, autosync. Emacs is always up in this workflow. |
| D3 | **Python**, zero-dependency, hand-rolled JSON-RPC stdio | Emacs-Lisp server-side; Go | Matches the proven in-house `mu4e-mcp.py` pattern; fast to write/extend; already trusted in this stack. |
| D4 | **All writes go through Emacs** (option A) | Direct file appends; read-only v1 | Correct IDs/backlinks/autosync via real `org-roam`/`org-id`; nothing edits files behind Emacs's back. |
| D5 | **Full create + modify** scope for v1 | Create-only; in-between | Kjetil wants a full GTD assistant: create notes/dailies/links AND modify existing TODOs (state, schedule, refile). |
| D6 | **Approach 3: typed tools + companion `org-mcp.el`**, JSON in/out | (1) generic `org_eval`; (2) elisp inlined as Python strings | Clean separation (Python=plumbing, elisp=org logic); JSON boundary removes elisp string-escaping footguns; testable elisp. |
| D7 | **Add `org_search_content` (ripgrep full-text)** | roam metadata search only | org-roam DB indexes titles/tags/aliases/links only, not body text; content search is a core need. ripgrep run in Python for speed (read-only exception to "all via elisp"). |
| D8 | **Source + docs in `~/code/org-mcp` (own git repo)** | In `~/.emacs.d` repo; unversioned in `~/.local/mcp/` | Standalone, publishable later; keeps emacs.d clean. |
| D9 | **Register in BOTH `Emacs.org` (`agent-shell-mcp-servers`) and `~/.claude.json`** | Only agent-shell | Works inside agent-shell AND from bare `claude` CLI; mirrors mu4e. |
| D10 | **Path confinement gate, double-sided, canonical-path based** | Trust the model; Python-only check | Server must only read/write inside `org-roam-directory`/`org-directory`. `file-truename` defeats `..`, absolute escapes, and symlink escapes; ripgrep root pinned in Python. |
| D11 | **All writes complete non-interactively via dedicated immediate-finish capture** (org-roam-capture for nodes, dailies capture for dailies, org-capture into inbox.org for TODOs) | Reuse Kjetil's existing interactive templates; hand-build files via `org-id-get-create` + `org-roam-db-update-file` | `emacsclient -e` is synchronous and headless — an interactive capture buffer (or a `%?` escape, as in the dailies template) would hang until timeout. Claude supplies full content as args, so there is nothing for a human to fill. Immediate-finish capture stays inside org-roam's pipeline so slug/`:ID:`/DB wiring follow org-roam's own conventions rather than being reimplemented. One *principle* (non-interactive completion), the right capture machinery per target. |

---

## Tool Surface (v1) — 10 tools

### Search & context
- `org_search(query, max_results=20)` — org-roam metadata search (title/tags/
  aliases). Returns `id`, `title`, `tags`, `file`, `[[id:…]]` link.
- `org_search_content(query, tags=[], max_results=20, context_lines=2)` —
  full-text body search via ripgrep over the roam root (excludes
  `agent-shell-transcripts/`). Optional `tags` filter resolves matching files
  from the roam DB first, then restricts ripgrep to them. Returns `file`,
  `line`, enclosing `heading`, resolved roam `id` + link, snippet.
- `org_get_node(id_or_title)` — full node/heading content, properties, outline
  path, existing links. (Context access.)
- `org_backlinks(id)` — nodes linking *to* this node.

### Note creation & linking (writes via Emacs)
- `org_create_node(title, content, tags=[], file=None)` — via real
  `org-roam-capture` (immediate-finish template). Returns new `id` + link.
- `org_create_daily(content, date="today")` — appends to `daily/` via dailies
  capture.
- `org_insert_link(source_id, target_id, description=None)` — adds an
  `[[id:…]]` link inside an existing node.

### Agenda & TODOs (full create + modify)
- `org_agenda(key="g")` — runs a custom agenda command by key (`g`, `d`, `j`,
  `R`, …); returns structured items (heading, todo state, scheduled/deadline,
  tags, file, `id`).
- `org_capture_todo(text, target="inbox", tags=[])` — create an inbox/agenda
  TODO.
- `org_update_todo(id, state=…, schedule=…, deadline=…, refile=…)` — modify an
  existing TODO via org's own API.

---

## Architecture & Data Flow

```
Claude → MCP tools/call (JSON args)
      → org-mcp.py builds an emacsclient call
      → emacsclient -e "(org-mcp-dispatch \"TOOL\" JSON-ARGS-STRING)"
      → Emacs: org-mcp-dispatch
                 (json-parse-string args) → plist/hash
                 → confinement gate (org-mcp--confine)
                 → org-mcp-TOOL …
                 → json-encode result
      → org-mcp.py reads stdout, strips prin1 quoting, json.loads
      → MCP text content (JSON) back to Claude
```

- **One elisp entry point** (`org-mcp-dispatch`). Python only ever interpolates
  a single JSON **string literal** into elisp — no multi-line note body touches
  elisp syntax. Pure JSON in both directions.
- **Content search bypasses Emacs** (ripgrep in Python) except tag→files and
  line→node-id resolver helpers.

## Confinement Gate (security)

- Roots read once from the live Emacs at startup:
  `(file-truename org-roam-directory)` and `(file-truename org-directory)` —
  never hardcoded.
- **Elisp side** `org-mcp--confine`: every read/write canonicalizes its target
  with `file-truename` (resolves `..`, `~`, symlinks) and asserts a root prefix.
  Out-of-root → `{"error":"path outside org root"}`, no operation. Writes are
  double-gated (must be a real org file under a root).
- **Python side**: ripgrep root pinned to the roam root; caller paths with `..`
  or absolute-outside are rejected before reaching elisp.
- `org_agenda` takes no caller path (renders existing `org-agenda-files`).

## Error Handling & Edge Cases

- **Emacs down:** `emacsclient` fails fast → clean `isError` MCP result; 10s
  subprocess timeout guards a wedged Emacs.
- **prin1 gotcha:** `emacsclient -e` prints the return via `prin1`, so a string
  comes back quoted/escaped — Python unwraps before `json.loads`. Locked with a
  multi-line-body test up front. (The org-equivalent of the wuzapi-stderr
  lesson.)
- **Write failures / interactive prompts:** every tool wrapped in
  `condition-case` returning `{"error":…}`; force non-interactive capture
  (immediate-finish, suppress prompts) so `emacsclient` never blocks on a
  minibuffer.
- **Empty/not-found:** search → `[]`; unknown id → `{"error":"node not found"}`;
  unknown agenda key → `{"error":…, "available":[…]}`.
- **Autosync:** `org-roam-db-sync` (or autosync) before returning a new id.
- **Concurrency:** serialized by the single Emacs / single `emacsclient`.

## File Layout

```
~/code/org-mcp/
  org-mcp.py        # zero-dep stdio server (MCP plumbing + JSON)
  org-mcp.el        # org-mcp-dispatch + org-mcp-* (all org logic)
  README.md         # what it is, prin1 + confinement gotchas, tool list
  DESIGN.md         # this file
  tests/            # ert (elisp), python unit tests, smoke.sh
```

## Registration

1. `Emacs.org` → `agent-shell-mcp-servers` gains an `org_mcp` entry
   (`python3 ~/code/org-mcp/org-mcp.py`); re-tangles to `Emacs.el`.
2. `~/.claude.json` → same server under `mcpServers` (bare `claude` CLI).
3. `org-mcp.el` loaded into Emacs (via `Emacs.org`) so `org-mcp-dispatch` exists.

## Testing Strategy

- **elisp (ert):** against a throwaway temp org-roam dir — create→search→
  backlink; confine rejects `../escape`.
- **Python:** JSON↔elisp framing + prin1 unwrap on a multi-line body;
  Emacs-down → clean error; path pre-validation rejects `..`.
- **Smoke:** `tests/smoke.sh` pipes a real `tools/call` into the server and
  checks the response.

## Reference Prior Art

- `aserranoni/org-roam-mcp` — Python, org-roam SQLite, create/update/link, no
  agenda. (Tool shapes for roam search/backlinks/create.)
- `szaffarano/org-mcp-server` — Rust, file parser, read-only, agenda + outline/
  heading/id resources. (Tool shapes for agenda + heading resources.)
- In-house `~/.local/mcp/mu4e-mcp.py` — the structural template (zero-dep stdio,
  slim JSON, org-link return values).
