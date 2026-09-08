import json
from datetime import datetime, timedelta
from pathlib import Path


def fmt_number(value):
    try:
        return f"{int(round(value)):,}".replace(",", ".")
    except Exception:
        return "0"


def get_target_day():
    """
    Chỉ kiểm tra ngày cách đây 2 ngày
    Ví dụ hôm nay 20 -> check ngày 18
    """
    return (datetime.today() - timedelta(days=2)).strftime("%Y-%m-%d")


def load_day_result(shop_key, target_date):
    data_file = Path(f"data_{shop_key}.json")
    if not data_file.exists():
        return None

    try:
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    rows = data.get("data", [])
    for row in rows:
        if row.get("Time.day") == target_date:
            return row.get("result", {})
    return None


def has_activity(result):
    """
    Chỉ coi là cần nhập ads nếu ngày đó có phát sinh thật:
    - có doanh thu
    hoặc
    - có đơn chốt
    hoặc
    - có order_count
    """
    if not result:
        return False

    revenue = result.get("revenue", 0)
    success_order_count = result.get("success_order_count", 0)
    order_count = result.get("order_count", 0)

    return revenue > 0 or success_order_count > 0 or order_count > 0


def build_alert():
    target_day = get_target_day()

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            shops = json.load(f)

    lines = []
    found_issue = False

    lines.append("⚠️ CANH BAO CHI PHI QUANG CAO")
    lines.append("")
    lines.append(f"Ngay can kiem tra: {target_day}")
    lines.append("Chi canh bao khi da qua 1 ngay nhap lieu nhung van chua co chi phi quang cao.")
    lines.append("")

    for shop in shops:
        if shop.get("status") != "active":
            continue

        shop_key = shop.get("shop_key", "")
        shop_name = shop.get("shop_name", shop_key)
        shop_id = shop.get("shop_id", "")
        shop_display = f"{shop_name} ({shop_key} | {shop_id})"

        result = load_day_result(shop_key, target_day)

        if not result:
            continue

        ads = result.get("ads_amount", 0)
        orders = result.get("success_order_count", 0)
        revenue = result.get("revenue", 0)

        if has_activity(result) and ads == 0:
            found_issue = True
            lines.append(shop_display)
            lines.append(
                f"- Ads = 0 | Don chot = {fmt_number(orders)} | Doanh thu = {fmt_number(revenue)}"
            )
            lines.append("=> Co the chua dien chi phi quang cao")
            lines.append("=> Yeu cau ke toan kiem tra")
            lines.append("")

    if not found_issue:
        return f"✅ KHONG PHAT HIEN SHOP NAO THIEU CHI PHI QUANG CAO CHO NGAY {target_day}"

    return "\n".join(lines)


if __name__ == "__main__":
    print(build_alert())
