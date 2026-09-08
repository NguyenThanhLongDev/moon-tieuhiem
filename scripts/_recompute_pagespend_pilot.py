#!/usr/bin/env python3
"""PILOT: tính lại fb_ads_page_daily_spend cho 1 TK + 1 ngày từ ad-level (mb_fb_entity_daily).

Nguyên tắc:
- Chỉ cộng spend của ad GẮN ĐÚNG page (page_id khác rỗng).
- KHÔNG dồn spend 'vô chủ' (ad không page) vào page nào.
- Ghi đè trọn (TK, ngày): xóa dòng cũ rồi insert bộ mới → hết rác stale.
Có backup CSV trước khi ghi để hoàn lại.
"""
import os, sys, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_conn

ACC = "1287101560174717"
DATE = "2026-06-02"
BACKUP = f"/home/admin1/tieuhiemsoft/posbottieuhiem/backup/pagespend_{ACC}_{DATE}.csv"
os.makedirs(os.path.dirname(BACKUP), exist_ok=True)

with get_conn() as conn, conn.cursor() as cur:
    # ── BEFORE ──
    cur.execute("""SELECT page_id, page_name, spend, impressions, clicks
                     FROM fb_ads_page_daily_spend
                    WHERE fb_ad_account_id=%s AND metric_date=%s
                    ORDER BY spend DESC""", (ACC, DATE))
    before = cur.fetchall()
    print(f"=== BEFORE — page-table TK {ACC} ngày {DATE} ===")
    tb = 0
    for pid, nm, sp, imp, cl in before:
        tb += float(sp or 0)
        print(f"  page={pid:<20} spend={float(sp or 0):>12,.0f}  {nm}")
    print(f"  TỔNG = {tb:,.0f}")

    # backup
    with open(BACKUP, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fb_ad_account_id","page_id","page_name","metric_date","spend","impressions","clicks"])
        for pid, nm, sp, imp, cl in before:
            w.writerow([ACC, pid, nm, DATE, sp, imp, cl])
    print(f"  → backup: {BACKUP}")

    # ── NEW từ ad-level (chỉ ad có page) ──
    cur.execute("""SELECT page_id, SUM(spend), SUM(impressions), SUM(clicks)
                     FROM mb_fb_entity_daily
                    WHERE account_id=%s AND metric_date=%s
                      AND page_id IS NOT NULL AND page_id<>''
                    GROUP BY page_id ORDER BY 2 DESC""", (ACC, DATE))
    newrows = cur.fetchall()

    # spend 'vô chủ' (để báo, không ghi)
    cur.execute("""SELECT COALESCE(SUM(spend),0) FROM mb_fb_entity_daily
                    WHERE account_id=%s AND metric_date=%s
                      AND (page_id IS NULL OR page_id='')""", (ACC, DATE))
    voochu = float(cur.fetchone()[0] or 0)

    # page_name map
    pids = [r[0] for r in newrows]
    namemap = {}
    if pids:
        cur.execute("""SELECT page_id, MAX(page_name) FROM fb_ads_page_daily_spend
                        WHERE page_id = ANY(%s) AND page_name<>'' GROUP BY page_id""", (pids,))
        namemap = {str(p): n for p, n in cur.fetchall()}

    print(f"\n=== NEW (tính lại từ ad-level, chỉ ad gắn page) ===")
    tn = 0
    for pid, sp, imp, cl in newrows:
        tn += float(sp or 0)
        print(f"  page={pid:<20} spend={float(sp or 0):>12,.0f}  {namemap.get(str(pid),'')}")
    print(f"  TỔNG page = {tn:,.0f}")
    print(f"  (spend 'vô chủ' bị loại khỏi báo cáo page = {voochu:,.0f})")
    print(f"  → account ad-level total = {tn+voochu:,.0f}  |  Facebook thật = 233,736")

    # ── APPLY ──
    cur.execute("DELETE FROM fb_ads_page_daily_spend WHERE fb_ad_account_id=%s AND metric_date=%s",
                (ACC, DATE))
    for pid, sp, imp, cl in newrows:
        cur.execute("""INSERT INTO fb_ads_page_daily_spend
                         (fb_ad_account_id, page_id, page_name, metric_date, spend, impressions, clicks, synced_at)
                       VALUES (%s,%s,%s,%s::date,%s,%s,%s,NOW())""",
                    (ACC, str(pid), namemap.get(str(pid),''), DATE, sp, imp, cl))
    conn.commit()

    # ── AFTER ──
    cur.execute("""SELECT page_id, page_name, spend FROM fb_ads_page_daily_spend
                    WHERE fb_ad_account_id=%s AND metric_date=%s ORDER BY spend DESC""", (ACC, DATE))
    print(f"\n=== AFTER — page-table TK {ACC} ngày {DATE} ===")
    for pid, nm, sp in cur.fetchall():
        mark = "  <-- Kim Khi Giá Rẻ" if str(pid)=="1047506838441212" else ""
        print(f"  page={pid:<20} spend={float(sp or 0):>12,.0f}  {nm}{mark}")
