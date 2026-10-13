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
    stock, name, price, diagnostic = get_stock_for_product(page, PRODUCT_URL)
    emit('PRODUCTION_RESULT', {'stock': stock, 'diagnostic': diagnostic,
                              'option_info': page.evaluate('() => window.__lg_stock_option_info || {}')})
    raw = page.evaluate('() => typeof option_stock_data !== "undefined" ? option_stock_data : null')
    options = json.loads(raw) if isinstance(raw, str) else (raw or {})
    emit('PAGE_FLAGS', {'host': urllib.parse.urlparse(page.url).hostname,
                       'options': {code: {key: (options.get(code) or {}).get(key) for key in ['option_value', 'use_stock', 'use_stock_original', 'is_selling', 'stock_number']} for code in ITEM_CODES},
                       'limits': page.evaluate('() => ({max: window.product_max, type: window.product_max_type})')})
    for code in ITEM_CODES:
        url = 'https://nolmdshop.com/exec/front/shop/CalculatorProduct?' + urllib.parse.urlencode({'product_no': '436', 'is_subscription': 'F', 'product[' + code + ']': '9999'})
        browser_result = page.evaluate('''async url => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await fetch(url, {signal: controller.signal});
                const info = {status: response.status, host: new URL(response.url).hostname, content_type: response.headers.get('content-type')};
                try {info.payload = await response.json();} catch(e) {info.error = e.name;}
                return info;
            } catch(e) {return {error: e.name};} finally {clearTimeout(timer);}
        }''', url)
        if 'payload' in browser_result:
            browser_result['payload'] = summarize(browser_result['payload'], code)
        emit('BROWSER_FETCH_' + code, browser_result)
        for mode, headers in [('PLAIN', {}), ('XHR', {'Referer': page.url, 'Accept': 'application/json, text/javascript, */*; q=0.01', 'X-Requested-With': 'XMLHttpRequest'})]:
            response = None
            try:
                response = page.request.get(url, timeout=10000, headers=headers)
                result = {'status': response.status, 'host': urllib.parse.urlparse(response.url).hostname,
                          'content_type': response.headers.get('content-type')}
                try:
                    result['payload'] = summarize(response.json(), code)
                except Exception as exc:
                    result['error'] = type(exc).__name__
                emit('REQUEST_FETCH_' + mode + '_' + code, result)
            except Exception as exc:
                emit('REQUEST_FETCH_' + mode + '_' + code, {'error': type(exc).__name__})
            finally:
                if response is not None:
                    response.dispose()
    browser.close()
