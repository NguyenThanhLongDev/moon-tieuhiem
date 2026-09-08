"""Cron 20:00 — Lan nhắc NV báo chi phí trong ngày.

Loop tất cả Zalo group Lan đang ở (trừ group inbox + test), post 1 tin
nhắc nhẹ nhàng. NV ai mua/chi gì hôm nay → tự gửi ghi nhận trong group.
"""
from __future__ import annotations

import logging
import os
import random

import requests

logger = logging.getLogger(__name__)

# Đa dạng giọng nhắc để không lặp lại
_PHRASES = [
    "🌆 Cả nhà ơi, chiều rồi nha 🌸\n\nHôm nay ai có chi/mua/thanh toán gì cho công ty thì gửi ngay vào group này giúp Lan với nhé — có ảnh hoá đơn càng tốt 🧸\n\nLan sẽ ghi vô sổ kế toán chốt ngày cho mượt. Cảm ơn cả nhà! 🌷",
    "🌙 Sắp hết giờ làm rồi cả nhà! 🌸\n\nAi hôm nay có chi phí gì (VPP, đo đạc, thanh toán, taxi, tiếp khách...) đừng quên gửi cho Lan vào group này nha — gõ tin hoặc ảnh đều được ạ ✨\n\nKhỏi mai phải nhớ lại, mệt lắm! 🥺",
    "🧸 Lan ngó lại thấy hôm nay có ít chi phí được báo ạ 🌷\n\nCả nhà check xem có gì chi/mua quên báo không nhỉ? Cứ gõ vô group này, Lan ghi vô sổ luôn — dễ cho kế toán cuối tháng đối chiếu ✨",
    "🌸 20h rồi cả nhà ơi!\n\nNếu hôm nay có chi/mua gì cho cty (kể cả vài trăm k) thì gửi giúp Lan trong group nha — text hoặc ảnh đều OK 🧸\n\nLan luôn sẵn sàng ghi, chỉ sợ cả nhà quên thôi 🥺🌷",
]

# Group KHÔNG gửi reminder:
# - 6432337956406416569 (Nhóm Báo Chi Phí Cty — inbox tổng hợp, không phải nơi báo)
# - 638544760243854625  (nhom étt — group test)
# (Có thể move sang app_config sau)
_SKIP_THREAD_IDS = {"638544760243854625"}


def _get_inbox_id() -> str:
    try:
        from app_ctx import load_config
        v = (load_config() or {}).get("zalo_expense_inbox")
        return str(v).strip() if v else ""
    except Exception:
        return ""


def send_expense_reminder() -> int:
    """Cron entry. Gửi 1 tin nhắc vào tất cả group Lan đang ở (trừ inbox/test)."""
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        logger.warning("expense_reminder: ZALO_BRIDGE_SECRET không có — skip")
        return 0

    try:
        r = requests.get("http://127.0.0.1:5051/list-groups",
                         headers={"X-Bridge-Secret": secret}, timeout=10)
        if r.status_code != 200:
            logger.warning("expense_reminder: list-groups HTTP %s", r.status_code)
            return 0
        groups = r.json().get("groups", [])
    except Exception as exc:
        logger.warning("expense_reminder: list-groups error: %s", exc)
        return 0

    inbox_tid = _get_inbox_id()
    skip = set(_SKIP_THREAD_IDS) | ({inbox_tid} if inbox_tid else set())

    text = random.choice(_PHRASES)
    sent = 0
    for g in groups:
        tid = str(g.get("thread_id") or "")
        if not tid or tid in skip:
            continue
        try:
            rr = requests.post(
                "http://127.0.0.1:5051/send",
                headers={"X-Bridge-Secret": secret, "Content-Type": "application/json"},
                json={"thread_id": tid, "text": text}, timeout=10,
            )
            if rr.status_code == 200 and '"ok":true' in rr.text:
                sent += 1
                logger.info("expense_reminder → %s (%s) OK", g.get("name", ""), tid)
            else:
                logger.warning("expense_reminder → %s fail %s", tid, rr.text[:120])
        except Exception as exc:
            logger.warning("expense_reminder → %s exc: %s", tid, exc)
    logger.info("expense_reminder: hoàn tất, gửi %d group", sent)
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    send_expense_reminder()
