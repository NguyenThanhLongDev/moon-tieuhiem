#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_URL="${DATABASE_URL:-postgresql://localhost:5432/pos_dashboard}"
ALERT="${DB_HEALTHCHECK_TELEGRAM_ALERT:-1}"

FULL_LINE="30 6 * * * cd \"$BASE_DIR\" && DATABASE_URL=\"$DB_URL\" DB_HEALTHCHECK_TELEGRAM_ALERT=$ALERT ./run_daily_db_healthcheck.sh >> logs/db_healthcheck_cron.log 2>&1"
LIGHT_LINE="30 11 * * * cd \"$BASE_DIR\" && DATABASE_URL=\"$DB_URL\" DB_HEALTHCHECK_TELEGRAM_ALERT=$ALERT ./run_daily_db_healthcheck.sh \"\$(date +\\%F)\" \"\$(date -v-1d +\\%F)\" verify-only >> logs/db_healthcheck_cron.log 2>&1"

TMP_FILE="$(mktemp)"
crontab -l 2>/dev/null > "$TMP_FILE" || true

# Remove old entries for this healthcheck script to avoid duplicates.
grep -v "run_daily_db_healthcheck.sh" "$TMP_FILE" > "${TMP_FILE}.clean" || true
mv "${TMP_FILE}.clean" "$TMP_FILE"

{
  echo "$FULL_LINE"
  echo "$LIGHT_LINE"
} >> "$TMP_FILE"

crontab "$TMP_FILE"
rm -f "$TMP_FILE"

echo "Installed DB healthcheck cron jobs:"
crontab -l | grep "run_daily_db_healthcheck.sh" || true
echo
echo "To remove them later:"
echo "  crontab -l | grep -v 'run_daily_db_healthcheck.sh' | crontab -"
