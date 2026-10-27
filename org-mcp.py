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

Transports: stdio (default) or Streamable HTTP (`--http HOST:PORT`), both
served by the same `handle`. `ORG_MCP_PROFILE=remote` narrows the tool surface
to read tools + inbox capture for exposure beyond the local machine.
"""

import argparse
import copy
import hmac
import ipaddress
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ORG_MCP_EL = os.path.join(HERE, "org-mcp.el")
EMACS_TIMEOUT = 10
TRANSCRIPTS_GLOB = "!agent-shell-transcripts/**"  # mirrors org-roam-file-exclude-regexp

# Newest first; `initialize` echoes the client's version when it is listed here.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# Tool profiles. "remote" is for any transport reachable from outside this
# machine: read tools plus capture-to-inbox, nothing that edits existing notes.
REMOTE_TOOLS = frozenset({"org_search", "org_search_content", "org_get_node",
                          "org_agenda", "org_backlinks", "org_capture_todo"})
PROFILES = {"full": None, "remote": REMOTE_TOOLS}
PROFILE = os.environ.get("ORG_MCP_PROFILE", "full")

# HTTP mode. Meant to sit behind an authenticating proxy on loopback; the
# optional static bearer token is a second lock, not the primary auth.
HTTP_PATH = "/mcp"
HTTP_TOKEN = os.environ.get("ORG_MCP_HTTP_TOKEN") or None
# Header that carries the token. Unset: "Authorization: Bearer <token>". Set
# (e.g. X-Upstream-Key): the raw token in that header, for proxies that own
# Authorization themselves (Pomerium in MCP mode).
HTTP_TOKEN_HEADER = os.environ.get("ORG_MCP_HTTP_TOKEN_HEADER") or None
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ORG_MCP_ALLOWED_ORIGINS", "").split(",")
                   if o.strip()}
MAX_BODY = 1 << 20
AUDIT = False  # log every tools/call to stderr; on in HTTP mode


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
     "description": "Create a new TODO. `text` is the heading and MUST be a short, "
                    "succinct action (one line) — keep agenda/project views readable; "
                    "put any longer details, context, or links in `body` (multi-line "
                    "org markup placed in the entry body, after the property drawer), "
                    "NOT in `text`. `target` is a friendly name (inbox/agenda/notes/"
                    "someday) or a filename, default 'inbox'. In a file with a 'Tasks' "
                    "heading the TODO is filed as its child. Returns the new TODO's id "
                    "(usable with org_update_todo / org_edit_node_body).",
     "inputSchema": _obj({"text": _STR, "body": _STR,
                          "target": {**_STR, "default": "inbox"},
                          "tags": _STRS}, ["text"])},
    {"name": "org_update_todo",
     "description": "Modify an existing TODO heading (located by org id). Any of: change "
                    "`state` (NEXT/TODO/WAITING/SOMEDAY/DONE/CANCELLED), set `schedule` / "
                    "`deadline` (org date string), or `refile` to a target file.",
     "inputSchema": _obj({"id": _STR, "state": _STR, "schedule": _STR,
                          "deadline": _STR, "refile": _STR}, ["id"])},
    {"name": "org_rename_heading",
     "description": "Rename an existing heading (located by org id): replace only its "
                    "`title` text, keeping the TODO keyword, tags, planning lines, "
                    "property drawer and body intact. `title` must be a single line — "
                    "keep it a short, succinct action and move any detail into the body "
                    "with org_edit_node_body. Use this to shorten a bloated heading.",
     "inputSchema": _obj({"id": _STR, "title": _STR}, ["id", "title"])},
    {"name": "org_edit_node_body",
     "description": "Append to (or replace) the BODY text of an existing heading, "
                    "located by org id. `operation` is 'append' (default — add after "
                    "any existing body, before child headings) or 'replace' (overwrite "
                    "the body, keeping the heading, planning lines and property drawer). "
                    "`content` may be multi-line org markup, but a line starting with "
                    "'*' is rejected (this edits body text, not outline structure). "
                    "Use this to enrich a task with notes/context; use org_update_todo "
                    "for state/schedule/deadline.",
     "inputSchema": _obj({"id": _STR, "content": _STR,
                          "operation": {**_STR, "default": "append"}},
                         ["id", "content"])},
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


class ToolNotAvailable(Exception):
    """Tool unknown in the active profile -> JSON-RPC -32602, never reaches Emacs."""


def check_profile(name):
    if name not in PROFILES:
        sys.exit(f"org-mcp: unknown ORG_MCP_PROFILE {name!r} (use: {', '.join(PROFILES)})")


def _inbox_only(tool):
    t = copy.deepcopy(tool)
    t["inputSchema"]["properties"].pop("target", None)
    t["description"] = (
        "Capture a new TODO into the user's inbox (inbox.org) for later review. "
        "`text` is the heading and MUST be a short, succinct action (one line); put "
        "any longer details, context, or links in `body` (multi-line org markup "
        "placed in the entry body). Returns the new TODO's id.")
    return t


def visible_tools():
    allowed = PROFILES[PROFILE]
    if allowed is None:
        return TOOLS
    return [_inbox_only(t) if t["name"] == "org_capture_todo" else t
            for t in TOOLS if t["name"] in allowed]


def call_tool(name, args):
    allowed = PROFILES[PROFILE]
    if allowed is not None:
        if name not in allowed:
            raise ToolNotAvailable(f"Unknown tool: {name}")
        if name == "org_capture_todo":
            if args.get("target", "inbox") != "inbox":
                raise ValueError("this server only captures to the inbox")
            args = {**args, "target": "inbox"}
    if name == "org_search_content":
        return search_content(
            args["query"], args.get("tags"),
            args.get("max_results", 20), args.get("context_lines", 2))
    return dispatch(name, args)


# --------------------------------------------------------------------------- #
# JSON-RPC handling
# --------------------------------------------------------------------------- #
def _audit(name, args, ok, size):
    if AUDIT:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sys.stderr.write(f"org-mcp audit: {ts} tool={name} args={sorted(args)} "
                         f"ok={ok} bytes={size}\n")


def handle(request):
    """Answer one JSON-RPC message: the response dict, or None for a notification."""
    method = request.get("method")
    rid = request.get("id")

    def result(r):
        return {"jsonrpc": "2.0", "id": rid, "result": r}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = (request.get("params") or {}).get("protocolVersion")
        return result({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "org-mcp", "version": "1.1.0"},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": visible_tools()})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            out = call_tool(name, args)
        except ToolNotAvailable as e:
            _audit(name, args, False, 0)
            return error(-32602, str(e))
        except Exception as e:
            _audit(name, args, False, 0)
            return result({"content": [{"type": "text", "text": f"Error: {e}"}],
                           "isError": True})
        text = json.dumps(out, indent=2, ensure_ascii=False)
        is_error = isinstance(out, dict) and "error" in out
        _audit(name, args, not is_error, len(text))
        return result({"content": [{"type": "text", "text": text}], "isError": is_error})
    if rid is None:
        return None
    return error(-32601, f"Method not found: {method}")


# --------------------------------------------------------------------------- #
# Streamable HTTP (stateless: POST in, JSON out; no server-initiated stream)
# --------------------------------------------------------------------------- #
class _MCPHandler(BaseHTTPRequestHandler):
    server_version = "org-mcp"
    timeout = 30  # the server is single-threaded: don't let one slow client stall it

    def log_message(self, fmt, *a):
        sys.stderr.write("org-mcp http: " + fmt % a + "\n")

    def _reply(self, code, body=None, headers=()):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _admit(self):
        """Path, Origin and token checks shared by every method. False = replied."""
        if self.path.split("?", 1)[0] != HTTP_PATH:
            self._reply(404)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self._reply(403)
            return False
        if HTTP_TOKEN and HTTP_TOKEN_HEADER:
            # 403, not 401: a 401 makes an OAuth proxy start upstream OAuth.
            got = self.headers.get(HTTP_TOKEN_HEADER, "")
            if not hmac.compare_digest(got.encode(), HTTP_TOKEN.encode()):
                self._reply(403)
                return False
        elif HTTP_TOKEN:
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {HTTP_TOKEN}".encode()):
                self._reply(401, headers=[("WWW-Authenticate", "Bearer")])
                return False
        return True

    def do_GET(self):
        if self._admit():
            self._reply(405, headers=[("Allow", "POST")])

    do_DELETE = do_GET

    def do_POST(self):
        if not self._admit():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._reply(413)
            return
        try:
            msg = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Parse error"}})
            return
        if isinstance(msg, list):  # 2025-03-26 batches
            out = [r for r in (handle(m) for m in msg if isinstance(m, dict)) if r]
        elif isinstance(msg, dict):
            out = handle(msg)
        else:
            out = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "Invalid Request"}}
        if out:
            self._reply(200, out)
        else:
            self._reply(202)


def make_http_server(host, port):
    # Single-threaded on purpose: requests are serialised, like Emacs itself.
    return HTTPServer((host, port), _MCPHandler)


def serve_http(addr):
    global AUDIT
    host, _, port = addr.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        sys.stderr.write(f"org-mcp: WARNING binding {host} (not loopback) — put an "
                         "authenticating proxy in front and use ORG_MCP_PROFILE=remote\n")
    AUDIT = True
    httpd = make_http_server(host, int(port))
    sys.stderr.write(f"org-mcp: profile={PROFILE} listening on http://{addr}{HTTP_PATH}\n")
    httpd.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="MCP server for org-mode + org-roam")
    ap.add_argument("--http", metavar="HOST:PORT",
                    help="serve Streamable HTTP at HOST:PORT/mcp instead of stdio")
    opts = ap.parse_args(argv)
    check_profile(PROFILE)
    if opts.http:
        serve_http(opts.http)
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
