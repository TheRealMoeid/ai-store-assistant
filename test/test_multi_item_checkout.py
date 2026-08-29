# test_multi_item_checkout.py
"""
Covers Issue #6 (bug-audit-evaluation.md numbering: 1.1): "Multi-item Cart
N-tuple Stock Decrement."

The bug: `agent/tools.py::_place_order` called
`inventory_manager.decrement_stock_for_cart(cart)` *inside* a
`for item in cart:` loop. `decrement_stock_for_cart` already iterates the
whole cart internally and decrements every line item in one atomic pass, so
wrapping it in another loop re-ran the full-cart decrement once per line
item — an N-item cart had its stock decremented N times instead of once.
Single-item carts were unaffected (the loop only ran once), which is why
this shipped unnoticed until multi-item checkouts were exercised — see
`test_stock_reversal.py`'s note about routing around this exact code path
for its own multi-item coverage.

The fix (Issue #6, Approach A) removes the wrapping loop and calls
`decrement_stock_for_cart(cart)` exactly once, matching the function's
existing atomic, whole-cart contract.

Sandboxes `config` paths to `test_data/`, matching the pattern already used
by `test/test_backend.py` and `test_stock_reversal.py`, so real `data/` is
never touched. Drives the fix through the real `agent.tools.call_tool`
public surface (search -> add_to_cart x N -> place_order), not by calling
`inventory_manager.decrement_stock_for_cart` directly, since the bug lived
in how `_place_order` called that function, not in the function itself.
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

from utils import inventory_manager, order_manager, cart_manager
from agent.tools import call_tool


TWO_PRODUCT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 10}],
        },
        {
            "id": "p002", "name": "Test Hoodie", "category": "clothing",
            "variants": [
                {"size": "M", "color": "cream", "stock": 6},
                {"size": "L", "color": "cream", "stock": 6},
            ],
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


def _stock(product_id: str, size: str, color: str) -> int:
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    product = next(p for p in inv["products"] if p["id"] == product_id)
    variant = next(v for v in product["variants"] if v["size"] == size and v["color"] == color)
    return variant["stock"]


async def _add(user_id: int, product_id: str, size: str, color: str, quantity: int):
    res = await call_tool(
        "add_to_cart",
        {"product_id": product_id, "size": size, "color": color, "quantity": quantity},
        user_id, "user",
    )
    assert res["status"] in ("added", "already_in_cart"), res
    return res


# --- core Issue #6 cases -----------------------------------------------

@pytest.mark.asyncio
async def test_multi_item_cart_different_products_decrements_exactly_once():
    """
    Core Issue #6 case: a two-product cart must decrement each product's
    stock by exactly the ordered quantity — not by (quantity * cart size).
    Before the fix, both p001 and p002 would each be decremented twice
    (once per loop iteration over the 2-item cart).
    """
    await _add(1, "p001", "42", "black", 2)
    await _add(1, "p002", "M", "cream", 3)

    res = await call_tool("place_order", {}, 1, "user")
    assert res["status"] == "order_placed", res

    assert _stock("p001", "42", "black") == 10 - 2, "p001 stock decremented incorrectly"
    assert _stock("p002", "M", "cream") == 6 - 3, "p002 stock decremented incorrectly"
    # Untouched variant/product must be completely unaffected.
    assert _stock("p002", "L", "cream") == 6


@pytest.mark.asyncio
async def test_three_item_cart_decrements_exactly_once_per_item():
    """
    Extends the core case to three line items across two products (two
    variants of the hoodie plus the sneaker), to catch any fix that
    accidentally only handles the 2-item case (e.g. an off-by-one instead
    of a full removal of the wrapping loop).
    """
    await _add(2, "p001", "42", "black", 1)
    await _add(2, "p002", "M", "cream", 2)
    await _add(2, "p002", "L", "cream", 4)

    res = await call_tool("place_order", {}, 2, "user")
    assert res["status"] == "order_placed", res

    assert _stock("p001", "42", "black") == 10 - 1
    assert _stock("p002", "M", "cream") == 6 - 2
    assert _stock("p002", "L", "cream") == 6 - 4


@pytest.mark.asyncio
async def test_single_item_cart_still_decrements_once_regression():
    """
    Regression check: single-item carts were already correct before the fix
    (the old wrapping loop only ran once), so the fix must not change that
    behavior.
    """
    await _add(3, "p001", "42", "black", 5)

    res = await call_tool("place_order", {}, 3, "user")
    assert res["status"] == "order_placed", res

    assert _stock("p001", "42", "black") == 10 - 5


@pytest.mark.asyncio
async def test_multi_item_cart_quantity_greater_than_one_per_line():
    """Each line item's own quantity (not just cart length) must be respected
    exactly once — combines multi-item with multi-quantity-per-item."""
    await _add(4, "p001", "42", "black", 4)
    await _add(4, "p002", "L", "cream", 5)

    res = await call_tool("place_order", {}, 4, "user")
    assert res["status"] == "order_placed", res

    assert _stock("p001", "42", "black") == 10 - 4
    assert _stock("p002", "L", "cream") == 6 - 5


# --- insufficient-stock / atomicity regression --------------------------

@pytest.mark.asyncio
async def test_multi_item_cart_insufficient_stock_decrements_nothing():
    """
    Atomicity guarantee (from decrement_stock_for_cart's own design, which
    this fix must not weaken): if one line item in a multi-item cart can't
    be fulfilled, NOTHING should be decremented for any item in the cart —
    not even a single, correct-count decrement.
    """
    await _add(5, "p001", "42", "black", 3)
    # p002/M/cream only has 6 in stock; request more than available.
    await _add(5, "p002", "M", "cream", 4)
    # Manually oversell it after adding to cart, to simulate stock changing
    # between add_to_cart and place_order without touching add_to_cart's
    # own pre-check.
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    for p in inv["products"]:
        if p["id"] == "p002":
            for v in p["variants"]:
                if v["size"] == "M":
                    v["stock"] = 2  # now less than the 4 in cart
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(inv, f)
    inventory_manager._cache["mtime"] = None

    res = await call_tool("place_order", {}, 5, "user")
    assert "error" in res, res

    # p001's stock must be completely untouched — no partial decrement
    # despite p001 itself having plenty of stock.
    assert _stock("p001", "42", "black") == 10, "p001 stock was decremented despite the order failing on p002"
    assert _stock("p002", "M", "cream") == 2, "p002 stock should be unchanged (still the manually-set value)"


@pytest.mark.asyncio
async def test_multi_item_order_persists_correct_items_and_quantities():
    """
    Sanity check that the order record itself (independent of stock math)
    still reflects the real cart contents, unaffected by the decrement fix.
    """
    await _add(6, "p001", "42", "black", 2)
    await _add(6, "p002", "L", "cream", 1)

    res = await call_tool("place_order", {}, 6, "user")
    order = res["order"]

    items_by_product = {item["product_id"]: item["quantity"] for item in order["items"]}
    assert items_by_product == {"p001": 2, "p002": 1}


def main():
    """Lets this be run directly with `python test_multi_item_checkout.py`,
    matching the style of the other root-level test_*.py files in this
    project."""
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
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
