from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Facebook Ads aggregate by shop/day for multi-account mappings."
    )
    parser.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    parser.add_argument("--shop-key", default="", help="Optional single shop_key to verify")
    parser.add_argument(
        "--allow-missing-metrics",
        action="store_true",
        help="Do not fail when active mappings have no metric row for target date",
    )
    parser.add_argument("--show-passes", action="store_true", help="Print PASS lines per shop")
    return parser.parse_args()


def parse_date(value: str) -> str:
    text = str(value or "").strip()
    datetime.strptime(text, "%Y-%m-%d")
    return text


def load_shop_scope(cur, shop_key: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT id, shop_key, shop_name
        FROM shops
        WHERE status = 'active'
    """
    params: List[Any] = []
    if shop_key:
        sql += " AND shop_key = %s"
        params.append(shop_key)
    sql += " ORDER BY shop_key"
    cur.execute(sql, tuple(params))
    return [
        {"shop_id": int(row[0]), "shop_key": str(row[1] or ""), "shop_name": str(row[2] or "")}
        for row in cur.fetchall()
        if row and row[1]
    ]


def load_active_mapping_count_by_shop(cur, shop_ids: List[int]) -> Dict[int, int]:
    if not shop_ids:
        return {}
    cur.execute(
        """
        SELECT shop_id, COUNT(1)
        FROM fb_ad_account_mappings
        WHERE status = 'active'
          AND shop_id = ANY(%s)
        GROUP BY shop_id
        """,
        (shop_ids,),
    )
    return {int(row[0]): int(row[1] or 0) for row in cur.fetchall()}


def load_metric_account_count_by_shop(cur, shop_ids: List[int], target_date: str) -> Dict[int, int]:
    if not shop_ids:
        return {}
    cur.execute(
        """
        SELECT shop_id, COUNT(DISTINCT fb_ad_account_id)
        FROM fb_ads_daily_metrics
        WHERE metric_date = %s::date
          AND shop_id = ANY(%s)
        GROUP BY shop_id
        """,
        (target_date, shop_ids),
    )
    return {int(row[0]): int(row[1] or 0) for row in cur.fetchall()}


def load_shop_total_spend_by_shop(cur, shop_ids: List[int], target_date: str) -> Dict[int, float]:
    if not shop_ids:
        return {}
    cur.execute(
        """
        SELECT shop_id, COALESCE(SUM(spend), 0)
        FROM fb_ads_daily_metrics
        WHERE metric_date = %s::date
          AND shop_id = ANY(%s)
        GROUP BY shop_id
        """,
        (target_date, shop_ids),
    )
    return {int(row[0]): float(row[1] or 0) for row in cur.fetchall()}


def load_account_rows(cur, shop_id: int, target_date: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT fb_ad_account_id, COALESCE(spend, 0), COALESCE(impressions, 0), COALESCE(clicks, 0)
        FROM fb_ads_daily_metrics
        WHERE shop_id = %s
          AND metric_date = %s::date
        ORDER BY fb_ad_account_id
        """,
        (shop_id, target_date),
    )
    rows = []
    for row in cur.fetchall():
        rows.append(
            {
                "fb_ad_account_id": str(row[0] or "").strip(),
                "spend": float(row[1] or 0),
                "impressions": int(row[2] or 0),
                "clicks": int(row[3] or 0),
            }
        )
    return rows


def main() -> None:
    args = parse_args()
    target_date = parse_date(args.date)
    target_shop_key = str(args.shop_key or "").strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            shops = load_shop_scope(cur, target_shop_key)
            shop_ids = [int(s["shop_id"]) for s in shops]
            mapping_count_map = load_active_mapping_count_by_shop(cur, shop_ids)
            metric_count_map = load_metric_account_count_by_shop(cur, shop_ids, target_date)
            total_spend_map = load_shop_total_spend_by_shop(cur, shop_ids, target_date)

            pass_count = 0
            fail_count = 0
            warn_count = 0
            checked_count = 0

            print("=== FB Ads Mapping Aggregate Verify ===")
            print(f"date={target_date}")
            print(f"shops_targeted={len(shops)}")

            for shop in shops:
                shop_id = int(shop["shop_id"])
                shop_key = str(shop["shop_key"])
                mapped_accounts = int(mapping_count_map.get(shop_id, 0))
                metric_accounts = int(metric_count_map.get(shop_id, 0))
                shop_total = float(total_spend_map.get(shop_id, 0))
                account_rows = load_account_rows(cur, shop_id, target_date)
                recomputed_total = round(sum(float(x["spend"]) for x in account_rows), 2)
                shop_total_rounded = round(shop_total, 2)
                total_match = abs(recomputed_total - shop_total_rounded) < 0.01
                missing_metric = mapped_accounts > metric_accounts
                zero_spend_rows = sum(1 for x in account_rows if abs(float(x["spend"])) < 0.01)

                checked_count += 1
                if mapped_accounts == 0:
                    warn_count += 1
                    print(f"WARN {shop_key} | no active FB mapping")
                    continue

                if missing_metric and not args.allow_missing_metrics:
                    fail_count += 1
                    print(
                        f"FAIL {shop_key} | mapped_accounts={mapped_accounts} metric_accounts={metric_accounts} "
                        f"| reason=missing_metric_rows"
                    )
                    continue

                if not total_match:
                    fail_count += 1
                    print(
                        f"FAIL {shop_key} | mapped_accounts={mapped_accounts} metric_accounts={metric_accounts} "
                        f"| total={shop_total_rounded:.2f} recomputed={recomputed_total:.2f}"
                    )
                    continue

                pass_count += 1
                if args.show_passes:
                    print(
                        f"PASS {shop_key} | mapped_accounts={mapped_accounts} metric_accounts={metric_accounts} "
                        f"| total={shop_total_rounded:.2f} zero_spend_rows={zero_spend_rows}"
                    )

            print(f"shops_checked={checked_count}")
            print(f"pass={pass_count}")
            print(f"fail={fail_count}")
            print(f"warnings={warn_count}")
            all_pass = fail_count == 0
            print("RESULT:", "PASS" if all_pass else "FAIL")


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required. Example: export DATABASE_URL=postgresql://user:pass@host:5432/db")
    main()
