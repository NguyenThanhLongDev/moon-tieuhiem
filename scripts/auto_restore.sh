#!/usr/bin/env bash
# Auto-restore Tiểu Hiềm POS từ Drive snapshot.
#
# Chạy trên server mới (Ubuntu 24.04 sạch). Script sẽ:
#   1. Cài system packages (rclone, postgresql-client, pgbouncer, git, python3.12, redis)
#   2. Tải snapshot mới nhất từ Drive
#   3. Extract project + secrets + system configs
#   4. Restore PostgreSQL từ dump mới nhất
#   5. Cài Python deps (uv / pip)
#   6. Start systemd services
#
# Prerequisites trên server mới:
#   - User `admin1` đã tồn tại (sudo)
#   - Có internet
#   - Có rclone OAuth token (paste vào /home/admin1/.config/rclone/rclone.conf TRƯỚC khi chạy)
#     HOẶC tải snapshot từ máy khác rồi scp lên trước
#
# Usage:
#   bash auto_restore.sh                   # Full auto-restore
#   bash auto_restore.sh --skip-packages   # Bỏ qua bước cài system packages
#   bash auto_restore.sh --dry-run         # Chỉ in kế hoạch

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESTORE_DIR="/tmp/tieuhiem-restore-$$"
PROJECT_ROOT="/home/admin1/tieuhiemsoft"
RCLONE_CONF="/home/admin1/.config/rclone/rclone.conf"

DRY_RUN=0; SKIP_PKG=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --skip-packages) SKIP_PKG=1 ;;
  esac
done

run() {
  echo ">>> $*"
  [[ $DRY_RUN -eq 1 ]] && return 0
  eval "$@"
}

echo "════════════════════════════════════════════════════════════════"
echo "  TIỂU HIỀM AUTO-RESTORE  (Ubuntu 24.04)"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ─── 1) System packages ───────────────────────────────────────────────
if [[ $SKIP_PKG -eq 0 ]]; then
  echo "▶ Bước 1: Cài system packages..."
  run "sudo apt-get update -y"
  run "sudo apt-get install -y \
    rclone postgresql postgresql-client pgbouncer redis-server \
    git python3.12 python3.12-venv python3-pip \
    nginx curl wget unzip"
fi

# ─── 2) Check rclone config ───────────────────────────────────────────
echo ""
echo "▶ Bước 2: Kiểm tra rclone token..."
if [[ ! -f "$RCLONE_CONF" ]]; then
  echo "❌ rclone.conf KHÔNG có. Chọn 1 trong 2 cách:"
  echo "   (A) Trên laptop có browser: chạy 'rclone authorize \"drive\"'"
  echo "       Copy JSON token → tạo file $RCLONE_CONF với nội dung:"
  echo "       [gdrive]"
  echo "       type = drive"
  echo "       scope = drive"
  echo "       token = <JSON>"
  echo "   (B) SCP file rclone.conf từ backup riêng lên trước khi chạy script"
  exit 1
fi
rclone about gdrive: > /dev/null 2>&1 || { echo "❌ Drive connection FAIL"; exit 1; }
echo "✓ Drive OK"

# ─── 3) Download snapshot mới nhất ────────────────────────────────────
echo ""
echo "▶ Bước 3: Tải snapshot mới nhất từ Drive..."
run "mkdir -p $RESTORE_DIR"
LATEST=$(rclone lsf gdrive:tieuhiem-backup/snapshots/ --include "snapshot-*-user.tar.gz" | sort | tail -1)
TS_PART=$(echo "$LATEST" | sed -E 's/snapshot-([0-9]+-[0-9]+)-user.tar.gz/\1/')
echo "  Snapshot ID: $TS_PART"

run "rclone copy gdrive:tieuhiem-backup/snapshots/snapshot-$TS_PART-user.tar.gz   $RESTORE_DIR/"
run "rclone copy gdrive:tieuhiem-backup/snapshots/snapshot-$TS_PART-system.tar.gz $RESTORE_DIR/ || true"
run "rclone copy gdrive:tieuhiem-backup/snapshots/snapshot-$TS_PART-manifest.txt  $RESTORE_DIR/ || true"

