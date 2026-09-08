#!/usr/bin/env python3
"""
Báo cáo tổng kết 4 NGÀY TRƯỚC (rewrite 2026-05-14, Phase 2 extension).

Chạy 09:00 mỗi sáng. Target = today - 4 ngày.
Sau 4 ngày, NV kế toán đã ghi gần như đủ CP QC POS → số liệu chốt chính xác hơn
so với báo cáo tổng kết hôm qua (07:30).

Khác với daily_summary:
  - Target ngày T-4 (đã chốt hoàn toàn)
  - LIỆT KÊ TẤT CẢ SHOP (kể cả shop doanh thu 0) — không chỉ top 5

CLI:
  python3 send_4day_summary.py            # mặc định T-4
  python3 send_4day_summary.py 2026-05-10
  python3 send_4day_summary.py --dry-run
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

from db import get_conn
from tz_utils import now_hcm
from telegram_common import (
    fmt_money, fmt_int, fmt_pct, fmt_header, fmt_footer, safe_send, section,
)

log = logging.getLogger(__name__)


def parse_target_date(args: list[str]) -> date:
    raw = next((a for a in args if not a.startswith("--")), "").strip().lower()
    today = now_hcm().date()
    default = today - timedelta(days=4)
    if not raw:
        return default
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d%m%Y"):
        try:
            return _dt.strptime(raw, fmt).date()
        except Exception:
            pass
    raise SystemExit(f"Không hiểu ngày '{raw}'. Ví dụ: 2026-05-10, 10/05/2026")


def build_report(target: date) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Tổng quan
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
                (target,),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0)
            total_orders, orders, gross, net, ads, pl, loss_cnt = row
            total_orders = int(total_orders or 0)
            orders   = int(orders or 0)
            gross    = float(gross or 0)
            net      = float(net or 0)
            ads      = float(ads or 0)
            pl       = float(pl or 0)
            loss_cnt = int(loss_cnt or 0)

            # Đơn hủy / hoàn
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE pancake_status = 'cancelled')::int,
                    COUNT(*) FILTER (WHERE pancake_status IN ('returned','returning'))::int
                FROM wh_outbound_requests
                WHERE order_inserted_at::date = %s
                """,
                (target,),
            )
            r2 = cur.fetchone() or (0, 0)
            cancelled = int(r2[0] or 0)
            returned  = int(r2[1] or 0)
            pending_unknown = max(0, total_orders - orders - cancelled - returned)

            # CP QC FB API
            cur.execute(
                """
                SELECT COALESCE(SUM(spend), 0)
                FROM fb_ads_page_daily_spend
                WHERE metric_date = %s
                """,
                (target,),
            )
            ads_fb = float(cur.fetchone()[0] or 0) * 1.113

            # Liệt kê TẤT CẢ shop active — kể cả shop có doanh thu = 0
            cur.execute(
                """
                SELECT s.shop_name,
                       COALESCE(m.order_count, 0)::int       AS oc,
                       COALESCE(m.net_revenue, 0)::numeric   AS rev,
                       COALESCE(m.pos_profit_loss, 0)::numeric AS pl
                FROM shops s
                LEFT JOIN daily_shop_metrics m
                       ON m.shop_id = s.id AND m.metric_date = %s
                WHERE COALESCE(s.status, 'active') = 'active'
                ORDER BY rev DESC, s.shop_name ASC
                """,
                (target,),
            )
            all_shops = [(r[0] or "?", int(r[1] or 0), float(r[2] or 0), float(r[3] or 0))
                         for r in cur.fetchall()]

    if total_orders == 0 and not all_shops:
        return "\n".join([
            fmt_header("Tổng kết 4 ngày trước", target.strftime("%d/%m/%Y")),
            "",
            "⚠️ Chưa có dữ liệu cho ngày này.",
        ])

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
        f"   🎯 CP TB / đơn:       {fmt_money(cp_per_order)}\n"
        f"   {'💵 Lợi nhuận:         ' if pl >= 0 else '🔻 Lỗ:                '}{fmt_money(pl)}   (theo CP QC POS)"
    )
    if loss_cnt > 0:
        sec_overview += f"\n   🚨 Shop bị lỗ: {loss_cnt}"

    # All shops: name left-aligned 30 chars, đơn 5w, doanh thu, lợi nhuận
    rows = []
    for i, (name, oc, rev, pl_) in enumerate(all_shops, 1):
        rows.append(
            f"   {i:>3}. {name[:28]:<30} {fmt_int(oc):>4} đơn — {fmt_money(rev)}"
        )
    sec_all = "\n".join(rows)

    parts = [
        fmt_header("Tổng kết 4 ngày trước", target.strftime("%d/%m/%Y")),
        "(Sau 4 ngày: CP QC POS đã được kế toán chốt đầy đủ)",
        "",
        section("TỔNG QUAN", sec_overview),
        "",
        section(f"DOANH THU TẤT CẢ SHOP ({len(all_shops)})", sec_all),
        fmt_footer("Chi tiết: /reports/daily"),
    ]
    return "\n".join(parts)


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    try:
        target = parse_target_date(args)
        text = build_report(target)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("4day_summary build error")
        print(f"[4day_summary] build error: {exc}", file=sys.stderr)
        return 1
    if dry:
        print(text)
        return 0
    ok, fail = safe_send(text, audience="default")
    print(f"[4day_summary] target={target} ok={ok} fail={fail}")
    return 0 if (ok > 0 or fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
