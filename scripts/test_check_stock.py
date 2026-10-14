import inspect
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import check_stock


class NolLoadMoreTests(unittest.TestCase):
    @staticmethod
    def state(current=1, total=3, count=20, button=True):
        return {"current": current, "total": total, "link_count": count, "button": button}

    def page(self, states, batches=()):
        page = Mock()
        page.evaluate.side_effect = states
        page.eval_on_selector_all.side_effect = batches
        return page

    def test_hidden_control_advances_and_collects_every_batch(self):
        page = self.page([self.state(), self.state(2, 3, 40), self.state(3, 3, 60)], [
            ["https://nolmdshop.com/product/first/1/?x=1"],
            ["https://nolmdshop.com/product/second/2/"],
            ["https://nolmdshop.com/product/third/3/"],
        ])
        links = set()
        self.assertTrue(check_stock.scrape_nol_load_more(page, links))
        self.assertEqual(len(links), 3)
        self.assertIn("https://nolmdshop.com/product/first/1/", links)
        self.assertEqual(page.locator.call_args.args, (check_stock.NOL_MORE_SELECTOR,))
        self.assertEqual(page.locator.return_value.first.dispatch_event.call_count, 2)
        page.locator.return_value.first.dispatch_event.assert_called_with("click")
        self.assertEqual(page.wait_for_function.call_count, 2)
        self.assertEqual(page.wait_for_function.call_args.kwargs["arg"]["current"], 2)
        self.assertIn("count > previous.link_count", page.wait_for_function.call_args.args[0])
        page.mouse.wheel.assert_not_called()

    def test_timeout_is_failure_not_partial_success(self):
        page = self.page([self.state()], [[]])
        page.wait_for_function.side_effect = TimeoutError()
        with self.assertRaisesRegex(RuntimeError, "failed to advance from 1/3"):
            check_stock.scrape_nol_load_more(page, set())

    def test_no_advance_missing_controls_and_no_link_growth_fail(self):
        for after in (self.state(), self.state(2, 3, 20), self.state(2, 4, 40), None):
            with self.subTest(after=after):
                with self.assertRaisesRegex(RuntimeError, "no valid advancement"):
                    check_stock.scrape_nol_load_more(self.page([self.state(), after], [[]]), set())

    def test_invalid_initial_counters_and_safety_limit_fail(self):
        for state in (self.state(None), self.state(0), self.state(4, 3),
                      self.state(button=False), self.state(total=31)):
            with self.subTest(state=state):
                with self.assertRaisesRegex(RuntimeError, "NOL load-more"):
                    check_stock.scrape_nol_load_more(self.page([state]), set())

    def test_completed_single_page_needs_no_dispatch_and_filters_keywords(self):
        page = self.page([self.state(1, 1, 20, False)], [[
            "https://nolmdshop.com/product/마킹키트/2/", "https://nolmdshop.com/product/first/1/",
        ]])
        links = set()
        self.assertTrue(check_stock.scrape_nol_load_more(page, links))
        self.assertEqual(links, {"https://nolmdshop.com/product/first/1/"})
        page.locator.assert_not_called()
        page.wait_for_function.assert_not_called()

    def test_no_controls_returns_false_for_ordinary_pagination(self):
        page = self.page([None])
        self.assertFalse(check_stock.scrape_nol_load_more(page, set()))
        page.eval_on_selector_all.assert_not_called()

    def test_collector_keeps_ordinary_pagination_for_other_sites(self):
        with patch.object(check_stock, "CATEGORY_URLS", [("https://other.example/category/1/", True)]), patch.object(check_stock, "scrape_links_on_current_page") as scrape, patch.object(check_stock, "scrape_nol_load_more") as more, patch.object(check_stock, "find_next_page_url", side_effect=["https://other.example/category/1/?page=2", None]):
            check_stock.collect_product_links(Mock())
        self.assertEqual(scrape.call_count, 2)
        more.assert_not_called()

    def test_nol_collector_uses_load_more_and_propagates_failure(self):
        for outcome in (True, RuntimeError("NOL load-more: stalled")):
            with self.subTest(outcome=outcome):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(check_stock, "CATEGORY_URLS", [("https://nolmdshop.com/category/club/30/", True)]), patch.object(check_stock, "scrape_nol_load_more", **kwargs), patch.object(check_stock, "scrape_links_on_current_page") as scrape, patch.object(check_stock, "find_next_page_url") as ordinary:
                    if isinstance(outcome, Exception):
                        with self.assertRaisesRegex(RuntimeError, "stalled"):
                            check_stock.collect_product_links(Mock())
                    else:
                        check_stock.collect_product_links(Mock())
                    scrape.assert_not_called()
                    ordinary.assert_not_called()


