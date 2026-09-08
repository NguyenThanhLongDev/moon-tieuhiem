import json
import glob
import os
from datetime import datetime, timedelta

DAYS = 7
MIN_QTY = 1


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            return json.load(f)


def build_shop_map():
    shops = load_shops()
    shop_map = {}
    for shop in shops:
        shop_map[str(shop.get("shop_id"))] = shop.get("shop_name", str(shop.get("shop_id")))
    return shop_map


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_stock_map(stock_data):
    result = {}

    for product in stock_data.get("data", []):
        product_name = product.get("name", "Không tên")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")
            custom_id = variation.get("custom_id") or product.get("custom_id") or ""
            key_base = f"{product.get('id')}|{variation_id}|{custom_id}|{product_name}"

            for wh in variation.get("variations_warehouses", []):
                warehouse_id = wh.get("warehouse_id")
                qty = wh.get("available_quantity")

                if qty is None:
                    continue

                key = f"{key_base}|{warehouse_id}"
                result[key] = {
                    "product_name": product_name,
                    "custom_id": custom_id,
                    "variation_id": variation_id,
                    "warehouse_id": warehouse_id,
                    "qty": qty,
                }

    return result


def compare_stagnant(today_data, old_data, min_qty=MIN_QTY):
    today_map = build_stock_map(today_data)
    old_map = build_stock_map(old_data)

    alerts = []

    for key, today_item in today_map.items():
        old_item = old_map.get(key)
        if not old_item:
            continue

        today_qty = today_item["qty"]
        old_qty = old_item["qty"]

        if today_qty >= min_qty and today_qty == old_qty:
            alerts.append({
                "product_name": today_item["product_name"],
                "custom_id": today_item["custom_id"],
                "warehouse_id": today_item["warehouse_id"],
                "qty": today_qty,
                "days": DAYS,
            })

    return alerts


def main():
    shop_map = build_shop_map()
    today = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    today_files = glob.glob(f"stock_history/{today}/shop_*.json")

    for today_file in today_files:
        shop_id = os.path.basename(today_file).replace("shop_", "").replace(".json", "")
        shop_name = shop_map.get(shop_id, f"shop_id {shop_id}")
        old_file = f"stock_history/{old_day}/shop_{shop_id}.json"

        if not os.path.exists(old_file):
            print(f"SKIP {shop_name}: chưa có dữ liệu ngày {old_day}")
            continue

        today_data = load_json(today_file)
        old_data = load_json(old_file)
        alerts = compare_stagnant(today_data, old_data)

        print(f"\n=== SHOP: {shop_name} | ID: {shop_id} ===")
        print(f"Tồn đứng yên {DAYS} ngày: {len(alerts)}")

        for a in alerts[:20]:
            print(f"📦 {a['product_name']} | tồn: {a['qty']} | đứng yên {a['days']} ngày | kho: {a['warehouse_id']}")


if __name__ == "__main__":
    main()
