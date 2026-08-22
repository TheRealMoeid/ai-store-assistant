import json
from aiogram import Router, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
from utils import order_manager

router = Router()


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