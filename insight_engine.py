import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from date_parser import parse_target_date


def fmt_number(v):
    try:
        return f"{int(round(float(v))):,}".replace(",", ".")
    except:
        return "0"


def load_day(shop_key, date):
    f = Path(f"data_{shop_key}.json")
    if not f.exists():
        return {}

    try:
        data = json.load(open(f, "r", encoding="utf-8"))
    except:
        return {}

    for row in data.get("data", []):
        if row.get("Time.day") == date:
            return row.get("result", {})
    return {}


def get_prev(date):
    return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def percent_change(cur, prev):
    if prev == 0:
        return 0
    return (cur - prev) / prev * 100


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        text = "hom nay"

    target = parse_target_date(text)
    if not target:
        print("Khong hieu ngay")
        sys.exit(1)

    prev = get_prev(target)

    try:
        from shop_helpers import load_all_shops
        shops = load_all_shops()
    except Exception:
        shops = json.load(open("shops.json", "r", encoding="utf-8"))

    rows = []

    for s in shops:
        if s.get("status") != "active":
            continue

        cur = load_day(s["shop_key"], target)
        pre = load_day(s["shop_key"], prev)

        rows.append({
            "name": f"{s['shop_name']} ({s['shop_key']})",
            "rev": cur.get("revenue", 0) or 0,
            "rev_prev": pre.get("revenue", 0) or 0,
            "profit": cur.get("profit", 0) or 0,
            "ads": cur.get("ads_amount", 0) or 0,
            "ads_order": cur.get("ads_order", 0) or 0,
            "orders": cur.get("success_order_count", 0) or 0
        })

    total_rev = sum(x["rev"] for x in rows)
    total_prev = sum(x["rev_prev"] for x in rows)
    total_ads = sum(x["ads"] for x in rows)
    total_profit = sum(x["profit"] for x in rows)

    change = percent_change(total_rev, total_prev)

    lines = []
    lines.append(f"📊 INSIGHT NGAY {target}")
    lines.append("")

    # ===== Tổng quan =====
    trend = "⬆️" if change > 0 else "⬇️"
    lines.append("TONG QUAN")
    lines.append(f"- Doanh thu: {fmt_number(total_rev)} ({trend} {round(change,1)}%)")
    lines.append(f"- Ads: {fmt_number(total_ads)}")
    lines.append(f"- Loi nhuan: {fmt_number(total_profit)}")
    lines.append("")

    if change < -10:
        lines.append("⚠️ Doanh thu giam manh")
    elif change > 10:
        lines.append("🔥 Doanh thu tang tot")
    else:
        lines.append("➡️ Doanh thu on dinh")
    lines.append("")

    # ===== Shop giảm mạnh =====
    drop = []
    for x in rows:
        diff = x["rev"] - x["rev_prev"]
        if diff < -3000000:
            drop.append((x, diff))

    if drop:
        lines.append("🔻 SHOP GIAM MANH")
        for x, d in sorted(drop, key=lambda x: x[1]):
            lines.append(f"- {x['name']}: {fmt_number(d)}")
        lines.append("")

    # ===== Shop tốt =====
    top = sorted(rows, key=lambda x: x["rev"], reverse=True)[:3]
    lines.append("🔥 SHOP TOT")
    for x in top:
        lines.append(f"- {x['name']}: {fmt_number(x['rev'])}")
    lines.append("")

    # ===== Ads bất thường =====
    bad_ads = []
    for x in rows:
        if x["rev"] > 0 and x["ads"] == 0:
            bad_ads.append(x)

    if bad_ads:
        lines.append("⚠️ ADS BAT THUONG")
        for x in bad_ads[:5]:
            lines.append(f"- {x['name']} (co doanh thu nhung ads=0)")
        lines.append("")

    # ===== Shop lỗ =====
    loss = [x for x in rows if x["profit"] < 0]
    if loss:
        lines.append("⚠️ SHOP LO")
        for x in loss[:5]:
            lines.append(f"- {x['name']}: {fmt_number(x['profit'])}")
        lines.append("")

    # ===== Kết luận =====
    lines.append("🧠 KET LUAN")

    if change < -10:
        lines.append("- Hieu suat dang xau di")
    if bad_ads:
        lines.append("- Co shop chua nhap ads")
    if loss:
        lines.append("- Co shop dang lo")

    lines.append("")

    # ===== Action =====
    lines.append("🎯 HANH DONG")
    if drop:
        lines.append("- Check cac shop giam manh")
    if bad_ads:
        lines.append("- Yeu cau nhap ads")
    if loss:
        lines.append("- Xu ly shop lo")

    print("\n".join(lines))
