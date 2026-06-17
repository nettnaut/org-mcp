#!/usr/bin/env python3
"""Pure-Python unit tests for the org-mcp Python/elisp boundary.

No Emacs required. Run: python3 tests/test_boundary.py
Covers the prin1 unwrap, the elisp-string escaping, their round-trip on a
multi-line body, and the Emacs-down clean error.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "orgmcp", os.path.join(os.path.dirname(__file__), "..", "org-mcp.py"))
orgmcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orgmcp)


def emacs_prin1(s):
    """Emulate how `emacsclient -e` prints an elisp string: wrap in quotes,
    escape backslash and double-quote (newlines stay literal)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Boundary(unittest.TestCase):
    def test_unwrap_simple(self):
        payload = '{"id":"abc","title":"hi"}'
        self.assertEqual(orgmcp._unwrap_elisp_string(emacs_prin1(payload)), payload)

    def test_unwrap_multiline_body_with_quotes(self):
        # JSON whose string values contain newlines (\n) and quotes (\")
        payload = json.dumps({"content": 'Line one.\nLine "two", with a comma.'})
        unwrapped = orgmcp._unwrap_elisp_string(emacs_prin1(payload))
        self.assertEqual(unwrapped, payload)
        self.assertEqual(json.loads(unwrapped)["content"],
                         'Line one.\nLine "two", with a comma.')

    def test_unwrap_rejects_non_string(self):
        with self.assertRaises(orgmcp.EmacsError):
            orgmcp._unwrap_elisp_string("nil")

    def test_elisp_str_escaping(self):
        self.assertEqual(orgmcp._elisp_str('a"b\\c'), '"a\\"b\\\\c"')

    def test_emacs_down_clean_error(self):
        # Point emacsclient at a socket that cannot exist -> EmacsError, no trace.
        old = os.environ.get("EMACS_SOCKET_NAME")
        os.environ["EMACS_SOCKET_NAME"] = "/nonexistent/org-mcp-test-socket"
        try:
            with self.assertRaises(orgmcp.EmacsError):
                orgmcp._emacs_eval('(+ 1 1)', timeout=5)
        finally:
            if old is None:
                os.environ.pop("EMACS_SOCKET_NAME", None)
            else:
                os.environ["EMACS_SOCKET_NAME"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
