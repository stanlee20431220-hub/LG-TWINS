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
SCRIPT_VERSION = "v16-stock-schema2-browser-xhr"

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


def _explicit_server_stock(payload, item_code, product_no):
    """Trust only exact nonnegative stock tied to item/product and managed-stock metadata."""
    if not isinstance(payload, dict):
        return None
    value = None
    if payload.get("sItemCode") == item_code or payload.get("item_code") == item_code:
        value = payload.get("stock_number")
        if payload.get("product_no") is not None and str(payload["product_no"]) != str(product_no):
            return None
    row = payload.get(item_code)
    if value is None and isinstance(row, dict):
        if row.get("item_code") not in (None, item_code):
            return None
        if row.get("product_no") is not None and str(row["product_no"]) != str(product_no):
            return None
        value = row.get("stock_number")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not re.fullmatch(r"\d+", text):
        return None
    quantity = int(text)
    return quantity if quantity <= 9007199254740991 else None


def get_explicit_stock_via_request(page, domain, product_no, item_code):
    """Same-origin XHR-style fallback, trusting only explicit JSON inventory."""
    origin = urllib.parse.urlparse(domain)
    page_url = urllib.parse.urlparse(getattr(page, "url", ""))
    safe_host = lambda host: re.sub(r"[^A-Za-z0-9._-]", "", host or "unknown")[:253]
    final_host = safe_host(origin.hostname)
    page_host = safe_host(page_url.hostname)
    status, content_type = "unknown", "unknown"

    def diagnostic(failure):
        return (
            f"request-fallback: {failure}; HTTP={status}; type={content_type}; "
            f"response-host={final_host}; page-host={page_host}"
        )

    def same_origin(left, right):
        return (left.scheme, left.hostname, left.port or (443 if left.scheme == "https" else 80)) == (
            right.scheme, right.hostname, right.port or (443 if right.scheme == "https" else 80)
        )

    if item_code == "__single":
        return None, diagnostic("unsupported_single_item")
    if origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password or page_url.username or page_url.password or not same_origin(origin, page_url):
        return None, diagnostic("same_origin_required")
    query = urllib.parse.urlencode({
        "product_no": str(product_no),
        "is_subscription": "F",
        f"product[{item_code}]": "9999",
    })
    url = f"{domain}/exec/front/shop/CalculatorProduct?{query}"
    # Exclude credentials, query and fragment from the same-origin Referer.
    referer = urllib.parse.urlunparse((page_url.scheme, origin.netloc, page_url.path, "", "", ""))
    headers = {
        "Referer": referer,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    failure = "no_explicit_stock"
    for attempt in range(3):
        response = None
        status, content_type = "unknown", "unknown"
        try:
            response = page.request.get(url, timeout=8000, headers=headers)
            status = response.status
            response_url = urllib.parse.urlparse(response.url)
            final_host = safe_host(response_url.hostname)
            response_headers = response.headers
            raw_type = next((value for key, value in response_headers.items() if key.lower() == "content-type"), "unknown")
            media_type = str(raw_type).split(";", 1)[0].strip().lower()
            content_type = re.sub(r"[^A-Za-z0-9/._+-]", "", media_type)[:80] or "unknown"
            if not same_origin(origin, response_url):
                failure = "unexpected_response_origin"
                break
            if not response.ok:
                failure = "HTTPError"
            else:
                is_json_type = media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))
                if not is_json_type:
                    # Some same-origin Cafe24 endpoints mislabel JSON as text/html.
                    # Inspect only the prefix; never feed an HTML document to JSON parsing.
                    body = response.text().lstrip()
                    if not body.startswith("{"):
                        failure = "non_json_response"
                        break
                payload = response.json()
                exact = _explicit_server_stock(payload, item_code, product_no)
                if exact is not None:
                    return exact, None
                # A price calculation for 9999 units is not an exact inventory value.
                failure = "no_matching_explicit_stock"
                break
        except Exception as exc:
            failure = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:40] or "RequestError"
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    pass
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    return None, diagnostic(failure)


