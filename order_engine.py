import requests
import json
from datetime import datetime, timedelta, timezone

from pancake_auth import get_shop_api_key as _get_shop_key


def get_day_range_timestamps(target_date_str):
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")

    # Giờ VN: 00:00 -> 23:59:59
    # Đổi sang UTC giống Pancake đang gửi
    start_local = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_local = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)

    # Việt Nam UTC+7 => UTC = local - 7h
    start_utc = start_local - timedelta(hours=7)
    end_utc = end_local - timedelta(hours=7)

    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    return start_ts, end_ts, start_iso, end_iso


def get_new_orders_by_day(shop, target_date_str):
    shop_id = shop["shop_id"]
    shop_name = shop.get("shop_name", shop.get("shop_key", ""))

    start_ts, end_ts, start_iso, end_iso = get_day_range_timestamps(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    params = [
        ("api_key", _get_shop_key(str(shop_id))),
        ("page_size", 30),
        ("status", 0),
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
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        data = res.json()

        # API này trả aggregation
        buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])

        if not buckets:
            return 0

        # Với status=0, bucket key thường là "0"
        for bucket in buckets:
            if str(bucket.get("key")) == "0":
                return bucket.get("doc_count", 0)

        # fallback
        return buckets[0].get("doc_count", 0)

    except Exception as e:
        print(f"Loi lay don moi shop {shop_name}: {e}")
        return 0


if __name__ == "__main__":
    target_date = "2026-03-16"   # test đúng ngày bạn đang kiểm tra

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            shops = json.load(f)

    print(f"DON MOI CHUA XU LY NGAY {target_date}:\n")

    for shop in shops:
        if shop.get("status") != "active":
            continue

        count = get_new_orders_by_day(shop, target_date)
        print(f"{shop['shop_name']} ({shop['shop_key']} | {shop['shop_id']}): {count}")
