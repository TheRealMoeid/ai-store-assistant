# test/test_order_status.py
"""
Covers the new order-status tail (shipped/delivered) added on top of the
already-hardened pending_confirmation/awaiting_review/confirmed/rejected
lifecycle -- "Not built yet" item 2 (2026-11-08, Moeid's prioritized
backlog, after item 1's rate limiting landed).

Three layers:
  1. order_manager.advance_order_status() -- the forward-only transition
     guard itself (confirmed -> shipped -> delivered, no skipping, no
     double-advancing, no acting on an order in the wrong state).
  2. order_manager.get_latest_order_for_user() -- the unfiltered-by-status
     lookup that check_order_status needs (unlike get_latest_unpaid_order,
     which only matches unpaid orders).
  3. handlers/admin_handlers.py::ship_order/deliver_order -- the actual
     /ship and /deliver commands end-to-end, plus _format_payment_status's
     new shipped/delivered labels.
  4. agent/tools.py::_check_order_status -- the new customer-facing tool,
     including the cross-user ownership check (a customer must not be able
     to look up another customer's order by passing/guessing its id).

Sandboxes config paths to a throwaway test_data/ directory, same pattern
as test_stock_reversal.py / test_payment_resubmission.py, so real data/ is
never touched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from handlers.admin_handlers import ship_order, deliver_order, _format_payment_status


ONE_PRODUCT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 10}],
        }
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(ONE_PRODUCT_INVENTORY, f)
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


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def answer(self, text: str):
        self.replies.append(text)


class FakeCommand:
    def __init__(self, args: str | None):
        self.args = args


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


async def _place_and_confirm(user_id: int, username: str = "user") -> dict:
    """Helper: gets a real order into 'confirmed' status via the real
    business logic, so /ship has something legal to act on."""
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        user_id, username,
    )
    assert res["status"] == "added", res
    res = await call_tool("place_order", {}, user_id, username)
    assert res["status"] == "order_placed", res
    order = res["order"]
    confirmed = await order_manager.set_order_status(order["order_id"], "confirmed")
    return confirmed


# --- order_manager.advance_order_status() -- the transition guard itself --

@pytest.mark.asyncio
async def test_confirmed_to_shipped_succeeds():
    order = await _place_and_confirm(user_id=1)
    updated, error = await order_manager.advance_order_status(order["order_id"], "shipped")
    assert error is None
    assert updated["status"] == "shipped"


@pytest.mark.asyncio
async def test_shipped_to_delivered_succeeds():
    order = await _place_and_confirm(user_id=2)
    await order_manager.advance_order_status(order["order_id"], "shipped")
    updated, error = await order_manager.advance_order_status(order["order_id"], "delivered")
    assert error is None
    assert updated["status"] == "delivered"


@pytest.mark.asyncio
async def test_cannot_ship_an_order_still_awaiting_payment():
    """Core guard case: an order sitting in pending_confirmation (never
    confirmed) must not be shippable."""
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        3, "user",
    )
    res = await call_tool("place_order", {}, 3, "user")
    order = res["order"]
    assert order["status"] == "pending_confirmation"

    updated, error = await order_manager.advance_order_status(order["order_id"], "shipped")
    assert error is not None
    assert "pending_confirmation" in error
    # Status must be completely unchanged.
    reloaded = await order_manager.get_order(order["order_id"])
    assert reloaded["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_cannot_skip_shipped_straight_to_delivered():
    """A confirmed order that was never shipped must not jump straight to
    delivered -- the transition chain is exactly one step at a time."""
    order = await _place_and_confirm(user_id=4)
    updated, error = await order_manager.advance_order_status(order["order_id"], "delivered")
    assert error is not None
    assert "confirmed" in error  # names the required predecessor ("shipped")
    reloaded = await order_manager.get_order(order["order_id"])
    assert reloaded["status"] == "confirmed"


@pytest.mark.asyncio
async def test_double_ship_is_refused_not_silently_reapplied():
    """Mirrors /reject's idempotency guarantee: a second advance to
    'shipped' on an already-shipped order must be refused with a clear
    reason, not silently treated as a no-op success."""
    order = await _place_and_confirm(user_id=5)
    first, err1 = await order_manager.advance_order_status(order["order_id"], "shipped")
    assert err1 is None

    second, err2 = await order_manager.advance_order_status(order["order_id"], "shipped")
    assert err2 is not None
    assert "shipped" in err2


@pytest.mark.asyncio
async def test_advance_order_status_order_not_found():
    order, error = await order_manager.advance_order_status("ord_does_not_exist", "shipped")
    assert order is None


# --- order_manager.get_latest_order_for_user() -----------------------------

@pytest.mark.asyncio
async def test_get_latest_order_for_user_returns_most_recent_regardless_of_status():
    order1 = await _place_and_confirm(user_id=6)
    await order_manager.advance_order_status(order1["order_id"], "shipped")

    # A second, newer order for the same user, left pending.
    await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        6, "user",
    )
    res2 = await call_tool("place_order", {}, 6, "user")
    order2 = res2["order"]

    latest = await order_manager.get_latest_order_for_user(6)
    assert latest["order_id"] == order2["order_id"]


@pytest.mark.asyncio
async def test_get_latest_order_for_user_none_when_no_orders():
    assert await order_manager.get_latest_order_for_user(999) is None


# --- handlers/admin_handlers.py: /ship and /deliver end-to-end ------------

@pytest.mark.asyncio
async def test_ship_order_handler_success():
    order = await _place_and_confirm(user_id=10)

    message, bot = FakeMessage(), FakeBot()
    await ship_order(message, FakeCommand(order["order_id"]), bot)

    assert any("shipped" in r.lower() for r in message.replies)
    updated = await order_manager.get_order(order["order_id"])
    assert updated["status"] == "shipped"
    # Customer gets DM'd.
    assert bot.sent and bot.sent[0][0] == order["user_id"]
    assert "shipped" in bot.sent[0][1].lower()


@pytest.mark.asyncio
async def test_ship_order_handler_refuses_unconfirmed_order():
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        11, "user",
    )
    res = await call_tool("place_order", {}, 11, "user")
    order = res["order"]  # still pending_confirmation

    message, bot = FakeMessage(), FakeBot()
    await ship_order(message, FakeCommand(order["order_id"]), bot)

    assert any("can't mark" in r.lower() for r in message.replies)
    assert bot.sent == []  # no customer DM for a refused transition
    updated = await order_manager.get_order(order["order_id"])
    assert updated["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_ship_order_handler_unknown_order():
    message, bot = FakeMessage(), FakeBot()
    await ship_order(message, FakeCommand("ord_nonexistent"), bot)
    assert any("no order found" in r.lower() for r in message.replies)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_ship_order_handler_missing_args():
    message, bot = FakeMessage(), FakeBot()
    await ship_order(message, FakeCommand(None), bot)
    assert any("usage" in r.lower() for r in message.replies)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_deliver_order_handler_success():
    order = await _place_and_confirm(user_id=12)
    await order_manager.advance_order_status(order["order_id"], "shipped")

    message, bot = FakeMessage(), FakeBot()
    await deliver_order(message, FakeCommand(order["order_id"]), bot)

    assert any("delivered" in r.lower() for r in message.replies)
    updated = await order_manager.get_order(order["order_id"])
    assert updated["status"] == "delivered"
    assert bot.sent and "delivered" in bot.sent[0][1].lower()


@pytest.mark.asyncio
async def test_deliver_order_handler_refuses_unshipped_order():
    order = await _place_and_confirm(user_id=13)  # confirmed, never shipped

    message, bot = FakeMessage(), FakeBot()
    await deliver_order(message, FakeCommand(order["order_id"]), bot)

    assert any("can't mark" in r.lower() for r in message.replies)
    assert bot.sent == []
    updated = await order_manager.get_order(order["order_id"])
    assert updated["status"] == "confirmed"


@pytest.mark.asyncio
async def test_double_ship_handler_is_a_clean_refusal_not_a_duplicate_dm():
    order = await _place_and_confirm(user_id=14)

    message1, bot1 = FakeMessage(), FakeBot()
    await ship_order(message1, FakeCommand(order["order_id"]), bot1)
    assert len(bot1.sent) == 1

    message2, bot2 = FakeMessage(), FakeBot()
    await ship_order(message2, FakeCommand(order["order_id"]), bot2)
    assert any("can't mark" in r.lower() for r in message2.replies)
    assert bot2.sent == [], "a refused double-/ship must not send a second customer DM"


# --- _format_payment_status: new shipped/delivered labels ------------------

def test_format_payment_status_shipped():
    assert "📦" in _format_payment_status({"status": "shipped", "payment_proof": None})
    assert "shipped" in _format_payment_status({"status": "shipped", "payment_proof": None}).lower()


def test_format_payment_status_delivered():
    assert "delivered" in _format_payment_status({"status": "delivered", "payment_proof": None}).lower()


def test_format_payment_status_existing_statuses_unaffected():
    """Regression: pending_confirmation/awaiting_review formatting, already
    covered by test_pending_orders_detail.py, must be completely untouched
    by this additive change."""
    assert "Awaiting payment" in _format_payment_status({"status": "pending_confirmation", "payment_proof": None})
    assert "awaiting review" in _format_payment_status({"status": "awaiting_review", "payment_proof": "TX1"}).lower()


# --- agent/tools.py: check_order_status tool --------------------------------

@pytest.mark.asyncio
async def test_check_order_status_no_order_id_returns_latest():
    order = await _place_and_confirm(user_id=20)
    await order_manager.advance_order_status(order["order_id"], "shipped")

    res = await call_tool("check_order_status", {}, 20, "user")
    assert res["order_id"] == order["order_id"]
    assert res["status"] == "shipped"


@pytest.mark.asyncio
async def test_check_order_status_specific_order_id_owned_by_customer():
    order = await _place_and_confirm(user_id=21)

    res = await call_tool("check_order_status", {"order_id": order["order_id"]}, 21, "user")
    assert res["order_id"] == order["order_id"]
    assert res["status"] == "confirmed"


@pytest.mark.asyncio
async def test_check_order_status_cannot_see_another_customers_order():
    """Security case: a customer (or a hallucinating model) passing an
    order_id that belongs to someone ELSE must get a clean 'not found'
    style error, never that other customer's real order data."""
    victim_order = await _place_and_confirm(user_id=22, username="victim")

    res = await call_tool("check_order_status", {"order_id": victim_order["order_id"]}, 23, "attacker")
    assert "error" in res
    assert "status" not in res
    assert "items" not in res


@pytest.mark.asyncio
async def test_check_order_status_no_orders_at_all():
    res = await call_tool("check_order_status", {}, 999, "nobody")
    assert "error" in res


@pytest.mark.asyncio
async def test_check_order_status_nonexistent_order_id():
    res = await call_tool("check_order_status", {"order_id": "ord_does_not_exist"}, 24, "user")
    assert "error" in res


def main():
    """Lets this be run directly with `python test/test_order_status.py`,
    matching the style of the other test_*.py files in this project."""
    import inspect

    async_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]
    sync_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn)
    ]

    passed = 0
    total = len(async_test_fns) + len(sync_test_fns)

    def _reset_data():
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(ONE_PRODUCT_INVENTORY, f)
        with open(config.ORDERS_FILE, "w") as f:
            json.dump([], f)
        with open(config.FEEDBACK_FILE, "w") as f:
            json.dump([], f)
        with open(config.CARTS_FILE, "w") as f:
            json.dump({}, f)
        inventory_manager._cache["mtime"] = None

    for name, fn in sync_test_fns:
        _reset_data()
        try:
            fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for name, fn in async_test_fns:
        _reset_data()
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
