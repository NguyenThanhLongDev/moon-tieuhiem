#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_URL="${DATABASE_URL:-postgresql://localhost:5432/pos_dashboard}"
FB_TOKEN="${FACEBOOK_ACCESS_TOKEN:-}"
# Đồng bộ với run_auto_refresh: cửa sổ dài để JSON/DB không mất tháng cũ. Có thể hạ SYNC_DAYS nếu mỗi lần chạy quá lâu.
SYNC_DAYS="${SYNC_DAYS:-150}"

# Every 15 minutes: refresh today data (revenue/orders/ads/inventory).
LINE="*/15 * * * * cd \"$BASE_DIR\" && DATABASE_URL=\"$DB_URL\" FACEBOOK_ACCESS_TOKEN=\"$FB_TOKEN\" SYNC_DAYS=\"$SYNC_DAYS\" bash ./run_auto_refresh_all_data.sh \"\$(date +\\%F)\" >> logs/auto_refresh_cron.log 2>&1"

TMP_FILE="$(mktemp)"
crontab -l 2>/dev/null > "$TMP_FILE" || true

grep -v "run_auto_refresh_all_data.sh" "$TMP_FILE" > "${TMP_FILE}.clean" || true
mv "${TMP_FILE}.clean" "$TMP_FILE"

echo "$LINE" >> "$TMP_FILE"
crontab "$TMP_FILE"
rm -f "$TMP_FILE"

echo "Installed auto-refresh cron:"
crontab -l | grep "run_auto_refresh_all_data.sh" || true
