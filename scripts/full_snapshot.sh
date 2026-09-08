#!/usr/bin/env bash
# Full website snapshot — gói toàn bộ code + secrets + system configs để khôi phục
# trên server hoàn toàn mới khi SSD chính hỏng.
#
# Bao gồm:
#   - Toàn bộ thư mục project (code + .git + .env + config.json + permissions.json + users.json)
#   - Rclone config (gdrive token)
#   - SSH keys (private + known_hosts) — CẨN THẬN: file nhạy cảm
#   - Systemd unit files cho pos-dashboard + pos-scheduler + pgbouncer
#   - Pgbouncer config (ini + userlist)
#   - PostgreSQL config + pg_hba (cần sudo)
#   - Crontab (admin1 + root)
#   - Nginx config (nếu có)
#   - Bashrc / profile
#
# Output: /mnt/nvme/backup/snapshots/snapshot-YYYYMMDD-HHMM.tar.gz
# Sau đó upload lên gdrive:tieuhiem-backup/snapshots/ (giữ 8 tuần).
#
# Schedule: weekly (Chủ nhật 03:00) qua APScheduler — không cần daily vì size lớn.
#
# Usage:
#   bash scripts/full_snapshot.sh            # Snapshot + upload Drive
#   bash scripts/full_snapshot.sh --dry-run  # In file list, không tar
#   bash scripts/full_snapshot.sh --local    # Snapshot local only (skip upload)

set -euo pipefail

PROJECT_ROOT="/home/admin1/tieuhiemsoft"
SNAPSHOT_DIR="/mnt/nvme/backup/snapshots"
LOG_FILE="$SNAPSHOT_DIR/snapshot.log"
TS=$(date +%Y%m%d-%H%M)
SNAPSHOT_FILE="$SNAPSHOT_DIR/snapshot-$TS.tar.gz"
TMP_LIST=$(mktemp)
DRY_RUN=0
LOCAL_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --local)   LOCAL_ONLY=1 ;;
  esac
done

mkdir -p "$SNAPSHOT_DIR"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

log "=== START full snapshot ==="

# ─── 1) Project code + .git + secrets ──────────────────────────────────
# Loại trừ: logs/, exports/, __pycache__/, .venv/, file dump SQL >50MB,
# data_shop*.json legacy lớn, .pyc, .pyo
cat > "$TMP_LIST" <<EOF
$PROJECT_ROOT/posbottieuhiem
$PROJECT_ROOT/lib
$PROJECT_ROOT/scripts
$PROJECT_ROOT/main.py
$PROJECT_ROOT/package.json
$PROJECT_ROOT/pyproject.toml
$PROJECT_ROOT/pnpm-workspace.yaml
$PROJECT_ROOT/tsconfig.base.json
$PROJECT_ROOT/tsconfig.json
$PROJECT_ROOT/replit.md
$PROJECT_ROOT/uv.lock
EOF

# Rclone token + bash profile
echo "/home/admin1/.config/rclone" >> "$TMP_LIST"
echo "/home/admin1/.bashrc" >> "$TMP_LIST"
echo "/home/admin1/.profile" >> "$TMP_LIST"

# SSH keys (cho phép git push, deploy)
if [[ -d /home/admin1/.ssh ]]; then
  echo "/home/admin1/.ssh" >> "$TMP_LIST"
fi

# System configs (cần sudo)
SUDO_LIST=$(mktemp)
cat > "$SUDO_LIST" <<EOF
/etc/systemd/system/pos-dashboard.service
/etc/systemd/system/pos-scheduler.service
/etc/systemd/system/pgbouncer.service
/etc/pgbouncer
/etc/postgresql
/etc/nginx
/etc/cron.d
/etc/crontab
/var/spool/cron/crontabs
EOF

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY RUN — file list:"
  cat "$TMP_LIST" "$SUDO_LIST" | tee -a "$LOG_FILE"
  rm -f "$TMP_LIST" "$SUDO_LIST"
  exit 0
fi

# ─── 2) Build tar ─────────────────────────────────────────────────────
log "Tarring project + user configs..."
SECONDS=0

EXCLUDE_PATTERNS=(
  '--exclude=*/__pycache__'
  '--exclude=*/.venv'
  '--exclude=*/node_modules'
  '--exclude=*/logs/*.log'
  '--exclude=*/logs/*.log.*'
  '--exclude=*/exports/*'
  '--exclude=*.pyc'
  '--exclude=*.pyo'
  '--exclude=*.bak'
  '--exclude=*.bak.*'
  '--exclude=*/deploy/*.bak.*'
  '--exclude=*/data_shop*.json'
  '--exclude=*/data_SHOP*.json'
  '--exclude=*/data_shp*.json'
  '--exclude=*/stock_*.json'
  '--exclude=*/stock_history'
  '--exclude=*/live_pos_status.json'
  '--exclude=*/*.dump'
  '--exclude=*/*.sql'
  '--exclude=*/.claude'
  '--exclude=*/claude/worktrees'
)

