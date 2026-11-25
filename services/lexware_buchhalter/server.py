from __future__ import annotations

import logging
import os

from common.config import RuntimeConfig
from common.mcp_server import McpApplication, Tool, serve
from .provider import LexwareDesktopReader


BASE = {"type": "object", "additionalProperties": False}
COMPANY = {"company_id": {"type": "integer", "minimum": 0, "description": "Firmennummer aus lxbh_list_companies"}}
YEAR = {"year": {"type": "integer", "minimum": 1990, "maximum": 2100, "description": "Wirtschaftsjahr"}}
PAGING = {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}, "offset": {"type": "integer", "minimum": 0}}
DATES = {"date_from": {"type": "string", "format": "date"}, "date_to": {"type": "string", "format": "date"}}


def schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    result = BASE | {"properties": properties}
    if required:
        result["required"] = required
    return result


def build_application() -> McpApplication:
    runtime = RuntimeConfig.from_env()
    reader = LexwareDesktopReader.from_env()
    tools = [
        Tool("lxbh_list_companies", "Listet die Firmen in Lexware buchhaltung mit Nummer, Name und vorhandenen Wirtschaftsjahren.", schema({}), reader.list_companies),
        Tool("lxbh_list_accounts", "Kontenplan einer Firma (Kontonummer, Bezeichnung, bebucht, gesperrt).",
             schema(COMPANY | {"only_booked": {"type": "boolean"}}, ["company_id"]), reader.accounts),
        Tool("lxbh_list_bookings", "Buchungen eines Wirtschaftsjahres aus Journal (festgeschrieben) und/oder Buchungsstapel, optional nach Belegdatum und Konto gefiltert.",
             schema(COMPANY | YEAR | DATES | PAGING | {"source": {"type": "string", "enum": ["journal", "stapel", "both"]}, "account": {"type": "integer"}}, ["company_id", "year"]),
             reader.journal),
        Tool("lxbh_account_balances", "Kontensalden eines Wirtschaftsjahres: Vortrag, aktueller Stand, Stapelanteil und Monatswerte je Konto.",
             schema(COMPANY | YEAR, ["company_id", "year"]), reader.balances),
        Tool("lxbh_list_tables", "Technische Liste aller lesbaren Lexware-Tabellen einer Firma (und optional eines Jahres) mit Feldnamen und Zeilenzahl.",
             schema(COMPANY | YEAR, ["company_id"]), reader.list_tables),
        Tool("lxbh_read_table", "Liest eine beliebige Lexware-Tabelle aus lxbh_list_tables dekodiert. Ohne year: Kontenplan-Datei, mit year: Jahresdatei. where filtert exakt bzw. bei Text per Teilstring.",
             schema(COMPANY | YEAR | PAGING | {"table": {"type": "string"}, "where": {"type": "object"}}, ["company_id", "table"]),
             reader.read_table),
    ]
    app = McpApplication("lexware-buchhaltung-readonly", "0.1.0", runtime, tools)
    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    serve(build_application())


if __name__ == "__main__":
    main()
