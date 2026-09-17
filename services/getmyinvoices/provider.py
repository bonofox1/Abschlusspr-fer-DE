from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from common.config import RuntimeConfig, json_object, required, validate_provider_url
from common.downloads import DownloadStore
from common.errors import ServiceError
from common.http_client import ProviderHttpClient
from common.validation import date_range, identifier, page_size


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


@dataclass(slots=True)
class GmiProvider:
    runtime: RuntimeConfig
    http: ProviderHttpClient
    account_scope: str
    scope_param: str
    list_path: str
    detail_template: str
    download_template: str
    types_path: str | None
    param_map: dict[str, str]
    downloads: DownloadStore
    public_base_url: str | None

    @classmethod
    def from_env(cls, runtime: RuntimeConfig, downloads: DownloadStore) -> "GmiProvider":
        auth_header = os.getenv("GMI_AUTH_HEADER", "Authorization").strip()
        if auth_header.lower() not in {"authorization", "x-api-key"}:
            raise RuntimeError("GMI_AUTH_HEADER must be Authorization or X-API-Key")
        params = json_object("GMI_LIST_PARAM_MAP_JSON")
        required_params = {"date_from", "date_to", "page_size", "cursor"}
        if not required_params.issubset(params):
            raise RuntimeError("GMI_LIST_PARAM_MAP_JSON is missing required mappings")
        return cls(
            runtime=runtime,
            http=ProviderHttpClient(
                validate_provider_url(required("GMI_API_BASE_URL")),
                required("GMI_API_TOKEN"),
                runtime.timeout_seconds,
                auth_header=auth_header,
                auth_scheme=os.getenv("GMI_AUTH_SCHEME", "Bearer").strip(),
            ),
            account_scope=required("GMI_ACCOUNT_SCOPE"),
            scope_param=required("GMI_ACCOUNT_SCOPE_PARAM"),
            list_path=required("GMI_LIST_PATH"),
            detail_template=required("GMI_DETAIL_PATH_TEMPLATE"),
            download_template=required("GMI_DOWNLOAD_PATH_TEMPLATE"),
            types_path=os.getenv("GMI_TYPES_PATH", "").strip() or None,
            param_map=params,
            downloads=downloads,
            public_base_url=os.getenv("MCP_PUBLIC_BASE_URL", "").strip() or None,
        )

    def _path(self, template: str, document_id: str) -> str:
        if "{document_id}" not in template:
            raise ServiceError("API_OPERATION_UNAVAILABLE", "Provider endpoint template is not configured")
        return template.replace("{document_id}", identifier(document_id, "document_id"))

    def _scope_query(self) -> dict[str, str]:
        return {self.scope_param: self.account_scope}

    def _assert_scope(self, payload: object) -> None:
        if isinstance(payload, dict):
            observed = _pick(payload, "accountId", "account_id", "account.id")
            if observed is not None and str(observed) != self.account_scope:
                raise ServiceError("ACCOUNT_SCOPE_VIOLATION", "Provider response is outside the configured account scope")

    @staticmethod
    def normalize_document(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid document object")
        document_id = _pick(raw, "id", "documentId", "document_id")
        if document_id is None:
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider document has no identifier")
        return {
            "document_id": str(document_id),
            "document_type": _pick(raw, "documentType", "document_type", "type"),
            "document_date": _pick(raw, "documentDate", "document_date", "date"),
            "service_period_from": _pick(raw, "servicePeriodFrom", "service_period_from"),
            "service_period_to": _pick(raw, "servicePeriodTo", "service_period_to"),
            "supplier_name": _pick(raw, "supplierName", "supplier_name", "supplier.name"),
            "invoice_number": _pick(raw, "invoiceNumber", "invoice_number", "number"),
            "currency": _pick(raw, "currency", "amounts.currency"),
            "net_amount": _pick(raw, "netAmount", "net_amount", "amounts.net"),
            "tax_amount": _pick(raw, "taxAmount", "tax_amount", "amounts.tax"),
            "gross_amount": _pick(raw, "grossAmount", "gross_amount", "amounts.gross"),
            "tax_rates": _pick(raw, "taxRates", "tax_rates") or [],
            "payment_status": _pick(raw, "paymentStatus", "payment_status"),
            "source_reference": _pick(raw, "sourceReference", "source_reference") or str(document_id),
            "file_name": _pick(raw, "fileName", "file_name"),
            "mime_type": _pick(raw, "mimeType", "mime_type"),
            "updated_at": _pick(raw, "updatedAt", "updated_at", "modifiedAt"),
            "raw_fields_omitted": True,
        }

    def health_check(self, _: dict[str, object]) -> dict[str, object]:
        query = self._scope_query()
        query[self.param_map["date_from"]] = "1970-01-01"
        query[self.param_map["date_to"]] = "1970-01-01"
        query[self.param_map["page_size"]] = "1"
        payload = self.http.get_json(self.list_path, query)
        self._assert_scope(payload)
        return {"reachable": True, "authenticated": True, "account_scope": self.account_scope, "api_version": "v3", "read_only": True}

    def list_documents(self, args: dict[str, object]) -> dict[str, object]:
        start, end = date_range(args.get("date_from"), args.get("date_to"), self.runtime.max_date_range_days)
        size = page_size(args.get("page_size"), self.runtime.max_page_size)
        query: dict[str, object] = self._scope_query()
        for key, value in {"date_from": start, "date_to": end, "page_size": size, "cursor": args.get("cursor"), "document_type": args.get("document_type"), "status": args.get("status"), "supplier": args.get("supplier")}.items():
            if value not in (None, ""):
                mapped = self.param_map.get(key)
                if not mapped:
                    raise ServiceError("API_OPERATION_UNAVAILABLE", f"Provider parameter mapping is missing for {key}")
                query[mapped] = value
        payload = self.http.get_json(self.list_path, query)
        self._assert_scope(payload)
        if isinstance(payload, list):
            items, next_cursor = payload, None
        elif isinstance(payload, dict):
            items = _pick(payload, "documents", "items", "content", "data")
            next_cursor = _pick(payload, "nextCursor", "next_cursor", "pagination.nextCursor")
        else:
            items, next_cursor = None, None
        if not isinstance(items, list):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid document list")
        for item in items:
            self._assert_scope(item)
        return {"documents": [self.normalize_document(item) for item in items], "next_cursor": next_cursor, "account_scope": self.account_scope}

    def get_metadata(self, args: dict[str, object]) -> dict[str, object]:
        document_id = identifier(args.get("document_id"), "document_id")
        payload = self.http.get_json(self._path(self.detail_template, document_id), self._scope_query())
        self._assert_scope(payload)
        return {"document": self.normalize_document(payload), "original_reference": document_id, "raw_fields_omitted": True}

    def download_document(self, args: dict[str, object]) -> dict[str, object]:
        document_id = identifier(args.get("document_id"), "document_id")
        query: dict[str, object] = self._scope_query()
        if args.get("variant"):
            mapped = self.param_map.get("variant")
            if not mapped:
                raise ServiceError("API_OPERATION_UNAVAILABLE", "Provider parameter mapping is missing for variant")
            query[mapped] = identifier(args["variant"], "variant")
        response = self.http.request(self._path(self.download_template, document_id), query, "application/pdf, application/xml, text/xml, image/jpeg, image/png")
        item = self.downloads.save_response(response, f"{document_id}.bin")
        return item.metadata(self.public_base_url)

    def list_document_types(self, _: dict[str, object]) -> dict[str, object]:
        if not self.types_path:
            raise ServiceError("API_OPERATION_UNAVAILABLE", "The document types endpoint is not configured")
        payload = self.http.get_json(self.types_path, self._scope_query())
        self._assert_scope(payload)
        return {"document_types": payload, "raw_fields_omitted": True}
