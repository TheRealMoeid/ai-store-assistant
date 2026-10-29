# test_payment_resubmission.py
# Issue 4.2 (Option B): payment-proof resubmission with audit history.
# Run directly: python test/test_payment_resubmission.py
# (Not pytest-discovered: contains no module-level test_* functions, like
# test_narration_guard.py.)
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from utils import order_manager


def _sandbox():
    """Points the managers at a throwaway directory for the duration of the test."""
    tmp = tempfile.mkdtemp()
    config.ORDERS_FILE = os.path.join(tmp, "orders.json")
    config.FEEDBACK_FILE = os.path.join(tmp, "feedback.json")
    return tmp


async def main():
    _sandbox()

    # 1. First-time reference submission — regression: behavior must be unchanged.
    order = await order_manager.place_order(1001, "alice", [
        {"product_id": "p001", "size": "42", "color": "red", "quantity": 1},
    ])
    assert order["status"] == "pending_confirmation"
    assert order["payment_proof_history"] == []

    updated = await order_manager.attach_payment_proof(order["order_id"], "REF-123")
    assert updated["status"] == "awaiting_review"
    assert updated["payment_proof"] == "REF-123"
    assert len(updated["payment_proof_history"]) == 1
    assert updated["payment_proof_history"][0]["proof"] == "REF-123"
    assert "submitted_at" in updated["payment_proof_history"][0]
    # Resubmission signal used by handlers/user_handlers.py must be off.
    assert not (len(updated.get("payment_proof_history", [])) > 1)

    # 2. Core Issue 4.2 bug: the lookup must still find the awaiting_review order.
    found = order_manager.get_latest_unpaid_order(1001)
    assert found is not None and found["order_id"] == order["order_id"]

    # 3. Resubmission: the replacement is recorded, not rejected; status is
    #    not reset back to pending_confirmation.
    updated2 = await order_manager.attach_payment_proof(order["order_id"], "REF-999")
    assert updated2["payment_proof"] == "REF-999"
    assert updated2["status"] == "awaiting_review"
    assert [h["proof"] for h in updated2["payment_proof_history"]] == ["REF-123", "REF-999"]
    # This is what _notify_admin / handle_payment_screenshot use to flag it.
    assert len(updated2.get("payment_proof_history", [])) > 1

    # 4. Screenshot-style proofs append too, and the history persists to disk.
    updated3 = await order_manager.attach_payment_proof(order["order_id"], "photo:fileid_1")
    assert len(updated3["payment_proof_history"]) == 3
    assert updated3["payment_proof"] == "photo:fileid_1"
    reloaded = order_manager.get_order(order["order_id"])
    assert len(reloaded["payment_proof_history"]) == 3

    # 5. Legacy orders (created before payment_proof_history existed) must not
    #    need a schema migration — attach initializes the field via setdefault.
    legacy = await order_manager.place_order(2002, "bob", [
        {"product_id": "p002", "size": "M", "color": "blue", "quantity": 2},
    ])
    with open(config.ORDERS_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)
    for r in records:
        if r["order_id"] == legacy["order_id"]:
            del r["payment_proof_history"]  # simulate a pre-fix record
    with open(config.ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    updated_legacy = await order_manager.attach_payment_proof(legacy["order_id"], "REF-LEGACY-1")
    assert updated_legacy["payment_proof"] == "REF-LEGACY-1"
    assert len(updated_legacy["payment_proof_history"]) == 1
    resubmitted_legacy = await order_manager.attach_payment_proof(legacy["order_id"], "REF-LEGACY-2")
    assert len(resubmitted_legacy["payment_proof_history"]) == 2

    # 6. Confirmed orders are excluded from the lookup; a newer pending order
    #    for the same user is returned instead.
    await order_manager.set_order_status(order["order_id"], "confirmed")
    assert order_manager.get_latest_unpaid_order(1001) is None
    newer = await order_manager.place_order(1001, "alice", [
        {"product_id": "p003", "size": "S", "color": "green", "quantity": 1},
    ])
    assert order_manager.get_latest_unpaid_order(1001)["order_id"] == newer["order_id"]

    print("✅ SUCCESS: Payment-proof resubmission tracking works (Issue 4.2, Option B).")


if __name__ == "__main__":
    asyncio.run(main())
