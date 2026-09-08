import json
import os
import sys


LOW_STOCK_THRESHOLD = 5


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


def load_stock_file(shop_id):
    path = f"stock_{shop_id}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def match_mode(qty, mode):
    if mode == "all":
        return True
    if mode == "low":
        return qty <= LOW_STOCK_THRESHOLD
    if mode == "out":
        return qty == 0
    if mode == "positive":
        return qty > 0
    return True


def match_keyword(product_name, product_code, keyword):
    if not keyword:
        return True

    text = f"{product_name} {product_code}".lower()
    return keyword.lower() in text


def main():
    if len(sys.argv) < 2:
        print('Cách dùng: python3 stock_query.py "ten_shop" [all|low|out|positive] [tu_khoa] [export]')
        return

    keyword_shop = sys.argv[1]
    mode = sys.argv[2].lower() if len(sys.argv) >= 3 else "all"
    product_keyword = sys.argv[3].strip().lower() if len(sys.argv) >= 4 else ""
    export_flag = len(sys.argv) >= 5 and sys.argv[4].lower() == "export"

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

    stock_path = f"stock_{shop_id}.json"
    if not os.path.exists(stock_path):
        print(f"Chưa có file tồn kho cho shop: {shop_name}")
        print("Hãy chạy: python3 sync_stock_pos.py")
        return

    data = load_stock_file(shop_id)

    header = f"=== SHOP: {shop_name} | ID: {shop_id} | MODE: {mode} | KEYWORD: {product_keyword or 'none'} ==="
    print(header)

    lines = []
    total_rows = 0
    matched_rows = 0

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

                total_rows += 1

                if not match_mode(available_qty, mode):
                    continue

                if not match_keyword(product_name, variation_code, product_keyword):
                    continue

                matched_rows += 1

                line = (
                    f"Tên: {product_name} | "
                    f"Mã: {variation_code} | "
                    f"Kho: {warehouse_id} | "
                    f"Available: {available_qty} | "
                    f"Remain: {remain_qty} | "
                    f"Actual: {actual_remain_qty} | "
                    f"Variation: {variation_id}"
                )

                lines.append(line)
                print(line)

    summary_1 = f"\nTổng dòng tồn kho: {total_rows}"
    summary_2 = f"Số dòng khớp điều kiện: {matched_rows}"
    print(summary_1)
    print(summary_2)

    if export_flag:
        safe_shop_name = shop_name.replace(" ", "_")
        output_file = f"stock_query_{safe_shop_name}_{mode}.txt"

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(header + "\n\n")
            for line in lines:
                f.write(line + "\n")
            f.write(summary_1 + "\n")
            f.write(summary_2 + "\n")

        print(f"\nĐã xuất file: {output_file}")


if __name__ == "__main__":
    main()
