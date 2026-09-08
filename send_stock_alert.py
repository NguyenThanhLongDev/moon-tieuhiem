import json
import glob
import os
import requests
import argparse
from datetime import datetime

from telegram_notify import broadcast_send_text

LOW_STOCK_THRESHOLD = 5
CACHE_FILE = "stock_alert_cache.json"
TELEGRAM_LIMIT = 3800


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
        shop_id = str(shop.get("shop_id"))
        shop_name = shop.get("shop_name", shop_id)
        shop_map[shop_id] = shop_name
    return shop_map


def load_stock_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_shop_id_from_path(path):
    name = os.path.basename(path).replace(".json", "")
    return name.replace("stock_", "")


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def send_telegram(message):
    ok, fail = broadcast_send_text(message, max_part_length=TELEGRAM_LIMIT)
    if ok == 0 and fail > 0:
        raise requests.RequestException("Khong gui duoc Telegram toi bat ky chat nao")


def split_message(text, limit=TELEGRAM_LIMIT):
    parts = []
    current = ""

    for line in str(text).splitlines():
        if len(current) + len(line) + 1 > limit:
            if current.strip():
                parts.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"

    if current.strip():
        parts.append(current.strip())

    return parts


def extract_current_alerts(stock_data, shop_id, threshold=LOW_STOCK_THRESHOLD):
    current_alerts = {}

    for product in stock_data.get("data", []):
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")

        for variation in product.get("variations", []):
            variation_id = variation.get("id")
            variation_code = variation.get("custom_id") or product_code or ""

            for wh in variation.get("variations_warehouses", []):
                qty = wh.get("available_quantity")
                warehouse_id = wh.get("warehouse_id", "")

                if qty is None:
                    continue

                alert_type = None
                if qty == 0:
                    alert_type = "out_of_stock"
                elif qty <= threshold:
                    alert_type = "low_stock"

                if not alert_type:
                    continue

                alert_key = f"{shop_id}|{variation_id}|{warehouse_id}|{alert_type}"

                current_alerts[alert_key] = {
                    "shop_id": str(shop_id),
                    "product_name": product_name,
                    "product_code": variation_code,
                    "variation_id": variation_id,
                    "warehouse_id": warehouse_id,
                    "qty": qty,
                    "type": alert_type,
                }

    return current_alerts


def build_daily_grouped_messages(all_current_alerts, shop_map, threshold):
    by_shop = {}
    out_count = 0
    low_count = 0

    for _, alert in all_current_alerts.items():
        shop_id = str(alert.get("shop_id", ""))
        shop_name = shop_map.get(shop_id, shop_id)
        by_shop.setdefault(shop_id, {
            "shop_name": shop_name,
            "items": [],
        })
        by_shop[shop_id]["items"].append(alert)
        if alert.get("type") == "out_of_stock":
            out_count += 1
        else:
            low_count += 1

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "📦 CẢNH BÁO TỒN KHO HÀNG NGÀY",
        f"⏰ Thời điểm: {now_str}",
        f"⚙️ Ngưỡng cảnh báo: < {threshold}",
        f"🏪 Số shop có cảnh báo: {len(by_shop)}",
        f"❌ Hết hàng: {out_count} | ⚠️ Sắp hết: {low_count}",
        "",
    ]

    for shop_id, data in sorted(by_shop.items(), key=lambda x: x[1]["shop_name"]):
        items = data["items"]
        items.sort(key=lambda x: (x.get("type") != "out_of_stock", x.get("qty", 0), x.get("product_name", "")))
        lines.append(f"🏪 {data['shop_name']} | ID: {shop_id} | Cảnh báo: {len(items)}")
        for item in items[:20]:
            icon = "❌" if item.get("type") == "out_of_stock" else "⚠️"
            status = "hết hàng" if item.get("type") == "out_of_stock" else f"còn {item.get('qty', 0)}"
            lines.append(f"  {icon} {item.get('product_name', 'Không tên')} | Mã: {item.get('product_code', '')} | {status}")
        if len(items) > 20:
            lines.append(f"  ... và {len(items) - 20} mặt hàng khác")
        lines.append("")

    if not by_shop:
        lines = [
            "📦 CẢNH BÁO TỒN KHO HÀNG NGÀY",
            f"⏰ Thời điểm: {now_str}",
            f"⚙️ Ngưỡng cảnh báo: < {threshold}",
            "✅ Không có mặt hàng nào dưới ngưỡng cảnh báo.",
        ]

    text = "\n".join(lines)
    return split_message(text)


