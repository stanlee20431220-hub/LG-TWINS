import copy
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from sheets_sync import (
    SheetSyncError,
    build_sheet_rows,
    summarize_inventory,
    sync_to_sheet,
)

# === twinscorestore.co.kr (LG트윈스 콜랩샵) ===
# 유니폼(42) / 의류(43) / 용품·잡화(60) / 트윈스 X 호빵맨 기획전(104)
# 법인구매(75)는 재고 모니터링 대상이 아니라 제외
# === nolmdshop.com (NOL MD shop - LG트윈스 공식 상품 판매처, 별도 사이트) ===
# LG트윈스 전체(31)
CATEGORY_URLS = [
    ("https://twinscorestore.co.kr/category/%EC%9C%A0%EB%8B%88%ED%8F%BC/42/", True),
    ("https://twinscorestore.co.kr/category/%EC%9D%98%EB%A5%98/43/", True),
    ("https://twinscorestore.co.kr/category/%EC%9A%A9%ED%92%88-%C2%B7-%EC%9E%A1%ED%99%94/60/", True),
    ("https://twinscorestore.co.kr/category/%ED%8A%B8%EC%9C%88%EC%8A%A4-x-%ED%98%B8%EB%B9%B5%EB%A7%A8/104/", False),
    ("https://nolmdshop.com/category/LG%ED%8A%B8%EC%9C%88%EC%8A%A4/31/", True),
]

_DEFAULT_HISTORY = "data/stock_history.json"
HISTORY_FILE = os.environ.get("STOCK_HISTORY_FILE") or (
    _DEFAULT_HISTORY
    if os.path.exists(_DEFAULT_HISTORY) or not os.path.exists("stock_history.json")
    else "stock_history.json"
)
LOW_STOCK_THRESHOLD = 50
COMPACT_ITEM_LIMIT = 12
PRODUCT_URL_RE = re.compile(r"/product/([^/]+/\d+)/")
EXCLUDE_KEYWORDS = ["마킹키트"]
UNCERTAIN_MARKER = "[불확실]"
SCRIPT_VERSION = "v14-stock-retry"

REMOVE_OVERLAYS_JS = """
() => {
    const selectors = ['.worldshipLayer', '.xans-layout-multishopshipping', '.ec-base-layer'];
    selectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
            el.style.display = 'none';
        });
    });
}
"""


def canonicalize_product_url(href):
    """Normalize product links while retaining each source site's origin."""
    href = href.split("?")[0]
    match = PRODUCT_URL_RE.search(href)
    if not match:
        return None
    parsed = urllib.parse.urlparse(href)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/product/{match.group(1)}/"


def is_excluded(url, apply_keywords=True):
    if not apply_keywords:
        return False
    decoded = urllib.parse.unquote(url)
    return any(keyword in decoded for keyword in EXCLUDE_KEYWORDS)


def dismiss_overlays(page):
    try:
        page.evaluate(REMOVE_OVERLAYS_JS)
    except Exception:
        pass


MAX_PAGES_PER_CATEGORY = 30


def scrape_links_on_current_page(page, links, apply_keywords=True):
    """Collect product links, including links exposed by ordinary lazy scrolling."""
    last_count = -1
    rounds_without_growth = 0
    for _ in range(60):
        hrefs = page.eval_on_selector_all(
            'a[href*="/product/"]', "els => els.map(e => e.href)"
        )
        for href in hrefs:
            canonical = canonicalize_product_url(href)
            if canonical and not is_excluded(canonical, apply_keywords):
                links.add(canonical)

        current_count = len(links)
        if current_count > last_count:
            last_count = current_count
            rounds_without_growth = 0
        else:
            rounds_without_growth += 1
        if rounds_without_growth >= 5:
            break

        dismiss_overlays(page)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(500)


