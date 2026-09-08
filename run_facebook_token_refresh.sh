#!/usr/bin/env bash
# Định kỳ gia hạn long-lived token trong JSON store (facebook_ads_tokens).
# Cần FACEBOOK_APP_ID + FACEBOOK_APP_SECRET (vd. trong deploy/pos-dashboard.env).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
WITHIN_DAYS="${FB_TOKEN_REFRESH_WITHIN_DAYS:-14}"
ENV_FILE="${FB_TOKEN_REFRESH_ENV_FILE:-$BASE_DIR/deploy/pos-dashboard.env}"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

LOG_FILE="$LOG_DIR/fb_token_refresh.log"
{
  echo "=== FB token refresh-all $(date '+%F %T') within_days=$WITHIN_DAYS env_file=${ENV_FILE} ==="
  if [[ -z "${FACEBOOK_APP_ID:-}" ]] || [[ -z "${FACEBOOK_APP_SECRET:-}" ]]; then
    echo "SKIP: thiếu FACEBOOK_APP_ID hoặc FACEBOOK_APP_SECRET (kiểm tra $ENV_FILE)"
    exit 0
  fi
  "$PYTHON_BIN" -m facebook_ads_tokens refresh-all --within-days "$WITHIN_DAYS"
} >>"$LOG_FILE" 2>&1
