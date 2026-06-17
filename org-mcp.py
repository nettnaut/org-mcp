#!/usr/bin/env python3
# Copyright (C) 2026 Kjetil Rohde Jakobsen
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software under the GNU General Public License v3
# or later; see the LICENSE file or <https://www.gnu.org/licenses/>.
"""MCP server exposing org-mode + org-roam to Claude via a live Emacs.

Structure mirrors the in-house mu4e-mcp.py: zero dependencies, a hand-rolled
JSON-RPC stdio loop. The data layer is different — every org tool calls a single
elisp entry point, `org-mcp-dispatch`, over `emacsclient -e`, passing the tool
name and a JSON-encoded argument object. All org logic lives in org-mcp.el.

Two boundary gotchas handled here:
  1. `emacsclient -e` prints the return value with `prin1`, so a returned string
     comes back wrapped in quotes with `"` and `\\` escaped. `_unwrap_elisp_string`
     reverses that before json.loads.
  2. The only thing this file ever interpolates into elisp is a JSON string,
     escaped into an elisp string literal by `_elisp_str` — no note body ever
     touches elisp syntax directly.

Content search (org_search_content) runs ripgrep directly in Python for speed,
pinned to the org-roam root, and only calls Emacs for tag->file and line->node
resolution. See DESIGN.md.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ORG_MCP_EL = os.path.join(HERE, "org-mcp.el")
EMACS_TIMEOUT = 10
TRANSCRIPTS_GLOB = "!agent-shell-transcripts/**"  # mirrors org-roam-file-exclude-regexp


# --------------------------------------------------------------------------- #
# stdio plumbing
# --------------------------------------------------------------------------- #
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class EmacsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Python <-> elisp boundary
# --------------------------------------------------------------------------- #
def _elisp_str(s):
    """Encode a Python string as an elisp string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unwrap_elisp_string(out):
    """Reverse `prin1` quoting of a returned elisp string back to its text.

    emacsclient prints a string as `"...\\"...\\\\..."`; only `"` and `\\` are
    escaped (newlines are literal). Un-escape pairwise: `\\X` -> X.
    """
    s = out.strip()
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        raise EmacsError(f"unexpected emacsclient output: {s[:200]}")
    inner = s[1:-1]
    res, i, n = [], 0, len(inner)
    while i < n:
        c = inner[i]
        if c == "\\" and i + 1 < n:
            res.append(inner[i + 1])
            i += 2
        else:
            res.append(c)
            i += 1
    return "".join(res)


def _emacs_eval(form, timeout=EMACS_TIMEOUT):
    try:
        p = subprocess.run(
            ["emacsclient", "-e", form],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise EmacsError("emacsclient not found on PATH")
    except subprocess.TimeoutExpired:
        raise EmacsError("Emacs call timed out; is Emacs wedged?")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise EmacsError(msg or "Emacs server not reachable; is Emacs running?")
    return p.stdout


def dispatch(tool, args):
    """Call (org-mcp-dispatch TOOL JSON-ARGS); return the parsed JSON result.

    On a void-function error (org-mcp.el not loaded yet), load it once and retry.
    """
    form = f"(org-mcp-dispatch {_elisp_str(tool)} {_elisp_str(json.dumps(args))})"
    try:
        out = _emacs_eval(form)
    except EmacsError as e:
        if "void" in str(e).lower() and "org-mcp" in str(e).lower():
            _emacs_eval(f"(load-file {_elisp_str(ORG_MCP_EL)})")
            out = _emacs_eval(form)
        else:
            raise
    return json.loads(_unwrap_elisp_string(out))


_ROOTS = None


def roots():
    global _ROOTS
    if _ROOTS is None:
        out = _emacs_eval(f"(org-mcp-roots)")
        _ROOTS = json.loads(_unwrap_elisp_string(out))
    return _ROOTS


# --------------------------------------------------------------------------- #
# Content search (ripgrep, pinned to the roam root)
# --------------------------------------------------------------------------- #
def _snippet(path, line, ctx):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    lo = max(0, line - 1 - ctx)
    hi = min(len(lines), line + ctx)
    return "".join(lines[lo:hi]).rstrip()[:1000]


def search_content(query, tags=None, max_results=20, context_lines=2):
    root = roots()["roam"]
    targets = [root]
    if tags:
        files = dispatch("tag_files", {"tags": tags})
        if not files:
            return []  # no node carries those tags
        targets = files

    cmd = ["rg", "--json", "-i", "--max-count", str(max_results),
           "--glob", TRANSCRIPTS_GLOB, "--glob", "*.org", "-e", query]
    cmd += targets
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 1):  # 1 = no matches
        raise RuntimeError(p.stderr.strip() or "ripgrep failed")

    results = []
    for raw in p.stdout.splitlines():
        if not raw.strip():
            continue
        ev = json.loads(raw)
        if ev.get("type") != "match":
            continue
        data = ev["data"]
        path = data["path"]["text"]
        line = data["line_number"]
        node = dispatch("node_at", {"file": path, "line": line})
        results.append({
            "file": path,
            "line": line,
            "heading": node.get("heading", ""),
            "id": node.get("id", ""),
            "link": node.get("link", ""),
            "snippet": _snippet(path, line, context_lines),
        })
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------------------- #
# Tool declarations
# --------------------------------------------------------------------------- #
def _obj(props, required):
    return {"type": "object", "properties": props, "required": required}

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_INT = {"type": "integer"}

