"""
Unit test cho telegram_access.verify_group_interactive_access (getChatMember + whitelist).

Chạy: python3 -m unittest tests.test_telegram_access -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from telegram_access import (
    DENY_MESSAGE,
    verify_group_interactive_access,
)


def _update(chat_type, chat_id: int, user_id: int):
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        update_id=1,
    )


class TestVerifyGroupInteractiveAccess(unittest.IsolatedAsyncioTestCase):
    """Các tình huống: trong nhóm, rời nhóm, kick, private, nhóm không whitelist."""

    async def test_whitelist_empty_always_allows_without_api(self) -> None:
        bot = AsyncMock()
        u = _update(ChatType.PRIVATE, 123, 456)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[]
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        bot.get_chat_member.assert_not_called()

    async def test_user_still_member_allowed(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
        )
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 42)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        bot.get_chat_member.assert_awaited_once_with(gid, 42)

    async def test_administrator_allowed(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
        )
        gid = -1001234567890
        u = _update(ChatType.GROUP, gid, 99)
        ok, _ = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertTrue(ok)

    async def test_owner_allowed(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.OWNER)
        )
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 1)
        ok, _ = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertTrue(ok)

    async def test_user_left_denied(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.LEFT)
        )
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 77)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)

    async def test_user_kicked_banned_denied(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.BANNED)
        )
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 88)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)

    async def test_restricted_denied(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.RESTRICTED)
        )
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 5)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)

    async def test_private_chat_denied(self) -> None:
        bot = AsyncMock()
        u = _update(ChatType.PRIVATE, 6785341304, 42)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=["-1001234567890"]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)
        bot.get_chat_member.assert_not_called()

    async def test_group_not_in_whitelist_denied(self) -> None:
        bot = AsyncMock()
        u = _update(ChatType.SUPERGROUP, -9999999999, 42)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=["-1001234567890"]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)
        bot.get_chat_member.assert_not_called()

    async def test_get_chat_member_error_denied(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(side_effect=TelegramError("not found"))
        gid = -1001234567890
        u = _update(ChatType.SUPERGROUP, gid, 42)
        ok, msg = await verify_group_interactive_access(
            bot, u, allowed_group_ids=[str(gid)]
        )
        self.assertFalse(ok)
        self.assertEqual(msg, DENY_MESSAGE)


if __name__ == "__main__":
    unittest.main()
