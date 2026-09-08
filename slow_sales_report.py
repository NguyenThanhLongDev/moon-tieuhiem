import json
import os
from datetime import datetime, timedelta

DAYS = 7
MIN_OLD_QTY = 50
MAX_SOLD_IN_7D = 49
TOP_PER_SHOP = 10
TOP_TOTAL = 50


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


def analyze_shop(shop, today_str, old_str):
    shop_id = str(shop.get("shop_id"))
    shop_name = shop.get("shop_name", shop_id)

    today_data = load_history_file(shop_id, today_str)
    old_data = load_history_file(shop_id, old_str)

    if not today_data or not old_data:
        return {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "missing": True,
            "items": []
        }

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

    return {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "missing": False,
        "items": items
    }


def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    old_str = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    shops = load_shops()
    total_problem_items = 0
    shown_total = 0

    print(f"=== BÁN CHẬM TOÀN HỆ THỐNG | {DAYS} NGÀY | XUẤT < 50 ===")
    print(f"So sánh: {old_str} -> {today_str}")

    for shop in shops:
        result = analyze_shop(shop, today_str, old_str)

        if result["missing"]:
            continue

        if not result["items"]:
            continue

        shop_items = result["items"][:TOP_PER_SHOP]
        if shown_total >= TOP_TOTAL:
            break

        print(f"\n🏪 {result['shop_name']} | shop_id: {result['shop_id']} | Số SP chậm: {len(result['items'])}")

        for item in shop_items:
            if shown_total >= TOP_TOTAL:
                break

            print(
                f"- {item['product_name']} | "
                f"Mã: {item['product_code']} | "
                f"Xuất 7 ngày: {item['sold_7d']} | "
                f"Tồn hiện tại: {item['today_qty']} | "
                f"Tồn 7 ngày trước: {item['old_qty']}"
            )
            shown_total += 1

        total_problem_items += len(result["items"])

    print(f"\nTỔNG SẢN PHẨM BÁN CHẬM TOÀN HỆ THỐNG: {total_problem_items}")
    print(f"SỐ DÒNG ĐANG HIỂN THỊ: {shown_total}")


if __name__ == "__main__":
    main()
