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

# twinscorestore.co.kr 카테고리: 유니폼(42) / 의류(43) / 용품·잡화(60)
# 법인구매(75)는 재고 모니터링 대상이 아니라 제외
CATEGORY_URLS = [
    "https://twinscorestore.co.kr/category/%EC%9C%A0%EB%8B%88%ED%8F%BC/42/",
    "https://twinscorestore.co.kr/category/%EC%9D%98%EB%A5%98/43/",
    "https://twinscorestore.co.kr/category/%EC%9A%A9%ED%92%88-%C2%B7-%EC%9E%A1%ED%99%94/60/",
]

PRODUCT_DOMAIN = "https://twinscorestore.co.kr"

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

def is_excluded(url):
    decoded = urllib.parse.unquote(url)
    return any(kw in decoded for kw in EXCLUDE_KEYWORDS)

def dismiss_overlays(page):
    try:
        page.evaluate(REMOVE_OVERLAYS_JS)
    except Exception:
        pass

MAX_PAGES_PER_CATEGORY = 30  # 안전장치 (무한루프 방지)

def scrape_links_on_current_page(page, links):
    """현재 로드된 페이지에서 상품 링크를 수집 + 스크롤로 지연로딩 요소도 추가 수집."""
    last_count = -1
    rounds_without_growth = 0
    for _ in range(60):
        hrefs = page.eval_on_selector_all(
            'a[href*="/product/"]', "els => els.map(e => e.href)"
        )
        for h in hrefs:
            h = h.split("?")[0]
            m = PRODUCT_URL_RE.search(h)
            if not m:
                continue
            canonical = f"{PRODUCT_DOMAIN}/product/{m.group(1)}/"
            if is_excluded(canonical):
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
    for base_url in CATEGORY_URLS:
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
            scrape_links_on_current_page(page, links)
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

    result = {}
    unparsed_entries = []
    for key, v in data.items():
        if not isinstance(v, dict):
            unparsed_entries.append(f"{key}={str(v)[:60]}")
            continue
        stock = v.get("stock_number")
        label = v.get("option_value") or v.get("option_text") or str(key)
        result[label] = stock if stock is not None else 0

    if result:
        return result, None
    if unparsed_entries:
        return {"재고": 0}, f"항목 형식이 달라 재고 0으로 처리함: {unparsed_entries[:5]}"

    return None, "option_stock_data는 있었지만 파싱 가능한 항목이 없음"

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
        result, diagnostic = parse_option_stock(option_data)
        if result is not None:
            return result, name, price, diagnostic

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

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    prev_products = load_previous()
    current_products = {}

    change_blocks = []
    full_stock_blocks = []
    low_stock_blocks = []
    price_change_lines = []
    error_lines = []

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

            prev_entry = prev_products.get(url, {})
            prev_stock = prev_entry.get("stock", {})
            prev_price = prev_entry.get("price")

            price_str = f" ({fmt_won(price)})" if price is not None else ""

            # 전체 재고 표시용: 모든 옵션을 항상 출력, 변동이 있으면 증감도 같이 표시
            full_option_lines = []
            changed_option_lines = []
            has_low_stock = False

            for size, qty in stock.items():
                diff = qty - prev_stock.get(size, qty)
                diff_str = ""
                if diff != 0:
                    sign = "+" if diff > 0 else ""
                    diff_str = f" ({sign}{diff})"
                    changed_option_lines.append(f"  - {size}: {qty}개{diff_str}")

                full_option_lines.append(f"  - {size}: {qty}개{diff_str}")

                if qty > 0 and qty < LOW_STOCK_THRESHOLD:
                    has_low_stock = True

            block_text = f"■ {link}{price_str}\n" + "\n".join(full_option_lines)
            full_stock_blocks.append(block_text)

            if has_low_stock:
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

    SCRIPT_VERSION = "v5-fullstock"  # 배포 확인용 - 이 값이 메시지에 안 보이면 구버전이 실행된 것

    header = (
        f"[전상품 재고 확인] {now} ({SCRIPT_VERSION})\n"
        f"확인된 상품: {len(current_products)}개"
    )

    # 메시지 순서: 헤더 -> 50개 미만 재고(상단) -> 재고 변동 -> 가격 변동 -> 전체 재고 -> 오류
    messages = [header]
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
