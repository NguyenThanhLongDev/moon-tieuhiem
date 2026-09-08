"""
Kiểm tra quyền tương tác Telegram bot (chỉ nhóm whitelist + getChatMember).

- Cảnh báo tự động (cron / telegram_notify.py) không đi qua module này.
- Cấu hình ALLOWED_GROUP_IDS (env, cách nhau bởi dấu phẩy) và/hoặc config.json:
  telegram_allowed_group_ids | ALLOWED_GROUP_IDS (mảng hoặc chuỗi CSV).
- Nếu không cấu hình nhóm nào (rỗng): tắt kiểm tra → giữ hành vi cũ (private + mọi group).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Thông báo từ chối (đúng theo yêu cầu nghiệp vụ)
DENY_MESSAGE = "Bot này chỉ hoạt động trong nhóm nội bộ được cấp quyền."

# Chỉ chấp nhận: creator (OWNER), administrator, member
_ACCEPTED_STATUSES = frozenset(
    {
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }
)


def _normalize_id_list(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def load_allowed_group_ids() -> list[str]:
    """Đọc whitelist group id: env ALLOWED_GROUP_IDS trước, gộp với config.json."""
    seen: set[str] = set()
    out: list[str] = []

    env_raw = os.environ.get("ALLOWED_GROUP_IDS", "").strip()
    for s in _normalize_id_list(env_raw):
        if s not in seen:
            seen.add(s)
            out.append(s)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.debug("telegram_access: không đọc config.json: %s", e)
        return out

    for key in ("telegram_allowed_group_ids", "ALLOWED_GROUP_IDS"):
        for s in _normalize_id_list(cfg.get(key)):
            if s not in seen:
                seen.add(s)
                out.append(s)

    return out


def restriction_enabled(allowed_group_ids: list[str] | None = None) -> bool:
    ids = allowed_group_ids if allowed_group_ids is not None else load_allowed_group_ids()
    return bool(ids)


async def verify_group_interactive_access(
    bot,
    update: Update,
    *,
    allowed_group_ids: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Trả (True, "") nếu được phép xử lý lệnh; ngược lại (False, DENY_MESSAGE).

    Khi không bật whitelist (danh sách rỗng): luôn cho phép (tương thích cũ).
    """
    allowed = allowed_group_ids if allowed_group_ids is not None else load_allowed_group_ids()
    allowed_set = set(allowed)

    if not allowed_set:
        return True, ""

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        logger.warning(
            "telegram_access: deny missing chat/user update_id=%s",
            getattr(update, "update_id", None),
        )
        return False, DENY_MESSAGE

    user_id = user.id
    chat_id = chat.id
    chat_id_str = str(chat_id)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        logger.warning(
            "telegram_access: deny chat_type=%s user_id=%s chat_id=%s",
            chat.type,
            user_id,
            chat_id_str,
        )
        return False, DENY_MESSAGE

    if chat_id_str not in allowed_set:
        logger.warning(
            "telegram_access: deny group_not_whitelisted user_id=%s chat_id=%s allowed=%s",
            user_id,
            chat_id_str,
            sorted(allowed_set),
        )
        return False, DENY_MESSAGE

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        logger.warning(
            "telegram_access: get_chat_member failed user_id=%s chat_id=%s err=%s",
            user_id,
            chat_id_str,
            e,
        )
        return False, DENY_MESSAGE

    status = member.status
    if status not in _ACCEPTED_STATUSES:
        logger.warning(
            "telegram_access: deny bad_member_status user_id=%s chat_id=%s status=%s",
            user_id,
            chat_id_str,
            status,
        )
        return False, DENY_MESSAGE

    logger.info(
        "telegram_access: allow user_id=%s chat_id=%s member_status=%s",
        user_id,
        chat_id_str,
        status,
    )
    return True, ""
