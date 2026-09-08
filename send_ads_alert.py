#!/usr/bin/env python3
"""
Báo cáo chi phí quảng cáo Telegram (rewrite 2026-05-14, Phase 2).

3 phần:
  1) HÔM NAY (real-time từ FB Ads)         — tổng + VAT 11,3%
  2) PHÂN TÁCH                              — Đã gán shop / Chưa gán shop / Page test
  3) ĐÃ CHỐT KẾ TOÁN (từ POS)               — 3 ngày gần nhất có ads_cost > 0
                                              (POS chỉ điền sau 2-3 ngày để tránh
                                              rủi ro hoàn tiền FB)

Nguồn data:
  - `fb_ads_page_daily_spend` (PG)  — spend per (account, page, ngày)
  - `fb_page_shop_binding`     (PG) — page có thuộc shop nào không
  - `daily_shop_metrics`       (PG) — số POS đã chốt
"""
from __future__ import annotations

import logging
import sys

from db import get_conn
from tz_utils import now_hcm
from telegram_common import (
    fmt_money, fmt_pct, fmt_header, fmt_footer, safe_send, section,
)

# FB Ads Việt Nam — spend × 1.113 = đã VAT (xem CLAUDE.md §11.4)
ADS_VAT = 1.113

log = logging.getLogger(__name__)


def build_report() -> str:
    today = now_hcm().date()

    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) Tổng spend hôm nay (page-level, PK unique nên không trùng)
            cur.execute(
                """
                SELECT COALESCE(SUM(spend), 0)
                FROM fb_ads_page_daily_spend
                WHERE metric_date = %s
                """,
                (today,),
            )
            total_today = float(cur.fetchone()[0] or 0) * ADS_VAT

            # 2) Phân tách theo binding:
            #    bound   — có row binding với pos_shop_id IS NOT NULL
            #    test    — binding tồn tại nhưng pos_shop_id NULL (page test/marketing)
            #    unbound — chưa có row binding
            cur.execute(
                """
                SELECT
                    CASE
                        WHEN b.pos_shop_id IS NOT NULL THEN 'bound'
                        WHEN b.id IS NOT NULL AND b.pos_shop_id IS NULL THEN 'test'
                        ELSE 'unbound'
                    END AS bucket,
                    COALESCE(SUM(s.spend), 0) AS total_spend,
                    COUNT(DISTINCT s.page_id) AS page_count
                FROM fb_ads_page_daily_spend s
                LEFT JOIN fb_page_shop_binding b
                       ON b.page_id = s.page_id AND b.assigned_to IS NULL
                WHERE s.metric_date = %s
                GROUP BY 1
                """,
                (today,),
            )
            buckets = {
                r[0]: (float(r[1] or 0) * ADS_VAT, int(r[2] or 0))
                for r in cur.fetchall()
            }

            # 3) POS spend — 3 ngày gần nhất có ads_cost > 0
            cur.execute(
                """
                SELECT metric_date, SUM(ads_cost) AS total
                FROM daily_shop_metrics
                WHERE metric_date < %s
                GROUP BY metric_date
                HAVING SUM(ads_cost) > 0
                ORDER BY metric_date DESC
                LIMIT 3
                """,
                (today,),
            )
            pos_rows = [(r[0], float(r[1] or 0)) for r in cur.fetchall()]

    bound_spend,   bound_pages   = buckets.get("bound",   (0.0, 0))
    unbound_spend, unbound_pages = buckets.get("unbound", (0.0, 0))
    test_spend,    test_pages    = buckets.get("test",    (0.0, 0))

    section_today = (
        f"   Tổng spend: {fmt_money(total_today)}  (đã VAT 11,3%)\n"
        f"   ✅ Đã gán shop:    {fmt_money(bound_spend)}  ({fmt_pct(bound_spend, total_today)}) — {bound_pages} page\n"
        f"   ⚠️ Chưa gán shop:  {fmt_money(unbound_spend)}  ({fmt_pct(unbound_spend, total_today)}) — {unbound_pages} page"
    )
    if test_pages > 0:
        section_today += (
            f"\n   ⚙️ Page test:      {fmt_money(test_spend)}  "
            f"({fmt_pct(test_spend, total_today)}) — {test_pages} page"
        )

    if pos_rows:
        section_pos_body = "\n".join(
            f"   {d.strftime('%d/%m/%Y')}: {fmt_money(v)}" for d, v in pos_rows
        )
    else:
        section_pos_body = "   Chưa có dữ liệu chốt"

    lines = [
        fmt_header("Báo cáo chi phí quảng cáo", f"hôm nay {today.strftime('%d/%m/%Y')}"),
        "",
        section("HÔM NAY (real-time từ FB Ads)", section_today),
        "",
        section("ĐÃ CHỐT KẾ TOÁN (từ POS)", section_pos_body),
        "",
        "(POS chỉ điền sau 2-3 ngày để tránh rủi ro hoàn tiền FB)",
        fmt_footer("Page chưa gán → /page-account/page-binding"),
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        text = build_report()
    except Exception as exc:
        log.exception("ads_alert build error")
        print(f"[ads_alert] build error: {exc}", file=sys.stderr)
        return 1
    if "--dry-run" in sys.argv:
        print(text)
        return 0
    ok, fail = safe_send(text, audience="default")
    print(f"[ads_alert] ok={ok} fail={fail}")
    return 0 if (ok > 0 or fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
