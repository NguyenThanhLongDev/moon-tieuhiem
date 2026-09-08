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
    query_text = " ".join(sys.argv[1:]).strip().lower()
    if not query_text:
        query_text = "phan tich hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can phan tich")
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

        shop_key = shop.get("shop_key", "")
        shop_name = shop.get("shop_name", shop_key)
        shop_id = shop.get("shop_id", "")
        shop_display = f"{shop_name} ({shop_key} | {shop_id})"

        result = load_day_result(shop_key, target_date) or {}
        rows.append({
            "shop_display": shop_display,
            "revenue": result.get("revenue", 0) or 0,
            "profit": result.get("profit", 0) or 0,
            "ads_order": result.get("ads_order", 0) or 0,
            "success_order_count": result.get("success_order_count", 0) or 0,
        })

    if "lo" in query_text:
        losers = [x for x in rows if x["profit"] < 0]
        if not losers:
            print(f"Khong co shop nao lo ngay {target_date}")
        else:
            print(f"SHOP LO NGAY {target_date}\n")
            for item in sorted(losers, key=lambda x: x["profit"]):
                print(f"{item['shop_display']}: {fmt_number(item['profit'])}")
        sys.exit(0)

    if "cao nhat" in query_text and "doanh thu" in query_text:
        top_items = sorted(rows, key=lambda x: x["revenue"], reverse=True)[:5]
        print(f"TOP DOANH THU NGAY {target_date}\n")
        for i, item in enumerate(top_items, start=1):
            print(f"{i}. {item['shop_display']}: {fmt_number(item['revenue'])}")
        sys.exit(0)

    if "thap nhat" in query_text and "loi nhuan" in query_text:
        low_items = sorted(rows, key=lambda x: x["profit"])[:5]
        print(f"TOP LOI NHUAN THAP NGAY {target_date}\n")
        for i, item in enumerate(low_items, start=1):
            print(f"{i}. {item['shop_display']}: {fmt_number(item['profit'])}")
        sys.exit(0)

    print(f"PHAN TICH NGAY {target_date}\n")
    for item in rows:
        print(item["shop_display"])
        print(f"- Doanh thu: {fmt_number(item['revenue'])}")
        print(f"- Loi nhuan: {fmt_number(item['profit'])}")
        print(f"- QC/don: {fmt_number(item['ads_order'])}")
        print(f"- Don chot: {fmt_number(item['success_order_count'])}")
        print("")
