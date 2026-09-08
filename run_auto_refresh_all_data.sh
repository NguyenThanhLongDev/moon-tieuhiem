#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
# Default PYTHON_BIN = venv python (có psycopg2 + deps). System python3 KHÔNG có.
# Bug 2026-05-21: trước đây default = "python3" (system) → sync_pos.py không load
# được api_key ("No module named 'psycopg2'") → toàn bộ shops bị SKIP → auto_refresh
# chạy mỗi 15 phút nhưng KHÔNG sync gì cả → DB stale.
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$BASE_DIR/.venv/bin/python3" ]]; then
    PYTHON_BIN="$BASE_DIR/.venv/bin/python3"
  else
    PYTHON_BIN="python3"
  fi
fi
TARGET_DATE="${1:-$(date +%F)}"
# Rolling window for recent days in JSON (full replace per shop); dashboard date is always merged via sync_pos --date below.
# ~150 days ≈ 5 tháng — đủ để xem lại doanh thu các tháng trước. Giảm SYNC_DAYS nếu cron/POS API chậm.
SYNC_DAYS="${SYNC_DAYS:-150}"
LOG_FILE="$LOG_DIR/auto_refresh_$(date +%F).log"

# Avoid proxy-related failures when calling POS/Facebook APIs.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

echo "=== AUTO REFRESH START $(date '+%F %T') ===" | tee -a "$LOG_FILE"
echo "TARGET_DATE=$TARGET_DATE SYNC_DAYS=$SYNC_DAYS" | tee -a "$LOG_FILE"
# Optional: set SKIP_SYNC_ORDERS_PICKUP_DB=1 nếu chỉ muốn sync inserted_at (không khuyến nghị).

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is not set." | tee -a "$LOG_FILE"
  exit 1
fi

echo "[1/8] Sync POS analytics -> data_shop*.json (rolling window)" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" sync_pos.py --days "$SYNC_DAYS" | tee -a "$LOG_FILE"

echo "[2/8] Merge POS analytics for dashboard date $TARGET_DATE" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" sync_pos.py --date "$TARGET_DATE" | tee -a "$LOG_FILE"

echo "[3/8] Bootstrap daily_shop_metrics from JSON" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/bootstrap_daily_shop_metrics_from_json.py --require-date "$TARGET_DATE" | tee -a "$LOG_FILE"

echo "[3b/8] Sync order status cache (aggregate counts per shop → shop_order_status_cache)" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_order_status_cache.py --date "$TARGET_DATE" --days 2 | tee -a "$LOG_FILE" || \
  echo "WARN: sync_order_status_cache thất bại (không ảnh hưởng dữ liệu doanh thu)" | tee -a "$LOG_FILE"

echo "[4/8] Sync orders + order_items for target date (inserted_at / ngày tạo đơn)" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$TARGET_DATE" | tee -a "$LOG_FILE"

if [[ "${SKIP_SYNC_ORDERS_PICKUP_DB:-0}" != "1" ]]; then
  echo "[5/8] Sync orders by carrier pickup (xuất hàng / DB)" | tee -a "$LOG_FILE"
  DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_orders_order_items_to_db.py --date "$TARGET_DATE" --update-status carrier_picked_up_at | tee -a "$LOG_FILE"
else
  echo "[5/8] SKIP carrier pickup sync (SKIP_SYNC_ORDERS_PICKUP_DB=1)" | tee -a "$LOG_FILE"
fi

echo "[5b/8] Sync POS theo PAGE (tên page POS + doanh thu/lợi nhuận/chốt-hoàn theo page)" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_pos_page_metrics.py --all --date-from "$TARGET_DATE" --date-to "$TARGET_DATE" | tee -a "$LOG_FILE" || \
  echo "WARN: sync_pos_page_metrics thất bại (shop chưa có api_key hợp lệ?)" | tee -a "$LOG_FILE"

if [[ "${SKIP_SYNC_PRODUCTS:-0}" != "1" ]]; then
  echo "[6/8] Sync inventory snapshots (stock json/history)" | tee -a "$LOG_FILE"
  DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" sync_stock_pos.py | tee -a "$LOG_FILE"
else
  echo "[6/8] SKIP inventory/products sync (SKIP_SYNC_PRODUCTS=1)" | tee -a "$LOG_FILE"
fi

echo "[7/8] Sync Facebook Ads (chi phí QC) — token per-page từ Page & Tài khoản" | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" FACEBOOK_ACCESS_TOKEN="${FACEBOOK_ACCESS_TOKEN:-}" \
  "$PYTHON_BIN" scripts/sync_facebook_ads_to_db.py --date "$TARGET_DATE" | tee -a "$LOG_FILE" || \
  echo "WARN: sync FB ads bỏ qua (chưa nhúng token FB hoặc chưa gán TK QC cho page)" | tee -a "$LOG_FILE"

echo "[8/8] Verify key aggregates" | tee -a "$LOG_FILE"
ORDERS_VERIFY="$(DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/verify_orders_api_vs_db.py --date "$TARGET_DATE" --max-pages 80 || true)"
echo "$ORDERS_VERIFY" | tee -a "$LOG_FILE"
FB_VERIFY="$(DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/verify_fb_ads_mapping_aggregate.py --date "$TARGET_DATE" || true)"
echo "$FB_VERIFY" | tee -a "$LOG_FILE"

if echo "$ORDERS_VERIFY" | grep -q "RESULT: PASS" && echo "$FB_VERIFY" | grep -q "RESULT: PASS"; then
  echo "AUTO_REFRESH_RESULT=PASS" | tee -a "$LOG_FILE"
  exit 0
fi

echo "AUTO_REFRESH_RESULT=WARN" | tee -a "$LOG_FILE"
exit 0
