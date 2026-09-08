#!/usr/bin/env bash
# ============================================================
# bootstrap_sync_returns.sh
#
# Chạy 1 lần sau khi cài web mới để sync đầy đủ dữ liệu hoàn:
#   - Đang hoàn (Pancake status=4)
#   - Đã hoàn   (Pancake status=5)
#   - Chờ hoàn  (counted in shop_order_status_cache)
#
# Script này an toàn để chạy lại (idempotent).
# Sau lần đầu, APScheduler tự chạy lại mỗi 2 giờ (sync_wh_returns_pg.py)
# và mỗi 15 phút (sync_order_status_cache qua run_auto_refresh_all_data.sh).
#
# Cách dùng:
#   DATABASE_URL="postgresql://..." bash scripts/bootstrap_sync_returns.sh
# ============================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="$BASE_DIR/logs"
LOG_FILE="$LOG_DIR/bootstrap_sync_returns_$(date +%F_%H%M).log"
TODAY="$(date +%F)"

mkdir -p "$LOG_DIR"

echo "=== BOOTSTRAP SYNC RETURNS — $(date '+%F %T') ===" | tee "$LOG_FILE"
echo "BASE_DIR=$BASE_DIR" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Kiểm tra DATABASE_URL
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL chưa được set." | tee -a "$LOG_FILE"
  echo "  Chạy: export DATABASE_URL='postgresql://user:pass@host:5432/dbname'" | tee -a "$LOG_FILE"
  exit 1
fi

cd "$BASE_DIR"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy 2>/dev/null || true
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

# ── Bước 1: Sync shop_order_status_cache ─────────────────────────────────────
# Đây là nguồn dữ liệu cho dashboard KPI: đang hoàn, đã hoàn, chờ hoàn
echo "[1/2] Sync order status cache (đang/đã/chờ hoàn → dashboard)..." | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_order_status_cache.py \
  --date "$TODAY" --days 7 2>&1 | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# ── Bước 2: Sync + backfill wh_return_receipts ───────────────────────────────
# - Cập nhật pancake_return_status cho đơn đã có
# - Tạo mới phiếu cho đơn chưa có (backfill tự động)
# - Xoá trạng thái đơn không còn status 4/5
echo "[2/2] Sync + backfill wh_return_receipts từ Pancake..." | tee -a "$LOG_FILE"
DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" scripts/sync_wh_returns_pg.py 2>&1 | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

echo "=== BOOTSTRAP SYNC RETURNS HOÀN TẤT — $(date '+%F %T') ===" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE"
