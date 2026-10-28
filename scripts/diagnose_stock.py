"""Read-only stock probe. Never writes history, Sheets, carts or Telegram."""
import json
import re
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


def redact_diagnostic(value):
    return re.sub(r"(?i)([?&]auth=)[^&\s\"']+", r"\1[redacted]", str(value))




with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    for product_url in ["https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%EB%B0%98%ED%8C%94%ED%8B%B0%EC%85%94%EC%B8%A0%EB%B8%94%EB%9E%99/1782/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%EC%9C%A0%EB%8B%88%ED%8F%BC/1785/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EC%98%A4%EB%A6%AC%EC%A7%80%EB%84%90-%EB%AA%A8%EC%9E%90/436/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EC%98%88%EC%95%BD%ED%95%9C%EC%A0%95%ED%8C%90%EB%A7%A412%EC%9B%94%EB%A7%90%EB%B0%B0%EC%86%A1-%EA%B5%BF%EB%B0%94%EC%9D%B4-%EC%9E%A0%EC%8B%A4-%EA%B7%B8%EB%9D%BC%EC%9A%B4%EB%93%9C-%EC%9C%A0%EB%A6%AC%EB%B3%91/2792/","https://nolmdshop.com/product/lg%ED%8A%B8%EC%9C%88%EC%8A%A4-%EB%84%A4%EB%A1%9C-%EC%97%90%EB%94%94%EC%85%98-%ED%82%A4%EC%A6%88-%EC%9C%A0%EB%8B%88%ED%8F%BC/1784/"]:
        stock, name, price, diagnostic = get_stock_for_product(page, product_url)
        emit('CASE_RESULT', {'product_no': product_url.rstrip('/').split('/')[-1], 'name': name, 'stock': stock, 'diagnostic': diagnostic, 'option_info': page.evaluate('() => window.__lg_stock_option_info || {}')})

    # Read-only category diagnostics: no Sheets/history/cart/Telegram writes.
    category_cases = [
        ('LG트윈스', '31', 'https://nolmdshop.com/category/LG%ED%8A%B8%EC%9C%88%EC%8A%A4/31/'),
        ('두산베어스', '30', 'https://nolmdshop.com/category/%EB%91%90%EC%82%B0%EB%B2%A0%EC%96%B4%EC%8A%A4/30/'),
        ('키움히어로즈', '29', 'https://nolmdshop.com/category/%ED%82%A4%EC%9B%80%ED%9E%88%EC%96%B4%EB%A1%9C%EC%A6%88/29/'),
    ]
    for team_name, cate_no, category_url in category_cases:
        probe = browser.new_page(viewport={'width': 1400, 'height': 900})
        net_events = []
        page_errors = []
        def record_request(req):
            if 'ApiProductNormal' in req.url:
                net_events.append({'kind': 'request', 'method': req.method, 'url': req.url})
        def record_failed(req):
            if 'ApiProductNormal' in req.url or req.is_navigation_request():
                net_events.append({'kind': 'requestfailed', 'url': req.url, 'failure': redact_diagnostic(req.failure)})
        def record_response(resp):
            if 'ApiProductNormal' in resp.url:
                net_events.append({'kind': 'response', 'status': resp.status, 'url': resp.url,
                                   'content_type': resp.headers.get('content-type', '')})
        probe.on('request', record_request)
        probe.on('requestfailed', record_failed)
        probe.on('response', record_response)
        probe.on('pageerror', lambda exc: page_errors.append(redact_diagnostic(exc)[:300]))
        probe.on('console', lambda msg: page_errors.append('console: ' + redact_diagnostic(msg.text)[:300]) if msg.type == 'error' else None)
        result = {'team': team_name, 'category_url': category_url, 'network_events': net_events,
                  'page_errors': page_errors, 'direct_api_pages': []}
        try:
            nav = probe.goto(category_url, wait_until='domcontentloaded', timeout=30000)
            result['navigation'] = {'status': nav.status if nav else None, 'final_url': probe.url,
                                    'title': probe.title()[:160]}
            state = probe.evaluate('''() => ({
                ready: document.readyState,
                current: Number(document.querySelector('#more_current_page')?.textContent || 0),
                total: Number(document.querySelector('#more_total_page')?.textContent || 0),
                link_count: new Set([...document.querySelectorAll('a[href*="/product/"]')].map(a => a.href)).size,
                more_button: !!document.querySelector('.xans-product-listmore .btnMore'),
                display_more_type: typeof window.$M?.displayMore,
                cafe24_template_ready: typeof window.CAFE24?.FRONT_XANS_TEMPLATE?.getTemplateForVDOM === 'function'
            })''')
            result['dom_initial'] = state
            for page_no in range(1, min(max(state['total'], 1), 10) + 1):
                try:
                    api_result = probe.evaluate('''async (args) => {
                        const u = new URL('/exec/front/Product/ApiProductNormal', location.origin);
                        u.search = new URLSearchParams({cate_no: args.cate, supplier_code: 'S0000000',
                            page: String(args.page), bInitMore: 'F', count: '20'});
                        const r = await fetch(u.href, {credentials: 'omit', redirect: 'follow'});
                        const type = r.headers.get('content-type') || '';
                        const text = await r.text();
                        let summary = {parse: 'non-json', prefix: text.slice(0, 180)};
                        try {
                            const d = JSON.parse(text);
                            summary = {parse: 'json', rtn_code: d.rtn_code,
                                item_count: Array.isArray(d.rtn_data?.data) ? d.rtn_data.data.length : null,
                                end: d.rtn_data?.end ?? null,
                                product_numbers: Array.isArray(d.rtn_data?.data)
                                    ? d.rtn_data.data.map(x => String(x.product_no)).slice(0, 2) : []};
                        } catch (_) {}
                        return {requested_url: u.href, status: r.status, final_url: r.url,
                            content_type: type, body_length: text.length, response: summary};
                    }''', {'cate': cate_no, 'page': page_no})
                    result['direct_api_pages'].append(api_result)
                except Exception as exc:
                    result['direct_api_pages'].append({'page': page_no, 'error': type(exc).__name__ + ': ' + redact_diagnostic(exc)[:200]})
            if state['more_button'] and state['current'] < state['total']:
                before = {'current': state['current'], 'link_count': state['link_count']}
                try:
                    probe.locator('.xans-product-listmore .btnMore').first.dispatch_event('click')
                    probe.wait_for_function('''before => {
                        const cur = Number(document.querySelector('#more_current_page')?.textContent || 0);
                        const n = new Set([...document.querySelectorAll('a[href*="/product/"]')].map(a => a.href)).size;
                        return cur > before.current && n > before.link_count;
                    }''', arg=before, timeout=15000)
                    result['ui_more'] = {'outcome': 'advanced', 'before': before,
                        'after': probe.evaluate('''() => ({current: Number(document.querySelector('#more_current_page')?.textContent || 0),
                            link_count: new Set([...document.querySelectorAll('a[href*="/product/"]')].map(a => a.href)).size})''')}
                except Exception as exc:
                    result['ui_more'] = {'outcome': 'failed_to_advance', 'before': before,
                        'error': type(exc).__name__ + ': ' + str(exc)[:200],
                        'after': probe.evaluate('''() => ({current: Number(document.querySelector('#more_current_page')?.textContent || 0),
                            link_count: new Set([...document.querySelectorAll('a[href*="/product/"]')].map(a => a.href)).size})''')}
            else:
                result['ui_more'] = {'outcome': 'unavailable_or_not_needed'}
        except Exception as exc:
            result['navigation_error'] = type(exc).__name__ + ': ' + redact_diagnostic(exc)[:300]
        emit('CATEGORY_DIAGNOSTIC', result)
        probe.close()
    browser.close()
