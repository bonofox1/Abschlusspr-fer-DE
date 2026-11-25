"""Read-only access to Lexware buchhalter / buchhaltung plus (desktop) data.

Lexware stores each company and fiscal year in plain SQLite files (".dbs").
Business records are packed into BLOB columns; the table ``__mapping`` inside
each file describes every field (name, type, source column, byte offset).
This module decodes records generically from that mapping, so it follows the
layout of the installed Lexware version instead of hard-coding offsets.

Files are always opened with ``mode=ro``. Nothing is ever written.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from common.errors import ServiceError, validation_error

DEFAULT_DATA_DIR = r"C:\ProgramData\Lexware\buchhalter\Daten"
COMPANY_DIR = re.compile(r"^\d{8}$")
YEAR_DIR = re.compile(r"^\d{4}$")
TABLE_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SKIP_FIELD = re.compile(r"^(old_|.*bReserve$)")
MAX_ROWS = 1000

_FIXED = {"int8": "<b", "uint8": "<B", "int16": "<h", "uint16": "<H", "int32": "<i", "uint32": "<I",
          "int64": "<q", "float32": "<f", "float64": "<d"}
_CHAR = re.compile(r"^char\((\d+)\)$")


def decode_date(value: object) -> object:
    """Lexware packs dates as (year << 16) | (month << 8) | day."""
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        return date(value >> 16, (value >> 8) & 0xFF, value & 0xFF).isoformat()
    except ValueError:
        return value


def encode_date(text: str) -> int:
    d = date.fromisoformat(text)
    return (d.year << 16) | (d.month << 8) | d.day


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: str
    column: str
    offset: int

    def read(self, row: sqlite3.Row) -> object:
        raw = row[self.column]
        if self.offset < 0:
            value: object = raw
            if isinstance(raw, bytes):
                value = _decode_bytes(raw, self.type, 0)
        else:
            if not isinstance(raw, bytes):
                return None
            value = _decode_bytes(raw, self.type, self.offset)
        if self.name.startswith("dat") and self.type in ("int32", "uint32"):
            return decode_date(value)
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, str):
            return value.strip()
        return value


def _decode_bytes(blob: bytes, typ: str, offset: int) -> object:
    if typ in _FIXED:
        fmt = _FIXED[typ]
        size = struct.calcsize(fmt)
        if offset + size > len(blob):
            return None
        return struct.unpack_from(fmt, blob, offset)[0]
    match = _CHAR.match(typ)
    if match:
        chunk = blob[offset:offset + int(match.group(1))]
        return chunk.split(b"\0", 1)[0].decode("cp1252", "replace")
    return None


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    source: str
    fields: tuple[FieldSpec, ...]

    def public_fields(self) -> list[str]:
        return [f.name for f in self.fields if not SKIP_FIELD.match(f.name)]


def _text(raw: bytes) -> str:
    """Lexware stores TEXT columns in cp1252; SQLite assumes UTF-8."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def _connect(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.text_factory = _text
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_mapping(conn: sqlite3.Connection) -> dict[str, TableSpec]:
    try:
        rows = conn.execute("SELECT vtable, cid, name, type, rtable, rname, roff FROM __mapping ORDER BY vtable, cid").fetchall()
    except sqlite3.DatabaseError:
        return {}
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["vtable"], []).append(row)
    specs: dict[str, TableSpec] = {}
    for vtable, items in grouped.items():
        public = vtable.removesuffix("_vtemp").lower()
        source = items[0]["rtable"]
        if not TABLE_NAME.match(source or ""):
            continue
        fields = tuple(FieldSpec(r["name"], (r["type"] or "").lower(), r["rname"], int(r["roff"] if r["roff"] is not None else -1))
                       for r in items if TABLE_NAME.match(r["rname"] or ""))
        specs[public] = TableSpec(public, source, fields)
    return specs


