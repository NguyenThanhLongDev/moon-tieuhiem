#!/usr/bin/env bash
# Backup PostgreSQL hàng ngày — rotate 7 daily / 4 weekly / 3 monthly.
# Lưu vào /mnt/nvme/backup/postgres/ (NVMe 117GB).
#
# Usage:
#   bash scripts/backup_db.sh                    # Backup ngay + rotate
#   bash scripts/backup_db.sh --verify-only      # Chỉ list files, không backup
#
# Restore:
#   PGPASSWORD=... pg_restore -h localhost -p 5432 -U tieuhiem \
#     -d tieuhiem_pos --clean --if-exists --no-owner \
#     /mnt/nvme/backup/postgres/daily/tieuhiem-YYYYMMDD-HHMM.dump

set -euo pipefail

BACKUP_ROOT="/mnt/nvme/backup/postgres"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
MONTHLY_DIR="$BACKUP_ROOT/monthly"
LOG_FILE="$BACKUP_ROOT/backup.log"

# Đọc DATABASE_URL trực tiếp PostgreSQL (KHÔNG qua PgBouncer — pg_dump cần
# session-level features không tương thích transaction pool).
ENV_FILE="/home/admin1/tieuhiemsoft/posbottieuhiem/deploy/pos-dashboard.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 1
fi
DB_URL=$(grep -oP '(?<=^DATABASE_URL=).*' "$ENV_FILE" | head -1)
# Force port 5432 (skip PgBouncer 6432)
DB_URL_DIRECT=$(echo "$DB_URL" | sed 's|@localhost:6432/|@localhost:5432/|')

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR" "$MONTHLY_DIR"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

if [[ "${1:-}" == "--verify-only" ]]; then
  log "=== Verify-only — listing backups ==="
  for d in daily weekly monthly; do
    echo "--- $d ---"
    ls -lh "$BACKUP_ROOT/$d" 2>/dev/null | tail -n +2 || echo "(empty)"
  done
  echo "--- Disk usage ---"
  du -sh "$BACKUP_ROOT"
  df -h /mnt/nvme | tail -1
  exit 0
fi

TS=$(date +%Y%m%d-%H%M)
DOW=$(date +%u)   # 1-7 (Mon-Sun)
DOM=$(date +%d)   # 01-31

DUMP_FILE="$DAILY_DIR/tieuhiem-$TS.dump"

log "=== START backup ==="
log "DB URL: ${DB_URL_DIRECT/:*@/:****@}"
log "Output: $DUMP_FILE"

# pg_dump custom format, compress 9 (max), no owner (portable)
SECONDS=0
pg_dump "$DB_URL_DIRECT" \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-acl \
  --file="$DUMP_FILE" 2>>"$LOG_FILE"

DUMP_SIZE=$(stat -c %s "$DUMP_FILE" | numfmt --to=iec)
log "pg_dump done: $DUMP_SIZE in ${SECONDS}s"

# ── Verify integrity bằng pg_restore --list ────────────────────────────
if pg_restore --list "$DUMP_FILE" >/dev/null 2>>"$LOG_FILE"; then
  log "✓ Integrity check passed (pg_restore --list)"
else
  log "✗ INTEGRITY FAILED — backup file CORRUPT, removing"
  rm -f "$DUMP_FILE"
  exit 2
fi

# ── Rotate weekly: copy nếu Chủ nhật (DOW=7) ──────────────────────────
if [[ "$DOW" == "7" ]]; then
  cp "$DUMP_FILE" "$WEEKLY_DIR/tieuhiem-week-$TS.dump"
  log "→ Copied to weekly/"
fi

# ── Rotate monthly: copy nếu ngày 1 ───────────────────────────────────
if [[ "$DOM" == "01" ]]; then
  cp "$DUMP_FILE" "$MONTHLY_DIR/tieuhiem-month-$TS.dump"
  log "→ Copied to monthly/"
fi