def build_new_alert_messages(all_current_alerts, old_cache, shop_map):
    messages = []

    for alert_key, alert in all_current_alerts.items():
        old_alert = old_cache.get(alert_key)

        # chưa từng gửi -> gửi mới
        if not old_alert:
            shop_name = shop_map.get(alert["shop_id"], alert["shop_id"])

            icon = "❌" if alert["type"] == "out_of_stock" else "⚠️"
            status = "hết hàng" if alert["type"] == "out_of_stock" else f"còn {alert['qty']}"

            msg = (
                f"📦 CẢNH BÁO KHO\n"
                f"🏪 Shop: {shop_name}\n"
                f"🆔 Shop ID: {alert['shop_id']}\n\n"
                f"{icon} {alert['product_name']} | Mã: {alert['product_code']} | {status}"
            )
            messages.append(msg)
            continue

        # đã gửi rồi nhưng qty thay đổi -> gửi cập nhật
        old_qty = old_alert.get("qty")
        new_qty = alert.get("qty")

        if old_qty != new_qty:
            shop_name = shop_map.get(alert["shop_id"], alert["shop_id"])

            icon = "❌" if alert["type"] == "out_of_stock" else "⚠️"
            status = "hết hàng" if alert["type"] == "out_of_stock" else f"còn {alert['qty']}"

            msg = (
                f"🔄 CẬP NHẬT CẢNH BÁO KHO\n"
                f"🏪 Shop: {shop_name}\n"
                f"🆔 Shop ID: {alert['shop_id']}\n\n"
                f"{icon} {alert['product_name']} | Mã: {alert['product_code']} | "
                f"{status} (trước đó: {old_qty})"
            )
            messages.append(msg)

    return messages


def send_messages(messages, dry_run=False):
    if not messages:
        print("Không có tin nhắn để gửi.")
        return

    sent_count = 0
    failed_count = 0
    for msg in messages:
        if dry_run:
            print("----- DRY RUN MESSAGE -----")
            print(msg)
            continue
        try:
            send_telegram(msg)
            sent_count += 1
            print("Đã gửi Telegram.")
        except requests.RequestException as exc:
            failed_count += 1
            print(f"Lỗi gửi Telegram: {exc}")

    if not dry_run:
        print(f"Kết quả gửi Telegram: thành công {sent_count}, lỗi {failed_count}")


def parse_args():
    parser = argparse.ArgumentParser(description="Stock alert sender")
    parser.add_argument(
        "--mode",
        choices=["incremental", "daily"],
        default="incremental",
        help="incremental: gửi mới/cập nhật theo cache; daily: gửi tổng hợp theo shop",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Ngưỡng low stock (qty < threshold)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Không gửi Telegram, chỉ in nội dung",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    threshold = args.threshold if args.threshold is not None else int(os.getenv("LOW_STOCK_THRESHOLD", LOW_STOCK_THRESHOLD))

    shop_map = build_shop_map()
    files = glob.glob("stock_*.json")
    all_current_alerts = {}

    for path in files:
        shop_id = get_shop_id_from_path(path)
        data = load_stock_file(path)
        shop_alerts = extract_current_alerts(data, shop_id, threshold=threshold)
        all_current_alerts.update(shop_alerts)

    if args.mode == "daily":
        messages = build_daily_grouped_messages(all_current_alerts, shop_map, threshold)
        send_messages(messages, dry_run=args.dry_run)
        print(f"[daily] Tổng cảnh báo hiện tại: {len(all_current_alerts)}")
        return

    old_cache = load_cache()
    messages = build_new_alert_messages(all_current_alerts, old_cache, shop_map)

    send_messages(messages, dry_run=args.dry_run)

    # chỉ lưu những cảnh báo còn đang tồn tại
    if not args.dry_run:
        save_cache(all_current_alerts)

    print(f"[incremental] Tổng cảnh báo mới/cập nhật: {len(messages)}")
    print(f"[incremental] Tổng cảnh báo đang active: {len(all_current_alerts)}")


if __name__ == "__main__":
    main()
