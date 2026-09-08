#!/usr/bin/env bash
# Sync orders + order_items vào Postgres: theo ngày tạo đơn (inserted_at) rồi theo ngày ĐVVC lấy (xuất hàng / WEB_SENT_ITEMS_DB).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DATE="${1:-$(date +%F)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
ENV_FILE="$BASE_DIR/deploy/pos-dashboard.env"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL not set (add to deploy/pos-dashboard.env or export before run)."
  exit 1
fi

cd "$BASE_DIR"
echo "=== run_sync_orders_to_db TARGET_DATE=$TARGET_DATE $(date '+%F %T') ==="

echo "[1/2] sync_orders_order_items_to_db.py --date $TARGET_DATE (inserted_at)"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$TARGET_DATE"

echo "[2/2] sync_orders_order_items_to_db.py --date $TARGET_DATE --update-status carrier_picked_up_at"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$TARGET_DATE" --update-status carrier_picked_up_at

echo "=== run_sync_orders_to_db done ==="
