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


def normalize_text(text):
    return str(text).strip().lower()


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


def find_shop_by_keyword(keyword, shops):
    keyword = normalize_text(keyword)

    exact_matches = []
    partial_matches = []

    for shop in shops:
        shop_name = normalize_text(shop.get("shop_name", ""))
        shop_key = normalize_text(shop.get("shop_key", ""))
        shop_id = normalize_text(shop.get("shop_id", ""))

        if keyword in {shop_name, shop_key, shop_id}:
            exact_matches.append(shop)
        elif keyword in shop_name or keyword in shop_key or keyword in shop_id:
            partial_matches.append(shop)

    if exact_matches:
        return exact_matches[0], []

    if len(partial_matches) == 1:
        return partial_matches[0], []

    return None, partial_matches


def request_sent_orders(shop_id, target_date_str, page=1, page_size=100,
                        access_token=None, cookie=None):
    from pancake_auth import get_shop_api_key as _sk
    api_key = access_token or _sk(str(shop_id))
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    params = [
        ("api_key", api_key),
        ("page_size", page_size),
        ("status", 2),  # da gui hang
        ("page", page),
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
    }

    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception as e:
        return {"error": str(e)}


def fetch_all_sent_orders(shop_id, target_date_str, access_token=None, cookie=None):
    all_orders = []
    page = 1
    page_size = 100

    while True:
        data = request_sent_orders(
            shop_id=shop_id,
            target_date_str=target_date_str,
            page=page,
            page_size=page_size,
        )

        if data.get("error"):
            return None, data["error"]

        orders = data.get("data", [])
        if not isinstance(orders, list):
            return None, "API khong tra ve danh sach don hang"

        all_orders.extend(orders)

        if len(orders) < page_size:
            break

        page += 1
        if page > 100:
            break

    return all_orders, None


def aggregate_sent_items_from_orders(orders):
    item_map = {}
    total_orders = 0
    total_quantity = 0

    for order in orders:
        if not isinstance(order, dict):
            continue

        if int(order.get("status", -1)) != 2:
            continue

        total_orders += 1

        for item in order.get("items", []):
            if not isinstance(item, dict):
                continue

            quantity = item.get("quantity", 0) or 0
            try:
                quantity = float(quantity)
            except Exception:
                quantity = 0

            variation_info = item.get("variation_info", {}) or {}

            product_name = (
                variation_info.get("name")
                or item.get("note_product")
                or item.get("note_product_internal")
                or "Khong ro ten"
            )

            product_code = (
                variation_info.get("custom_id")
                or variation_info.get("product_id")
                or item.get("keyword_variation")
                or ""
            )

            variation_id = item.get("variation_id") or ""
            key = f"{product_name}|{product_code}|{variation_id}"

            if key not in item_map:
                item_map[key] = {
                    "product_name": product_name,
                    "product_code": product_code,
                    "variation_id": variation_id,
                    "quantity": 0,
                    "order_count": 0,
                }

            item_map[key]["quantity"] += quantity
            item_map[key]["order_count"] += 1
            total_quantity += quantity

    items = list(item_map.values())
    items.sort(key=lambda x: (-x["quantity"], -x["order_count"], x["product_name"]))

    return {
        "total_orders": total_orders,
        "total_quantity": total_quantity,
        "items": items,
    }


def build_shop_report(shop, target_date_str, access_token=None, cookie=None):
    shop_name = shop.get("shop_name", shop.get("shop_key", ""))
    shop_key = shop.get("shop_key", "")
    shop_id = shop.get("shop_id", "")

    orders, error = fetch_all_sent_orders(shop_id, target_date_str)
    if error:
        return [
            f"🏪 {shop_name} ({shop_key} | {shop_id})",
            f"Loi API: {error}",
            ""
        ], {
            "shop_name": shop_name,
            "shop_key": shop_key,
            "shop_id": shop_id,
            "total_orders": 0,
            "total_quantity": 0,
            "distinct_items": 0,
        }

    agg = aggregate_sent_items_from_orders(orders)

    lines = [
        f"🏪 {shop_name} ({shop_key} | {shop_id})",
        f"- Don da gui: {fmt_number(agg['total_orders'])}",
        f"- Tong so luong SP da gui: {fmt_number(agg['total_quantity'])}",
        f"- So mat hang distinct: {fmt_number(len(agg['items']))}",
        ""
    ]

    for item in agg["items"][:30]:
        lines.append(
            f"- {item['product_name']} | Ma: {item['product_code']} | "
            f"SL da gui: {fmt_number(item['quantity'])} | "
            f"So don chua SP nay: {fmt_number(item['order_count'])}"
        )

    lines.append("")

    return lines, {
        "shop_name": shop_name,
        "shop_key": shop_key,
        "shop_id": shop_id,
        "total_orders": agg["total_orders"],
        "total_quantity": agg["total_quantity"],
        "distinct_items": len(agg["items"]),
    }


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]).strip()

    if not query_text:
        print("Cach dung:")
        print('python3 report_sent_items.py "23/03/2026"')
        print('python3 report_sent_items.py "namleader 23/03/2026"')
        sys.exit(1)

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can xem")
        sys.exit(1)

    shops = load_shops()

    query_lower = normalize_text(query_text)
    date_lower_1 = normalize_text(target_date)
    try:
        date_dt = datetime.strptime(target_date, "%Y-%m-%d")
        date_lower_2 = normalize_text(date_dt.strftime("%d/%m/%Y"))
    except Exception:
        date_lower_2 = ""

    shop_keyword = query_lower
    if date_lower_1:
        shop_keyword = shop_keyword.replace(date_lower_1, " ")
    if date_lower_2:
        shop_keyword = shop_keyword.replace(date_lower_2, " ")

    for word in ["hom nay", "hom qua", "hom kia", "ngay", "xem", "bao cao", "da gui", "gui hang", "xuat sp", "xuat hang"]:
        shop_keyword = shop_keyword.replace(word, " ")

    shop_keyword = " ".join(shop_keyword.split()).strip()

    target_shops = []

    if shop_keyword:
        matched_shop, partial_matches = find_shop_by_keyword(shop_keyword, shops)

        if matched_shop:
            target_shops = [matched_shop]
        elif partial_matches:
            print(f"Co nhieu shop khop voi tu khoa: {shop_keyword}")
            for shop in partial_matches:
                print(f"- {shop.get('shop_name')} ({shop.get('shop_key')} | {shop.get('shop_id')})")
            sys.exit(0)
        else:
            print(f"Khong tim thay shop nao voi tu khoa: {shop_keyword}")
            sys.exit(1)
    else:
        target_shops = [s for s in shops if s.get("status") == "active"]

    final_lines = [f"📦 BAO CAO SAN PHAM DA GUI - {target_date}", ""]

    total_orders_all = 0
    total_quantity_all = 0
    total_distinct_all = 0

    for shop in target_shops:
        report_lines, summary = build_shop_report(shop, target_date)
        final_lines.extend(report_lines)

        total_orders_all += summary["total_orders"]
        total_quantity_all += summary["total_quantity"]
        total_distinct_all += summary["distinct_items"]

    if len(target_shops) > 1:
        final_lines = [
            f"📦 BAO CAO SAN PHAM DA GUI - {target_date}",
            "",
            "TONG TOAN HE THONG",
            f"- Tong don da gui: {fmt_number(total_orders_all)}",
            f"- Tong so luong SP da gui: {fmt_number(total_quantity_all)}",
            f"- Tong mat hang distinct: {fmt_number(total_distinct_all)}",
            "",
        ] + final_lines[2:]

    print("\n".join(final_lines))
