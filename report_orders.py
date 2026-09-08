import json
import sys
import requests
from datetime import datetime, timedelta
from date_parser import parse_target_date


def fmt_number(value):
    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except Exception:
        return "0"


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            return json.load(f)


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


def request_orders_aggs(shop_id, target_date_str, access_token=None, cookie=None):
    from pancake_auth import get_shop_api_key as _sk
    api_key = access_token or _sk(str(shop_id))
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

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
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception:
        return {}


def get_shop_order_stats(shop, target_date_str, access_token=None, cookie=None):
    data = request_orders_aggs(shop["shop_id"], target_date_str)
    buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
    bucket_map = {}

    for bucket in buckets:
        bucket_map[str(bucket.get("key"))] = int(bucket.get("doc_count", 0))

    new_count = bucket_map.get("0", 0)
    confirmed_count = bucket_map.get("1", 0)
    sent_count = bucket_map.get("2", 0)
    received_count = bucket_map.get("3", 0)
    returning_count = bucket_map.get("4", 0)
    returned_count = bucket_map.get("5", 0)

    total_all = (
        new_count
        + confirmed_count
        + sent_count
        + received_count
        + returning_count
        + returned_count
    )

    return {
        "all": total_all,
        "new": new_count,
        "confirmed": confirmed_count,
        "waiting_shipment": 0,
        "sent": sent_count,
        "received": received_count,
        "returning": returning_count,
        "returned": returned_count,
    }


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]).strip()
    if not query_text:
        query_text = "hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can xem don hang")
        sys.exit(1)

    shops = load_shops()

    totals = {
        "all": 0,
        "new": 0,
        "confirmed": 0,
        "waiting_shipment": 0,
        "sent": 0,
        "received": 0,
        "returning": 0,
        "returned": 0,
    }

    shop_lines = []

    for shop in shops:
        if shop.get("status") != "active":
            continue

        shop_name = shop.get("shop_name", shop.get("shop_key", ""))
        shop_key = shop.get("shop_key", "")
        shop_id = shop.get("shop_id", "")
        shop_display = f"{shop_name} ({shop_key} | {shop_id})"

        stats = get_shop_order_stats(shop, target_date)

        for k in totals:
            totals[k] += stats[k]

        shop_lines.append(shop_display)
        shop_lines.append(f"- Tat ca: {fmt_number(stats['all'])}")
        shop_lines.append(f"- Moi: {fmt_number(stats['new'])}")
        shop_lines.append(f"- Da xac nhan: {fmt_number(stats['confirmed'])}")
        shop_lines.append(f"- Cho chuyen hang: {fmt_number(stats['waiting_shipment'])}")
        shop_lines.append(f"- Da gui hang: {fmt_number(stats['sent'])}")
        shop_lines.append(f"- Da nhan: {fmt_number(stats['received'])}")
        shop_lines.append(f"- Dang hoan: {fmt_number(stats['returning'])}")
        shop_lines.append(f"- Da hoan: {fmt_number(stats['returned'])}")
        shop_lines.append("")

    final_lines = [
        f"📦 BAO CAO DON HANG - {target_date}",
        "",
        "TONG TOAN HE THONG",
        f"- Tat ca: {fmt_number(totals['all'])}",
        f"- Moi: {fmt_number(totals['new'])}",
        f"- Da xac nhan: {fmt_number(totals['confirmed'])}",
        f"- Cho chuyen hang: {fmt_number(totals['waiting_shipment'])}",
        f"- Da gui hang: {fmt_number(totals['sent'])}",
        f"- Da nhan: {fmt_number(totals['received'])}",
        f"- Dang hoan: {fmt_number(totals['returning'])}",
        f"- Da hoan: {fmt_number(totals['returned'])}",
        "",
    ]
    final_lines.extend(shop_lines)
    final_lines.append("Ghi chu: 'Cho chuyen hang' dang tam de 0 cho toi khi bat duoc ma trang thai rieng tu Pancake.")

    print("\n".join(final_lines))
