#!/usr/bin/env bash
# Dời dự án cpqc từ /home/dev3/cpqc -> /home/admin1/cpqc, do admin1 sở hữu.
# Chạy bằng: sudo bash /home/dev3/cpqc/move_to_admin1.sh
set -euo pipefail

SRC=/home/dev3/cpqc
DST=/home/admin1/cpqc

echo "==> 1. Copy code (bỏ .venv cũ + __pycache__ + cache)"
mkdir -p "$DST"
rsync -a --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$SRC"/ "$DST"/

echo "==> 2. Đổi chủ sở hữu sang admin1"
chown -R admin1:admin1 "$DST"

echo "==> 3. Tạo lại venv mới tại $DST/.venv + cài thư viện"
sudo -u admin1 bash -c "
  cd '$DST'
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.runtime.txt
  ./.venv/bin/pip install --quiet bcrypt PyJWT cryptography
"

echo "==> 4. Kiểm tra import nhanh (tạo app OK?)"
sudo -u admin1 bash -c "
  cd '$DST'
  set -a; source deploy/cpqc.env; set +a
  ./.venv/bin/python -c 'import web_app; print(\"IMPORT OK\")'
"

echo ""
echo "✅ XONG. Dự án giờ ở: $DST (chủ: admin1)"
echo "   Chạy app:   cd $DST && bash run_cpqc.sh   (gunicorn :5070)"
echo "   Thư mục cũ /home/dev3/cpqc vẫn còn — xoá tay nếu muốn."
