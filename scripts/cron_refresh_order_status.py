"""
Cron job: re-sync orders for recent days to update order_status from POS API.

Reuses 100% logic from sync_orders_order_items_to_db.py (upsert_order ON CONFLICT
updates order_status automatically). No new logic is introduced.

Usage:
  DATABASE_URL=... python scripts/cron_refresh_order_status.py [--days 14]

Recommended cron (every 3 hours):
  0 */3 * * * cd /home/admin1/posbottieuhiem && source deploy/pos-dashboard.env.example && .venv/bin/python scripts/cron_refresh_order_status.py --days 14 >> logs/cron_refresh_order_status.log 2>&1
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402
from scripts.sync_orders_order_items_to_db import (  # noqa: E402
    fetch_all_orders_for_shop,
    load_active_shops_with_api_keys,
    load_db_shop_map,
    map_order_status,
    parse_pos_datetime,
    replace_order_items,
    to_business_local_datetime,
    upsert_order,
)


def find_dates_needing_refresh(days: int) -> List[str]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT DATE(o.created_at_pos)::text
                FROM orders o
                WHERE o.order_status IN ('new', 'confirmed', 'shipping')
                  AND DATE(o.created_at_pos) >= %s::date
                ORDER BY 1
                """,
                (cutoff,),
            )
            return [str(row[0]) for row in cur.fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-sync recent order statuses from POS API")
    parser.add_argument("--days", type=int, default=14, help="Look back N days for non-terminal orders (default 14)")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true", help="Only show which dates would be refreshed")
    args = parser.parse_args()

    dates = find_dates_needing_refresh(args.days)
    print(f"[cron_refresh_order_status] {datetime.now():%F %T}")
    print(f"  dates_to_refresh={len(dates)} (lookback={args.days} days)")
    if dates:
        print(f"  dates: {', '.join(dates)}")

    if args.dry_run:
        print("  dry-run mode, exiting.")
        return

    total_updated = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            active_shops = load_active_shops_with_api_keys(cur)

            for target_date in dates:
                date_orders = 0
                for shop in active_shops:
                    shop_key = shop["shop_key"]
                    shop_code = shop["shop_code"]
                    db_shop_id = shop["db_id"]
                    api_key = shop["api_key"]

                    orders, err = fetch_all_orders_for_shop(
                        shop_code=shop_code,
                        api_key=api_key,
                        target_date_str=target_date,
                        page_size=args.page_size,
                        max_pages=args.max_pages,
                        update_status="inserted_at",
                    )
                    if err:
                        print(f"  WARN {shop_key} {target_date}: {err}")
                        continue

                    for order in orders:
                        try:
                            created_raw = parse_pos_datetime(order.get("inserted_at") or order.get("created_at"))
                            created_local = to_business_local_datetime(created_raw)
                            if created_local.strftime("%Y-%m-%d") != target_date:
                                continue
                        except Exception:
                            continue
                        upsert_order(cur, db_shop_id=db_shop_id, order=order)
                        date_orders += 1

                total_updated += date_orders
                print(f"  {target_date}: upserted {date_orders} orders")

    print(f"  TOTAL upserted: {total_updated}")
    print(f"[cron_refresh_order_status] done {datetime.now():%F %T}")


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required.")
    main()
