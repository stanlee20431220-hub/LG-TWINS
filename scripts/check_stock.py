import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

# === twinscorestore.co.kr (LG트윈스 콜랩샵) ===
# 유니폼(42) / 의류(43) / 용품·잡화(60) / 트윈스 X 호빵맨 기획전(104)
# 법인구매(75)는 재고 모니터링 대상이 아니라 제외
# === nolmdshop.com (NOL MD shop - LG트윈스 공식 상품 판매처, 별도 사이트) ===
# LG트윈스 전체(31)
# 각 항목: (카테고리 URL, EXCLUDE_KEYWORDS 적용 여부)
# 104(호빵맨 콜라보 기획전)는 "마킹키트"가 들어간 상품명도 모니터링 대상이라 키워드 제외를 적용하지 않음
CATEGORY_URLS = [
    ("https://twinscorestore.co.kr/category/%EC%9C%A0%EB%8B%88%ED%8F%BC/42/", True),
    ("https://twinscorestore.co.kr/category/%EC%9D%98%EB%A5%98/43/", True),
    ("https://twinscorestore.co.kr/category/%EC%9A%A9%ED%92%88-%C2%B7-%EC%9E%A1%ED%99%94/60/", True),
    ("https://twinscorestore.co.kr/category/%ED%8A%B8%EC%9C%88%EC%8A%A4-x-%ED%98%B8%EB%B9%B5%EB%A7%A8/104/", False),
    ("https://nolmdshop.com/category/LG%ED%8A%B8%EC%9C%88%EC%8A%A4/31/", True),
]

HISTORY_FILE = "data/stock_history.json"
LOW_STOCK_THRESHOLD = 50
PRODUCT_URL_RE = re.compile(r"/product/([^/]+/\d+)/")
EXCLUDE_KEYWORDS = ["마킹키트"]

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
    """상품 링크를 정규화. 쿼리스트링을 제거하고 /product/{slug}/{번호}/ 형태로 통일하되,
    href 자신의 도메인(스킴+호스트)을 그대로 유지한다 (여러 사이트를 동시에 모니터링하기 위함)."""
    href = href.split("?")[0]
    m = PRODUCT_URL_RE.search(href)
    if not m:
        return None
    parsed = urllib.parse.urlparse(href)
    if not parsed.scheme or not parsed.netloc:
        return None
    domain = f"{parsed.scheme}://{parsed.netloc}"
    return f"{domain}/product/{m.group(1)}/"

def is_excluded(url, apply_keywords=True):
    if not apply_keywords:
        return False
    decoded = urllib.parse.unquote(url)
    return any(kw in decoded for kw in EXCLUDE_KEYWORDS)

def dismiss_overlays(page):
    try:
        page.evaluate(REMOVE_OVERLAYS_JS)
    except Exception:
        pass

MAX_PAGES_PER_CATEGORY = 30  # 안전장치 (무한루프 방지)

