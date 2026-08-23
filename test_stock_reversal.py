# test_stock_reversal.py
"""
Covers Issue #4: rejecting an order via /reject must restore the stock that
was reserved (decremented) at place_order time, instead of leaving it
permanently gone.

Mirrors the isolation pattern used in test/test_backend.py: config paths are
pointed at a throwaway "test_data" directory (same directory test_backend.py
uses) so every fixture writes fresh content before each test regardless of
import order, and real data/ files are never touched.

Two layers are tested:
  1. utils.inventory_manager.restore_stock_for_order() directly — the core
     restoration logic, including the "variant no longer exists" skip path.
  2. handlers.admin_handlers.reject_order() end-to-end — the actual command
     handler, using lightweight fake aiogram objects (Message/CommandObject/
     Bot) rather than spinning up a real Dispatcher, same approach as
     test_admin_auth.py.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

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

from utils import inventory_manager, order_manager, cart_manager
from agent.tools import call_tool
from handlers.admin_handlers import reject_order


# --- fakes for driving the admin handler directly (no real Telegram/aiogram Dispatcher needed) ---

class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def answer(self, text: str):
        self.replies.append(text)


class FakeCommand:
    def __init__(self, args: str):
        self.args = args


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


ONE_VARIANT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 5}],
        }
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(ONE_VARIANT_INVENTORY, f)
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


async def _place_order(user_id: int, quantity: int = 1) -> dict:
    """Helper: adds one line item to cart and places the order, returns the order dict."""
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": quantity},
        user_id, "user",
    )
    assert res["status"] == "added", res
    res = await call_tool("place_order", {}, user_id, "user")
    assert res["status"] == "order_placed", res
    return res["order"]


def _stock() -> int:
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    return inv["products"][0]["variants"][0]["stock"]


# --- inventory_manager.restore_stock_for_order() unit tests ---

@pytest.mark.asyncio
async def test_restore_stock_after_order_placed():
    """Core Issue #4 case: placing then restoring returns stock to its original level."""
    assert _stock() == 5

    order = await _place_order(user_id=1, quantity=2)
    assert _stock() == 3  # decremented at place_order time

    result = await inventory_manager.restore_stock_for_order(order["order_id"])
    assert result["status"] == "ok"
    assert result["skipped"] == []
    assert len(result["restored"]) == 1

    assert _stock() == 5, "stock should be back to its pre-order level"


