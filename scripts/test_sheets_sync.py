import unittest
from unittest.mock import patch

import sheets_sync


class SheetRowTests(unittest.TestCase):
    def test_sentinels_baselines_deltas_and_stale_rows(self):
        products = {
            "https://shop.example/product/a/1/": {
                "name": "A",
                "price": 1000,
                "stock": {"unknown": -1, "cap": 9999, "baseline": 3, "exact": 7},
                "image_url": "https://cdn.example/a.jpg",
                "checked_at": "2026-10-08 11:00 KST",
                "stale": False,
                "uncertain": False,
            },
            "https://shop.example/product/b/2/": {
                "name": "B",
                "price": 2000,
                "stock": {"M": 4},
                "checked_at": "2026-10-07 11:00 KST",
                "stale": True,
                "uncertain": True,
            },
        }
        previous = {
            "https://shop.example/product/a/1/": {
                "stock": {"unknown": 4, "cap": 20, "exact": 10}
            },
            "https://shop.example/product/b/2/": {"stock": {"M": 9}},
        }
        rows = sheets_sync.build_sheet_rows(products, previous)
        by_product_option = {(row[0], row[1]): row for row in rows[1:]}

        unknown = by_product_option[("A", "unknown")]
        self.assertEqual(unknown[2], "확인불가")
        self.assertEqual(unknown[7], "")
        self.assertIn("확인불가", unknown[8])

        cap = by_product_option[("A", "cap")]
        self.assertEqual(cap[2], "주문가능 9999 이상")
        self.assertEqual(cap[7], "")

        baseline = by_product_option[("A", "baseline")]
        self.assertEqual(baseline[6], "")
        self.assertEqual(baseline[7], "")

        exact = by_product_option[("A", "exact")]
        self.assertEqual(exact[6], 10)
        self.assertEqual(exact[7], -3)
        self.assertTrue(exact[4].startswith('=IMAGE("https://'))

        stale = by_product_option[("B", "M")]
        self.assertEqual(stale[5], "2026-10-07 11:00 KST")
        self.assertEqual(stale[7], "")
        self.assertEqual(stale[8], "오류 · 이전 데이터")

    def test_stale_or_uncertain_previous_run_is_only_a_baseline(self):
        products = {
            "https://shop.example/product/c/3/": {
                "name": "C", "stock": {"M": 0}, "stale": False, "uncertain": False
            },
            "https://shop.example/product/d/4/": {
                "name": "D", "stock": {"M": 4}, "stale": False, "uncertain": False
            },
        }
        previous = {
            "https://shop.example/product/c/3/": {
                "stock": {"M": 5}, "stale": True, "uncertain": True
            },
            "https://shop.example/product/d/4/": {
                "stock": {"M": 0}, "stale": False, "uncertain": True
            },
        }
        rows = sheets_sync.build_sheet_rows(products, previous)
        by_name = {row[0]: row for row in rows[1:]}
        self.assertEqual(by_name["C"][7], "")
        self.assertEqual(by_name["C"][8], "품절")
        self.assertEqual(by_name["D"][7], "")
        self.assertEqual(by_name["D"][8], "저재고")

        summary = sheets_sync.summarize_inventory(products, previous, item_limit=10)
        self.assertEqual(summary["newly_soldout"], 0)
        self.assertEqual(summary["restocked"], 0)
        self.assertEqual(summary["low_stock"], 1)

    def test_summary_counts_and_item_list_are_bounded(self):
        products = {}
        previous = {}
        for index in range(10):
            url = f"https://shop.example/product/p/{index}/"
            products[url] = {
                "name": f"P{index}",
                "stock": {"M": 0 if index < 4 else 3},
                "stale": False,
                "uncertain": False,
            }
            previous[url] = {"stock": {"M": 2 if index < 4 else 0}}
        summary = sheets_sync.summarize_inventory(products, previous, item_limit=3)
        self.assertEqual(summary["newly_soldout"], 4)
        self.assertEqual(summary["restocked"], 6)
        self.assertEqual(summary["low_stock"], 6)
        self.assertEqual(len(summary["items"]), 3)
        self.assertEqual(summary["omitted"], 13)


