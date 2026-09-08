import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from date_parser import parse_target_date


def fmt_number(value):
    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except Exception:
        return "0"


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


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]).strip()
    if not query_text:
        query_text = "so sanh hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can so sanh")
        sys.exit(1)

    prev_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            shops = json.load(f)

    lines = [f"SO SANH {target_date} VOI {prev_date}", ""]

    for shop in shops:
        if shop.get("status") != "active":
            continue

        shop_display = f"{shop.get('shop_name', shop.get('shop_key', ''))} ({shop.get('shop_key', '')} | {shop.get('shop_id', '')})"

        cur = load_day_result(shop.get("shop_key", ""), target_date) or {}
        prev = load_day_result(shop.get("shop_key", ""), prev_date) or {}

        cur_revenue = cur.get("revenue", 0) or 0
        prev_revenue = prev.get("revenue", 0) or 0
        cur_profit = cur.get("profit", 0) or 0
        prev_profit = prev.get("profit", 0) or 0

        revenue_diff = cur_revenue - prev_revenue
        profit_diff = cur_profit - prev_profit

        lines.append(shop_display)
        lines.append(f"- Doanh thu: {fmt_number(prev_revenue)} -> {fmt_number(cur_revenue)} | Lech: {fmt_number(revenue_diff)}")
        lines.append(f"- Loi nhuan: {fmt_number(prev_profit)} -> {fmt_number(cur_profit)} | Lech: {fmt_number(profit_diff)}")
        lines.append("")

    print("\n".join(lines))
