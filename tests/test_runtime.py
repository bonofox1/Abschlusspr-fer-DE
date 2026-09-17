from __future__ import annotations

import io
import tempfile
import unittest
from email.message import Message

from common.config import RuntimeConfig
from common.downloads import DownloadStore
from common.errors import ServiceError
from common.mcp_server import McpApplication, Tool
from common.validation import date_range, identifier


class FakeResponse:
    def __init__(self, body: bytes, mime: str = "application/pdf", declared_length: int | None = None):
        self._body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = mime
        self.headers["Content-Length"] = str(declared_length if declared_length is not None else len(body))
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        pass


class RuntimeTests(unittest.TestCase):
    def runtime(self) -> RuntimeConfig:
        return RuntimeConfig("secret", 5, 16, 10, 31, 60, frozenset({"localhost"}))

    def test_mcp_exposes_only_registered_read_tools(self) -> None:
        tool = Tool("read_item", "Read only", {"type": "object"}, lambda _: {"ok": True})
        app = McpApplication("test", "1", self.runtime(), [tool])
        status, response = app.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual(["read_item"], [item["name"] for item in response["result"]["tools"]])
        _, unknown = app.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_item"}})
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_validation_rejects_path_injection_and_large_ranges(self) -> None:
        with self.assertRaises(ServiceError):
            identifier("../../secret", "id")
        with self.assertRaises(ServiceError) as caught:
            date_range("2026-01-01", "2026-03-01", 31)
        self.assertEqual(caught.exception.error_code, "INVALID_DATE_RANGE")

    def test_download_store_enforces_size_and_mime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DownloadStore(4, 60, directory)
            with self.assertRaises(ServiceError) as large:
                store.save_response(FakeResponse(b"12345"), "test.pdf")
            self.assertEqual(large.exception.error_code, "DOWNLOAD_TOO_LARGE")
            with self.assertRaises(ServiceError) as mime:
                store.save_response(FakeResponse(b"abc", "text/html"), "test.html")
            self.assertEqual(mime.exception.error_code, "UNSUPPORTED_FILE_TYPE")


if __name__ == "__main__":
    unittest.main()

