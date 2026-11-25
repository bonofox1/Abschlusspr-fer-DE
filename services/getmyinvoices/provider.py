"""Read-only provider for the GetMyInvoices Accounts API v3.

Endpoint facts (researched 2026-11-25 from the published OpenAPI description of
https://api.getmyinvoices.com/accounts/v3/doc/api.json):

* Base URL ``https://api.getmyinvoices.com/accounts/v3``, API key in the
  ``X-API-KEY`` header (no ``Bearer`` prefix).
* Every read operation is a ``GET``. The client never sends anything else.
* ``GET /account`` returns ``accountId`` and ``apiKeyType``; one of the key
  types is ``READ_ONLY``. The account is bound to the key, there is no account
  query parameter.
* ``GET /documents`` filters with ``startDateFilter``/``endDateFilter``
  (Y-m-d), pages with ``pageNumber`` (from 1) and ``perPage`` and answers with
  ``records``, ``totalCount`` and ``maxPages``. Documents are identified by
  ``documentUid``.
* ``GET /documents/{documentUid}`` returns one document.

UNCONFIRMED (taken from a summarised part of the spec, verify against the
official documentation before production use; see ``UNCONFIRMED`` below):

* the query parameter that switches the file content on/off for a single
  document (configure it through ``GMI_DETAIL_QUERY_JSON`` and
  ``GMI_DOWNLOAD_QUERY_JSON``),
* that the file arrives base64 encoded in the JSON field ``fileContent``,
* the field names for amounts, dates and supplier inside a document record.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from common.config import RuntimeConfig, json_object, required, validate_provider_url
from common.downloads import DownloadStore
from common.errors import ServiceError
from common.http_client import ProviderHttpClient
from common.validation import date_range, identifier, page_size

DEFAULT_BASE_URL = "https://api.getmyinvoices.com/accounts/v3"
DEFAULT_PRODUCT = "abschlusspruefer-de-mcp/1.0"
READ_ONLY_KEY_TYPE = "READ_ONLY"

# Mapping of our tool arguments to GetMyInvoices query parameters.
DEFAULT_LIST_PARAMS = {
    "date_from": "startDateFilter",
    "date_to": "endDateFilter",
    "page_size": "perPage",
    "cursor": "pageNumber",
    "document_type": "documentTypeFilter[]",
    "status": "paymentStatusFilter",
    "supplier": "companyFilter",
}

# Document types as listed in the documentTypeFilter enum. The API has no
# endpoint for this list; it can be extended through GMI_DOCUMENT_TYPES.
DEFAULT_DOCUMENT_TYPES = ("INCOMING_INVOICE", "RECEIPT", "SALES_INVOICE", "CREDIT_NOTE")
PAYMENT_STATUSES = frozenset({"paid", "not_paid", "unknown", "all"})

# Points that still need to be checked against the official documentation.
UNCONFIRMED = (
    "detail/download query parameter for file content",
    "fileContent encoding (assumed base64)",
    "document record field names for amounts, dates and supplier",
)

# Hard cap for a JSON response that carries a base64 encoded file.
JSON_OVERHEAD_BYTES = 1024 * 1024


def _pick(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        value: Any = source
        for part in name.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return value
    return None


def _sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    head = data[:256].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        return "application/xml"
    return None


@dataclass(slots=True)
class GmiHttpClient(ProviderHttpClient):
    """Provider client with the User-Agent GetMyInvoices asks for."""

    user_agent: str = DEFAULT_PRODUCT

    def _headers(self, accept: str) -> dict[str, str]:
        headers = ProviderHttpClient._headers(self, accept)
        headers["User-Agent"] = self.user_agent
        return headers


@dataclass(slots=True)
class GmiProvider:
    runtime: RuntimeConfig
    http: ProviderHttpClient
    account_scope: str
    scope_param: str | None
    account_path: str
    list_path: str
    detail_template: str
    download_template: str
    detail_query: dict[str, str]
    download_query: dict[str, str]
    file_content_field: str
    param_map: dict[str, str]
    document_types: tuple[str, ...]
    require_read_only_key: bool
    downloads: DownloadStore
    public_base_url: str | None
    _verified: bool = field(default=False)
    _verify_lock: Lock = field(default_factory=Lock)

    @classmethod
    def from_env(cls, runtime: RuntimeConfig, downloads: DownloadStore) -> "GmiProvider":
        auth_header = os.getenv("GMI_AUTH_HEADER", "X-API-KEY").strip()
        if auth_header.lower() not in {"authorization", "x-api-key"}:
            raise RuntimeError("GMI_AUTH_HEADER must be Authorization or X-API-Key")
        params = dict(DEFAULT_LIST_PARAMS)
        params.update(json_object("GMI_LIST_PARAM_MAP_JSON"))
        account_scope = required("GMI_ACCOUNT_SCOPE")
        product = os.getenv("GMI_USER_AGENT_PRODUCT", DEFAULT_PRODUCT).strip() or DEFAULT_PRODUCT
        types_raw = os.getenv("GMI_DOCUMENT_TYPES", "").strip()
        document_types = tuple(x.strip() for x in types_raw.split(",") if x.strip()) if types_raw else DEFAULT_DOCUMENT_TYPES
        return cls(
            runtime=runtime,
            http=GmiHttpClient(
                validate_provider_url(os.getenv("GMI_API_BASE_URL", "").strip() or DEFAULT_BASE_URL),
                required("GMI_API_TOKEN"),
                runtime.timeout_seconds,
                auth_header=auth_header,
                auth_scheme=os.getenv("GMI_AUTH_SCHEME", "").strip(),
                user_agent=f"{product} (GetMyInvoices account {account_scope})",
            ),
            account_scope=account_scope,
            scope_param=os.getenv("GMI_ACCOUNT_SCOPE_PARAM", "").strip() or None,
            account_path=os.getenv("GMI_ACCOUNT_PATH", "").strip() or "/account",
            list_path=os.getenv("GMI_LIST_PATH", "").strip() or "/documents",
            detail_template=os.getenv("GMI_DETAIL_PATH_TEMPLATE", "").strip() or "/documents/{document_id}",
            download_template=os.getenv("GMI_DOWNLOAD_PATH_TEMPLATE", "").strip() or "/documents/{document_id}",
            detail_query=json_object("GMI_DETAIL_QUERY_JSON"),
            download_query=json_object("GMI_DOWNLOAD_QUERY_JSON"),
            file_content_field=os.getenv("GMI_FILE_CONTENT_FIELD", "").strip() or "fileContent",
            param_map=params,
            document_types=document_types,
            require_read_only_key=os.getenv("GMI_REQUIRE_READ_ONLY_KEY", "true").strip().lower() != "false",
            downloads=downloads,
            public_base_url=os.getenv("MCP_PUBLIC_BASE_URL", "").strip() or None,
        )

    # ------------------------------------------------------------------ helpers

    def _path(self, template: str, document_id: str) -> str:
        if "{document_id}" not in template:
            raise ServiceError("API_OPERATION_UNAVAILABLE", "Provider endpoint template is not configured")
        return template.replace("{document_id}", identifier(document_id, "document_id"))

    def _scope_query(self) -> dict[str, object]:
        return {self.scope_param: self.account_scope} if self.scope_param else {}

    def _assert_scope(self, payload: object) -> None:
        if isinstance(payload, dict):
            observed = _pick(payload, "accountId", "account_id", "account.id")
            if observed is not None and str(observed) != self.account_scope:
                raise ServiceError("ACCOUNT_SCOPE_VIOLATION", "Provider response is outside the configured account scope")

    def _get_large_json(self, path: str, query: dict[str, object]) -> object:
        """GET a JSON document that may carry a base64 encoded file."""
        limit = self.runtime.max_download_bytes * 4 // 3 + JSON_OVERHEAD_BYTES
        response = self.http.request(path, query)
        try:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ServiceError("DOWNLOAD_TOO_LARGE", "Provider response exceeds the configured size limit")
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned invalid JSON") from exc
        finally:
            response.close()

    def _account(self) -> dict[str, Any]:
        payload = self.http.get_json(self.account_path, self._scope_query())
        if not isinstance(payload, dict):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned invalid account data")
        observed = _pick(payload, "accountId", "account_id")
        if observed is None or str(observed) != self.account_scope:
            raise ServiceError("ACCOUNT_SCOPE_VIOLATION", "API key does not belong to the configured account")
        key_type = _pick(payload, "apiKeyType")
        if self.require_read_only_key and key_type != READ_ONLY_KEY_TYPE:
            raise ServiceError("API_KEY_NOT_READ_ONLY", "The configured API key is not of type READ_ONLY")
        return payload

    def _ensure_verified(self) -> None:
        """Checks account and key type once before the first data access."""
        if self._verified:
            return
        with self._verify_lock:
            if not self._verified:
                self._account()
                self._verified = True

    @staticmethod
    def normalize_document(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid document object")
        document_id = _pick(raw, "documentUid", "id", "documentId", "document_id")
        if document_id is None:
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider document has no identifier")
        # Field names besides documentUid/filename are UNCONFIRMED; the
        # fallbacks never invent values, missing fields stay None.
        return {
            "document_id": str(document_id),
            "document_type": _pick(raw, "documentType", "document_type", "type"),
            "document_date": _pick(raw, "documentDate", "document_date", "date"),
            "service_period_from": _pick(raw, "servicePeriodFrom", "service_period_from"),
            "service_period_to": _pick(raw, "servicePeriodTo", "service_period_to"),
            "supplier_name": _pick(raw, "companyName", "supplierName", "supplier_name", "supplier.name"),
            "invoice_number": _pick(raw, "documentNumber", "invoiceNumber", "invoice_number", "number"),
            "currency": _pick(raw, "currency", "amounts.currency"),
            "net_amount": _pick(raw, "netAmount", "net_amount", "amounts.net"),
            "tax_amount": _pick(raw, "taxAmount", "vatAmount", "tax_amount", "amounts.tax"),
            "gross_amount": _pick(raw, "grossAmount", "gross_amount", "amounts.gross"),
            "tax_rates": _pick(raw, "taxRates", "tax_rates") or [],
            "payment_status": _pick(raw, "paymentStatus", "payment_status"),
            "source_reference": _pick(raw, "sourceReference", "source_reference") or str(document_id),
            "file_name": _pick(raw, "filename", "fileName", "file_name"),
            "mime_type": _pick(raw, "mimeType", "mime_type"),
            "updated_at": _pick(raw, "updatedAt", "updated_at", "modifiedAt"),
            "raw_fields_omitted": True,
            "field_mapping_verified": False,
        }

    # -------------------------------------------------------------------- tools

    def health_check(self, _: dict[str, object]) -> dict[str, object]:
        payload = self._account()
        self._verified = True
        return {
            "reachable": True,
            "authenticated": True,
            "account_scope": self.account_scope,
            "api_key_type": _pick(payload, "apiKeyType"),
            "api_version": "v3",
            "read_only": True,
            "unconfirmed": list(UNCONFIRMED),
        }

    def list_documents(self, args: dict[str, object]) -> dict[str, object]:
        start, end = date_range(args.get("date_from"), args.get("date_to"), self.runtime.max_date_range_days)
        size = page_size(args.get("page_size"), self.runtime.max_page_size)
        page = self._page(args.get("cursor"))
        values: dict[str, object] = {"date_from": start, "date_to": end, "page_size": size, "cursor": page}
        if args.get("document_type") not in (None, ""):
            document_type = identifier(args.get("document_type"), "document_type")
            values["document_type"] = [document_type]
        if args.get("status") not in (None, ""):
            status = str(args.get("status"))
            if status not in PAYMENT_STATUSES:
                raise ServiceError("VALIDATION_FAILED", "status must be paid, not_paid, unknown or all")
            values["status"] = status
        if args.get("supplier") not in (None, ""):
            values["supplier"] = identifier(args.get("supplier"), "supplier")
        query: dict[str, object] = self._scope_query()
        for key, value in values.items():
            mapped = self.param_map.get(key)
            if not mapped:
                raise ServiceError("API_OPERATION_UNAVAILABLE", f"Provider parameter mapping is missing for {key}")
            query[mapped] = value
        self._ensure_verified()
        payload = self.http.get_json(self.list_path, query)
        self._assert_scope(payload)
        if isinstance(payload, list):
            items, max_pages = payload, None
        elif isinstance(payload, dict):
            items = _pick(payload, "records", "documents", "items", "data")
            max_pages = _pick(payload, "maxPages")
        else:
            items, max_pages = None, None
        if not isinstance(items, list):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid document list")
        for item in items:
            self._assert_scope(item)
        has_more = bool(items) and isinstance(max_pages, int) and page < max_pages
        return {
            "documents": [self.normalize_document(item) for item in items],
            "next_cursor": str(page + 1) if has_more else None,
            "total_count": _pick(payload, "totalCount") if isinstance(payload, dict) else None,
            "account_scope": self.account_scope,
        }

    @staticmethod
    def _page(cursor: object) -> int:
        if cursor in (None, ""):
            return 1
        try:
            page = int(str(cursor))
        except ValueError as exc:
            raise ServiceError("VALIDATION_FAILED", "cursor is invalid") from exc
        if page < 1 or page > 10000:
            raise ServiceError("VALIDATION_FAILED", "cursor is invalid")
        return page

    def _document(self, document_id: str, template: str, extra: dict[str, str]) -> dict[str, Any]:
        self._ensure_verified()
        query = self._scope_query()
        query.update(extra)
        payload = self._get_large_json(self._path(template, document_id), query)
        self._assert_scope(payload)
        if isinstance(payload, dict) and isinstance(payload.get("document"), dict):
            payload = payload["document"]
        if not isinstance(payload, dict):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid document object")
        return payload

    def get_metadata(self, args: dict[str, object]) -> dict[str, object]:
        document_id = identifier(args.get("document_id"), "document_id")
        payload = self._document(document_id, self.detail_template, self.detail_query)
        return {"document": self.normalize_document(payload), "original_reference": document_id, "raw_fields_omitted": True}

    def download_document(self, args: dict[str, object]) -> dict[str, object]:
        document_id = identifier(args.get("document_id"), "document_id")
        extra = dict(self.download_query)
        if args.get("variant"):
            mapped = self.param_map.get("variant")
            if not mapped:
                raise ServiceError("API_OPERATION_UNAVAILABLE", "Provider parameter mapping is missing for variant")
            extra[mapped] = identifier(args["variant"], "variant")
        payload = self._document(document_id, self.download_template, extra)
        encoded = payload.get(self.file_content_field)
        if not isinstance(encoded, str) or not encoded:
            raise ServiceError("RESOURCE_NOT_FOUND", "Provider returned no file content for this document")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider file content is not valid base64") from exc
        mime = _sniff_mime(data)
        if mime is None:
            raise ServiceError("UNSUPPORTED_FILE_TYPE", "Provider returned an unsupported file type")
        name = _pick(payload, "filename", "fileName") or f"{document_id}.bin"
        item = self.downloads.save_bytes(data, str(name), mime)
        return item.metadata(self.public_base_url)

    def list_document_types(self, _: dict[str, object]) -> dict[str, object]:
        return {
            "document_types": list(self.document_types),
            "source": "documentTypeFilter enum of GET /documents (no provider endpoint)",
            "raw_fields_omitted": True,
        }
