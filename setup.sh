#!/usr/bin/env bash
# ============================================================
# Setup script — chạy 1 lần trên máy chủ mới sau khi git clone
# ============================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

echo "=== TIỂU HIỆM POS — SETUP ==="
echo ""

# 1. Kiểm tra DATABASE_URL
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "❌  DATABASE_URL chưa được set."
  echo "    Chạy: export DATABASE_URL='postgresql://user:pass@host:5432/dbname'"
  exit 1
fi
echo "✅  DATABASE_URL: OK"

# 2. Cài dependencies
echo ""
echo "[1/4] Cài Python dependencies..."
pip install -r requirements.txt -q
echo "✅  Dependencies: OK"

# 3. Chạy migrations
echo ""
echo "[2/5] Tạo bảng DB (migrations)..."
python3 run_migrations.py
echo "✅  Migrations: OK"

# 4. Bootstrap shops/users/teams vào DB (bắt buộc trước khi sync)
echo ""
echo "[3/5] Nạp shops / users / teams vào DB..."
DATABASE_URL="$DATABASE_URL" python3 scripts/bootstrap_core_admin_from_json.py
echo "✅  Bootstrap core admin: OK"

# 5. Sync dữ liệu lần đầu
echo ""
echo "[4/6] Sync dữ liệu POS lần đầu (có thể mất 2-5 phút)..."
TODAY="$(date +%F)"
DATABASE_URL="$DATABASE_URL" bash run_auto_refresh_all_data.sh "$TODAY"
echo "✅  Sync POS: OK"

# 6. Bootstrap daily metrics từ JSON vào DB
echo ""
echo "[5/6] Nạp daily_shop_metrics vào DB..."
DATABASE_URL="$DATABASE_URL" python3 scripts/bootstrap_daily_shop_metrics_from_json.py
echo "✅  Bootstrap metrics: OK"

# 7. Sync dữ liệu hoàn hàng (đang hoàn / đã hoàn / chờ hoàn)
echo ""
echo "[6/6] Sync dữ liệu hoàn hàng từ Pancake (đang/đã/chờ hoàn)..."
DATABASE_URL="$DATABASE_URL" bash scripts/bootstrap_sync_returns.sh
echo "✅  Sync returns: OK"

echo ""
echo "============================================"
echo "✅  SETUP HOÀN TẤT"
echo "    Khởi động app: python3 web_app.py"
echo "    Hoặc:          PORT=8000 python3 web_app.py"
echo ""
echo "    Lịch sync tự động (APScheduler):"
echo "      • Mỗi 15 phút : Sync POS + order status cache"
echo "      • Mỗi 2 giờ   : Sync đơn hoàn từ Pancake"
echo "============================================"
