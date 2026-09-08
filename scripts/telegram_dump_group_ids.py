#!/usr/bin/env python3
"""
In chat_id các nhóm/supergroup mà bot đã nhận được update gần đây.

Cách dùng (quan trọng):
1) Tạm dừng bot để không tranh getUpdates với polling:
     sudo systemctl stop posbot
2) Thêm bot vào nhóm, trong nhóm gửi một tin có nhắc bot, ví dụ:
     /start@TenBotCuaBan
   (Thay TenBotCuaBan bằng username bot, không có @ ở đầu trong lệnh thì có @ giữa)
   Hoặc: @TenBotCuaBan hello
3) Chạy:
     python3 scripts/telegram_dump_group_ids.py
4) Bật lại bot:
     sudo systemctl start posbot

Nếu bot đang chạy polling, script thường KHÔNG thấy update (hoặc lỗi 409).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"


def main() -> int:
    if not CONFIG.exists():
        print("Khong tim thay config.json", file=sys.stderr)
        return 1
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    token = (cfg.get("telegram_bot_token") or "").strip()
    if not token:
        print("Thieu telegram_bot_token trong config.json", file=sys.stderr)
        return 1

    base = f"https://api.telegram.org/bot{token}"
    # Xóa webhook nếu có (không xóa pending trừ khi cần)
    r0 = requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=30)
    if not r0.ok:
        print("deleteWebhook:", r0.text[:500], file=sys.stderr)

    r = requests.get(f"{base}/getUpdates", params={"limit": 100, "timeout": 0}, timeout=45)
    if not r.ok:
        print("getUpdates loi:", r.text[:500], file=sys.stderr)
        return 1
    data = r.json()
    if not data.get("ok"):
        print("getUpdates:", data, file=sys.stderr)
        return 1

    seen: dict[str, tuple[str, str]] = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        typ = chat.get("type") or ""
        if typ not in ("group", "supergroup"):
            continue
        cid = str(chat.get("id", ""))
        title = chat.get("title") or "(no title)"
        if cid:
            seen[cid] = (title, typ)

    if not seen:
        print(
            "Khong co update nao tu group/supergroup trong 100 ban ghi gan nhat.\n"
            "Hay: stop posbot -> trong NHOM gui /start@username_bot hoac @username_bot test "
            "-> chay lai script.",
            file=sys.stderr,
        )
        return 2

    print("chat_id (dung cho ALLOWED_GROUP_IDS / config):")
    for cid, (title, typ) in sorted(seen.items(), key=lambda x: x[0]):
        print(f"  {cid}  type={typ}  title={title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
