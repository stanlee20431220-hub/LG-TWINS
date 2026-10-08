import unittest
import inspect
import ast
import shutil
import subprocess

import check_stock


class ScraperHelperTests(unittest.TestCase):
    def test_unknown_option_field_is_not_false_soldout(self):
        parsed, diagnostic = check_stock.parse_option_stock({
            "item-code": {"option_value": "M", "unexpected": "value"}
        })
        self.assertEqual(parsed, {"M": -1})
        self.assertIn(check_stock.UNCERTAIN_MARKER, diagnostic)
        self.assertFalse(check_stock.is_fully_sold_out(parsed))

    def test_bad_json_is_uncertain_failure(self):
        parsed, diagnostic = check_stock.parse_option_stock("not json")
        self.assertIsNone(parsed)
        self.assertIn(check_stock.UNCERTAIN_MARKER, diagnostic)

    def test_image_url_is_absolute_https(self):
        self.assertEqual(
            check_stock.normalize_image_url(
                "//cdn.example.com/images/a.jpg?size=large",
                "http://shop.example/product/a/1/",
            ),
            "https://cdn.example.com/images/a.jpg?size=large",
        )
        self.assertEqual(
            check_stock.normalize_image_url(
                "/images/a.jpg", "https://shop.example/product/a/1/"
            ),
            "https://shop.example/images/a.jpg",
        )
        self.assertIsNone(
            check_stock.normalize_image_url(
                "data:image/png;base64,abc", "https://shop.example/product/a/1/"
            )
        )

    def test_cap_and_unknown_display(self):
        self.assertEqual(check_stock.stock_status(-1)[1], "확인불가")
        self.assertEqual(check_stock.stock_status(-2)[1], "선택가능 · 수량 미확인")
        self.assertEqual(check_stock.stock_status(-3)[1], "확인불가")
        self.assertFalse(check_stock.is_fully_sold_out({"M": -2}))
        self.assertIn("실제 재고 아님", check_stock.stock_status(9999)[1])


@unittest.skipUnless(shutil.which("node"), "Node.js is required for real CalculatorProduct JS tests")
class CalculatorJsTests(unittest.TestCase):
    def test_real_js_response_validation_retries_and_unknowns(self):
        tree = ast.parse(inspect.getsource(check_stock.get_option_stock_via_calculator))
        js = next(node.value.value for node in ast.walk(tree)
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "js" for target in node.targets))
        harness = "const calculate = (" + js + ");\n" + r'''
const assert = (condition, message) => { if (!condition) throw new Error(message); };
// Exercise the production JS without real network delays or unexpired timeout handles.
globalThis.setTimeout = (callback, delay) => { if (delay < 1000) queueMicrotask(callback); return 1; };
globalThis.clearTimeout = () => {};
const code = 'P000TEST000A';
const exact = qty => ({Result:false, msg:'상품의 수량이 재고수량 보다 많습니다.', sItemCode:code, stock_number:qty});
const inventoryFailure = {Result:false, msg:'재고수량 초과', sItemCode:code};
async function run(replies, {selectable=true, additional=false, soldout=false}={}) {
  let calls = 0;
  globalThis.document = {querySelectorAll: () => selectable || additional || soldout ? [{
    disabled:false,
    closest: () => additional ? {} : null,
    options:[{value:code, textContent:soldout ? 'M [품절]' : 'M', disabled:false}]
  }] : []};
  globalThis.fetch = async url => {
    const qty = Number(new URL(url).searchParams.get('product['+code+']'));
    const reply = replies[Math.min(calls++, replies.length-1)];
    if (reply instanceof Error) throw reply;
    if (reply === 'HTTP503') return {ok:false, status:503};
    if (reply === 'JSONFAIL') return {ok:true, json:async()=>{throw new Error('bad JSON');}};
    const body = reply === 'SUCCESS' ? {[code]:{item_code:code, product_no:1, quantity:qty, product_price:'1000'}} : reply;
    return {ok:true, json:async()=>body};
  };
  const result = await calculate({domain:'https://shop.example', productNo:'1', optionDataJson:{[code]:{option_value:'M', is_selling:true}}});
  return {qty:result.data?.M, calls, error:result.error};
}
let result = await run([exact(14)]);
assert(result.qty===14 && result.calls===1, 'explicit server stock must bypass binary search');
result = await run(['HTTP503', 'JSONFAIL', exact(1)]);
assert(result.qty===1 && result.calls===3, 'HTTP/JSON errors must retry and recover');
result = await run([{}], {selectable:false});
assert(result.qty===-1 && result.calls===3, 'malformed response must fail closed after 3 retries');
result = await run([{}]);
assert(result.qty===-2 && result.calls===3, 'selectable main option can retain unknown quantity');
result = await run([{}], {selectable:false, additional:true});
assert(result.qty===-1, 'additional marking-kit selector must not imply main availability');
result = await run([{...exact(14), sItemCode:'ANOTHER_PRODUCT'}], {selectable:false});
assert(result.qty===-1, 'mismatched stock item must not be trusted');
result = await run([inventoryFailure, 'SUCCESS', new Error('network failed')]);
assert(result.qty===-2 && result.calls===5, 'search errors must not become a false exact lower boundary');
result = await run([exact(0)]);
assert(result.qty===0 && result.calls===1, 'explicit zero server stock remains zero');
result = await run(['SUCCESS']);
assert(result.qty===9999 && result.calls===1, 'valid per-item success can retain capped orderable boundary');
result = await run([{}], {soldout:true});
assert(result.qty===0 && result.calls===0, 'main option soldout must avoid unnecessary requests');
result = await run([{Result:false, msg:'최대 구매수량을 초과했습니다.', sItemCode:code}]);
assert(result.qty===-2, 'non-inventory restriction must not become false soldout');
console.log('11 CalculatorProduct JS cases passed');
'''
        completed = subprocess.run([shutil.which("node"), "--input-type=module"],
                                   input=harness, text=True, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("11 CalculatorProduct JS cases passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
