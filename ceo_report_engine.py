import json
import sys
from pathlib import Path
from datetime import datetime
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


def is_ads_abnormal(item):
    orders = item.get("success_order_count", 0) or 0
    revenue = item.get("revenue", 0) or 0
    ads = item.get("ads_amount", 0) or 0
    ads_order = item.get("ads_order", 0) or 0

    reasons = []

    if (orders > 0 or revenue > 0) and ads == 0:
        reasons.append("ads = 0")

    if orders >= 10 and ads_order > 0 and ads_order < 10000:
        reasons.append("ads/don thap")

    if ads_order > 80000:
        reasons.append("ads/don cao")

    return reasons


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]).strip()
    if not query_text:
        query_text = "hom nay"

    target_date = parse_target_date(query_text)
    if target_date is None:
        print("Khong hieu ngay can tong hop")
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
            "ads_amount": result.get("ads_amount", 0) or 0,
            "ads_order": result.get("ads_order", 0) or 0,
            "success_order_count": result.get("success_order_count", 0) or 0,
        })

    total_revenue = sum(x["revenue"] for x in rows)
    total_orders = sum(x["success_order_count"] for x in rows)
    total_ads = sum(x["ads_amount"] for x in rows)
    total_profit = sum(x["profit"] for x in rows)

    losers = [x for x in rows if x["profit"] < 0]

    abnormal_ads = []
    for item in rows:
        reasons = is_ads_abnormal(item)
        if reasons:
            abnormal_ads.append({
                "shop_display": item["shop_display"],
                "reasons": ", ".join(reasons),
                "ads_amount": item["ads_amount"],
                "ads_order": item["ads_order"],
            })

    top_revenue = sorted(rows, key=lambda x: x["revenue"], reverse=True)[:5]

    lines = [
        f"📊 TONG HOP TOAN HE THONG - {target_date}",
        "",
        f"- Tong doanh thu: {fmt_number(total_revenue)}",
        f"- Tong don: {fmt_number(total_orders)}",
        f"- Tong ads: {fmt_number(total_ads)}",
        f"- Tong loi nhuan: {fmt_number(total_profit)}",
        "",
        "⚠️ SHOP LO",
    ]

    if losers:
        for item in sorted(losers, key=lambda x: x["profit"]):
            lines.append(f"- {item['shop_display']}: {fmt_number(item['profit'])}")
    else:
        lines.append("- Khong co shop lo")

    lines.extend(["", "⚠️ SHOP ADS BAT THUONG"])

    if abnormal_ads:
        for item in abnormal_ads[:10]:
            lines.append(
                f"- {item['shop_display']} | {item['reasons']} | Ads: {fmt_number(item['ads_amount'])} | QC/don: {fmt_number(item['ads_order'])}"
            )
    else:
        lines.append("- Khong phat hien shop ads bat thuong")

    lines.extend(["", "🔥 SHOP TOP DOANH THU"])

    for i, item in enumerate(top_revenue, start=1):
        lines.append(f"{i}. {item['shop_display']}: {fmt_number(item['revenue'])}")

    if target_date == datetime.today().strftime("%Y-%m-%d"):
        lines.extend([
            "",
            "Ghi chu: Neu la hom nay, so lieu co the chua cap nhat day du. Nen /sync truoc khi xem."
        ])

    print("\n".join(lines))
