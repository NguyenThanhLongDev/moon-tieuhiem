#!/usr/bin/env python3
"""Sync hội thoại Pancake Chat → LÕI "Bắt sale gian lận".

Token Pancake Chat: env PANCAKE_CHAT_TOKEN, fallback config 'pancake_chat_token'.
Chạy 1 page:  .venv/bin/python scripts/sync_fraud_pancake.py <page_id>
Chạy hết:     .venv/bin/python scripts/sync_fraud_pancake.py

Cron gợi ý (mỗi 10 phút — chụp tin trước khi sale kịp xoá):
  */10 * * * * cd $PROJ && export $(grep -v '^#' $ENV_FILE | xargs) && \
               .venv/bin/python scripts/sync_fraud_pancake.py >> logs/sync_fraud.log 2>&1
"""
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_fraud_pancake")


def _get_token() -> str:
    t = (os.getenv("PANCAKE_CHAT_TOKEN", "") or "").strip()
    if t:
        return t
    try:
        from app_ctx import load_config
        return str((load_config() or {}).get("pancake_chat_token", "")).strip()
    except Exception:
        return ""


def main():
    token = _get_token()
    if not token:
        logger.error("Thiếu PANCAKE_CHAT_TOKEN (env) hoặc config 'pancake_chat_token'. "
                     "Chưa có token Pancake Chat của khách → không sync được.")
        sys.exit(1)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    from modules.fraud_detect.adapter_pancake import sync_all
    logger.info("=== sync Pancake Chat → fraud_detect ===")
    total = 0
    for r in sync_all(token, only_page_id=only):
        total += r.get("conversations", 0)
        logger.info("  page %s: %s hội thoại · %s tin · %s nghi xoá",
                    r.get("page_id"), r.get("conversations"), r.get("messages"), r.get("deleted_detected"))
    logger.info("=== DONE: %s hội thoại ===", total)


if __name__ == "__main__":
    main()
