# utils/cart_manager.py
"""
Persists per-user carts to data/carts.json so they survive bot restarts.
Uses an asyncio.Lock to prevent concurrent read-modify-write races,
matching the pattern in order_manager.py.

Issue #16 (bug-audit 3.4): the actual file I/O now runs via
utils.file_io.read_json/write_json_atomic, which offload the blocking
open()/json calls to a worker thread (asyncio.to_thread) and write
atomically (temp file + os.replace) so the event loop never stalls on
disk I/O and a concurrent read can never observe a half-written file.
_carts_lock is unchanged and still serializes the read-modify-write
sequence — the lock is what prevents two concurrent set_cart() calls from
racing each other; the atomic write is what protects a read happening
*outside* the lock (there isn't one here today, but get_cart() still goes
through the same lock as a matter of consistency with the other managers).
"""
import asyncio
import config
from utils import file_io

_carts_lock = asyncio.Lock()


async def _load() -> dict[str, list[dict]]:
    return await file_io.read_json(config.CARTS_FILE, {})


async def _save(carts: dict[str, list[dict]]):
    await file_io.write_json_atomic(config.CARTS_FILE, carts)


async def get_cart(user_id: int) -> list[dict]:
    async with _carts_lock:
        carts = await _load()
        return carts.get(str(user_id), [])


async def set_cart(user_id: int, items: list[dict]):
    async with _carts_lock:
        carts = await _load()
        if items:
            carts[str(user_id)] = items
        else:
            # Clean up empty carts from the file to keep it small
            carts.pop(str(user_id), None)
        await _save(carts)