class EvidenceReferenceTimeTests(unittest.TestCase):
    def test_anchor_freshness_preserves_original_timestamp_and_explicit_now(self):
        anchor = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        captured = anchor - timedelta(seconds=30)
        url = "https://shop.example/exec/front/shop/CalculatorProduct?product_no=1785&product%5BA%5D=9999"
        capture = {"product_no": "1785", "item_code": "A", "captured_at": captured.isoformat(), "request_url": url, "response_url": url, "content_type": "application/json", "api_payload": {"sItemCode": "A", "product_no": "1785", "stock_number": 14}}
        with patch.object(check_stock, "BROWSER_EVIDENCE_REFERENCE_TIME", anchor), patch.dict(check_stock.os.environ, {"BROWSER_STOCK_EVIDENCE_JSON": json.dumps({"schema": 1, "captures": [capture]})}):
            with patch.object(check_stock, "datetime") as clock:
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.now.return_value = anchor + timedelta(hours=1)
                qty, info = check_stock.get_explicit_stock_via_browser_evidence("https://shop.example", "1785", "A")
                clock.now.assert_not_called()
            self.assertEqual(qty, 14)
            self.assertEqual(info["captured_at"], captured.isoformat())
            self.assertEqual(info["quantity_checked_at"], "2026-10-08 20:59:30 KST")
            self.assertEqual(check_stock.get_explicit_stock_via_browser_evidence("https://shop.example", "1785", "A", now=anchor + timedelta(hours=1)), (None, None))
        for timestamp in (anchor - timedelta(seconds=601), anchor + timedelta(seconds=1)):
            capture["captured_at"] = timestamp.isoformat()
            with patch.object(check_stock, "BROWSER_EVIDENCE_REFERENCE_TIME", anchor), patch.dict(check_stock.os.environ, {"BROWSER_STOCK_EVIDENCE_JSON": json.dumps({"schema": 1, "captures": [capture]})}):
                self.assertEqual(check_stock.get_explicit_stock_via_browser_evidence("https://shop.example", "1785", "A"), (None, None))


class TeamConfigurationTests(unittest.TestCase):
    def test_original_lg_sources_are_preserved(self):
        categories = check_stock.team_config("LG트윈스")["categories"]
        self.assertEqual(len(categories), 5)
        self.assertEqual(sum("twinscorestore.co.kr" in url for url, _ in categories), 4)
        self.assertIn(("https://nolmdshop.com/category/LG%ED%8A%B8%EC%9C%88%EC%8A%A4/31/", True), categories)

    def test_new_clubs_have_verified_separate_categories_and_histories(self):
        histories = set()
        for team, category_id in [("LG트윈스", "31"), ("두산베어스", "30"), ("키움히어로즈", "29")]:
            config = check_stock.team_config(team)
            self.assertTrue(any(url.endswith(f"/{category_id}/") and "nolmdshop.com" in url for url, _ in config["categories"]))
            histories.add(config["history"])
        self.assertEqual(len(histories), 3)
        with self.assertRaises(ValueError):
            check_stock.team_config("not-a-team")

    def test_root_level_legacy_history_is_only_used_for_lg(self):
        with patch.object(check_stock.os.path, "exists", side_effect=lambda filename: filename == "stock_history.json"):
            self.assertEqual(check_stock.history_path("LG트윈스"), "stock_history.json")
            self.assertEqual(check_stock.history_path("두산베어스"), "data/stock_history_doosan.json")
            self.assertEqual(check_stock.history_path("키움히어로즈"), "data/stock_history_kiwoom.json")

    def test_export_passes_exact_team_tab_name(self):
        source = inspect.getsource(check_stock.main)
        self.assertIn("sheet_title=STOCK_TEAM", source)
        self.assertIn("if sheet_error:\n        raise SystemExit(1)", source)
        self.assertLess(source.index("save_history(now, current_products)"), source.index("raise SystemExit(1)"))
        self.assertLess(source.index("send_telegram_chunked(token, chat_id, telegram_message)"), source.index("raise SystemExit(1)"))


class HelperTests(unittest.TestCase):
    def test_parse_and_image_helpers(self):
        parsed, diagnostic = check_stock.parse_option_stock({"A": {"option_value": "M", "stock_number": 3}})
        self.assertEqual(parsed, {"M": 3})
        self.assertIsNone(diagnostic)
        parsed, diagnostic = check_stock.parse_option_stock({"A": {"option_value": "M"}})
        self.assertEqual(parsed, {"M": -1})
        self.assertIn(check_stock.UNCERTAIN_MARKER, diagnostic)
        self.assertIsNone(check_stock.parse_option_stock("not json")[0])
        self.assertEqual(check_stock.normalize_image_url("//cdn.example/a.jpg", "http://shop.example/p/1/"), "https://cdn.example/a.jpg")
        self.assertIsNone(check_stock.normalize_image_url("data:image/png,x", "https://shop.example/p/1/"))

    def test_stock_status_requires_option_kind(self):
        self.assertEqual(check_stock.stock_status(3)[1], "확인불가")
        self.assertEqual(check_stock.stock_status(9999)[1], "확인불가")
        self.assertEqual(check_stock.stock_status(3, {"kind": "purchase_limit", "purchase_limit": 3})[1], "구매제한 있음 · 재고 미공개")
        self.assertEqual(check_stock.stock_status(0, {"kind": "stock_unmanaged"})[1], "재고 미관리 · 수량 없음")
        self.assertEqual(check_stock.stock_status(9999, {"kind": "server_stock"})[1], "9999개")
        self.assertEqual(check_stock.stock_status(-1, {"kind": "not_selling"})[1], "구매불가 · 비노출/판매중지")
        self.assertEqual(check_stock.stock_status(-1, {"kind": "quantity_unknown", "diagnostic": "API failed"})[1], "재고 조회 실패 · 수량 미확인")


