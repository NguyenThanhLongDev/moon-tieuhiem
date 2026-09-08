import json
import os
import sys
from datetime import datetime, timedelta


MIN_QTY = 1


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


def compare_stagnant(today_data, old_data, days, min_qty=MIN_QTY):
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
                "product_code": today_item["product_code"],
                "warehouse_id": today_item["warehouse_id"],
                "qty": today_qty,
                "days": days,
            })

    return alerts


def main():
    if len(sys.argv) < 2:
        print('Cách dùng: python3 stagnant_query.py "ten_shop" [so_ngay] [export]')
        return

    keyword_shop = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) >= 3 and sys.argv[2].isdigit() else 7
    export_flag = len(sys.argv) >= 4 and sys.argv[3].lower() == "export"

    shop, matches = find_shop(keyword_shop)

    if not shop and matches == []:
        print(f"Không tìm thấy shop nào khớp với: {keyword_shop}")
        return

    if not shop and matches:
        print(f"Có nhiều shop khớp với: {keyword_shop}")
        for s in matches:
            print(f"- {s.get('shop_name')} | shop_id: {s.get('shop_id')}")
        return

    shop_name = shop.get("shop_name")
    shop_id = str(shop.get("shop_id"))

    today = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    today_file = f"stock_history/{today}/shop_{shop_id}.json"
    old_file = f"stock_history/{old_day}/shop_{shop_id}.json"

    if not os.path.exists(today_file):
        print(f"Chưa có dữ liệu hôm nay cho shop: {shop_name}")
        return

    if not os.path.exists(old_file):
        print(f"Chưa có dữ liệu ngày {old_day} cho shop: {shop_name}")
        return

    today_data = load_json(today_file)
    old_data = load_json(old_file)
    alerts = compare_stagnant(today_data, old_data, days)

    header = f"=== SHOP: {shop_name} | ID: {shop_id} | ĐỨNG YÊN: {days} NGÀY ==="
    print(header)

    lines = []
    for a in alerts:
        line = (
            f"Tên: {a['product_name']} | "
            f"Mã: {a['product_code']} | "
            f"Tồn: {a['qty']} | "
            f"Kho: {a['warehouse_id']} | "
            f"Đứng yên: {a['days']} ngày"
        )
        lines.append(line)
        print(line)

    print(f"\nTổng sản phẩm đứng yên: {len(alerts)}")

    if export_flag:
        safe_shop_name = shop_name.replace(" ", "_")
        output_file = f"stagnant_{safe_shop_name}_{days}d.txt"

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(header + "\n\n")
            for line in lines:
                f.write(line + "\n")
            f.write(f"\nTổng sản phẩm đứng yên: {len(alerts)}\n")

        print(f"\nĐã xuất file: {output_file}")


if __name__ == "__main__":
    main()
