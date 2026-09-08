#!/usr/bin/env python3
"""Kéo số liệu POS (Pancake) THEO TỪNG PAGE để đối chiếu với CP Ads FB bên mình.

Pancake endpoint: POST /api/v1/shops/{pos_shop_id}/analytics/sale
  split_by = ["Order.source"]  → trả MỖI PAGE 1 dòng:
    - field `account`              = page_id (facebook)
    - success.ads_amount           = chi phí QC (POS) của page
    - success.revenue / profit     = doanh thu / lợi nhuận
    - success.order_count          = đơn chốt
    - returned.order_count         = đơn hoàn
(Chú ý: split_by=["Order.page_id"] trả ads_amount=0, KHÔNG dùng được cho ad cost.)

Lưu vào pos_page_daily_metrics (UNIQUE pos_shop_id,page_id,metric_date) — idempotent.
Gọi THEO TỪNG NGÀY để UI cộng được mọi khoảng ngày.

Usage:
  python scripts/sync_pos_page_metrics.py --shop-ids 714953348,1942326560 --date-from 2026-05-01 --date-to 2026-05-26
  python scripts/sync_pos_page_metrics.py --user-id 568 --date-from 2026-05-26 --date-to 2026-05-26
"""
from __future__ import annotations
import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn
from pancake_auth import get_shop_api_key
from sync_pos import build_payload, utc_bounds_for_vn_calendar_day

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_pos_page_metrics")

POS_BASE = "https://pos.pancake.vn/api/v1/shops"


def _daterange(d_from: str, d_to: str) -> List[str]:
    a = datetime.strptime(d_from, "%Y-%m-%d").date()
    b = datetime.strptime(d_to, "%Y-%m-%d").date()
    if b < a:
        a, b = b, a
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _shops_for_user(user_id: int) -> List[tuple]:
    """[(pos_shop_id, our_shop_id), ...] cho 1 NV (shop active có pancake_shop_id)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.pancake_shop_id, s.id
              FROM user_shop_assignments usa
              JOIN shops s ON s.id = usa.shop_id
             WHERE usa.user_id = %s AND usa.assigned_to IS NULL AND s.status = 'active'
               AND s.pancake_shop_id IS NOT NULL AND s.pancake_shop_id <> ''
            """,
            (user_id,),
        )
        return [(str(r[0]), r[1]) for r in cur.fetchall()]


def _all_active_shops() -> List[tuple]:
    """[(pos_shop_id, our_shop_id), ...] cho MỌI shop active có pancake_shop_id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT pancake_shop_id, id
              FROM shops
             WHERE status = 'active'
               AND pancake_shop_id IS NOT NULL AND pancake_shop_id <> ''
            """
        )
        return [(str(r[0]), r[1]) for r in cur.fetchall()]


def _our_shop_id_for_pos(pos_shop_id: str) -> Optional[int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM shops WHERE pancake_shop_id = %s LIMIT 1", (str(pos_shop_id),))
        row = cur.fetchone()
        return int(row[0]) if row else None


def _fetch_pos_page_names(pos_shop_id: str, api_key: str) -> Dict[str, str]:
    """{page_id: page_name} lấy từ chi tiết shop POS (shop.pages[])."""
    url = f"{POS_BASE}/{pos_shop_id}"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params={"api_key": api_key}, headers=headers, timeout=30)
        r.raise_for_status()
        shop = (r.json() or {}).get("shop") or {}
        out: Dict[str, str] = {}
        for pg in shop.get("pages") or []:
            pid = str(pg.get("id") or "").strip()
            nm = (pg.get("name") or "").strip()
            if pid and nm:
                out[pid] = nm
        return out
    except Exception as exc:
        logger.warning("  %s: không lấy được tên page POS (%s)", pos_shop_id, str(exc)[:100])
        return {}