class ExplicitStockTests(unittest.TestCase):
    def setUp(self):
        # Legacy fake evaluate methods model calculator results, not a native XHR.
        self.xhr_patch = patch.object(check_stock, "get_explicit_stock_via_browser_xhr", return_value=(None, "browser-xhr: unsupported_xhr", None))
        self.xhr_mock = self.xhr_patch.start()
        self.addCleanup(self.xhr_patch.stop)

    def test_explicit_server_stock_requires_matching_item_and_valid_integer(self):
        self.assertEqual(check_stock._explicit_server_stock({"sItemCode": "A", "product_no": 1785, "stock_number": 14}, "A", "1785"), 14)
        self.assertEqual(check_stock._explicit_server_stock({"A": {"item_code": "A", "product_no": 1785, "stock_number": "1"}}, "A", "1785"), 1)
        for value in (-1, True, 1.5, "1.5", "", None):
            self.assertIsNone(check_stock._explicit_server_stock({"sItemCode": "A", "stock_number": value}, "A", "1785"))
        self.assertIsNone(check_stock._explicit_server_stock({"sItemCode": "OTHER", "stock_number": 14}, "A", "1785"))
        self.assertIsNone(check_stock._explicit_server_stock({"sItemCode": "A", "product_no": 9, "stock_number": 14}, "A", "1785"))

    def test_same_context_request_fallback_gating_and_recovery(self):
        page = SimpleNamespace(url="https://shop.example/product/x/1/", evaluate=Mock(), request=SimpleNamespace(get=Mock()))
        items = {"A": {"option_value": "M", "use_stock": "T"}}
        def outcome(kind="quantity_unknown", managed=True, cap=None, qty=-2):
            return {"data": {"M": qty}, "option_info": {"M": {"kind": kind, "availability": "selectable", "stock_managed": managed, "purchase_limit": cap}}}
        page.evaluate.return_value = outcome()
        response = SimpleNamespace(status=200, ok=True, url="https://shop.example/api", headers={"content-type": "application/json"}, json=Mock(return_value={"sItemCode": "A", "product_no": "1", "stock_number": 14}), dispose=Mock())
        page.request.get.return_value = response
        data, _ = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", items)
        self.assertEqual(data["M"], 14)
        headers = page.request.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Referer"], page.url)
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertIn("application/json", headers["Accept"])
        self.assertEqual(page.evaluate.call_count, 2)
        self.assertEqual(page.evaluate.call_args_list[-1].args[0], "info => { window.__lg_stock_option_info = info; }")
        for kind, managed, cap, qty in [("not_selling", True, None, -1), ("purchase_limit", True, 3, -1), ("quantity_unknown", False, None, -1), ("quantity_unknown", None, None, -1)]:
            page.evaluate.reset_mock()
            page.evaluate.return_value = outcome(kind, managed, cap, qty)
            page.request.get.reset_mock()
            check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", items)
            page.request.get.assert_not_called()

    def test_html_redirect_is_rejected_before_json_parsing(self):
        response = SimpleNamespace(status=200, ok=True,
            url="https://veritas-hub.cafe24.com/error?secret=hidden",
            headers={"content-type": "text/html; charset=utf-8"},
            json=Mock(side_effect=ValueError("raw secret HTML")), dispose=Mock())
        page = SimpleNamespace(url="https://shop.example/product/x/1/", request=SimpleNamespace(get=Mock(return_value=response)))
        exact, note = check_stock.get_explicit_stock_via_request(page, "https://shop.example", "1", "A")
        self.assertIsNone(exact)
        self.assertIn("unexpected_response_origin", note)
        self.assertIn("HTTP=200", note)
        self.assertIn("type=text/html", note)
        self.assertIn("response-host=veritas-hub.cafe24.com", note)
        self.assertNotIn("secret", note)
        self.assertNotIn("https://", note)
        response.json.assert_not_called()
        response.dispose.assert_called_once()
        self.assertEqual(page.request.get.call_count, 1)

    def test_same_origin_html_is_not_parsed_as_stock(self):
        response = SimpleNamespace(status=200, ok=True, url="https://shop.example/api",
            headers={"Content-Type": "text/html"}, text=Mock(return_value="  <!doctype html><html>not JSON</html>"), json=Mock(return_value={"sItemCode": "A", "stock_number": 14}), dispose=Mock())
        page = SimpleNamespace(url="https://shop.example/product/x/1/?token=secret#private", request=SimpleNamespace(get=Mock(return_value=response)))
        exact, note = check_stock.get_explicit_stock_via_request(page, "https://shop.example", "1", "A")
        self.assertIsNone(exact)
        self.assertIn("non_json_response", note)
        response.json.assert_not_called()
        self.assertEqual(page.request.get.call_args.kwargs["headers"]["Referer"], "https://shop.example/product/x/1/")

    def test_same_origin_mislabeled_json_is_accepted(self):
        payload = {"sItemCode": "A", "product_no": "1", "stock_number": 14}
        for headers in ({"content-type": "text/html"}, {}):
            response = SimpleNamespace(status=200, ok=True, url="https://shop.example/api",
                headers=headers, text=Mock(return_value="  " + json.dumps(payload)),
                json=Mock(return_value=payload), dispose=Mock())
            page = SimpleNamespace(url="https://shop.example/product/x/1/", request=SimpleNamespace(get=Mock(return_value=response)))
            exact, note = check_stock.get_explicit_stock_via_request(page, "https://shop.example", "1", "A")
            self.assertEqual(exact, 14)
            self.assertIsNone(note)
            response.text.assert_called_once()
            response.json.assert_called_once()

    def test_cross_origin_and_single_item_do_not_issue_requests(self):
        page = SimpleNamespace(url="https://other.example/product/x/1/", request=SimpleNamespace(get=Mock()))
        self.assertIn("same_origin_required", check_stock.get_explicit_stock_via_request(page, "https://shop.example", "1", "A")[1])
        page.url = "https://shop.example/product/x/1/"
        self.assertIn("unsupported_single_item", check_stock.get_explicit_stock_via_request(page, "https://shop.example", "1", "__single")[1])
        page.evaluate = Mock(return_value={"data": {"재고": -2}, "option_info": {"재고": {"kind": "quantity_unknown", "stock_managed": True}}})
        check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", {"__single": {"use_stock": "T"}})
        page.request.get.assert_not_called()

    def test_partial_failure_preserves_exact_counts_and_diagnostics(self):
        items = {"A": {"option_value": "M"}, "B": {"option_value": "L"}}
        infos = {"M": {"kind": "server_stock", "stock_managed": True},
                 "L": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}
        response = SimpleNamespace(status=200, ok=True, url="https://shop.example/api",
            headers={"content-type": "text/html"}, text=Mock(return_value="<html>error</html>"), json=Mock(), dispose=Mock())
        page = SimpleNamespace(url="https://shop.example/product/x/1/", evaluate=Mock(return_value={"data": {"M": 14, "L": -2}, "option_info": infos}), request=SimpleNamespace(get=Mock(return_value=response)))
        data, note = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", items)
        self.assertEqual(data, {"M": 14, "L": -2})
        self.assertIn("non_json_response", note)
        self.assertNotIn(check_stock.UNCERTAIN_MARKER, note)
        self.assertEqual(infos["L"]["kind"], "quantity_unknown")
        self.assertEqual(infos["L"]["error"], "network_failure")
        self.assertIn("HTTP=200", infos["L"]["diagnostic"])
        self.assertNotIn("diagnostic", infos["M"])
        self.assertEqual(page.evaluate.call_args.args[1], infos)
        self.assertEqual(check_stock.stock_status(-2, infos["L"])[1], "구매가능 · 수량조회실패")
        self.assertEqual(check_stock.stock_status(-2, {"kind": "quantity_unknown", "availability": "selectable"})[1], "구매가능 · 수량미확인")

    def test_unexpected_request_failure_is_not_silently_dropped(self):
        infos = {"M": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}
        page = SimpleNamespace(url="https://shop.example/product/x/1/", evaluate=Mock(return_value={"data": {"M": -2}, "option_info": infos}), request=SimpleNamespace(get=Mock(side_effect=RuntimeError("cookie=secret https://private.example"))))
        from unittest.mock import patch
        with patch("check_stock.time.sleep"):
            data, note = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", {"A": {"option_value": "M"}})
        self.assertEqual(data, {"M": -2})
        self.assertIn("RuntimeError", note)
        self.assertNotIn("secret", note)
        self.assertNotIn("https://", note)
        self.assertEqual(infos["M"]["error"], "network_failure")
        self.assertNotIn(check_stock.UNCERTAIN_MARKER, note)

    def test_browser_evidence_validation(self):
        now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        url = "https://shop.example/exec/front/shop/CalculatorProduct?product_no=1785&product%5BA%5D=9999"
        capture = {"product_no": "1785", "item_code": "A", "captured_at": (now - timedelta(seconds=30)).isoformat(),
                   "request_url": url, "response_url": url, "content_type": "application/json; charset=utf-8",
                   "api_payload": {"sItemCode": "A", "product_no": "1785", "stock_number": 14}}
        def lookup(record, schema=1):
            with patch.dict(check_stock.os.environ, {"BROWSER_STOCK_EVIDENCE_JSON": json.dumps({"schema": schema, "captures": [record]})}):
                return check_stock.get_explicit_stock_via_browser_evidence("https://shop.example", "1785", "A", now)
        qty, info = lookup(capture)
        self.assertEqual(qty, 14)
        self.assertEqual(info["quantity_checked_at"], "2026-10-08 20:59:30 KST")
        invalid = [
            {"captured_at": (now - timedelta(seconds=601)).isoformat()},
            {"captured_at": (now + timedelta(seconds=1)).isoformat()},
            {"captured_at": "2026-10-08T11:59:30"},
            {"response_url": url.replace("shop.example", "other.example")},
            {"request_url": url.replace("shop.example", "other.example")},
            {"request_url": url.replace("product_no=1785", "product_no=9")},
            {"request_url": url.replace("product%5BA%5D", "product%5BB%5D")},
            {"response_url": "https://shop.example/error"},
            {"product_no": "9"}, {"item_code": "B"}, {"content_type": "text/html"},
            {"api_payload": {"sItemCode": "B", "stock_number": 14}},
            {"api_payload": {"sItemCode": "A", "product_no": "9", "stock_number": 14}},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                self.assertEqual(lookup({**capture, **overrides}), (None, None))
        self.assertEqual(lookup(capture, schema=True), (None, None))
        for raw in ("", "not json", json.dumps({"schema": 1, "captures": [capture] * 1001}), "x" * 1_000_001):
            with patch.dict(check_stock.os.environ, {"BROWSER_STOCK_EVIDENCE_JSON": raw}):
                self.assertEqual(check_stock.get_explicit_stock_via_browser_evidence("https://shop.example", "1785", "A", now), (None, None))

    def test_browser_evidence_is_last_tier_and_zero_is_not_soldout(self):
        from copy import deepcopy
        url = "https://shop.example/exec/front/shop/CalculatorProduct?product_no=1785&product%5BA%5D=9999"
        capture = {"product_no": "1785", "item_code": "A", "captured_at": check_stock.BROWSER_EVIDENCE_REFERENCE_TIME.isoformat(),
                   "request_url": url, "response_url": url, "content_type": "application/json",
                   "api_payload": {"sItemCode": "A", "product_no": "1785", "stock_number": 14}}
        base = {"data": {"M": -2}, "option_info": {"M": {"kind": "quantity_unknown", "availability": "selectable", "stock_managed": True}}}
        items = {"A": {"option_value": "M", "use_stock": "T"}}
        for evidence_qty, expected in [(14, 14), (0, -2)]:
            capture["api_payload"]["stock_number"] = evidence_qty
            outcome = deepcopy(base)
            page = SimpleNamespace(evaluate=Mock(return_value=outcome))
            with patch.dict(check_stock.os.environ, {"BROWSER_STOCK_EVIDENCE_JSON": json.dumps({"schema": 1, "captures": [capture]})}), patch.object(check_stock, "get_explicit_stock_via_request", return_value=(None, "request-fallback: non_json_response")):
                data, note = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1785", items)
            self.assertEqual(data["M"], expected)
            info = outcome["option_info"]["M"]
            if evidence_qty:
                self.assertIsNone(note)
                self.assertEqual(info["source"], "verified_browser")
                self.assertEqual(info["kind"], "server_stock")
                self.assertIn("quantity_checked_at", info)
                self.assertIn("non_json_response", info["primary_diagnostic"])
                self.assertNotIn("diagnostic", info)
                self.assertNotIn("error", info)
                self.assertEqual(info["lookup_path"][-1], "verified_browser")
            else:
                self.assertEqual(info["kind"], "quantity_unknown")
                self.assertIn("non_json_response", note)

    def test_primary_results_never_consult_browser_evidence(self):
        items = {"A": {"option_value": "M", "use_stock": "T"}}
        for kind, qty in [("server_stock", 21), ("not_selling", -1), ("soldout", 0), ("purchase_limit", -2)]:
            page = SimpleNamespace(evaluate=Mock(return_value={"data": {"M": qty}, "option_info": {"M": {"kind": kind, "stock_managed": True, "availability": "selectable"}}}))
            with patch.object(check_stock, "get_explicit_stock_via_request") as request, patch.object(check_stock, "get_explicit_stock_via_browser_evidence") as evidence:
                data, _ = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1785", items)
                self.assertEqual(data["M"], qty)
                request.assert_not_called()
                evidence.assert_not_called()
                self.xhr_mock.assert_not_called()
        page = SimpleNamespace(evaluate=Mock(return_value={"data": {"M": -2}, "option_info": {"M": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}}))
        with patch.object(check_stock, "get_explicit_stock_via_request", return_value=(17, None)), patch.object(check_stock, "get_explicit_stock_via_browser_evidence") as evidence:
            data, _ = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1785", items)
            self.assertEqual(data["M"], 17)
            evidence.assert_not_called()
            self.xhr_mock.assert_not_called()

    def test_legacy_history_is_not_a_schema_two_baseline(self):
        source = inspect.getsource(check_stock.main)
        self.assertIn('history.get("inventory_schema") != 2', source)
        self.assertIn('previous_entry.get("inventory_schema") != 2', source)
        self.assertIn('"inventory_schema": 2', inspect.getsource(check_stock.save_history))


class BrowserXHRTests(unittest.TestCase):
    domain = "https://shop.example"

    def response(self, quantity=82, **overrides):
        result = {"status": 200,
                  "responseURL": self.domain + "/exec/front/shop/CalculatorProduct?product_no=2792&is_subscription=F&product%5BA%5D=9999",
                  "contentType": "application/json; charset=utf-8",
                  "responseText": json.dumps({"sItemCode": "A", "product_no": "2792", "stock_number": quantity}),
                  "capturedAt": datetime.now(timezone.utc).isoformat()}
        result.update(overrides)
        return result

    def page(self, result):
        return SimpleNamespace(url=self.domain + "/product/glass/2792/", evaluate=Mock(return_value=result))

    def test_xhr_returns_explicit_server_stock_and_completion_timestamp(self):
        page = self.page(self.response())
        qty, note, capture = check_stock.get_explicit_stock_via_browser_xhr(page, self.domain, "2792", "A")
        self.assertEqual(qty, 82)
        self.assertIsNone(note)
        self.assertRegex(capture["quantity_checked_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} KST$")
        self.assertIsNotNone(datetime.fromisoformat(capture["captured_at"]).tzinfo)
        self.assertEqual(page.evaluate.call_args.args[1]["itemCode"], "A")

    def test_xhr_rejects_redirect_html_wrong_item_and_invalid_payload(self):
        invalid = [
            ({"responseURL": self.response()["responseURL"].replace("shop.example", "other.example")}, "unexpected_response_origin"),
            ({"responseURL": self.response()["responseURL"].replace("shop.example", "shop.example:444")}, "unexpected_response_origin"),
            ({"responseURL": self.domain + "/error"}, "unexpected_response_endpoint"),
            ({"responseURL": self.response()["responseURL"].replace("product_no=2792", "product_no=9")}, "unexpected_response_product_item"),
            ({"contentType": "text/html", "responseText": "<html>secret</html>"}, "non_json_response"),
            ({"contentType": "application /json"}, "non_json_response"),
            ({"responseText": "<html>secret</html>"}, "non_json_response"),
            ({"responseText": "{broken"}, "malformed_json"),
            ({"responseText": json.dumps({"sItemCode": "B", "stock_number": 89})}, "no_matching_explicit_stock"),
            ({"responseText": json.dumps({"sItemCode": "A", "product_no": 9, "stock_number": 89})}, "no_matching_explicit_stock"),
            ({"responseText": json.dumps({"sItemCode": "A", "Result": True, "message": "9999", "max": 89})}, "no_matching_explicit_stock"),
            ({"status": 500}, "HTTPError"),
            ({"status": True}, "HTTPError"),
            ({"error": "timeout"}, "timeout"),
            ({"error": "unsupported_xhr"}, "unsupported_xhr"),
            ({"capturedAt": "bad"}, "ValueError"),
        ]
        for overrides, reason in invalid:
            with self.subTest(reason=reason, overrides=overrides):
                qty, note, capture = check_stock.get_explicit_stock_via_browser_xhr(self.page(self.response(**overrides)), self.domain, "2792", "A")
                self.assertIsNone(qty)
                self.assertIsNone(capture)
                self.assertIn(reason, note)
                self.assertNotIn("secret", note)

    def test_xhr_input_gate_and_malformed_results_are_conservative(self):
        for page_url, product, item in [("https://other.example/product/x/2792/", "2792", "A"), (self.domain + "/product/x/9/", "2792", "A"), (self.domain + "/product/x/2792/", "2792", "__single"), (self.domain + "/product/x/2792/", "bad", "A"), (self.domain + "/product/x/2792/", "2792", "A]&x=1")]:
            page = SimpleNamespace(url=page_url, evaluate=Mock())
            self.assertIsNone(check_stock.get_explicit_stock_via_browser_xhr(page, self.domain, product, item)[0])
            page.evaluate.assert_not_called()
        for result in (None, [], "bad", {}, {"responseURL": 1}):
            self.assertIsNone(check_stock.get_explicit_stock_via_browser_xhr(self.page(result), self.domain, "2792", "A")[0])
        page = self.page(None)
        page.evaluate.side_effect = RuntimeError("cookie=secret")
        self.assertNotIn("secret", check_stock.get_explicit_stock_via_browser_xhr(page, self.domain, "2792", "A")[1])

    def test_xhr_auto_recovers_fixture_counts_without_environment(self):
        # Historical glass/kids quantities are fixtures only, never production constants.
        for fixture_qty in (89, 86, 2, 6, 2):
            outcome = {"data": {"M": -2}, "option_info": {"M": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}}
            page = self.page(None)
            page.evaluate.side_effect = [outcome, self.response(fixture_qty), None]
            with patch.object(check_stock, "get_explicit_stock_via_request", return_value=(None, "request-fallback: non_json_response")), patch.object(check_stock, "get_explicit_stock_via_browser_evidence") as evidence, patch.dict(check_stock.os.environ, {}, clear=True):
                data, note = check_stock.get_option_stock_via_calculator(page, self.domain, "2792", {"A": {"option_value": "M"}})
            self.assertEqual(data["M"], fixture_qty)
            self.assertIsNone(note)
            evidence.assert_not_called()
            info = outcome["option_info"]["M"]
            self.assertEqual(info["source"], "browser_xhr")
            self.assertEqual(info["kind"], "server_stock")
            self.assertEqual(info["lookup_path"], ["page", "calculator", "request_fallback", "browser_xhr"])
            self.assertIn("non_json_response", info["primary_diagnostic"])
            self.assertNotIn("error", info)
            self.assertNotIn("diagnostic", info)

    def test_xhr_selected_zero_remains_unknown_and_recent_capture_is_last_tier(self):
        for native_qty, env_qty, expected in [(0, None, -2), (None, 14, 14), (0, 14, 14)]:
            outcome = {"data": {"M": -2}, "option_info": {"M": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}}
            page = self.page(None)
            page.evaluate.side_effect = [outcome, self.response(native_qty) if native_qty is not None else {"error": "timeout"}, None]
            with patch.object(check_stock, "get_explicit_stock_via_request", return_value=(None, "request-fallback: non_json_response")), patch.object(check_stock, "get_explicit_stock_via_browser_evidence", return_value=(env_qty, {"quantity_checked_at": "2026-10-08 21:00:00 KST"})) as evidence:
                data, note = check_stock.get_option_stock_via_calculator(page, self.domain, "2792", {"A": {"option_value": "M"}})
            self.assertEqual(data["M"], expected)
            evidence.assert_called_once()
            info = outcome["option_info"]["M"]
            self.assertIn("browser_xhr", info["lookup_path"])
            self.assertIn("non_json_response", info["primary_diagnostic"])
            self.assertIn("browser-xhr", info["browser_xhr_diagnostic"])
            if env_qty:
                self.assertEqual(info["source"], "verified_browser")
                self.assertEqual(info["lookup_path"][-2:], ["browser_xhr", "verified_browser"])
                self.assertIsNone(note)
            else:
                self.assertEqual(info["kind"], "quantity_unknown")
                self.assertIn("selectable_zero_not_confirmed", note)
                self.assertIn("non_json_response", note)

    def test_xhr_skips_blocked_and_primary_success_paths(self):
        cases = [("server_stock", True, None, "selectable", 14), ("soldout", True, None, "soldout", 0), ("not_selling", True, None, "selectable", -1), ("made_to_order", True, None, "selectable", -2), ("quantity_unknown", False, None, "selectable", -2), ("quantity_unknown", True, 3, "selectable", -2), ("quantity_unknown", True, None, "unknown", -1)]
        for kind, managed, cap, availability, qty in cases:
            page = self.page({"data": {"M": qty}, "option_info": {"M": {"kind": kind, "stock_managed": managed, "purchase_limit": cap, "availability": availability}}})
            with patch.object(check_stock, "get_explicit_stock_via_request", return_value=(None, "request-fallback: failed")), patch.object(check_stock, "get_explicit_stock_via_browser_xhr") as xhr, patch.object(check_stock, "get_explicit_stock_via_browser_evidence") as evidence:
                check_stock.get_option_stock_via_calculator(page, self.domain, "2792", {"A": {"option_value": "M"}})
                xhr.assert_not_called()
                evidence.assert_not_called()


class ProductionJavaScriptHarness(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for production JS harness")
    def test_actual_native_xhr_request_and_failure_events(self):
        source = inspect.getsource(check_stock.get_explicit_stock_via_browser_xhr)
        js = source.split('js = r"""', 1)[1].split('"""', 1)[0]
        harness = r'''const fn=__FUNCTION__;
(async()=>{let count=0;
for(const category of [undefined,104,'31',0,-1,'bad']){
 let req;
 class XHR {constructor(){this.status=200;this.responseText='{"sItemCode":"A","stock_number":82}';this.headers={};}open(method,url,async){req=this;Object.assign(this,{method,url,async,responseURL:url});}setRequestHeader(k,v){this.headers[k]=v;}getResponseHeader(){return 'application/json';}send(){this.onload();}}
 global.window={location:{href:'https://shop.example/product/glass/2792/'},iCategoryNo:category,XMLHttpRequest:XHR};
 const out=await fn({domain:'https://shop.example',productNo:'2792',itemCode:'A'}),q=new URL(req.url).searchParams;
 if(out.status!==200||out.error||req.method!=='GET'||req.async!==true||req.timeout!==8000||req.withCredentials!==undefined||req.headers['X-Requested-With']!=='XMLHttpRequest'||req.headers.Accept!=='application/json, text/javascript, */*; q=0.01')throw Error('request mismatch');
 if(q.get('product[A]')!=='9999'||q.get('is_subscription')!=='F'||q.get('product_no')!=='2792'||q.get('cate_no')!==([104,'31'].includes(category)?String(category):null))throw Error('query mismatch');
 if(Number.isNaN(Date.parse(out.capturedAt)))throw Error('completion missing');count++;
}
for(const [event,error] of [['ontimeout','timeout'],['onerror','network_failure'],['onabort','aborted']]){
 class XHR {open(){}setRequestHeader(){}getResponseHeader(){return '';}send(){this[event]();}}
 global.window={location:{href:'https://shop.example/product/x/2792/'},XMLHttpRequest:XHR};
 if((await fn({domain:'https://shop.example',productNo:'2792',itemCode:'A'})).error!==error)throw Error('failure mismatch');count++;
}
for(const href of ['https://other.example/product/x/2792/','https://shop.example:444/product/x/2792/','https://shop.example/product/x/9/']){
 global.window={location:{href},XMLHttpRequest:class{constructor(){throw Error('must not construct');}}};
 if((await fn({domain:'https://shop.example',productNo:'2792',itemCode:'A'})).error!=='same_origin_product_required')throw Error('gate mismatch');count++;
}
global.window={location:{href:'https://shop.example/product/x/2792/'}};
if((await fn({domain:'https://shop.example',productNo:'2792',itemCode:'A'})).error!=='unsupported_xhr')throw Error('unsupported mismatch');count++;
console.log('passed '+count+' native XHR cases');})().catch(e=>{console.error(e);process.exit(1)});'''.replace('__FUNCTION__', js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("passed 13", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for production JS harness")
    def test_actual_calculator_js_policy_cases(self):
        source = inspect.getsource(check_stock.get_option_stock_via_calculator)
        js = source.split('js = r"""', 1)[1].split('"""', 1)[0]
        harness = r'''const fn=__FUNCTION__;
async function run(c){const option={A:{option_value:'M',use_stock:c.useStock,is_selling:c.isSelling===false?'F':'T',stock_number:c.pageStock}};const sel={disabled:false,closest:()=>null,options:[{value:'A',textContent:c.text||'M',disabled:false}]};global.document={querySelectorAll:selector=>selector.startsWith('script')?[]:[sel]};global.window={product_max:c.max??0,product_max_type:c.maxType??'F',is_soldout_icon:c.sold??'F',use_stock:c.globalUse??'T'};global.fetch=async()=>({ok:true,json:async()=>c.api??{sItemCode:'A',product_no:'77',stock_number:c.apiQty??14}});return (await fn({optionDataJson:option,productNo:c.productNo||'77',domain:c.domain||'https://shop.example'})).data.M;}
(async()=>{const cases=[['page exact',{useStock:'T',pageStock:7},7],['managed uncapped API 14',{useStock:'T'},14],['purchase cap',{useStock:'T',max:3,maxType:'T'},-2],['unmanaged zero ignored',{useStock:'F',pageStock:0},-2],['unmanaged positive ignored',{useStock:'F',pageStock:14},-2],['unknown management API ignored',{useStock:undefined,globalUse:'?'},-2],['made order explicit override',{useStock:'F',pageStock:0,domain:'https://nolmdshop.com',productNo:'2812'},-2],['DOM soldout wins',{useStock:'F',text:'M [품절]'},0],['selectable zero stays unknown',{useStock:'T',pageStock:0,apiQty:0},-2],['numeric 9999 is exact',{useStock:'T',pageStock:9999},9999],['not selling',{useStock:'T',isSelling:false},-1]];for(const [n,c,w] of cases){if(c.isSelling===false) c.api={sItemCode:'A',stock_number:14};const got=await run(c);if(got!==w)throw Error(n+': got '+got+' expected '+w)}console.log('passed '+cases.length+' policy cases')})().catch(e=>{console.error(e);process.exit(1)});'''.replace('__FUNCTION__', js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("passed 11", result.stdout)


    @unittest.skipUnless(shutil.which("node"), "Node.js required for production JS harness")
    def test_actual_verified_flags_and_structured_availability(self):
        source = inspect.getsource(check_stock.get_option_stock_via_calculator)
        js = source.split('js = r"""', 1)[1].split('"""', 1)[0]
        harness = r'''const fn=__FUNCTION__;
const offer=(availability, overrides={})=>({'@type':'ProductGroup',hasVariant:[{'@type':'Product',sku:'cafe24_ticketmdshop_1_1782_A',offers:{url:'https://shop.example/product/black/1782/?item_code=A',availability:'https://schema.org/'+availability},...overrides}]});
async function run(c={}){
 const items=c.items||{A:{option_value:'M',use_stock:'T',is_selling:'T',is_display:'T',...c.flags}};
 const sel={disabled:false,closest:()=>null,options:c.dom||[]};
 global.document={querySelectorAll:selector=>selector.startsWith('script')?(c.ld||[]).map(x=>({textContent:JSON.stringify(x)})):[sel]};
 global.window={use_stock:'T',is_soldout_icon:'F',product_max:0,product_max_type:'F'};
 let calls=0;
 global.fetch=async()=>{calls++;return {ok:true,json:async()=>c.api||{}}};
 const result=await fn({optionDataJson:items,productNo:c.productNo||'1782',domain:c.domain||'https://shop.example'});
 return {...result,calls};
}
function assert(ok, message){if(!ok)throw Error(message);}
(async()=>{
 // Observed 1782 M/XXL: hidden, not selling, managed, no matching DOM option.
 let r=await run({flags:{is_display:'F',is_selling:'F'}});
 assert(r.data.M===-1&&r.option_info.M.kind==='not_selling'&&r.option_info.M.availability==='not_selling'&&r.calls===0,'invisible blocked option');
 assert(r.option_info.M.displayed===false&&r.option_info.M.selling===false&&r.option_info.M.stock_managed===true,'normalized flags');
 r=await run({flags:{is_display:'F',is_selling:'T'}});
 assert(r.data.M===-1&&r.option_info.M.kind==='not_selling'&&r.calls===0,'display F selling T');
 for(const c of [{flags:{is_selling:'F',use_stock:'F'}},{flags:{is_selling:'F',use_stock:'F'},domain:'https://nolmdshop.com',productNo:'2812'}]){
  r=await run(c);assert(r.option_info.M.kind==='not_selling'&&r.data.M===-1&&r.calls===0,'blocked outranks policy');
 }
 r=await run({flags:{is_display:'F',is_selling:'F'},dom:[{value:'A',textContent:'M [품절]',disabled:true}]});
 assert(r.data.M===0&&r.option_info.M.kind==='soldout'&&r.option_info.M.availability_source==='dom'&&r.calls===0,'explicit DOM soldout wins');
 r=await run({ld:[offer('InStock')]});
 assert(r.data.M===-2&&r.option_info.M.kind==='quantity_unknown'&&r.option_info.M.availability==='selectable'&&r.option_info.M.availability_source==='jsonld','JSONLD availability not quantity');
 r=await run({ld:[offer('OutOfStock')]});
 assert(r.data.M===0&&r.option_info.M.kind==='soldout'&&r.calls===0,'JSONLD soldout');
 for(const ld of [offer('InStock',{sku:null}),offer('InStock',{sku:'x_9_A'}),offer('InStock',{offers:{url:'https://other.example/product/black/1782/?item_code=A',availability:'https://schema.org/InStock'}}),offer('InStock',{offers:{url:'https://shop.example/product/black/9/?item_code=A',availability:'https://schema.org/InStock'}}),offer('InStock',{offers:{url:'https://shop.example/product/black/1782/?item_code=B',availability:'https://schema.org/InStock'}})]){
  r=await run({ld:[ld]});assert(r.data.M===-1&&r.option_info.M.availability==='unknown','unmatched offer rejected');
 }
 r=await run({flags:{is_display:'F'},ld:[offer('InStock')]});
 assert(r.data.M===-1&&r.option_info.M.kind==='not_selling'&&r.option_info.M.availability_source==='page_flags'&&r.calls===0,'JSONLD cannot override blocked flags');
 r=await run({flags:{stock_number:14},ld:[offer('OutOfStock')]});
 assert(r.data.M===14&&r.option_info.M.source==='page'&&r.calls===0,'exact page quantity preserved');
 r=await run({items:{A:{option_value:'M',use_stock:'T',stock_number:14},B:{option_value:'L',use_stock:'T'}},dom:[{value:'B',textContent:'L',disabled:false}]});
 assert(r.data.M===14&&r.data.L===-2&&r.option_info.M.kind==='server_stock'&&r.option_info.L.kind==='quantity_unknown'&&r.calls===1,'other option failure isolated');
 console.log('passed verified regression cases');
})().catch(e=>{console.error(e);process.exit(1)});'''.replace('__FUNCTION__', js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("passed verified regression cases", result.stdout)


if __name__ == "__main__":
    unittest.main()
