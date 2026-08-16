"""
Orders and feedback are append-only JSON logs (no DB, per project requirements).
Each write reads the whole file, appends, and writes back — fine at this scale.
Admin group notification happens one layer up (agent.py has the bot instance).

Order status lifecycle:
  pending_confirmation -> awaiting_review (payment proof submitted) -> confirmed | rejected
"""
import json
import secrets
from datetime import datetime, timezone
import config


def _load(path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path, records: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _append(path, record: dict):
    records = _load(path)
    records.append(record)
    _save(path, records)
    return record


def place_order(user_id: int, username: str, items: list[dict]) -> dict:
    order = {
        "order_id": f"ord_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(3)}",
        "user_id": user_id,
        "username": username,
        "items": items,
        "status": "pending_confirmation",
        "payment_proof": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _append(config.ORDERS_FILE, order)


def get_order(order_id: str) -> dict | None:
    for o in _load(config.ORDERS_FILE):
        if o["order_id"] == order_id:
            return o
    return None


def get_latest_unpaid_order(user_id: int) -> dict | None:
    """Most recent order from this user still waiting on payment proof."""
    matches = [
        o for o in _load(config.ORDERS_FILE)
        if o["user_id"] == user_id and o["status"] == "pending_confirmation"
    ]
    return matches[-1] if matches else None


def attach_payment_proof(order_id: str, proof: str) -> dict | None:
    """proof is a short description: a transaction ID, or 'photo:<file_id>'."""
    records = _load(config.ORDERS_FILE)
    for o in records:
        if o["order_id"] == order_id:
            o["status"] = "awaiting_review"
            o["payment_proof"] = proof
            _save(config.ORDERS_FILE, records)
            return o
    return None


def set_order_status(order_id: str, status: str) -> dict | None:
    records = _load(config.ORDERS_FILE)
    for o in records:
        if o["order_id"] == order_id:
            o["status"] = status
            _save(config.ORDERS_FILE, records)
            return o
    return None


def log_feedback(user_id: int, username: str, kind: str, message: str) -> dict:
    entry = {
        "user_id": user_id,
        "username": username,
        "kind": kind,  # "compliment" | "complaint"
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _append(config.FEEDBACK_FILE, entry)