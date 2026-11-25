from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from common.config import RuntimeConfig
from common.downloads import DownloadStore
from common.errors import ServiceError
from services.getmyinvoices.provider import DEFAULT_DOCUMENT_TYPES, DEFAULT_LIST_PARAMS, GmiHttpClient, GmiProvider


class _Response:
    def __init__(self, payload: object):
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        pass


class FakeOpener:
    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        path = urlsplit(request.full_url).path.removeprefix("/accounts/v3")
        return _Response(self.routes[path])


ACCOUNT = {"accountId": "G-1-0", "apiKeyType": "READ_ONLY"}


def make_provider(routes: dict[str, object], **overrides) -> tuple[GmiProvider, FakeOpener]:
    opener = FakeOpener(routes)
    runtime = RuntimeConfig("secret", 5, 1024, 50, 366, 60, frozenset())
    http = GmiHttpClient("https://api.getmyinvoices.com/accounts/v3", "key", 5, auth_header="X-API-KEY", auth_scheme="", opener=opener, user_agent="test (GetMyInvoices account G-1-0)")
    values = dict(
        runtime=runtime, http=http, account_scope="G-1-0", scope_param=None, account_path="/account",
        list_path="/documents", detail_template="/documents/{document_id}", download_template="/documents/{document_id}",
        detail_query={}, download_query={}, file_content_field="fileContent", param_map=dict(DEFAULT_LIST_PARAMS),
        document_types=DEFAULT_DOCUMENT_TYPES, require_read_only_key=True,
        downloads=DownloadStore(1024, 60, tempfile.mkdtemp()), public_base_url=None,
    )
    values.update(overrides)
    return GmiProvider(**values), opener


class GetMyInvoicesTests(unittest.TestCase):
    def test_headers_use_api_key_and_user_agent_without_bearer(self) -> None:
        provider, opener = make_provider({"/account": ACCOUNT})
        provider.health_check({})
        headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
        self.assertEqual(headers["x-api-key"], "key")
        self.assertIn("G-1-0", headers["user-agent"])
        self.assertEqual(opener.requests[0].get_method(), "GET")

    def test_rejects_key_that_is_not_read_only(self) -> None:
        provider, _ = make_provider({"/account": {"accountId": "G-1-0", "apiKeyType": "FULL_PERMISSION"}})
        with self.assertRaises(ServiceError) as caught:
            provider.health_check({})
        self.assertEqual(caught.exception.error_code, "API_KEY_NOT_READ_ONLY")

    def test_rejects_foreign_account_before_listing(self) -> None:
        provider, opener = make_provider({"/account": {"accountId": "G-2-0", "apiKeyType": "READ_ONLY"}, "/documents": {"records": []}})
        with self.assertRaises(ServiceError) as caught:
            provider.list_documents({"date_from": "2025-01-01", "date_to": "2025-12-31"})
        self.assertEqual(caught.exception.error_code, "ACCOUNT_SCOPE_VIOLATION")
        self.assertEqual(len(opener.requests), 1)

    def test_list_maps_filters_and_pages(self) -> None:
        provider, opener = make_provider({"/account": ACCOUNT, "/documents": {"records": [{"documentUid": 7, "filename": "a.pdf"}], "totalCount": 3, "maxPages": 3}})
        result = provider.list_documents({"date_from": "2025-01-01", "date_to": "2025-12-31", "page_size": 1, "document_type": "INCOMING_INVOICE", "status": "paid"})
        query = parse_qs(urlsplit(opener.requests[1].full_url).query)
        self.assertEqual(query["startDateFilter"], ["2025-01-01"])
        self.assertEqual(query["endDateFilter"], ["2025-12-31"])
        self.assertEqual(query["perPage"], ["1"])
        self.assertEqual(query["pageNumber"], ["1"])
        self.assertEqual(query["documentTypeFilter[]"], ["INCOMING_INVOICE"])
        self.assertEqual(query["paymentStatusFilter"], ["paid"])
        self.assertEqual(result["documents"][0]["document_id"], "7")
        self.assertEqual(result["next_cursor"], "2")
        last = provider.list_documents({"date_from": "2025-01-01", "date_to": "2025-12-31", "cursor": "3"})
        self.assertIsNone(last["next_cursor"])

    def test_download_decodes_base64_file_content(self) -> None:
        pdf = b"%PDF-1.4 test"
        provider, _ = make_provider({"/account": ACCOUNT, "/documents/7": {"documentUid": 7, "filename": "re 1.pdf", "fileContent": base64.b64encode(pdf).decode()}})
        result = provider.download_document({"document_id": "7"})
        self.assertEqual(result["mime_type"], "application/pdf")
        self.assertEqual(result["size"], len(pdf))
        self.assertEqual(result["file_name"], "re_1.pdf")

    def test_download_rejects_unknown_file_type(self) -> None:
        provider, _ = make_provider({"/account": ACCOUNT, "/documents/7": {"documentUid": 7, "fileContent": base64.b64encode(b"MZ\x90\x00").decode()}})
        with self.assertRaises(ServiceError) as caught:
            provider.download_document({"document_id": "7"})
        self.assertEqual(caught.exception.error_code, "UNSUPPORTED_FILE_TYPE")

    def test_metadata_does_not_return_file_content(self) -> None:
        provider, _ = make_provider({"/account": ACCOUNT, "/documents/7": {"documentUid": 7, "fileContent": "JVBERi0="}})
        result = provider.get_metadata({"document_id": "7"})
        self.assertNotIn("fileContent", json.dumps(result))
        self.assertFalse(result["document"]["field_mapping_verified"])

    def test_document_types_are_static(self) -> None:
        provider, opener = make_provider({})
        self.assertIn("INCOMING_INVOICE", provider.list_document_types({})["document_types"])
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