def find_next_page_url(page):
    try:
        return page.evaluate(
            """
            () => {
                const containers = document.querySelectorAll(
                    '.xans-product-normalpaging, .ec-base-paginate, .paging, [class*=paging]'
                );
                for (const box of containers) {
                    const anchors = Array.from(box.querySelectorAll('a'));
                    let next = anchors.find(a => {
                        const t = (a.textContent || '').trim();
                        return t.includes('다음') || t.toLowerCase().includes('next');
                    });
                    if (next) {
                        const href = next.getAttribute('href');
                        if (href && href !== '#none' && href !== '#' && !href.startsWith('javascript')) {
                            return next.href;
                        }
                    }
                }
                for (const box of containers) {
                    const active = box.querySelector('strong, .on, .active');
                    const activeNum = active ? parseInt((active.textContent || '').trim(), 10) : null;
                    if (!activeNum) continue;
                    const anchors = Array.from(box.querySelectorAll('a'));
                    for (const a of anchors) {
                        const n = parseInt((a.textContent || '').trim(), 10);
                        if (!isNaN(n) && n === activeNum + 1) {
                            const href = a.getAttribute('href');
                            if (href && href !== '#none' && href !== '#' && !href.startsWith('javascript')) {
                                return a.href;
                            }
                        }
                    }
                }
                return null;
            }
            """
        )
    except Exception:
        return None


def collect_product_links(page):
    links = set()
    page.mouse.move(700, 450)
    for base_url, apply_keywords in CATEGORY_URLS:
        current_url = base_url
        visited = set()
        for _ in range(MAX_PAGES_PER_CATEGORY):
            if current_url in visited:
                break
            visited.add(current_url)
            page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
            dismiss_overlays(page)

            before_count = len(links)
            scrape_links_on_current_page(page, links, apply_keywords)
            after_count = len(links)
            print(f"  - {current_url}: 누적 {after_count}개")
            next_url = find_next_page_url(page)
            if not next_url or (after_count == before_count and next_url in visited):
                break
            current_url = next_url
    return sorted(links)


def _to_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None


def parse_option_stock(raw_option_data):
    """Convert option_stock_data, using -1 rather than false zeroes on parse uncertainty."""
    data = raw_option_data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as exc:
            return None, f"{UNCERTAIN_MARKER} JSON 파싱 실패({type(exc).__name__}): {str(raw_option_data)[:200]}"
    if not isinstance(data, dict):
        return None, f"{UNCERTAIN_MARKER} 예상치 못한 최상위 타입({type(data).__name__}): {str(data)[:200]}"

    stock_keys = [
        "stock_number", "stock_cnt", "stock_qty", "stockQty",
        "quantity", "stock", "inventory", "safe_inventory",
    ]
    result = {}
    issues = []
    for key, value in data.items():
        if not isinstance(value, dict):
            issues.append(f"{key}=항목형식({type(value).__name__})")
            continue
        label = value.get("option_value") or value.get("option_text") or str(key)
        matched = next((candidate for candidate in stock_keys if value.get(candidate) is not None), None)
        if not matched:
            result[label] = -1
            issues.append(f"{label}: 재고 필드 없음 keys={list(value.keys())[:10]}")
            continue
        parsed = _to_int(value.get(matched))
        if parsed is None:
            result[label] = -1
            issues.append(f"{label}: {matched} 숫자 변환 실패")
        else:
            result[label] = parsed

    if result:
        diagnostic = None
        if issues:
            diagnostic = f"{UNCERTAIN_MARKER} 일부 옵션 파싱 불확실: " + " | ".join(issues[:3])
        return result, diagnostic
    if issues:
        return {"재고": -1}, f"{UNCERTAIN_MARKER} 옵션 항목 파싱 실패: " + " | ".join(issues[:5])
    return None, f"{UNCERTAIN_MARKER} option_stock_data에 파싱 가능한 항목이 없음"


def extract_product_no(url):
    match = re.search(r"/(\d+)/?$", url.rstrip("/"))
    return match.group(1) if match else None


