"""
Orders and feedback are append-only JSON logs (no DB, per project requirements).
Each write reads the whole file, appends, and writes back — fine at this scale.
Admin group notification happens one layer up (agent.py has the bot instance).
"""
import json
import secrets
from datetime import datetime, timezone
import config


def _append(path, record: dict):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    records.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return record


def place_order(user_id: int, username: str, items: list[dict]) -> dict:
    order = {
        "order_id": f"ord_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(3)}",
        "user_id": user_id,
        "username": username,
        "items": items,
        "status": "pending_confirmation",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _append(config.ORDERS_FILE, order)


def log_feedback(user_id: int, username: str, kind: str, message: str) -> dict:
    entry = {
        "user_id": user_id,
        "username": username,
        "kind": kind,  # "compliment" | "complaint"
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _append(config.FEEDBACK_FILE, entry)
