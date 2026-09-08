#!/usr/bin/env python3
"""Tra cứu 1 page: tên gì + TK QC nào chạy (spend) + NV phụ trách + shop bind.

Chạy: DATABASE_URL=... .venv/bin/python3 scripts/_lookup_page.py <page_id> [days_back]
Mặc định page_id=664088690129474, days_back=60. CHỈ ĐỌC, không sửa gì.
"""
import sys, os
_BASE = os.path.join(os.path.dirname(__file__), '..')
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
from db import get_conn

PAGE_ID = (sys.argv[1] if len(sys.argv) > 1 else "664088690129474").strip()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 60

def main():
    with get_conn() as conn, conn.cursor() as cur:
        # Tên page (ưu tiên pa_pages, fallback spend table)
        name = None
        cur.execute("SELECT page_name FROM pa_pages WHERE page_id=%s", (PAGE_ID,))
        r = cur.fetchone()
        if r and r[0]:
            name = r[0]
        cur.execute("SELECT MAX(page_name) FROM fb_ads_page_daily_spend WHERE page_id=%s", (PAGE_ID,))
        r = cur.fetchone()
        spend_name = r[0] if r else None
        print(f"==> PAGE {PAGE_ID}")
        print(f"    Tên (pa_pages): {name or '(không có trong pa_pages)'}")
        print(f"    Tên (theo spend): {spend_name or '(chưa từng có spend)'}")

        # TK QC nào chạy page này (trong DAYS ngày gần đây) + NV phụ trách hiện tại
        cur.execute("""
            SELECT s.fb_ad_account_id,
                   COALESCE(ai.account_name, s.fb_ad_account_id) AS acc_name,
                   SUM(s.spend) AS spend,
                   MIN(s.metric_date) AS first_d,
                   MAX(s.metric_date) AS last_d
              FROM fb_ads_page_daily_spend s
              LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
             WHERE s.page_id = %s
               AND s.metric_date >= (CURRENT_DATE - %s::int)
             GROUP BY s.fb_ad_account_id, ai.account_name
             ORDER BY spend DESC
        """, (PAGE_ID, DAYS))
        rows = cur.fetchall()
        print(f"\n    TK QC chạy page này ({DAYS} ngày gần đây):")
        if not rows:
            print("      (không có TK nào chạy page này trong khoảng)")
        for aid, acc_name, spend, fd, ld in rows:
            # NV đang phụ trách TK này
            cur.execute("""
                SELECT COALESCE(NULLIF(u.full_name,''), u.username), a.assigned_from
                  FROM user_ad_account_assignments a
                  JOIN users u ON u.id = a.user_id
                 WHERE a.ad_account_id = %s AND a.assigned_to IS NULL
            """, (aid,))
            nv = cur.fetchall()
            nv_txt = ", ".join(f"{n} (từ {af})" for n, af in nv) if nv else "(chưa gán NV)"
            print(f"      • {acc_name} [{aid}] — spend {float(spend or 0):,.0f}đ "
                  f"({fd}→{ld}) — NV: {nv_txt}")

        # Page bind shop hiện tại
        cur.execute("""
            SELECT sh.shop_name, sh.shop_key, b.assigned_from
              FROM fb_page_shop_binding b
              JOIN shops sh ON sh.id = b.pos_shop_id
             WHERE b.page_id = %s AND b.assigned_to IS NULL
        """, (PAGE_ID,))
        b = cur.fetchall()
        print(f"\n    Bind shop HIỆN TẠI: " +
              (", ".join(f"{n} ({k}, từ {af})" for n, k, af in b) if b else "(chưa bind shop)"))

        # LỊCH SỬ bind shop (tất cả khoảng, kể cả đã đóng)
        cur.execute("""
            SELECT sh.shop_name, sh.shop_key, b.assigned_from, b.assigned_to,
                   COALESCE(NULLIF(ub.full_name,''), ub.username) AS by_name, b.note
              FROM fb_page_shop_binding b
              JOIN shops sh ON sh.id = b.pos_shop_id
              LEFT JOIN users ub ON ub.id = b.assigned_by
             WHERE b.page_id = %s
             ORDER BY b.assigned_from
        """, (PAGE_ID,))
        hist = cur.fetchall()
        print(f"\n    LỊCH SỬ bind shop (page {PAGE_ID}):")
        if not hist:
            print("      (chưa từng bind shop nào)")
        for n, k, af, at_, by, note in hist:
            ky = f"{af} → {at_ if at_ else 'nay'}"
            extra = f" · bởi {by}" if by else ""
            extra += f" · {note}" if note else ""
            print(f"      • {ky}: {n} ({k}){extra}")

        # LỊCH SỬ TK QC chạy page (theo tháng, all-time)
        cur.execute("""
            SELECT to_char(s.metric_date, 'YYYY-MM') AS thang,
                   COALESCE(ai.account_name, s.fb_ad_account_id) AS acc_name,
                   s.fb_ad_account_id,
                   SUM(s.spend) AS spend
              FROM fb_ads_page_daily_spend s
              LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
             WHERE s.page_id = %s
             GROUP BY thang, ai.account_name, s.fb_ad_account_id
             ORDER BY thang, spend DESC
        """, (PAGE_ID,))
        mh = cur.fetchall()
        print(f"\n    LỊCH SỬ TK chạy page (theo tháng, all-time):")
        if not mh:
            print("      (chưa từng có spend)")
        cur_thang = None
        for thang, acc_name, aid, spend in mh:
            if thang != cur_thang:
                print(f"      [{thang}]")
                cur_thang = thang
            print(f"         - {acc_name} [{aid}]: {float(spend or 0):,.0f}đ")

if __name__ == "__main__":
    main()
