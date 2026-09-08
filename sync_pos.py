import argparse
import json
import os
import time as _time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests

# Pancake POS analytics "Time.day" follows the shop calendar in Vietnam (Asia/Ho_Chi_Minh).
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Rolling sync (no --date) replaces each data_shop*.json with only this window. ~150 days ≈ 5 months.
DEFAULT_SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "150") or "150")


def utc_bounds_for_vn_calendar_day(date_str: str) -> tuple[str, str]:
    """since/until in UTC ISO Z matching one Vietnam local calendar day (YYYY-MM-DD)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_vn = datetime.combine(d, time.min, tzinfo=VN_TZ)
    end_vn = datetime.combine(d, time(23, 59, 59, 999000), tzinfo=VN_TZ)
    start_utc = start_vn.astimezone(timezone.utc)
    end_utc = end_vn.astimezone(timezone.utc)
    return _format_utc_z(start_utc), _format_utc_z(end_utc)


def _format_utc_z(dt_utc: datetime) -> str:
    dt_utc = dt_utc.astimezone(timezone.utc)
    ms = dt_utc.microsecond // 1000
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def rolling_bounds_vn_days_back(days: int) -> tuple[str, str]:
    """Window from start of (today VN - days) through end of today VN, as UTC Z strings."""
    now_vn = datetime.now(VN_TZ)
    end_vn = datetime.combine(now_vn.date(), time(23, 59, 59, 999000), tzinfo=VN_TZ)
    start_day = now_vn.date() - timedelta(days=days)
    start_vn = datetime.combine(start_day, time.min, tzinfo=VN_TZ)
    return _format_utc_z(start_vn.astimezone(timezone.utc)), _format_utc_z(end_vn.astimezone(timezone.utc))


def parse_args():
    parser = argparse.ArgumentParser(description="Sync POS analytics/sale data to data_shop*.json")
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help=(
            "Rolling window: days backward from now (full file replace per shop). Ignored if --date is set. "
            f"Default when omitted/zero: {DEFAULT_SYNC_DAYS} (env SYNC_DAYS)."
        ),
    )
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="Single calendar day YYYY-MM-DD in Vietnam (Asia/Ho_Chi_Minh); since/until sent to POS API in UTC.",
    )
    return parser.parse_args()


def normalize_time_day(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3:
            d, m, y = parts[0], parts[1], parts[2]
            if len(y) == 4:
                return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return text[:10] if len(text) >= 10 else text


def merge_data_rows(existing_rows: List[Any], new_rows: List[Any]) -> List[Dict[str, Any]]:
    """Upsert by normalized Time.day: new rows override same day; days only in existing are kept."""
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        key = normalize_time_day(row.get("Time.day", ""))
        if not key or len(key) != 10:
            continue
        by_day[key] = row
    for row in new_rows:
        if not isinstance(row, dict):
            continue
        key = normalize_time_day(row.get("Time.day", ""))
        if not key or len(key) != 10:
            continue
        by_day[key] = row

    def sort_key(r: Dict[str, Any]) -> str:
        return normalize_time_day(r.get("Time.day", ""))

    return sorted(by_day.values(), key=sort_key, reverse=True)


def build_payload(since_iso: str, until_iso: str) -> Dict[str, Any]:
    return {
        "params": {
            "returned_record": "success_record",
            "success_record": "inserted_at",
            "success_status": 1,
            "user_type": "update",
            "since": since_iso,
            "until": until_iso,
            "split_by": ["Time.day"],
            "select_fields": [
                "cod",
                "prepaid",
                "partner_fee",
                "total_order_count",
                "order_count",
                "order_count_pagination",
                "shipping_fee",
                "ads_amount",
                "exchange_order_count",
                "price",
                "exchange_payment",
                "discount",
                "capital",
                "surcharge",
                "fee_marketplace",
                "affiliate_price",
                "marketplace_voucher",
                "diff_shipping_fee",
                "prepaid_by_point",
            ],
            "render_fields": [
                "ads_amount",
                "success_order_count",
                "returned_order_count",
                "price",
                "discount",
                "shipping_fee",
                "partner_fee",
                "capital",
                "revenue",
                "profit",
                "sales",
                "ads_order",
                "avg_profit",
            ],
            "sorter": {"order": "descend", "field": "Time.day"},
            "pagination": {"pageSize": 100, "current": 1},
            "filter": {},
            "id": "8be9c3f7-5db7-41ab-859a-24daf90311f1",
        }
    }


def sync_one_shop(
    shop_key: str,
    shop_name: str,
    shop_id: str,
    access_token: str,
    cookie: str,
    payload: Dict[str, Any],
    *,
    merge: bool,
) -> None:
    since = payload["params"]["since"]
    until = payload["params"]["until"]
    print(f"Dang sync: {shop_name} ({shop_id})")
    print(f"Khoang ngay: {since} -> {until}")

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/analytics/sale"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/overview",
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
    }
    params = {"api_key": access_token}

    out_file = Path(f"data_{shop_key}.json")
    try:
        r = _post_with_retry(url, params=params, headers=headers, json_body=payload, timeout=60)
        data = r.json()

        if data.get("success") is not True:
            print("API loi:", data.get("message", "khong ro loi"))
            return

        if merge:
            existing: Dict[str, Any] = {}
            if out_file.is_file():
                try:
                    existing = json.loads(out_file.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            old_list = existing.get("data", []) if isinstance(existing.get("data"), list) else []
            new_list = data.get("data", []) if isinstance(data.get("data"), list) else []
            merged = merge_data_rows(old_list, new_list)
            out_doc = dict(existing)
            for k, v in data.items():
                if k != "data":
                    out_doc[k] = v
            out_doc["data"] = merged
            out_file.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"OK: da merge vao {out_file.name} ({len(merged)} ngay)")
        else:
            out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"OK: da luu vao {out_file.name}")

    except Exception as e:
        print("Loi khi sync", shop_name, "->", str(e))


def _post_with_retry(
    url: str,
    *,
    params: Dict,
    headers: Dict,
    json_body: Any,
    timeout: int = 60,
    max_retries: int = 3,
    backoff: float = 2.0,
) -> requests.Response:
    """POST với retry tự động khi gặp lỗi mạng / timeout tạm thời.
    Retry tối đa max_retries lần, mỗi lần chờ backoff * attempt giây.
    Không retry khi API trả lỗi HTTP >= 400 (lỗi logic, không phải mạng).
    """
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, params=params, headers=headers, json=json_body, timeout=timeout)
            # Không retry lỗi HTTP client/server (4xx, 5xx) — lỗi logic
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout as e:
            last_exc = e
            print(f"  [retry {attempt}/{max_retries}] Timeout, thu lai sau {backoff * attempt:.0f}s...")
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            print(f"  [retry {attempt}/{max_retries}] ConnectionError, thu lai sau {backoff * attempt:.0f}s...")
        except requests.exceptions.HTTPError as e:
            # HTTP error (4xx/5xx) — không retry
            raise
        except Exception as e:
            last_exc = e
            print(f"  [retry {attempt}/{max_retries}] Loi khong xac dinh: {e}, thu lai...")
        if attempt < max_retries:
            _time.sleep(backoff * attempt)
    raise last_exc


def _is_valid_hex_api_key(key: str) -> bool:
    """Kiểm tra api_key là hex 32 ký tự (không phải JWT cũ)."""
    if not key:
        return False
    k = str(key).strip()
    return len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k)


def load_shop_api_keys_from_db() -> Dict[str, str]:
    """Đọc per-shop api_key từ wh_shops (pos_shop_id → pos_api_key). Chỉ trả về hex key hợp lệ."""
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        return {}
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT pos_shop_id, pos_api_key FROM wh_shops "
            "WHERE pos_api_key IS NOT NULL AND pos_api_key != ''"
        )
        result: Dict[str, str] = {}
        for row in cur.fetchall():
            sid = str(row[0] or "").strip()
            key = str(row[1] or "").strip()
            if sid and _is_valid_hex_api_key(key):
                result[sid] = key
        cur.close()
        conn.close()
        return result
    except Exception as exc:
        print(f"WARN: Khong load duoc api_key tu DB: {exc}")
        return {}


def main() -> None:
    args = parse_args()
    single_date = str(args.date or "").strip()

    try:
        from shop_helpers import load_all_shops as _load_shops
        shops = _load_shops()
    except Exception:
        shops = []
    if not shops:
        # Fallback shops.json (legacy) — bỏ qua nếu file không tồn tại (DB là nguồn chính)
        try:
            with open("shops.json", "r", encoding="utf-8") as f:
                shops = json.load(f)
        except Exception:
            shops = []

    shop_api_keys = load_shop_api_keys_from_db()
    if not shop_api_keys:
        print("WARN: Khong co api_key nao trong DB. Vui long nhap api_key per-shop tai Cai dat.")

    try:
        with open("session.json", "r", encoding="utf-8") as f:
            session = json.load(f)
        cookie = session.get("cookie", "")
    except Exception:
        cookie = ""

    if single_date:
        try:
            datetime.strptime(single_date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"Invalid --date (expected YYYY-MM-DD): {single_date!r}")
        since, until = utc_bounds_for_vn_calendar_day(single_date)
        payload = build_payload(since, until)
        synced = 0
        for shop in shops:
            if shop.get("status") != "active":
                continue
            shop_id = str(shop.get("shop_id", "")).strip()
            token = shop_api_keys.get(shop_id, "")
            if not token:
                print(f"SKIP {shop.get('shop_name')} ({shop_id}): chua co api_key hop le")
                continue
            sync_one_shop(
                shop["shop_key"],
                shop["shop_name"],
                shop_id,
                token,
                cookie,
                payload,
                merge=True,
            )
            synced += 1
        print(f"=== Single-date sync done: {synced}/{len(shops)} shops ===")
        return

    days = int(args.days if args.days and args.days > 0 else DEFAULT_SYNC_DAYS)
    since, until = rolling_bounds_vn_days_back(days)
    payload = build_payload(since, until)

    synced = 0
    for shop in shops:
        if shop.get("status") != "active":
            continue
        shop_id = str(shop.get("shop_id", "")).strip()
        token = shop_api_keys.get(shop_id, "")
        if not token:
            print(f"SKIP {shop.get('shop_name')} ({shop_id}): chua co api_key hop le")
            continue
        sync_one_shop(
            shop["shop_key"],
            shop["shop_name"],
            shop_id,
            token,
            cookie,
            payload,
            merge=False,
        )
        synced += 1
    print(f"=== Rolling sync done: {synced}/{len(shops)} shops ===")


if __name__ == "__main__":
    main()
