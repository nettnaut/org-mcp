#!/usr/bin/env python3
"""Tests for the transport layer: JSON-RPC handling, the remote tool profile,
and the Streamable HTTP mode.

No Emacs required — the elisp boundary (`call_tool`'s dispatch) is stubbed.
Run: python3 tests/test_transport.py
"""
import http.client
import importlib.util
import json
import os
import threading
import unittest

spec = importlib.util.spec_from_file_location(
    "orgmcp", os.path.join(os.path.dirname(__file__), "..", "org-mcp.py"))
orgmcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orgmcp)

REMOTE = {"org_search", "org_search_content", "org_get_node", "org_agenda",
          "org_backlinks", "org_capture_todo"}


def rpc(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        msg["id"] = rid
    if params is not None:
        msg["params"] = params
    return msg


class StubEmacs:
    """Replace the elisp dispatch; record what reached it."""
    def __init__(self):
        self.calls = []

    def __enter__(self):
        self._orig = orgmcp.dispatch
        orgmcp.dispatch = lambda tool, args: (self.calls.append((tool, args))
                                              or {"ok": True, "tool": tool})
        return self

    def __exit__(self, *exc):
        orgmcp.dispatch = self._orig


class Profile:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._orig = orgmcp.PROFILE
        orgmcp.PROFILE = self.name

    def __exit__(self, *exc):
        orgmcp.PROFILE = self._orig


# --------------------------------------------------------------------------- #
class Handle(unittest.TestCase):
    def test_initialize_returns_response(self):
        r = orgmcp.handle(rpc("initialize", {"protocolVersion": "2024-11-05"}))
        self.assertEqual(r["id"], 1)
        self.assertEqual(r["result"]["serverInfo"]["name"], "org-mcp")

    def test_initialize_echoes_supported_protocol_version(self):
        r = orgmcp.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_falls_back_for_unknown_version(self):
        r = orgmcp.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertIn(r["result"]["protocolVersion"], orgmcp.PROTOCOL_VERSIONS)

    def test_notification_has_no_response(self):
        self.assertIsNone(orgmcp.handle(rpc("notifications/initialized", rid=None)))

    def test_unknown_method_is_jsonrpc_error(self):
        r = orgmcp.handle(rpc("nope/nope"))
        self.assertEqual(r["error"]["code"], -32601)

    def test_tool_call_wraps_result_as_text(self):
        with StubEmacs():
            r = orgmcp.handle(rpc("tools/call", {"name": "org_get_node",
                                                 "arguments": {"id_or_title": "x"}}))
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["tool"],
                         "org_get_node")


# --------------------------------------------------------------------------- #
class RemoteProfile(unittest.TestCase):
    def tool_names(self):
        return {t["name"] for t in orgmcp.handle(rpc("tools/list"))["result"]["tools"]}

    def test_full_profile_lists_everything(self):
        with Profile("full"):
            self.assertEqual(self.tool_names(), {t["name"] for t in orgmcp.TOOLS})

    def test_remote_profile_lists_only_allowed(self):
        with Profile("remote"):
            self.assertEqual(self.tool_names(), REMOTE)

    def test_remote_capture_schema_has_no_target(self):
        with Profile("remote"):
            tools = orgmcp.handle(rpc("tools/list"))["result"]["tools"]
        cap = next(t for t in tools if t["name"] == "org_capture_todo")
        self.assertNotIn("target", cap["inputSchema"]["properties"])
        self.assertIn("inbox", cap["description"].lower())

    def test_remote_profile_does_not_mutate_full_tool_list(self):
        with Profile("remote"):
            orgmcp.handle(rpc("tools/list"))
        cap = next(t for t in orgmcp.TOOLS if t["name"] == "org_capture_todo")
        self.assertIn("target", cap["inputSchema"]["properties"])

    def test_blocked_tool_call_rejected_before_emacs(self):
        blocked = {t["name"] for t in orgmcp.TOOLS} - REMOTE
        self.assertTrue(blocked)
        with Profile("remote"), StubEmacs() as emacs:
            for name in sorted(blocked):
                r = orgmcp.handle(rpc("tools/call", {"name": name, "arguments": {}}))
                self.assertEqual(r["error"]["code"], -32602, name)
            self.assertEqual(emacs.calls, [])

    def test_remote_capture_forced_to_inbox(self):
        with Profile("remote"), StubEmacs() as emacs:
            orgmcp.handle(rpc("tools/call", {"name": "org_capture_todo",
                                             "arguments": {"text": "Call Bob"}}))
        self.assertEqual(emacs.calls, [("org_capture_todo",
                                        {"text": "Call Bob", "target": "inbox"})])

    def test_remote_capture_other_target_rejected(self):
        for target in ("agenda", "projects/cue.org", "../etc/passwd"):
            with Profile("remote"), StubEmacs() as emacs:
                r = orgmcp.handle(rpc("tools/call", {
                    "name": "org_capture_todo",
                    "arguments": {"text": "x", "target": target}}))
            self.assertTrue(r["result"]["isError"], target)
            self.assertEqual(emacs.calls, [], target)

    def test_remote_allowed_read_tool_reaches_emacs(self):
        with Profile("remote"), StubEmacs() as emacs:
            orgmcp.handle(rpc("tools/call", {"name": "org_backlinks",
                                             "arguments": {"id": "abc"}}))
        self.assertEqual(emacs.calls, [("org_backlinks", {"id": "abc"})])

    def test_unknown_profile_rejected(self):
        with self.assertRaises(SystemExit):
            orgmcp.check_profile("readwrite")


