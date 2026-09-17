from __future__ import annotations

import re
from datetime import date

from .errors import ServiceError, validation_error


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise validation_error(f"{name} is invalid")
    return value


def date_range(date_from: object, date_to: object, max_days: int) -> tuple[str, str]:
    try:
        start = date.fromisoformat(str(date_from))
        end = date.fromisoformat(str(date_to))
    except ValueError as exc:
        raise ServiceError("INVALID_DATE_RANGE", "Dates must use YYYY-MM-DD") from exc
    if start > end or (end - start).days > max_days:
        raise ServiceError("INVALID_DATE_RANGE", "Date range is reversed or exceeds the configured maximum")
    return start.isoformat(), end.isoformat()


def page_size(value: object, maximum: int) -> int:
    if value is None:
        return min(100, maximum)
    if isinstance(value, bool):
        raise validation_error("page_size must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise validation_error("page_size must be an integer") from exc
    if number < 1 or number > maximum:
        raise validation_error(f"page_size must be between 1 and {maximum}")
    return number


def cursor_page(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        page = int(str(value))
    except ValueError as exc:
        raise validation_error("cursor is invalid") from exc
    if page < 0 or page > 10000:
        raise validation_error("cursor is invalid")
    return page