def get_option_stock_via_calculator(page, domain, product_no, option_data_json):
    """Prefer explicit server stock; bound retries and never invent quantities on errors."""
    js = r"""
    async (args) => {
        const { domain, productNo, optionDataJson } = args;
        const CAP = 9999;
        const validStock = value => (
            (typeof value === 'number' || typeof value === 'string') &&
            /^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value))
        ) ? Number(value) : null;
        const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
        async function tryQty(itemCode, qty) {
            const url = new URL('/exec/front/shop/CalculatorProduct', domain);
            url.search = new URLSearchParams({product_no: productNo, is_subscription: 'F', ['product[' + itemCode + ']']: qty});
            for (let attempt = 0; attempt < 3; attempt++) {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 8000);
                try {
                    const response = await fetch(url.href, {signal: controller.signal});
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    const data = await response.json();
                    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Invalid response');
                    const row = data[itemCode];
                    const matchesTop = data.sItemCode === itemCode || data.item_code === itemCode;
                    const matchesRow = row && typeof row === 'object' && (!row.item_code || row.item_code === itemCode);
                    const topStock = matchesTop ? validStock(data.stock_number) : null;
                    const rowStock = matchesRow ? validStock(row.stock_number) : null;
                    if (topStock !== null) return {exact: topStock};
                    if (rowStock !== null) return {exact: rowStock};
                    if (data.Result === false) {
                        if (matchesTop && /재고|품절/.test(String(data.msg || ''))) return {ok: false};
                        return {unknown: true};
                    }
                    if (matchesRow && String(row.product_no) === String(productNo) &&
                        Number(row.quantity) === qty && Number.isFinite(Number(row.product_price))) return {ok: true};
                    throw new Error('Missing valid item response');
                } catch (e) {
                    if (attempt === 2) return {unknown: true};
                } finally {
                    clearTimeout(timer);
                }
                await wait(250 * (attempt + 1));
            }
            return {unknown: true};
        }
        async function findMaxOrderable(itemCode, selectable) {
            const big = await tryQty(itemCode, CAP);
            if (big.exact !== undefined) return big.exact;
            if (big.ok === true) return CAP;
            if (big.unknown) return selectable ? -2 : -1;
            const one = await tryQty(itemCode, 1);
            if (one.exact !== undefined) return one.exact;
            if (one.unknown) return selectable ? -2 : -1;
            if (one.ok === false) return 0;
            let lo = 1, hi = 2;
            while (hi < CAP) {
                const probe = await tryQty(itemCode, hi);
                if (probe.exact !== undefined) return probe.exact;
                if (probe.unknown) return -2;
                if (!probe.ok) break;
                lo = hi;
                hi = Math.min(CAP, hi * 2);
            }
            while (hi - lo > 1) {
                const mid = Math.floor((lo + hi) / 2);
                const probe = await tryQty(itemCode, mid);
                if (probe.exact !== undefined) return probe.exact;
                if (probe.unknown) return -2;
                if (probe.ok) lo = mid; else hi = mid;
            }
            return lo;
        }
        try {
            const items = typeof optionDataJson === 'string' ? JSON.parse(optionDataJson) : optionDataJson;
            if (!items || typeof items !== 'object' || Array.isArray(items)) return {error: '옵션 JSON 항목 형식 오류'};
            const soldOutCodes = new Set();
            const selectableCodes = new Set();
            // Additional marking kits are separate products, not main-product options.
            const selects = Array.from(document.querySelectorAll('select[id*="option"], select[name*="option"]'))
                .filter(sel => !sel.closest('.xans-product-addproduct, [class*="addproduct"]'));
            for (const sel of selects) {
                for (const option of Array.from(sel.options)) {
                    const code = option.value;
                    if (!Object.prototype.hasOwnProperty.call(items, code)) continue;
                    const text = String(option.textContent || '').trim();
                    if (/\[품절\]/.test(text)) soldOutCodes.add(code);
                    else if (!sel.disabled && !option.disabled) selectableCodes.add(code);
                }
            }
            const result = {};
            for (const [itemCode, val] of Object.entries(items)) {
                if (!val || typeof val !== 'object') { result[itemCode] = -1; continue; }
                const optName = val.option_value ?? itemCode;
                const direct = validStock(val.stock_number);
                if (direct !== null) { result[optName] = direct; continue; }
                const selling = String(val.is_selling).toUpperCase();
                if (val.is_selling === false || selling === 'F' || soldOutCodes.has(itemCode)) {
                    result[optName] = 0;
                    continue;
                }
                result[optName] = await findMaxOrderable(itemCode, selectableCodes.has(itemCode));
            }
            return {data: result};
        } catch (e) {
            return {error: e.message};
        }
    }
    """
    try:
        outcome = page.evaluate(
            js,
            {"domain": domain, "productNo": product_no, "optionDataJson": option_data_json},
        )
    except Exception as exc:
        return None, f"CalculatorProduct 평가 실패({type(exc).__name__}): {exc}"
    if not outcome:
        return None, "CalculatorProduct 평가 결과 없음"
    if outcome.get("error"):
        return None, f"CalculatorProduct 방식 실패: {outcome['error']}"
    data = outcome.get("data") or {}
    if not data:
        return None, "CalculatorProduct 방식: 옵션 항목 없음"
    unknown = [key for key, value in data.items() if value < 0 and value != -2]
    selectable = [key for key, value in data.items() if value == -2]
    notes = []
    if unknown:
        notes.append(f"일부 옵션 재고 조회 실패(확인불가): {unknown[:5]}")
    if selectable:
        notes.append(f"선택가능 · 수량 미확인 옵션: {selectable[:5]}")
    diagnostic = " | ".join(notes) or None
    return data, diagnostic