# Phần user-owned (không cần sudo)
# --ignore-failed-read: skip file không đọc được (vd .bak chown root) thay vì fail toàn tar
tar -czf "$SNAPSHOT_FILE.user" --ignore-failed-read "${EXCLUDE_PATTERNS[@]}" -T "$TMP_LIST" 2>>"$LOG_FILE" || {
  log "⚠ tar user-owned có lỗi đọc file — vẫn tiếp tục"
}

# Phần system-wide (sudo)
sudo tar -czf "$SNAPSHOT_FILE.system" --ignore-failed-read -T "$SUDO_LIST" 2>>"$LOG_FILE" || {
  log "⚠ tar system có lỗi đọc — vẫn tiếp tục"
}
sudo chown admin1:admin1 "$SNAPSHOT_FILE.system" 2>/dev/null || true

# Gộp 2 phần
tar -czf "$SNAPSHOT_FILE" \
  --transform 's|^|snapshot/|' \
  -C "$SNAPSHOT_DIR" \
  "$(basename "$SNAPSHOT_FILE.user")" \
  "$(basename "$SNAPSHOT_FILE.system" 2>/dev/null || true)" \
  2>>"$LOG_FILE" || true

# Đơn giản hơn: giữ 2 file riêng kèm nhau
mv "$SNAPSHOT_FILE.user"   "$SNAPSHOT_DIR/snapshot-$TS-user.tar.gz"
[[ -f "$SNAPSHOT_FILE.system" ]] && mv "$SNAPSHOT_FILE.system" "$SNAPSHOT_DIR/snapshot-$TS-system.tar.gz"
rm -f "$SNAPSHOT_FILE"  # gộp dùng-ko-tốt, xóa

# Tạo manifest đi kèm
MANIFEST="$SNAPSHOT_DIR/snapshot-$TS-manifest.txt"
{
  echo "=== Tiểu Hiềm Full Snapshot ==="
  echo "Created: $(date '+%F %T %z')"
  echo "Hostname: $(hostname)"
  echo "Git branch: $(cd "$PROJECT_ROOT" && git branch --show-current 2>/dev/null || echo unknown)"
  echo "Git HEAD: $(cd "$PROJECT_ROOT" && git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo ""
  echo "=== File user ==="
  ls -lh "$SNAPSHOT_DIR/snapshot-$TS-user.tar.gz" 2>/dev/null
  echo "=== File system ==="
  ls -lh "$SNAPSHOT_DIR/snapshot-$TS-system.tar.gz" 2>/dev/null
  echo ""
  echo "=== Restore command ==="
  echo "Xem /home/admin1/tieuhiemsoft/posbottieuhiem/scripts/auto_restore.sh"
} > "$MANIFEST"

SIZE_USER=$(stat -c %s "$SNAPSHOT_DIR/snapshot-$TS-user.tar.gz" 2>/dev/null | numfmt --to=iec || echo "?")
SIZE_SYS=$(stat -c %s "$SNAPSHOT_DIR/snapshot-$TS-system.tar.gz" 2>/dev/null | numfmt --to=iec || echo "?")
log "Snapshot done in ${SECONDS}s — user=$SIZE_USER system=$SIZE_SYS"

# ─── 3) Rotate local: giữ 4 weekly snapshots ──────────────────────────
DEL=$(find "$SNAPSHOT_DIR" -name "snapshot-*" -mtime +28 -delete -print | wc -l)
log "Rotate local: -$DEL files >28d"

# ─── 4) Upload Drive ───────────────────────────────────────────────────
if [[ $LOCAL_ONLY -eq 1 ]]; then
  log "--local mode — skip Drive upload"
elif command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q "^gdrive:"; then
  log "Uploading to gdrive:tieuhiem-backup/snapshots/..."
  rclone mkdir gdrive:tieuhiem-backup/snapshots 2>>"$LOG_FILE" || true
  UP_OK=1
  for f in \
    "$SNAPSHOT_DIR/snapshot-$TS-user.tar.gz" \
    "$SNAPSHOT_DIR/snapshot-$TS-system.tar.gz" \
    "$MANIFEST"; do
    [[ -f "$f" ]] || continue
    if ! rclone copy --quiet --timeout 30m "$f" gdrive:tieuhiem-backup/snapshots/ 2>>"$LOG_FILE"; then
      log "  ✗ FAIL upload: $(basename "$f")"
      UP_OK=0
    fi
  done
  [[ $UP_OK -eq 1 ]] && log "✓ Drive snapshot uploaded" || log "✗ Drive upload có lỗi"

  # Rotate Drive: giữ 8 weeks
  rclone delete --quiet --min-age 56d gdrive:tieuhiem-backup/snapshots/ 2>>"$LOG_FILE" || true
else
  log "⚠ rclone gdrive chưa cấu hình — skip Drive upload"
fi

rm -f "$TMP_LIST" "$SUDO_LIST"
log "=== END snapshot ==="
log ""
