#!/usr/bin/env python3
"""
Báo cáo cuối ngày HÔM NAY (rewrite 2026-05-14, Phase 2).

Gửi lúc 20:00 — snapshot real-time trước khi đóng ngày. Khác `daily_summary`
(07:00 sáng — tổng kết hôm qua đã chốt).

Sections:
  1) SNAPSHOT HÔM NAY (tới 20:00) — đơn + doanh thu net + CP QC tới giờ
  2) TOP 5 SHOP HÔM NAY theo doanh thu
  3) SHOP TIỀM NĂNG BỊ LỖ (CP QC > 30% doanh thu hoặc P&L âm)

Lưu ý: số trong báo cáo này CHƯA CHỐT — số chốt sẽ ở báo cáo sáng mai 07:00.

Nguồn: `daily_shop_metrics` (PG).
"""
from __future__ import annotations

import logging
import sys

from db import get_conn
from tz_utils import now_hcm
from telegram_common import (
    fmt_money, fmt_int, fmt_header, fmt_footer, safe_send, section,
)

log = logging.getLogger(__name__)


def build_report() -> str:
    today = now_hcm().date()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    SUM(order_count)::bigint,
                    SUM(net_revenue)::numeric,
                    SUM(ads_cost)::numeric,
                    SUM(pos_profit_loss)::numeric
                FROM daily_shop_metrics
                WHERE metric_date = %s
                """,
                (today,),
            )
            orders, net, ads, pl = cur.fetchone() or (0, 0, 0, 0)
            orders = int(orders or 0)
            net    = float(net or 0)
            ads    = float(ads or 0)
            pl     = float(pl or 0)

            cur.execute(
                """
                SELECT s.shop_name, m.order_count, m.net_revenue, m.pos_profit_loss
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s AND m.net_revenue > 0
                ORDER BY m.net_revenue DESC
                LIMIT 5
                """,
                (today,),
            )
            top_rev = [(r[0] or "?", int(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in cur.fetchall()]

            # Shop tiềm năng lỗ: CP QC chiếm > 30% doanh thu hoặc P&L < 0
            cur.execute(
                """
                SELECT s.shop_name, m.net_revenue, m.ads_cost, m.pos_profit_loss
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s
                  AND m.net_revenue > 0
                  AND (m.pos_profit_loss < 0 OR m.ads_cost > m.net_revenue * 0.3)
                ORDER BY m.pos_profit_loss ASC
                LIMIT 10
                """,
                (today,),
            )
            warning_shops = [
                (r[0] or "?", float(r[1] or 0), float(r[2] or 0), float(r[3] or 0))
                for r in cur.fetchall()
            ]

    if orders == 0:
        text = "\n".join([
            fmt_header("Báo cáo cuối ngày", today.strftime("%d/%m/%Y")),
            "",
            "⚠️ Chưa có dữ liệu hôm nay. Có thể sync chưa chạy hoặc thực sự chưa có đơn.",
        ])
        return text

    sec_snapshot = (
        f"   Tổng đơn:      {fmt_int(orders)}\n"
        f"   💰 Doanh thu:   {fmt_money(net)}\n"
        f"   📊 Chi phí QC:  {fmt_money(ads)}\n"
        f"   {'💵 Lợi nhuận:   ' if pl >= 0 else '🔻 Lỗ:          '}{fmt_money(pl)}"
    )

    sec_top = "\n".join(
        f"   {i+1}. {name[:30]:<32} {fmt_int(oc):>4} đơn — {fmt_money(rev)}"
        for i, (name, oc, rev, _) in enumerate(top_rev)
    ) if top_rev else "   (chưa có shop có doanh thu)"

    parts = [
        fmt_header("Báo cáo cuối ngày", today.strftime("%d/%m/%Y")),
        "",
        "(Snapshot 20:00 — số chốt cuối cùng sẽ ở báo cáo sáng mai 07:00)",
        "",
        section("HÔM NAY TỚI THỜI ĐIỂM HIỆN TẠI", sec_snapshot),
        "",
        section("TOP 5 SHOP DOANH THU", sec_top),
    ]
    if warning_shops:
        sec_warn = "\n".join(
            f"   • {name[:28]:<30} DT {fmt_money(rev)} | QC {fmt_money(ads_)} | "
            f"{'LN' if pl_ >= 0 else 'Lỗ'} {fmt_money(pl_)}"
            for name, rev, ads_, pl_ in warning_shops
        )
        parts.append("")
        parts.append(section(f"⚠️ SHOP CẦN CHÚ Ý ({len(warning_shops)})", sec_warn))
    parts.append(fmt_footer("Báo cáo chốt: 07:00 sáng mai"))
    return "\n".join(parts)


def main() -> int:
    try:
        text = build_report()
    except Exception as exc:
        log.exception("daily_top_report build error")
        print(f"[daily_top_report] build error: {exc}", file=sys.stderr)
        return 1
    if "--dry-run" in sys.argv:
        print(text)
        return 0
    ok, fail = safe_send(text, audience="default")
    print(f"[daily_top_report] ok={ok} fail={fail}")
    return 0 if (ok > 0 or fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