def get_stock_for_product(page, url):
    """Return (stock, name, price, diagnostic), preserving the existing interface."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    option_data = page.evaluate(
        "() => (typeof option_stock_data !== 'undefined') ? option_stock_data : null"
    )
    single_data = page.evaluate(
        "() => (typeof single_option_stock_data !== 'undefined') ? single_option_stock_data : null"
    )
    raw_price = page.evaluate(
        "() => (typeof product_price !== 'undefined') ? product_price : null"
    )

    name = None
    try:
        name = page.evaluate(
            "() => (typeof product_name !== 'undefined' && product_name) ? product_name : null"
        )
    except Exception:
        pass
    if not name:
        try:
            name = page.title().split(" - ")[0].strip()
        except Exception:
            pass
    price = _to_int(raw_price) if raw_price is not None else None
    diagnostic = None

    if option_data:
        product_no = extract_product_no(url)
        parsed_url = urllib.parse.urlparse(url)
        domain = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.scheme and parsed_url.netloc else None
        api_result, api_diagnostic = (None, None)
        if product_no and domain:
            api_result, api_diagnostic = get_option_stock_via_calculator(
                page, domain, product_no, option_data
            )
        elif not domain:
            api_diagnostic = "상품 URL에서 도메인을 추출하지 못함"
        if api_result:
            return api_result, name, price, api_diagnostic

        fallback_result, fallback_diagnostic = parse_option_stock(option_data)
        if fallback_result is not None:
            note = f"{UNCERTAIN_MARKER} 옵션 API 조회 실패 후 페이지 데이터 폴백 사용; 품절/증감 판단 제외"
            if api_diagnostic:
                note += f" | API 실패사유: {api_diagnostic}"
            if fallback_diagnostic:
                note += f" | {fallback_diagnostic}"
            return fallback_result, name, price, note

    if single_data:
        data = single_data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as exc:
                diagnostic = f"{UNCERTAIN_MARKER} single_option_stock_data JSON 파싱 실패({type(exc).__name__})"
                data = {}
        if isinstance(data, dict) and data.get("stock_number") is not None:
            qty = _to_int(data.get("stock_number"))
            if qty is not None:
                return {"재고": qty}, name, price, diagnostic
            diagnostic = f"{UNCERTAIN_MARKER} single_option_stock_data 수량 변환 실패"

    return None, name, price, diagnostic or "option_stock_data / single_option_stock_data 둘 다 못 찾음"


def normalize_image_url(raw_url, product_url):
    """Return an absolute HTTPS representative image URL, or None."""
    if not raw_url:
        return None
    candidate = html.unescape(str(raw_url)).strip()
    if not candidate or candidate.lower().startswith(("data:", "javascript:")):
        return None
    absolute = urllib.parse.urljoin(product_url, candidate)
    parsed = urllib.parse.urlparse(absolute)
    if not parsed.netloc:
        return None
    return urllib.parse.urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def extract_representative_image(page, product_url):
    """Read og:image from the product page already loaded by get_stock_for_product."""
    try:
        raw_url = page.evaluate(
            """() => {
                const selectors = [
                    'meta[property="og:image"]',
                    'meta[property="og:image:secure_url"]',
                    'meta[name="twitter:image"]'
                ];
                for (const selector of selectors) {
                    const value = document.querySelector(selector)?.getAttribute('content');
                    if (value && value.trim()) return value.trim();
                }
                return null;
            }"""
        )
    except Exception:
        return None
    return normalize_image_url(raw_url, product_url)


def html_link(name, url):
    safe_name = html.escape(name or url, quote=False)
    return f'<a href="{html.escape(url, quote=True)}">{safe_name}</a>'


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15)


def send_telegram_chunked(token, chat_id, text, limit=3500):
    pending = ""
    for line in text.splitlines(True):
        while len(line) > limit:
            if pending:
                send_telegram(token, chat_id, pending.rstrip())
                pending = ""
            send_telegram(token, chat_id, line[:limit])
            line = line[limit:]
        if len(pending) + len(line) > limit and pending:
            send_telegram(token, chat_id, pending.rstrip())
            pending = ""
        pending += line
    if pending.strip():
        send_telegram(token, chat_id, pending.rstrip())


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"last_checked": None, "products": {}}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except Exception as exc:
        print(f"History load failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"last_checked": None, "products": {}}
    if not isinstance(saved, dict):
        return {"last_checked": None, "products": {}}
    return {
        "last_checked": saved.get("last_checked"),
        "products": saved.get("products") if isinstance(saved.get("products"), dict) else {},
    }


def load_previous():
    """Compatibility helper retained for callers that only need product entries."""
    return load_history()["products"]


def save_history(now, products):
    directory = os.path.dirname(HISTORY_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = HISTORY_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"last_checked": now, "products": products},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(temp_path, HISTORY_FILE)


def fmt_won(value):
    return f"{value:,}원"


def stock_status(qty):
    qty = _to_int(qty)
    if qty == -2:
        return "?", "선택가능 · 수량 미확인"
    if qty is None or qty < 0:
        return "?", "확인불가"
    if qty == 0:
        return "[품절]", "품절"
    if qty < LOW_STOCK_THRESHOLD:
        return "[저재고]", f"{qty}개 ({LOW_STOCK_THRESHOLD}개 미만)"
    if qty >= 9999:
        return "[재고]", "9999 이상 (주문 가능 상한, 실제 재고 아님)"
    return "[재고]", f"{qty}개"


def is_fully_sold_out(stock, uncertain=False):
    if uncertain:
        return False
    values = [_to_int(value) for value in stock.values()]
    return bool(values) and all(value == 0 for value in values)


def _stale_entry(url, previous_entry, reason, now, previous_checked, name=None, image_url=None):
    entry = copy.deepcopy(previous_entry) if previous_entry else {}
    entry.setdefault("name", name or url)
    entry.setdefault("price", None)
    entry.setdefault("stock", {})
    if image_url:
        entry["image_url"] = image_url
    else:
        entry.setdefault("image_url", None)
    entry["diagnostic"] = reason
    entry["stale"] = True
    entry["uncertain"] = True
    entry["checked_at"] = entry.get("checked_at") or previous_checked
    entry["last_attempt"] = now
    return entry


def _safe_sheet_error(exc):
    if isinstance(exc, SheetSyncError):
        return str(exc)[:500]
    return f"{type(exc).__name__} (상세 내용은 실행 로그 확인)"


def _compact_telegram(now, products, previous_products, sheet_result):
    summary = summarize_inventory(
        products,
        previous_products,
        low_threshold=LOW_STOCK_THRESHOLD,
        item_limit=COMPACT_ITEM_LIMIT,
    )
    stale_count = sum(1 for entry in products.values() if entry.get("stale"))
    lines = [
        f"[재고 모니터] {now} ({SCRIPT_VERSION})",
        f"신규 품절 옵션 {summary['newly_soldout']} | 재입고 옵션 {summary['restocked']} | 저재고 옵션 {summary['low_stock']}",
        f"상품 {len(products)}개 · 이전 데이터 유지 {stale_count}개",
    ]
    if summary["items"]:
        lines.append("")
        for kind, item in summary["items"]:
            lines.append(f"- [{html.escape(kind)}] {html.escape(item)}")
        if summary["omitted"]:
            lines.append(f"- 외 {summary['omitted']}건")
    sheet_url = html.escape(sheet_result["url"], quote=True)
    lines.extend(["", f'<a href="{sheet_url}">Google Sheets에서 전체 재고 보기</a>'])
    return "\n".join(lines)


def main():
    from playwright.sync_api import sync_playwright

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID")

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    history = load_history()
    previous_products = history["products"]
    previous_checked = history.get("last_checked")
    current_products = {}

    change_blocks = []
    full_stock_blocks = []
    low_stock_blocks = []
    sold_out_blocks = []
    price_change_lines = []
    error_lines = []
    diagnostic_lines = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        product_urls = collect_product_links(page)
        print(f"Found {len(product_urls)} products across categories (마킹키트 제외)")

        for url in product_urls:
            previous_entry = previous_products.get(url) or {}
            try:
                stock, name, price, diagnostic = get_stock_for_product(page, url)
                # Deliberately read the image only after the stock function returns, while
                # the same product page is still loaded.
                image_url = extract_representative_image(page, url)
            except Exception as exc:
                reason = f"수집 실패({type(exc).__name__}): {exc}"
                current_products[url] = _stale_entry(
                    url, previous_entry, reason, now, previous_checked
                )
                error_lines.append(
                    f"{html_link(previous_entry.get('name') or url, url)}: {html.escape(reason)} · 이전 데이터 유지"
                )
                continue

            name = name or previous_entry.get("name") or url
            link = html_link(name, url)
            if stock is None:
                reason = diagnostic or "재고 데이터 없음"
                current_products[url] = _stale_entry(
                    url, previous_entry, reason, now, previous_checked, name=name, image_url=image_url
                )
                error_lines.append(f"{link}: {html.escape(reason)} · 이전 데이터 유지")
                continue

            normalized_stock = {}
            normalization_issues = []
            for option, raw_qty in stock.items():
                qty = _to_int(raw_qty)
                if qty is None:
                    qty = -1
                    normalization_issues.append(str(option))
                normalized_stock[str(option)] = qty
            if normalization_issues:
                extra = f"{UNCERTAIN_MARKER} 수량 변환 실패 옵션: {normalization_issues[:5]}"
                diagnostic = f"{diagnostic} | {extra}" if diagnostic else extra

            uncertain = bool(diagnostic and UNCERTAIN_MARKER in diagnostic)
            entry = {
                "name": name,
                "price": price,
                "stock": normalized_stock,
                "image_url": image_url or previous_entry.get("image_url"),
                "diagnostic": diagnostic,
                "stale": False,
                "uncertain": uncertain,
                "checked_at": now,
                "last_attempt": now,
            }
            current_products[url] = entry
            if diagnostic:
                diagnostic_lines.append(f"{link}: {html.escape(diagnostic)}")

            previous_stock = previous_entry.get("stock") or {}
            previous_price = previous_entry.get("price")
            previous_unreliable = bool(
                previous_entry.get("stale") or previous_entry.get("uncertain")
            )
            price_text = f" ({fmt_won(price)})" if price is not None else ""
            full_option_lines = []
            changed_option_lines = []
            has_low_stock = False

            for option, qty in normalized_stock.items():
                symbol, status_label = stock_status(qty)
                display_text = f"{symbol} {status_label}"
                diff_text = ""
                previous_exists = option in previous_stock
                previous_qty = _to_int(previous_stock.get(option)) if previous_exists else None
                comparable = (
                    not uncertain
                    and not previous_unreliable
                    and qty >= 0
                    and qty < 9999
                    and previous_exists
                    and previous_qty is not None
                    and 0 <= previous_qty < 9999
                )
                if comparable:
                    difference = qty - previous_qty
                    if difference:
                        sign = "+" if difference > 0 else ""
                        diff_text = f" ({sign}{difference})"
                        changed_option_lines.append(
                            f"  - {html.escape(option)}: {display_text}{diff_text}"
                        )
                full_option_lines.append(
                    f"  - {html.escape(option)}: {display_text}{diff_text}"
                )
                if not uncertain and 0 < qty < LOW_STOCK_THRESHOLD:
                    has_low_stock = True

            fully_sold_out = is_fully_sold_out(normalized_stock, uncertain=uncertain)
            header_prefix = "[전체품절] " if fully_sold_out else "■ "
            block_text = f"{header_prefix}{link}{price_text}\n" + "\n".join(full_option_lines)
            full_stock_blocks.append(block_text)
            if fully_sold_out:
                sold_out_blocks.append(block_text)
            elif has_low_stock:
                low_stock_blocks.append(block_text)
            if changed_option_lines:
                change_blocks.append(f"■ {link}{price_text}\n" + "\n".join(changed_option_lines))
            if (
                not uncertain
                and not previous_unreliable
                and price is not None
                and previous_price is not None
                and price != previous_price
            ):
                price_change_lines.append(
                    f"{link}: {fmt_won(previous_price)} → {fmt_won(price)}"
                )
            time.sleep(0.4)
        browser.close()

    header = (
        f"[전상품 재고 확인] {now} ({SCRIPT_VERSION})\n"
        f"확인 대상 상품: {len(current_products)}개"
    )
    messages = [header]
    if diagnostic_lines:
        messages.append(
            "[진단: 불확실한 재고는 품절/증감에서 제외]\n"
            + "\n".join(diagnostic_lines[:20])
            + (f"\n...외 {len(diagnostic_lines) - 20}건" if len(diagnostic_lines) > 20 else "")
        )
    if sold_out_blocks:
        messages.append("[전체품절]\n\n" + "\n\n".join(sold_out_blocks))
    if low_stock_blocks:
        messages.append(
            f"[{LOW_STOCK_THRESHOLD}개 미만 재고]\n\n" + "\n\n".join(low_stock_blocks)
        )
    if change_blocks:
        messages.append("[재고 변동]\n\n" + "\n\n".join(change_blocks))
    if price_change_lines:
        messages.append("[가격 변동]\n" + "\n".join(price_change_lines))
    if full_stock_blocks:
        messages.append("[전체 재고]\n\n" + "\n\n".join(full_stock_blocks))
    if error_lines:
        messages.append("[오류 · 이전 데이터 유지]\n" + "\n".join(error_lines))
    full_message = "\n\n".join(messages)
    print(full_message)

    sheet_result = None
    sheet_error = None
    if service_account_json and spreadsheet_id:
        try:
            rows = build_sheet_rows(
                current_products,
                previous_products,
                default_checked_time=now,
                low_threshold=LOW_STOCK_THRESHOLD,
            )
            sheet_result = sync_to_sheet(spreadsheet_id, service_account_json, rows)
            print(f"Google Sheets export complete: {sheet_result['row_count']} option rows")
        except Exception as exc:
            sheet_error = _safe_sheet_error(exc)
            print(f"Google Sheets export failed: {sheet_error}", file=sys.stderr)
    else:
        missing = []
        if not service_account_json:
            missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not spreadsheet_id:
            missing.append("GOOGLE_SHEET_ID")
        print(
            "Google Sheets export skipped; missing " + ", ".join(missing) + ". Using full Telegram report.",
            file=sys.stderr,
        )

    # Persist after the Sheets attempt and before Telegram, so notification failure cannot
    # discard a completed scrape or stale-state bookkeeping.
    save_history(now, current_products)

    if sheet_result:
        telegram_message = _compact_telegram(
            now, current_products, previous_products, sheet_result
        )
    else:
        telegram_message = full_message
        if sheet_error:
            telegram_message += (
                "\n\n[Google Sheets 내보내기 실패]\n"
                + html.escape(sheet_error)
                + "\n전체 Telegram 보고서로 대체했습니다."
            )

    if token and chat_id:
        try:
            send_telegram_chunked(token, chat_id, telegram_message)
        except Exception as exc:
            print(f"Telegram send failed after history save: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        print("Telegram credentials not set; skipping notification", file=sys.stderr)


if __name__ == "__main__":
    main()
