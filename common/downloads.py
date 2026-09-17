from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .errors import ServiceError


ALLOWED_MIME_TYPES = frozenset({"application/pdf", "application/xml", "text/xml", "image/jpeg", "image/png"})


@dataclass(frozen=True, slots=True)
class StoredDownload:
    reference: str
    path: Path
    file_name: str
    mime_type: str
    size: int
    sha256: str
    expires_at: float

    def metadata(self, public_base_url: str | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "file_reference": self.reference,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "expires_at_epoch": int(self.expires_at),
        }
        if public_base_url:
            result["download_url"] = f"{public_base_url.rstrip('/')}/downloads/{self.reference}"
        return result


class DownloadStore:
    def __init__(self, max_bytes: int, ttl_seconds: int, directory: str | None = None):
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.directory = Path(directory or tempfile.mkdtemp(prefix="mcp-downloads-"))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._items: dict[str, StoredDownload] = {}
        self._lock = Lock()

    def save_response(self, response, fallback_name: str) -> StoredDownload:
        mime = response.headers.get_content_type().lower()
        if mime not in ALLOWED_MIME_TYPES:
            response.close()
            raise ServiceError("UNSUPPORTED_FILE_TYPE", "Provider returned an unsupported file type", False, getattr(response, "status", None))
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                response.close()
                raise ServiceError("PROVIDER_UNAVAILABLE", "Provider returned an invalid file size") from exc
            if declared_size > self.max_bytes:
                response.close()
                raise ServiceError("DOWNLOAD_TOO_LARGE", "Download exceeds the configured size limit")
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.IGNORECASE)
        candidate = match.group(1) if match else fallback_name
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(candidate))[:160] or "document"
        reference = uuid4().hex
        target = self.directory / reference
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("xb") as handle:
                os.chmod(target, 0o600)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ServiceError("DOWNLOAD_TOO_LARGE", "Download exceeds the configured size limit")
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        item = StoredDownload(reference, target, name, mime, size, digest.hexdigest(), time.time() + self.ttl_seconds)
        with self._lock:
            self._cleanup_locked()
            self._items[reference] = item
        return item

    def get(self, reference: str) -> StoredDownload | None:
        with self._lock:
            self._cleanup_locked()
            return self._items.get(reference)

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [key for key, item in self._items.items() if item.expires_at <= now]
        for key in expired:
            self._items.pop(key).path.unlink(missing_ok=True)
