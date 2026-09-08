import json
import os
import sys
from datetime import datetime, timedelta

DAYS = 7
MIN_OLD_QTY = 50
MAX_SOLD_IN_7D = 49
TOP_PER_SHOP = 20


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            return json.load(f)


def normalize_text(s):
    return str(s).strip().lower()


def find_shop(keyword):
    shops = load_shops()
    keyword = normalize_text(keyword)

    exact = []
    partial = []

    for shop in shops:
        shop_name = shop.get("shop_name", "")
        if normalize_text(shop_name) == keyword:
            exact.append(shop)
        elif keyword in normalize_text(shop_name):
            partial.append(shop)

    if exact:
        return exact[0], None
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, partial
    return None, []


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history_file(shop_id, day_str):
    path = f"stock_history/{day_str}/shop_{shop_id}.json"
    if not os.path.exists(path):
        return None
    return load_json(path)


def build_stock_map(stock_data):
    result = {}

    for product in stock_data.get("data", []):
        product_id = product.get("id")
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")
            variation_code = variation.get("custom_id") or product_code or ""

            for wh in variation.get("variations_warehouses", []):
                warehouse_id = wh.get("warehouse_id")
                qty = wh.get("available_quantity")

                if qty is None:
                    continue

                key = f"{product_id}|{variation_id}|{warehouse_id}"
                result[key] = {
                    "product_name": product_name,
                    "product_code": variation_code,
                    "variation_id": variation_id,
                    "warehouse_id": warehouse_id,
                    "qty": qty,
                }

    return result


def analyze_shop(shop):
    shop_id = str(shop.get("shop_id"))
    shop_name = shop.get("shop_name", shop_id)

    today_str = datetime.now().strftime("%Y-%m-%d")
    old_str = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    today_data = load_history_file(shop_id, today_str)
    old_data = load_history_file(shop_id, old_str)

    if not today_data:
        return f"Chua co du lieu hom nay cho shop: {shop_name}"

    if not old_data:
        return f"Chua co du lieu ngay {old_str} cho shop: {shop_name}"

    today_map = build_stock_map(today_data)
    old_map = build_stock_map(old_data)

    items = []

    for key, old_item in old_map.items():
        today_item = today_map.get(key)
        if not today_item:
            continue

        old_qty = old_item["qty"]
        today_qty = today_item["qty"]
        sold_7d = old_qty - today_qty

        if old_qty < MIN_OLD_QTY:
            continue

        if sold_7d < 0:
            continue

        if sold_7d <= MAX_SOLD_IN_7D:
            items.append({
                "product_name": today_item["product_name"],
                "product_code": today_item["product_code"],
                "warehouse_id": today_item["warehouse_id"],
                "old_qty": old_qty,
                "today_qty": today_qty,
                "sold_7d": sold_7d,
            })

    items.sort(key=lambda x: (x["sold_7d"], -x["today_qty"], x["product_name"]))

    lines = [
        f"=== BAN CHAM SHOP: {shop_name} ===",
        f"Shop ID: {shop_id}",
        f"So sanh 7 ngay: {old_str} -> {today_str}",
        ""
    ]

    for item in items[:TOP_PER_SHOP]:
        lines.append(
            f"- {item['product_name']} | "
            f"Ma: {item['product_code']} | "
            f"Xuat 7 ngay: {item['sold_7d']} | "
            f"Ton hien tai: {item['today_qty']} | "
            f"Ton 7 ngay truoc: {item['old_qty']}"
        )

    lines.append("")
    lines.append(f"Tong san pham ban cham: {len(items)}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print('Cach dung: python3 slow_sales_by_shop.py "ten_shop"')
        return

    shop_keyword = " ".join(sys.argv[1:])
    shop, matches = find_shop(shop_keyword)

    if not shop and matches == []:
        print(f"Khong tim thay shop nao: {shop_keyword}")
        return

    if not shop and matches:
        print(f"Co nhieu shop khop voi: {shop_keyword}")
        for s in matches:
            print(f"- {s.get('shop_name')} | ID: {s.get('shop_id')}")
        return

    print(analyze_shop(shop))


if __name__ == "__main__":
    main()
