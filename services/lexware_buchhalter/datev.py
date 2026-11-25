"""Build DATEV-Format (EXTF) Buchungsstapel files for the Lexware DATEV import.

Lexware buchhaltung imports such a file into "Ansicht > Import Buchungsdaten".
Nothing is booked until the user checks the list there and books it into the
Stapel or Journal. This module only produces the file; it never touches the
Lexware database.

Layout: DATEV-Format version 700, category 21, format version 13 with the 125
booking columns. Separator ";", decimal comma, text fields in double quotes,
Windows-1252, CRLF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

COLUMNS: tuple[str, ...] = (
    "Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basisumsatz", "WKZ Basisumsatz",
    "Konto", "Gegenkonto (ohne BU-Schlüssel)", "BU-Schlüssel", "Belegdatum", "Belegfeld 1", "Belegfeld 2", "Skonto",
    "Buchungstext", "Postensperre", "Diverse Adressnummer", "Geschäftspartnerbank", "Sachverhalt", "Zinssperre",
    "Beleglink",
    *[f"Beleginfo – {kind} {i}" for i in range(1, 9) for kind in ("Art", "Inhalt")],
    "KOST1 – Kostenstelle", "KOST2 – Kostenstelle", "Kost Menge", "EU-Land u. USt-IdNr.", "EU-Steuersatz",
    "Abw. Versteuerungsart", "Sachverhalt L+L", "Funktionsergänzung L+L", "BU 49 Hauptfunktionstyp",
    "BU 49 Hauptfunktionsnummer", "BU 49 Funktionsergänzung",
    *[f"Zusatzinformation – {kind} {i}" for i in range(1, 21) for kind in ("Art", "Inhalt")],
    "Stück", "Gewicht", "Zahlweise", "Forderungsart", "Veranlagungsjahr", "Zugeordnete Fälligkeit", "Skontotyp",
    "Auftragsnummer", "Buchungstyp", "USt-Schlüssel (Anzahlungen)", "EU-Mitgliedstaat (Anzahlungen)",
    "Sachverhalt L+L (Anzahlungen)", "EU-Steuersatz (Anzahlungen)", "Erlöskonto (Anzahlungen)", "Herkunft-Kz",
    "Leerfeld", "KOST-Datum", "SEPA-Mandatsreferenz", "Skontosperre", "Gesellschaftername", "Beteiligtennummer",
    "Identifikationsnummer", "Zeichnernummer", "Postensperre bis", "Bezeichnung", "Kennzeichen", "Festschreibung",
    "Leistungsdatum", "Datum Zuord.", "Fälligkeit", "Generalumkehr", "Steuersatz", "Land", "Abrechnungsreferent",
    "BVV-Position", "EU-Mitgliedstaat u. UStID (Ursprung)", "EU-Steuersatz (Ursprung)", "Abw. Skontokonto",
)
assert len(COLUMNS) == 125
_IDX = {name: i for i, name in enumerate(COLUMNS)}

DOC_NUMBER = re.compile(r"^[A-Za-z0-9$&%*+\-/]{0,20}$")  # DATEV charset; Lexware keeps 20 characters
BU_KEY = re.compile(r"^\d{1,4}$")
COST_CENTRE = re.compile(r"^[A-Za-z0-9]{1,8}$")
MAX_BOOKINGS = 1000
MAX_AMOUNT = Decimal("9999999999.99")


@dataclass(frozen=True, slots=True)
class Account:
    number: int
    name: str | None
    locked: bool
    automatic: bool
    datev_number: int | None


@dataclass(frozen=True, slots=True)
class Booking:
    booking_date: date
    amount: Decimal
    debit: int
    credit: int
    text: str
    document_number: str
    bu_key: str
    cost_centre_1: str
    cost_centre_2: str
    service_date: date | None


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _amount(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def _cp1252(text: str, field_name: str, errors: list[str], prefix: str) -> None:
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        errors.append(f"{prefix}{field_name} enthält Zeichen, die DATEV/Lexware nicht speichern kann")


def _date(value: object, name: str, errors: list[str], prefix: str) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        errors.append(f"{prefix}{name} muss ein Datum JJJJ-MM-TT sein")
        return None


def parse_bookings(raw: object, year: int, accounts: dict[int, Account],
                   fiscal_start: date, fiscal_end: date) -> tuple[list[Booking], list[str], list[str]]:
    """Validate the requested bookings. Returns (bookings, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, list) or not raw:
        return [], ["bookings muss eine nicht leere Liste sein"], []
    if len(raw) > MAX_BOOKINGS:
        return [], [f"Höchstens {MAX_BOOKINGS} Buchungen je Datei"], []
    allowed = {"date", "amount", "debit_account", "credit_account", "text", "document_number", "bu_key",
               "cost_centre_1", "cost_centre_2", "service_date"}
    result: list[Booking] = []
    for n, item in enumerate(raw, start=1):
        p = f"Buchung {n}: "
        if not isinstance(item, dict):
            errors.append(p + "muss ein Objekt sein")
            continue
        unknown = sorted(set(item) - allowed)
        if unknown:
            errors.append(p + "unbekannte Felder " + ", ".join(unknown))
        d = _date(item.get("date"), "date", errors, p)
        if d and not fiscal_start <= d <= fiscal_end:
            errors.append(p + f"Datum {d.isoformat()} liegt nicht im Wirtschaftsjahr {year}")
        try:
            amount = Decimal(str(item.get("amount")))
        except InvalidOperation:
            amount = Decimal("NaN")
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
            errors.append(p + "amount muss eine positive Zahl sein (Bruttobetrag, Punkt als Dezimaltrenner)")
            amount = Decimal("0")
        elif amount != amount.quantize(Decimal("0.01")):
            errors.append(p + "amount hat mehr als zwei Nachkommastellen")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        numbers: list[int] = []
        for key in ("debit_account", "credit_account"):
            value = item.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(p + f"{key} muss eine Kontonummer sein")
                numbers.append(0)
                continue
            acc = accounts.get(value)
            if acc is None:
                errors.append(p + f"Konto {value} gibt es im Kontenplan nicht (Personenkonten werden noch nicht unterstützt)")
            elif acc.locked:
                errors.append(p + f"Konto {value} ist gesperrt")
            elif acc.datev_number not in (None, 0, value):
                errors.append(p + f"Konto {value} hat eine abweichende DATEV-Zuordnung ({acc.datev_number}); bitte in Lexware prüfen")
            numbers.append(value)
        if len(numbers) == 2 and numbers[0] == numbers[1] and numbers[0]:
            errors.append(p + "Soll- und Habenkonto sind gleich")
        text = str(item.get("text") or "").strip()
        if not text or len(text) > 60:
            errors.append(p + "text ist Pflicht und darf höchstens 60 Zeichen haben")
        _cp1252(text, "text", errors, p)
        doc = str(item.get("document_number") or "").strip()
        if not DOC_NUMBER.match(doc):
            errors.append(p + "document_number: höchstens 20 Zeichen aus A-Z, 0-9 und $ & % * + - /")
        if not doc:
            warnings.append(p + "keine Belegnummer angegeben")
        bu = str(item.get("bu_key") or "").strip()
        if bu and not BU_KEY.match(bu):
            errors.append(p + "bu_key muss aus 1 bis 4 Ziffern bestehen")
        if bu and bu != "40":
            auto = [str(x) for x in numbers if x in accounts and accounts[x].automatic]
            if auto:
                errors.append(p + f"Automatikkonto {', '.join(auto)} darf keinen BU-Schlüssel haben; Lexware rechnet die Steuer selbst")
        costs = []
        for key in ("cost_centre_1", "cost_centre_2"):
            value = str(item.get(key) or "").strip()
            if value and not COST_CENTRE.match(value):
                errors.append(p + f"{key}: höchstens 8 Buchstaben oder Ziffern")
            costs.append(value)
        service = _date(item["service_date"], "service_date", errors, p) if item.get("service_date") else None
        if d and len(numbers) == 2:
            result.append(Booking(d, amount, numbers[0], numbers[1], text, doc, bu, costs[0], costs[1], service))
    return result, errors, warnings