# Tải dump DB mới nhất
LATEST_DUMP=$(rclone lsf gdrive:tieuhiem-backup/daily/ --include "tieuhiem-*.dump" | sort | tail -1)
echo "  DB dump:     $LATEST_DUMP"
run "rclone copy gdrive:tieuhiem-backup/daily/$LATEST_DUMP $RESTORE_DIR/"

# ─── 4) Extract user + system files ───────────────────────────────────
echo ""
echo "▶ Bước 4: Extract files..."
run "tar -xzf $RESTORE_DIR/snapshot-$TS_PART-user.tar.gz -C / 2>&1 | tail -20"
if [[ -f "$RESTORE_DIR/snapshot-$TS_PART-system.tar.gz" ]]; then
  run "sudo tar -xzf $RESTORE_DIR/snapshot-$TS_PART-system.tar.gz -C /"
fi
run "sudo systemctl daemon-reload"

# ─── 5) PostgreSQL: tạo DB + user + restore ───────────────────────────
echo ""
echo "▶ Bước 5: Restore PostgreSQL..."
# Đọc DATABASE_URL từ env file
ENV_FILE="$PROJECT_ROOT/posbottieuhiem/deploy/pos-dashboard.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ KHÔNG có $ENV_FILE — không biết DATABASE_URL. Dừng."
  exit 1
fi
DB_URL=$(grep -oP '(?<=^DATABASE_URL=).*' "$ENV_FILE" | head -1)
# Parse user:pass@host:port/dbname
DB_USER=$(echo "$DB_URL" | sed -E 's|.*//([^:]+):.*|\1|')
DB_PASS=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
DB_NAME=$(echo "$DB_URL" | sed -E 's|.*/([^/?]+)(\?.*)?$|\1|')

echo "  User: $DB_USER  DB: $DB_NAME"

run "sudo -u postgres psql -c \"CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';\" || true"
run "sudo -u postgres psql -c \"CREATE DATABASE $DB_NAME OWNER $DB_USER;\" || true"
run "sudo -u postgres psql -c \"GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;\""

run "PGPASSWORD='$DB_PASS' pg_restore -h localhost -p 5432 -U $DB_USER \
  -d $DB_NAME --clean --if-exists --no-owner --no-acl \
  $RESTORE_DIR/$LATEST_DUMP"

# ─── 6) Python venv + deps ────────────────────────────────────────────
echo ""
echo "▶ Bước 6: Cài Python deps..."
run "cd $PROJECT_ROOT/posbottieuhiem && python3.12 -m venv .venv"
if [[ -f "$PROJECT_ROOT/posbottieuhiem/pyproject.toml" ]] && command -v uv >/dev/null; then
  run "cd $PROJECT_ROOT/posbottieuhiem && uv pip install -e ."
else
  run "cd $PROJECT_ROOT/posbottieuhiem && .venv/bin/pip install -r requirements.txt 2>/dev/null || .venv/bin/pip install psycopg2-binary flask gunicorn redis requests apscheduler openpyxl"
fi

# ─── 7) Khởi động services ────────────────────────────────────────────
echo ""
echo "▶ Bước 7: Start services..."
run "sudo systemctl enable --now pgbouncer redis-server pos-dashboard pos-scheduler"
run "sleep 5"
run "sudo systemctl status pos-dashboard --no-pager | head -8"
run "sudo systemctl status pos-scheduler --no-pager | head -8"

# ─── 8) Smoke test ────────────────────────────────────────────────────
echo ""
echo "▶ Bước 8: Smoke test..."
run "curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:5050/login || echo 'web không phản hồi'"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  HOÀN TẤT — kiểm tra https://tieuhiem.com hoặc IP server"
echo "  Snapshot ID: $TS_PART"
echo "  Tmp dir: $RESTORE_DIR (tự xoá sau)"
echo "════════════════════════════════════════════════════════════════"

# Cleanup tmp sau 1 ngày
echo "rm -rf $RESTORE_DIR" | at now + 1 day 2>/dev/null || true
