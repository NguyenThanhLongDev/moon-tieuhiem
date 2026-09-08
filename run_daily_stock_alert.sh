#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
THRESHOLD="${LOW_STOCK_DAILY_THRESHOLD:-10}"

# Avoid local proxy intercepting Telegram API calls.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

mkdir -p "$LOG_DIR"

cd "$BASE_DIR"
"$PYTHON_BIN" send_stock_alert.py --mode daily --threshold "$THRESHOLD" >> "$LOG_DIR/daily_stock_alert.log" 2>&1
