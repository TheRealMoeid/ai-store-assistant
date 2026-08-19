"""
Orders and feedback are append-only JSON logs (no DB, per project requirements).
Each write reads the whole file, appends, and writes back — fine at this scale.
Admin group notification happens one layer up (agent.py has the bot instance).

Order status lifecycle:
  pending_confirmation -> awaiting_review (payment proof submitted) -> confirmed | rejected
"""
import json
import secrets
import asyncio
from datetime import datetime, timezone
import config

# Locks to prevent concurrent read-modify-write races
_orders_lock = asyncio.Lock()
_feedback_lock = asyncio.Lock()

def _load(path) -> list[dict]:
    """Safely loads JSON, returning an empty list if the file doesn't exist yet."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def _save(path, records: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

async def _append(path, record: dict, lock: asyncio.Lock):
    async with lock:
        records = _load(path)
        records.append(record)
        _save(path, records)
        return record

async def place_order(user_id: int, username: str, items: list[dict]) -> dict:
    order = {
        "order_id": f"ord_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(3)}",
        "user_id": user_id,
        "username": username,
        "items": items,
        "status": "pending_confirmation",
        "payment_proof": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _append(config.ORDERS_FILE, order, _orders_lock)

def get_order(order_id: str) -> dict | None:
    for o in _load(config.ORDERS_FILE):
        if o["order_id"] == order_id:
            return o
    return None

def get_latest_unpaid_order(user_id: int) -> dict | None:
    matches = [
        o for o in _load(config.ORDERS_FILE)
        if o["user_id"] == user_id and o["status"] == "pending_confirmation"
    ]
    return matches[-1] if matches else None

async def attach_payment_proof(order_id: str, proof: str) -> dict | None:
    async with _orders_lock:
        records = _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                o["status"] = "awaiting_review"
                o["payment_proof"] = proof
                _save(config.ORDERS_FILE, records)
                return o
        return None

async def set_order_status(order_id: str, status: str) -> dict | None:
    async with _orders_lock:
        records = _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                o["status"] = status
                _save(config.ORDERS_FILE, records)
                return o
        return None

async def get_all_orders() -> list[dict]:
    """
    Safely reads all orders, protected by the lock to prevent reading 
    a partially written file if a write operation is happening at the exact same moment.
    """
    async with _orders_lock:
        return _load(config.ORDERS_FILE)

async def log_feedback(user_id: int, username: str, kind: str, message: str) -> dict:
    entry = {
        "user_id": user_id,
        "username": username,
        "kind": kind,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _append(config.FEEDBACK_FILE, entry, _feedback_lock)