import json
import sys
import glob
import os


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
        shop_id = str(shop.get("shop_id", ""))

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


def load_stock_file(shop_id):
    path = f"stock_{shop_id}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) < 2:
        print('Cách dùng: python3 view_stock_by_name.py "ten_shop"')
        return

    keyword = " ".join(sys.argv[1:])
    shop, matches = find_shop(keyword)

    if not shop and matches == []:
        print(f"Không tìm thấy shop nào khớp với: {keyword}")
        return

    if not shop and matches:
        print(f"Có nhiều shop khớp với: {keyword}")
        for s in matches:
            print(f"- {s.get('shop_name')} | shop_id: {s.get('shop_id')}")
        return

    shop_name = shop.get("shop_name")
    shop_id = str(shop.get("shop_id"))

    stock_path = f"stock_{shop_id}.json"
    if not os.path.exists(stock_path):
        print(f"Chưa có file tồn kho cho shop: {shop_name}")
        print("Hãy chạy: python3 sync_stock_pos.py")
        return

    data = load_stock_file(shop_id)

    print(f"=== SHOP: {shop_name} | ID: {shop_id} ===")

    total_rows = 0
    for product in data.get("data", []):
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")
            variation_code = variation.get("custom_id") or product_code or ""

            for wh in variation.get("variations_warehouses", []):
                available_qty = wh.get("available_quantity", 0)
                remain_qty = wh.get("remain_quantity", 0)
                actual_remain_qty = wh.get("actual_remain_quantity", 0)
                warehouse_id = wh.get("warehouse_id", "")

                print(
                    f"Tên: {product_name} | "
                    f"Mã: {variation_code} | "
                    f"Kho: {warehouse_id} | "
                    f"Available: {available_qty} | "
                    f"Remain: {remain_qty} | "
                    f"Actual: {actual_remain_qty} | "
                    f"Variation: {variation_id}"
                )
                total_rows += 1

    print(f"\nTổng dòng tồn kho: {total_rows}")


if __name__ == "__main__":
    main()