def get_explicit_stock_via_browser_xhr(page, domain, product_no, item_code):
    """Ask the current Chromium page for explicit inventory using its native XHR."""
    status, media_type, response_host = "unknown", "unknown", "unknown"

    def diagnostic(reason):
        return (f"browser-xhr: {reason}; HTTP={status}; type={media_type}; "
                f"response-host={response_host}")

    def origin_key(url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid origin")
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    js = r"""
async (args) => {
 const endpoint='/exec/front/shop/CalculatorProduct';
 try {
  const base=new URL(args.domain), here=new URL(window.location.href);
  const product=here.pathname.match(/\/product\/(?:[^/]+\/)?(\d+)\/?$/);
  if(base.origin!==here.origin||base.username||base.password||here.username||here.password||!product||product[1]!==args.productNo)return {error:'same_origin_product_required'};
  if(typeof window.XMLHttpRequest!=='function')return {error:'unsupported_xhr'};
  const url=new URL(endpoint,base);
  url.search=new URLSearchParams({product_no:args.productNo,is_subscription:'F',['product['+args.itemCode+']']:'9999'});
  const category=window.iCategoryNo;
  if((typeof category==='number'||typeof category==='string')&&/^\d+$/.test(String(category))&&Number.isSafeInteger(Number(category))&&Number(category)>0)url.searchParams.set('cate_no',String(Number(category)));
  return await new Promise(resolve=>{
   const xhr=new window.XMLHttpRequest();let done=false;
   const finish=error=>{if(done)return;done=true;try{resolve({error,status:xhr.status,responseURL:xhr.responseURL,contentType:xhr.getResponseHeader('Content-Type')||'',responseText:error?'':xhr.responseText,capturedAt:new Date().toISOString()});}catch(e){resolve({error:'response_read_failure'});}};
   xhr.open('GET',url.href,true);xhr.timeout=8000;
   xhr.setRequestHeader('Accept','application/json, text/javascript, */*; q=0.01');
   xhr.setRequestHeader('X-Requested-With','XMLHttpRequest');
   xhr.onload=()=>finish(null);xhr.onerror=()=>finish('network_failure');xhr.ontimeout=()=>finish('timeout');xhr.onabort=()=>finish('aborted');
   xhr.send();
  });
 }catch(e){return {error:'xhr_exception'};}
}
    """
    try:
        if item_code == "__single":
            return None, diagnostic("unsupported_single_item"), None
        if not isinstance(item_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item_code) or not re.fullmatch(r"[1-9]\d*", str(product_no)):
            return None, diagnostic("invalid_item_product"), None
        page_url = getattr(page, "url", "")
        if origin_key(domain) != origin_key(page_url) or extract_product_no(urllib.parse.urlparse(page_url).path) != str(product_no) or not re.fullmatch(r"/product/(?:[^/]+/)?\d+/?", urllib.parse.urlparse(page_url).path):
            return None, diagnostic("same_origin_product_required"), None
        result = page.evaluate(js, {"domain": domain, "productNo": str(product_no), "itemCode": item_code})
        if not isinstance(result, dict):
            return None, diagnostic("malformed_result"), None
        raw_status = result.get("status")
        status = raw_status if type(raw_status) is int else "unknown"
        raw_type = result.get("contentType", "")
        actual_type = raw_type.split(";", 1)[0].strip().lower() if isinstance(raw_type, str) else ""
        media_type = re.sub(r"[^A-Za-z0-9/._+-]", "", actual_type)[:80] or "unknown"
        if result.get("error"):
            reason = result["error"]
            allowed = {"same_origin_product_required", "unsupported_xhr", "network_failure", "timeout", "aborted", "response_read_failure", "xhr_exception"}
            return None, diagnostic(reason if isinstance(reason, str) and reason in allowed else "xhr_failure"), None
        response = urllib.parse.urlparse(result.get("responseURL", ""))
        response_host = re.sub(r"[^A-Za-z0-9._-]", "", response.hostname or "unknown")[:253]
        if origin_key(result.get("responseURL", "")) != origin_key(domain):
            return None, diagnostic("unexpected_response_origin"), None
        if response.path != "/exec/front/shop/CalculatorProduct" or response.fragment:
            return None, diagnostic("unexpected_response_endpoint"), None
        query = urllib.parse.parse_qs(response.query, keep_blank_values=True)
        if query.get("product_no") != [str(product_no)] or query.get(f"product[{item_code}]") != ["9999"]:
            return None, diagnostic("unexpected_response_product_item"), None
        if type(raw_status) is not int or not 200 <= raw_status < 300:
            return None, diagnostic("HTTPError"), None
        if not (actual_type == "application/json" or (actual_type.startswith("application/") and actual_type.endswith("+json") and re.fullmatch(r"application/[A-Za-z0-9!#        if not (media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))):
            return None, diagnostic("non_json_response"), None^_.+-]+", actual_type))):
            return None, diagnostic("non_json_response"), None
        body = result.get("responseText")
        if not isinstance(body, str) or len(body) > 1_000_000 or not body.lstrip().startswith("{"):
            return None, diagnostic("non_json_response"), None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None, diagnostic("malformed_json"), None
        quantity = _explicit_server_stock(payload, item_code, product_no)
        if quantity is None:
            return None, diagnostic("no_matching_explicit_stock"), None
        captured = datetime.fromisoformat(result["capturedAt"].replace("Z", "+00:00"))
        if captured.tzinfo is None or not -5 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 60:
            return None, diagnostic("invalid_completion_time"), None
        capture_info = {"captured_at": captured.astimezone(timezone.utc).isoformat(), "quantity_checked_at": captured.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")}
        return quantity, None, capture_info
    except Exception as exc:
        return None, diagnostic(re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:40] or "XHRFailure"), None


def get_explicit_stock_via_browser_evidence(domain, product_no, item_code, now=None):
    """Optional last-tier evidence from a recent, same-origin browser API capture."""
    raw = os.environ.get("BROWSER_STOCK_EVIDENCE_JSON", "")
    if not raw or len(raw.encode("utf-8")) > 1_000_000 or item_code == "__single":
        return None, None
    try:
        evidence = json.loads(raw)
        if not isinstance(evidence, dict) or type(evidence.get("schema")) is not int or evidence["schema"] != 1:
            return None, None
        captures = evidence.get("captures")
        if not isinstance(captures, list) or len(captures) > 1000:
            return None, None
        origin = urllib.parse.urlparse(domain)
        def same_origin(url):
            return (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)) == (
                origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80)
            ) and not url.username and not url.password
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password:
            return None, None
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            return None, None
        valid = []
        for capture in captures:
            if not isinstance(capture, dict):
                continue
            try:
                if str(capture.get("product_no")) != str(product_no) or capture.get("item_code") != item_code:
                    continue
                captured = datetime.fromisoformat(capture["captured_at"].replace("Z", "+00:00"))
                if captured.tzinfo is None or not 0 <= (current - captured).total_seconds() <= 600:
                    continue
                request = urllib.parse.urlparse(capture["request_url"])
                response = urllib.parse.urlparse(capture["response_url"])
                endpoint = "/exec/front/shop/CalculatorProduct"
                if not same_origin(request) or not same_origin(response) or request.path != endpoint or response.path != endpoint:
                    continue
                query = urllib.parse.parse_qs(request.query, keep_blank_values=True)
                if query.get("product_no") != [str(product_no)] or f"product[{item_code}]" not in query:
                    continue
                media_type = capture["content_type"].split(";", 1)[0].strip().lower()
                if not (media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))):
                    continue
                qty = _explicit_server_stock(capture.get("api_payload"), item_code, product_no)
                if qty is not None:
                    valid.append((captured, qty))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if valid:
            captured, qty = max(valid, key=lambda entry: entry[0])
            return qty, {"captured_at": captured.isoformat(), "quantity_checked_at": captured.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")}
    except (ValueError, TypeError, AttributeError):
        pass
    return None, None


def get_option_stock_via_calculator(page, domain, product_no, option_data_json):
    """Use only validated explicit inventory; calculator purchase checks are not stock."""
    js = r"""
async (args) => {
 const items=typeof args.optionDataJson==='string'?JSON.parse(args.optionDataJson):args.optionDataJson;
 if(!items||typeof items!=='object'||Array.isArray(items))return {error:'invalid options'};
 const selects=[...document.querySelectorAll('select[id*="option"],select[name*="option"]')].filter(s=>!s.closest('.xans-product-addproduct,[class*="addproduct"]'));
 const flag=x=>x===true||String(x).toUpperCase()==='T'?true:x===false||String(x).toUpperCase()==='F'?false:null;
 const number=x=>(typeof x==='number'||typeof x==='string')&&/^\d+$/.test(String(x))&&Number.isSafeInteger(Number(x))?Number(x):null;
 const cap=number(window.product_max),hasCap=cap>0&&flag(window.product_max_type)===true;
 const productSold=flag(window.is_soldout_icon)===true;
 const madeOrder=String(args.productNo)==='2812'&&/^(www\.)?nolmdshop\.com$/.test(new URL(args.domain).hostname);
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 async function explicitStock(code){
  if(code==='__single')return null;
  const u=new URL('/exec/front/shop/CalculatorProduct',args.domain);u.search=new URLSearchParams({product_no:String(args.productNo),is_subscription:'F',['product['+code+']']:'9999'});
  for(let attempt=0;attempt<3;attempt++){
   const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
   try{const response=await fetch(u.href,{signal:controller.signal});if(!response.ok)throw new Error('HTTP');const d=await response.json();
    if(!d||typeof d!=='object')throw new Error('JSON');const row=d[code];
    if((d.sItemCode===code||d.item_code===code)&&(d.product_no==null||String(d.product_no)===String(args.productNo)))return number(d.stock_number);
    if(row&&(!row.item_code||row.item_code===code)&&(row.product_no==null||String(row.product_no)===String(args.productNo)))return number(row.stock_number);
    return null;
   }catch(e){if(attempt===2)return null;}finally{clearTimeout(timer);}await wait(250*(attempt+1));
  }return null;
 }
 // Structured offers supply availability only, never an inventory quantity.
 const structuredOffers=[];
 const collect=node=>{
  if(Array.isArray(node)){node.forEach(collect);return;}
  if(!node||typeof node!=='object')return;
  if(node['@type']==='ProductGroup'&&Array.isArray(node.hasVariant)){
   for(const variant of node.hasVariant){
    if(!variant||variant['@type']!=='Product')continue;
    for(const offer of Array.isArray(variant.offers)?variant.offers:[variant.offers])
     if(offer&&typeof offer==='object')structuredOffers.push({sku:variant.sku,offer});
   }
  }
  if(node['@graph'])collect(node['@graph']);
 };
 for(const script of document.querySelectorAll('script[type="application/ld+json"]')){
  try{collect(JSON.parse(script.textContent||''));}catch(e){}
 }
 function structuredAvailability(code){
  const matches=[];
  for(const {sku,offer} of structuredOffers){
   if(typeof sku!=='string'||!sku.endsWith('_'+args.productNo+'_'+code))continue;
   try{
    const u=new URL(offer.url,args.domain),origin=new URL(args.domain).origin;
    const product=u.pathname.match(/\/product\/(?:[^/]+\/)?(\d+)\/?$/);
    if(u.origin!==origin||!product||product[1]!==String(args.productNo)||u.searchParams.get('item_code')!==code)continue;
    if(/^https?:\/\/schema\.org\/InStock$/.test(offer.availability))matches.push('selectable');
    if(/^https?:\/\/schema\.org\/OutOfStock$/.test(offer.availability))matches.push('soldout');
   }catch(e){}
  }
  return matches.length&&matches.every(x=>x===matches[0])?matches[0]:null;
 }
 const out={},info={};
 for(const [code,v0]of Object.entries(items)){
  const v=v0&&typeof v0==='object'?v0:{};const name=code==='__single'?'재고':v.option_value||v.option_text||code;
  const domOptions=selects.flatMap(s=>[...s.options].filter(o=>o.value===code||String(o.textContent||'').replace(/\s*\[품절\]\s*$/,'').trim()===String(name).trim()).map(o=>({o,s})));
  const sold=productSold||domOptions.some(({o})=>/\[품절\]/.test(o.textContent||''));
  const selectable=domOptions.some(({o,s})=>!s.disabled&&!o.disabled&&!/\[품절\]/.test(o.textContent||''))||(code==='__single'&&flag(window.is_soldout_icon)===false);
  const managed=flag(v.use_stock)??flag(v.use_stock_original)??flag(window.use_stock);
  const selling=flag(v.is_selling),displayed=flag(v.is_display);
  let kind='quantity_unknown',availability=selectable?'selectable':'unknown',qty=selectable?-2:-1,source='selection',availability_source=selectable?'dom':null;
  if(sold){kind='soldout';availability='soldout';qty=0;source='dom';availability_source='dom';}
  // Explicit storefront soldout wins; blocked options are not warehouse zero.
  else if(selling===false||displayed===false){kind='not_selling';availability='not_selling';qty=-1;source='page_flags';availability_source='page_flags';}
  else if(madeOrder){kind='made_to_order';source='user_override';}
  else if(managed===false){kind='stock_unmanaged';source='page_flags';}
  else {
   const structured=availability==='unknown'?structuredAvailability(code):null;
   if(structured==='selectable'){availability='selectable';qty=-2;availability_source='jsonld';}
   let exact=managed===true?number(v.stock_number):null;
   if(structured==='soldout'&&(exact===null||exact===0)){kind='soldout';availability='soldout';qty=0;source='jsonld';availability_source='jsonld';}
   else {
   if(exact===0&&availability==='selectable')exact=null;
   if(exact!==null){kind='server_stock';qty=exact;source='page';}
   else if(hasCap){kind='purchase_limit';}
   else if(managed===true){exact=await explicitStock(code);if(exact!==null&&!(exact===0&&availability==='selectable')){kind='server_stock';qty=exact;source='calculator';}}
  }
  }
  out[name]=qty;info[name]={kind,availability,availability_source,purchase_limit:hasCap?cap:null,source,stock_managed:managed,selling,displayed};
 }
 window.__lg_stock_option_info=info;return {data:out,option_info:info};
}
    """
    try:
        outcome = page.evaluate(js, {"domain":domain,"productNo":product_no,"optionDataJson":option_data_json})
    except Exception as exc:
        return None, f"CalculatorProduct 평가 실패({type(exc).__name__})"
    if not outcome or outcome.get("error"):
        return None, "옵션 재고 메타데이터 조회 실패"
    data=outcome.get("data") or {}
    infos=outcome.get("option_info") or {}
    # Same-context request fallback is restricted to managed, uncapped options.
    failures = []
    try:
        items = json.loads(option_data_json) if isinstance(option_data_json, str) else option_data_json
        for code, item in items.items():
            if code == "__single" or not isinstance(item, dict):
                continue
            label = item.get("option_value") or item.get("option_text") or code
            info = infos.get(label) or {}
            if data.get(label, -1) < 0 and info.get("kind") == "quantity_unknown" and info.get("stock_managed") is True and not info.get("purchase_limit"):
                try:
                    exact, note = get_explicit_stock_via_request(page, domain, product_no, code)
                except Exception as exc:
                    exact, note = None, f"request-fallback: {type(exc).__name__}"
                if exact is not None and not (exact == 0 and info.get("availability") == "selectable"):
                    data[label] = exact
                    info.update(kind="server_stock", source="calculator")
                    info.pop("diagnostic", None)
                    info.pop("error", None)
                else:
                    note = note or "request-fallback: selectable_zero_not_confirmed"
                    info["diagnostic"] = note
                    info["error"] = "quantity_unknown" if "no_matching_explicit_stock" in note or "selectable_zero_not_confirmed" in note else "network_failure"
                    path = ["page", "calculator", "request_fallback"]
                    xhr_qty, xhr_note, xhr_capture = None, None, None
                    if info.get("availability") == "selectable" and info.get("selling") is not False and info.get("displayed") is not False:
                        path.append("browser_xhr")
                        try:
                            xhr_qty, xhr_note, xhr_capture = get_explicit_stock_via_browser_xhr(page, domain, product_no, code)
                        except Exception as exc:
                            xhr_note = f"browser-xhr: {type(exc).__name__}"
                        if xhr_qty == 0:
                            xhr_note = "browser-xhr: selectable_zero_not_confirmed"
                    info["primary_diagnostic"] = note
                    if xhr_qty is not None and xhr_qty > 0 and isinstance(xhr_capture, dict) and xhr_capture.get("quantity_checked_at"):
                        data[label] = xhr_qty
                        info.update(kind="server_stock", source="browser_xhr", quantity_checked_at=xhr_capture["quantity_checked_at"], lookup_path=path)
                        info.pop("diagnostic", None)
                        info.pop("error", None)
                    else:
                        # Optional recent captures remain the final tier, never the primary lookup.
                        evidence_qty, capture_info = get_explicit_stock_via_browser_evidence(domain, product_no, code) if info.get("availability") == "selectable" else (None, None)
                        if xhr_note:
                            info["browser_xhr_diagnostic"] = xhr_note
                        if evidence_qty is not None and evidence_qty > 0:
                            data[label] = evidence_qty
                            info.update(kind="server_stock", source="verified_browser", quantity_checked_at=capture_info["quantity_checked_at"], lookup_path=path + ["verified_browser"])
                            info.pop("diagnostic", None)
                            info.pop("error", None)
                        else:
                            info["lookup_path"] = path
                            info["diagnostic"] = " | ".join(filter(None, [note, xhr_note]))
                            failures.append(f"{label}: {info['diagnostic']}")
                infos[label] = info
    except Exception as exc:
        failures.append(f"request-fallback metadata: {type(exc).__name__}")
    try:
        page.evaluate("info => { window.__lg_stock_option_info = info; }", infos)
    except Exception as exc:
        failures.append(f"option metadata persistence: {type(exc).__name__}")
    # Partial failures must not suppress genuine counts on successful options.
    return (data or None), (" | ".join(failures) if failures else None)


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
        try:
            data = json.loads(single_data) if isinstance(single_data, str) else single_data
            if isinstance(data, dict):
                parsed_url = urllib.parse.urlparse(url)
                stock, note = get_option_stock_via_calculator(page, f"{parsed_url.scheme}://{parsed_url.netloc}", extract_product_no(url), {"__single": data})
                return stock, name, price, note
        except Exception as exc:
            diagnostic = f"{UNCERTAIN_MARKER} 단일상품 판정 실패({type(exc).__name__})"

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
        "inventory_schema": saved.get("inventory_schema"),
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
            {"last_checked": now, "inventory_schema": 2, "products": products},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(temp_path, HISTORY_FILE)


def fmt_won(value):
    return f"{value:,}원"


def stock_status(qty, info=None):
    info = info or {}
    kind = info.get("kind")
    labels = {"made_to_order": "주문제작 · 수량 없음", "stock_unmanaged": "재고 미관리 · 수량 없음",
              "purchase_limit": "구매제한 있음 · 재고 미공개", "not_selling": "구매불가 · 비노출/판매중지",
              "quantity_unknown": "구매가능 · 수량미확인" if info.get("availability") == "selectable" else "확인불가"}
    # API failure is not evidence of soldout; retain storefront availability.
    if kind == "quantity_unknown" and info.get("diagnostic"):
        return "?", "구매가능 · 수량조회실패" if info.get("availability") == "selectable" else "재고 조회 실패 · 수량 미확인"
    if kind in labels:
        return "[정책]", labels[kind]
    qty = _to_int(qty)
    if kind not in {"server_stock", "soldout"} or qty is None or qty < 0:
        return "?", "확인불가"
    if qty == 0:
        return "[품절]", "품절"
    if qty < LOW_STOCK_THRESHOLD:
        return "[저재고]", f"{qty}개 ({LOW_STOCK_THRESHOLD}개 미만)"
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
                option_info = page.evaluate("() => window.__lg_stock_option_info || null") or {}
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
                info = option_info.get(str(option)) or {}
                if info.get("kind") not in {"server_stock", "soldout"} and qty >= 0:
                    qty = -2 if info.get("availability") == "selectable" else -1
                normalized_stock[str(option)] = qty
            if normalization_issues:
                extra = f"{UNCERTAIN_MARKER} 수량 변환 실패 옵션: {normalization_issues[:5]}"
                diagnostic = f"{diagnostic} | {extra}" if diagnostic else extra

            uncertain = bool(diagnostic and UNCERTAIN_MARKER in diagnostic)
            entry = {
                "name": name,
                "price": price,
                "stock": normalized_stock,
                "option_info": option_info,
                "inventory_schema": 2,
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
                or history.get("inventory_schema") != 2
                or previous_entry.get("inventory_schema") != 2
            )
            price_text = f" ({fmt_won(price)})" if price is not None else ""
            full_option_lines = []
            changed_option_lines = []
            has_low_stock = False

            for option, qty in normalized_stock.items():
                symbol, status_label = stock_status(qty, option_info.get(option))
                display_text = f"{symbol} {status_label}"
                diff_text = ""
                previous_exists = option in previous_stock
                previous_qty = _to_int(previous_stock.get(option)) if previous_exists else None
                comparable = (
                    not uncertain
                    and not previous_unreliable
                    and qty >= 0
                    and (option_info.get(option) or {}).get("kind") in {"server_stock", "soldout"}
                    and ((previous_entry.get("option_info") or {}).get(option) or {}).get("kind") in {"server_stock", "soldout"}
                    and previous_exists
                    and previous_qty is not None
                    and previous_qty >= 0
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
