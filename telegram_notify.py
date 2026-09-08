"""
Gửi tin nhắn Telegram tới mọi chat đã từng dùng bot (private) + chat_id trong config.

- Danh sách lưu tại telegram_bot_subscribers.json (gitignored).
- Luôn gộp thêm config.json -> telegram_chat_id nếu có (không trùng).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SUBSCRIBERS_PATH = BASE_DIR / "telegram_bot_subscribers.json"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_broadcast_chat_ids() -> list[str]:
    """Các chat_id nhận cảnh báo tự động / broadcast."""
    seen: set[str] = set()
    out: list[str] = []

    if SUBSCRIBERS_PATH.exists():
        try:
            raw = json.loads(SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for x in raw:
                    s = str(x).strip()
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
        except Exception:
            pass

    try:
        cfg = load_config()
        legacy = (cfg.get("telegram_chat_id") or "").strip()
        if legacy and legacy not in seen:
            seen.add(legacy)
            out.append(legacy)
    except Exception:
        pass

    return out


def register_private_chat(chat_id: int) -> None:
    """Thêm user (chat private) vào danh sách nhận broadcast."""
    sid = str(int(chat_id))
    data: list = []
    if SUBSCRIBERS_PATH.exists():
        try:
            raw = json.loads(SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                data = [str(x).strip() for x in raw if str(x).strip()]
        except Exception:
            data = []
    if sid in data:
        return
    data.append(sid)
    SUBSCRIBERS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def split_message(text: str, max_length: int = 4000) -> list[str]:
    parts: list[str] = []
    t = text or ""
    while len(t) > max_length:
        split_at = t.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(t[:split_at])
        t = t[split_at:].lstrip()
    if t:
        parts.append(t)
    return parts if parts else [""]


def broadcast_send_text(
    text: str,
    *,
    bot_token: str | None = None,
    max_part_length: int = 4000,
) -> tuple[int, int]:
    """
    Gửi cùng một nội dung tới mọi chat trong danh sách.
    Returns: (số request thành công, số request lỗi).
    """
    cfg = load_config()
    token = (bot_token or cfg.get("telegram_bot_token") or "").strip()
    if not token:
        raise ValueError("Thiếu telegram_bot_token (config.json)")

    chat_ids = get_broadcast_chat_ids()
    if not chat_ids:
        raise ValueError("Không có chat_id nhận tin (subscriber hoặc telegram_chat_id)")

    parts = split_message(text, max_part_length)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = 0
    fail = 0
    for cid in chat_ids:
        for part in parts:
            try:
                r = requests.post(
                    url,
                    data={"chat_id": cid, "text": part},
                    timeout=30,
                )
                if r.ok:
                    ok += 1
                else:
                    fail += 1
                    print(f"Telegram lỗi chat_id={cid}: {r.text}", file=sys.stderr)
            except requests.RequestException as e:
                fail += 1
                print(f"Telegram exception chat_id={cid}: {e}", file=sys.stderr)
    return ok, fail


if __name__ == "__main__":
    msg = sys.stdin.read()
    if not (msg or "").strip():
        sys.exit(0)
    broadcast_send_text(msg)
