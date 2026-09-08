from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from web_app import build_sent_items_data  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Carrier pickup vs sent-in-day metrics (uses same logic as web xuất hàng)."
    )
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--shop-key", default="", help="Optional shop_key filter.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = build_sent_items_data(args.date, args.shop_key.strip() or None)
    print(
        json.dumps(
            {
                "date": data.get("target_date", args.date),
                "total_orders_sent_day_inserted_at": data.get("total_orders", 0),
                "total_carrier_pickup_orders": int(data.get("total_carrier_pickup_orders", 0) or 0),
                "error": data.get("error", ""),
            },
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
