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
        # Expected, normal shutdown path once the agent call finishes --
        # not an error, just means the reply is ready and the indicator
        # no longer needs refreshing.
        raise
    except Exception:
        # A transient Telegram API hiccup on the typing indicator itself
        # must never take down the real reply the customer is waiting on.
        # Logged, not re-raised -- a single failed refresh just means one
        # missed "still typing" pulse, not a reason to abort the whole
        # background task (the while loop keeps retrying on the next tick).
        logger.warning("Typing indicator refresh failed for chat_id=%s", chat_id, exc_info=True)


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
        # Issue 4.2 (Option B): flag resubmissions so the seller knows the
        # customer replaced a previously submitted proof and should look again.
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
    """
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    # order_manager.get_latest_unpaid_order is now async + locked (Issue #16)
    # -- was a plain synchronous unlocked read.
    order = await order_manager.get_latest_unpaid_order(user_id)
    if not order:
        await message.answer(
            "I don't see an order waiting on payment for you right now -- "
            "if you'd like to order something first, just tell me what you're looking for!"
        )
        return

    # Largest photo size is last in the list
    file_id = message.photo[-1].file_id
    updated = await order_manager.attach_payment_proof(order["order_id"], f"photo:{file_id}")

    # Issue 4.2 (Option B): a proof attached to an order already in
    # awaiting_review is a resubmission -- the customer is replacing a
    # previously submitted proof, and the seller should be told to look again.
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

    # Rate limiting: checked BEFORE any LLM call is made, so a throttled
    # user's message never spends agent/LLM budget at all -- the whole
    # point is to avoid the spend, not spend it and apologize after. See
    # utils/rate_limiter.py for the sliding-window algorithm and why this
    # state is deliberately in-memory rather than persisted to disk.
    allowed = await rate_limiter.check_rate_limit(user_id)
    if not allowed:
        await message.answer(
            "You're sending messages a bit fast -- give it a few seconds and try again!"
        )
        return

    # Keep the "typing..." indicator alive for the full duration of the
    # agent call, not just a single send at the start. Telegram's typing
    # indicator expires after ~5s on its own; a turn that legitimately
    # takes tens of seconds (slow/throttled network path, a multi-round
    # tool-calling turn, a large local model) would otherwise show
    # "typing..." for a few seconds and then silently go quiet while the
    # customer is left wondering if the bot is still there. The refresh
    # task is cancelled in `finally` so it never outlives the real reply,
    # whether run_agent succeeds or raises.
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
        await _notify_admin(bot, event)