TOOLS = [
    {"name": "org_search",
     "description": "Search org-roam nodes by TITLE, TAGS, or ALIASES (metadata only, "
                    "not body text — use org_search_content for body text). Returns slim "
                    "node summaries with id, title, tags, file, and an [[id:]] link.",
     "inputSchema": _obj({"query": _STR,
                          "max_results": {**_INT, "default": 20}}, ["query"])},
    {"name": "org_search_content",
     "description": "Full-text search across the BODY of all notes (ripgrep). Use to find "
                    "notes that *mention* something. Optional `tags` restricts to notes "
                    "carrying any of those roam tags. Returns file, line, enclosing heading, "
                    "resolved node id + link, and a snippet with context.",
     "inputSchema": _obj({"query": _STR, "tags": _STRS,
                          "max_results": {**_INT, "default": 20},
                          "context_lines": {**_INT, "default": 2}}, ["query"])},
    {"name": "org_get_node",
     "description": "Fetch a node's full content, properties, outline path and tags, "
                    "by org-roam id, title, or alias.",
     "inputSchema": _obj({"id_or_title": _STR}, ["id_or_title"])},
    {"name": "org_backlinks",
     "description": "List org-roam nodes that link TO the node with the given id.",
     "inputSchema": _obj({"id": _STR}, ["id"])},
    {"name": "org_create_node",
     "description": "Create a new org-roam node (new file) via org-roam capture. Returns "
                    "the new id, file, and [[id:]] link.",
     "inputSchema": _obj({"title": _STR, "content": _STR, "tags": _STRS}, ["title"])},
    {"name": "org_create_daily",
     "description": "Append a timestamped entry to the org-roam daily file for `date` "
                    "(default 'today'; or an org date like '2026-06-20').",
     "inputSchema": _obj({"content": _STR,
                          "date": {**_STR, "default": "today"}}, ["content"])},
    {"name": "org_insert_link",
     "description": "Insert an [[id:]] link to target_id inside the node source_id. "
                    "Optional description overrides the link text.",
     "inputSchema": _obj({"source_id": _STR, "target_id": _STR, "description": _STR},
                         ["source_id", "target_id"])},
    {"name": "org_agenda",
     "description": "Run one of the user's custom agenda commands by key (e.g. 'g' GTD "
                    "dashboard, 'j' all NEXT, 'R' weekly review). Returns structured items "
                    "(text, todo state, id, file) plus the rendered agenda text.",
     "inputSchema": _obj({"key": {**_STR, "default": "g"}}, [])},
    {"name": "org_capture_todo",
     "description": "Create a new TODO. `target` is a friendly name (inbox/agenda/notes/"
                    "someday) or a filename, default 'inbox'. Returns the new TODO's id "
                    "(usable with org_update_todo).",
     "inputSchema": _obj({"text": _STR, "target": {**_STR, "default": "inbox"},
                          "tags": _STRS}, ["text"])},
    {"name": "org_update_todo",
     "description": "Modify an existing TODO heading (located by org id). Any of: change "
                    "`state` (NEXT/TODO/WAITING/SOMEDAY/DONE/CANCELLED), set `schedule` / "
                    "`deadline` (org date string), or `refile` to a target file.",
     "inputSchema": _obj({"id": _STR, "state": _STR, "schedule": _STR,
                          "deadline": _STR, "refile": _STR}, ["id"])},
    {"name": "org_ensure_todo_ids",
     "description": "Backfill org IDs onto tasks. Ensures every TODO-state heading "
                    "(any keyword) in `files` — default the user's `org-agenda-files` "
                    "— has an :ID:, which org_update_todo and org_get_node locate by. "
                    "Tasks created before this server lack one, so a content-search on "
                    "them resolves to the file node instead; run this once to fix that. "
                    "Pass dry_run=true to only count. Returns todos/missing/added counts, "
                    "per-file detail, and any skipped (out-of-root) files.",
     "inputSchema": _obj({"files": _STRS,
                          "dry_run": {"type": "boolean", "default": False}}, [])},
]

# Tools served by Python directly (not a 1:1 elisp dispatch).
_LOCAL = {"org_search_content"}


def call_tool(name, args):
    if name == "org_search_content":
        return search_content(
            args["query"], args.get("tags"),
            args.get("max_results", 20), args.get("context_lines", 2))
    return dispatch(name, args)


# --------------------------------------------------------------------------- #
# JSON-RPC handling
# --------------------------------------------------------------------------- #
def handle(request):
    method = request.get("method")
    rid = request.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "org-mcp", "version": "1.0.0"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = request["params"]["name"]
        args = request["params"].get("arguments", {})
        try:
            result = call_tool(name, args)
            is_error = isinstance(result, dict) and "error" in result
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                "isError": is_error,
            }})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }})
    else:
        if rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(request)


if __name__ == "__main__":
    main()
