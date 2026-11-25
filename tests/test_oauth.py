from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import socket
import threading
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

from common.config import RuntimeConfig
from common.mcp_server import McpApplication, Tool, serve
from common.oauth import OAuthError, OAuthServer

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthUnitTests(unittest.TestCase):
    def server(self) -> OAuthServer:
        return OAuthServer("https://mcp.example.test", b"k" * 40, "correct horse battery", "Test")

    def authorize(self, oauth: OAuthServer, challenge: str) -> tuple[str, str]:
        client_id = str(oauth.register({"redirect_uris": [CALLBACK]})["client_id"])
        redirect = oauth.authorize({
            "client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
            "password": "correct horse battery",
        })
        query = parse_qs(urlparse(redirect).query)
        self.assertEqual(query["state"], ["xyz"])
        return client_id, query["code"][0]

    def test_full_flow_and_refresh(self) -> None:
        oauth = self.server()
        verifier, challenge = pkce()
        client_id, code = self.authorize(oauth, challenge)
        tokens = oauth.token({"grant_type": "authorization_code", "client_id": client_id, "code": code,
                              "redirect_uri": CALLBACK, "code_verifier": verifier})
        self.assertTrue(oauth.valid_access_token(str(tokens["access_token"])))
        refreshed = oauth.token({"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]})
        self.assertTrue(oauth.valid_access_token(str(refreshed["access_token"])))
        self.assertFalse(oauth.valid_access_token(str(tokens["refresh_token"])))

    def test_code_is_single_use_and_needs_pkce(self) -> None:
        oauth = self.server()
        verifier, challenge = pkce()
        client_id, code = self.authorize(oauth, challenge)
        form = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "redirect_uri": CALLBACK}
        with self.assertRaises(OAuthError):
            oauth.token(form | {"code_verifier": pkce()[0]})
        oauth.token(form | {"code_verifier": verifier})
        with self.assertRaises(OAuthError):
            oauth.token(form | {"code_verifier": verifier})

    def test_rejects_foreign_redirects_wrong_password_and_forged_tokens(self) -> None:
        oauth = self.server()
        with self.assertRaises(OAuthError):
            oauth.register({"redirect_uris": ["https://evil.example/cb"]})
        with self.assertRaises(OAuthError):
            oauth.register({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback.evil.example"]})
        client_id = str(oauth.register({"redirect_uris": [CALLBACK]})["client_id"])
        base = {"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
                "code_challenge": pkce()[1], "code_challenge_method": "S256"}
        with self.assertRaises(OAuthError) as wrong:
            oauth.authorize(base | {"password": "nope"})
        self.assertEqual(wrong.exception.error, "access_denied")
        other = OAuthServer("https://mcp.example.test", b"x" * 40, "correct horse battery", "Test")
        self.assertFalse(oauth.valid_access_token(str(other._issue(client_id)["access_token"])))
        with self.assertRaises(OAuthError):
            oauth.authorize(base | {"client_id": "forged.client", "password": "correct horse battery"})

    def test_lockout_after_repeated_failures(self) -> None:
        oauth = self.server()
        client_id = str(oauth.register({"redirect_uris": [CALLBACK]})["client_id"])
        base = {"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
                "code_challenge": pkce()[1], "code_challenge_method": "S256"}
        for _ in range(5):
            with self.assertRaises(OAuthError):
                oauth.authorize(base | {"password": "nope"})
        with self.assertRaises(OAuthError) as locked:
            oauth.authorize(base | {"password": "correct horse battery"})
        self.assertEqual(locked.exception.status, 429)


class OAuthHttpTests(unittest.TestCase):
    def test_http_flow_reaches_mcp(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        oauth = OAuthServer("https://mcp.example.test", b"k" * 40, "correct horse battery", "Test")
        runtime = RuntimeConfig("static-secret", 5, 16, 10, 31, 60, frozenset({"127.0.0.1"}))
        app = McpApplication("test", "1", runtime, [Tool("read_item", "r", {"type": "object"}, lambda _: {"ok": True})])
        threading.Thread(target=serve, args=(app, "127.0.0.1", port, oauth), daemon=True).start()
        time.sleep(0.3)

        def request(method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()

        status, headers, _ = request("POST", "/mcp", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata=", headers.get("WWW-Authenticate", ""))
        status, _, body = request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(json.loads(body)["code_challenge_methods_supported"], ["S256"])
        status, _, body = request("POST", "/register", json.dumps({"redirect_uris": [CALLBACK]}).encode(), {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        client_id = json.loads(body)["client_id"]
        verifier, challenge = pkce()
        params = {"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1"}
        status, _, page = request("GET", "/authorize?" + urlencode(params))
        self.assertEqual(status, 200)
        self.assertIn(b"type=password", page)
        form = urlencode(params | {"password": "correct horse battery"}).encode()
        status, headers, _ = request("POST", "/authorize", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 302)
        code = parse_qs(urlparse(headers["Location"]).query)["code"][0]
        token_form = urlencode({"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                "redirect_uri": CALLBACK, "code_verifier": verifier}).encode()
        status, _, body = request("POST", "/token", token_form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        access = json.loads(body)["access_token"]
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        status, _, body = request("POST", "/mcp", payload, {"Content-Type": "application/json", "Authorization": f"Bearer {access}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["tools"][0]["name"], "read_item")


if __name__ == "__main__":
    unittest.main()
