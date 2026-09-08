import json
import sys


def load_stock_file(shop_id):
    path = f"stock_{shop_id}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) < 2:
        print("Cách dùng: python3 view_stock_shop.py <shop_id>")
        return

    shop_id = sys.argv[1]
    data = load_stock_file(shop_id)

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
