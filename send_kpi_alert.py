#!/usr/bin/env python3
"""
Cảnh báo Telegram — báo cáo TUẦN/THÁNG: shop lỗ + so sánh kỳ trước.

Usage:
  python send_kpi_alert.py --period week    # Tuần (Mon→Sun trước) so với tuần trước nữa
  python send_kpi_alert.py --period month   # Tháng trước so với tháng trước nữa

Chế độ test (gửi cá nhân, không broadcast):
  TEST_CHAT_ID=7714595589 python send_kpi_alert.py --period week

Dry-run (in ra terminal, không gửi):
  python send_kpi_alert.py --period week --dry-run

Data: daily_shop_metrics (loss_flag, pos_profit_loss, net_revenue, ads_cost,
      confirmed_count, total_order_count) × user_shop_assignments.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import requests

from db import get_conn
from telegram_notify import broadcast_send_text, split_message, load_config


TOP_N = 10  # Số NV hiển thị TOP

# Ignore list: NV vừa được giao shop, chưa đủ data 1 tuần đầy đủ.
# Khi NV có shop từ N ngày qua < 1/2 tuần, so sánh sẽ không công bằng.
# Format: user_id → (lý do, ngày được giao). Sau ngày đó >= 7 ngày thì
# có thể xóa khỏi list (script tự bỏ qua khi cur_from > được_giao + 7).
EXCLUDE_USERS = {
    # NV: Võ Minh Đạt (user_id=13796) — mới giao shop "Minh Đạt ĐN TH" từ 06/06/2026
    # (NV cũ kenleader nghỉ 31/05). Tự động skip cho đến khi NV đã có >= 7 ngày data.
    13796: ("Mới giao shop", date(2026, 6, 6)),
}


def is_user_excluded(user_id: int, cur_from: date) -> bool:
    """True nếu NV nên bị loại khỏi báo cáo kỳ này (mới giao shop chưa đủ data)."""
    entry = EXCLUDE_USERS.get(int(user_id))
    if not entry:
        return False
    _, assigned_from = entry
    # Nếu kỳ báo cáo bắt đầu trước ngày giao → NV chưa có data → loại
    return cur_from < assigned_from


# ───────────────────── DATE RANGE HELPERS ─────────────────────

def get_week_ranges(today: date) -> Tuple[Tuple[date, date], Tuple[date, date]]:
    """
    Trả về ((cur_from, cur_to), (prev_from, prev_to)):
      - cur:  Mon→Sun của tuần TRƯỚC tuần hiện tại
      - prev: Mon→Sun của tuần TRƯỚC NỮA (2 tuần trước)
    """
    days_since_monday = today.weekday()           # Mon=0..Sun=6
    this_monday = today - timedelta(days=days_since_monday)
    cur_from = this_monday - timedelta(days=7)
    cur_to   = this_monday - timedelta(days=1)
    prev_from = cur_from - timedelta(days=7)
    prev_to   = cur_from - timedelta(days=1)
    return (cur_from, cur_to), (prev_from, prev_to)


def get_month_ranges(today: date) -> Tuple[Tuple[date, date], Tuple[date, date]]:
    """
    Trả về ((cur_from, cur_to), (prev_from, prev_to)):
      - cur:  tháng trước
      - prev: tháng trước nữa (2 tháng trước)
    """
    first_this_month = today.replace(day=1)
    cur_to = first_this_month - timedelta(days=1)
    cur_from = cur_to.replace(day=1)
    prev_to = cur_from - timedelta(days=1)
    prev_from = prev_to.replace(day=1)
    return (cur_from, cur_to), (prev_from, prev_to)


# ───────────────────── DB QUERIES ─────────────────────

def query_overall(conn, date_from: date, date_to: date) -> dict:
    """Tổng quan toàn hệ thống trong khoảng (chỉ shop active)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(m.total_order_count), 0)       AS orders,
                   COALESCE(SUM(m.net_revenue), 0)             AS rev,
                   COALESCE(SUM(m.ads_cost), 0)                AS ads,
                   COALESCE(SUM(m.pos_profit_loss), 0)         AS profit,
                   COUNT(DISTINCT m.shop_id)                   AS n_shops_active,
                   COUNT(DISTINCT m.shop_id) FILTER (WHERE m.loss_flag) AS n_shops_with_loss
              FROM daily_shop_metrics m
              JOIN shops s ON s.id = m.shop_id
             WHERE s.status='active'
               AND m.metric_date BETWEEN %s AND %s
        """, (date_from, date_to))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone()))


def query_top_shops_loss_with_compare(conn, cur_from: date, cur_to: date,
                                       prev_from: date, prev_to: date,
                                       limit: int) -> List[dict]:
    """TOP shop THỰC SỰ LỖ (profit<0) tuần này, kèm số liệu tuần trước để so sánh."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH cur_w AS (
                SELECT shop_id,
                       SUM(net_revenue)       AS rev,
                       SUM(pos_profit_loss)   AS profit,
                       SUM(ads_cost)          AS ads,
                       SUM(confirmed_count)   AS confirmed,
                       SUM(total_order_count) AS orders,
                       COUNT(*) FILTER (WHERE loss_flag) AS days_loss
                  FROM daily_shop_metrics
                 WHERE metric_date BETWEEN %s AND %s
                 GROUP BY shop_id
            ),
            prev_w AS (
                SELECT shop_id,
                       SUM(net_revenue)       AS rev,
                       SUM(pos_profit_loss)   AS profit,
                       SUM(ads_cost)          AS ads,
                       SUM(total_order_count) AS orders
                  FROM daily_shop_metrics
                 WHERE metric_date BETWEEN %s AND %s
                 GROUP BY shop_id
            )
            SELECT s.id, s.shop_name,
                   c.rev, c.profit, c.ads, c.confirmed, c.orders, c.days_loss,
                   COALESCE(p.rev, 0)    AS prev_rev,
                   COALESCE(p.profit, 0) AS prev_profit,
                   COALESCE(p.ads, 0)    AS prev_ads,
                   COALESCE(p.orders, 0) AS prev_orders
              FROM shops s
              JOIN cur_w c ON c.shop_id = s.id
              LEFT JOIN prev_w p ON p.shop_id = s.id
             WHERE s.status='active'
               AND c.profit < 0
             ORDER BY c.profit ASC
             LIMIT %s
        """, (cur_from, cur_to, prev_from, prev_to, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def query_top_users_low_rev_with_compare(conn, cur_from: date, cur_to: date,
                                          prev_from: date, prev_to: date,
                                          limit: int) -> List[dict]:
    """TOP NV doanh thu thấp nhất, kèm so sánh kỳ trước.

    Filter:
      - users.status = 'active'
      - users.resigned_at IS NULL OR resigned_at > cur_to (NV còn làm hết kỳ này)
      - role không phải admin/manager/...
      - shop status='active' (loại shop nghỉ)
      - NV có >= ceil(period_days/2) ngày có đơn (loại NV mới giao shop giữa kỳ)
    """
    period_days = (cur_to - cur_from).days + 1
    min_days_with_orders = max(1, (period_days + 1) // 2)  # >= half period

    with conn.cursor() as cur:
        cur.execute("""
            WITH cur_w AS (
                SELECT u.id AS user_id,
                       COUNT(DISTINCT m.shop_id)             AS n_shops,
                       SUM(m.net_revenue)                    AS rev,
                       SUM(m.pos_profit_loss)                AS profit,
                       SUM(m.ads_cost)                       AS ads,
                       SUM(m.confirmed_count)                AS confirmed,
                       SUM(m.total_order_count)              AS orders,
                       COUNT(*) FILTER (WHERE m.loss_flag)   AS days_loss,
                       COUNT(DISTINCT m.metric_date) FILTER (WHERE m.total_order_count > 0) AS days_with_orders
                  FROM users u
                  JOIN user_shop_assignments usa ON usa.user_id = u.id
                  JOIN shops s ON s.id = usa.shop_id AND s.status='active'
                  JOIN daily_shop_metrics m ON m.shop_id = s.id
                       AND m.metric_date::date >= usa.assigned_from
                       AND (usa.assigned_to IS NULL OR m.metric_date::date <= usa.assigned_to)
                 WHERE u.status='active'
                   AND (u.resigned_at IS NULL OR u.resigned_at > %s)
                   AND u.role::text NOT IN ('admin','manager','accountant','ketoan','it')
                   AND m.metric_date BETWEEN %s AND %s
                 GROUP BY u.id
                HAVING COUNT(DISTINCT m.metric_date) FILTER (WHERE m.total_order_count > 0) >= %s
            ),
            prev_w AS (
                SELECT u.id AS user_id,
                       SUM(m.net_revenue)         AS rev,
                       SUM(m.pos_profit_loss)     AS profit,
                       SUM(m.total_order_count)   AS orders
                  FROM users u
                  JOIN user_shop_assignments usa ON usa.user_id = u.id
                  JOIN shops s ON s.id = usa.shop_id AND s.status='active'
                  JOIN daily_shop_metrics m ON m.shop_id = s.id
                       AND m.metric_date::date >= usa.assigned_from
                       AND (usa.assigned_to IS NULL OR m.metric_date::date <= usa.assigned_to)
                 WHERE u.status='active'
                   AND m.metric_date BETWEEN %s AND %s
                 GROUP BY u.id
            )
            SELECT u.id, u.username, u.full_name,
                   c.n_shops, c.rev, c.profit, c.ads,
                   c.confirmed, c.orders, c.days_loss, c.days_with_orders,
                   COALESCE(p.rev, 0)    AS prev_rev,
                   COALESCE(p.profit, 0) AS prev_profit,
                   COALESCE(p.orders, 0) AS prev_orders
              FROM users u
              JOIN cur_w c ON c.user_id = u.id
              LEFT JOIN prev_w p ON p.user_id = u.id
             ORDER BY c.rev ASC, c.orders ASC
             LIMIT %s
        """, (cur_to, cur_from, cur_to, min_days_with_orders, prev_from, prev_to, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def query_shop_nv(conn, shop_id: int) -> Optional[Tuple[str, str]]:
    """NV phụ trách shop. Trả về (username, full_name)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT u.username, u.full_name
              FROM user_shop_assignments usa
              JOIN users u ON u.id = usa.user_id
             WHERE usa.shop_id = %s
               AND usa.assigned_to IS NULL
               AND u.status = 'active'
               AND u.role::text NOT IN ('admin','manager','accountant','ketoan','it')
             ORDER BY usa.created_at DESC
             LIMIT 1
        """, (shop_id,))
        return cur.fetchone()


def nv_display_name(row) -> str:
    """Trả về tên đầy đủ NV, fallback username nếu không có full_name."""
    if row is None:
        return "(chưa gán)"
    full = (row[1] or "").strip()
    user = (row[0] or "").strip()
    return full if full else (user or "(chưa gán)")


# ───────────────────── FORMATTERS ─────────────────────

def fmt_vnd(n) -> str:
    """Format số tiền VND có dấu: '+12,345,000đ' hoặc '-5,000đ'."""
    return f"{float(n or 0):+,.0f}đ"


def fmt_vnd_abs(n) -> str:
    """Format số tiền VND không dấu: '12,345,000đ'."""
    return f"{float(n or 0):,.0f}đ"


def fmt_compact(n) -> str:
    """Format gọn: 1.3tr, 589k, -728k."""
    v = float(n or 0)
    sign = "-" if v < 0 else ""
    av = abs(v)
    if av >= 1_000_000:
        return f"{sign}{av/1_000_000:.1f}tr"
    if av >= 1_000:
        return f"{sign}{av/1_000:.0f}k"
    return f"{sign}{av:.0f}đ"


def fmt_delta(cur_v, prev_v) -> str:
    """Format delta % giữa 2 giá trị: '▲ +12%' / '▼ -45%' / '(mới)' / '(không có)'."""
    cur_v = float(cur_v or 0)
    prev_v = float(prev_v or 0)
    if prev_v == 0:
        if cur_v == 0:
            return "(không phát sinh)"
        return "(mới phát sinh)"
    pct = (cur_v - prev_v) / abs(prev_v) * 100
    arrow = "▲" if cur_v > prev_v else ("▼" if cur_v < prev_v else "─")
    return f"{arrow} {pct:+.0f}%"


def fmt_delta_with_prev(cur_v, prev_v) -> str:
    """Delta % + giá trị kỳ trước: '▲ +12% (kỳ trước 1,500,000đ)'."""
    delta = fmt_delta(cur_v, prev_v)
    prev_v = float(prev_v or 0)
    if prev_v == 0:
        return delta
    return f"{delta} (kỳ trước {fmt_vnd_abs(prev_v)})"


# ───────────────────── REPORT BUILDER ─────────────────────

def build_report(period_label: str,
                 cur_from: date, cur_to: date,
                 prev_from: date, prev_to: date) -> str:
    """Tạo report text với so sánh kỳ trước."""
    lines = []
    lines.append(f"📊 BÁO CÁO {period_label.upper()}")
    lines.append(f"📅 Kỳ: {cur_from.strftime('%d/%m')} → {cur_to.strftime('%d/%m/%Y')}")
    lines.append(f"🆚 So với: {prev_from.strftime('%d/%m')} → {prev_to.strftime('%d/%m/%Y')}")
    lines.append("")

    with get_conn() as conn:
        cur_o = query_overall(conn, cur_from, cur_to)
        prev_o = query_overall(conn, prev_from, prev_to)

        # ── Tổng quan ──
        lines.append("▶ TỔNG QUAN")
        lines.append(
            f"Đơn:       {int(cur_o['orders']):,}  "
            f"{fmt_delta(cur_o['orders'], prev_o['orders'])}"
        )
        lines.append(
            f"Doanh thu: {fmt_vnd_abs(cur_o['rev'])}  "
            f"{fmt_delta(cur_o['rev'], prev_o['rev'])}"
        )
        lines.append(
            f"Ads:       {fmt_vnd_abs(cur_o['ads'])}  "
            f"{fmt_delta(cur_o['ads'], prev_o['ads'])}"
        )
        lines.append(
            f"Lợi nhuận: {fmt_vnd(cur_o['profit'])}  "
            f"{fmt_delta(cur_o['profit'], prev_o['profit'])}"
        )
        lines.append(
            f"Shop lỗ:   {cur_o['n_shops_with_loss']}/{cur_o['n_shops_active']} shop"
        )
        lines.append("")

        # ────────── TOP NHÂN VIÊN DOANH THU THẤP ──────────
        # Fetch dư + filter:
        #   - EXCLUDE_USERS (NV mới giao shop, chưa đủ data)
        #   - Loại NV mà doanh thu kỳ này TĂNG so kỳ trước (không phải vấn đề)
        #     Chỉ giữ NV: doanh thu giảm HOẶC mới phát sinh (kỳ trước = 0)
        users_raw = query_top_users_low_rev_with_compare(
            conn, cur_from, cur_to, prev_from, prev_to, TOP_N * 3
        )

        def is_revenue_problem(u: dict) -> bool:
            """True nếu NV có doanh thu giảm hoặc kỳ trước = 0 (mới phát sinh)."""
            rev = float(u["rev"] or 0)
            prev_rev = float(u["prev_rev"] or 0)
            if prev_rev == 0:
                return True  # mới phát sinh — giữ để check
            return rev < prev_rev  # giảm (strict, không phải tăng dù 1 đồng)

        users = [
            u for u in users_raw
            if not is_user_excluded(u["id"], cur_from) and is_revenue_problem(u)
        ]
        users = users[:TOP_N]
        if not users:
            lines.append("✅ Không có nhân viên doanh thu thấp trong kỳ.")
            return "\n".join(lines).rstrip()

        lines.append(f"👤 TOP {len(users)} NHÂN VIÊN DOANH THU THẤP")
        lines.append("")
        for i, u in enumerate(users, 1):
            name = (u["full_name"] or u["username"] or "").strip()
            rev = float(u["rev"] or 0)
            prev_rev = float(u["prev_rev"] or 0)
            orders = int(u["orders"] or 0)
            confirmed = int(u["confirmed"] or 0)
            rate = (confirmed / orders * 100.0) if orders else 0.0
            days_loss = u["days_loss"]

            lines.append(f"{i}. {name}")
            lines.append(
                f"   💰 Doanh thu: {fmt_vnd_abs(rev)}  {fmt_delta_with_prev(rev, prev_rev)}"
            )
            lines.append(
                f"   📦 Đơn: {confirmed}/{orders} ({rate:.0f}% chốt) · {days_loss} ngày lỗ"
            )
            lines.append("")

    return "\n".join(lines).rstrip()


# ───────────────────── SEND ─────────────────────

def send_message(text: str) -> None:
    """Send to TEST_CHAT_ID nếu có, otherwise broadcast."""
    test_chat = os.environ.get("TEST_CHAT_ID", "").strip()
    if test_chat:
        cfg = load_config()
        token = (cfg.get("telegram_bot_token") or "").strip()
        if not token:
            print("ERROR: Thiếu telegram_bot_token (config.json)", file=sys.stderr)
            sys.exit(1)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for part in split_message(text):
            r = requests.post(url, data={"chat_id": test_chat, "text": part},
                              timeout=30)
            if not r.ok:
                print(f"Telegram error chat_id={test_chat}: {r.text}",
                      file=sys.stderr)
        print(f"✅ Sent TEST to chat_id={test_chat}")
    else:
        ok, fail = broadcast_send_text(text)
        print(f"✅ Broadcast: ok={ok} fail={fail}")


# ───────────────────── MAIN ─────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["week", "month"], required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="In ra nội dung, không gửi")
    args = ap.parse_args()

    today = date.today()
    if args.period == "week":
        (cur_from, cur_to), (prev_from, prev_to) = get_week_ranges(today)
        period_label = "tuần"
    else:
        (cur_from, cur_to), (prev_from, prev_to) = get_month_ranges(today)
        period_label = "tháng"

    text = build_report(period_label, cur_from, cur_to, prev_from, prev_to)

    if args.dry_run:
        print(text)
        return

    send_message(text)


if __name__ == "__main__":
    main()