def build_file(bookings: list[Booking], *, advisor: int, client: int, fiscal_start: date, account_length: int,
               label: str, created: datetime, user: str = "Claude") -> bytes:
    dates = [b.booking_date for b in bookings]
    header = [
        _quote("EXTF"), "700", "21", _quote("Buchungsstapel"), "13", created.strftime("%Y%m%d%H%M%S") + f"{created.microsecond // 1000:03d}",
        "", _quote("CL"), _quote(user[:25]), "", str(advisor), str(client), fiscal_start.strftime("%Y%m%d"),
        str(account_length), min(dates).strftime("%Y%m%d"), max(dates).strftime("%Y%m%d"), _quote(label[:30]), "",
        "1", "0", "0", _quote("EUR"), "", "", "", "", "", "", "", "", "",
    ]
    assert len(header) == 31
    lines = [";".join(header), ";".join(COLUMNS)]
    for b in bookings:
        row = [""] * len(COLUMNS)
        row[_IDX["Umsatz (ohne Soll/Haben-Kz)"]] = _amount(b.amount)
        row[_IDX["Soll/Haben-Kennzeichen"]] = _quote("S")
        row[_IDX["WKZ Umsatz"]] = _quote("EUR")
        row[_IDX["Konto"]] = str(b.debit)
        row[_IDX["Gegenkonto (ohne BU-Schlüssel)"]] = str(b.credit)
        if b.bu_key:
            row[_IDX["BU-Schlüssel"]] = _quote(b.bu_key)
        row[_IDX["Belegdatum"]] = b.booking_date.strftime("%d%m")
        if b.document_number:
            row[_IDX["Belegfeld 1"]] = _quote(b.document_number)
        row[_IDX["Buchungstext"]] = _quote(b.text)
        if b.cost_centre_1:
            row[_IDX["KOST1 – Kostenstelle"]] = _quote(b.cost_centre_1)
        if b.cost_centre_2:
            row[_IDX["KOST2 – Kostenstelle"]] = _quote(b.cost_centre_2)
        if b.service_date:
            row[_IDX["Leistungsdatum"]] = b.service_date.strftime("%d%m%Y")
        lines.append(";".join(row))
    return ("\r\n".join(lines) + "\r\n").encode("cp1252")
