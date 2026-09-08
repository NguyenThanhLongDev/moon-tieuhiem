#!/usr/bin/env bash
# ============================================================
# pack.sh — Đóng gói TOÀN BỘ Posbot để deploy sang VPS mới
# Chạy trên máy NGUỒN (VPS cũ hoặc Replit):
#   cd ~/tieuhiemsoft/posbottieuhiem
#   bash deploy/pack.sh
#
# Kết quả: ~/posbot_full_YYYYMMDD_HHMM.tar.gz
# Copy sang VPS mới rồi chạy: bash deploy/install.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M)"
PKG_NAME="posbot_full_${STAMP}"
PKG_DIR="/tmp/${PKG_NAME}"
OUT_FILE="${HOME}/${PKG_NAME}.tar.gz"

echo "========================================"
echo "  Posbot Full Packer"
echo "  Nguồn: $APP_DIR"
echo "  Output: $OUT_FILE"
echo "========================================"

# ── Load env để lấy DATABASE_URL ──────────────────────────────
ENV_FILE="$APP_DIR/deploy/pos-dashboard.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "❌ DATABASE_URL chưa set. Source env file trước:"
  echo "   set -a && source deploy/pos-dashboard.env && set +a"
  exit 1
fi

# ── Tạo thư mục đóng gói ──────────────────────────────────────
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR/app" "$PKG_DIR/db"

echo ""
echo "▶ 1/4 Copy source code..."
rsync -a --exclude='.venv' \
         --exclude='__pycache__' \
         --exclude='*.pyc' \
         --exclude='.git' \
         --exclude='logs/*.log' \
         --exclude='exports/*.xlsx' \
         --exclude='stock_history' \
         --exclude='dated_json_backup' \
         --exclude='old' \
         "$APP_DIR/" "$PKG_DIR/app/"

echo "▶ 2/4 Dump PostgreSQL database..."
pg_dump "$DATABASE_URL" \
  --no-owner --no-acl \
  --format=custom \
  --file="$PKG_DIR/db/posbot_db.dump" \
  && echo "   ✓ DB dump xong"

echo "▶ 3/4 Đóng gói các file nhạy cảm..."
# Đảm bảo các file config quan trọng đều có mặt
for f in session.json shops.json users.json config.json; do
  if [ -f "$APP_DIR/$f" ]; then
    cp "$APP_DIR/$f" "$PKG_DIR/app/$f"
    echo "   ✓ $f"
  else
    echo "   ⚠ Thiếu $f — bỏ qua"
  fi
done

# Đảm bảo env file được pack
cp "$ENV_FILE" "$PKG_DIR/app/deploy/pos-dashboard.env"
echo "   ✓ pos-dashboard.env"

# Kèm script install
cp "$APP_DIR/deploy/install.sh" "$PKG_DIR/install.sh"
chmod +x "$PKG_DIR/install.sh"

echo "▶ 4/4 Nén thành tar.gz..."
tar -czf "$OUT_FILE" -C "/tmp" "$PKG_NAME"
rm -rf "$PKG_DIR"

SIZE=$(du -sh "$OUT_FILE" | cut -f1)
echo ""
echo "========================================"
echo "  ✅ XONG! Package: $OUT_FILE ($SIZE)"
echo ""
echo "  Copy sang VPS mới:"
echo "    scp $OUT_FILE admin1@<IP_VPS_MOI>:~/"
echo ""
echo "  Chạy trên VPS mới:"
echo "    tar -xzf ~/${PKG_NAME}.tar.gz -C ~/"
echo "    bash ~/${PKG_NAME}/install.sh"
echo "========================================"
