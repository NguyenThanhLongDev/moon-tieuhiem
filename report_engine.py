import json
import sys
import requests
from datetime import datetime, timedelta
from pathlib import Path
from date_parser import parse_target_date


def fmt_number(value):
    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except Exception:
        return "0"


def detect_metric(text):
    t = text.lower()
    if "loi nhuan" in t or "lợi nhuận" in t or " lai " in f" {t} " or t.startswith("lai "):
        return "profit"
    if "ads" in t or "quang cao" in t or "quảng cáo" in t:
        return "ads_amount"
    return "revenue"


def detect_mode(text):
    t = text.lower()
    if "bao cao" in t or "báo cáo" in t:
        return "full"
    return "simple"


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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


def extract_new_order_count(data):
    if not isinstance(data, dict):
        return 0

    buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
    if not buckets:
        return 0

    for bucket in buckets:
        if str(bucket.get("key")) == "0":
            return bucket.get("doc_count", 0)

    return 0


def get_open_new_orders_current(shop_id, access_token=None, cookie=None):
    from pancake_auth import get_shop_api_key as _sk
    api_key = access_token or _sk(str(shop_id))
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    params = [
        ("api_key", api_key),
        ("page_size", 30),
        ("status", 0),
        ("page", 1),
        ("updateStatus", "inserted_at"),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
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
        return extract_new_order_count(data)
    except Exception:
        return 0


def get_new_orders_by_day(shop_id, access_token=None, cookie=None, target_date_str=None):
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    from pancake_auth import get_shop_api_key as _sk
    api_key = access_token or _sk(str(shop_id))

    params = [
        ("api_key", api_key),
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
        return extract_new_order_count(data)
    except Exception:
        return 0


def get_new_order_display(shop, target_date):
    shop_id = shop.get("shop_id", "")
    from pancake_auth import get_shop_api_key as _sk
    api_key = _sk(str(shop_id))
    if not api_key:
        return 0

    today_str = datetime.today().strftime("%Y-%m-%d")

    if target_date == today_str:
        return get_open_new_orders_current(shop_id, api_key)

    return get_new_orders_by_day(shop_id, target_date_str=target_date)


def load_result_for_day(shop_key, target_date):
    data_file = Path(f"data_{shop_key}.json")
    if not data_file.exists():
        return None

    try:
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    rows = data.get("data", [])
    for row in rows:
        if row.get("Time.day") == target_date:
            return row.get("result", {})
    return None


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]).strip()
    if not query_text:
        query_text = "bao cao hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can xem")
        sys.exit(1)

    metric = detect_metric(query_text)
    mode = detect_mode(query_text)

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            shops = json.load(f)

    lines = []

    for shop in shops:
        if shop.get("status") != "active":
            continue

        shop_key = shop.get("shop_key", "")
        shop_name = shop.get("shop_name", shop_key)
        shop_id = shop.get("shop_id", "")
        shop_display = f"{shop_name} ({shop_key} | {shop_id})"

        result = load_result_for_day(shop_key, target_date) or {}

        sales = result.get("sales", 0)
        revenue = result.get("revenue", 0)
        profit = result.get("profit", 0)
        ads_amount = result.get("ads_amount", 0)
        ads_order = result.get("ads_order", 0)
        avg_profit = result.get("avg_profit", 0)
        success_order_count = result.get("success_order_count", 0)
        returned_order_count = result.get("returned_order_count", 0)
        capital = result.get("capital", 0)
        discount = result.get("discount", 0)
        new_order_count = get_new_order_display(shop, target_date)

        if mode == "full":
            lines.append("============================")
            lines.append(f"Shop: {shop_display}")
            lines.append("============================")
            lines.append("----------------------------")
            lines.append(f"Ngay: {target_date}")
            lines.append(f"Doanh so: {fmt_number(sales)}")
            lines.append(f"Doanh thu: {fmt_number(revenue)}")
            lines.append(f"Loi nhuan: {fmt_number(profit)}")
            lines.append(f"Chi phi quang cao: {fmt_number(ads_amount)}")
            lines.append(f"QC / don: {fmt_number(ads_order)}")
            lines.append(f"LNTB / don: {fmt_number(avg_profit)}")
            lines.append(f"Don moi: {fmt_number(new_order_count)}")
            lines.append(f"Don chot: {fmt_number(success_order_count)}")
            lines.append(f"Don hoan: {fmt_number(returned_order_count)}")
            lines.append(f"Gia von: {fmt_number(capital)}")
            lines.append(f"Chiet khau: {fmt_number(discount)}")
            lines.append("")
        else:
            if metric == "profit":
                value = profit
            elif metric == "ads_amount":
                value = ads_amount
            else:
                value = revenue

            lines.append(f"{shop_display}: {fmt_number(value)}")

    if not lines:
        print(f"Khong tim thay du lieu cho ngay {target_date}")
    else:
        print("\n".join(lines))
