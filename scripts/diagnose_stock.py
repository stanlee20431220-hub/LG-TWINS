"""Read-only stock probe. Never writes history, Sheets, carts or Telegram."""
import json
import urllib.parse
from playwright.sync_api import sync_playwright
from check_stock import get_stock_for_product, _explicit_server_stock

PRODUCT_URL = 'https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EC%98%A4%EB%A6%AC%EC%A7%80%EB%84%90-%EB%AA%A8%EC%9E%90/436/'
ITEM_CODES = ['P00000QU000C', 'P00000QU000D']
SAFE_FIELDS = ['Result', 'msg', 'sItemCode', 'item_code', 'stock_number', 'product_no']


def summarize(payload, code):
    if not isinstance(payload, dict):
        return {'payload_type': type(payload).__name__}
    result = {'keys': list(payload)[:20]}
    for key in SAFE_FIELDS:
        if key in payload:
            result[key] = str(payload[key])[:160] if key == 'msg' else payload[key]
    row = payload.get(code)
    if isinstance(row, dict):
        result['item_keys'] = list(row)[:20]
        result['item'] = {key: row[key] for key in SAFE_FIELDS if key in row}
    result['validated_stock'] = _explicit_server_stock(payload, code, '436')
    return result


def emit(label, result):
    print(label + ': ' + json.dumps(result, ensure_ascii=False), flush=True)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    for product_url in ["https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%EB%B0%98%ED%8C%94%ED%8B%B0%EC%85%94%EC%B8%A0%EB%B8%94%EB%9E%99/1782/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%EC%9C%A0%EB%8B%88%ED%8F%BC/1785/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EC%98%A4%EB%A6%AC%EC%A7%80%EB%84%90-%EB%AA%A8%EC%9E%90/436/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EC%98%88%EC%95%BD%ED%95%9C%EC%A0%95%ED%8C%90%EB%A7%A412%EC%9B%94%EB%A7%90%EB%B0%B0%EC%86%A1-%EA%B5%BF%EB%B0%94%EC%9D%B4-%EC%9E%A0%EC%8B%A4-%EA%B7%B8%EB%9D%BC%EC%9A%B4%EB%93%9C-%EC%9C%A0%EB%A6%AC%EB%B3%91/2792/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%ED%82%A4%EC%A6%88-%EC%9C%A0%EB%8B%88%ED%8F%BC/1784/"]:
        stock, name, price, diagnostic = get_stock_for_product(page, product_url)
        emit('CASE_RESULT', {'product_no': product_url.rstrip('/').split('/')[-1], 'name': name, 'stock': stock, 'diagnostic': diagnostic, 'option_info': page.evaluate('() => window.__lg_stock_option_info || {}')})
    browser.close()
