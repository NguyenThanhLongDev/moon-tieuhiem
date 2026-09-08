import json
import glob
import os

LOW_STOCK_THRESHOLD = 5


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        pass
    with open("shops.json", "r", encoding="utf-8") as f:
        return json.load(f)


def build_shop_map():
    shops = load_shops()
    shop_map = {}
    for shop in shops:
        shop_map[str(shop.get("shop_id"))] = shop.get("shop_name", str(shop.get("shop_id")))
    return shop_map


def load_stock_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_shop_id_from_path(path):
    name = os.path.basename(path).replace(".json", "")
    return name.replace("stock_", "")


def extract_alerts(stock_data, threshold=LOW_STOCK_THRESHOLD):
    alerts = []

    for product in stock_data.get("data", []):
        product_name = product.get("name", "Không tên")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")
            custom_id = variation.get("custom_id") or product.get("custom_id") or ""

            for wh in variation.get("variations_warehouses", []):
                qty = wh.get("available_quantity")
                warehouse_id = wh.get("warehouse_id")

                if qty is None:
                    continue

                if qty == 0:
                    alerts.append({
                        "type": "out_of_stock",
                        "product_name": product_name,
                        "custom_id": custom_id,
                        "variation_id": variation_id,
                        "warehouse_id": warehouse_id,
                        "qty": qty,
                    })
                elif qty <= threshold:
                    alerts.append({
                        "type": "low_stock",
                        "product_name": product_name,
                        "custom_id": custom_id,
                        "variation_id": variation_id,
                        "warehouse_id": warehouse_id,
                        "qty": qty,
                    })

    return alerts


def main():
    shop_map = build_shop_map()
    files = glob.glob("stock_*.json")

    for path in files:
        shop_id = get_shop_id_from_path(path)
        shop_name = shop_map.get(shop_id, f"shop_id {shop_id}")

        data = load_stock_file(path)
        alerts = extract_alerts(data)

        print(f"\n=== SHOP: {shop_name} | ID: {shop_id} ===")
        print(f"Tổng cảnh báo: {len(alerts)}")

        for a in alerts[:20]:
            icon = "❌" if a["type"] == "out_of_stock" else "⚠️"
            print(f"{icon} {a['product_name']} | tồn: {a['qty']} | kho: {a['warehouse_id']}")


if __name__ == "__main__":
    main()
