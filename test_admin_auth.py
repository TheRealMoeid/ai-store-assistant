# test_admin_auth.py
"""
Verifies Issue #1 fix: admin commands (/pending_orders, /confirm, /reject)
must be rejected for any chat/user that isn't the configured admin group
or on the ADMIN_USER_IDS whitelist.

Uses a minimal fake Message (duck-typed) rather than spinning up aiogram's
full dispatcher, since _is_admin() only reads message.chat.id and
message.from_user.id.
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
config.ADMIN_GROUP_ID = -1009999999
config.ADMIN_USER_IDS = {555}

from handlers.admin_handlers import _is_admin, AdminOnlyMiddleware


def fake_message(chat_id: int, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
        text="/confirm ord_123",
    )


def test_is_admin_checks():
    # Admin group -> allowed, regardless of who's posting in it
    assert _is_admin(fake_message(chat_id=config.ADMIN_GROUP_ID, user_id=1)) is True

    # Whitelisted user DMing the bot privately -> allowed
    assert _is_admin(fake_message(chat_id=555, user_id=555)) is True

    # Random customer in their own private chat -> blocked
    assert _is_admin(fake_message(chat_id=999999, user_id=999999)) is False

    # Random customer *inside* the admin group's chat_id guessed/spoofed
    # user_id, but wrong chat -> still blocked (chat_id is what's checked
    # for the group case, not user_id)
    assert _is_admin(fake_message(chat_id=42, user_id=42)) is False

    print("✅ _is_admin() correctly distinguishes admin vs non-admin")


async def _run_middleware(message, allowed_calls: list):
    async def handler(event, data):
        allowed_calls.append(event)
        return "handled"

    middleware = AdminOnlyMiddleware()
    return await middleware(handler, message, {})


@pytest.mark.asyncio
async def test_middleware_blocks_unauthorized():
    calls = []
    result = await _run_middleware(fake_message(chat_id=1, user_id=1), calls)
    assert result is None, "unauthorized call should be silently dropped"
    assert calls == [], "handler must never run for an unauthorized caller"
    print("✅ Middleware silently drops unauthorized admin command")


@pytest.mark.asyncio
async def test_middleware_allows_admin_group():
    calls = []
    msg = fake_message(chat_id=config.ADMIN_GROUP_ID, user_id=1)
    result = await _run_middleware(msg, calls)
    assert result == "handled"
    assert calls == [msg]
    print("✅ Middleware allows a call from the configured admin group")


@pytest.mark.asyncio
async def test_middleware_allows_whitelisted_user():
    calls = []
    msg = fake_message(chat_id=555, user_id=555)
    result = await _run_middleware(msg, calls)
    assert result == "handled"
    assert calls == [msg]
    print("✅ Middleware allows a call from a whitelisted ADMIN_USER_IDS user")


def main():
    test_is_admin_checks()
    asyncio.run(test_middleware_blocks_unauthorized())
    asyncio.run(test_middleware_allows_admin_group())
    asyncio.run(test_middleware_allows_whitelisted_user())
    print("\n✅ SUCCESS: Admin authorization gate works as expected (Issue #1 fixed)")


if __name__ == "__main__":
    main()