class SheetApiOrderingTests(unittest.TestCase):
    def test_grid_grows_before_large_write_without_prewrite_clear(self):
        calls = []

        def fake_request(method, url, token, payload=None, timeout=30):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "sheets": [{
                        "properties": {
                            "sheetId": 7,
                            "title": "Stock",
                            "gridProperties": {"rowCount": 2, "columnCount": 5},
                        },
                        "conditionalFormats": [],
                    }]
                }
            return {}

        rows = [sheets_sync.HEADERS] + [[""] * 11 for _ in range(1005)]
        with patch.object(sheets_sync, "get_access_token", return_value="token"), patch.object(
            sheets_sync, "_request_json", side_effect=fake_request
        ):
            sheets_sync.sync_to_sheet("sheet-id", "{}", rows)

        grow_index = next(
            i for i, (_, url, payload) in enumerate(calls)
            if url.endswith(":batchUpdate")
            and payload
            and payload.get("requests", [{}])[0].get("updateSheetProperties", {}).get("fields")
            == "gridProperties.rowCount,gridProperties.columnCount"
        )
        write_index = next(
            i for i, (_, url, _) in enumerate(calls) if "values:batchUpdate" in url
        )
        growth = calls[grow_index][2]["requests"][0]["updateSheetProperties"]["properties"]["gridProperties"]
        self.assertEqual(growth, {"rowCount": len(rows), "columnCount": 11})
        self.assertLess(grow_index, write_index)
        self.assertFalse(any(":clear" in url for _, url, _ in calls[:write_index]))

    def test_failed_table_write_never_clears_old_tail(self):
        calls = []

        def fake_request(method, url, token, payload=None, timeout=30):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "sheets": [{
                        "properties": {
                            "sheetId": 7,
                            "title": "Stock",
                            "gridProperties": {"rowCount": 100},
                        },
                        "conditionalFormats": [],
                    }]
                }
            if "values:batchUpdate" in url:
                raise sheets_sync.SheetSyncError("simulated write failure")
            return {}

        with patch.object(sheets_sync, "get_access_token", return_value="token"), patch.object(
            sheets_sync, "_request_json", side_effect=fake_request
        ):
            with self.assertRaises(sheets_sync.SheetSyncError):
                sheets_sync.sync_to_sheet("sheet-id", "{}", [sheets_sync.HEADERS, [""] * 11])

        self.assertTrue(any("values:batchUpdate" in url for _, url, _ in calls))
        self.assertFalse(any(":clear" in url for _, url, _ in calls))

    def test_successful_write_clears_tail_then_formats(self):
        calls = []

        def fake_request(method, url, token, payload=None, timeout=30):
            calls.append(url)
            if method == "GET":
                return {
                    "sheets": [{
                        "properties": {
                            "sheetId": 7,
                            "title": "Stock",
                            "gridProperties": {"rowCount": 100},
                        },
                        "conditionalFormats": [{"booleanRule": {}}],
                    }]
                }
            return {}

        with patch.object(sheets_sync, "get_access_token", return_value="token"), patch.object(
            sheets_sync, "_request_json", side_effect=fake_request
        ):
            result = sheets_sync.sync_to_sheet(
                "sheet-id", "{}", [sheets_sync.HEADERS, [""] * 11]
            )

        write_index = next(i for i, url in enumerate(calls) if "values:batchUpdate" in url)
        clear_index = next(i for i, url in enumerate(calls) if ":clear" in url)
        format_index = max(i for i, url in enumerate(calls) if url.endswith(":batchUpdate"))
        self.assertLess(write_index, clear_index)
        self.assertLess(clear_index, format_index)
        self.assertEqual(result["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
