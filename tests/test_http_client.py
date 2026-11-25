from __future__ import annotations

import threading
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

from common.errors import ServiceError
from common.http_client import CredentialSafeRedirectHandler, ProviderHttpClient


def _start(handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RedirectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: dict[str, str | None] = {}
        seen = self.seen

        class Target(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen[self.path] = self.headers.get("Authorization")
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.other = _start(Target)
        other_port = self.other.server_address[1]

        class Origin(Target):
            def do_GET(self) -> None:
                if self.path == "/cross":
                    location = f"http://127.0.0.1:{other_port}/landed-cross"
                elif self.path == "/same":
                    location = "/landed-same"
                else:
                    return super().do_GET()
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.origin = _start(Origin)
        base = f"http://127.0.0.1:{self.origin.server_address[1]}"
        self.client = ProviderHttpClient(base, "provider-secret", 5)

    def tearDown(self) -> None:
        for server in (self.origin, self.other):
            server.shutdown()
            server.server_close()

    def test_same_origin_redirect_keeps_credentials(self) -> None:
        self.client.get_json("/same")
        self.assertEqual(self.seen["/landed-same"], "Bearer provider-secret")

    def test_cross_origin_redirect_strips_credentials(self) -> None:
        self.client.get_json("/cross")
        self.assertIn("/landed-cross", self.seen)
        self.assertIsNone(self.seen["/landed-cross"])

    def test_https_to_http_redirect_is_refused(self) -> None:
        handler = CredentialSafeRedirectHandler()
        request = Request("https://api.example.test/v1/files/1", headers={"Authorization": "Bearer x"})
        with self.assertRaises(HTTPError):
            handler.redirect_request(request, None, 302, "Found", Message(), "http://cdn.example.test/file")

    def test_cross_origin_https_redirect_strips_api_key(self) -> None:
        handler = CredentialSafeRedirectHandler()
        request = Request("https://api.example.test/v1/files/1", headers={"X-API-Key": "k", "Accept": "application/pdf"})
        redirected = handler.redirect_request(request, None, 302, "Found", Message(), "https://cdn.example.test/file")
        self.assertNotIn("x-api-key", {name.lower() for name in redirected.headers})
        self.assertEqual(redirected.get_header("Accept"), "application/pdf")

    def test_refused_redirect_becomes_structured_error(self) -> None:
        class Refusing:
            def __call__(self, req, timeout):
                raise HTTPError(req.full_url, 302, "Refusing redirect away from HTTPS", Message(), None)

        client = ProviderHttpClient("https://api.example.test", "t", 5, opener=Refusing())
        with self.assertRaises(ServiceError) as caught:
            client.request("/v1/files/1")
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