# --------------------------------------------------------------------------- #
class HTTP(unittest.TestCase):
    """Run the real HTTP server on an ephemeral loopback port."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = orgmcp.make_http_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self._token = orgmcp.HTTP_TOKEN
        self._origins = orgmcp.ALLOWED_ORIGINS

    def tearDown(self):
        orgmcp.HTTP_TOKEN = self._token
        orgmcp.ALLOWED_ORIGINS = self._origins

    def request(self, method="POST", body=None, path="/mcp", headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, (json.loads(payload) if payload else None)

    def test_initialize_over_http(self):
        resp, body = self.request(body=rpc("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("application/json"))
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")

    def test_notification_is_202_empty(self):
        resp, body = self.request(body=rpc("notifications/initialized", rid=None))
        self.assertEqual(resp.status, 202)
        self.assertIsNone(body)

    def test_tools_call_over_http(self):
        with StubEmacs() as emacs:
            resp, body = self.request(body=rpc("tools/call", {
                "name": "org_get_node", "arguments": {"id_or_title": "x"}}))
        self.assertEqual(resp.status, 200)
        self.assertFalse(body["result"]["isError"])
        self.assertEqual(emacs.calls, [("org_get_node", {"id_or_title": "x"})])

    def test_get_is_405(self):
        resp, _ = self.request(method="GET")
        self.assertEqual(resp.status, 405)
        self.assertIn("POST", resp.getheader("Allow"))

    def test_wrong_path_is_404(self):
        resp, _ = self.request(body=rpc("tools/list"), path="/other")
        self.assertEqual(resp.status, 404)

    def test_bad_json_is_400_parse_error(self):
        resp, body = self.request(raw=b"{not json")
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], -32700)

    def test_oversized_body_is_413(self):
        # Only claim the size: the server must refuse before reading the body.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(orgmcp.MAX_BODY + 1))
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 413)

    def test_foreign_origin_is_403(self):
        orgmcp.ALLOWED_ORIGINS = set()
        resp, _ = self.request(body=rpc("tools/list"),
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)

    def test_allowed_origin_passes(self):
        orgmcp.ALLOWED_ORIGINS = {"https://claude.ai"}
        resp, _ = self.request(body=rpc("tools/list"),
                               headers={"Origin": "https://claude.ai"})
        self.assertEqual(resp.status, 200)

    def test_token_required_when_configured(self):
        orgmcp.HTTP_TOKEN = "s3cret-test-token"
        resp, _ = self.request(body=rpc("tools/list"))
        self.assertEqual(resp.status, 401)
        self.assertTrue(resp.getheader("WWW-Authenticate").startswith("Bearer"))
        resp, _ = self.request(body=rpc("tools/list"),
                               headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status, 401)
        resp, _ = self.request(body=rpc("tools/list"),
                               headers={"Authorization": "Bearer s3cret-test-token"})
        self.assertEqual(resp.status, 200)

    def test_remote_profile_enforced_over_http(self):
        with Profile("remote"), StubEmacs() as emacs:
            resp, body = self.request(body=rpc("tools/call", {
                "name": "org_edit_node_body",
                "arguments": {"id": "x", "content": "y"}}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["error"]["code"], -32602)
        self.assertEqual(emacs.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
