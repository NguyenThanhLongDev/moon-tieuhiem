import json
import os
from datetime import datetime, timedelta

DAYS = 7
MIN_OLD_QTY = 50
MAX_SOLD_IN_7D = 49


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            return json.load(f)


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
        product_name = product.get("name", "")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")

            for wh in variation.get("variations_warehouses", []):
                warehouse_id = wh.get("warehouse_id")
                qty = wh.get("available_quantity")

                if qty is None:
                    continue

                key = f"{product_id}|{variation_id}|{warehouse_id}"
                result[key] = qty

    return result


def analyze_shop(shop):
    shop_id = str(shop.get("shop_id"))
    shop_name = shop.get("shop_name", shop_id)

    today = datetime.now().strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    today_data = load_history_file(shop_id, today)
    old_data = load_history_file(shop_id, old)

    if not today_data or not old_data:
        return None

    today_map = build_stock_map(today_data)
    old_map = build_stock_map(old_data)

    total_stuck = 0
    count_items = 0

    for key, old_qty in old_map.items():
        today_qty = today_map.get(key)
        if today_qty is None:
            continue

        sold = old_qty - today_qty

        if old_qty < MIN_OLD_QTY:
            continue

        if sold < 0:
            continue

        if sold <= MAX_SOLD_IN_7D:
            count_items += 1
            total_stuck += today_qty

    return {
        "shop_name": shop_name,
        "shop_id": shop_id,
        "count": count_items,
        "stuck": total_stuck
    }


def main():
    shops = load_shops()
    results = []

    for shop in shops:
        r = analyze_shop(shop)
        if r and r["count"] > 0:
            results.append(r)

    results.sort(key=lambda x: (-x["count"], -x["stuck"]))

    print("=== TOP SHOP BAN CHAM ===\n")

    for i, r in enumerate(results[:10], start=1):
        print(
            f"{i}. {r['shop_name']} | "
            f"SP cham: {r['count']} | "
            f"Ton ket: {r['stuck']}"
        )


if __name__ == "__main__":
    main()
