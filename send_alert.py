import subprocess

from telegram_notify import broadcast_send_text


def main():
    result = subprocess.run(
        ["python3", "alert_engine.py", "hôm", "qua"],
        capture_output=True,
        text=True,
        cwd="."
    )

    message = result.stdout.strip()

    if not message:
        message = "Không có cảnh báo."

    if len(message) > 4000:
        message = message[:4000] + "\n\n...tin nhắn quá dài, đã cắt bớt"

    ok, fail = broadcast_send_text(message)
    print(f"telegram: ok={ok} fail={fail}")

if __name__ == "__main__":
    main()