def scrape_links_on_current_page(page, links, apply_keywords=True):
    """현재 로드된 페이지에서 상품 링크를 수집 + 스크롤로 지연로딩 요소도 추가 수집."""
    last_count = -1
    rounds_without_growth = 0
    for _ in range(60):
        hrefs = page.eval_on_selector_all(
            'a[href*="/product/"]', "els => els.map(e => e.href)"
        )
        for h in hrefs:
            canonical = canonicalize_product_url(h)
            if not canonical:
                continue
            if is_excluded(canonical, apply_keywords):
                continue
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
    """카페24 카테고리 페이지네이션 UI에서 '다음 페이지' 링크를 찾아 반환.
    없으면 None."""
    try:
        return page.evaluate(
            """
            () => {
                const containers = document.querySelectorAll(
                    '.xans-product-normalpaging, .ec-base-paginate, .paging, [class*=paging]'
                );
                for (const box of containers) {
                    const anchors = Array.from(box.querySelectorAll('a'));
                    // '다음' / 'next' 텍스트를 가진 링크 우선
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
                // '다음' 링크가 없으면, 현재 활성 페이지 번호보다 큰 숫자 링크를 찾음
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

            # 다음 페이지가 없거나, 페이지를 넘겼는데도 새 상품이 전혀 없으면 종료
            if not next_url or (after_count == before_count and next_url in visited):
                break

            current_url = next_url

    return sorted(links)

def parse_option_stock(raw_option_data):
    """option_stock_data를 {옵션라벨: 재고수} 형태로 변환.
    실패 시 (None, 진단정보) 반환."""
    data = raw_option_data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            return None, f"JSON 파싱 실패({type(e).__name__}): {str(raw_option_data)[:200]}"

    if not isinstance(data, dict):
        return None, f"예상치 못한 최상위 타입({type(data).__name__}): {str(data)[:200]}"

    # 재고 수량 필드명이 스킨/옵션 구성에 따라 다를 수 있어 후보를 순서대로 시도
    STOCK_KEY_CANDIDATES = [
        "stock_number", "stock_cnt", "stock_qty", "stockQty",
        "quantity", "stock", "inventory", "safe_inventory",
    ]

    def to_int(v):
        try:
            return int(str(v).replace(",", "").strip())
        except Exception:
            return None

    result = {}
    unparsed_entries = []
    unknown_key_entries = []
    for key, v in data.items():
        if not isinstance(v, dict):
            unparsed_entries.append(f"{key}={str(v)[:60]}")
            continue

        stock_raw = None
        matched_key = None
        for cand in STOCK_KEY_CANDIDATES:
            if cand in v and v.get(cand) is not None:
                stock_raw = v.get(cand)
                matched_key = cand
                break

        label = v.get("option_value") or v.get("option_text") or str(key)

        if matched_key is None:
            # 알려진 필드명 중 어느 것도 없음 -> 0으로 두되, 실제 키 목록을 진단정보에 남김
            unknown_key_entries.append(f"{label}: keys={list(v.keys())[:10]}")
            result[label] = 0
            continue

        stock_int = to_int(stock_raw)
        result[label] = stock_int if stock_int is not None else 0

    diagnostic = None
    if unknown_key_entries:
        diagnostic = "재고 필드명을 못 찾아 0으로 처리(실제 키 확인 필요): " + " | ".join(unknown_key_entries[:3])

    if result:
        return result, diagnostic
    if unparsed_entries:
        return {"재고": 0}, f"항목 형식이 달라 재고 0으로 처리함: {unparsed_entries[:5]}"

    return None, "option_stock_data는 있었지만 파싱 가능한 항목이 없음"

def extract_product_no(url):
    """상품 URL에서 숫자 상품번호를 추출. 예: .../361/ -> "361" """
    m = re.search(r"/(\d+)/?$", url.rstrip("/"))
    return m.group(1) if m else None

def get_option_stock_via_calculator(page, domain, product_no, option_data_json):
    """옵션(사이즈 등)이 있는 상품의 실제 재고를 조회.

    두 가지 신뢰도 문제가 확인됨:
    1) CalculatorProduct API가 재고 부족 시 돌려주는 stock_number 필드는 실제 재고와
       무관한 고정/부정확한 값(예: 항상 3). -> 이 값은 절대 쓰지 않고, 성공/실패
       경계를 이분탐색으로 직접 찾는다.
    2) 이분탐색만으로는 충분치 않음: 실제로는 품절(재고 0)인 옵션에도 이 API가
       수량 1 주문은 통과시켜버리는 경우가 확인됨(품절 상품이 "1개"로 잘못 표시).
       -> 그래서 화면에 실제로 보이는 옵션 드롭다운의 "[품절]" 표시를 최우선
       신뢰 소스로 삼고, 거기서 품절로 확인된 옵션은 API 결과와 무관하게 0으로 확정.

    option_data_json은 이미 로드된 페이지에서 읽어온 option_stock_data 값(JSON 문자열)을
    그대로 넘겨받는다 — 다시 detail.html을 fetch할 필요 없음(현재 페이지가 이미 그 상품
    페이지이므로, DOM의 [품절] 표시도 같은 페이지에서 함께 확인 가능).
    domain은 상품 URL에서 추출한 "https://호스트" 형태(사이트별로 다름).
    반환: ({옵션라벨: 재고수}, 진단정보) — 실패 시 (None, 진단정보)
    """
    js = """
    async (args) => {
        const { domain, productNo, optionDataJson } = args;

        async function tryQty(itemCode, qty) {
            const url = `${domain}/exec/front/shop/CalculatorProduct?product_no=${productNo}&is_subscription=F&product[${itemCode}]=${qty}`;
            try {
                const data = await fetch(url).then(r => r.json());
                // Result가 명시적으로 false면 그 수량은 주문 불가(재고초과 등)
                return data.Result !== false;
            } catch (e) {
                return null; // 조회 자체 실패(네트워크 등) - 알 수 없음
            }
        }

        // stock_number 값은 신뢰할 수 없으므로 절대 사용하지 않고,
        // 성공/실패 경계를 직접 찾아 실제 주문 가능한 최대 수량을 구한다.
        async function findMaxOrderable(itemCode) {
            const CAP = 9999;

            const bigOk = await tryQty(itemCode, CAP);
            if (bigOk === null) return -1;
            if (bigOk) return CAP; // 9999개도 통과 = 재고 충분

            const oneOk = await tryQty(itemCode, 1);
            if (oneOk === null) return -1;
            if (!oneOk) return 0; // 1개도 안 됨 = 품절

            // 2배씩 늘려가며 실패 지점을 대략 찾음 (지수 탐색)
            let lo = 1, hi = 2;
            while (hi < CAP) {
                const ok = await tryQty(itemCode, hi);
                if (ok === null) break; // 알 수 없음 -> 지금까지의 lo/hi로 이분탐색 진행
                if (!ok) break;
                lo = hi;
                hi = hi * 2;
            }
            if (hi > CAP) hi = CAP;

            // lo(성공)와 hi(실패) 사이를 이분탐색으로 좁혀 정확한 경계를 찾음
            while (hi - lo > 1) {
                const mid = Math.floor((lo + hi) / 2);
                const ok = await tryQty(itemCode, mid);
                if (ok === null) { hi = mid; continue; }
                if (ok) { lo = mid; } else { hi = mid; }
            }
            return lo; // 마지막으로 성공이 확인된 수량(단, DOM [품절] 표시가 없을 때만 신뢰)
        }

        try {
            let items;
            try {
                items = JSON.parse(optionDataJson);
            } catch (e) {
                return { error: "옵션 JSON 파싱 실패: " + e.message };
            }

            // 현재 로드된 페이지의 옵션 드롭다운에서 "[품절]" 표시가 붙은 옵션들을 수집.
            // 이게 사용자가 실제로 보는 화면과 정확히 일치하는 최우선 판단 기준.
            const soldOutLabels = new Set();
            document.querySelectorAll('select[id*="option"], select[name*="option"]').forEach(sel => {
                Array.from(sel.options).forEach(o => {
                    const text = (o.textContent || '').trim();
                    if (text.includes('[품절]')) {
                        soldOutLabels.add(o.value);
                        soldOutLabels.add(text.replace(/\\s*\\[품절\\]\\s*$/, '').trim());
                    }
                });
            });

            const result = {};
            for (const [itemCode, val] of Object.entries(items)) {
                const optName = val.option_value ?? itemCode;
                const isSelling = val.is_selling === true || String(val.is_selling).toUpperCase() === "T";

                if (!isSelling || soldOutLabels.has(optName) || soldOutLabels.has(itemCode)) {
                    result[optName] = 0;
                    continue;
                }
                result[optName] = await findMaxOrderable(itemCode);
            }
            return { data: result };
        } catch (e) {
            return { error: e.message };
        }
    }
    """
    try:
        outcome = page.evaluate(
            js,
            {"domain": domain, "productNo": product_no, "optionDataJson": option_data_json},
        )
    except Exception as e:
        return None, f"CalculatorProduct 평가 실패({type(e).__name__}): {e}"

    if not outcome:
        return None, "CalculatorProduct 평가 결과 없음"
    if outcome.get("error"):
        return None, f"CalculatorProduct 방식 실패: {outcome['error']}"

    data = outcome.get("data") or {}
    if not data:
        return None, "CalculatorProduct 방식: 옵션 항목 없음"

    unknown_entries = [k for k, v in data.items() if v == -1]
    diagnostic = None
    if unknown_entries:
        diagnostic = f"일부 옵션 재고 조회 실패(알 수 없음으로 표시): {unknown_entries[:5]}"

    return data, diagnostic

def get_stock_for_product(page, url):
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
            title = page.title()
            name = title.split(" - ")[0].strip()
        except Exception:
            pass

    price = None
    if raw_price is not None:
        try:
            price = int(str(raw_price).replace(",", "").strip())
        except Exception:
            price = None

    diagnostic = None

    if option_data:
        product_no = extract_product_no(url)
        parsed = urllib.parse.urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None

        api_result, api_diagnostic = (None, None)
        if product_no and domain:
            api_result, api_diagnostic = get_option_stock_via_calculator(page, domain, product_no, option_data)
        elif not domain:
            api_diagnostic = "상품 URL에서 도메인을 추출하지 못함"

        if api_result:
            return api_result, name, price, api_diagnostic

        # API 방식 실패 시에만 기존 파싱 방식으로 폴백 (참고용, 정확하지 않을 수 있음)
        result, diagnostic = parse_option_stock(option_data)
        if result is not None:
            fallback_note = "옵션 API 조회 실패 → 기존 방식으로 대체(부정확할 수 있음)"
            if api_diagnostic:
                fallback_note += f" | API 실패사유: {api_diagnostic}"
            combined_diag = f"{diagnostic} | {fallback_note}" if diagnostic else fallback_note
            return result, name, price, combined_diag

    if single_data:
        data = single_data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                diagnostic = f"single_option_stock_data JSON 파싱 실패({type(e).__name__})"
                data = {}
        if isinstance(data, dict):
            stock_number = data.get("stock_number")
            if stock_number is not None:
                return {"재고": stock_number}, name, price, None

    return None, name, price, (diagnostic or "option_stock_data / single_option_stock_data 둘 다 못 찾음")

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
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)

def send_telegram_chunked(token, chat_id, text, limit=3500):
    if len(text) <= limit:
        send_telegram(token, chat_id, text)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            send_telegram(token, chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        send_telegram(token, chat_id, chunk)

def load_previous():
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    return saved.get("products", {})

def fmt_won(v):
    return f"{v:,}원"

def stock_status(qty):
    """재고 수량을 (기호, 표시문구) 튜플로 변환.
    9999는 'CalculatorProduct API가 9999개 주문도 통과시킴' = 충분한 재고를 뜻하는 상한 센티널,
    -1은 옵션 재고 조회 자체가 실패했음(확인불가)을 뜻함."""
    if qty == -1:
        return "❓", "확인불가"
    if qty == 0:
        return "🔴", "품절"
    if qty < LOW_STOCK_THRESHOLD:
        return "🟡", f"{qty}개 (50개 미만)"
    if qty >= 9999:
        return "🟢", "재고 있음(9999개 이상)"
    return "🟢", f"{qty}개"

def is_fully_sold_out(stock):
    """모든 옵션이 확인된 품절(0)인 경우에만 True. 확인불가(-1)가 섞여 있으면
    전체품절 여부를 단정할 수 없으므로 False로 취급."""
    values = list(stock.values())
    if not values:
        return False
    return all(v == 0 for v in values)

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    prev_products = load_previous()
    current_products = {}

    change_blocks = []
    full_stock_blocks = []
    low_stock_blocks = []
    sold_out_blocks = []
    price_change_lines = []
    error_lines = []
    diagnostic_lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        product_urls = collect_product_links(page)
        print(f"Found {len(product_urls)} products across categories (마킹키트 제외)")

        for url in product_urls:
            try:
                stock, name, price, diagnostic = get_stock_for_product(page, url)
            except Exception as e:
                error_lines.append(f"{url}: {type(e).__name__}: {e}")
                continue

            name = name or url
            link = html_link(name, url)

            if stock is None:
                detail = f" ({diagnostic})" if diagnostic else ""
                error_lines.append(f"{link}{detail}")
                continue

            current_products[url] = {"name": name, "price": price, "stock": stock}

            if diagnostic:
                diagnostic_lines.append(f"{link}: {diagnostic}")

            prev_entry = prev_products.get(url, {})
            prev_stock = prev_entry.get("stock", {})
            prev_price = prev_entry.get("price")

            price_str = f" ({fmt_won(price)})" if price is not None else ""

            # 전체 재고 표시용: 모든 옵션을 항상 출력, 변동이 있으면 증감도 같이 표시
            full_option_lines = []
            changed_option_lines = []
            has_low_stock = False

            for size, qty in stock.items():
                symbol, status_label = stock_status(qty)
                display = f"{symbol} {status_label}"

                # -1(조회 실패)은 증감 비교나 저재고 판정 대상에서 제외
                if qty == -1:
                    full_option_lines.append(f"  - {size}: {display}")
                    continue

                prev_qty = prev_stock.get(size, qty)
                diff = qty - prev_qty if prev_qty != -1 else 0
                diff_str = ""
                if diff != 0:
                    sign = "+" if diff > 0 else ""
                    diff_str = f" ({sign}{diff})"
                    changed_option_lines.append(f"  - {size}: {display}{diff_str}")

                full_option_lines.append(f"  - {size}: {display}{diff_str}")

                if 0 < qty < LOW_STOCK_THRESHOLD:
                    has_low_stock = True

            fully_sold_out = is_fully_sold_out(stock)
            header_prefix = "⛔ [전체품절] " if fully_sold_out else "■ "
            block_text = f"{header_prefix}{link}{price_str}\n" + "\n".join(full_option_lines)
            full_stock_blocks.append(block_text)

            if fully_sold_out:
                sold_out_blocks.append(block_text)
            elif has_low_stock:
                low_stock_blocks.append(block_text)

            if changed_option_lines:
                change_blocks.append(f"■ {link}{price_str}\n" + "\n".join(changed_option_lines))

            if price is not None and prev_price is not None and price != prev_price:
                price_change_lines.append(
                    f"{link}: {fmt_won(prev_price)} → {fmt_won(price)}"
                )

            time.sleep(0.4)

        browser.close()

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    SCRIPT_VERSION = "v10-soldout-dom-check"  # 배포 확인용 - 이 값이 메시지에 안 보이면 구버전이 실행된 것

    header = (
        f"[전상품 재고 확인] {now} ({SCRIPT_VERSION})\n"
        f"확인된 상품: {len(current_products)}개"
    )

    # 메시지 순서: 헤더 -> 진단(필드명 문제) -> 전체품절 -> 50개 미만 재고 -> 재고 변동 -> 가격 변동 -> 전체 재고 -> 오류
    messages = [header]
    if diagnostic_lines:
        messages.append(
            "[진단: 재고 필드명 확인 필요]\n"
            + "\n".join(diagnostic_lines[:20])
            + (f"\n...외 {len(diagnostic_lines) - 20}건" if len(diagnostic_lines) > 20 else "")
        )
    if sold_out_blocks:
        messages.append("[⛔ 전체품절]\n\n" + "\n\n".join(sold_out_blocks))
    if low_stock_blocks:
        messages.append(
            f"[🟡 {LOW_STOCK_THRESHOLD}개 미만 재고]\n\n" + "\n\n".join(low_stock_blocks)
        )
    if change_blocks:
        messages.append("[재고 변동]\n\n" + "\n\n".join(change_blocks))
    if price_change_lines:
        messages.append("[가격 변동]\n" + "\n".join(price_change_lines))
    if full_stock_blocks:
        messages.append("[전체 재고]\n\n" + "\n\n".join(full_stock_blocks))
    if error_lines:
        messages.append("[오류]\n" + "\n".join(error_lines))

    full_message = "\n\n".join(messages)
    print(full_message)

    if token and chat_id:
        send_telegram_chunked(token, chat_id, full_message)
    else:
        print("Telegram credentials not set; skipping notification", file=sys.stderr)

    os.makedirs("data", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"last_checked": now, "products": current_products},
            f,
            ensure_ascii=False,
            indent=2,
        )

if __name__ == "__main__":
    main()
