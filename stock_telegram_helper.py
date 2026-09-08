import json
import os
from datetime import datetime, timedelta

LOW_STOCK_THRESHOLD = 5
TELEGRAM_LIMIT = 3500
STAGNANT_DAYS = 7
MIN_STAGNANT_QTY = 1


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
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history_file(shop_id, day_str):
    path = f"stock_history/{day_str}/shop_{shop_id}.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def split_message(text, limit=TELEGRAM_LIMIT):
    parts = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit:
            if current.strip():
                parts.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"

    if current.strip():
        parts.append(current.strip())

    return parts


def build_shop_stock_message(shop_name, mode="all"):
    shop, matches = find_shop(shop_name)

    if not shop and matches == []:
        return [f"Không tìm thấy shop nào: {shop_name}"]

    if not shop and matches:
        lines = [f"Có nhiều shop khớp với: {shop_name}"]
        for s in matches:
            lines.append(f"- {s.get('shop_name')} | ID: {s.get('shop_id')}")
        return ["\n".join(lines)]

    real_shop_name = shop.get("shop_name")
    shop_id = str(shop.get("shop_id"))

    data = load_stock_file(shop_id)
    if not data:
        return [f"Chưa có dữ liệu tồn kho cho shop: {real_shop_name}"]

    lines = [f"📦 TỒN KHO SHOP: {real_shop_name}", f"🆔 Shop ID: {shop_id}", ""]

    total_rows = 0
    matched_rows = 0

    for product in data.get("data", []):
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")

        for variation in product.get("variations", []):
            variation_code = variation.get("custom_id") or product_code or ""

            for wh in variation.get("variations_warehouses", []):
                qty = wh.get("available_quantity", 0)
                warehouse_id = wh.get("warehouse_id", "")

                total_rows += 1

                show = False
                if mode == "all":
                    show = True
                elif mode == "hethang" and qty == 0:
                    show = True
                elif mode == "saphet" and 0 < qty <= LOW_STOCK_THRESHOLD:
                    show = True
                elif mode == "conhang" and qty > LOW_STOCK_THRESHOLD:
                    show = True

                if not show:
                    continue

                matched_rows += 1

                icon = "📦"
                if qty == 0:
                    icon = "❌"
                elif qty <= LOW_STOCK_THRESHOLD:
                    icon = "⚠️"

                lines.append(
                    f"{icon} {product_name} | "
                    f"Mã: {variation_code} | "
                    f"Kho: {warehouse_id} | "
                    f"Tồn: {qty}"
                )

    lines.append("")
    lines.append(f"Tổng dòng tồn kho: {total_rows}")
    lines.append(f"Số dòng khớp điều kiện: {matched_rows}")

    return split_message("\n".join(lines))


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


def build_stagnant_message(shop_name, days=STAGNANT_DAYS):
    shop, matches = find_shop(shop_name)

    if not shop and matches == []:
        return [f"Không tìm thấy shop nào: {shop_name}"]

    if not shop and matches:
        lines = [f"Có nhiều shop khớp với: {shop_name}"]
        for s in matches:
            lines.append(f"- {s.get('shop_name')} | ID: {s.get('shop_id')}")
        return ["\n".join(lines)]

    real_shop_name = shop.get("shop_name")
    shop_id = str(shop.get("shop_id"))

    today = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    today_data = load_history_file(shop_id, today)
    old_data = load_history_file(shop_id, old_day)

    if not today_data:
        return [f"Chưa có dữ liệu hôm nay cho shop: {real_shop_name}"]

    if not old_data:
        return [f"Chưa có dữ liệu ngày {old_day} cho shop: {real_shop_name}"]

    today_map = build_stock_map(today_data)
    old_map = build_stock_map(old_data)

    lines = [
        f"🐢 HÀNG BÁN CHẬM: {real_shop_name}",
        f"🆔 Shop ID: {shop_id}",
        f"📅 So với {days} ngày trước ({old_day})",
        ""
    ]

    matched = 0

    for key, today_item in today_map.items():
        old_item = old_map.get(key)
        if not old_item:
            continue

        today_qty = today_item["qty"]
        old_qty = old_item["qty"]

        if today_qty >= MIN_STAGNANT_QTY and today_qty == old_qty:
            matched += 1
            lines.append(
                f"🐢 {today_item['product_name']} | "
                f"Mã: {today_item['product_code']} | "
                f"Tồn: {today_qty} | "
                f"Kho: {today_item['warehouse_id']}"
            )

    lines.append("")
    lines.append(f"Tổng sản phẩm bán chậm: {matched}")

    return split_message("\n".join(lines))


def build_all_shops_summary():
    shops = load_shops()
    lines = ["📊 TÓM TẮT TỒN KHO TẤT CẢ SHOP", ""]

    total_shops = 0

    for shop in shops:
        shop_id = str(shop.get("shop_id"))
        shop_name = shop.get("shop_name", shop_id)

        data = load_stock_file(shop_id)
        if not data:
            lines.append(f"🏪 {shop_name}: chưa có dữ liệu")
            continue

        total_shops += 1
        total_rows = 0
        positive_rows = 0
        low_rows = 0
        out_rows = 0

        for product in data.get("data", []):
            for variation in product.get("variations", []):
                for wh in variation.get("variations_warehouses", []):
                    qty = wh.get("available_quantity", 0)
                    total_rows += 1

                    if qty > 0:
                        positive_rows += 1
                    if qty == 0:
                        out_rows += 1
                    elif qty <= LOW_STOCK_THRESHOLD:
                        low_rows += 1

        lines.append(
            f"🏪 {shop_name} | Tổng: {total_rows} | Còn hàng: {positive_rows} | "
            f"Sắp hết: {low_rows} | Hết hàng: {out_rows}"
        )

    lines.append("")
    lines.append(f"Tổng shop có dữ liệu: {total_shops}")

    return split_message("\n".join(lines))


def handle_stock_command(text):
    raw = str(text).strip()
    if not raw:
        return None

    raw_lower = raw.lower()

    if raw_lower == "tonkhotatca":
        return build_all_shops_summary()

    if not raw_lower.startswith("tonkho"):
        return None

    parts = raw.split()
    lower_parts = raw_lower.split()

    if len(parts) == 2:
        shop_name = raw[len("tonkho "):].strip()
        return build_shop_stock_message(shop_name, "all")

    if len(parts) >= 3:
        mode = lower_parts[1]
        shop_name = raw.split(maxsplit=2)[2].strip()

        if mode in ["hethang", "saphet", "conhang"]:
            return build_shop_stock_message(shop_name, mode)

        if mode == "bancham":
            return build_stagnant_message(shop_name, STAGNANT_DAYS)

    return [
        "Sai cú pháp.\n\n"
        "Dùng:\n"
        "tonkho ten_shop\n"
        "tonkho hethang ten_shop\n"
        "tonkho saphet ten_shop\n"
        "tonkho conhang ten_shop\n"
        "tonkho bancham ten_shop\n"
        "tonkhotatca"
    ]
