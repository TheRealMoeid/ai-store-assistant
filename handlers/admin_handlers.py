import json
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Router, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

import config
from utils import order_manager, inventory_manager

logger = logging.getLogger("admin_handlers")

router = Router()

# Telegram caps a single message at 4096 characters. We stay well under that
# so formatting overhead (emoji, joins) never risks tripping the real limit.
MAX_MESSAGE_CHARS = 3500


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


def _format_created_at(created_at: str | None) -> str:
    if not created_at:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(created_at)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return created_at


def _format_payment_status(order: dict) -> str:
    """
    Human-readable payment state, distinguishing "no proof yet" from
    "proof submitted, waiting on the seller" and surfacing what kind of
    proof (screenshot vs typed reference) was submitted, since /confirm and
    /reject decisions hinge on this.
    """
    status = order.get("status")
    proof = order.get("payment_proof")

    if status == "pending_confirmation":
        return "⏳ Awaiting payment"
    if status == "awaiting_review":
        if isinstance(proof, str) and proof.startswith("photo:"):
            return "📸 Payment screenshot submitted — awaiting review"
        if proof:
            return f'📎 Payment reference submitted ("{proof}") — awaiting review'
        return "📎 Payment proof submitted — awaiting review"
    return status or "unknown status"


def _format_order_detail(order: dict) -> str:
    """
    Renders one order as a multi-line block: id/status, who placed it and
    when, every line item (resolved to a product name/size/color/quantity
    rather than a bare count), and an estimated total from current
    inventory prices. Previously /pending_orders only showed an item
    *count*, forcing the seller to open orders.json by hand to see what was
    actually ordered — see Issue #5.
    """
    lines = [
        f"🧾 {order.get('order_id')} — {_format_payment_status(order)}",
        f"From: @{order.get('username') or 'unknown'} (user_id: {order.get('user_id')})",
        f"Placed: {_format_created_at(order.get('created_at'))}",
        "Items:",
    ]

    items = order.get("items", [])
    total = 0.0
    total_known = bool(items)

    for item in items:
        product = inventory_manager.get_product_by_id(item.get("product_id", ""))
        qty = item.get("quantity", 0)
        size = item.get("size")
        color = item.get("color")

        if not product:
            # Product may have been deleted/renamed since the order was
            # placed (same edge case restore_stock_for_order already
            # handles) — fall back to the raw id instead of failing.
            total_known = False
            lines.append(f"  • {item.get('product_id')} (no longer in inventory) — {size}/{color} × {qty}")
            continue

        name = product.get("name", item.get("product_id"))
        price = product.get("price")
        if isinstance(price, (int, float)):
            subtotal = price * qty
            total += subtotal
            lines.append(
                f"  • {name} — {size}/{color} × {qty} (≈{price:,.0f} each, ≈{subtotal:,.0f} subtotal)"
            )
        else:
            total_known = False
            lines.append(f"  • {name} — {size}/{color} × {qty}")

    if total_known:
        lines.append(f"Est. total (current prices): ≈{total:,.0f}")

    return "\n".join(lines)


def _chunk_order_blocks(blocks: list[str]) -> list[str]:
    """
    Groups formatted order blocks into messages that stay under Telegram's
    character limit, instead of one giant message that could get rejected
    or truncated once there are enough pending orders.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + 2  # + the "\n\n" separator between blocks
        if current and current_len + block_len > MAX_MESSAGE_CHARS:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


@router.message(Command("pending_orders"))
async def pending_orders(message: Message):
    # Use the new safe, locked read function instead of direct file access
    orders = await order_manager.get_all_orders()

    pending = [o for o in orders if o["status"] in ("pending_confirmation", "awaiting_review")]
    if not pending:
        await message.answer("No pending orders.")
        return

    blocks = [_format_order_detail(o) for o in pending]
    for chunk in _chunk_order_blocks(blocks):
        await message.answer(chunk)


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

    # Atomic check-and-set: only the call that actually flips the status to
    # "rejected" is allowed to restore stock. A second /reject on an
    # already-rejected order (double tap, retry, etc.) is a safe no-op
    # instead of crediting stock back twice. See Issue #4.
    order, transitioned = await order_manager.set_order_status_if_not(order_id, "rejected", "rejected")
    if not order:
        await message.answer(f"No order found with id {order_id}")
        return

    if not transitioned:
        await message.answer(f"{order_id} was already rejected — no changes made.")
        return

    restore_result = await inventory_manager.restore_stock_for_order(order_id)

    reply_lines = [f"❌ {order_id} rejected."]
    if restore_result.get("error"):
        # Order existed a moment ago (we just transitioned its status) so this
        # shouldn't normally happen, but don't let a lookup hiccup hide the
        # fact that stock wasn't restored.
        logger.warning("Stock restoration failed for %s: %s", order_id, restore_result["error"])
        reply_lines.append(f"⚠️ Could not restore stock: {restore_result['error']}")
    else:
        for item in restore_result.get("skipped", []):
            reply_lines.append(
                f"⚠️ Could not restore stock for {item.get('product_id')} "
                f"({item.get('size')}/{item.get('color')}) — that variant no longer "
                f"exists in inventory. Please adjust stock manually if needed."
            )

    await message.answer("\n".join(reply_lines))
    await bot.send_message(
        order["user_id"],
        f"Your order {order_id} couldn't be confirmed — please contact us if you believe this is a mistake.",
    )