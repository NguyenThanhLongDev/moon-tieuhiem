#!/usr/bin/env python3
"""
Báo cáo đơn hàng hôm nay (rewrite 2026-05-14, Phase 2).

Sections:
  1) TỔNG QUAN ĐƠN HÔM NAY (real-time) — từ wh_outbound_requests
       confirmed / pre_confirmed (đã chuẩn bị) / pending (chờ chuyển) / cancelled
  2) DOANH THU SO VỚI HÔM QUA (real-time, sync liên tục)
       Hôm nay vs Hôm qua → đơn + doanh thu net + chênh lệch %
  3) TOP 5 SHOP HÔM NAY theo doanh thu

CLI:
  python3 send_order_report.py            # mặc định hôm nay
  python3 send_order_report.py yesterday  # hôm qua
  python3 send_order_report.py 2026-05-14
  python3 send_order_report.py --dry-run  # in ra stdout, không gửi Telegram
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
    """Parse `python script.py [date]` → date object."""
    raw = next((a for a in args if not a.startswith("--")), "").strip().lower()
    today = now_hcm().date()
    if not raw:
        return today
    if raw in {"yesterday", "hom qua", "hôm qua"}:
        return today - timedelta(days=1)
    if raw in {"today", "hom nay", "hôm nay"}:
        return today
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d%m%Y"):
        try:
            return _dt.strptime(raw, fmt).date()
        except Exception:
            pass
    try:
        d = _dt.strptime(raw, "%d/%m").date()
        return d.replace(year=today.year)
    except Exception:
        pass
    raise SystemExit(f"Không hiểu ngày '{raw}'. Ví dụ: 2026-05-14, 14/05/2026, yesterday")


def build_report(target: date) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # ─── 1) Status breakdown đơn theo order_inserted_at ───
            cur.execute(
                """
                SELECT status, pancake_status, COUNT(*)::int AS c
                FROM wh_outbound_requests
                WHERE order_inserted_at::date = %s
                GROUP BY 1, 2
                """,
                (target,),
            )
            buckets: dict[tuple[str, str], int] = {}
            for s, ps, c in cur.fetchall():
                buckets[(s or "", ps or "")] = c

            # Map status combos → bucket
            confirmed = buckets.get(("confirmed", "shipped"), 0) + buckets.get(("confirmed", "received"), 0)
            prepared  = buckets.get(("pre_confirmed", "waiting"), 0)
            shipping  = buckets.get(("pre_confirmed", "shipped"), 0)
            pending   = buckets.get(("pending", "waiting"), 0)
            cancelled = buckets.get(("pending", "cancelled"), 0) + buckets.get(("auto_cleaned", "cancelled"), 0)
            lech_pos  = buckets.get(("pending", "shipped"), 0) + buckets.get(("pending", "received"), 0)
            total_orders = sum(buckets.values())

            # ─── 2) Doanh thu hôm nay vs hôm qua (daily_shop_metrics) ───
            yesterday = target - timedelta(days=1)
            cur.execute(
                """
                SELECT metric_date,
                       SUM(order_count)::bigint   AS orders,
                       SUM(net_revenue)::numeric  AS net_rev
                FROM daily_shop_metrics
                WHERE metric_date IN (%s, %s)
                GROUP BY metric_date
                """,
                (target, yesterday),
            )
            rev_by_date = {r[0]: (int(r[1] or 0), float(r[2] or 0)) for r in cur.fetchall()}
            today_orders, today_rev = rev_by_date.get(target,    (0, 0.0))
            y_orders,     y_rev     = rev_by_date.get(yesterday, (0, 0.0))

            # ─── 3) Top 5 shop hôm nay ───
            cur.execute(
                """
                SELECT s.shop_name, m.order_count, m.net_revenue
                FROM daily_shop_metrics m
                JOIN shops s ON s.id = m.shop_id
                WHERE m.metric_date = %s AND m.net_revenue > 0
                ORDER BY m.net_revenue DESC
                LIMIT 5
                """,
                (target,),
            )
            top_shops = [(r[0] or "?", int(r[1] or 0), float(r[2] or 0)) for r in cur.fetchall()]

    # ─── Build message ───
    sec_orders = (
        f"   Tổng đơn:           {fmt_int(total_orders)}\n"
        f"   ✅ Đã xuất kho:     {fmt_int(confirmed)}  ({fmt_pct(confirmed, total_orders)})\n"
        f"   📦 Đã chuẩn bị:     {fmt_int(prepared)}\n"
        f"   🚚 Chờ xuất kho:    {fmt_int(shipping)}\n"
        f"   ⏳ Chờ chuyển hàng: {fmt_int(pending)}  (NV chưa quét)"
    )
    if lech_pos:
        sec_orders += f"\n   ⚠️ Lệch POS:        {fmt_int(lech_pos)}  (POS lấy, kho chưa quét)"
    if cancelled:
        sec_orders += f"\n   ❌ Đã hủy:          {fmt_int(cancelled)}"

    # Doanh thu so sánh
    delta_orders = today_orders - y_orders
    delta_rev = today_rev - y_rev
    arrow_o = "▲" if delta_orders > 0 else ("▼" if delta_orders < 0 else "•")
    arrow_r = "▲" if delta_rev > 0 else ("▼" if delta_rev < 0 else "•")
    sec_rev = (
        f"   Hôm nay:  {fmt_int(today_orders)} đơn — {fmt_money(today_rev)}\n"
        f"   Hôm qua:  {fmt_int(y_orders)} đơn — {fmt_money(y_rev)}\n"
        f"   {arrow_o} Đơn:      {'+' if delta_orders >= 0 else ''}{fmt_int(delta_orders)}\n"
        f"   {arrow_r} Doanh thu: {'+' if delta_rev >= 0 else ''}{fmt_money(delta_rev)}"
    )

    if top_shops:
        top_body = "\n".join(
            f"   {i+1}. {name[:30]:<32} {fmt_int(oc):>4} đơn — {fmt_money(rev)}"
            for i, (name, oc, rev) in enumerate(top_shops)
        )
    else:
        top_body = "   Chưa có shop nào có doanh thu hôm nay."

    period_label = "hôm nay " + target.strftime("%d/%m/%Y") if target == now_hcm().date() else target.strftime("%d/%m/%Y")
    lines = [
        fmt_header("Báo cáo đơn hàng", period_label),
        "",
        section("TỔNG QUAN ĐƠN HÔM NAY (real-time)", sec_orders),
        "",
        section("DOANH THU SO VỚI HÔM QUA", sec_rev),
        "",
        section("TOP 5 SHOP HÔM NAY (theo doanh thu)", top_body),
        fmt_footer("Chi tiết: /kho-vat-ly/outbound"),
    ]
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    try:
        target = parse_target_date(args)
        text = build_report(target)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("order_report build error")
        print(f"[order_report] build error: {exc}", file=sys.stderr)
        return 1
    if dry:
        print(text)
        return 0
    ok, fail = safe_send(text, audience="default")
    print(f"[order_report] target={target} ok={ok} fail={fail}")
    return 0 if (ok > 0 or fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
