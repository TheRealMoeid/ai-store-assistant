"""
Reads data/inventory.json. The seller edits that file directly (no DB),
so we cache it in memory but check the file's mtime before every read and
reload if it changed. Cheap for a file this size, and means the bot never
needs restarting after an inventory edit.

decrement_stock_for_cart() and restore_stock_for_order() are the only two
places that *write* to inventory.json. decrement_stock_for_cart is called
from place_order once an order is placed; restore_stock_for_order is called
from the /reject admin command to give that reserved stock back. Both hold
_inventory_lock and validate before writing.
"""
import json
import logging
import asyncio
import config
from utils import order_manager

logger = logging.getLogger("inventory_manager")

_cache = {"mtime": None, "products": []}
_inventory_lock = asyncio.Lock()

def _load_if_changed():
    mtime = config.INVENTORY_FILE.stat().st_mtime
    if mtime != _cache["mtime"]:
        with open(config.INVENTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache["products"] = data.get("products", [])
        _cache["mtime"] = mtime
    return _cache["products"]

def get_all_products() -> list[dict]:
    return _load_if_changed()

def get_product_by_id(product_id: str) -> dict | None:
    for p in _load_if_changed():
        if p["id"] == product_id:
            return p
    return None

def search_products(query: str = "", category: str = "") -> list[dict]:
    """Simple case-insensitive substring match on name/description/category."""
    query = query.lower().strip()
    category = category.lower().strip()
    results = []
    for p in _load_if_changed():
        if category and category not in p.get("category", "").lower():
            continue
        # Use .get() to safely handle missing fields
        haystack = f"{p.get('name', '')} {p.get('description', '')} {p.get('category', '')}".lower()
        if query and query not in haystack:
            continue
        results.append(p)
    return results

def check_variant(product_id: str, size: str = "", color: str = "") -> list[dict]:
    """Returns matching variants (with stock count) for a product."""
    product = get_product_by_id(product_id)
    if not product:
        return []
    matches = []
    # Use .get() to safely handle missing variants
    for v in product.get("variants", []):
        if size and v.get("size", "").lower() != size.lower():
            continue
        if color and color.lower() not in v.get("color", "").lower():
            continue
        matches.append(v)
    return matches

async def decrement_stock_for_cart(cart_items: list[dict]) -> dict:
    """
    Atomically decrements stock for an entire cart.
    Validates all items in memory first. If any item lacks sufficient stock,
    the operation aborts and NO changes are written to disk.
    """
    async with _inventory_lock:
        # 1. Read current state from disk
        with open(config.INVENTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 2. Phase 1: Validate all items in the cart
        for item in cart_items:
            product = next((p for p in data.get("products", []) if p.get("id") == item.get("product_id")), None)
            if not product:
                return {"error": f"product '{item.get('product_id')}' not found during checkout"}

            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower() and item.get("color", "").lower() in v.get("color", "").lower()
            ]
            if not matches:
                return {"error": f"variant {item.get('size')}/{item.get('color')} not found for {item.get('product_id')}"}

            total_stock = sum(v.get("stock", 0) for v in matches)
            if total_stock < item.get("quantity", 0):
                return {
                    "error": "insufficient stock at checkout",
                    "hint": f"Not enough stock for {product.get('name')} ({item.get('size')}/{item.get('color')}). Requested {item.get('quantity')}, available {total_stock}.",
                    "shortfall": item
                }

        # 3. Phase 2: All items passed validation. Now actually decrement.
        for item in cart_items:
            product = next(p for p in data.get("products", []) if p.get("id") == item.get("product_id"))
            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower() and item.get("color", "").lower() in v.get("color", "").lower()
            ]

            remaining = item.get("quantity", 0)
            for v in matches:
                if remaining <= 0:
                    break
                take = min(v.get("stock", 0), remaining)
                v["stock"] = v.get("stock", 0) - take
                remaining -= take

        # 4. Write the fully updated inventory back to disk
        with open(config.INVENTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Invalidate cache so next read gets the fresh data
        _cache["mtime"] = None
        return {"status": "ok"}


async def restore_stock_for_order(order_id: str) -> dict:
    """
    Gives back the stock reserved by decrement_stock_for_cart when an order
    is rejected. Re-runs the exact same size/color matching used to decrement,
    so (in the normal case of one exact-matching variant per line item) stock
    is credited back to precisely the variant it was taken from.

    Note: if a line item's size/color matched more than one variant at
    decrement time (possible since color matching is substring-based), the
    original decrement may have spread across multiple variants, but the
    order record only stores product_id/size/color/quantity — not which
    specific variant(s) absorbed it. In that rare case, this credits the
    full quantity back to the first matching variant rather than trying to
    reconstruct the original split. Flagging this as a known limitation
    rather than solving it now, since real inventories in this project use
    one variant per size/color combo.

    Returns a dict:
      {"status": "ok", "restored": [items], "skipped": [items]}
    or {"error": "..."} if the order itself can't be found.

    An item whose product or variant no longer exists in inventory.json
    (e.g. the seller deleted/renamed it since the order was placed) is
    logged and added to "skipped" rather than raising — the caller
    (admin_handlers.reject_order) surfaces "skipped" to the admin so the
    seller can reconcile it by hand instead of it silently vanishing.
    """
    order = order_manager.get_order(order_id)
    if not order:
        return {"error": f"order '{order_id}' not found"}

    items = order.get("items", [])
    if not items:
        return {"status": "ok", "restored": [], "skipped": []}

    async with _inventory_lock:
        with open(config.INVENTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        restored = []
        skipped = []

        for item in items:
            product = next(
                (p for p in data.get("products", []) if p.get("id") == item.get("product_id")),
                None,
            )
            if not product:
                logger.warning(
                    "restore_stock_for_order(%s): product '%s' no longer exists — skipping restoration for this item",
                    order_id, item.get("product_id"),
                )
                skipped.append(item)
                continue

            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower()
                and item.get("color", "").lower() in v.get("color", "").lower()
            ]
            if not matches:
                logger.warning(
                    "restore_stock_for_order(%s): variant %s/%s no longer exists for product '%s' — skipping restoration for this item",
                    order_id, item.get("size"), item.get("color"), item.get("product_id"),
                )
                skipped.append(item)
                continue

            matches[0]["stock"] = matches[0].get("stock", 0) + item.get("quantity", 0)
            restored.append(item)

        if restored:
            with open(config.INVENTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # Invalidate cache so next read gets the fresh data
            _cache["mtime"] = None

        return {"status": "ok", "restored": restored, "skipped": skipped}