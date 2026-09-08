#!/usr/bin/env bash
# ============================================================
# install.sh — Cài đặt Posbot lên VPS mới từ package đã đóng gói
# Chạy trên VPS MỚI sau khi giải nén package:
#   tar -xzf posbot_full_YYYYMMDD_HHMM.tar.gz -C ~/
#   bash ~/posbot_full_YYYYMMDD_HHMM/install.sh
#
# Yêu cầu VPS: Ubuntu 22.04+ / Debian 11+, user có sudo
# ============================================================
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_SRC="$PKG_DIR/app"
DB_DUMP="$PKG_DIR/db/posbot_db.dump"

# ── Cấu hình — chỉnh nếu cần ──────────────────────────────────
INSTALL_DIR="${HOME}/tieuhiemsoft/posbottieuhiem"
VENV_DIR="$INSTALL_DIR/.venv"
SERVICE_NAME="pos-dashboard"
APP_USER="${USER}"
DB_NAME="pos_dashboard"
DB_USER="posapp"
DB_PASS="Posapp_2026_db"
APP_PORT="5050"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
fail() { echo -e "${RED}✗ LỖI:${NC} $*"; exit 1; }
step() { echo -e "\n${YELLOW}▶ $*${NC}"; }

echo "========================================"
echo "  Posbot Full Installer"
echo "  Install dir: $INSTALL_DIR"
echo "  Service: $SERVICE_NAME (port $APP_PORT)"
echo "========================================"

# ── BƯỚC 1: Cài hệ thống ─────────────────────────────────────
step "1/8 Cài Python 3.12, PostgreSQL, công cụ cần thiết..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3.12 python3.12-venv python3.12-dev \
  postgresql postgresql-client \
  libpq-dev build-essential \
  git curl rsync 2>&1 | tail -5
ok "Hệ thống đã cài xong"

# ── BƯỚC 2: Khởi động PostgreSQL ──────────────────────────────
step "2/8 Khởi động PostgreSQL..."
sudo systemctl enable postgresql --now 2>/dev/null || true
sudo systemctl start postgresql 2>/dev/null || true
sleep 2
ok "PostgreSQL đang chạy"

# ── BƯỚC 3: Tạo DB user + database ───────────────────────────
step "3/8 Tạo database '$DB_NAME' và user '$DB_USER'..."
sudo -u postgres psql -c "
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DB_USER') THEN
      CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
    END IF;
  END \$\$;
" 2>/dev/null || true

sudo -u postgres psql -c "
  SELECT 1 FROM pg_database WHERE datname='$DB_NAME'
" | grep -q 1 || \
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
ok "Database '$DB_NAME' sẵn sàng"

# ── BƯỚC 4: Copy source code ──────────────────────────────────
step "4/8 Copy source code vào $INSTALL_DIR..."
mkdir -p "$(dirname "$INSTALL_DIR")"
rsync -a --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  "$APP_SRC/" "$INSTALL_DIR/"
ok "Source code đã copy"

# ── BƯỚC 5: Tạo Python venv + cài packages ───────────────────
step "5/8 Tạo virtualenv và cài Python packages..."
python3.12 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
ok "Python packages đã cài xong"

# ── BƯỚC 6: Restore database ──────────────────────────────────
step "6/8 Restore database từ dump..."
if [ -f "$DB_DUMP" ]; then
  export PGPASSWORD="$DB_PASS"
  pg_restore \
    --host=localhost --port=5432 \
    --username="$DB_USER" --dbname="$DB_NAME" \
    --no-owner --no-acl \
    --clean --if-exists \
    "$DB_DUMP" 2>&1 | grep -v "^pg_restore:" | head -20 || true
  ok "Database restored thành công"
else
  warn "Không tìm thấy $DB_DUMP — bỏ qua restore (DB sẽ tự tạo bảng khi app start)"
fi

# ── BƯỚC 7: Cấu hình env + service ───────────────────────────
step "7/8 Cài systemd service '$SERVICE_NAME'..."

# Đảm bảo DATABASE_URL đúng trong env file
ENV_FILE="$INSTALL_DIR/deploy/pos-dashboard.env"
if grep -q "localhost" "$ENV_FILE" 2>/dev/null; then
  ok "pos-dashboard.env đã có"
else
  cat > "$ENV_FILE" <<ENVEOF
DATABASE_URL=postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME
WEB_DAILY_DASHBOARD_DB_ENABLED=1
WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED=1
WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED=1
WEB_HOME_ALERT_CACHE_TTL_SECONDS=300
WEB_APP_DEBUG=0
WEB_PANCAKE_PARALLEL_WORKERS=8
WEB_DASHBOARD_CARRIER_PICKUP_KPI=1
WEB_SHIPPING_SUMMARY_CACHE_TTL_SECONDS=120
ENVEOF
  ok "Tạo pos-dashboard.env mới"
fi

# Tạo systemd service
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<SVCEOF
[Unit]
Description=POS Dashboard Web (Posbot)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/web_app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
ok "Service '$SERVICE_NAME' đã cài"

# ── BƯỚC 8: Khởi động app + kiểm tra ─────────────────────────
step "8/8 Khởi động app và kiểm tra..."
sudo systemctl restart "$SERVICE_NAME"
sleep 5

if systemctl is-active --quiet "$SERVICE_NAME"; then
  ok "Service đang chạy!"
else
  warn "Service chưa active — xem log: journalctl -u $SERVICE_NAME -n 50"
fi

# Chạy check_data để xác nhận DB
echo ""
echo "  Chạy kiểm tra dữ liệu..."
cd "$INSTALL_DIR"
set -a; source "$ENV_FILE"; set +a
"$VENV_DIR/bin/python3" scripts/check_data.py 2>&1 || warn "check_data có lỗi — xem bên trên"

# Trigger sync lần đầu
step "Bonus: Sync kho outbound + returns lần đầu..."
"$VENV_DIR/bin/python3" - <<'PYEOF' 2>&1 || warn "Sync không thành công — scheduler sẽ tự chạy sau"
import sys, datetime
sys.path.insert(0, '.')
from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range
from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range

d_from = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
d_to   = datetime.date.today().strftime("%Y-%m-%d")

print("  Sync outbound...")
r1 = sync_outbound_for_date_range(d_from, d_to)
print(f"  → outbound: inserted={r1.get('inserted',0)} updated={r1.get('updated',0)}")

print("  Sync returns...")
r2 = sync_returns_for_date_range(d_from, d_to)
print(f"  → returns:  inserted={r2.get('inserted',0)} updated={r2.get('updated',0)}")
PYEOF

echo ""
echo "========================================"
echo "  ✅ CÀI ĐẶT HOÀN THÀNH!"
echo ""
echo "  App chạy tại: http://localhost:$APP_PORT"
echo "  Log:  journalctl -u $SERVICE_NAME -f"
echo "  Stop: sudo systemctl stop $SERVICE_NAME"
echo "  Restart: sudo systemctl restart $SERVICE_NAME"
echo ""
echo "  Nếu dùng Cloudflare Tunnel, xem thêm:"
echo "    $INSTALL_DIR/deploy/cloudflared-tunnel.service"
echo "========================================"