@dataclass(slots=True)
class LexwareDesktopReader:
    data_dir: Path
    max_rows: int = MAX_ROWS
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> "LexwareDesktopReader":
        path = Path(os.getenv("LXBH_DATA_DIR", DEFAULT_DATA_DIR))
        if not path.is_dir():
            raise RuntimeError(f"LXBH_DATA_DIR not found: {path}")
        return cls(path)

    # --- paths ----------------------------------------------------------
    def _company_dir(self, company_id: object) -> Path:
        try:
            number = int(str(company_id))
        except ValueError as exc:
            raise validation_error("company_id must be a number") from exc
        if not 0 <= number <= 99999999:
            raise validation_error("company_id is out of range")
        path = self.data_dir / f"{number:08d}"
        if not path.is_dir():
            raise ServiceError("NOT_FOUND", "Company not found")
        return path

    def _file(self, company_id: object, year: object | None, scope: str) -> Path:
        company = self._company_dir(company_id)
        if scope == "year":
            if year is None or not YEAR_DIR.match(str(year)):
                raise validation_error("year (YYYY) is required for this table")
            path = company / str(year) / "BHStdYear.dbs"
        elif scope == "accounts":
            path = company / "BHStdAccountList.dbs"
        else:
            raise validation_error("unknown scope")
        if not path.is_file():
            raise ServiceError("NOT_FOUND", "Fiscal year or file not found")
        return path

    def _open(self, path: Path) -> tuple[sqlite3.Connection, dict[str, TableSpec]]:
        conn = _connect(path)
        return conn, load_mapping(conn)

    # --- public operations ----------------------------------------------
    def list_companies(self, _: dict[str, object] | None = None) -> dict[str, object]:
        names = self._company_names()
        result = []
        for entry in sorted(self.data_dir.iterdir()):
            if entry.is_dir() and COMPANY_DIR.match(entry.name):
                number = int(entry.name)
                years = sorted(int(y.name) for y in entry.iterdir() if y.is_dir() and YEAR_DIR.match(y.name) and (y / "BHStdYear.dbs").is_file())
                result.append({"company_id": number, "name": names.get(number), "fiscal_years": years})
        return {"companies": result, "read_only": True}

    def _company_names(self) -> dict[int, str]:
        path = self.data_dir / "lxstdcompanies.dbs"
        if not path.is_file():
            return {}
        names: dict[int, str] = {}
        try:
            with closing(_connect(path)) as conn:
                for row in conn.execute("SELECT col0, col1, data1 FROM lx_firma WHERE col1 = 1"):
                    blob = row["data1"] if isinstance(row["data1"], bytes) else b""
                    parts = [p.strip() for p in re.split(r"[\x00-\x1f]+", blob.decode("cp1252", "replace")) if len(p.strip()) >= 3]
                    if parts:
                        names[int(row["col0"])] = parts[0]
        except sqlite3.DatabaseError:
            return {}
        return names

    def list_tables(self, args: dict[str, object]) -> dict[str, object]:
        out: list[dict[str, object]] = []
        scopes = [("accounts", None)]
        if args.get("year") is not None:
            scopes.append(("year", args.get("year")))
        for scope, year in scopes:
            conn, specs = self._open(self._file(args.get("company_id"), year, scope))
            with closing(conn):
                for spec in specs.values():
                    try:
                        count = conn.execute(f'SELECT COUNT(*) FROM "{spec.source}"').fetchone()[0]
                    except sqlite3.DatabaseError:
                        count = None
                    out.append({"table": spec.name, "scope": scope, "rows": count, "fields": spec.public_fields()})
        return {"tables": out}

    def _rows(self, path: Path, table: str, where: dict[str, object] | None = None,
              limit: int | None = None, offset: int = 0) -> tuple[list[dict[str, object]], int]:
        if not TABLE_NAME.match(table):
            raise validation_error("table is invalid")
        conn, specs = self._open(path)
        with closing(conn):
            spec = specs.get(table.lower())
            if spec is None:
                raise ServiceError("NOT_FOUND", f"Table {table} not found")
            public = set(spec.public_fields())
            filters = {}
            for key, value in (where or {}).items():
                if key not in public:
                    raise validation_error(f"Unknown filter field {key}")
                filters[key] = value
            columns = sorted({f.column for f in spec.fields})
            sql = "SELECT " + ", ".join(f'"{c}"' for c in columns) + f' FROM "{spec.source}"'
            result: list[dict[str, object]] = []
            matched = 0
            cap = limit if limit is not None else self.max_rows
            for raw in conn.execute(sql):
                record = {f.name: f.read(raw) for f in spec.fields if f.name in public}
                if any(not _matches(record.get(k), v) for k, v in filters.items()):
                    continue
                if matched >= offset and len(result) < cap:
                    result.append(record)
                matched += 1
            return result, matched

    def read_table(self, args: dict[str, object]) -> dict[str, object]:
        table = str(args.get("table", ""))
        scope = "year" if args.get("year") is not None else "accounts"
        limit, offset = _bounds(args, self.max_rows)
        rows, total = self._rows(self._file(args.get("company_id"), args.get("year"), scope), table,
                                 _where(args.get("where")), limit, offset)
        return {"table": table, "rows": rows, "total_matching": total, "offset": offset,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def accounts(self, args: dict[str, object]) -> dict[str, object]:
        rows, _ = self._rows(self._file(args.get("company_id"), None, "accounts"), "bh_konto", limit=self.max_rows * 5)
        only_used = bool(args.get("only_booked"))
        result = [{"account": r.get("lNr"), "name": r.get("szName"), "booked": bool(r.get("fBebucht")),
                   "locked": bool(r.get("bGesperrt")), "datev_number": r.get("lNr_Datev")}
                  for r in rows if not only_used or r.get("fBebucht")]
        return {"accounts": result, "count": len(result)}

    def _account_names(self, company_id: object) -> dict[int, str]:
        try:
            rows, _ = self._rows(self._file(company_id, None, "accounts"), "bh_konto", limit=self.max_rows * 5)
        except ServiceError:
            return {}
        return {int(r["lNr"]): r.get("szName") for r in rows if isinstance(r.get("lNr"), int)}

    def journal(self, args: dict[str, object]) -> dict[str, object]:
        source = str(args.get("source") or "both")
        tables = {"journal": ["bh_jour4"], "stapel": ["bh_stap4"], "both": ["bh_jour4", "bh_stap4"]}.get(source)
        if tables is None:
            raise validation_error("source must be journal, stapel or both")
        date_from, date_to = args.get("date_from"), args.get("date_to")
        for value in (date_from, date_to):
            if value is not None:
                try:
                    date.fromisoformat(str(value))
                except ValueError as exc:
                    raise validation_error("dates must use YYYY-MM-DD") from exc
        account = args.get("account")
        names = self._account_names(args.get("company_id"))
        path = self._file(args.get("company_id"), args.get("year"), "year")
        limit, offset = _bounds(args, self.max_rows)
        bookings: list[dict[str, object]] = []
        for table in tables:
            try:
                rows, _ = self._rows(path, table, limit=self.max_rows * 50)
            except ServiceError as exc:
                if exc.error_code == "NOT_FOUND" and source == "both":
                    continue
                raise
            for r in rows:
                d = r.get("datBeleg")
                if date_from and (not isinstance(d, str) or d < str(date_from)):
                    continue
                if date_to and (not isinstance(d, str) or d > str(date_to)):
                    continue
                if account is not None and int(account) not in (r.get("lSoll"), r.get("lHaben")):
                    continue
                bookings.append(_booking(r, "journal" if table == "bh_jour4" else "stapel", names))
        bookings.sort(key=lambda b: (str(b.get("document_date") or ""), b.get("number") or 0))
        page = bookings[offset:offset + limit]
        return {"bookings": page, "total_matching": len(bookings), "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(bookings) else None,
                "note": "journal = festgeschriebene Buchungen, stapel = noch nicht festgeschriebener Buchungsstapel"}

    def balances(self, args: dict[str, object]) -> dict[str, object]:
        names = self._account_names(args.get("company_id"))
        try:
            rows, _ = self._rows(self._file(args.get("company_id"), args.get("year"), "year"), "bh_saldi", limit=self.max_rows * 5)
        except ServiceError as exc:
            if exc.error_code != "NOT_FOUND" or not exc.message.startswith("Table"):
                raise
            rows = []  # Jahr ohne Buchungen: Lexware legt bh_saldi erst bei Bedarf an
        result = []
        for r in rows:
            if r.get("lKstNdx") not in (None, 0, -1):
                continue
            monthly = [r.get(f"dftSaldoEuro_{i}") for i in range(13)]
            result.append({"account": r.get("lNr"), "name": names.get(r.get("lNr")),
                           "opening_balance": r.get("dftVortragEuro"), "balance": r.get("dftKtoStandEuro"),
                           "stapel_amount": r.get("dftStapelEuro"), "last_booking": r.get("datLast"),
                           "monthly": monthly})
        result.sort(key=lambda x: x["account"] or 0)
        return {"balances": result, "count": len(result), "currency": "EUR",
                "note": "monthly[0] ist die Eroeffnung bzw. Periode 0, monthly[1..12] die Monate. Vorzeichen wie in Lexware gespeichert."}


def _matches(actual: object, expected: object) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.lower() in actual.lower()
    return actual == expected


def _where(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 10:
        raise validation_error("where must be an object with up to 10 fields")
    return value


def _bounds(args: dict[str, object], maximum: int) -> tuple[int, int]:
    try:
        limit = int(args.get("limit") or 200)
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError) as exc:
        raise validation_error("limit and offset must be integers") from exc
    if not 1 <= limit <= maximum or not 0 <= offset <= 10_000_000:
        raise validation_error(f"limit must be 1..{maximum}, offset >= 0")
    return limit, offset


def _booking(r: dict[str, Any], source: str, names: dict[int, str]) -> dict[str, object]:
    debit, credit = r.get("lSoll"), r.get("lHaben")
    return {
        "source": source,
        "number": r.get("lNr"),
        "document_date": r.get("datBeleg"),
        "booking_date": r.get("datBuchung"),
        "service_date": r.get("datService"),
        "document_number": r.get("szBelegNr") or None,
        "document_range": r.get("iBelegKreis"),
        "text": r.get("szText"),
        "debit_account": debit,
        "debit_account_name": names.get(debit) if isinstance(debit, int) else None,
        "credit_account": credit,
        "credit_account_name": names.get(credit) if isinstance(credit, int) else None,
        "net_amount": r.get("dftNetto"),
        "tax_amount": r.get("dftUSt"),
        "tax_rate": r.get("dftUStProz"),
        "tax_account": r.get("lUSt") or None,
        "cost_centre_1": r.get("lKost1") or None,
        "cost_centre_2": r.get("lKost2") or None,
        "status": r.get("iStatus"),
    }


def iter_fields(specs: Iterable[TableSpec]) -> Iterable[str]:
    for spec in specs:
        yield from spec.public_fields()
