from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from common.config import RuntimeConfig, required, validate_provider_url
from common.downloads import DownloadStore
from common.errors import ServiceError, validation_error
from common.http_client import ProviderHttpClient
from common.validation import cursor_page, date_range, identifier, page_size


BOOKKEEPING_TYPES = frozenset({"salesinvoice", "salescreditnote", "purchaseinvoice", "purchasecreditnote"})
DETAIL_ENDPOINTS = {
    "salesinvoice": "/v1/vouchers/{id}",
    "salescreditnote": "/v1/vouchers/{id}",
    "purchaseinvoice": "/v1/vouchers/{id}",
    "purchasecreditnote": "/v1/vouchers/{id}",
    "invoice": "/v1/invoices/{id}",
    "downpaymentinvoice": "/v1/down-payment-invoices/{id}",
    "creditnote": "/v1/credit-notes/{id}",
    "orderconfirmation": "/v1/order-confirmations/{id}",
    "quotation": "/v1/quotations/{id}",
    "deliverynote": "/v1/delivery-notes/{id}",
}
FILE_ENDPOINTS = {
    "invoice": "/v1/invoices/{id}/file",
    "downpaymentinvoice": "/v1/down-payment-invoices/{id}/file",
    "creditnote": "/v1/credit-notes/{id}/file",
    "orderconfirmation": "/v1/order-confirmations/{id}/file",
    "quotation": "/v1/quotations/{id}/file",
    "deliverynote": "/v1/delivery-notes/{id}/file",
}


def _first_not_none(*values: object) -> object:
    return next((value for value in values if value is not None), None)


def _voucher_type(value: object) -> str:
    if not isinstance(value, str) or value not in DETAIL_ENDPOINTS:
        raise validation_error("voucher_type is not supported")
    return value


