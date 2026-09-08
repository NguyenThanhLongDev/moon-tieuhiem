#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-5050}"
CLOUDFLARED_BIN="./tools/cloudflared"

if [ ! -x "$CLOUDFLARED_BIN" ]; then
  echo "cloudflared not found. Downloading to ./tools/cloudflared ..."
  mkdir -p ./tools
  curl -fsSL -o "$CLOUDFLARED_BIN" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
  chmod +x "$CLOUDFLARED_BIN"
fi

echo "Opening tunnel for http://localhost:${PORT}"
if [ -n "${CLOUDFLARED_TUNNEL_NAME:-}" ]; then
  echo "Named tunnel mode: CLOUDFLARED_TUNNEL_NAME='${CLOUDFLARED_TUNNEL_NAME}'"
  echo "NOTE: This requires named-tunnel credentials (created via 'cloudflared tunnel login/create')."
  # cloudflared 2026.x: `tunnel run` không nhận cờ --no-autoupdate (quick tunnel `tunnel --url` vẫn nhận).
  exec "$CLOUDFLARED_BIN" tunnel run --url "http://localhost:${PORT}" "${CLOUDFLARED_TUNNEL_NAME}"
else
  echo "Quick tunnel mode: public URL will appear as: https://<random>.trycloudflare.com"
  exec "$CLOUDFLARED_BIN" tunnel --url "http://localhost:${PORT}" --no-autoupdate
fi
