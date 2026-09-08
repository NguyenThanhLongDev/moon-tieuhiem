"""
Standalone scheduler process — chạy độc lập với gunicorn web workers.

Mục đích: Tách APScheduler ra khỏi web worker để:
  1. Tăng gunicorn --workers lên 3+ mà không bị duplicate jobs
  2. Web workers không chịu thêm CPU/RAM từ sync tasks nặng
  3. Scheduler không bị restart khi gunicorn watchdog kill worker

Chạy qua systemd: pos-scheduler.service
"""
from __future__ import annotations

import os
import sys
import signal
import time

# ── Load .env (nếu có) ────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

# ── Timezone ──────────────────────────────────────────────────────────────────
if not os.environ.get("TZ"):
    os.environ["TZ"] = "Asia/Ho_Chi_Minh"
try:
    time.tzset()
except AttributeError:
    pass

# ── DATABASE_URL required ─────────────────────────────────────────────────────
if not os.environ.get("DATABASE_URL"):
    sys.exit("ERROR: DATABASE_URL is not set. Scheduler needs DB access.")

# ── Start scheduler ───────────────────────────────────────────────────────────
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler-standalone")

from scheduler import start_scheduler

log.info("Starting standalone scheduler process (PID=%s)…", os.getpid())
_scheduler = start_scheduler()
log.info("Scheduler started. Running jobs: %s", [j.name for j in _scheduler.get_jobs()])

# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _shutdown(signum, frame):
    log.info("Signal %s received — shutting down scheduler…", signum)
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# ── Keep alive ────────────────────────────────────────────────────────────────
try:
    while True:
        time.sleep(60)
        # Log heartbeat mỗi 10 phút
        jobs = _scheduler.get_jobs()
        running_now = [j for j in jobs if hasattr(j, 'next_run_time') and j.next_run_time]
        log.debug("Heartbeat — %s jobs scheduled", len(running_now))
except KeyboardInterrupt:
    _shutdown(signal.SIGINT, None)
