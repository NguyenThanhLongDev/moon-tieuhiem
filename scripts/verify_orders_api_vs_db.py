from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402
from scripts.sync_orders_order_items_to_db import load_active_shops_with_api_keys  # noqa: E402


def read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify parity API vs DB for orders by shop/day")
    parser.add_argument("--date", default="", help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--shop-key", default="", help="Verify only one shop_key")
    parser.add_argument("--page-size", type=int, default=100, help="API page size")
    parser.add_argument("--max-pages", type=int, default=200, help="Safety page cap")
    parser.add_argument("--show-matches", action="store_true", help="Print matched shops too")
    return parser.parse_args()


def parse_target_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now().strftime("%Y-%m-%d")
    datetime.strptime(text, "%Y-%m-%d")
    return text


def get_day_range_params(target_date_str: str) -> Tuple[int, int, str, str]:
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_local = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_local = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    start_utc = start_local - timedelta(hours=7)
    end_utc = end_local - timedelta(hours=7)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return start_ts, end_ts, start_iso, end_iso


def request_orders_page(
    shop_code: str,
    api_key: str,
    target_date_str: str,
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_code}/orders/get_orders"
    params = [
        ("api_key", api_key),
        ("page_size", page_size),
        ("page", page),
        ("updateStatus", "inserted_at"),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_code}/order",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=45)
        return res.json()
    except Exception as exc:
        return {"error": str(exc)}


def fetch_api_orders(
    shop_code: str,
    api_key: str,
    target_date_str: str,
    page_size: int,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    all_orders: List[Dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        payload = request_orders_page(shop_code, api_key, target_date_str, page, page_size)
        if payload.get("error"):
            return [], str(payload["error"])
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return [], "Invalid API payload"
        all_orders.extend([r for r in rows if isinstance(r, dict)])
        if len(rows) < page_size:
            break
        page += 1
    return all_orders, None


def api_order_metrics(orders: List[Dict[str, Any]]) -> Tuple[int, float]:
    count = 0
    revenue = 0.0
    for order in orders:
        count += 1
        revenue += float(order.get("price", order.get("total_price", 0)) or 0)
    return count, revenue


def db_order_metrics(cur, db_shop_id: int, target_date_str: str) -> Tuple[int, float]:
    cur.execute(
        """
        SELECT COUNT(1), COALESCE(SUM(net_revenue), 0)
        FROM orders
        WHERE shop_id = %s
          AND DATE(created_at_pos) = %s::date
        """,
        (db_shop_id, target_date_str),
    )
    row = cur.fetchone()
    return int(row[0] or 0), float(row[1] or 0)


def main() -> None:
    args = parse_args()
    target_date = parse_target_date(args.date)

    with get_conn() as conn:
        with conn.cursor() as cur:
            active_shops = load_active_shops_with_api_keys(cur)

            if args.shop_key:
                active_shops = [s for s in active_shops if s["shop_key"] == args.shop_key.strip()]

            total_checked = 0
            total_pass = 0
            mismatches: List[Dict[str, Any]] = []
            errors: List[str] = []

            for shop in active_shops:
                shop_key = shop["shop_key"]
                shop_code = shop["shop_code"]
                db_shop_id = shop["db_id"]
                api_key = shop["api_key"]

                api_orders, err = fetch_api_orders(
                    shop_code=shop_code,
                    api_key=api_key,
                    target_date_str=target_date,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                )
                if err:
                    errors.append(f"{shop_key}:api_error={err}")
                    continue

                api_count, api_revenue = api_order_metrics(api_orders)
                db_count, db_revenue = db_order_metrics(cur, db_shop_id=db_shop_id, target_date_str=target_date)
                total_checked += 1

                count_diff = db_count - api_count
                revenue_diff = round(db_revenue - api_revenue, 2)
                count_match = count_diff == 0
                revenue_match = abs(revenue_diff) < 0.01

                if count_match and revenue_match:
                    total_pass += 1
                    if args.show_matches:
                        print(
                            f"PASS {shop_key} | date={target_date} | "
                            f"count api/db={api_count}/{db_count} | revenue api/db={api_revenue:.2f}/{db_revenue:.2f}"
                        )
                else:
                    mismatches.append(
                        {
                            "shop_key": shop_key,
                            "date": target_date,
                            "api_count": api_count,
                            "db_count": db_count,
                            "count_diff": count_diff,
                            "api_revenue": round(api_revenue, 2),
                            "db_revenue": round(db_revenue, 2),
                            "revenue_diff": revenue_diff,
                            "count_match": count_match,
                            "revenue_match": revenue_match,
                        }
                    )

            print("=== Phase B.1 Parity Check (API vs DB) ===")
            print(f"date={target_date}")
            print(f"shops_targeted={len(active_shops)}")
            print(f"shops_checked={total_checked}")
            print(f"pass={total_pass}")
            print(f"fail={len(mismatches)}")
            print(f"errors={len(errors)}")

            if mismatches:
                print("\n--- MISMATCHES ---")
                for m in mismatches:
                    print(
                        f"FAIL {m['shop_key']} | {m['date']} | "
                        f"count api/db={m['api_count']}/{m['db_count']} (diff={m['count_diff']}) | "
                        f"revenue api/db={m['api_revenue']:.2f}/{m['db_revenue']:.2f} (diff={m['revenue_diff']:.2f})"
                    )

            if errors:
                print("\n--- ERRORS ---")
                for e in errors:
                    print(f"ERROR {e}")

            all_pass = len(mismatches) == 0 and len(errors) == 0
            print("\nRESULT:", "PASS" if all_pass else "FAIL")


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required. Example: export DATABASE_URL=postgresql://user:pass@host:5432/db")
    main()
