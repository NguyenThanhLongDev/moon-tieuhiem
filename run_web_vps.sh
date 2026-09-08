#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

export WEB_DAILY_DASHBOARD_DB_ENABLED="${WEB_DAILY_DASHBOARD_DB_ENABLED:-1}"
export WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED="${WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED:-1}"
export WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED="${WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED:-1}"
export WEB_HOME_ALERT_CACHE_TTL_SECONDS="${WEB_HOME_ALERT_CACHE_TTL_SECONDS:-120}"
export WEB_APP_DEBUG="${WEB_APP_DEBUG:-0}"
export WEB_PERF_LOG="${WEB_PERF_LOG:-0}"
export WEB_PANCAKE_PARALLEL_WORKERS="${WEB_PANCAKE_PARALLEL_WORKERS:-8}"
export WEB_SENT_ITEMS_DB_ENABLED="${WEB_SENT_ITEMS_DB_ENABLED:-0}"
export WEB_SENT_ITEMS_DB_PICKUP_FETCH="${WEB_SENT_ITEMS_DB_PICKUP_FETCH:-0}"
export WEB_SENT_ITEMS_DB_FALLBACK_API="${WEB_SENT_ITEMS_DB_FALLBACK_API:-1}"
export WEB_SENT_ITEMS_DB_CREATED_LOOKBACK_DAYS="${WEB_SENT_ITEMS_DB_CREATED_LOOKBACK_DAYS:-120}"
export WEB_SHIPPING_SUMMARY_CACHE_TTL_SECONDS="${WEB_SHIPPING_SUMMARY_CACHE_TTL_SECONDS:-120}"
export WEB_SHIPPED_TRUST_CARRIER_PICKUP_ES_WINDOW="${WEB_SHIPPED_TRUST_CARRIER_PICKUP_ES_WINDOW:-0}"
export WEB_SHIPPED_COUNT_LENIENT_MISSING_PICKUP_TS="${WEB_SHIPPED_COUNT_LENIENT_MISSING_PICKUP_TS:-1}"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: Missing DATABASE_URL"
  echo "Example:"
  echo "  export DATABASE_URL='postgresql://posapp:YOUR_PASSWORD@localhost:5432/pos_dashboard'"
  exit 1
fi

if command -v ss >/dev/null 2>&1; then
  PORT_INFO="$(ss -ltnp 2>/dev/null | awk '/:5050 / {print}')"
  if [ -n "$PORT_INFO" ]; then
    echo "ERROR: Port 5050 is already in use"
    echo "$PORT_INFO"
    echo "Stop the old process first, then run again."
    exit 1
  fi
fi

PORT="${PORT:-5050}"

echo "Starting web with:"
echo "  SERVER: gunicorn (1 worker, 4 threads)"
echo "  DATABASE_URL set: yes"
echo "  WEB_DAILY_DASHBOARD_DB_ENABLED=$WEB_DAILY_DASHBOARD_DB_ENABLED"
echo "  WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED=$WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED"
echo "  WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED=$WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED"
echo "  WEB_APP_DEBUG=$WEB_APP_DEBUG"
echo "  WEB_PERF_LOG=$WEB_PERF_LOG"
echo "  WEB_PANCAKE_PARALLEL_WORKERS=$WEB_PANCAKE_PARALLEL_WORKERS"
echo "  WEB_SENT_ITEMS_DB_ENABLED=$WEB_SENT_ITEMS_DB_ENABLED"
echo "  WEB_SENT_ITEMS_DB_PICKUP_FETCH=$WEB_SENT_ITEMS_DB_PICKUP_FETCH"
echo "  WEB_SHIPPING_SUMMARY_CACHE_TTL_SECONDS=$WEB_SHIPPING_SUMMARY_CACHE_TTL_SECONDS"

# Dùng gunicorn thay Flask dev server.
# --workers 5: 5 processes (tăng từ 3 sau sự cố 20/05 webhook Pancake bão làm tắc worker pool)
# --threads 6: 6 threads/worker → 30 concurrent requests (tăng từ 12). RAM ~290MB/proc.
# --timeout 120: request sync POS có thể chạy lâu
# --worker-class gthread: hỗ trợ multi-thread
exec gunicorn \
  --config gunicorn.conf.py \
  --preload \
  --bind "0.0.0.0:${PORT}" \
  --workers 5 \
  --worker-class gthread \
  --threads 6 \
  --timeout 120 \
  --keep-alive 5 \
  --max-requests 200 \
  --max-requests-jitter 30 \
  --access-logfile - \
  --error-logfile - \
  --log-level info \
  web_app:app
