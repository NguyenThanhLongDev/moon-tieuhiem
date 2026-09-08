#!/usr/bin/env bash
# Watchdog RAM cho gunicorn web_app
# - Tìm worker process (child của master)
# - Nếu RSS > THRESHOLD_MB → gửi SIGTERM, gunicorn tự respawn worker mới, RAM trả về OS
# - Master không bị đụng → không downtime (worker respawn < 1s, request mới vào worker kế)
#
# Chạy qua cron mỗi phút:
#   * * * * * /home/admin1/tieuhiemsoft/posbottieuhiem/scripts/gunicorn_memory_watchdog.sh >> /home/admin1/tieuhiemsoft/posbottieuhiem/logs/mem_watchdog.log 2>&1

set -euo pipefail

THRESHOLD_MB="${THRESHOLD_MB:-6144}"   # 6 GB — máy 23GB, 3 workers × 6GB = 18GB an toàn
LOG_TAG="[mem-watchdog $(date '+%F %T')]"

# Tìm master (PPID=1, cmd chứa "web_app:app")
MASTER_PID=$(pgrep -f 'gunicorn.*web_app:app' | while read p; do
  ppid=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  [ "$ppid" = "1" ] && echo "$p" && break
done | head -1)

if [ -z "$MASTER_PID" ]; then
  echo "$LOG_TAG Không tìm thấy gunicorn master — bỏ qua."
  exit 0
fi

# Worker = child process của master
WORKER_PIDS=$(pgrep -P "$MASTER_PID" || true)
if [ -z "$WORKER_PIDS" ]; then
  echo "$LOG_TAG Master $MASTER_PID không có worker — bỏ qua."
  exit 0
fi

for WPID in $WORKER_PIDS; do
  RSS_KB=$(ps -o rss= -p "$WPID" 2>/dev/null | tr -d ' ' || echo 0)
  [ -z "$RSS_KB" ] && continue
  RSS_MB=$(( RSS_KB / 1024 ))

  if [ "$RSS_MB" -gt "$THRESHOLD_MB" ]; then
    echo "$LOG_TAG Worker $WPID RSS ${RSS_MB} MB > ${THRESHOLD_MB} MB → SIGTERM (master $MASTER_PID sẽ respawn)"
    kill -TERM "$WPID" || true
    # Chờ graceful shutdown — nếu sau 35s worker vẫn còn thì SIGKILL
    (
      sleep 35
      if kill -0 "$WPID" 2>/dev/null; then
        echo "$LOG_TAG Worker $WPID lì sau SIGTERM 35s → SIGKILL"
        kill -KILL "$WPID" || true
      fi
    ) &
    disown
  else
    # Chỉ log khi gần ngưỡng (tiết kiệm log)
    if [ "$RSS_MB" -gt $(( THRESHOLD_MB * 80 / 100 )) ]; then
      echo "$LOG_TAG Worker $WPID RSS ${RSS_MB} MB (ngưỡng ${THRESHOLD_MB} MB) — OK, đang theo dõi"
    fi
  fi
done