# ── Cleanup: daily > 7 ngày, weekly > 28 ngày, monthly > 90 ngày ──────
DEL_DAILY=$(find "$DAILY_DIR" -name "tieuhiem-*.dump" -mtime +7 -print -delete | wc -l)
DEL_WEEKLY=$(find "$WEEKLY_DIR" -name "tieuhiem-week-*.dump" -mtime +28 -print -delete | wc -l)
DEL_MONTHLY=$(find "$MONTHLY_DIR" -name "tieuhiem-month-*.dump" -mtime +90 -print -delete | wc -l)
log "Rotate: -$DEL_DAILY daily, -$DEL_WEEKLY weekly, -$DEL_MONTHLY monthly"

# ── Bonus: backup config files quan trọng (kích thước nhỏ) ────────────
CONFIG_BACKUP="$BACKUP_ROOT/configs-$TS.tar.gz"
sudo tar -czf "$CONFIG_BACKUP" 2>>"$LOG_FILE" \
  /etc/pgbouncer/pgbouncer.ini \
  /etc/pgbouncer/userlist.txt \
  /etc/systemd/system/pos-dashboard.service \
  /etc/systemd/system/pos-scheduler.service \
  "$ENV_FILE" 2>/dev/null || true
sudo chown admin1:admin1 "$CONFIG_BACKUP" 2>/dev/null || true
log "Config snapshot: $(stat -c %s "$CONFIG_BACKUP" 2>/dev/null | numfmt --to=iec)"
# Keep last 14 config snapshots
find "$BACKUP_ROOT" -maxdepth 1 -name "configs-*.tar.gz" -mtime +14 -delete

# ── Offsite backup: upload lên Google Drive qua rclone ────────────────
# Drive folder: tieuhiem-backup/{daily,weekly,monthly,configs}
# Drive giữ rotation 90 ngày (xóa file Drive cũ hơn 90 ngày)
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q "^gdrive:"; then
  RC_FLAGS="--config /home/admin1/.config/rclone/rclone.conf --quiet --timeout 5m"
  log "→ rclone: upload daily/$(basename "$DUMP_FILE")"
  rclone copy $RC_FLAGS "$DUMP_FILE" gdrive:tieuhiem-backup/daily/ 2>>"$LOG_FILE" \
    && log "  ✓ Drive daily OK" \
    || log "  ✗ Drive daily FAIL (xem log)"

  if [[ "$DOW" == "7" ]]; then
    rclone copy $RC_FLAGS "$WEEKLY_DIR/tieuhiem-week-$TS.dump" gdrive:tieuhiem-backup/weekly/ 2>>"$LOG_FILE" \
      && log "  ✓ Drive weekly OK"
  fi
  if [[ "$DOM" == "01" ]]; then
    rclone copy $RC_FLAGS "$MONTHLY_DIR/tieuhiem-month-$TS.dump" gdrive:tieuhiem-backup/monthly/ 2>>"$LOG_FILE" \
      && log "  ✓ Drive monthly OK"
  fi
  rclone copy $RC_FLAGS "$CONFIG_BACKUP" gdrive:tieuhiem-backup/configs/ 2>>"$LOG_FILE" \
    && log "  ✓ Drive config snapshot OK"

  # Rotation trên Drive: xoá daily > 90 ngày, weekly > 180 ngày, monthly > 365 ngày
  rclone delete $RC_FLAGS --min-age 90d gdrive:tieuhiem-backup/daily/   2>>"$LOG_FILE" || true
  rclone delete $RC_FLAGS --min-age 180d gdrive:tieuhiem-backup/weekly/ 2>>"$LOG_FILE" || true
  rclone delete $RC_FLAGS --min-age 365d gdrive:tieuhiem-backup/monthly/ 2>>"$LOG_FILE" || true
  rclone delete $RC_FLAGS --min-age 30d gdrive:tieuhiem-backup/configs/ 2>>"$LOG_FILE" || true
else
  log "⚠ rclone gdrive chưa cấu hình — skip offsite upload"
fi

log "=== END backup ==="
log ""
