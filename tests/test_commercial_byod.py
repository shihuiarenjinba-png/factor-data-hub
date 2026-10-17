"""Offline, dependency-local regression tests for the commercial BYOD path."""
from __future__ import annotations

import io
import unittest
import zipfile

from commercial_byod import DataImportError, MAX_UPLOAD_BYTES, inspect_upload


def inspect(data: bytes, name: str = "data.csv", **kwargs):
    opts = {
        "source": "ユーザー所有データ",
        "license_note": "利用権を確認済み",
        "rights_confirmed": True,
    }
    opts.update(kwargs)
    return inspect_upload(data, name, **opts)


class CommercialBYODTests(unittest.TestCase):
    def test_valid_csv_quality_report(self):
        frame, report = inspect(b"date,price\n2026-01-01,100\n2026-01-02,102\n")
        self.assertEqual(len(frame), 2)
        self.assertEqual(report["status"], "STRUCTURE_CHECKED")
        self.assertEqual(report["duplicate_dates"], 0)
        self.assertEqual(len(report["input_sha256"]), 64)

    def test_upload_requires_rights_confirmation(self):
        with self.assertRaisesRegex(DataImportError, "権利"):
            inspect(b"date,value\n2026-01-01,5\n", rights_confirmed=False)

    def test_missing_provenance_rejected(self):
        with self.assertRaises(DataImportError):
            inspect(b"x\n1\n", source="")
        with self.assertRaises(DataImportError):
            inspect(b"x\n1\n", license_note="")

    def test_reject_unsupported_extension(self):
        with self.assertRaisesRegex(DataImportError, "CSVとXLSX"):
            inspect(b"just plain text", "data.txt")

    def test_file_size_limit(self):
        with self.assertRaisesRegex(DataImportError, "10MiB"):
            inspect(b"x" * (MAX_UPLOAD_BYTES + 1))

    def test_invalid_date_and_duplicates_are_review(self):
        _, report = inspect(
            b"date,close\n2026-01-01,100\n2026-01-01,100\nnot-a-date,101\n"
        )
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["duplicate_dates"], 1)
        self.assertEqual(report["invalid_dates"], 1)
        self.assertEqual(report["duplicate_rows"], 1)

    def test_utf8_bom_and_missing_value_are_supported(self):
        _, report = inspect(b"\xef\xbb\xbfdate,value\n2026-01-01,1\n2026-01-02,\n")
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["missing_by_column"]["value"], 1)

    def test_duplicate_normalized_headers_rejected(self):
        with self.assertRaisesRegex(DataImportError, "重複した列名"):
            inspect(b"date,price, price\n2026-01-01,10,20\n")

    def test_bad_excel_zip_rejected(self):
        with self.assertRaises(DataImportError):
            inspect(b"not-an-xlsx", "data.xlsx")

    def test_xlsx_zip_expansion_limit_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", b"a" * (81 * 1024 * 1024))
        with self.assertRaisesRegex(DataImportError, "展開後"):
            inspect(buffer.getvalue(), "data.xlsx")

    def test_valid_xlsx_import(self):
        import pandas as pd
        frame = pd.DataFrame({"date": ["2026-01-01"], "close": [100]})
        mem = io.BytesIO()
        frame.to_excel(mem, index=False, engine="openpyxl")
        read, report = inspect(mem.getvalue(), "local.xlsx")
        self.assertEqual(len(read), 1)
        self.assertEqual(report["file_type"], "xlsx")
        self.assertEqual(report["status"], "STRUCTURE_CHECKED")

    def test_does_not_call_external_network(self):
        from unittest.mock import patch
        with patch("socket.create_connection", side_effect=AssertionError("network used")):
            _, report = inspect(b"date,price\n2026-01-01,1\n")
        self.assertEqual(report["rows"], 1)


if __name__ == "__main__":
    unittest.main()
