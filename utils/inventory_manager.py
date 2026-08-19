import json
import asyncio
import config

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

# ... (get_all_products, get_product_by_id, search_products, check_variant remain exactly as they were, sync is fine for reads) ...

async def decrement_stock(product_id: str, size: str, color: str, quantity: int) -> dict:
    async with _inventory_lock:
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

        _cache["mtime"] = None
        return {"status": "ok"}