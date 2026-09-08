#!/usr/bin/env python3
"""Marketing/Leader Brain — Cảnh báo ads + ghi thẻ việc cho leader.

2 loại cảnh báo:
1. dot_0don — ad spend ≥ ngưỡng trong D-3..D-2 (2 ngày chín) mà 0 đơn POS,
   CHỈ tính ads NGOÀI page test (test có hạn mức riêng ở Túi test).
2. hoan_cao — ad có ≥10 đơn trong D-21..D-7 mà tỷ lệ hoàn/hủy ≥25%.

Mỗi cảnh báo: (a) upsert vào lb_alert_log thành THẺ VIỆC trên /leader-brain
(leader bấm Đã xử lý/Giữ lại/Chuyển NV — tính điểm kỷ luật); (b) bắn Telegram.
Idempotent: UNIQUE (alert_date, alert_type, ad_id) — chạy lại không nhân đôi,
không đè trạng thái leader đã bấm.

Usage: python scripts/mb_alert_burning_ads.py [--dry-run] [--threshold 300000]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import get_conn  # noqa: E402
from tz_utils import now_hcm  # noqa: E402
from telegram_common import fmt_money, safe_send  # noqa: E402

ADS_COST_MULTIPLIER = 1.113

_NV_TEAM_SQL = """
    LEFT JOIN LATERAL (
        SELECT us.id AS nv_id, COALESCE(NULLIF(us.full_name,''), us.username) AS nv_name,
               us.team_id
        FROM user_ad_account_assignments ua
        JOIN users us ON us.id = ua.user_id
        WHERE ua.ad_account_id = s.account_id AND ua.assigned_to IS NULL
        LIMIT 1
    ) nv ON true
