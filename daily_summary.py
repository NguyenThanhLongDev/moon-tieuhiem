#!/usr/bin/env python3
"""
Tổng kết HÔM QUA (rewrite 2026-05-14, Phase 2).

Gửi lúc 07:00 mỗi sáng — data hôm qua đã được sync đầy đủ qua đêm.

Sections:
  1) TỔNG QUAN — đơn / doanh thu net / chi phí QC / P&L
  2) TOP 5 SHOP theo doanh thu
  3) TOP 5 SHOP theo lợi nhuận
  4) SHOP BỊ LỖ (nếu có)

Nguồn: `daily_shop_metrics` (PG).
"""
from __future__ import annotations

import logging
import sys
from datetime import timedelta

from db import get_conn
from tz_utils import now_hcm
from telegram_common import (
    fmt_money, fmt_int, fmt_pct, fmt_header, fmt_footer, safe_send, section,
)

log = logging.getLogger(__name__)


def build_report() -> str:
    yesterday = (now_hcm().date()) - timedelta(days=1)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    SUM(total_order_count)::bigint,
                    SUM(order_count)::bigint,
                    SUM(gross_revenue)::numeric,
                    SUM(net_revenue)::numeric,
                    SUM(ads_cost)::numeric,
                    SUM(pos_profit_loss)::numeric,
                    SUM(CASE WHEN loss_flag THEN 1 ELSE 0 END)::int
                FROM daily_shop_metrics
                WHERE metric_date = %s
                """,
                (yesterday,),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0)
            total_orders, orders, gross, net, ads, pl, loss_cnt = row
            total_orders = int(total_orders or 0)   # tổng đơn tạo (POS)
            orders   = int(orders or 0)             # đơn thành công (POS)
            gross    = float(gross or 0)
            net      = float(net or 0)
            ads      = float(ads or 0)              # CP QC POS (NV ghi thủ công)
            pl       = float(pl or 0)
            loss_cnt = int(loss_cnt or 0)

            # Đơn hủy / hoàn từ wh_outbound_requests
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE pancake_status = 'cancelled')::int AS huy,
                    COUNT(*) FILTER (WHERE pancake_status IN ('returned','returning'))::int AS hoan
                FROM wh_outbound_requests
                WHERE order_inserted_at::date = %s
                """,
                (yesterday,),
            )
            r2 = cur.fetchone() or (0, 0)
            cancelled = int(r2[0] or 0)
            returned  = int(r2[1] or 0)
            pending_unknown = max(0, total_orders - orders - cancelled - returned)

            # CP QC từ FB Ads real-time (đã VAT 11.3%)
            cur.execute(
                """
                SELECT COALESCE(SUM(spend), 0)
                FROM fb_ads_page_daily_spend
                WHERE metric_date = %s
                """,
                (yesterday,),
            )
            ads_fb = float(cur.fetchone()[0] or 0) * 1.113

            # Top 5 shop theo doanh thu
            cur.execute(
                """
                SELECT s.shop_name, m.order_count, m.net_revenue, m.pos_profit_loss
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s AND m.net_revenue > 0
                ORDER BY m.net_revenue DESC
                LIMIT 5
                """,
                (yesterday,),
            )
            top_rev = [(r[0] or "?", int(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in cur.fetchall()]

            # Top 5 shop theo lợi nhuận
            cur.execute(
                """
                SELECT s.shop_name, m.net_revenue, m.pos_profit_loss
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s AND m.pos_profit_loss > 0
                ORDER BY m.pos_profit_loss DESC
                LIMIT 5
                """,
                (yesterday,),
            )
            top_profit = [(r[0] or "?", float(r[1] or 0), float(r[2] or 0)) for r in cur.fetchall()]

            # Shop bị lỗ
            cur.execute(
                """
                SELECT s.shop_name, m.net_revenue, m.pos_profit_loss
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s AND m.pos_profit_loss < 0
                ORDER BY m.pos_profit_loss ASC
                LIMIT 10
                """,
                (yesterday,),
            )
            loss_shops = [(r[0] or "?", float(r[1] or 0), float(r[2] or 0)) for r in cur.fetchall()]

    if orders == 0 and gross == 0:
        text = "\n".join([
            fmt_header("Tổng kết hôm qua", yesterday.strftime("%d/%m/%Y")),
            "",
            "⚠️ Chưa có dữ liệu cho ngày này. Có thể chưa sync xong, kiểm tra lại sau.",
        ])
        return text

    cp_per_order = (ads_fb / orders) if orders > 0 else 0
    success_rate = f"({fmt_pct(orders, total_orders)})" if total_orders else ""
    sec_overview = (
        f"   📝 Đơn tạo:           {fmt_int(total_orders)}\n"
        f"   ✅ Đơn thành công:    {fmt_int(orders)}   {success_rate}\n"
        f"   ❌ Đơn hủy:           {fmt_int(cancelled)}\n"
        f"   ↩️ Đơn hoàn:          {fmt_int(returned)}\n"
        f"   ⏳ Đơn chưa xử lý:    {fmt_int(pending_unknown)}\n"
        f"\n"
        f"   💰 Doanh thu:         {fmt_money(net)}\n"
        f"   📊 CP QC (POS):       {fmt_money(ads)}   (NV ghi thủ công)\n"
        f"   📡 CP QC (FB API):    {fmt_money(ads_fb)}   (real-time, đã VAT)\n"
        f"   🎯 CP TB / đơn:       {fmt_money(cp_per_order)}   (FB API ÷ đơn thành công)\n"
        f"   {'💵 Lợi nhuận:         ' if pl >= 0 else '🔻 Lỗ:                '}{fmt_money(pl)}   (theo CP QC POS)"
    )
    if loss_cnt > 0:
        sec_overview += f"\n   🚨 Shop bị lỗ: {loss_cnt}"

    sec_top_rev = "\n".join(
        f"   {i+1}. {name[:30]:<32} {fmt_int(oc):>4} đơn — {fmt_money(rev)}"
        for i, (name, oc, rev, _) in enumerate(top_rev)
    ) if top_rev else "   (không có)"

    sec_top_profit = "\n".join(
        f"   {i+1}. {name[:30]:<32} LN {fmt_money(pl)} / DT {fmt_money(rev)}"
        for i, (name, rev, pl) in enumerate(top_profit)
    ) if top_profit else "   (không có)"

    parts = [
        fmt_header("Tổng kết hôm qua", yesterday.strftime("%d/%m/%Y")),
        "",
        section("TỔNG QUAN", sec_overview),
        "",
        section("TOP 5 DOANH THU", sec_top_rev),
        "",
        section("TOP 5 LỢI NHUẬN", sec_top_profit),
    ]
    if loss_shops:
        sec_loss = "\n".join(
            f"   • {name[:30]:<32} {fmt_money(pl)}  (DT {fmt_money(rev)})"
            for name, rev, pl in loss_shops
        )
        parts.append("")
        parts.append(section(f"🚨 SHOP BỊ LỖ ({len(loss_shops)})", sec_loss))
    parts.append(fmt_footer("Chi tiết: /reports/daily"))
    return "\n".join(parts)


def main() -> int:
    try:
        text = build_report()
    except Exception as exc:
        log.exception("daily_summary build error")
        print(f"[daily_summary] build error: {exc}", file=sys.stderr)
        return 1
    if "--dry-run" in sys.argv:
        print(text)
        return 0
    ok, fail = safe_send(text, audience="default")
    print(f"[daily_summary] ok={ok} fail={fail}")
    return 0 if (ok > 0 or fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