@pytest.mark.asyncio
async def test_restore_stock_multi_item_order():
    """
    Multiple line items in one order all get restored correctly.

    Note: this builds the order via order_manager.place_order() +
    inventory_manager.decrement_stock_for_cart() directly, rather than via
    the agent.tools "place_order" tool, to isolate restore_stock_for_order()
    from an unrelated pre-existing bug in agent/tools.py::_place_order --
    its decrement loop calls decrement_stock_for_cart(cart) once *per item*
    instead of once total, so a multi-item cart gets fully decremented
    multiple times. That's a real bug worth its own issue, but it's outside
    the scope of Issue #4 and orthogonal to what's being tested here.
    """
    inv = {
        "products": [
            {"id": "p001", "name": "Sneakers", "category": "sneakers",
             "variants": [{"size": "42", "color": "black", "stock": 5}]},
            {"id": "p002", "name": "Hoodie", "category": "clothing",
             "variants": [{"size": "M", "color": "cream", "stock": 4}]},
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(inv, f)
    inventory_manager._cache["mtime"] = None

    cart = [
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        {"product_id": "p002", "size": "M", "color": "cream", "quantity": 2},
    ]
    decrement_result = await inventory_manager.decrement_stock_for_cart(cart)
    assert decrement_result["status"] == "ok"
    order = await order_manager.place_order(2, "user", cart)

    with open(config.INVENTORY_FILE) as f:
        after_order = json.load(f)
    assert after_order["products"][0]["variants"][0]["stock"] == 4
    assert after_order["products"][1]["variants"][0]["stock"] == 2

    result = await inventory_manager.restore_stock_for_order(order["order_id"])
    assert len(result["restored"]) == 2
    assert result["skipped"] == []

    with open(config.INVENTORY_FILE) as f:
        after_restore = json.load(f)
    assert after_restore["products"][0]["variants"][0]["stock"] == 5
    assert after_restore["products"][1]["variants"][0]["stock"] == 4


@pytest.mark.asyncio
async def test_restore_stock_order_not_found():
    result = await inventory_manager.restore_stock_for_order("ord_does_not_exist")
    assert "error" in result


@pytest.mark.asyncio
async def test_restore_stock_skips_deleted_variant():
    """
    Simulates the seller deleting/renaming a variant (by hand-editing
    inventory.json) between order placement and rejection. Restoration for
    that line item should be skipped and reported, not crash or silently
    restore the wrong thing.
    """
    order = await _place_order(user_id=3, quantity=1)
    assert _stock() == 4

    # Simulate the seller removing the variant entirely.
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump({"products": [{"id": "p001", "name": "Test Sneakers",
                                  "category": "sneakers", "variants": []}]}, f)
    inventory_manager._cache["mtime"] = None

    result = await inventory_manager.restore_stock_for_order(order["order_id"])
    assert result["status"] == "ok"
    assert result["restored"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["product_id"] == "p001"

    # Nothing should have been written/changed since the only line item was skipped.
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    assert inv["products"][0]["variants"] == []


# --- handlers.admin_handlers.reject_order() end-to-end tests ---

@pytest.mark.asyncio
async def test_reject_order_handler_restores_stock():
    order = await _place_order(user_id=4, quantity=1)
    assert _stock() == 4

    message = FakeMessage()
    bot = FakeBot()
    await reject_order(message, FakeCommand(order["order_id"]), bot)

    assert _stock() == 5
    assert any("rejected" in r.lower() for r in message.replies)
    # No skipped-variant warning expected in the normal case.
    assert not any("⚠️" in r for r in message.replies)

    updated = order_manager.get_order(order["order_id"])
    assert updated["status"] == "rejected"

    # Customer gets DM'd.
    assert bot.sent and bot.sent[0][0] == order["user_id"]


@pytest.mark.asyncio
async def test_reject_order_handler_is_idempotent():
    """
    Rejecting the same order twice must not restore stock twice — this is
    the double-invocation gap the issue's own proposed solution left open.
    """
    order = await _place_order(user_id=5, quantity=1)
    assert _stock() == 4

    message1 = FakeMessage()
    bot1 = FakeBot()
    await reject_order(message1, FakeCommand(order["order_id"]), bot1)
    assert _stock() == 5

    message2 = FakeMessage()
    bot2 = FakeBot()
    await reject_order(message2, FakeCommand(order["order_id"]), bot2)

    # Stock must NOT have been credited a second time.
    assert _stock() == 5, "stock was restored twice on a double /reject"
    assert any("already rejected" in r.lower() for r in message2.replies)
    # No customer DM should be sent on the no-op second call.
    assert bot2.sent == []


@pytest.mark.asyncio
async def test_reject_order_handler_unknown_order():
    message = FakeMessage()
    bot = FakeBot()
    await reject_order(message, FakeCommand("ord_nonexistent"), bot)
    assert any("no order found" in r.lower() for r in message.replies)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_reject_order_handler_warns_on_skipped_variant():
    order = await _place_order(user_id=6, quantity=1)

    # Simulate the seller deleting the variant before the admin gets to /reject.
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump({"products": [{"id": "p001", "name": "Test Sneakers",
                                  "category": "sneakers", "variants": []}]}, f)
    inventory_manager._cache["mtime"] = None

    message = FakeMessage()
    bot = FakeBot()
    await reject_order(message, FakeCommand(order["order_id"]), bot)

    # Order still gets marked rejected even though stock couldn't be restored.
    updated = order_manager.get_order(order["order_id"])
    assert updated["status"] == "rejected"

    # Admin sees an explicit warning naming the affected product.
    assert any("⚠️" in r and "p001" in r for r in message.replies)


@pytest.mark.asyncio
async def test_reject_order_handler_missing_args():
    message = FakeMessage()
    bot = FakeBot()
    await reject_order(message, FakeCommand(None), bot)
    assert any("usage" in r.lower() for r in message.replies)
    assert bot.sent == []


def main():
    """Lets this be run directly with `python test_stock_reversal.py`, matching
    the style of the other root-level test_*.py files in this project."""
    import inspect
    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]
    passed = 0
    for name, fn in test_fns:
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(ONE_VARIANT_INVENTORY, f)
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
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
