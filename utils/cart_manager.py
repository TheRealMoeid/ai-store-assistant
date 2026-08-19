# utils/cart_manager.py
"""
Persists per-user carts to data/carts.json so they survive bot restarts.
Uses an asyncio.Lock to prevent concurrent read-modify-write races,
matching the pattern in order_manager.py.
"""
import json
import asyncio
import config

_carts_lock = asyncio.Lock()

def _load() -> dict[str, list[dict]]:
    if not config.CARTS_FILE.exists():
        return {}
    with open(config.CARTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(carts: dict[str, list[dict]]):
    with open(config.CARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(carts, f, ensure_ascii=False, indent=2)

async def get_cart(user_id: int) -> list[dict]:
    async with _carts_lock:
        carts = _load()
        return carts.get(str(user_id), [])

async def set_cart(user_id: int, items: list[dict]):
    async with _carts_lock:
        carts = _load()
        if items:
            carts[str(user_id)] = items
        else:
            # Clean up empty carts from the file to keep it small
            carts.pop(str(user_id), None)
        _save(carts)