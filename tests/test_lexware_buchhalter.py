from __future__ import annotations

import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from common.errors import ServiceError
from services.lexware_buchhalter.provider import LexwareDesktopReader, decode_date, encode_date


def _mk_year(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE __mapping (ver TEXT, vtable TEXT, cid INTEGER, name TEXT, type TEXT, preset TEXT, rtable TEXT, rname TEXT, roff INT)")
    fields = [
        (1, "lNr", "int32", "bh_jour4", "col0", -1), (4, "datBeleg", "int32", "bh_jour4", "col2", -1),
        (7, "szText", "char(80)", "bh_jour4", "data6", 2), (8, "lSoll", "int32", "bh_jour4", "data6", 82),
        (9, "lHaben", "int32", "bh_jour4", "data6", 86), (13, "dftNetto", "float64", "bh_jour4", "data6", 104),
        (26, "old_szBelegNr", "char(11)", "bh_jour4", "col5", -1),
    ]
    conn.executemany("INSERT INTO __mapping VALUES ('1','bh_jour4_vtemp',?,?,?,NULL,?,?,?)", fields)
    conn.execute("CREATE TABLE bh_jour4 (col0 INT, col2 INT, data6 BLOB, col5 CHAR(11))")
    blob = bytearray(120)
    text = "Miete Büro".encode("cp1252")
    blob[2:2 + len(text)] = text
    struct.pack_into("<i", blob, 82, 4210)
    struct.pack_into("<i", blob, 86, 1800)
    struct.pack_into("<d", blob, 104, 1234.5)
    conn.execute("INSERT INTO bh_jour4 VALUES (?,?,?,?)", (7, encode_date("2025-03-15"), bytes(blob), "x"))
    conn.execute("CREATE TABLE bh_stap4 (col0 INT)")
    conn.commit()
    conn.execute("UPDATE bh_jour4 SET col5 = CAST(? AS TEXT)", ("Ähnlich".encode("cp1252"),))
    conn.commit()
    conn.close()


def _mk_accounts(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE __mapping (ver TEXT, vtable TEXT, cid INTEGER, name TEXT, type TEXT, preset TEXT, rtable TEXT, rname TEXT, roff INT)")
    fields = [(1, "lNr", "int32", "bh_konto", "col0", -1), (2, "szName", "char(40)", "bh_konto", "col1", -1),
              (3, "bGesperrt", "int16", "bh_konto", "col2", -1), (4, "fDatevAutomatik", "int16", "bh_konto", "col3", -1),
              (5, "lNr_Datev", "int32", "bh_konto", "col4", -1)]
    conn.executemany("INSERT INTO __mapping VALUES ('1','bh_konto_vtemp',?,?,?,NULL,?,?,?)", fields)
    conn.execute("CREATE TABLE bh_konto (col0 INT, col1 TEXT, col2 INT, col3 INT, col4 INT)")
    conn.executemany("INSERT INTO bh_konto VALUES (?,?,?,?,?)", [
        (1200, "Bank", 0, 0, 1200), (4210, "Miete", 0, 0, 4210), (8400, "Erlöse 19 % USt\x00\x00", 0, 1, 8400),
        (1000, "Kasse", 1, 0, 1000), (4185, "Sonder", 0, 0, 1590)])
    conn.commit()
    conn.close()


class LexwareDesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "00000002" / "2025").mkdir(parents=True)
        _mk_year(base / "00000002" / "2025" / "BHStdYear.dbs")
        _mk_accounts(base / "00000002" / "BHStdAccountList.dbs")
        self.export = base / "export"
        self.reader = LexwareDesktopReader(base, export_dir=self.export)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_date_codec(self) -> None:
        self.assertEqual(decode_date(132710657), "2025-01-01")
        self.assertEqual(encode_date("2026-11-09"), 132778761)
        self.assertIsNone(decode_date(0))

    def test_decodes_blob_fields_from_mapping(self) -> None:
        out = self.reader.read_table({"company_id": 2, "year": 2025, "table": "bh_jour4"})
        row = out["rows"][0]
        self.assertEqual(row["lNr"], 7)
        self.assertEqual(row["datBeleg"], "2025-03-15")
        self.assertEqual(row["szText"], "Miete Büro")
        self.assertEqual((row["lSoll"], row["lHaben"], row["dftNetto"]), (4210, 1800, 1234.5))
        self.assertNotIn("old_szBelegNr", row)

    def test_balances_empty_when_year_has_no_saldi_table(self) -> None:
        out = self.reader.balances({"company_id": 2, "year": 2025})
        self.assertEqual(out["count"], 0)

    def test_cp1252_text_columns_do_not_break_reads(self) -> None:
        out = self.reader.read_table({"company_id": 2, "year": 2025, "table": "bh_jour4"})
        self.assertEqual(out["rows"][0]["lNr"], 7)

    def test_bookings_filter_and_companies(self) -> None:
        hit = self.reader.journal({"company_id": 2, "year": 2025, "account": 4210, "date_from": "2025-03-01"})
        self.assertEqual(hit["total_matching"], 1)
        self.assertEqual(hit["bookings"][0]["debit_account"], 4210)
        miss = self.reader.journal({"company_id": 2, "year": 2025, "date_to": "2025-02-28"})
        self.assertEqual(miss["total_matching"], 0)
        companies = self.reader.list_companies()["companies"]
        self.assertEqual(companies, [{"company_id": 2, "name": None, "fiscal_years": [2025]}])

    def test_rejects_bad_input_and_never_writes(self) -> None:
        with self.assertRaises(ServiceError):
            self.reader.read_table({"company_id": 2, "year": 2025, "table": "bh_jour4; DROP TABLE x"})
        with self.assertRaises(ServiceError):
            self.reader.read_table({"company_id": "../1", "year": 2025, "table": "bh_jour4"})
        with self.assertRaises(ServiceError):
            self.reader.read_table({"company_id": 2, "year": 2025, "table": "bh_jour4", "where": {"secret": 1}})
        path = Path(self.tmp.name) / "00000002" / "2025" / "BHStdYear.dbs"
        before = path.stat().st_mtime_ns
        self.reader.journal({"company_id": 2, "year": 2025})
        self.assertEqual(before, path.stat().st_mtime_ns)


    def test_text_columns_are_cut_at_nul(self) -> None:
        names = {a["account"]: a["name"] for a in self.reader.accounts({"company_id": 2})["accounts"]}
        self.assertEqual(names[8400], "Erlöse 19 % USt")


class DatevImportTests(LexwareDesktopTests):
    GOOD = {"date": "2025-03-15", "amount": 119.0, "debit_account": 1200, "credit_account": 8400,
            "text": "Rechnung 17 Müller", "document_number": "RE-17"}

    def test_preview_does_not_write(self) -> None:
        out = self.reader.create_datev_import({"company_id": 2, "year": 2025, "bookings": [self.GOOD]})
        self.assertTrue(out["valid"], out["errors"])
        self.assertFalse(out["written"])
        self.assertTrue(out["preview"][0]["automatic_tax"])
        self.assertFalse(self.export.exists())

    def test_writes_extf_file(self) -> None:
        lexware = Path(self.tmp.name) / "00000002" / "2025" / "BHStdYear.dbs"
        before = lexware.stat().st_mtime_ns
        out = self.reader.create_datev_import({"company_id": 2, "year": 2025, "confirm": True, "label": "Test",
                                               "bookings": [self.GOOD, {**self.GOOD, "amount": 50.5, "debit_account": 4210,
                                                                        "credit_account": 1200, "bu_key": "9", "text": 'Miete "März"'}]})
        self.assertTrue(out["written"], out)
        raw = Path(out["file"]).read_bytes()
        lines = raw.decode("cp1252").split("\r\n")
        header = lines[0].split(";")
        self.assertEqual(header[:5], ['"EXTF"', "700", "21", '"Buchungsstapel"', "13"])
        self.assertEqual(len(header), 31)
        self.assertEqual(header[12:16], ["20250101", "4", "20250315", "20250315"])
        self.assertEqual(len(lines[1].split(";")), 125)
        row = lines[2].split(";")
        self.assertEqual(len(row), 125)
        self.assertEqual(row[:10], ["119,00", '"S"', '"EUR"', "", "", "", "1200", "8400", "", "1503"])
        self.assertEqual((row[10], row[13]), ('"RE-17"', '"Rechnung 17 Müller"'))
        row2 = lines[3].split(";")
        self.assertEqual((row2[0], row2[6], row2[7], row2[8], row2[13]), ("50,50", "4210", "1200", '"9"', '"Miete ""März"""'))
        self.assertEqual(lines[-1], "")
        self.assertEqual(before, lexware.stat().st_mtime_ns)

    def test_personal_accounts_are_accepted_with_warning(self) -> None:
        out = self.reader.create_datev_import({"company_id": 2, "year": 2025, "bookings": [
            {**self.GOOD, "debit_account": 10001}, {**self.GOOD, "debit_account": 4210, "credit_account": 70001}]})
        self.assertTrue(out["valid"], out["errors"])
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("Debitor", out["warnings"][0])
        self.assertEqual(out["preview"][1]["credit_name"], "Personenkonto (Kreditor)")

    def test_rejects_invalid_bookings_without_writing(self) -> None:
        bad = [
            {**self.GOOD, "date": "2024-12-31"},
            {**self.GOOD, "debit_account": 9999},
            {**self.GOOD, "debit_account": 100000},
            {**self.GOOD, "debit_account": 1000},
            {**self.GOOD, "debit_account": 4185},
            {**self.GOOD, "bu_key": "3"},
            {**self.GOOD, "amount": -5},
            {**self.GOOD, "amount": 1.234},
            {**self.GOOD, "document_number": "RE 17"},
            {**self.GOOD, "text": "x" * 61},
            {**self.GOOD, "credit_account": 1200},
            {**self.GOOD, "foo": 1},
        ]
        out = self.reader.create_datev_import({"company_id": 2, "year": 2025, "confirm": True, "bookings": bad})
        self.assertFalse(out["valid"])
        self.assertFalse(out["written"])
        self.assertEqual(len(out["errors"]), len(bad), out["errors"])
        self.assertFalse(self.export.exists())
        with self.assertRaises(ServiceError):
            self.reader.create_datev_import({"company_id": 2, "year": 2024, "bookings": [self.GOOD]})


if __name__ == "__main__":
    unittest.main()