def _fetch_pos_pages(pos_shop_id: str, api_key: str, day: str) -> List[dict]:
    """Gọi POS analytics/sale split_by Order.source cho 1 ngày → list page rows."""
    since, until = utc_bounds_for_vn_calendar_day(day)
    payload = build_payload(since, until)
    payload["params"]["split_by"] = ["Order.source"]
    url = f"{POS_BASE}/{pos_shop_id}/analytics/sale"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{pos_shop_id}/statistic/analytic/Revenue",
        "User-Agent": "Mozilla/5.0",
    }
    r = requests.post(url, params={"api_key": api_key}, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    d = r.json()
    if d.get("success") is not True:
        logger.warning("  %s %s: API báo lỗi %s", pos_shop_id, day, d.get("message"))
        return []
    return d.get("data") or []


def _upsert(rows: List[tuple]) -> int:
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO pos_page_daily_metrics
                    (pos_shop_id, shop_id, page_id, metric_date,
                     ads_amount, revenue, profit, success_order_count, returned_order_count, page_name, sales, capital, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (pos_shop_id, page_id, metric_date) DO UPDATE SET
                    shop_id              = EXCLUDED.shop_id,
                    ads_amount           = EXCLUDED.ads_amount,
                    revenue              = EXCLUDED.revenue,
                    profit               = EXCLUDED.profit,
                    success_order_count  = EXCLUDED.success_order_count,
                    returned_order_count = EXCLUDED.returned_order_count,
                    page_name            = COALESCE(EXCLUDED.page_name, pos_page_daily_metrics.page_name),
                    sales                = EXCLUDED.sales,
                    capital              = EXCLUDED.capital,
                    updated_at           = NOW()
                """,
                r,
            )
        conn.commit()
    return len(rows)


def sync_shop(pos_shop_id: str, our_shop_id: Optional[int], days: List[str]) -> int:
    api_key = get_shop_api_key(str(pos_shop_id))
    if not api_key:
        logger.warning("Shop %s: KHÔNG có api_key — bỏ qua", pos_shop_id)
        return 0
    if our_shop_id is None:
        our_shop_id = _our_shop_id_for_pos(pos_shop_id)
    name_map = _fetch_pos_page_names(str(pos_shop_id), api_key)
    total = 0
    for day in days:
        try:
            data = _fetch_pos_pages(pos_shop_id, api_key, day)
        except Exception as exc:
            logger.warning("  %s %s: lỗi gọi POS %s", pos_shop_id, day, str(exc)[:120])
            continue
        rows: List[tuple] = []
        for item in data:
            page_id = str(item.get("account") or "").strip()
            if not page_id.isdigit():
                continue  # bỏ dòng không phải page (account != page_id)
            succ = item.get("success") or {}
            # Đơn hoàn nằm trong block "result" (returned_order_count), KHÔNG phải "returned"
            # (top-level "returned" thường = None). Sửa 2026-08-04.
            res = item.get("result") or {}
            rows.append((
                str(pos_shop_id), our_shop_id, page_id, day,
                float(succ.get("ads_amount") or 0),
                float(succ.get("revenue") or 0),
                float(succ.get("profit") or 0),
                int(succ.get("order_count") or 0),
                int(res.get("returned_order_count") or 0),
                name_map.get(page_id),
                float(succ.get("sales") or succ.get("price") or 0),
                float(succ.get("capital") or 0),
            ))
        n = _upsert(rows)
        total += n
        if n:
            logger.info("  %s %s → %d page", pos_shop_id, day, n)
        # RECONCILE: Pancake gán lại chi phí theo thời gian (retroactive) → 1 page có thể
        # RỚT khỏi response cho (shop, ngày) này. Upsert ở trên chỉ đụng page Pancake TRẢ VỀ,
        # nên số cũ của page đã rớt sẽ KẸT lại mãi → báo cáo hiện CP QC POS/lợi nhuận ảo.
        # Zero các page có trong DB nhưng KHÔNG nằm trong response. Giữ row (không DELETE) để
        # giữ page_name + báo cáo hiện 0₫ nhất quán.
        # GUARD: chỉ chạy khi có >=1 page hợp lệ — tránh wipe oan toàn shop khi API rỗng/lỗi.
        returned_pids = [r[2] for r in rows]
        if returned_pids:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pos_page_daily_metrics
                       SET ads_amount=0, revenue=0, profit=0,
                           success_order_count=0, returned_order_count=0, updated_at=NOW()
                     WHERE pos_shop_id=%s AND metric_date=%s
                       AND page_id <> ALL(%s)
                       AND (ads_amount<>0 OR revenue<>0 OR profit<>0
                            OR success_order_count<>0 OR returned_order_count<>0)
                    """,
                    (str(pos_shop_id), day, returned_pids),
                )
                if cur.rowcount:
                    logger.info("  %s %s ↺ reconcile %d page POS bỏ trả về → 0",
                                pos_shop_id, day, cur.rowcount)
                conn.commit()
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop-ids", default="", help="Danh sách pancake_shop_id, phẩy ngăn cách")
    ap.add_argument("--user-id", type=int, default=0, help="Kéo cho mọi shop active của 1 NV")
    ap.add_argument("--all", action="store_true", help="Kéo cho MỌI shop active có pancake_shop_id")
    ap.add_argument("--days-back", type=int, default=0, help="Kéo N ngày gần nhất tính từ hôm nay (ghi đè --date-from)")
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    args = ap.parse_args()

    today = date.today()
    if args.days_back and args.days_back > 0:
        d_from = (today - timedelta(days=args.days_back - 1)).isoformat()
        d_to = today.isoformat()
    else:
        d_from = args.date_from or today.isoformat()
        d_to = args.date_to or today.isoformat()
    days = _daterange(d_from, d_to)

    targets: List[tuple] = []  # (pos_shop_id, our_shop_id)
    if args.all:
        targets = _all_active_shops()
    if args.user_id:
        targets += _shops_for_user(args.user_id)
    if args.shop_ids:
        for sid in args.shop_ids.split(","):
            sid = sid.strip()
            if sid:
                targets.append((sid, None))
    # dedupe theo pos_shop_id
    seen = set(); uniq = []
    for pid, oid in targets:
        if pid not in seen:
            seen.add(pid); uniq.append((pid, oid))
    targets = uniq
    if not targets:
        logger.error("Cần --all hoặc --shop-ids hoặc --user-id")
        sys.exit(1)

    logger.info("Kéo POS theo page: %d shop × %d ngày (%s → %s)", len(targets), len(days), d_from, d_to)
    grand = 0
    for pos_shop_id, our_shop_id in targets:
        grand += sync_shop(pos_shop_id, our_shop_id, days)
    logger.info("=== DONE: upsert %d dòng page-day ===", grand)


if __name__ == "__main__":
    main()
