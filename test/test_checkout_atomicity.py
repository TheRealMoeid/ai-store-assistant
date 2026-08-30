# test/test_checkout_atomicity.py
"""
Covers Issue #10 (bug-audit-evaluation.md numbering: 3.1): "No atomic
cart-consume across `_place_order`."

The bug: `cart_manager.get_cart`/`set_cart` each acquire and release
`_carts_lock` independently. The full `_place_order` sequence (read cart ->
validate stock -> decrement stock -> create order -> clear cart) was not
wrapped in any single critical section. Two near-simultaneous `place_order`
calls for the *same* user (double-tap on "checkout", a client retry, etc.)
could both read the same cart before either cleared it, both pass
validation, and each independently decrement stock and create its own
order — a duplicate order (and doubled stock loss) for what the customer
intended as a single checkout.

The fix (Approach D, chosen over an "atomically consume-then-restore"
design specifically to avoid a new data-loss race where a concurrent
add_to_cart during the restore window would be silently overwritten — see
CLAUDE.md / bug-audit-evaluation-discussion.md for the full comparison):
a single module-level `_checkout_lock = asyncio.Lock()` in `agent/tools.py`,
held for the *entire* `_place_order` body. The cart is only ever cleared as
the very last step, after order creation has already succeeded, so there is
no window where a partially-completed checkout has already mutated the
cart — nothing to restore, and no concurrent `add_to_cart` write can be
lost to a compensating rollback that doesn't exist.

Sandboxes `config` paths to `test_data/`, matching the pattern already used
by `test/test_backend.py`, `test/test_multi_item_checkout.py`, and
`test/test_stock_reversal.py`, so real `data/` is never touched. Drives the
fix entirely through the real `agent.tools.call_tool` public surface
(`add_to_cart` -> concurrent `place_order` calls), not by calling
`_checkout_lock`/internals directly, since the bug (and the fix) lives in
how `_place_order` orchestrates the existing, already-correct
`cart_manager`/`inventory_manager`/`order_manager` primitives.
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

from utils import inventory_manager, order_manager, cart_manager
from agent.tools import call_tool


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


def _set_stock(quantity: int):
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    inv["products"][0]["variants"][0]["stock"] = quantity
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(inv, f)
    inventory_manager._cache["mtime"] = None


def _stock() -> int:
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    return inv["products"][0]["variants"][0]["stock"]


async def _seed_cart(user_id: int, quantity: int = 1):
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": quantity},
        user_id, "user",
    )
    assert res["status"] == "added", res


def _widen_race_window(monkeypatch):
    """
    CPython's asyncio.Lock.acquire() returns immediately (no real yield to
    the event loop) whenever the lock is uncontended. Since every lock in
    this codebase (_carts_lock, _inventory_lock, _orders_lock, and — post-fix
    — _checkout_lock) starts uncontended, a single `_place_order` call can in
    practice run start-to-finish in one uninterrupted burst even WITHOUT the
    fix, because none of its awaits ever genuinely suspend. That means
    plain `asyncio.gather()` alone can pass even against the pre-fix code for
    the wrong reason (no real interleaving ever happens), the same gap this
    project already hit with conversation_manager (Issue 3.3 needed a
    separate thread-pool repro to actually prove that race — see CLAUDE.md).

    To make the race window real and reliably exercised inside a single
    asyncio event loop, this patches `cart_manager.get_cart` (the read at
    the very top of `_place_order`, common to every concurrent caller) to
    insert one genuine `await asyncio.sleep(0)` after the read returns. That
    forces the event loop to actually interleave the 50 gathered calls at
    that point, instead of letting one run to completion uninterrupted —
    reliably reproducing the pre-fix duplicate-order race, and confirming
    the fix's `_checkout_lock` closes it even under real interleaving.
    """
    import agent.tools as tools_module
    original_get_cart = cart_manager.get_cart

    async def delayed_get_cart(user_id):
        cart = await original_get_cart(user_id)
        await asyncio.sleep(0)
        return cart

    monkeypatch.setattr(tools_module.cart_manager, "get_cart", delayed_get_cart)

    # `_checkout_lock` is a module-level singleton created once at import
    # time. CPython's asyncio.Lock only binds itself to a specific running
    # loop the first time it's genuinely contended (its fast, uncontended
    # path never touches the loop at all) — see asyncio.mixins._LoopBoundMixin.
    # pytest-asyncio gives each test function its own fresh event loop by
    # default, so if an EARLIER test in this file drives real contention on
    # the shared lock, it binds to that test's (now-closed) loop, and any
    # LATER test that also drives real contention on the same object raises
    # "is bound to a different event loop" — purely a test-isolation
    # artifact of sharing one lock object across independently-looped tests,
    # not a real bug: the actual bot process has exactly one long-lived
    # event loop for its whole lifetime, so this never occurs in production.
    # Giving each concurrency test its own fresh Lock instance sidesteps it
    # cleanly without needing any pytest.ini/event-loop-scope configuration.
    monkeypatch.setattr(tools_module, "_checkout_lock", asyncio.Lock())


# --- core Issue #10 case --------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_place_order_same_user_creates_exactly_one_order(monkeypatch):
    """
    Core case: 50 concurrent place_order calls for the SAME user, same cart,
    stock only sufficient for one order. Before the fix, all (or many) of
    these could read the same not-yet-cleared cart, pass validation, and
    each create their own order + decrement stock — massive duplication.
    After the fix, the checkout lock serializes them: exactly one succeeds
    ("order_placed"), and every other call sees the cart already emptied by
    the winner and gets the existing "cart is empty" error — no duplicate
    orders, no duplicate stock loss.

    Uses _widen_race_window() to force genuine interleaving between the
    gathered calls — see that helper's docstring for why plain
    asyncio.gather() alone isn't sufficient to exercise this race.
    """
    _widen_race_window(monkeypatch)
    _set_stock(1)
    await _seed_cart(user_id=1, quantity=1)

    results = await asyncio.gather(*[
        call_tool("place_order", {}, 1, "user") for _ in range(50)
    ])

    successes = [r for r in results if r.get("status") == "order_placed"]
    empty_cart_errors = [r for r in results if r.get("error") == "cart is empty"]

    assert len(successes) == 1, (
        f"expected exactly 1 successful order out of 50 concurrent checkout "
        f"attempts, got {len(successes)} — duplicate orders were created"
    )
    assert len(empty_cart_errors) == 49, (
        f"expected the other 49 calls to see an already-cleared cart, got "
        f"{len(empty_cart_errors)} — some call read a stale, not-yet-cleared cart"
    )

    # Exactly one order record on disk, not 50.
    all_orders = await order_manager.get_all_orders()
    assert len(all_orders) == 1

    # Stock decremented exactly once (from 1 -> 0), not 50 times into the negatives.
    assert _stock() == 0

    # Cart is empty and stays empty — no leftover/duplicated state.
    assert await cart_manager.get_cart(1) == []


@pytest.mark.asyncio
async def test_concurrent_place_order_higher_stock_still_only_one_order(monkeypatch):
    """
    Same race, but with stock comfortably higher than the cart's quantity
    (10 in stock, 2 requested), to rule out a fix that only "happens" to
    look correct because insufficient stock masks the duplication (i.e.
    later concurrent calls failing on a stock shortfall rather than on the
    intended empty-cart path). The lock itself — not incidental stock
    exhaustion — must be what prevents the duplicate orders.
    """
    _widen_race_window(monkeypatch)
    _set_stock(10)
    await _seed_cart(user_id=2, quantity=2)

    results = await asyncio.gather(*[
        call_tool("place_order", {}, 2, "user") for _ in range(50)
    ])

    successes = [r for r in results if r.get("status") == "order_placed"]
    assert len(successes) == 1

    all_orders = await order_manager.get_all_orders()
    assert len(all_orders) == 1

    # Exactly one decrement of 2 units, not 50.
    assert _stock() == 8


@pytest.mark.asyncio
async def test_concurrent_place_order_different_users_all_succeed_independently(monkeypatch):
    """
    Sanity check that the single GLOBAL lock (deliberately not per-user,
    matching the conversation_manager._history_lock precedent) still
    produces correct results for concurrent checkouts across DIFFERENT
    users — it serializes them, but every distinct user's legitimate order
    still goes through exactly once, none are dropped or merged.
    """
    _widen_race_window(monkeypatch)
    _set_stock(100)
    for uid in range(10, 15):
        await _seed_cart(uid, quantity=1)

    results = await asyncio.gather(*[
        call_tool("place_order", {}, uid, "user") for uid in range(10, 15)
    ])

    assert all(r.get("status") == "order_placed" for r in results), results

    all_orders = await order_manager.get_all_orders()
    assert len(all_orders) == 5
    ordered_user_ids = {o["user_id"] for o in all_orders}
    assert ordered_user_ids == set(range(10, 15))

    assert _stock() == 100 - 5


# --- no-restore-needed design: failure paths leave the cart untouched -----

@pytest.mark.asyncio
async def test_failed_checkout_leaves_cart_untouched_no_restore_needed():
    """
    Confirms the design rationale for holding the lock across the whole
    sequence instead of "consume-then-restore-on-failure": since the cart
    is only cleared as the very last step (after order creation has
    already succeeded), a checkout that fails validation must leave the
    cart exactly as it was — no clear-then-restore round trip happened at
    all, so there's no window where that restore could have raced with (and
    clobbered) a concurrent add_to_cart.
    """
    _set_stock(1)
    await _seed_cart(user_id=3, quantity=1)

    # Oversell the variant after it's already in the cart, forcing the
    # shortfall path inside the lock.
    _set_stock(0)

    res = await call_tool("place_order", {}, 3, "user")
    assert "error" in res

    # Cart must be completely untouched — still exactly what was added.
    cart = await cart_manager.get_cart(3)
    assert cart == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]

    # Restock (simulating the seller replenishing) so a follow-up add_to_cart
    # can be evaluated on its own terms rather than hitting an unrelated
    # insufficient-stock error.
    _set_stock(5)

    # A concurrent add_to_cart-style follow-up still works normally against
    # that same, never-cleared cart.
    res2 = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        3, "user",
    )
    assert res2["status"] == "already_in_cart"
    assert res2["cart"] == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 2}]


@pytest.mark.asyncio
async def test_single_checkout_regression_unaffected():
    """Plain single (non-concurrent) checkout must behave exactly as
    before — the lock shouldn't change normal, uncontended behavior."""
    _set_stock(5)
    await _seed_cart(user_id=4, quantity=2)

    res = await call_tool("place_order", {}, 4, "user")
    assert res["status"] == "order_placed"
    assert res["order"]["items"] == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 2}]

    assert _stock() == 3
    assert await cart_manager.get_cart(4) == []


class _Monkeypatch:
    """Minimal stand-in for pytest's `monkeypatch` fixture, for direct
    `python test/test_checkout_atomicity.py` execution — same shim already
    used by test_leaked_json_recovery.py and test_narration_guard_loop.py
    for their own monkeypatch-taking tests."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


def main():
    """Lets this be run directly with `python test/test_checkout_atomicity.py`,
    matching the style of the other test_*.py files in this project."""
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
            json.dump(ONE_PRODUCT_INVENTORY, f)
        with open(config.ORDERS_FILE, "w") as f:
            json.dump([], f)
        with open(config.FEEDBACK_FILE, "w") as f:
            json.dump([], f)
        with open(config.CARTS_FILE, "w") as f:
            json.dump({}, f)
        inventory_manager._cache["mtime"] = None

        needs_monkeypatch = "monkeypatch" in inspect.signature(fn).parameters
        mp = _Monkeypatch() if needs_monkeypatch else None
        try:
            if needs_monkeypatch:
                asyncio.run(fn(mp))
            else:
                asyncio.run(fn())
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
        finally:
            if mp:
                mp.undo()
    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
