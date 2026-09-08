#!/usr/bin/env bash
# ============================================================
# restart_and_sync.sh
#
# Áp dụng migrations, seed wh_shops + kho vật lý, sync cache
# trạng thái đơn + phiếu hoàn, rồi restart systemd.
#
# Cách dùng (trên VPS, từ thư mục posbottieuhiem):
#   ./restart_and_sync.sh
#
# Biến tùy chọn:
#   CACHE_DAYS=14 ./restart_and_sync.sh        # mặc định 7
#   OUTBOUND_DAYS=60 ./restart_and_sync.sh     # mặc định 30
#   SKIP_RESTART=1 ./restart_and_sync.sh       # chỉ sync, không restart service
#   FORCE_KHO_SYNC=1 ./restart_and_sync.sh     # ép sync kho dù đã có dữ liệu
#   SYSTEMD_UNIT=pos-dashboard.service          # mặc định pos-dashboard.service
# ============================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$BASE_DIR/deploy/pos-dashboard.env"
CACHE_DAYS="${CACHE_DAYS:-7}"
OUTBOUND_DAYS="${OUTBOUND_DAYS:-30}"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-pos-dashboard.service}"
TODAY="$(date +%F)"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy 2>/dev/null || true
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

# Nạp biến môi trường từ file env nếu có
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Kích hoạt virtualenv nếu có
if [[ -f "$BASE_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$BASE_DIR/.venv/bin/activate"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL chưa set (thêm vào deploy/pos-dashboard.env hoặc export trước khi chạy)."
  exit 1
fi

cd "$BASE_DIR"

echo "=== restart_and_sync: $TODAY $(date '+%F %T') ==="

# [1/6] Migrations
echo "[1/6] run_migrations.py — áp dụng migrations còn thiếu"
"$PYTHON_BIN" run_migrations.py

# [2/6] Seed wh_shops
echo "[2/6] bootstrap_wh_shops_from_shops_json.py — seed wh_shops cho Kho vật lý"
"$PYTHON_BIN" scripts/bootstrap_wh_shops_from_shops_json.py

# [3/6] Bootstrap kho vật lý: POS inventory + outbound orders
# Luôn chạy khi wh_outbound_requests trống HOẶC khi FORCE_KHO_SYNC=1
OUTBOUND_COUNT=$("$PYTHON_BIN" -c "
import os, psycopg2
try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM wh_outbound_requests')
    print(cur.fetchone()[0])
    conn.close()
except Exception:
    print(0)
" 2>/dev/null || echo "0")

if [[ "${FORCE_KHO_SYNC:-0}" == "1" ]] || [[ "$OUTBOUND_COUNT" == "0" ]]; then
  echo "[3/6] bootstrap_kho_vat_ly_data.py — sync POS inventory + ${OUTBOUND_DAYS} ngày outbound (${OUTBOUND_COUNT} rows hiện tại)"
  OUTBOUND_DAYS="$OUTBOUND_DAYS" "$PYTHON_BIN" scripts/bootstrap_kho_vat_ly_data.py
else
  echo "[3/6] SKIP: wh_outbound_requests đã có $OUTBOUND_COUNT rows (dùng FORCE_KHO_SYNC=1 để ép chạy lại)"
fi

# [4/6] Sync order status cache (dashboard KPIs: đang hoàn, đã hoàn, chờ hoàn)
echo "[4/6] sync_order_status_cache.py --date $TODAY --days $CACHE_DAYS"
"$PYTHON_BIN" scripts/sync_order_status_cache.py --date "$TODAY" --days "$CACHE_DAYS"

# [5/6] Sync + backfill wh_return_receipts
echo "[5/6] sync_wh_returns_pg.py — sync + backfill đơn hoàn từ Pancake"
"$PYTHON_BIN" scripts/sync_wh_returns_pg.py

# [6/6] Restart service
if [[ -n "${SKIP_RESTART:-}" ]]; then
  echo "[6/6] SKIP_RESTART=1 — bỏ qua systemctl restart"
else
  echo "[6/6] systemctl restart $SYSTEMD_UNIT"
  sudo systemctl restart "$SYSTEMD_UNIT"
  sudo systemctl is-active "$SYSTEMD_UNIT"
fi

echo "=== restart_and_sync hoàn tất $(date '+%F %T') ==="
