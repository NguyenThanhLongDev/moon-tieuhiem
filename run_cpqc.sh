#!/usr/bin/env bash
# Chạy dự án CPQC (chi phí quảng cáo) — độc lập với app gốc tieuhiemsoft.
set -euo pipefail
cd "$(dirname "$0")"

# Nạp env
set -a
# shellcheck disable=SC1091
source deploy/cpqc.env
set +a

# venv
# shellcheck disable=SC1091
source .venv/bin/activate

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

PORT="${PORT:-5060}"
echo "Starting CPQC web on :${PORT} (DB=db_cpqc)"

exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --worker-class gthread \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --log-level info \
  web_app:app
