#!/bin/bash
# ============================================================
# install.sh — Cài đặt Posbot từ đầu, tự động hoàn toàn
# Chạy: bash install.sh
# ============================================================
set -e
cd "$(dirname "$0")"

echo "=== [1/6] Kiểm tra Python + venv ==="
sudo apt-get install -y python3-venv python3-full libpq-dev 2>/dev/null || true
if [ ! -f ".venv/bin/python3" ]; then
    rm -rf .venv
    python3 -m venv .venv
    echo "  → Tạo venv mới"
else
    echo "  → venv đã tồn tại"
fi

echo "=== [2/6] Cài packages ==="
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "  → OK"

echo "=== [3/6] Kiểm tra .env ==="
if [ ! -f ".env" ]; then
    echo "  [LỖI] Chưa có file .env!"
    echo "  Tạo file .env với nội dung:"
    echo "  DATABASE_URL=postgresql://posapp:matkhau@localhost:5432/pos_dashboard"
    exit 1
fi
export $(grep -v '^#' .env | xargs)
echo "  → DATABASE_URL đã load"

echo "=== [4/6] Chạy migrations ==="
.venv/bin/python3 run_migrations.py
echo "  → Migrations xong"

echo "=== [5/6] Tạo thư mục logs ==="
mkdir -p logs

echo "=== [6/6] Khởi động app ==="
# Dừng tiến trình cũ nếu có
pkill -f "python3 web_app.py" 2>/dev/null || true
sleep 1

PORT="${PORT:-5050}"
nohup .venv/bin/python3 web_app.py > logs/app.log 2>&1 &
APP_PID=$!
echo "  → App khởi động PID=$APP_PID port=$PORT"
sleep 3

# Kiểm tra xem app có chạy không
if kill -0 $APP_PID 2>/dev/null; then
    echo ""
    echo "============================================"
    echo "  ✓ App đang chạy tại http://localhost:$PORT"
    echo "  ✓ Xem log: tail -f logs/app.log"
    echo "  ✓ Bootstrap dữ liệu đang chạy trong nền"
    echo "============================================"
else
    echo "  [LỖI] App không khởi động được — xem log:"
    tail -20 logs/app.log
    exit 1
fi
