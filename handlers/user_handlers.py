from aiogram import Router, Bot, F
from aiogram.types import Message

import config
from agent.agent import run_agent
from utils import order_manager

router = Router()


async def _notify_admin(bot: Bot, event: dict):
    if not config.ADMIN_GROUP_ID:
        return  # not configured yet — skip silently

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
                "proof — please review the latest one.",
            ]
        await bot.send_message(config.ADMIN_GROUP_ID, "\n".join(lines))


@router.message(F.photo)
async def handle_payment_screenshot(message: Message, bot: Bot):
    """
    Payment screenshots are handled directly here, not routed through the LLM —
    file/image handling doesn't need an agent decision, and this is more
    reliable than hoping the model recognizes an image message correctly.
    """
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    order = order_manager.get_latest_unpaid_order(user_id)
    if not order:
        await message.answer(
            "I don't see an order waiting on payment for you right now — "
            "if you'd like to order something first, just tell me what you're looking for!"
        )
        return

    # Largest photo size is last in the list
    file_id = message.photo[-1].file_id
    updated = await order_manager.attach_payment_proof(order["order_id"], f"photo:{file_id}")

    # Issue 4.2 (Option B): a proof attached to an order already in
    # awaiting_review is a resubmission — the customer is replacing a
    # previously submitted proof, and the seller should be told to look again.
    is_resubmission = updated is not None and len(updated.get("payment_proof_history", [])) > 1

    if is_resubmission:
        await message.answer(
            f"Got your updated payment screenshot for order {order['order_id']} — "
            "it replaces the previous one you sent, and I've flagged the update to the "
            "seller. You'll hear back once it's reviewed!"
        )
    else:
        await message.answer(
            f"Got your payment screenshot for order {order['order_id']} — "
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
                "proof — please review the latest one."
            )
        await bot.send_photo(config.ADMIN_GROUP_ID, file_id, caption=caption)


@router.message(F.text)
async def handle_message(message: Message, bot: Bot):
    await bot.send_chat_action(message.chat.id, "typing")

    reply, events = await run_agent(
        user_id=message.from_user.id,
        username=message.from_user.username or message.from_user.full_name,
        user_message=message.text,
    )

    await message.answer(reply)

    for event in events:
        await _notify_admin(bot, event)