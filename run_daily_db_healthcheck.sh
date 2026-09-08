#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
DB_URL="${DATABASE_URL:-}"
SYNC_DATE="${1:-$(date +%F)}"
VERIFY_DATE="${2:-$(date -v-1d +%F)}"
MODE="${3:-full}" # full | verify-only
LOG_FILE="$LOG_DIR/db_healthcheck_$(date +%F).log"
ENABLE_TELEGRAM_ALERT="${DB_HEALTHCHECK_TELEGRAM_ALERT:-0}"

if [[ -z "$DB_URL" ]]; then
  echo "ERROR: DATABASE_URL is not set."
  echo "Example: export DATABASE_URL=postgresql://localhost:5432/pos_dashboard"
  exit 1
fi

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

send_telegram_alert() {
  local status="$1"
  local message="$2"

  if [[ "$ENABLE_TELEGRAM_ALERT" != "1" ]]; then
    return 0
  fi

  if [[ ! -f "$BASE_DIR/config.json" ]]; then
    return 0
  fi

  token_check="$("$PYTHON_BIN" - <<'PY'
import json
try:
    cfg=json.load(open("config.json","r",encoding="utf-8"))
    t=(cfg.get("telegram_bot_token") or "").strip()
    print("1" if t else "")
except Exception:
    print("")
PY
)"
  if [[ -z "$token_check" ]]; then
    return 0
  fi

  printf '%s' "$message" | "$PYTHON_BIN" "$BASE_DIR/telegram_notify.py" >/dev/null 2>&1 || true
}

echo "=== DB Healthcheck Start $(date '+%F %T') ===" | tee -a "$LOG_FILE"
echo "MODE=$MODE | SYNC_DATE=$SYNC_DATE | VERIFY_DATE=$VERIFY_DATE" | tee -a "$LOG_FILE"

if [[ "$MODE" == "full" ]]; then
  echo "[1/5] Sync orders -> DB (inserted_at)" | tee -a "$LOG_FILE"
  DATABASE_URL="$DB_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$SYNC_DATE" | tee -a "$LOG_FILE"
  echo "[2/5] Sync orders -> DB (carrier_picked_up_at / xuất hàng)" | tee -a "$LOG_FILE"
  DATABASE_URL="$DB_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$SYNC_DATE" --update-status carrier_picked_up_at | tee -a "$LOG_FILE"
else
  echo "[1/5] Skip sync (verify-only mode)" | tee -a "$LOG_FILE"
fi

echo "[3/5] Verify API vs DB parity" | tee -a "$LOG_FILE"
VERIFY_OUTPUT="$(DATABASE_URL="$DB_URL" "$PYTHON_BIN" scripts/verify_orders_api_vs_db.py --date "$VERIFY_DATE")"
echo "$VERIFY_OUTPUT" | tee -a "$LOG_FILE"

echo "[4/5] Verify FB Ads mapping aggregate" | tee -a "$LOG_FILE"
FB_VERIFY_OUTPUT="$(DATABASE_URL="$DB_URL" "$PYTHON_BIN" scripts/verify_fb_ads_mapping_aggregate.py --date "$VERIFY_DATE")"
echo "$FB_VERIFY_OUTPUT" | tee -a "$LOG_FILE"

echo "[5/5] Final status" | tee -a "$LOG_FILE"
if echo "$VERIFY_OUTPUT" | grep -q "RESULT: PASS" && echo "$FB_VERIFY_OUTPUT" | grep -q "RESULT: PASS"; then
  echo "DB_HEALTHCHECK_RESULT=PASS" | tee -a "$LOG_FILE"
  send_telegram_alert "PASS" "✅ DB healthcheck PASS\nSYNC_DATE=${SYNC_DATE}\nVERIFY_DATE=${VERIFY_DATE}\nOrders=PASS\nFB Ads=PASS"
  exit 0
fi

echo "DB_HEALTHCHECK_RESULT=FAIL" | tee -a "$LOG_FILE"
send_telegram_alert "FAIL" "❌ DB healthcheck FAIL\nSYNC_DATE=${SYNC_DATE}\nVERIFY_DATE=${VERIFY_DATE}\nOrders/FB Ads verify failed\nXem log: ${LOG_FILE}"
exit 2
