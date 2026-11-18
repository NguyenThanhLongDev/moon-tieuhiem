"""Lịch sync trong Docker (thay crontab host):
  - Nhẹ : mỗi 30 phút, 07:00-22:30  -> scripts/cron_sync.sh
  - Đầy : 01:00 mỗi ngày            -> scripts/cron_sync.sh full
Chạy tuần tự (không chồng nhau). Giờ theo TZ container (Asia/Ho_Chi_Minh).
"""
import subprocess
import time
from datetime import datetime

SCRIPT = "scripts/cron_sync.sh"
last = None

while True:
    now = datetime.now()
    key = now.strftime("%Y%m%d%H%M")
    if key != last and now.minute in (0, 30):
        mode = None
        if now.hour == 1 and now.minute == 0:
            mode = "full"
        elif 7 <= now.hour <= 22:
            mode = "light"
        if mode:
            last = key
            print(f"[{now:%F %T}] chạy cron_sync {mode}", flush=True)
            subprocess.run(["bash", SCRIPT] + (["full"] if mode == "full" else []))
    time.sleep(20)
