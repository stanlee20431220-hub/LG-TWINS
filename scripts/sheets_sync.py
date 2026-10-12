"""Google Sheets export helpers for the LG Twins stock monitor.

Uses google-auth only for service-account OAuth and urllib for the Sheets API.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
HEADERS = [
    "상품명",
    "옵션",
    "재고 수량",
    "상품 링크",
    "이미지",
    "확인 시각",
    "이전 수량",
    "증감",
    "상태",
    "가격",
    "출처 도메인",
    "구매제한",
    "수량 근거",
]
CAP_SENTINEL = 9999
UNKNOWN_SENTINEL = -1
SELECTABLE_UNKNOWN_SENTINEL = -2


class SheetSyncError(RuntimeError):
    """Safe-to-report Sheets export failure (never includes credentials)."""


TRUSTED_KINDS = {"server_stock", "soldout"}


def _trusted_option(entry, option):
    info = (entry.get("option_info") or {}).get(option) or {}
    return (entry.get("inventory_schema") == 2 and info.get("kind") in TRUSTED_KINDS
            and not entry.get("stale") and not entry.get("uncertain"))


def _exact_quantity(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        qty = int(value)
    except (TypeError, ValueError):
        return None
    return qty if qty >= 0 else None


def quantity_display(value, info=None):
    info = info or {}
    kind = info.get("kind")
    if kind in {"stock_unmanaged", "made_to_order"}:
        return "수량 없음"
    if kind in TRUSTED_KINDS:
        qty = _exact_quantity(value)
        return qty if qty is not None else "확인불가"
    if kind == "quantity_unknown":
        return "미공개" if info.get("availability") == "selectable" else "확인불가"
    if kind in {"purchase_limit", "not_selling"}:
        return "미공개"
    if value is None or value == "":
        return ""
    return "확인불가" if value == -1 else "미검증"


def _transition(current, previous, stale=False, uncertain=False):
    if stale or uncertain:
        return None
    current = _exact_quantity(current)
    previous = _exact_quantity(previous)
    if current is None or previous is None:
        return None
    if current == 0 and previous > 0:
        return "soldout"
    if current > 0 and previous == 0:
        return "restocked"
    return None


def _status_for(current, previous, stale=False, uncertain=False, low_threshold=50,
                suppress_transition=False, info=None):
    if stale:
        return "오류 · 이전 데이터"
    info = info or {}
    kind = info.get("kind")
    if uncertain:
        return "확인불가 · 불확실"
    if kind == "made_to_order":
        return "주문제작 · 수량 없음"
    if kind == "stock_unmanaged":
        return "재고 미관리 · 수량 없음"
    if kind == "purchase_limit":
        return "선택가능 · 구매제한 있음 · 재고 미공개" if info.get("availability") == "selectable" else "구매제한 있음 · 재고 미공개"
    if kind == "not_selling":
        return "판매중지 · 수량 미공개"
    if kind == "quantity_unknown":
        return "선택가능 · 수량 미공개" if info.get("availability") == "selectable" else "확인불가"
    if kind not in TRUSTED_KINDS:
        return "확인불가 · 이전 방식 미검증"
    qty = _exact_quantity(current)
    if qty is None:
        return "확인불가"
    change = _transition(qty, previous, uncertain=suppress_transition)
    if change == "soldout":
        return "신규 품절"
    if change == "restocked":
        return "재입고"
    if qty == 0:
        return "품절"
    return "저재고" if qty < low_threshold else "재고 있음"


def image_formula(image_url):
    if not image_url or not str(image_url).lower().startswith("https://"):
        return ""
    escaped = str(image_url).replace('"', '""')
    return f'=IMAGE("{escaped}",4,80,80)'


def build_sheet_rows(products, previous_products=None, default_checked_time="", low_threshold=50):
    previous_products = previous_products or {}
    rows = [list(HEADERS)]
    sources = {"page": "페이지 재고값", "calculator": "재고관리 활성 · 서버값",
               "selection": "옵션 선택 상태", "dom": "옵션 품절 표시",
               "user_override": "사용자 제보", "page_flags": "페이지 재고관리 설정"}
    for url in sorted(products):
        entry = products[url] or {}
        previous = previous_products.get(url) or {}
        previous_stock = previous.get("stock") or {}
        stale, uncertain = bool(entry.get("stale")), bool(entry.get("uncertain"))
        infos = entry.get("option_info") or {}
        previous_infos = previous.get("option_info") or {}
        for option, qty in (entry.get("stock") or {"-": -1}).items():
            info = infos.get(option) or {}
            previous_qty = previous_stock.get(option)
            current_trusted = _trusted_option(entry, option)
            previous_trusted = option in previous_stock and _trusted_option(previous, option)
            current_exact = _exact_quantity(qty) if current_trusted else None
            previous_exact = _exact_quantity(previous_qty) if previous_trusted else None
            delta = current_exact - previous_exact if current_exact is not None and previous_exact is not None else ""
            limit = info.get("purchase_limit")
            rows.append([
                entry.get("name") or url, option,
                quantity_display(qty, info if entry.get("inventory_schema") == 2 else None),
                url, image_formula(entry.get("image_url")),
                entry.get("checked_at") or ("" if stale else default_checked_time),
                quantity_display(previous_qty, previous_infos.get(option)) if previous_trusted else "",
                delta,
                _status_for(qty, previous_qty, stale, uncertain, low_threshold,
                            suppress_transition=not (current_trusted and previous_trusted), info=info if entry.get("inventory_schema") == 2 else None),
                entry.get("price") if entry.get("price") is not None else "",
                urllib.parse.urlparse(url).netloc.lower(),
                f"{limit}개" if isinstance(limit, int) and limit > 0 else "",
                sources.get(info.get("source"), "미확인"),
            ])
    return rows


def summarize_inventory(products, previous_products=None, low_threshold=50, item_limit=12):
    previous_products = previous_products or {}
    soldout, restocked, low = [], [], []
    for url in sorted(products):
        entry = products[url] or {}
        previous = previous_products.get(url) or {}
        for option, raw_qty in (entry.get("stock") or {}).items():
            if not _trusted_option(entry, option):
                continue
            qty = _exact_quantity(raw_qty)
            if qty is None:
                continue
            label = f"{entry.get('name') or url} / {option}"
            old = (previous.get("stock") or {}).get(option) if _trusted_option(previous, option) else None
            change = _transition(qty, old)
            if change == "soldout":
                soldout.append(label)
            elif change == "restocked":
                restocked.append(label)
            if 0 < qty < low_threshold:
                low.append(f"{label}: {qty}개")
    prioritized = ([('신규 품절', x) for x in soldout] + [('재입고', x) for x in restocked] + [('저재고', x) for x in low])
    bounded = prioritized[:max(0, int(item_limit))]
    return {"newly_soldout": len(soldout), "restocked": len(restocked), "low_stock": len(low),
            "items": bounded, "omitted": max(0, len(prioritized) - len(bounded))}


def _safe_sheet_title(title):
    return "'" + str(title).replace("'", "''") + "'"


def _load_service_account_info(raw_value):
    if not raw_value or not str(raw_value).strip():
        raise SheetSyncError("GOOGLE_SERVICE_ACCOUNT_JSON이 설정되지 않았습니다")
    value = str(raw_value).strip()
    try:
        if value.startswith("{"):
            info = json.loads(value)
        elif os.path.isfile(value):
            with open(value, "r", encoding="utf-8") as handle:
                info = json.load(handle)
        else:
            raise ValueError("not JSON or a readable file")
    except Exception as exc:
        raise SheetSyncError("서비스 계정 JSON을 읽을 수 없습니다") from exc
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise SheetSyncError("서비스 계정 JSON 형식이 올바르지 않습니다")
    return info


def get_access_token(service_account_json):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise SheetSyncError("google-auth 의존성이 설치되지 않았습니다") from exc

    info = _load_service_account_info(service_account_json)
    try:
        credentials = Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
        credentials.refresh(Request())
    except Exception as exc:
        raise SheetSyncError("Google 서비스 계정 인증에 실패했습니다") from exc
    if not credentials.token:
        raise SheetSyncError("Google 서비스 계정 토큰을 받지 못했습니다")
    return credentials.token


def _request_json(method, url, token, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = detail.get("error", {}).get("message", "")
        except Exception:
            message = ""
        suffix = f": {message[:300]}" if message else ""
        raise SheetSyncError(f"Google Sheets API HTTP {exc.code}{suffix}") from exc
    except Exception as exc:
        raise SheetSyncError(f"Google Sheets API 연결 실패({type(exc).__name__})") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SheetSyncError("Google Sheets API 응답을 해석할 수 없습니다") from exc


def _format_requests(sheet_id, row_count, conditional_rule_count=0):
    requests = []
    for index in range(conditional_rule_count - 1, -1, -1):
        requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}})

    requests.extend([
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.13, "green": 0.24, "blue": 0.39}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3},
                "cell": {"note": "숫자는 재고관리 활성 상태에서 확인된 서버 재고값만 표시합니다. 구매제한이 있는 상품의 CalculatorProduct 수량은 재고로 쓰지 않습니다. 주문제작·재고 미관리는 수량 없음, 수량을 공개하지 않는 상품은 미공개로 표시합니다. 이전 방식 수량은 비교 기준에서 제외합니다."},
                "fields": "note",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 10, "endColumnIndex": 11},
                "cell": {"note": "출처 도메인은 각 행의 상품 링크 호스트입니다."},
                "fields": "note",
            }
        },
    ])
    if row_count > 1:
        requests.extend([
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_count, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                    "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_count, "startColumnIndex": 9, "endColumnIndex": 10},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0\"원\""}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": row_count},
                    "properties": {"pixelSize": 88},
                    "fields": "pixelSize",
                }
            },
        ])

    widths = [260, 130, 110, 280, 100, 155, 105, 80, 225, 105, 150, 100, 170]
    for index, width in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    requests.append({
        "setBasicFilter": {
            "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": max(1, row_count), "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}
        }
    })

    if row_count > 1:
        status_rules = [
            (r'=REGEXMATCH($I2,"품절")', {"red": 0.96, "green": 0.80, "blue": 0.80}),
            (r'=REGEXMATCH($I2,"재입고")', {"red": 0.80, "green": 0.93, "blue": 0.82}),
            (r'=REGEXMATCH($I2,"저재고")', {"red": 1.0, "green": 0.93, "blue": 0.68}),
            (r'=REGEXMATCH($I2,"확인불가|오류|불확실")', {"red": 0.88, "green": 0.88, "blue": 0.88}),
        ]
        for formula, color in status_rules:
            requests.append({
                "addConditionalFormatRule": {
                    "index": 0,
                    "rule": {
                        "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_count, "startColumnIndex": 8, "endColumnIndex": 9}],
                        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]}, "format": {"backgroundColor": color}},
                    },
                }
            })
    return requests


def sync_to_sheet(spreadsheet_id, service_account_json, rows):
    """Atomically write the new table first, then clear only the stale tail and format it."""
    if not spreadsheet_id or not str(spreadsheet_id).strip():
        raise SheetSyncError("GOOGLE_SHEET_ID가 설정되지 않았습니다")
    if not rows or rows[0] != HEADERS:
        raise SheetSyncError("Sheets 내보내기 행 형식이 올바르지 않습니다")

    spreadsheet_id = str(spreadsheet_id).strip()
    token = get_access_token(service_account_json)
    quoted_id = urllib.parse.quote(spreadsheet_id, safe="")
    metadata_url = f"{SHEETS_API}/{quoted_id}?fields=sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),conditionalFormats)"
    metadata = _request_json("GET", metadata_url, token)
    sheets = metadata.get("sheets") or []
    if not sheets:
        raise SheetSyncError("스프레드시트에서 시트를 찾지 못했습니다")

    sheet = sheets[0]
    properties = sheet.get("properties") or {}
    sheet_id = properties.get("sheetId")
    title = properties.get("title")
    grid_properties = properties.get("gridProperties") or {}
    old_row_count = grid_properties.get("rowCount") or 0
    old_column_count = grid_properties.get("columnCount") or 0
    if sheet_id is None or not title:
        raise SheetSyncError("시트 메타데이터가 올바르지 않습니다")

    range_prefix = _safe_sheet_title(title)
    row_count = len(rows)
    batch_update_url = f"{SHEETS_API}/{quoted_id}:batchUpdate"

    # Grow the grid non-destructively before writing when the result exceeds the
    # current sheet bounds. This is not a clear/delete operation and preserves all
    # existing cells until the complete replacement table succeeds.
    if row_count > old_row_count or len(HEADERS) > old_column_count:
        grown_grid = {}
        fields = []
        if row_count > old_row_count:
            grown_grid["rowCount"] = row_count
            fields.append("gridProperties.rowCount")
        if len(HEADERS) > old_column_count:
            grown_grid["columnCount"] = len(HEADERS)
            fields.append("gridProperties.columnCount")
        _request_json(
            "POST",
            batch_update_url,
            token,
            {
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": grown_grid,
                        },
                        "fields": ",".join(fields),
                    }
                }]
            },
        )

    values_url = f"{SHEETS_API}/{quoted_id}/values:batchUpdate"
    values_payload = {
        "valueInputOption": "USER_ENTERED",
        "data": [{
            "range": f"{range_prefix}!A1:M{row_count}",
            "majorDimension": "ROWS",
            "values": rows,
        }],
    }

    # No destructive operation occurs before this complete-table write succeeds.
    _request_json("POST", values_url, token, values_payload)

    # Only after the new table exists do we remove values left from an older, longer run.
    if old_row_count > row_count:
        tail_range = urllib.parse.quote(f"{range_prefix}!A{row_count + 1}:M{old_row_count}", safe="")
        clear_url = f"{SHEETS_API}/{quoted_id}/values/{tail_range}:clear"
        _request_json("POST", clear_url, token, {})

    format_payload = {
        "requests": _format_requests(
            sheet_id,
            row_count,
            conditional_rule_count=len(sheet.get("conditionalFormats") or []),
        )
    }
    _request_json("POST", batch_update_url, token, format_payload)

    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet_id,
        "sheet_title": title,
        "row_count": max(0, row_count - 1),
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}",
    }