def _minimal_profile(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ServiceError("PROVIDER_UNAVAILABLE", "Lexware returned an invalid profile")
    return {
        "organization_id": raw.get("organizationId"),
        "company_name": raw.get("companyName"),
        "features": raw.get("features") or [],
        "business_features": raw.get("businessFeatures") or [],
        "subscription_status": raw.get("subscriptionStatus"),
        "tax_type": raw.get("taxType"),
        "small_business": raw.get("smallBusiness"),
        "personal_profile_fields_omitted": True,
    }


def _normalize_voucher(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("id") is None:
        raise ServiceError("PROVIDER_UNAVAILABLE", "Lexware returned an invalid voucher")
    total = raw.get("totalPrice") if isinstance(raw.get("totalPrice"), dict) else {}
    items = raw.get("voucherItems") if isinstance(raw.get("voucherItems"), list) else []
    tax_rates = raw.get("taxAmounts") if isinstance(raw.get("taxAmounts"), list) else [
        {"tax_rate": item.get("taxRatePercent"), "tax_amount": item.get("taxAmount")}
        for item in items if isinstance(item, dict)
    ]
    shipping = raw.get("shippingConditions") if isinstance(raw.get("shippingConditions"), dict) else {}
    return {
        "organization_id": raw.get("organizationId"),
        "voucher_id": str(raw["id"]),
        "voucher_type": raw.get("voucherType") or raw.get("type"),
        "voucher_number": raw.get("voucherNumber"),
        "voucher_date": raw.get("voucherDate"),
        "service_period_from": raw.get("shippingDate") or shipping.get("shippingDate"),
        "service_period_to": None,
        "contact_id": raw.get("contactId"),
        "contact_name": raw.get("contactName"),
        "currency": raw.get("currency") or total.get("currency"),
        "net_amount": _first_not_none(raw.get("totalNetAmount"), total.get("totalNetAmount")),
        "tax_amount": _first_not_none(raw.get("totalTaxAmount"), total.get("totalTaxAmount")),
        "gross_amount": _first_not_none(raw.get("totalGrossAmount"), raw.get("totalAmount"), total.get("totalGrossAmount")),
        "tax_rates": tax_rates,
        "payment_status": raw.get("paymentStatus"),
        "voucher_status": raw.get("voucherStatus"),
        "source_reference": str(raw["id"]),
        "updated_at": raw.get("updatedDate"),
        "raw_fields_omitted": True,
    }


@dataclass(slots=True)
class LexwareProvider:
    runtime: RuntimeConfig
    http: ProviderHttpClient
    organization_scope: str
    downloads: DownloadStore
    public_base_url: str | None
    _scope_checked_at: float = field(default=0.0)

    @classmethod
    def from_env(cls, runtime: RuntimeConfig, downloads: DownloadStore) -> "LexwareProvider":
        return cls(
            runtime=runtime,
            http=ProviderHttpClient(validate_provider_url(os.getenv("LEXWARE_API_BASE_URL", "https://api.lexware.io")), required("LEXWARE_API_TOKEN"), runtime.timeout_seconds),
            organization_scope=identifier(required("LEXWARE_ORGANIZATION_SCOPE"), "LEXWARE_ORGANIZATION_SCOPE"),
            downloads=downloads,
            public_base_url=os.getenv("MCP_PUBLIC_BASE_URL", "").strip() or None,
        )

    def _assert_scope(self, payload: object, required_field: bool = False) -> None:
        if isinstance(payload, dict):
            observed = payload.get("organizationId")
            if observed is not None and str(observed) != self.organization_scope:
                raise ServiceError("ORGANIZATION_SCOPE_VIOLATION", "Lexware response is outside the configured organization scope")
            if required_field and observed is None:
                raise ServiceError("ORGANIZATION_SCOPE_VIOLATION", "Lexware response did not identify its organization")

    def _ensure_scope(self) -> dict[str, object]:
        if time.monotonic() - self._scope_checked_at < 300:
            return {"organizationId": self.organization_scope}
        raw = self.http.get_json("/v1/profile")
        self._assert_scope(raw, required_field=True)
        self._scope_checked_at = time.monotonic()
        return raw if isinstance(raw, dict) else {}

    def health_check(self, _: dict[str, object]) -> dict[str, object]:
        profile = self._ensure_scope()
        return {"reachable": True, "authenticated": True, "organization_scope": profile.get("organizationId"), "api_version": "v1", "read_only": True}

    def profile(self, _: dict[str, object]) -> dict[str, object]:
        raw = self.http.get_json("/v1/profile")
        self._assert_scope(raw, required_field=True)
        return _minimal_profile(raw)

    def _paged(self, path: str, query: dict[str, object], cursor: object, size_value: object) -> dict[str, object]:
        self._ensure_scope()
        page = cursor_page(cursor)
        size = page_size(size_value, self.runtime.max_page_size)
        query |= {"page": page, "size": size}
        raw = self.http.get_json(path, query)
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), list):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Lexware returned an invalid paged response")
        for item in raw["content"]:
            self._assert_scope(item)
        return {"items": raw["content"], "next_cursor": None if raw.get("last") else str(page + 1), "total_elements": raw.get("totalElements")}

    def list_vouchers(self, args: dict[str, object]) -> dict[str, object]:
        start, end = date_range(args.get("date_from"), args.get("date_to"), self.runtime.max_date_range_days)
        voucher_type = args.get("voucher_type") or "any"
        status = args.get("voucher_status") or "any"
        if not isinstance(voucher_type, str) or not isinstance(status, str):
            raise validation_error("voucher_type and voucher_status must be strings")
        query: dict[str, object] = {"voucherType": voucher_type, "voucherStatus": status, "voucherDateFrom": start, "voucherDateTo": end}
        if args.get("contact_id"):
            query["contactId"] = identifier(args["contact_id"], "contact_id")
        page = self._paged("/v1/voucherlist", query, args.get("cursor"), args.get("page_size"))
        return {"vouchers": [_normalize_voucher(item) for item in page.pop("items")], **page}

    def get_voucher(self, args: dict[str, object]) -> dict[str, object]:
        self._ensure_scope()
        voucher_id = identifier(args.get("voucher_id"), "voucher_id")
        voucher_type = _voucher_type(args.get("voucher_type"))
        raw = self.http.get_json(DETAIL_ENDPOINTS[voucher_type].format(id=voucher_id))
        self._assert_scope(raw, required_field=True)
        result = _normalize_voucher(raw)
        if isinstance(raw, dict):
            result["positions"] = raw.get("voucherItems") or raw.get("lineItems") or []
        return result

    def list_contacts(self, args: dict[str, object]) -> dict[str, object]:
        query: dict[str, object] = {}
        if args.get("customer_number"):
            query["customerNumber"] = str(args["customer_number"])
        if args.get("vendor_number"):
            query["vendorNumber"] = str(args["vendor_number"])
        page = self._paged("/v1/contacts", query, args.get("cursor"), args.get("page_size"))
        contacts = []
        for raw in page.pop("items"):
            if not isinstance(raw, dict):
                continue
            company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
            person = raw.get("person") if isinstance(raw.get("person"), dict) else {}
            contacts.append({"contact_id": raw.get("id"), "organization_id": raw.get("organizationId"), "name": company.get("name") or " ".join(x for x in [person.get("firstName"), person.get("lastName")] if x), "roles": raw.get("roles") or {}, "archived": raw.get("archived"), "sensitive_fields_omitted": True})
        return {"contacts": contacts, **page}

    def get_contact(self, args: dict[str, object]) -> dict[str, object]:
        self._ensure_scope()
        contact_id = identifier(args.get("contact_id"), "contact_id")
        raw = self.http.get_json(f"/v1/contacts/{contact_id}")
        self._assert_scope(raw, required_field=True)
        if not isinstance(raw, dict):
            raise ServiceError("PROVIDER_UNAVAILABLE", "Lexware returned an invalid contact")
        allowed = {key: raw.get(key) for key in ("id", "organizationId", "version", "roles", "company", "person", "addresses", "archived")}
        if isinstance(allowed.get("company"), dict):
            contact_people = allowed["company"].get("contactPersons")
            allowed["company"] = {
                "name": allowed["company"].get("name"),
                "contactPersons": [
                    {key: person.get(key) for key in ("salutation", "firstName", "lastName")}
                    for person in contact_people or [] if isinstance(person, dict)
                ],
            }
        allowed["sensitive_fields_omitted"] = True
        return allowed

    def list_invoices(self, args: dict[str, object]) -> dict[str, object]:
        forwarded = dict(args)
        forwarded["voucher_type"] = "invoice"
        return self.list_vouchers(forwarded)

    def get_invoice(self, args: dict[str, object]) -> dict[str, object]:
        return self.get_voucher({"voucher_id": args.get("invoice_id"), "voucher_type": "invoice"})

    def list_credit_notes(self, args: dict[str, object]) -> dict[str, object]:
        forwarded = dict(args)
        forwarded["voucher_type"] = "creditnote"
        return self.list_vouchers(forwarded)

    def list_recurring_templates(self, args: dict[str, object]) -> dict[str, object]:
        page = self._paged("/v1/recurring-templates", {}, args.get("cursor"), args.get("page_size"))
        templates = []
        for raw in page.pop("items"):
            if isinstance(raw, dict):
                templates.append({key: raw.get(key) for key in ("id", "organizationId", "voucherType", "voucherStatus", "voucherNumber", "archived", "updatedDate", "retroactiveInvoice")})
        return {"recurring_templates": templates, **page, "raw_fields_omitted": True}

    def download_voucher(self, args: dict[str, object]) -> dict[str, object]:
        self._ensure_scope()
        voucher_id = identifier(args.get("voucher_id"), "voucher_id")
        voucher_type = _voucher_type(args.get("voucher_type"))
        accept = args.get("variant") or "*/*"
        if accept not in {"*/*", "application/pdf", "application/xml", "image/jpeg", "image/png"}:
            raise validation_error("variant must be an allowed MIME type")
        if voucher_type in BOOKKEEPING_TYPES:
            raw = self.http.get_json(DETAIL_ENDPOINTS[voucher_type].format(id=voucher_id))
            self._assert_scope(raw, required_field=True)
            files = raw.get("files") if isinstance(raw, dict) else None
            selected = args.get("file_id")
            if selected is None:
                if not isinstance(files, list) or len(files) != 1:
                    raise validation_error("file_id is required unless the voucher has exactly one file")
                selected = files[0].get("id") if isinstance(files[0], dict) else files[0]
            file_id = identifier(selected, "file_id")
            if isinstance(files, list):
                allowed_ids = {str(item.get("id") if isinstance(item, dict) else item) for item in files}
                if file_id not in allowed_ids:
                    raise ServiceError("ORGANIZATION_SCOPE_VIOLATION", "File is not attached to the scoped voucher")
            path = f"/v1/files/{file_id}"
        else:
            path = FILE_ENDPOINTS.get(voucher_type, "")
            if not path:
                raise ServiceError("API_OPERATION_UNAVAILABLE", "No documented file endpoint is enabled for this voucher type")
            raw = self.http.get_json(DETAIL_ENDPOINTS[voucher_type].format(id=voucher_id))
            self._assert_scope(raw, required_field=True)
            path = path.format(id=voucher_id)
        response = self.http.request(path, accept=accept)
        item = self.downloads.save_response(response, f"{voucher_id}.bin")
        return item.metadata(self.public_base_url)

    def capabilities(self, _: dict[str, object]) -> dict[str, object]:
        return {
            "read_only": True,
            "api_base": "https://api.lexware.io/v1",
            "implemented": ["profile", "voucherlist", "voucher_details", "voucher_file_download", "contacts", "invoice_details", "credit_note_list", "recurring_templates"],
            "unavailable": {"list_payments": "The official API documents GET /v1/payments/{voucherId}, not a date-filtered payment collection.", "write_operations": "Disabled by design."},
        }
