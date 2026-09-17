from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(slots=True)
class ServiceError(Exception):
    error_code: str
    message: str
    retryable: bool = False
    provider_status: int | None = None
    request_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "provider_status": self.provider_status,
            "request_id": self.request_id or str(uuid4()),
        }


def validation_error(message: str) -> ServiceError:
    return ServiceError("VALIDATION_FAILED", message)

