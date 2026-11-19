import unittest
from datetime import date

from scripts.nse_eod_trial import inspect_option_rows, parse_expiry


class ExpiryParsingTests(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(parse_expiry("2026-11-26"), date(2026, 11, 26))

    def test_iso_datetime(self):
        self.assertEqual(parse_expiry("2026-11-26T00:00:00+05:30"), date(2026, 11, 26))

    def test_epoch_milliseconds(self):
        self.assertEqual(parse_expiry("1795651200000"), date(2026, 11, 26))

    def test_epoch_seconds(self):
        self.assertEqual(parse_expiry("1795651200"), date(2026, 11, 26))

    def test_excel_serial(self):
        self.assertEqual(parse_expiry("46352"), date(2026, 11, 26))

    def test_invalid_value(self):
        self.assertIsNone(parse_expiry("not-a-date"))

    def test_contract_master_semantic_validation(self):
        headers = ["TckrSymb", "XpryDt", "OptnTp", "StrkPric"]
        rows = [
            {"TckrSymb": "NIFTY", "XpryDt": "2026-11-26T00:00:00", "OptnTp": "CE", "StrkPric": "25000"},
            {"TckrSymb": "NIFTY", "XpryDt": "2026-11-26T00:00:00", "OptnTp": "PE", "StrkPric": "25000"},
            {"TckrSymb": "BANKNIFTY", "XpryDt": "2026-11-26", "OptnTp": "CE", "StrkPric": "50000"},
        ]
        result = inspect_option_rows(rows, headers, date(2026, 11, 18))
        self.assertTrue(result["valid"])
        self.assertEqual(result["nifty_option_rows"], 2)
        self.assertEqual(result["current_or_future_expiries"], ["2026-11-26"])

    def test_stale_contract_master_rejected(self):
        headers = ["TckrSymb", "XpryDt", "OptnTp"]
        rows = [
            {"TckrSymb": "NIFTY", "XpryDt": "2016-11-26", "OptnTp": "CE"},
            {"TckrSymb": "NIFTY", "XpryDt": "2016-11-26", "OptnTp": "PE"},
        ]
        result = inspect_option_rows(rows, headers, date(2026, 11, 18))
        self.assertFalse(result["valid"])
        self.assertEqual(result["current_or_future_expiries"], [])


if __name__ == "__main__":
    unittest.main()
