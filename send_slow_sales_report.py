import subprocess

from telegram_notify import broadcast_send_text

PROJECT_DIR = "/home/admin/posbot"


def run_script(cmd):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR
    )
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip() or "Khong co du lieu phu hop."
    return result.stdout.strip() or result.stderr.strip() or "Khong co du lieu phu hop."


def send_telegram(text):
    broadcast_send_text(text)


def main():
    top_text = run_script(["python3", "slow_sales_ranking.py"])
    all_text = run_script(["python3", "slow_sales_report.py"])

    send_telegram("📊 BAO CAO BAN CHAM MOI SANG\n")
    send_telegram(top_text)
    send_telegram(all_text)


if __name__ == "__main__":
    main()
