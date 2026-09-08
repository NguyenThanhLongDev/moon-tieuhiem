import json
import requests
from datetime import datetime, timedelta

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

ACCESS_TOKEN = config["access_token"]
COOKIE = config["cookie"]


def get_day_range_params(target_date_str):
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")

    start_local = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_local = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)

    start_utc = start_local - timedelta(hours=7)
    end_utc = end_local - timedelta(hours=7)

    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    return start_ts, end_ts, start_iso, end_iso


def main():
    shop_id = "1328943920"   # namleader
    target_date_str = "2026-03-23"   # dùng ngày đã có "Đã gửi hàng"

    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    params = [
        ("access_token", ACCESS_TOKEN),
        ("page_size", 5),
        ("status", 2),  # da gui hang
        ("page", 1),
        ("updateStatus", "inserted_at"),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
    ]

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
        "Cookie": COOKIE,
    }

    res = requests.post(url, params=params, headers=headers, json={}, timeout=30)

    print("STATUS CODE:", res.status_code)
    try:
        data = res.json()
        print(json.dumps(data, ensure_ascii=False, indent=2)[:15000])
    except Exception as e:
        print("JSON ERROR:", e)
        print(res.text[:5000])


if __name__ == "__main__":
    main()
