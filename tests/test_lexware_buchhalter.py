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


class LexwareDesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "00000002" / "2025").mkdir(parents=True)
        _mk_year(base / "00000002" / "2025" / "BHStdYear.dbs")
        self.reader = LexwareDesktopReader(base)

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


if __name__ == "__main__":
    unittest.main()
