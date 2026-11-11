import asyncio
import logging

from aiogram import Router, Bot, F
from aiogram.types import Message

import config
from agent.agent import run_agent
from utils import order_manager, rate_limiter

logger = logging.getLogger("user_handlers")

router = Router()

# Telegram's "typing..." indicator expires after ~5 seconds -- it's not a
# persistent flag, it's a short-lived hint the client re-sends on an
# interval. A single send_chat_action() call at the start of a turn is
# fine for a fast reply, but once a turn's actual duration can run into
# tens of seconds (e.g. under a throttled/high-latency network path -- see
# CLAUDE.md's "Known environment gotchas" entry on national bandwidth
# throttling), the indicator silently disappears partway through and the
# customer is left staring at a dead chat with no signal the bot is still
# working. _TYPING_REFRESH_SECONDS re-sends it periodically for as long as
# the agent call is in flight, comfortably under Telegram's ~5s expiry.
_TYPING_REFRESH_SECONDS = 4


async def _keep_typing(bot: Bot, chat_id: int):
    """Resends the 'typing...' action every _TYPING_REFRESH_SECONDS until
    cancelled. Meant to be run as a background task alongside run_agent()
    and cancelled once the real reply is ready -- see handle_message()."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Typing indicator refresh failed for chat_id=%s", chat_id, exc_info=True)


async def _send_product_image(bot: Bot, chat_id: int, event: dict):
    """
    Sends the customer the photo for a product they just asked about in
    detail (get_product_details returned an `image` -- a Telegram file_id
    from the product-images channel, or a plain URL if the seller
    hand-edited inventory.json -- see utils/inventory_manager.py's module
    docstring; both work identically as send_photo's `photo` argument).

    Deliberately customer-facing, not routed through _notify_admin below
    -- this is the one event type that goes to the person who triggered
    the turn, not the admin group. Failure-tolerant like _keep_typing
    above: a broken/expired image (a stale file_id, an unreachable URL)
    must never crash the turn or swallow the text reply that's already
    been sent by the time this runs.
    """
    data = event["data"]
    name = data.get("name") or ""
    color = data.get("color")
    caption = f"{name} ({color})" if name and color else (name or None)
    try:
        await bot.send_photo(chat_id, data["image"], caption=caption)
    except Exception:
        logger.warning("Failed to send product image to chat_id=%s", chat_id, exc_info=True)


async def _notify_admin(bot: Bot, event: dict):
    if not config.ADMIN_GROUP_ID:
        return  # not configured yet -- skip silently

    if event["type"] == "order":
        order = event["data"]
        lines = [f"🛍 New order {order['order_id']}", f"From: @{order['username']} ({order['user_id']})", ""]
        for item in order["items"]:
            lines.append(f"- {item['product_id']} | size {item['size']} | {item['color']} | x{item['quantity']}")
        lines.append("")
        lines.append("Awaiting payment proof.")
        await bot.send_message(config.ADMIN_GROUP_ID, "\n".join(lines))

    elif event["type"] == "feedback":
        fb = event["data"]
        emoji = "💬" if fb["kind"] == "compliment" else "⚠️"
        await bot.send_message(
            config.ADMIN_GROUP_ID,
            f"{emoji} {fb['kind'].title()} from @{fb['username']} ({fb['user_id']}):\n{fb['message']}",
        )

    elif event["type"] == "payment_reference":
        order = event["data"]
        lines = [
            f"💳 Payment reference for {order['order_id']} (@{order['username']}):",
            str(order["payment_proof"]),
            "",
            f"Reply with /confirm {order['order_id']} or /reject {order['order_id']}",
        ]
        if len(order.get("payment_proof_history", [])) > 1:
            lines += [
                "",
                "⚠️ RESUBMISSION: the customer replaced a previously submitted "
                "proof -- please review the latest one.",
            ]
        await bot.send_message(config.ADMIN_GROUP_ID, "\n".join(lines))


@router.message(F.photo)
async def handle_payment_screenshot(message: Message, bot: Bot):
    """
    Payment screenshots are handled directly here, not routed through the LLM --
    file/image handling doesn't need an agent decision, and this is more
    reliable than hoping the model recognizes an image message correctly.

    Not rate-limited: photo messages never reach the LLM at all (this
    handler talks to order_manager directly), so they don't consume the
    budget per-user rate limiting exists to protect. Rate limiting is
    scoped to handle_message below, which is the only path that calls
    run_agent.

    Note: this is a `message` handler (F.photo), distinct from
    handlers/channel_images.py's `channel_post` handler -- a customer
    sending a photo in their private chat with the bot (a payment
    screenshot) and a seller posting a photo in the product-images
    channel are two different Telegram update types that never collide,
    even though both are "a photo with a caption/text" superficially.
    """
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    order = await order_manager.get_latest_unpaid_order(user_id)
    if not order:
        await message.answer(
            "I don't see an order waiting on payment for you right now -- "
            "if you'd like to order something first, just tell me what you're looking for!"
        )
        return

    file_id = message.photo[-1].file_id
    updated = await order_manager.attach_payment_proof(order["order_id"], f"photo:{file_id}")

    is_resubmission = updated is not None and len(updated.get("payment_proof_history", [])) > 1

    if is_resubmission:
        await message.answer(
            f"Got your updated payment screenshot for order {order['order_id']} -- "
            "it replaces the previous one you sent, and I've flagged the update to the "
            "seller. You'll hear back once it's reviewed!"
        )
    else:
        await message.answer(
            f"Got your payment screenshot for order {order['order_id']} -- "
            "I've sent it to the seller for confirmation. You'll hear back once it's reviewed!"
        )

    if config.ADMIN_GROUP_ID:
        caption = (
            f"💳 Payment screenshot for {order['order_id']} (@{username}, {user_id})\n\n"
            f"Reply with /confirm {order['order_id']} or /reject {order['order_id']}"
        )
        if is_resubmission:
            caption += (
                "\n\n⚠️ RESUBMISSION: the customer replaced a previously submitted "
                "proof -- please review the latest one."
            )
        await bot.send_photo(config.ADMIN_GROUP_ID, file_id, caption=caption)


@router.message(F.text)
async def handle_message(message: Message, bot: Bot):
    user_id = message.from_user.id

    allowed = await rate_limiter.check_rate_limit(user_id)
    if not allowed:
        await message.answer(
            "You're sending messages a bit fast -- give it a few seconds and try again!"
        )
        return

    typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id))
    try:
        reply, events = await run_agent(
            user_id=user_id,
            username=message.from_user.username or message.from_user.full_name,
            user_message=message.text,
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    await message.answer(reply)

    for event in events:
        if event["type"] == "product_image":
            await _send_product_image(bot, message.chat.id, event)
        else:
            await _notify_admin(bot, event)