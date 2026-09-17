from __future__ import annotations

import logging
import os

from common.config import RuntimeConfig
from common.downloads import DownloadStore
from common.mcp_server import McpApplication, Tool, serve
from .provider import GmiProvider


OBJECT = {"type": "object", "additionalProperties": False}


def build_application() -> McpApplication:
    runtime = RuntimeConfig.from_env()
    downloads = DownloadStore(runtime.max_download_bytes, runtime.download_ttl_seconds)
    provider = GmiProvider.from_env(runtime, downloads)
    tools = [
        Tool("gmi_health_check", "Checks provider reachability, authentication and the fixed account scope without returning document content.", OBJECT | {"properties": {}}, provider.health_check),
        Tool("gmi_list_documents", "Lists and normalizes documents in a bounded date range. This tool never changes provider data.", OBJECT | {"properties": {"date_from": {"type": "string", "format": "date"}, "date_to": {"type": "string", "format": "date"}, "document_type": {"type": "string"}, "status": {"type": "string"}, "supplier": {"type": "string"}, "page_size": {"type": "integer", "minimum": 1}, "cursor": {"type": "string"}}, "required": ["date_from", "date_to"]}, provider.list_documents),
        Tool("gmi_get_document_metadata", "Returns normalized metadata for one document in the configured account scope.", OBJECT | {"properties": {"document_id": {"type": "string"}}, "required": ["document_id"]}, provider.get_metadata),
        Tool("gmi_download_document", "Downloads one allow-listed document variant into short-lived protected server storage and returns an opaque reference.", OBJECT | {"properties": {"document_id": {"type": "string"}, "variant": {"type": "string"}}, "required": ["document_id"]}, provider.download_document),
        Tool("gmi_list_document_types", "Returns provider document types when the documented endpoint has been configured.", OBJECT | {"properties": {}}, provider.list_document_types),
    ]
    return McpApplication("getmyinvoices-readonly", "1.0.0", runtime, tools, downloads)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    serve(build_application())


if __name__ == "__main__":
    main()

