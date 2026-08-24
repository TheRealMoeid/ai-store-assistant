# test_pending_orders_detail.py
"""
Covers Issue #5: /pending_orders only ever showed an order_id, @username,
a bare item *count*, and status — the seller had to open orders.json by
hand to see what was actually ordered before deciding /confirm or /reject.

`_format_order_detail()` now renders each order's actual line items
(product name, size, color, quantity), when it was placed, a payment-proof
status distinguishing "no proof yet" from "proof submitted, awaiting
review" (and screenshot vs typed reference), and an estimated total from
*current* inventory prices (explicitly not a historical price snapshot,
since orders.json doesn't store price-at-purchase — see CLAUDE.md's note
on Approach B being out of scope for this issue).

`_chunk_order_blocks()` is covered separately so a seller with many
pending orders gets several messages instead of one that could exceed
Telegram's 4096-character limit.

Sandboxes config paths to test_data/, matching test_stock_reversal.py and
test/test_backend.py, so real data/ is never touched. Uses the same
lightweight FakeMessage double as test_stock_reversal.py / test_admin_auth.py
rather than spinning up a real aiogram Dispatcher.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncio
import json

import pytest

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.ORDERS_FILE = TEST_DATA_DIR / "orders.json"
config.FEEDBACK_FILE = TEST_DATA_DIR / "feedback.json"
config.CARTS_FILE = TEST_DATA_DIR / "carts.json"
config.ADMIN_GROUP_ID = -1009999999

from utils import inventory_manager, order_manager
from handlers.admin_handlers import (
    pending_orders,
    _format_order_detail,
    _format_payment_status,
    _chunk_order_blocks,
    MAX_MESSAGE_CHARS,
)


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def answer(self, text: str):
        self.replies.append(text)


TWO_PRODUCT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers", "price": 4200000,
            "variants": [{"size": "42", "color": "black", "stock": 5}],
        },
        {
            "id": "p002", "name": "Test Hoodie", "category": "clothing", "price": 850000,
            "variants": [{"size": "M", "color": "cream", "stock": 5}],
        },
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(TWO_PRODUCT_INVENTORY, f)
    with open(config.ORDERS_FILE, "w") as f:
        json.dump([], f)
    with open(config.FEEDBACK_FILE, "w") as f:
        json.dump([], f)
    with open(config.CARTS_FILE, "w") as f:
        json.dump({}, f)

    inventory_manager._cache["mtime"] = None

    yield

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()


async def _seed_order(user_id: int, username: str, items: list[dict], status: str = "pending_confirmation", proof=None) -> dict:
    order = await order_manager.place_order(user_id, username, items)
    if status != "pending_confirmation" or proof is not None:
        records = order_manager._load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order["order_id"]:
                o["status"] = status
                o["payment_proof"] = proof
        order_manager._save(config.ORDERS_FILE, records)
        order = order_manager.get_order(order["order_id"])
    return order


# --- _format_order_detail() ------------------------------------------------

@pytest.mark.asyncio
async def test_format_order_detail_shows_item_name_size_color_quantity():
    """The core Issue #5 fix: real product details, not just a count."""
    order = await _seed_order(
        1, "alice",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 2}],
    )
    block = _format_order_detail(order)

    assert "Test Sneakers" in block
    assert "42/black" in block
    assert "× 2" in block
    assert order["order_id"] in block


@pytest.mark.asyncio
async def test_format_order_detail_shows_multiple_items():
    order = await _seed_order(
        2, "bob",
        [
            {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
            {"product_id": "p002", "size": "M", "color": "cream", "quantity": 3},
        ],
    )
    block = _format_order_detail(order)

    assert "Test Sneakers" in block
    assert "Test Hoodie" in block
    assert "× 1" in block
    assert "× 3" in block


@pytest.mark.asyncio
async def test_format_order_detail_includes_timestamp_and_user_id():
    order = await _seed_order(
        42, "carol",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}],
    )
    block = _format_order_detail(order)

    assert "user_id: 42" in block
    assert "Placed:" in block
    # created_at is a real ISO timestamp set by order_manager.place_order
    assert order["created_at"][:4] in block  # the year shows up in the formatted date


@pytest.mark.asyncio
async def test_format_order_detail_shows_estimated_total():
    order = await _seed_order(
        3, "dave",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 2}],  # 4,200,000 * 2
    )
    block = _format_order_detail(order)

    assert "Est. total" in block
    assert "8,400,000" in block


@pytest.mark.asyncio
async def test_format_order_detail_handles_deleted_product_gracefully():
    """
    If the seller removes/renames a product after the order was placed,
    formatting must fall back to the raw product_id instead of crashing —
    mirrors the same edge case already handled in restore_stock_for_order.
    """
    order = await _seed_order(
        4, "erin",
        [{"product_id": "p_deleted", "size": "9", "color": "red", "quantity": 1}],
    )
    block = _format_order_detail(order)

    assert "p_deleted" in block
    assert "no longer in inventory" in block
    # No total should be claimed when a line item's price is unknown.
    assert "Est. total" not in block


