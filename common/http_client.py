from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .errors import ServiceError


UrlOpen = Callable[..., object]

# Header names (lower case) that carry provider credentials and must never be sent
# to a different origin.
CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class CredentialSafeRedirectHandler(HTTPRedirectHandler):
    """Follows redirects without leaking provider credentials.

    urllib's default handler copies every request header, including
    Authorization, to whatever host the redirect points at. This handler
    refuses redirects away from HTTPS and strips credential headers whenever
    the redirect leaves the original origin (e.g. a pre-signed storage URL).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        old_origin = _origin(req.full_url)
        new_origin = _origin(new_req.full_url)
        if new_origin[0] != "https" and old_origin[0] == "https":
            raise HTTPError(req.full_url, code, "Refusing redirect away from HTTPS", headers, fp)
        if new_origin != old_origin:
            for name in list(new_req.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new_req.headers[name]
        return new_req


_safe_opener = build_opener(CredentialSafeRedirectHandler)


def safe_urlopen(request: Request, timeout: float):
    return _safe_opener.open(request, timeout=timeout)


@dataclass(slots=True)
class ProviderHttpClient:
    base_url: str
    token: str
    timeout_seconds: int
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    max_attempts: int = 3
    opener: UrlOpen = safe_urlopen

    def _url(self, path: str, query: dict[str, object] | None = None) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ServiceError("VALIDATION_FAILED", "Provider path is invalid")
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if query:
            clean = {k: v for k, v in query.items() if v not in (None, "")}
            url += "?" + urlencode(clean, doseq=True)
        return url

    def _headers(self, accept: str) -> dict[str, str]:
        value = f"{self.auth_scheme} {self.token}".strip()
        return {self.auth_header: value, "Accept": accept, "User-Agent": "abschlusspruefer-de-mcp/1.0"}

    def request(self, path: str, query: dict[str, object] | None = None, accept: str = "application/json"):
        request_id = str(uuid4())
        for attempt in range(self.max_attempts):
            try:
                req = Request(self._url(path, query), headers=self._headers(accept), method="GET")
                return self.opener(req, timeout=self.timeout_seconds)
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if retryable and attempt + 1 < self.max_attempts:
                    retry_after = exc.headers.get("Retry-After")
                    delay = min(float(retry_after), 4.0) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(0.25 * (2**attempt) + random.random() * 0.1, 4.0)
                    time.sleep(delay)
                    continue
                mapping = {401: "AUTHENTICATION_FAILED", 403: "PERMISSION_DENIED", 404: "RESOURCE_NOT_FOUND", 429: "RATE_LIMITED"}
                raise ServiceError(mapping.get(exc.code, "PROVIDER_UNAVAILABLE"), "Provider request failed", retryable, exc.code, request_id) from exc
            except (URLError, TimeoutError) as exc:
                if attempt + 1 < self.max_attempts:
                    time.sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise ServiceError("PROVIDER_UNAVAILABLE", "Provider is unavailable", True, None, request_id) from exc

    def get_json(self, path: str, query: dict[str, object] | None = None) -> object:
        response = self.request(path, query)
        try:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ServiceError("PROVIDER_UNAVAILABLE", "Provider response exceeded the safe JSON limit")
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned invalid JSON") from exc
        finally:
            response.close()

