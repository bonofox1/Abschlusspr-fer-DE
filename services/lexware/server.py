from __future__ import annotations

import logging
import os

from common.config import RuntimeConfig
from common.downloads import DownloadStore
from common.mcp_server import McpApplication, Tool, serve
from .provider import DETAIL_ENDPOINTS, LexwareProvider


BASE = {"type": "object", "additionalProperties": False}
PAGING = {"page_size": {"type": "integer", "minimum": 1, "maximum": 250}, "cursor": {"type": "string"}}
DATES = {"date_from": {"type": "string", "format": "date"}, "date_to": {"type": "string", "format": "date"}}


def schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    result = BASE | {"properties": properties}
    if required:
        result["required"] = required
    return result


def build_application() -> McpApplication:
    runtime = RuntimeConfig.from_env()
    downloads = DownloadStore(runtime.max_download_bytes, runtime.download_ttl_seconds)
    provider = LexwareProvider.from_env(runtime, downloads)
    list_schema = schema(DATES | PAGING | {"voucher_type": {"type": "string"}, "voucher_status": {"type": "string"}, "contact_id": {"type": "string"}}, ["date_from", "date_to"])
    tools = [
        Tool("lexware_health_check", "Checks Lexware reachability, authentication and fixed organization scope without exposing business data.", schema({}), provider.health_check),
        Tool("lexware_get_profile_or_organization", "Returns a minimized organization profile; personal creator fields are omitted.", schema({}), provider.profile),
        Tool("lexware_list_vouchers", "Lists normalized voucher metadata for a bounded period using the documented voucherlist endpoint.", list_schema, provider.list_vouchers),
        Tool("lexware_get_voucher", "Reads one voucher from its documented type-specific endpoint.", schema({"voucher_id": {"type": "string"}, "voucher_type": {"type": "string", "enum": sorted(DETAIL_ENDPOINTS)}}, ["voucher_id", "voucher_type"]), provider.get_voucher),
        Tool("lexware_download_voucher_file", "Downloads one allow-listed voucher file into short-lived protected server storage and returns an opaque reference.", schema({"voucher_id": {"type": "string"}, "voucher_type": {"type": "string"}, "file_id": {"type": "string"}, "variant": {"type": "string", "enum": ["*/*", "application/pdf", "application/xml", "image/jpeg", "image/png"]}}, ["voucher_id", "voucher_type"]), provider.download_voucher),
        Tool("lexware_list_contacts", "Lists minimized customer or supplier contact metadata with pagination.", schema(PAGING | {"customer_number": {"type": "string"}, "vendor_number": {"type": "string"}}), provider.list_contacts),
        Tool("lexware_get_contact", "Reads one contact while omitting tax numbers, bank data and communication details.", schema({"contact_id": {"type": "string"}}, ["contact_id"]), provider.get_contact),
        Tool("lexware_list_invoices", "Lists outgoing invoices for a bounded period through the documented voucherlist endpoint.", schema(DATES | PAGING | {"voucher_status": {"type": "string"}, "contact_id": {"type": "string"}}, ["date_from", "date_to"]), provider.list_invoices),
        Tool("lexware_get_invoice", "Reads one outgoing invoice including normalized totals and positions.", schema({"invoice_id": {"type": "string"}}, ["invoice_id"]), provider.get_invoice),
        Tool("lexware_list_credit_notes", "Lists credit notes for a bounded period through the documented voucherlist endpoint.", schema(DATES | PAGING | {"voucher_status": {"type": "string"}, "contact_id": {"type": "string"}}, ["date_from", "date_to"]), provider.list_credit_notes),
        Tool("lexware_list_recurring_templates", "Lists recurring templates with minimized metadata and pagination.", schema(PAGING), provider.list_recurring_templates),
        Tool("lexware_get_api_capabilities", "Returns the implemented, documented read-only Lexware operations and explicit omissions.", schema({}), provider.capabilities),
    ]
    return McpApplication("lexware-office-readonly", "1.0.0", runtime, tools, downloads)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    serve(build_application())


if __name__ == "__main__":
    main()
