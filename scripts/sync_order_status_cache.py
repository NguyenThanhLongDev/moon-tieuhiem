#!/usr/bin/env python3
"""
Sync order status aggregate counts for all active shops from Pancake API
into shop_order_status_cache table.

Usage:
    python3 sync_order_status_cache.py [--date YYYY-MM-DD] [--days N]

Default: syncs today (Vietnam time). With --days N, syncs last N days.
Dùng per-shop api_key (hex 32 ký tự) từ wh_shops DB — không cần cookie/JWT.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SHOPS_FILE = os.path.join(BASE_DIR, "shops.json")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

DELAY_BETWEEN_SHOPS = float(os.environ.get("ORDER_STATUS_SYNC_DELAY_SEC", "0.4"))


def _is_valid_hex_key(key: str) -> bool:
    k = str(key or "").strip()
    return len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k)


def load_shop_api_keys_from_db() -> dict:
    """Trả về {pos_shop_id: api_key} — chỉ key hex 32 ký tự hợp lệ."""
    try:
        from pancake_auth import is_valid_hex_api_key
    except Exception:
        is_valid_hex_api_key = _is_valid_hex_key
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT pos_shop_id, pos_api_key FROM wh_shops "
            "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' AND status='active'"
        )
        result = {
            str(r[0]): r[1]
            for r in cur.fetchall()
            if r[0] and is_valid_hex_api_key(str(r[1] or ""))
        }
        cur.close()
        conn.close()
        return result
    except Exception as exc:
        print(f"WARN: Không load được api_key từ DB: {exc}")
        return {}


def get_day_range_params(date_str: str):
    from datetime import timezone as _tz
    target = datetime.strptime(date_str, "%Y-%m-%d")
    start_utc = datetime(target.year, target.month, target.day, 0, 0, 0) - timedelta(hours=7)
    end_utc   = datetime(target.year, target.month, target.day, 23, 59, 59) - timedelta(hours=7)
    start_ts = int(start_utc.replace(tzinfo=_tz.utc).timestamp())
    end_ts   = int(end_utc.replace(tzinfo=_tz.utc).timestamp())
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return start_ts, end_ts, start_iso, end_iso


def fetch_order_status_aggs(pancake_shop_id: str, api_key: str, date_str: str) -> dict:
    """Gọi Pancake API lấy aggregate order counts theo ngày (dùng api_key)."""
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(date_str)
    url = f"https://pos.pancake.vn/api/v1/shops/{pancake_shop_id}/orders/get_orders"
    params = [
        ("api_key", api_key),
        ("page_size", 1),
        ("page", 1),
        ("updateStatus", "inserted_at"),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
        ("es_only", "true"),
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{pancake_shop_id}/order",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception as e:
        print(f"    [WARN] API call failed: {e}")
        return {}


def fetch_order_status_totals_all_time(pancake_shop_id: str, api_key: str) -> dict:
    """Gọi Pancake API KHÔNG lọc ngày — lấy tích lũy toàn lịch sử (đang hoàn / đã hoàn)."""
    url = f"https://pos.pancake.vn/api/v1/shops/{pancake_shop_id}/orders/get_orders"
    params = [
        ("api_key", api_key),
        ("page_size", 1),
        ("page", 1),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("es_only", "true"),
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{pancake_shop_id}/order",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception as e:
        print(f"    [WARN] all-time API call failed: {e}")
        return {}


def parse_buckets(data: dict) -> dict:
    """Parse Pancake aggs.status.buckets → order status counts."""
    buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
    m = {}
    for b in buckets:
        m[str(b.get("key", ""))] = int(b.get("doc_count", 0))
    return {
        "new_orders":       m.get("0", 0),
        "confirmed_orders": m.get("9", 0),
        "sent_orders":      m.get("2", 0),
        "received_orders":  m.get("3", 0),
        "returning_orders": m.get("4", 0),
        "returned_orders":  m.get("5", 0),
        "cancelled_orders": m.get("6", 0),
    }


def sync_date(date_str: str, shops_list: list, shop_api_keys: dict,
              shop_key_to_db_id: dict, conn) -> tuple:
    """Sync tất cả shop cho 1 ngày. shop_api_keys = {pos_shop_id: api_key}."""
    cur = conn.cursor()
    ok_count = 0
    fail_count = 0

    for shop in shops_list:
        shop_key = str(shop.get("shop_key", "")).strip()
        pancake_shop_id = str(shop.get("shop_id", "")).strip()

        if not pancake_shop_id or not shop_key:
            continue

        db_id = shop_key_to_db_id.get(shop_key)
        if not db_id:
            print(f"  [SKIP] {shop_key}: không có trong bảng shops")
            continue

        api_key = shop_api_keys.get(pancake_shop_id, "")
        if not api_key:
            print(f"  [SKIP] {shop_key} ({pancake_shop_id}): chưa có api_key hợp lệ")
            continue

        print(f"  [{shop_key}] {pancake_shop_id} — fetching {date_str}...", end=" ", flush=True)
        data = fetch_order_status_aggs(pancake_shop_id, api_key, date_str)
        counts = parse_buckets(data)

        total = sum(counts.values())
        print(f"total={total} (new={counts['new_orders']} confirmed={counts['confirmed_orders']} "
              f"sent={counts['sent_orders']} recv={counts['received_orders']})", end=" ")

        all_time_data = fetch_order_status_totals_all_time(pancake_shop_id, api_key)
        all_time_buckets = all_time_data.get("aggs", {}).get("status", {}).get("buckets", [])
        all_time_map = {str(b.get("key", "")): int(b.get("doc_count", 0)) for b in all_time_buckets}
        total_active_returning = all_time_map.get("4", 0)
        total_returned_all = all_time_map.get("5", 0)
        print(f"[tích_lũy: đang_hoàn={total_active_returning} đã_hoàn={total_returned_all}]")

        try:
            cur.execute("""
                INSERT INTO shop_order_status_cache
                    (shop_id, metric_date, new_orders, confirmed_orders, sent_orders,
                     received_orders, returning_orders, returned_orders, cancelled_orders,
                     total_active_returning, total_returned_all, synced_at)
                VALUES (%s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (shop_id, metric_date) DO UPDATE SET
                    new_orders             = EXCLUDED.new_orders,
                    confirmed_orders       = EXCLUDED.confirmed_orders,
                    sent_orders            = EXCLUDED.sent_orders,
                    received_orders        = EXCLUDED.received_orders,
                    returning_orders       = EXCLUDED.returning_orders,
                    returned_orders        = EXCLUDED.returned_orders,
                    cancelled_orders       = EXCLUDED.cancelled_orders,
                    total_active_returning = EXCLUDED.total_active_returning,
                    total_returned_all     = EXCLUDED.total_returned_all,
                    synced_at              = EXCLUDED.synced_at
            """, (
                db_id, date_str,
                counts["new_orders"], counts["confirmed_orders"], counts["sent_orders"],
                counts["received_orders"], counts["returning_orders"], counts["returned_orders"],
                counts["cancelled_orders"], total_active_returning, total_returned_all,
            ))
            conn.commit()
            ok_count += 1
        except Exception as e:
            conn.rollback()
            fail_count += 1
            print(f"    [DB ERROR] {shop_key}: {e}")

        if DELAY_BETWEEN_SHOPS > 0:
            time.sleep(DELAY_BETWEEN_SHOPS)

    cur.close()
    return ok_count, fail_count


def main():
    parser = argparse.ArgumentParser(description="Sync order status cache from Pancake POS API")
    parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today VN time)")
    parser.add_argument("--days", type=int, default=1, help="Số ngày muốn sync (đếm ngược từ --date)")
    args = parser.parse_args()

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    # Đọc shops từ DB (nguồn đúng) — KHÔNG dùng shops.json để tránh lệch
    # (shops.json có thể thiếu shop → sync sót, cache cũ).
    import psycopg2 as _pg
    try:
        _c = _pg.connect(DATABASE_URL)
        _cur0 = _c.cursor()
        _cur0.execute(
            "SELECT shop_key, pancake_shop_id, status::text FROM shops "
            "WHERE status='active' AND COALESCE(pancake_shop_id,'') <> ''"
        )
        shops_list = [{"shop_key": r[0], "shop_id": str(r[1]), "status": r[2]} for r in _cur0.fetchall()]
        _cur0.close(); _c.close()
    except Exception as exc:
        print(f"ERROR: không đọc được shops từ DB: {exc}")
        sys.exit(1)
    active_shops = [s for s in shops_list if s.get("status") == "active" and s.get("shop_id") and s.get("shop_key")]
    print(f"Loaded {len(active_shops)} active shops from DB")

    shop_api_keys = load_shop_api_keys_from_db()
    valid_count = len(shop_api_keys)
    print(f"Loaded {valid_count} valid api_key(s) from DB")
    if valid_count == 0:
        print("WARN: Không có api_key nào hợp lệ — vào Cài đặt → Shop & Web để nhập api_key cho từng shop")

    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT shop_key, id FROM shops")
    shop_key_to_db_id = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    print(f"Loaded {len(shop_key_to_db_id)} shops from DB")

    base_date = args.date or datetime.now(VN_TZ).strftime("%Y-%m-%d")
    base_dt = datetime.strptime(base_date, "%Y-%m-%d")

    dates_to_sync = []
    for i in range(args.days):
        d = (base_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        dates_to_sync.append(d)

    total_ok = 0
    total_fail = 0

    for date_str in dates_to_sync:
        print(f"\n=== Syncing {date_str} ({len(active_shops)} active shops) ===")
        ok, fail = sync_date(date_str, active_shops, shop_api_keys, shop_key_to_db_id, conn)
        total_ok += ok
        total_fail += fail
        print(f"  → Date {date_str}: {ok} OK, {fail} failed")

    conn.close()
    print(f"\n=== DONE: {total_ok} OK, {total_fail} failed ===")
    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
