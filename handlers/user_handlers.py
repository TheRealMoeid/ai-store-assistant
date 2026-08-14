from aiogram import Router, Bot, F
from aiogram.types import Message

import config
from agent.agent import run_agent

router = Router()


async def _notify_admin(bot: Bot, event: dict):
    if not config.ADMIN_GROUP_ID:
        return  # not configured yet — skip silently

    if event["type"] == "order":
        order = event["data"]
        lines = [f"🛍 New order {order['order_id']}", f"From: @{order['username']} ({order['user_id']})", ""]
        for item in order["items"]:
            lines.append(f"- {item['product_id']} | size {item['size']} | {item['color']} | x{item['quantity']}")
        await bot.send_message(config.ADMIN_GROUP_ID, "\n".join(lines))

    elif event["type"] == "feedback":
        fb = event["data"]
        emoji = "💬" if fb["kind"] == "compliment" else "⚠️"
        await bot.send_message(
            config.ADMIN_GROUP_ID,
            f"{emoji} {fb['kind'].title()} from @{fb['username']} ({fb['user_id']}):\n{fb['message']}",
        )


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
