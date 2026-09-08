from datetime import datetime, timedelta
import subprocess
import sys

def run_sync_for_date(day_str: str):
    print(f"\n=== Sync ngay {day_str} ===")
    subprocess.run(["python3", "sync_pos.py", "--date", day_str], check=False)

def daterange(start_date, end_date):
    cur = start_date
    while cur <= end_date:
        yield cur
        cur += timedelta(days=1)

today = datetime.now().date()
mode = sys.argv[1] if len(sys.argv) > 1 else "today"

if mode == "today":
    run_sync_for_date(today.strftime("%Y-%m-%d"))

elif mode == "last7":
    start_7 = today - timedelta(days=7)
    end_7 = today - timedelta(days=1)
    for d in daterange(start_7, end_7):
        run_sync_for_date(d.strftime("%Y-%m-%d"))

elif mode == "last30":
    start_30 = today - timedelta(days=30)
    end_30 = today - timedelta(days=8)
    for d in daterange(start_30, end_30):
        run_sync_for_date(d.strftime("%Y-%m-%d"))

else:
    print(f"Mode khong hop le: {mode}")
