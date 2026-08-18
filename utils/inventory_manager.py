"""
Reads data/inventory.json. The seller edits that file directly (no DB),
so we cache it in memory but check the file's mtime before every read and
reload if it changed. Cheap for a file this size, and means the bot never
needs restarting after an inventory edit.

decrement_stock() is the one place that *writes* to inventory.json — it's
called from place_order once an order is confirmed to actually happen, so
that stock reflects real sales instead of just being checked-and-forgotten
at add_to_cart time.
"""
import json
import config

_cache = {"mtime": None, "products": []}


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
        if category and category not in p["category"].lower():
            continue
        haystack = f"{p['name']} {p['description']} {p['category']}".lower()
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
    for v in product["variants"]:
        if size and v["size"].lower() != size.lower():
            continue
        if color and color.lower() not in v["color"].lower():
            continue
        matches.append(v)
    return matches


def decrement_stock(product_id: str, size: str, color: str, quantity: int) -> dict:
    """
    Subtracts `quantity` from stock for the variant(s) matching size/color
    (color is substring-matched, same as check_variant, so this can span
    more than one variant row — see note in _place_order about how ties
    are broken). Writes the file directly (not through the cache) and then
    forces a reload so subsequent reads in this process see the new value
    immediately rather than waiting for the next mtime check.

    Returns {"status": "ok"} or {"error": ...} — this should only ever be
    called after callers have already validated sufficient stock exists,
    so an error here indicates a race (something else decremented stock
    between validation and this call) rather than a normal user mistake.
    """
    with open(config.INVENTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    product = next((p for p in data["products"] if p["id"] == product_id), None)
    if not product:
        return {"error": f"product '{product_id}' not found during decrement"}

    matches = [
        v for v in product["variants"]
        if v["size"].lower() == size.lower() and color.lower() in v["color"].lower()
    ]
    if not matches:
        return {"error": f"no variant matched size={size!r} color={color!r} during decrement"}

    remaining = quantity
    for v in matches:
        if remaining <= 0:
            break
        take = min(v["stock"], remaining)
        v["stock"] -= take
        remaining -= take

    if remaining > 0:
        return {
            "error": "insufficient stock at decrement time",
            "hint": f"Needed {quantity}, only had {quantity - remaining} across matching variants.",
        }

    with open(config.INVENTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Force a reload on next read instead of trusting the mtime check —
    # some filesystems have coarse mtime resolution and a same-second
    # write-then-read could otherwise serve the stale cached value.
    _cache["mtime"] = None

    return {"status": "ok"}