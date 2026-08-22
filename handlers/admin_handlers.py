import json
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Router, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

import config
from utils import order_manager

logger = logging.getLogger("admin_handlers")

router = Router()


def _is_admin(message: Message) -> bool:
    """
    Authorized if the message comes from the configured admin group, OR
    from a user_id on the ADMIN_USER_IDS whitelist (e.g. an admin messaging
    the bot privately). Telegram group/supergroup chat_ids are negative;
    user_ids are positive, so there's no ambiguity between the two checks.
    """
    if config.ADMIN_GROUP_ID and message.chat.id == config.ADMIN_GROUP_ID:
        return True
    if message.from_user and message.from_user.id in config.ADMIN_USER_IDS:
        return True
    return False


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Gates every handler registered on this router. Applied once here rather
    than repeated per-handler, so a new admin command added later is safe
    by default instead of needing someone to remember the check.
    Unauthorized attempts are logged and silently dropped — no response is
    sent, so we don't confirm to a random user that admin commands exist.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not _is_admin(event):
            logger.warning(
                "Blocked admin command from unauthorized user_id=%s chat_id=%s: %r",
                event.from_user.id if event.from_user else None,
                event.chat.id,
                event.text,
            )
            return  # silently ignore — don't leak that admin commands exist
        return await handler(event, data)


router.message.middleware(AdminOnlyMiddleware())


@router.message(Command("pending_orders"))
async def pending_orders(message: Message):
    # Use the new safe, locked read function instead of direct file access
    orders = await order_manager.get_all_orders()
    
    pending = [o for o in orders if o["status"] in ("pending_confirmation", "awaiting_review")]
    if not pending:
        await message.answer("No pending orders.")
        return
    
    text = "\n\n".join(
        f"{o['order_id']} — @{o['username']} — {len(o['items'])} item(s) — {o['status']}" 
        for o in pending
    )
    await message.answer(text)


@router.message(Command("confirm"))
async def confirm_order(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Usage: /confirm <order_id>")
        return

    order_id = command.args.strip()
    order = await order_manager.set_order_status(order_id, "confirmed")
    if not order:
        await message.answer(f"No order found with id {order_id}")
        return

    await message.answer(f"✅ {order_id} confirmed.")
    await bot.send_message(
        order["user_id"],
        f"🎉 Your order {order_id} has been confirmed! We'll be in touch about delivery.",
    )


@router.message(Command("reject"))
async def reject_order(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Usage: /reject <order_id>")
        return

    order_id = command.args.strip()
    order = await order_manager.set_order_status(order_id, "rejected")
    if not order:
        await message.answer(f"No order found with id {order_id}")
        return

    await message.answer(f"❌ {order_id} rejected.")
    await bot.send_message(
        order["user_id"],
        f"Your order {order_id} couldn't be confirmed — please contact us if you believe this is a mistake.",
    )