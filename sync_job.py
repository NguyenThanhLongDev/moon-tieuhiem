import subprocess
from pathlib import Path
from datetime import datetime

from telegram_notify import broadcast_send_text

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync_job.log"


def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_telegram(text: str) -> None:
    try:
        broadcast_send_text(text)
    except Exception as e:
        log(f"ERROR send telegram: {e}")


def main():
    log("===== START SYNC JOB =====")

    try:
        result = subprocess.run(
            ["python3", str(BASE_DIR / "sync_pos.py")],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=1800,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            log("STDOUT:")
            for line in stdout.splitlines():
                log(line)

        if stderr:
            log("STDERR:")
            for line in stderr.splitlines():
                log(line)

        # gom lỗi từ output của sync_pos.py
        error_lines = []
        ok_count = 0

        for line in stdout.splitlines():
            if "ERROR:" in line:
                error_lines.append(line)
            if line.startswith("OK:"):
                ok_count += 1

        # trường hợp script chết hẳn
        if result.returncode != 0:
            msg = (
                "⚠️ SYNC POS LỖI\n"
                f"Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Return code: {result.returncode}\n\n"
                f"STDERR:\n{stderr[:3000]}"
            )
            send_telegram(msg)
            log("SYNC JOB FAILED BY RETURN CODE")
            return

        # trường hợp script chạy xong nhưng có shop lỗi
        if error_lines:
            msg = (
                "⚠️ SYNC POS CÓ SHOP LỖI\n"
                f"Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Shop OK: {ok_count}\n"
                f"Shop lỗi: {len(error_lines)}\n\n"
                "Chi tiết lỗi:\n"
                + "\n".join(error_lines[:20])
            )
            send_telegram(msg)
            log(f"SYNC JOB DONE WITH ERRORS: {len(error_lines)}")
        else:
            log("SYNC JOB SUCCESS - KHÔNG CÓ LỖI")

    except Exception as e:
        err = str(e)
        log(f"SYNC JOB EXCEPTION: {err}")
        send_telegram(
            "⚠️ SYNC POS LỖI NGHIÊM TRỌNG\n"
            f"Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Lỗi: {err}"
        )


if __name__ == "__main__":
    main()
