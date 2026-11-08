"""
Orders and feedback are append-only JSON logs (no DB, per project requirements).
Each write reads the whole file, appends, and writes back -- fine at this scale.
Admin group notification happens one layer up (agent.py has the bot instance).

Order status lifecycle:
  pending_confirmation -> awaiting_review (payment proof submitted) -> confirmed -> shipped -> delivered
                                                                     -> rejected (terminal)

shipped/delivered are an optional tail onto the already-hardened
pending_confirmation/awaiting_review/confirmed/rejected lifecycle --
every existing transition (place_order, attach_payment_proof,
set_order_status_if_not for /reject) is completely unchanged. See
advance_order_status() below for the new forward-only transition guard
that shipped/delivered uses.

Issue #16 (bug-audit 3.4): all file I/O now goes through
utils.file_io.read_json/write_json_atomic (asyncio.to_thread offload +
atomic temp-file/os.replace writes), so disk access never blocks the event
loop and a read can never observe a half-written file.

As part of this fix, get_order() and get_latest_unpaid_order() -- previously
plain synchronous functions doing their own unlocked file read -- are now
async and acquire _orders_lock like every other read/write in this module.
This wasn't optional: once writes to ORDERS_FILE run in a worker thread
(instead of on the single event-loop thread, where a read could never
overlap a write), an unlocked synchronous read sitting mid-await relative
to a write becomes a real torn-read hazard, not just a theoretical one.
Locking these reads the same way get_all_orders() already was closes that
gap. All call sites (agent/tools.py, utils/inventory_manager.py,
handlers/admin_handlers.py) are already inside async functions and have
been updated to `await` these two calls.
"""
import secrets
import asyncio
from datetime import datetime, timezone
import config
from utils import file_io

# Locks to prevent concurrent read-modify-write races
_orders_lock = asyncio.Lock()
_feedback_lock = asyncio.Lock()

# Order status, beyond confirmed/rejected (shipped/delivered): forward-only
# transitions, each keyed by the status it advances TO -> the status it
# requires the order to currently be in. advance_order_status() uses this
# so a seller can't skip a step (confirmed straight to delivered) or act on
# an order that isn't ready yet (shipping something still awaiting
# payment). rejected is intentionally absent here -- it's a separate,
# terminal transition handled by set_order_status_if_not(), not part of
# this forward chain.
_REQUIRED_PREDECESSOR = {
    "shipped": "confirmed",
    "delivered": "shipped",
}


async def _load(path) -> list[dict]:
    """Loads JSON, returning an empty list if the file doesn't exist yet."""
    return await file_io.read_json(path, [])


async def _save(path, records: list[dict]):
    await file_io.write_json_atomic(path, records)


async def _append(path, record: dict, lock: asyncio.Lock):
    async with lock:
        records = await _load(path)
        records.append(record)
        await _save(path, records)
        return record


