import json
import tempfile
import unittest
from pathlib import Path

from sms_price_check import (
    build_price_rows,
    build_recommendation,
    load_success_price_rows,
    record_success_price,
    save_success_price_rows,
)


class SmsPriceCheckTests(unittest.TestCase):
    def test_load_success_price_rows_returns_empty_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sms_success_prices.json"

            self.assertEqual(load_success_price_rows(path), [])

    def test_save_and_load_success_price_rows_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "sms_success_prices.json"
            rows = [
                {
                    "sms_provider": "smsbower",
                    "country": "4",
                    "success_price": "0.027",
                    "success_count": 2,
                }
            ]

            save_success_price_rows(path, rows)

            self.assertEqual(load_success_price_rows(path), rows)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), rows)

    def test_record_success_price_groups_by_provider_country_price(self):
        rows = load_success_price_rows(Path("missing-sms-success-prices.json"))
        rows = record_success_price(rows, "smsbower", "4", "0.027")
        rows = record_success_price(rows, "smsbower", "4", "0.027000")
        rows = record_success_price(rows, "smsbower", "4", "0.031")

        self.assertEqual(
            rows,
            [
                {
                    "sms_provider": "smsbower",
                    "country": "4",
                    "success_price": "0.027",
                    "success_count": 2,
                },
                {
                    "sms_provider": "smsbower",
                    "country": "4",
                    "success_price": "0.031",
                    "success_count": 1,
                },
            ],
        )

    def test_build_recommendation_prefers_low_price_with_history(self):
        row = build_recommendation(
            configured_countries={"4"},
            country="52",
            lowest_price=0.025,
            second_price=0.031,
            lowest_count=8,
            second_count=3,
            history_success_count=12,
            history_success_price="0.026",
            max_price="0.03",
        )

        self.assertTrue(row["text"].startswith("推荐"))
        self.assertGreater(row["score"], 100)

    def test_build_price_rows_merges_catalog_pool_and_history(self):
        price_catalog = [
            {
                "sms_provider": "smsbower",
                "country": "4",
                "country_name": "United States",
                "lowest_price": 0.027,
                "second_price": 0.031,
                "lowest_count": 2,
                "second_count": 1,
            },
            {
                "sms_provider": "smsbower",
                "country": "16",
                "lowest_price": 0.05,
                "second_price": "",
                "lowest_count": 1,
                "second_count": 0,
            },
        ]
        success_rows = [
            {
                "sms_provider": "smsbower",
                "country": "4",
                "success_price": "0.027",
                "success_count": 3,
            }
        ]

        rows = build_price_rows(price_catalog, ["4"], success_rows, "0.03")

        self.assertEqual(rows[0]["country"], "4")
        self.assertEqual(rows[0]["country_name"], "United States")
        self.assertTrue(rows[0]["in_purchase_pool"])
        self.assertEqual(rows[0]["recommendation"], "已在池中")
        self.assertEqual(rows[0]["history_success_count"], 3)
        self.assertEqual(rows[1]["recommendation"], "不推荐：高于最高价格")


if __name__ == "__main__":
    unittest.main()