"""


def find_burning_ads(cur, threshold_vnd: float):
    """dot_0don: spend D-8..D-7 ≥ ngưỡng, 0 đơn THỰC, NGOÀI page test.

    Cửa sổ D-8..D-7 (không phải D-3..D-2): attribution Pancake chín ~80-90% ở
    tuổi 7-8 ngày, vs ~50% ở tuổi 2-3 ngày — quét sớm hơn là phạt oan 75-85%
    (audit 12/06, sếp duyệt). Đồng bộ với widget cảnh báo trên /marketing-brain."""
    today = now_hcm().date()
    d_from = (today - timedelta(days=8)).isoformat()
    d_to = (today - timedelta(days=7)).isoformat()
    cur.execute(f"""
        WITH lbl AS (
            SELECT DISTINCT ON (page_id) page_id, phan_loai
            FROM fb_page_auto_phan_loai ORDER BY page_id, date DESC
        ),
        spend2d AS (
            SELECT e.ad_id, MAX(e.campaign_name) AS campaign_name,
                   MAX(e.account_id) AS account_id, MAX(NULLIF(e.page_id,'')) AS page_id,
                   SUM(e.spend) AS spend
            FROM mb_fb_entity_daily e
            WHERE e.metric_date BETWEEN %s AND %s
            GROUP BY e.ad_id
            HAVING SUM(e.spend) >= %s
        ),
        orders2d AS (
            -- chỉ đếm đơn THỰC: ad mà toàn bộ đơn bị hủy vẫn phải bị nêu tên
            SELECT a.ad_id, COUNT(*) AS orders
            FROM mb_order_attribution a
            JOIN orders o ON o.id = a.order_id
                AND o.order_status NOT IN ('cancelled', 'returned', 'returning')
            WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s
            GROUP BY a.ad_id
        )
        SELECT s.ad_id, s.campaign_name, s.spend,
               COALESCE(pg.page_name, '') AS page_name,
               nv.nv_id, nv.nv_name, nv.team_id
        FROM spend2d s
        LEFT JOIN orders2d o ON o.ad_id = s.ad_id
        LEFT JOIN lbl l ON l.page_id = s.page_id
        LEFT JOIN LATERAL (
            SELECT page_name FROM fb_ads_page_daily_spend p
            WHERE p.page_id = s.page_id AND p.page_name <> '' LIMIT 1
        ) pg ON true
        {_NV_TEAM_SQL}
        WHERE COALESCE(o.orders, 0) = 0
          AND COALESCE(l.phan_loai, 'ma_win') <> 'ma_test'
        ORDER BY s.spend DESC
        LIMIT 20
    """, (d_from, d_to, threshold_vnd, d_from, d_to))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for r in rows:
        spend_vat = float(r["spend"]) * ADS_COST_MULTIPLIER
        out.append({
            "alert_type": "dot_0don", "ad_id": r["ad_id"],
            "campaign_name": r["campaign_name"] or "", "page_name": r["page_name"] or "",
            "spend": float(r["spend"]),
            "detail": f"đốt {fmt_money(round(spend_vat))} (đã VAT) / 2 ngày {d_from}→{d_to} · 0 đơn POS · ngoài page test",
            "team_id": r["team_id"], "nv_user_id": r["nv_id"], "nv_name": r["nv_name"] or "",
        })
    return today.isoformat(), out


def find_high_return_ads(cur, min_orders=10, min_ratio=0.25):
    """hoan_cao: đơn D-21..D-7 (đủ thời gian hoàn về), hoàn/hủy ≥25%, ≥10 đơn."""
    today = now_hcm().date()
    d_from = (today - timedelta(days=21)).isoformat()
    d_to = (today - timedelta(days=7)).isoformat()
    cur.execute(f"""
        WITH od AS (
            -- bad loại đơn cancelled 0đ ảo (gần nửa số bad là 0đ — phồng tỷ lệ, audit 12/06)
            SELECT a.ad_id, MAX(a.page_name) AS page_name,
                   COUNT(*) AS orders,
                   COUNT(*) FILTER (WHERE o.order_status IN ('cancelled','returned','returning')
                                    AND NOT (o.order_status = 'cancelled' AND o.net_revenue = 0)) AS bad
            FROM mb_order_attribution a
            JOIN orders o ON o.id = a.order_id
            WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s
            GROUP BY a.ad_id
            HAVING COUNT(*) >= %s
               AND COUNT(*) FILTER (WHERE o.order_status IN ('cancelled','returned','returning')
                                    AND NOT (o.order_status = 'cancelled' AND o.net_revenue = 0))::float
                   / COUNT(*) >= %s
        )
        SELECT od.*, s.campaign_name, s.account_id, nv.nv_id, nv.nv_name, nv.team_id
        FROM od
        LEFT JOIN LATERAL (
            SELECT MAX(e.campaign_name) AS campaign_name, MAX(e.account_id) AS account_id
            FROM mb_fb_entity_daily e WHERE e.ad_id = od.ad_id
        ) s ON true
        {_NV_TEAM_SQL}
        ORDER BY od.bad DESC
        LIMIT 15
    """, (d_from, d_to, min_orders, min_ratio))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for r in rows:
        pct = round(100.0 * r["bad"] / r["orders"], 0)
        out.append({
            "alert_type": "hoan_cao", "ad_id": r["ad_id"],
            "campaign_name": r["campaign_name"] or "", "page_name": r["page_name"] or "",
            "spend": 0.0,
            "detail": f"{r['orders']} đơn ({d_from}→{d_to}), {r['bad']} hoàn/hủy ({pct:.0f}%) · kiểm tra SP/tư vấn",
            "team_id": r["team_id"], "nv_user_id": r["nv_id"], "nv_name": r["nv_name"] or "",
        })
    return out


def auto_close_unfair_cards(cur) -> int:
    """Tự đóng thẻ dot_0don OAN: đơn về sau khi attribution chín (sếp duyệt 12/06).

    Thẻ open mà ad đã có đơn thực trong cửa sổ alert_date-8..alert_date-1 (phủ cả
    cửa sổ cũ D-3..D-2 lẫn mới D-8..D-7) → status='auto_closed', không tính kỷ luật."""
    cur.execute("""
        UPDATE lb_alert_log
        SET status = 'auto_closed', acted_by_name = 'hệ thống',
            acted_at = NOW(),
            note = 'tự đóng: đơn đã về sau khi Pancake gắn mã ads (thẻ phát khi data chưa chín)'
        WHERE status = 'open' AND alert_type = 'dot_0don'
          AND EXISTS (
              SELECT 1 FROM mb_order_attribution a
              JOIN orders o ON o.id = a.order_id
                  AND o.order_status NOT IN ('cancelled', 'returned', 'returning')
              WHERE a.ad_id = lb_alert_log.ad_id
                AND a.order_date BETWEEN lb_alert_log.alert_date - 8
                                     AND lb_alert_log.alert_date - 1
          )
    """)
    return cur.rowcount


def upsert_alerts(cur, alert_date: str, alerts) -> int:
    """Ghi thẻ việc — KHÔNG đè thẻ đã tồn tại (giữ trạng thái leader đã bấm)."""
    n = 0
    for a in alerts:
        cur.execute("""
            INSERT INTO lb_alert_log
                (alert_date, alert_type, ad_id, campaign_name, page_name,
                 spend, detail, team_id, nv_user_id, nv_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alert_date, alert_type, ad_id) DO NOTHING
        """, (alert_date, a["alert_type"], a["ad_id"], a["campaign_name"][:512],
              a["page_name"][:255], a["spend"], a["detail"],
              a["team_id"], a["nv_user_id"], a["nv_name"][:255]))
        n += cur.rowcount
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=300_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with get_conn() as conn:
        with conn.cursor() as cur:
            alert_date, burning = find_burning_ads(cur, args.threshold)
            returns = find_high_return_ads(cur)
            if not args.dry_run:
                closed = auto_close_unfair_cards(cur)
                new_cards = upsert_alerts(cur, alert_date, burning + returns)
                conn.commit()
                if closed:
                    print(f"[mb_alert] auto_closed {closed} thẻ oan (đơn đã về sau khi chín)")
            else:
                new_cards = len(burning) + len(returns)

    if not burning and not returns:
        print(f"[mb_alert] {alert_date}: sạch — không có ad đốt tiền/hoàn cao.")
        return

    now = now_hcm()
    lines = [
        "🔥 THẺ VIỆC ADS HÔM NAY",
        f"⏰ {now.strftime('%d/%m/%Y %H:%M')}",
        "─" * 28,
    ]
    if burning:
        lines.append(f"⛔ ĐỐT TIỀN 0 ĐƠN (ngoài test) — {len(burning)} ad:")
        for i, a in enumerate(burning[:12], 1):
            nv = f" · NV: {a['nv_name']}" if a["nv_name"] else ""
            lines.append(f"{i}. {(a['campaign_name'] or a['ad_id'])[:46]}")
            lines.append(f"   💸 {a['detail'].split(' · ')[0]}{nv}")
    if returns:
        lines.append("─" * 28)
        lines.append(f"📦 HOÀN/HỦY CAO — {len(returns)} ad:")
        for i, a in enumerate(returns[:8], 1):
            nv = f" · NV: {a['nv_name']}" if a["nv_name"] else ""
            lines.append(f"{i}. {(a['campaign_name'] or a['ad_id'])[:46]} — {a['detail'].split(' · ')[0]}{nv}")
    lines.append("─" * 28)
    lines.append("👉 Leader bấm xử lý tại: https://tieuhiem.com/leader-brain/ (tính điểm kỷ luật)")
    text = "\n".join(lines)

    if args.dry_run:
        print(text)
        print(f"\n[dry-run] thẻ việc: {new_cards} (không ghi DB, không gửi)")
        return
    ok, fail = safe_send(text)
    print(f"[mb_alert] cards_new={new_cards} telegram ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
