#!/usr/bin/env bash
# End-to-end smoke test for org-mcp against the live Emacs.
# Pipes real JSON-RPC requests into the server and checks the responses.
# Requires: a running Emacs server with org-mcp.el loaded, ripgrep, python3.
set -euo pipefail
cd "$(dirname "$0")/.."

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
  echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
  echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"org_search","arguments":{"query":"","max_results":1}}}'
  echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"org_search_content","arguments":{"query":"org","max_results":1}}}'
  echo '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"org_agenda","arguments":{"key":"g"}}}'
  echo '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"org_get_node","arguments":{"id_or_title":"definitely-missing-xyz"}}}'
} | python3 org-mcp.py > "$tmp"

# Validator reads the captured responses from the file path in argv[1],
# so there is no stdin/heredoc collision.
python3 - "$tmp" <<'PY'
import sys, json
resp = {}
with open(sys.argv[1]) as f:
    for ln in f:
        ln = ln.strip()
        if ln:
            o = json.loads(ln); resp[o["id"]] = o

assert resp[1]["result"]["serverInfo"]["name"] == "org-mcp"; print("ok  initialize")
n = len(resp[2]["result"]["tools"]); assert n == 10, n; print(f"ok  tools/list ({n})")
assert not resp[3]["result"]["isError"]; print("ok  org_search")
assert not resp[4]["result"]["isError"]; print("ok  org_search_content")
items = json.loads(resp[5]["result"]["content"][0]["text"])
assert "items" in items; print(f"ok  org_agenda ({len(items['items'])} items)")
assert resp[6]["result"]["isError"]; print("ok  org_get_node error path")
print("\nSMOKE PASS")
PY
