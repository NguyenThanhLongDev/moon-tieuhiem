import json
import sys
from pathlib import Path
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
        query_text = "top hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can xem top")
        sys.exit(1)

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            shops = json.load(f)

    rows = []

    for shop in shops:
        if shop.get("status") != "active":
            continue

        result = load_day_result(shop.get("shop_key", ""), target_date) or {}
        rows.append({
            "shop_display": f"{shop.get('shop_name', shop.get('shop_key', ''))} ({shop.get('shop_key', '')} | {shop.get('shop_id', '')})",
            "revenue": result.get("revenue", 0) or 0,
            "profit": result.get("profit", 0) or 0,
            "ads_order": result.get("ads_order", 0) or 0,
        })

    top_revenue = sorted(rows, key=lambda x: x["revenue"], reverse=True)[:3]
    top_profit = sorted(rows, key=lambda x: x["profit"], reverse=True)[:3]
    top_ads = sorted(rows, key=lambda x: x["ads_order"], reverse=True)[:3]
    losers = [x for x in rows if x["profit"] < 0]

    lines = [f"TOP NGAY {target_date}", "", "Doanh thu:"]
    for i, item in enumerate(top_revenue, start=1):
        lines.append(f"{i}. {item['shop_display']}: {fmt_number(item['revenue'])}")

    lines.extend(["", "Loi nhuan:"])
    for i, item in enumerate(top_profit, start=1):
        lines.append(f"{i}. {item['shop_display']}: {fmt_number(item['profit'])}")

    lines.extend(["", "QC/don cao:"])
    for i, item in enumerate(top_ads, start=1):
        lines.append(f"{i}. {item['shop_display']}: {fmt_number(item['ads_order'])}")

    lines.extend(["", "Shop lo:"])
    if losers:
        for item in losers:
            lines.append(f"- {item['shop_display']}: {fmt_number(item['profit'])}")
    else:
        lines.append("Khong co shop lo")

    print("\n".join(lines))
