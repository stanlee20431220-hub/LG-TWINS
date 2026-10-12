import inspect
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
        self.assertEqual(page.evaluate.call_count, 2)
        self.assertEqual(page.evaluate.call_args_list[-1].args[0], "info => { window.__lg_stock_option_info = info; }")
        for kind, managed, cap, qty in [("purchase_limit", True, 3, -1), ("quantity_unknown", False, None, -1), ("quantity_unknown", None, None, -1)]:
            page.evaluate.reset_mock()
            page.evaluate.return_value = outcome(kind, managed, cap, qty)
            page.request.get.reset_mock()
            check_stock.get_option_stock_via_calculator(page, "https://shop.example", "1", items)
            page.request.get.assert_not_called()

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
async function run(c){const option={A:{option_value:'M',use_stock:c.useStock,is_selling:c.isSelling===false?'F':'T',stock_number:c.pageStock}};const sel={disabled:false,closest:()=>null,options:[{value:'A',textContent:c.text||'M',disabled:false}]};global.document={querySelectorAll:()=>[sel]};global.window={product_max:c.max??0,product_max_type:c.maxType??'F',is_soldout_icon:c.sold??'F',use_stock:c.globalUse??'T'};global.fetch=async()=>({ok:true,json:async()=>c.api??{sItemCode:'A',product_no:'77',stock_number:c.apiQty??14}});return (await fn({optionDataJson:option,productNo:c.productNo||'77',domain:c.domain||'https://shop.example'})).data.M;}
(async()=>{const cases=[['page exact',{useStock:'T',pageStock:7},7],['managed uncapped API 14',{useStock:'T'},14],['purchase cap',{useStock:'T',max:3,maxType:'T'},-2],['unmanaged zero ignored',{useStock:'F',pageStock:0},-2],['unmanaged positive ignored',{useStock:'F',pageStock:14},-2],['unknown management API ignored',{useStock:undefined,globalUse:'?'},-2],['made order explicit override',{useStock:'F',pageStock:0,domain:'https://nolmdshop.com',productNo:'2812'},-2],['DOM soldout wins',{useStock:'F',text:'M [품절]'},0],['selectable zero stays unknown',{useStock:'T',pageStock:0,apiQty:0},-2],['numeric 9999 is exact',{useStock:'T',pageStock:9999},9999],['not selling',{useStock:'T',isSelling:false},-1]];for(const [n,c,w] of cases){if(c.isSelling===false) c.api={sItemCode:'A',stock_number:14};const got=await run(c);if(got!==w)throw Error(n+': got '+got+' expected '+w)}console.log('passed '+cases.length+' policy cases')})().catch(e=>{console.error(e);process.exit(1)});'''.replace('__FUNCTION__', js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("passed 11", result.stdout)


if __name__ == "__main__":
    unittest.main()
