#!/usr/bin/env bash
# Thêm một dòng cron riêng cho gia hạn token FB (KHÔNG sửa job auto_refresh cũ).
# Mặc định: 03:15 hằng ngày. Đổi lịch: export FB_TOKEN_REFRESH_CRON='15 3 * * 1' rồi chạy lại script.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
# minute hour day month weekday — mặc định mỗi ngày 03:15
CRON_SCHEDULE="${FB_TOKEN_REFRESH_CRON:-15 3 * * *}"

LINE="$CRON_SCHEDULE cd \"$BASE_DIR\" && /bin/bash \"$BASE_DIR/run_facebook_token_refresh.sh\""

TMP_FILE="$(mktemp)"
crontab -l 2>/dev/null >"$TMP_FILE" || true

grep -v "run_facebook_token_refresh.sh" "$TMP_FILE" >"${TMP_FILE}.clean" || true
mv "${TMP_FILE}.clean" "$TMP_FILE"

echo "$LINE" >>"$TMP_FILE"
crontab "$TMP_FILE"
rm -f "$TMP_FILE"

echo "Đã cài cron refresh token FB (một dòng riêng):"
crontab -l | grep "run_facebook_token_refresh.sh" || true
echo ""
echo "Log: $BASE_DIR/logs/fb_token_refresh.log"
echo "Tuỳ chỉnh: FB_TOKEN_REFRESH_WITHIN_DAYS=14 FB_TOKEN_REFRESH_CRON='15 3 * * *' $0"
