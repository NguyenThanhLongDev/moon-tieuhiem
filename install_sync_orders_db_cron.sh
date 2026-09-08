#!/usr/bin/env bash
# Thêm một dòng cron chạy sync đơn (inserted_at + carrier_picked_up_at) mỗi ngày.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
# Sau các job tối; đổi CRON_SCHEDULE nếu muốn (vd: "20 6 * * *" sáng VN).
CRON_SCHEDULE="${SYNC_ORDERS_DB_CRON:-25 21 * * *}"

LINE="$CRON_SCHEDULE cd \"$BASE_DIR\" && bash ./run_sync_orders_to_db.sh \"\$(date +\\%F)\" >> logs/sync_orders_db_cron.log 2>&1"

TMP_FILE="$(mktemp)"
crontab -l 2>/dev/null >"$TMP_FILE" || true

grep -v "run_sync_orders_to_db.sh" "$TMP_FILE" >"${TMP_FILE}.clean" || true
mv "${TMP_FILE}.clean" "$TMP_FILE"

echo "$LINE" >>"$TMP_FILE"
crontab "$TMP_FILE"
rm -f "$TMP_FILE"

echo "Installed sync-orders-DB cron ($CRON_SCHEDULE):"
crontab -l | grep "run_sync_orders_to_db.sh" || true
