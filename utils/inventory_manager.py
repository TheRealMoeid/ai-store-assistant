"""
Reads data/inventory.json. The seller edits that file directly (no DB),
so we cache it in memory but check the file's mtime before every read and
reload if it changed. Cheap for a file this size, and means the bot never
needs restarting after an inventory edit.
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
