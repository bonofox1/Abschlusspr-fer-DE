from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required configuration: {name}")
    return value


def positive_int(name: str, default: int, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        raise RuntimeError(f"{name} is outside the allowed range")
    return value


def json_object(name: str, default: dict[str, str] | None = None) -> dict[str, str]:
    raw = os.getenv(name)
    if not raw:
        return default or {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise RuntimeError(f"{name} must be a JSON object with string keys and values")
    return value


def validate_provider_url(value: str) -> str:
    parsed = urlparse(value)
    allow_insecure = os.getenv("ALLOW_INSECURE_PROVIDER_URLS", "").lower() == "true"
    if parsed.scheme != "https" and not (allow_insecure and parsed.scheme == "http"):
        raise RuntimeError("Provider base URLs must use HTTPS")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Provider base URL is invalid")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    inbound_token: str
    timeout_seconds: int
    max_download_bytes: int
    max_page_size: int
    max_date_range_days: int
    download_ttl_seconds: int
    allowed_hosts: frozenset[str]

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        hosts = frozenset(x.strip().lower() for x in os.getenv("MCP_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x.strip())
        return cls(
            inbound_token=required("MCP_SERVER_AUTH"),
            timeout_seconds=positive_int("REQUEST_TIMEOUT_SECONDS", 20, 120),
            max_download_bytes=positive_int("MAX_DOWNLOAD_BYTES", 20 * 1024 * 1024, 100 * 1024 * 1024),
            max_page_size=positive_int("MAX_PAGE_SIZE", 250, 250),
            max_date_range_days=positive_int("MAX_DATE_RANGE_DAYS", 366, 3660),
            download_ttl_seconds=positive_int("DOWNLOAD_TTL_SECONDS", 900, 86400),
            allowed_hosts=hosts,
        )

