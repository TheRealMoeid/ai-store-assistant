import json
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import config

router = Router()


@router.message(Command("pending_orders"))
async def pending_orders(message: Message):
    with open(config.ORDERS_FILE, "r", encoding="utf-8") as f:
        orders = json.load(f)
    pending = [o for o in orders if o["status"] == "pending_confirmation"]
    if not pending:
        await message.answer("No pending orders.")
        return
    text = "\n\n".join(
        f"{o['order_id']} — @{o['username']} — {len(o['items'])} item(s)" for o in pending
    )
    await message.answer(text)
