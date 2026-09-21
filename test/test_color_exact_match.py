# test/test_color_exact_match.py
"""
Covers bug-audit-evaluation.md Issue 3.2: "Substring Color Matching Causes
Inventory Drift on Rejection."

The bug: utils/inventory_manager.py matched a cart/order line item's color
against a variant's color with substring containment (`item_color in
variant_color`) in three places — check_variant(), decrement_stock_for_cart(),
and restore_stock_for_order(). If a product had two variants whose color
strings overlap (e.g. "red" and "red/blue"), a line item requesting color
"red" matched *both* variants, and decrement_stock_for_cart() could spread
a single line item's quantity across both of them. But the order record
only ever stores a flat product_id/size/color/quantity per item — not which
specific variant(s) absorbed the decrement — so restore_stock_for_order()
(run on /reject) had no way to reconstruct the original split and always
credited the *full* quantity back to just the *first* matching variant.
Net effect: rejecting an order for an overlapping-color product silently
over-credited one variant and under-credited (or entirely missed) the
other(s) — permanent inventory drift.

The fix (Approach A, chosen over reworking the order schema to record a
per-variant decrement breakdown): change all three match sites from
substring (`in`) to exact, case-insensitive equality (`==`). A line item's
color can then only ever match a single variant, so decrement and restore
are structurally guaranteed to agree — restore's "credit the (only) match"
becomes correct by construction instead of a documented limitation. Chosen
over the schema-change alternative specifically because it stays inside
utils/inventory_manager.py, doesn't touch the order record shape or the
already-hardened checkout/concurrency path (Issue #10/3.1), and matches
this project's "minimal, structurally-scoped fixes" convention.

Sandboxes config paths to a throwaway test_data/ directory, matching the
pattern already used by test_stock_reversal.py and test_multi_item_checkout.py,
so real data/ is never touched. Drives most cases through the real
agent.tools.call_tool / inventory_manager public surface rather than
poking internals directly, since the bug (and the fix) lives in how those
functions match colors, not in unrelated logic around them.
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
from handlers.admin_handlers import reject_order


# --- fakes for driving reject_order() directly, matching test_stock_reversal.py ---

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


# A product whose color strings deliberately overlap as substrings, the
# exact shape that triggered the bug: "red" is a substring of "red/blue".
OVERLAPPING_COLOR_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [
                {"size": "42", "color": "red", "stock": 5},
                {"size": "42", "color": "red/blue", "stock": 5},
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(OVERLAPPING_COLOR_INVENTORY, f)
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


def _stock(color: str) -> int:
    with open(config.INVENTORY_FILE) as f:
        inv = json.load(f)
    variant = next(v for v in inv["products"][0]["variants"] if v["color"] == color)
    return variant["stock"]


# --- check_variant() -------------------------------------------------------

def test_check_variant_no_longer_matches_substring():
    """
    Core fix, at the lookup layer: requesting color 'red' must match ONLY
    the 'red' variant, not 'red/blue' too. Before the fix, this returned
    both variants (since 'red' in 'red/blue').
    """
    matches = inventory_manager.check_variant("p001", size="42", color="red")
    assert len(matches) == 1
    assert matches[0]["color"] == "red"


def test_check_variant_still_case_insensitive():
    """The fix must stay case-insensitive — only the substring behavior
    (`in`) is removed, not case folding."""
    matches = inventory_manager.check_variant("p001", size="42", color="RED")
    assert len(matches) == 1
    assert matches[0]["color"] == "red"


def test_check_variant_exact_match_for_the_longer_color_still_works():
    """Requesting the longer color string exactly must still match its own
    variant (and only its own variant) — the fix isn't a one-directional
    fluke that happens to work only for the shorter string."""
    matches = inventory_manager.check_variant("p001", size="42", color="red/blue")
    assert len(matches) == 1
    assert matches[0]["color"] == "red/blue"


def test_check_variant_no_match_for_partial_color_now_returns_empty():
    """
    Regression/behavior-change check: a genuinely partial color that isn't
    an exact match to any variant (e.g. a customer typing just 'blue' when
    only 'red/blue' exists) no longer fuzzy-matches. This is the deliberate
    trade-off of Approach A — flagged explicitly during approach selection.
    """
    matches = inventory_manager.check_variant("p001", size="42", color="blue")
    assert matches == []


# --- decrement_stock_for_cart() --------------------------------------------

@pytest.mark.asyncio
async def test_decrement_only_affects_the_exact_variant_requested():
    """
    Core Issue 3.2 case at the decrement layer: ordering 'red' must decrement
    ONLY the 'red' variant's stock. Before the fix, decrement_stock_for_cart's
    matches list included both 'red' and 'red/blue', so a large enough order
    (or even a normal one, depending on stock levels) could pull stock from
    'red/blue' to help fulfill a 'red' order.
    """
    cart = [{"product_id": "p001", "size": "42", "color": "red", "quantity": 3}]
    result = await inventory_manager.decrement_stock_for_cart(cart)
    assert result["status"] == "ok"

    assert _stock("red") == 2, "the exact variant ordered must be decremented"
    assert _stock("red/blue") == 5, "the unrelated overlapping-substring variant must be untouched"


@pytest.mark.asyncio
async def test_decrement_insufficient_stock_no_longer_borrows_from_other_variant():
    """
    Before the fix, total_stock for a 'red' order was sum(red.stock,
    red/blue.stock) = 10, so ordering 6 units of 'red' (more than 'red'
    alone has) would have incorrectly succeeded by quietly pulling from
    'red/blue'. After the fix, only the exact 'red' variant's stock (5)
    counts, so this must now correctly fail as insufficient stock.
    """
    cart = [{"product_id": "p001", "size": "42", "color": "red", "quantity": 6}]
    result = await inventory_manager.decrement_stock_for_cart(cart)

    assert "error" in result
    assert _stock("red") == 5, "a failed validation must not decrement anything"
    assert _stock("red/blue") == 5, "must especially not borrow from the other variant"


# --- restore_stock_for_order() / reject_order() — the actual drift case ---

@pytest.mark.asyncio
async def test_restore_after_rejection_no_longer_drifts_between_overlapping_variants():
    """
    The actual bug as originally reported: place an order for the 'red'
    variant, reject it, and confirm stock is restored to exactly its
    pre-order level for 'red' — and that 'red/blue' is left completely
    alone throughout, with no drift in either direction.

    Before the fix this was already latent-broken even for a single-variant
    match (decrement and restore both used the same substring matches list,
    so for a *single* line item they happened to agree) — the real damage
    only showed up on multi-variant spread, which the next test isolates
    explicitly. This test is the end-to-end round trip a seller would
    actually see: place -> reject -> stock is back to normal.
    """
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "red", "quantity": 2},
        1, "user",
    )
    assert res["status"] == "added", res

    res = await call_tool("place_order", {}, 1, "user")
    assert res["status"] == "order_placed", res
    order = res["order"]

    assert _stock("red") == 3
    assert _stock("red/blue") == 5

    message, bot = FakeMessage(), FakeBot()
    await reject_order(message, FakeCommand(order["order_id"]), bot)

    assert _stock("red") == 5, "stock must be fully restored to its pre-order level"
    assert _stock("red/blue") == 5, "the overlapping-substring variant must never have been touched"
    assert any("rejected" in r.lower() for r in message.replies)


@pytest.mark.asyncio
async def test_no_drift_even_when_order_would_have_spanned_variants_pre_fix():
    """
    Sharper version of the drift case: construct a cart whose quantity is
    large enough that, under the OLD substring-matching decrement logic,
    it would have spread across both 'red' and 'red/blue' (since their
    combined stock, but not 'red' alone, could cover it). Under the fix,
    this order must never be placeable in the first place — decrement now
    validates against 'red' alone and correctly rejects it as insufficient
    stock, so there is no multi-variant spread left to ever need restoring
    incorrectly. This directly proves the root cause (decrement spreading
    across variants that restore can't track) no longer exists.
    """
    # 'red' alone only has 5; request more than that but within the old
    # (bugged) combined total of 10.
    cart = [{"product_id": "p001", "size": "42", "color": "red", "quantity": 8}]

    # add_to_cart's own pre-check uses check_variant/total_stock, so under
    # the fix it should already refuse at this step rather than at checkout.
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "red", "quantity": 8},
        2, "user",
    )
    assert "error" in res, "add_to_cart must reject a quantity exceeding the exact variant's own stock"
    assert _stock("red") == 5
    assert _stock("red/blue") == 5


@pytest.mark.asyncio
async def test_ordering_the_longer_overlapping_color_is_unaffected_by_the_shorter_one():
    """
    Symmetry check: ordering 'red/blue' (the longer string) must decrement
    only 'red/blue', and rejecting that order must restore only 'red/blue'
    — 'red' must never be touched in either direction. Confirms the fix
    isn't accidentally one-directional.
    """
    res = await call_tool(
        "add_to_cart",
        {"product_id": "p001", "size": "42", "color": "red/blue", "quantity": 4},
        3, "user",
    )
    assert res["status"] == "added", res

    res = await call_tool("place_order", {}, 3, "user")
    assert res["status"] == "order_placed", res
    order = res["order"]

    assert _stock("red") == 5
    assert _stock("red/blue") == 1

    message, bot = FakeMessage(), FakeBot()
    await reject_order(message, FakeCommand(order["order_id"]), bot)

    assert _stock("red") == 5, "must remain untouched throughout"
    assert _stock("red/blue") == 5, "must be fully restored"


# --- regression: non-overlapping, real-world-shaped inventory unaffected --

@pytest.mark.asyncio
async def test_regression_non_overlapping_colors_still_work_end_to_end():
    """
    Sanity check against inventory shaped like the project's real
    data/inventory.json (non-overlapping colors like 'black'/'cream') to
    confirm the fix doesn't regress the common case that was already
    working: exact-match colors that never had a substring collision to
    begin with.
    """
    inv = {
        "products": [
            {
                "id": "p002", "name": "Hoodie", "category": "clothing",
                "variants": [
                    {"size": "M", "color": "black", "stock": 5},
                    {"size": "M", "color": "cream", "stock": 5},
                ],
            }
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(inv, f)
    inventory_manager._cache["mtime"] = None

    res = await call_tool(
        "add_to_cart",
        {"product_id": "p002", "size": "M", "color": "black", "quantity": 2},
        4, "user",
    )
    assert res["status"] == "added", res

    res = await call_tool("place_order", {}, 4, "user")
    assert res["status"] == "order_placed", res
    order = res["order"]

    with open(config.INVENTORY_FILE) as f:
        inv_after = json.load(f)
    variants = {v["color"]: v["stock"] for v in inv_after["products"][0]["variants"]}
    assert variants == {"black": 3, "cream": 5}

    message, bot = FakeMessage(), FakeBot()
    await reject_order(message, FakeCommand(order["order_id"]), bot)

    with open(config.INVENTORY_FILE) as f:
        inv_final = json.load(f)
    variants_final = {v["color"]: v["stock"] for v in inv_final["products"][0]["variants"]}
    assert variants_final == {"black": 5, "cream": 5}


def main():
    """Lets this be run directly with `python test/test_color_exact_match.py`,
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
            json.dump(OVERLAPPING_COLOR_INVENTORY, f)
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
