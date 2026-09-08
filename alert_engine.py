import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOSS_THRESHOLD = 0
ADS_ORDER_THRESHOLD = 70000

def fmt_number(value):
    try:
        return f"{int(round(value)):,}".replace(",", ".")
    except:
        return "0"

def parse_date(text):
    text = text.lower().strip()
    today = datetime.today()

    if "hôm nay" in text:
        return today.strftime("%Y-%m-%d")

    if "hôm qua" in text:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    if "hôm kia" in text:
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")

    for part in text.replace(",", " ").split():
        try:
            return datetime.strptime(part, "%Y-%m-%d").strftime("%Y-%m-%d")
        except:
            pass

        try:
            return datetime.strptime(part, "%d/%m/%Y").strftime("%Y-%m-%d")
        except:
            pass

        try:
            return datetime.strptime(part, "%d%m%Y").strftime("%Y-%m-%d")
        except:
            pass

        try:
            return datetime.strptime(part, "%d/%m").replace(year=today.year).strftime("%Y-%m-%d")
        except:
            pass

    return None

query_text = " ".join(sys.argv[1:]).strip()
target_date = parse_date(query_text) if query_text else datetime.today().strftime("%Y-%m-%d")

try:
    from shop_helpers import load_all_shops as _load_shops
    shops = _load_shops()
except Exception:
    with open("shops.json", "r", encoding="utf-8") as f:
        shops = json.load(f)

alerts = []

for shop in shops:
    shop_key = shop.get("shop_key", "")
    shop_name = shop.get("shop_name", shop_key)

    data_file = Path(f"data_{shop_key}.json")
    if not data_file.exists():
        continue

    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("data", [])

    for row in rows:
        ngay = row.get("Time.day", "")
        if ngay != target_date:
            continue

        result = row.get("result", {})

        revenue = result.get("revenue", 0)
        profit = result.get("profit", 0)
        ads_amount = result.get("ads_amount", 0)
        ads_order = result.get("ads_order", 0)
        avg_profit = result.get("avg_profit", 0)

        if profit < LOSS_THRESHOLD:
            alerts.append(
                f"⚠️ Shop lỗ: {shop_name}\n"
                f"Ngày: {ngay}\n"
                f"Doanh thu: {fmt_number(revenue)}\n"
                f"Lợi nhuận: {fmt_number(profit)}\n"
                f"Chi phí quảng cáo: {fmt_number(ads_amount)}"
            )

        if ads_order > ADS_ORDER_THRESHOLD:
            alerts.append(
                f"⚠️ QC/đơn cao: {shop_name}\n"
                f"Ngày: {ngay}\n"
                f"QC/đơn: {fmt_number(ads_order)}\n"
                f"Chi phí quảng cáo: {fmt_number(ads_amount)}\n"
                f"LNTB/đơn: {fmt_number(avg_profit)}"
            )

if alerts:
    print("\n\n".join(alerts))
else:
    print(f"Không có cảnh báo cho ngày {target_date}")