# --- _format_payment_status() ----------------------------------------------

@pytest.mark.asyncio
async def test_payment_status_pending_confirmation():
    order = await _seed_order(5, "frank", [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}])
    assert "Awaiting payment" in _format_payment_status(order)


@pytest.mark.asyncio
async def test_payment_status_awaiting_review_with_screenshot():
    order = await _seed_order(
        6, "gina",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}],
        status="awaiting_review", proof="photo:AgAC123",
    )
    status_text = _format_payment_status(order)
    assert "screenshot" in status_text.lower()
    assert "awaiting review" in status_text.lower()


@pytest.mark.asyncio
async def test_payment_status_awaiting_review_with_reference():
    order = await _seed_order(
        7, "hank",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}],
        status="awaiting_review", proof="TX998877",
    )
    status_text = _format_payment_status(order)
    assert "TX998877" in status_text
    assert "awaiting review" in status_text.lower()


# --- _chunk_order_blocks() --------------------------------------------------

def test_chunk_order_blocks_single_chunk_for_small_input():
    blocks = ["short block one", "short block two"]
    chunks = _chunk_order_blocks(blocks)
    assert len(chunks) == 1
    assert "short block one" in chunks[0]
    assert "short block two" in chunks[0]


def test_chunk_order_blocks_splits_when_over_limit():
    # Each block is ~2000 chars; two of them exceed MAX_MESSAGE_CHARS (3500)
    # combined, so they must land in separate chunks.
    big_block = "x" * 2000
    chunks = _chunk_order_blocks([big_block, big_block, big_block])
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS + 4  # small slack for separators/rounding


# --- pending_orders() handler end-to-end -----------------------------------

@pytest.mark.asyncio
async def test_pending_orders_no_orders_message_unchanged():
    message = FakeMessage()
    await pending_orders(message)
    assert message.replies == ["No pending orders."]


@pytest.mark.asyncio
async def test_pending_orders_excludes_confirmed_and_rejected():
    await _seed_order(8, "ivan", [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}], status="confirmed")
    await _seed_order(9, "jill", [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}], status="rejected")
    pending_order = await _seed_order(10, "kate", [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}])

    message = FakeMessage()
    await pending_orders(message)

    full_text = "\n".join(message.replies)
    assert pending_order["order_id"] in full_text
    assert "ivan" not in full_text
    assert "jill" not in full_text


@pytest.mark.asyncio
async def test_pending_orders_end_to_end_detail_present():
    order = await _seed_order(
        11, "leo",
        [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}],
        status="awaiting_review", proof="TX555",
    )

    message = FakeMessage()
    await pending_orders(message)

    assert len(message.replies) == 1
    text = message.replies[0]
    assert order["order_id"] in text
    assert "Test Sneakers" in text
    assert "user_id: 11" in text
    assert "TX555" in text


@pytest.mark.asyncio
async def test_pending_orders_chunks_across_multiple_messages_when_many_orders():
    # 25 orders, each with 3 line items -> comfortably exceeds MAX_MESSAGE_CHARS
    # once formatted, so this must produce more than one message.answer call.
    for i in range(25):
        await _seed_order(
            100 + i, f"user_{i}",
            [
                {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
                {"product_id": "p002", "size": "M", "color": "cream", "quantity": 2},
            ],
        )

    message = FakeMessage()
    await pending_orders(message)

    assert len(message.replies) > 1, "expected pending orders to be split across multiple messages"
    for reply in message.replies:
        assert len(reply) <= 4096, "a single Telegram message must never exceed the API's hard limit"

    # Every order_id must still show up somewhere across the chunks.
    full_text = "\n".join(message.replies)
    all_orders = await order_manager.get_all_orders()
    for o in all_orders:
        assert o["order_id"] in full_text


def main():
    """Lets this be run directly with `python test_pending_orders_detail.py`,
    matching the style of the other root-level test_*.py files in this project."""
    import inspect

    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]
    sync_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn)
    ]

    passed = 0
    total = len(test_fns) + len(sync_test_fns)

    for name, fn in sync_test_fns:
        try:
            fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for name, fn in test_fns:
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(TWO_PRODUCT_INVENTORY, f)
        with open(config.ORDERS_FILE, "w") as f:
            json.dump([], f)
        with open(config.FEEDBACK_FILE, "w") as f:
            json.dump([], f)
        with open(config.CARTS_FILE, "w") as f:
            json.dump({}, f)
        inventory_manager._cache["mtime"] = None
        try:
            asyncio.run(fn())
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{total} passed")


if __name__ == "__main__":
    main()
