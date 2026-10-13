import inspect
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import check_stock


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
        capture = {"product_no": "1785", "item_code": "A", "captured_at": datetime.now(timezone.utc).isoformat(),
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
        page = SimpleNamespace(evaluate=Mock(return_value={"data": {"M": -2}, "option_info": {"M": {"kind": "quantity_unknown", "stock_managed": True, "availability": "selectable"}}}))
        with patch.object(check_stock, "get_explicit_stock_via_request", return_value=(17, None)), patch.object(check_stock, "get_explicit_stock_via_browser_evidence") as evidence:
            data, _ = check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1785", items)
            self.assertEqual(data["M"], 17)
            evidence.assert_not_called()

    def test_legacy_history_is_not_a_schema_two_baseline(self):
        source = inspect.getsource(check_stock.main)
        self.assertIn('history.get("inventory_schema") != 2', source)
        self.assertIn('previous_entry.get("inventory_schema") != 2', source)
        self.assertIn('"inventory_schema": 2', inspect.getsource(check_stock.save_history))


class ProductionJavaScriptHarness(unittest.TestCase):
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