async def place_order(user_id: int, username: str, items: list[dict]) -> dict:
    order = {
        "order_id": f"ord_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(3)}",
        "user_id": user_id,
        "username": username,
        "items": items,
        "status": "pending_confirmation",
        "payment_proof": None,
        # Audit trail for payment-proof resubmissions (Issue 4.2, Option B):
        # every submission is appended below, never overwritten.
        "payment_proof_history": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _append(config.ORDERS_FILE, order, _orders_lock)


async def get_order(order_id: str) -> dict | None:
    """
    Now async + locked (Issue #16) -- was a plain synchronous unlocked read.
    Callers: agent/tools.py, utils/inventory_manager.py (restore_stock_for_order),
    handlers/admin_handlers.py -- all already async, all updated to await this.
    """
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                return o
        return None


async def get_latest_unpaid_order(user_id: int) -> dict | None:
    """
    Now async + locked (Issue #16), same reasoning as get_order() above.
    Matches both orders that never had proof attached (pending_confirmation)
    and orders already sitting in awaiting_review. The latter is what makes
    resubmission reachable at all: previously this filter stopped matching
    the instant any proof was attached, so a customer who needed to replace
    their proof hit a dead end (Issue 4.2, Option B).
    """
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        matches = [
            o for o in records
            if o["user_id"] == user_id and o["status"] in ("pending_confirmation", "awaiting_review")
        ]
        return matches[-1] if matches else None


async def get_latest_order_for_user(user_id: int) -> dict | None:
    """
    Returns the customer's single most recent order, regardless of status --
    unlike get_latest_unpaid_order(), which only matches orders still
    awaiting payment. Added alongside the shipped/delivered status tail so
    a customer can ask "where's my order?" (agent/tools.py's
    check_order_status) even after it's already confirmed, shipped, or
    delivered, not just while payment is still pending.
    """
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        matches = [o for o in records if o["user_id"] == user_id]
        return matches[-1] if matches else None


async def attach_payment_proof(order_id: str, proof: str) -> dict | None:
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                o["status"] = "awaiting_review"
                o["payment_proof"] = proof
                # Option B (Issue 4.2): keep every submission, in order, so a
                # resubmission is auditable and admin notifications can flag
                # it. setdefault (rather than requiring the field at creation)
                # keeps this safe for orders that already exist in orders.json
                # from before this field was introduced.
                o.setdefault("payment_proof_history", []).append({
                    "proof": proof,
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                })
                await _save(config.ORDERS_FILE, records)
                return o
        return None


async def set_order_status(order_id: str, status: str) -> dict | None:
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                o["status"] = status
                await _save(config.ORDERS_FILE, records)
                return o
        return None


async def set_order_status_if_not(order_id: str, status: str, forbidden_current_status: str) -> tuple[dict | None, bool]:
    """
    Atomically transitions order_id to `status`, unless its current status
    already equals `forbidden_current_status` -- in which case it's left
    untouched. Returns (order, transitioned):
      - order is None if no order with that id exists.
      - transitioned is True only if this call actually changed the status.

    Exists so a caller can tell "I just performed this transition" apart
    from "this was already done" under concurrent calls (e.g. two /reject
    commands for the same order_id landing at nearly the same time), without
    a separate read-then-write race. Used by /reject so stock restoration
    (utils/inventory_manager.restore_stock_for_order) only ever runs once
    per order, even if the admin taps /reject twice.
    """
    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                if o["status"] == forbidden_current_status:
                    return o, False
                o["status"] = status
                await _save(config.ORDERS_FILE, records)
                return o, True
        return None, False


async def advance_order_status(order_id: str, new_status: str) -> tuple[dict | None, str | None]:
    """
    Atomically transitions order_id FORWARD to new_status, but only if the
    order's CURRENT status is exactly the required predecessor for that
    step (see _REQUIRED_PREDECESSOR: confirmed -> shipped -> delivered).
    Used for the /ship and /deliver admin commands.

    Mirrors set_order_status_if_not()'s atomic check-and-set shape and
    _orders_lock usage, but the check is inverted: set_order_status_if_not
    refuses a FORBIDDEN current status (used for idempotent /reject);
    this refuses anything OTHER than the one REQUIRED predecessor, since
    shipped/delivered must advance exactly one step at a time -- no
    skipping (confirmed straight to delivered), no shipping an order
    that isn't confirmed yet, and no double-shipping an order that's
    already shipped (a second /ship on an already-shipped order sees its
    current status as "shipped", not "confirmed", and is refused the same
    way a double /reject already is -- a clear message, not a silent
    no-op or a duplicate customer DM).

    Returns (order, error):
      - order is None only if no order with that id exists at all.
      - error is None on success. On a refused transition, order is the
        order as it currently stands (so the caller can report its real
        status) and error is a short, human-readable reason -- e.g.
        "order is currently 'pending_confirmation', not 'confirmed'".
    """
    required = _REQUIRED_PREDECESSOR.get(new_status)
    if required is None:
        return None, f"'{new_status}' is not a valid order-status advancement"

    async with _orders_lock:
        records = await _load(config.ORDERS_FILE)
        for o in records:
            if o["order_id"] == order_id:
                if o["status"] != required:
                    return o, f"order is currently '{o['status']}', not '{required}'"
                o["status"] = new_status
                await _save(config.ORDERS_FILE, records)
                return o, None
        return None, None


async def get_all_orders() -> list[dict]:
    """
    Safely reads all orders, protected by the lock to prevent reading
    a partially written file if a write operation is happening at the exact same moment.
    """
    async with _orders_lock:
        return await _load(config.ORDERS_FILE)


async def log_feedback(user_id: int, username: str, kind: str, message: str) -> dict:
    entry = {
        "user_id": user_id,
        "username": username,
        "kind": kind,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _append(config.FEEDBACK_FILE, entry, _feedback_lock)