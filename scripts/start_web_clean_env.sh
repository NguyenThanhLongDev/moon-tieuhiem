#!/usr/bin/env bash
# Khởi động web trong môi trường sạch (env -i) để tránh lỗi BASH_FUNC_* / completion
# bị export từ session khác (Cursor, SSH profile lỗi) làm bash con crash → web không lên → Cloudflare 502.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
ENVFILE="$ROOT/deploy/pos-dashboard.env"
if [[ ! -f "$ENVFILE" ]]; then
  ENVFILE="$ROOT/deploy/pos-dashboard.env.example"
fi
if [[ ! -f "$ENVFILE" ]]; then
  echo "ERROR: Không tìm thấy deploy/pos-dashboard.env hoặc .example"
  exit 1
fi

exec /usr/bin/env -i \
  HOME="${HOME:-/home/admin1}" \
  PATH="/usr/local/bin:/usr/bin:/bin" \
  USER="${USER:-admin1}" \
  LANG="${LANG:-C.UTF-8}" \
  /bin/bash --noprofile --norc -c "
    set -euo pipefail
    set -a
    # shellcheck disable=SC1090
    source '$ENVFILE'
    set +a
    cd '$ROOT'
    exec ./run_web_vps.sh
  "
