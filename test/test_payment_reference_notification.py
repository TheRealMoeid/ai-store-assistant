# test_payment_reference_notification.py
"""
Covers Issue 1.3 (bug-audit-evaluation.md, High priority): run_agent only
ever appended to `events` for place_order and log_feedback outcomes. There
was no case for submit_payment_reference, even though
handlers/user_handlers.py::_notify_admin already has a fully-built
"payment_reference" branch ready to receive it. The result: a customer who
types a transaction reference (as opposed to sending a payment screenshot,
which is handled by a separate non-LLM photo handler) gets their reference
recorded in orders.json, but the admin group is never told — the order sits
in "awaiting_review" forever with no signal that a seller needs to look at
it.

The fix adds the same `if name == "..." and result.get("status") == "...":
events.append(...)` shape already used for place_order/log_feedback, at all
three places agent.py processes a tool result:
  1. the normal structured tool-call path,
  2. the native-function-call recovery path (BadRequestError branch), and
  3. the leaked-plain-text-JSON recovery path,
so a recovered/malformed submit_payment_reference call notifies the admin
just as reliably as a clean one — matching the project's own "silent gap"
principle (prompt-level and code-level changes must land together, and
recovery paths must mirror the structured-call path exactly) documented in
CLAUDE.md.

Sandboxes config paths to test_data/ (including CONVERSATIONS_DIR), same
pattern as test_leaked_json_recovery.py, with only the LLM client mocked
(agent.agent.get_client) — everything else (call_tool, order_manager,
cart_manager) is the real business logic.
"""
import sys
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
import openai
import pytest

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)
TEST_CONVERSATIONS_DIR = TEST_DATA_DIR / "conversations"
TEST_CONVERSATIONS_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.ORDERS_FILE = TEST_DATA_DIR / "orders.json"
config.FEEDBACK_FILE = TEST_DATA_DIR / "feedback.json"
config.CARTS_FILE = TEST_DATA_DIR / "carts.json"
config.CONVERSATIONS_DIR = TEST_CONVERSATIONS_DIR

from utils import inventory_manager, order_manager, cart_manager
import agent.agent as agent_module
from agent.agent import run_agent

ONE_PRODUCT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 5}],
        }
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(ONE_PRODUCT_INVENTORY, f)
    with open(config.ORDERS_FILE, "w") as f:
        json.dump([], f)
    with open(config.FEEDBACK_FILE, "w") as f:
        json.dump([], f)
    with open(config.CARTS_FILE, "w") as f:
        json.dump({}, f)
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()

    inventory_manager._cache["mtime"] = None

    yield

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()


# --- test doubles -----------------------------------------------------

class _FakeMessage(SimpleNamespace):
    """
    Mimics an OpenAI ChatCompletionMessage closely enough for agent.py's
    needs, including model_dump() — agent.py calls
    choice.model_dump(exclude_unset=True) on a successful structured
    tool-call turn.
    """
    def model_dump(self, exclude_unset: bool = False) -> dict:
        tool_calls = None
        if self.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        return {"role": "assistant", "content": self.content, "tool_calls": tool_calls}


def _message(content: str | None, tool_calls=None):
    return _FakeMessage(content=content, tool_calls=tool_calls)


def _response(content: str | None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=_message(content, tool_calls))])


def _tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _bad_request_error(failed_generation: str) -> openai.BadRequestError:
    """Mirrors the helper in test_leaked_json_recovery.py — builds a real
    openai.BadRequestError shaped the way agent.py expects to read it."""
    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    body = {"error": {"failed_generation": failed_generation}}
    resp = httpx.Response(400, request=req, json=body)
    return openai.BadRequestError("bad request", response=resp, body=body)


class FakeClient:
    def __init__(self, responses: list):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )


def _patch_client(monkeypatch, responses: list):
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))
    return fake_client


async def _place_unpaid_order(user_id: int, username: str = "user") -> dict:
    """Helper: gets a real order into 'pending_confirmation' status (i.e.
    awaiting payment) via the real business logic, so submit_payment_reference
    has something to attach to."""
    await cart_manager.set_cart(
        user_id, [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]
    )
    order = await order_manager.place_order(user_id, username, await cart_manager.get_cart(user_id))
    await cart_manager.set_cart(user_id, [])
    return order


# --- structured tool-call path -------------------------------------------

@pytest.mark.asyncio
async def test_structured_call_emits_payment_reference_event(monkeypatch):
    """Core Issue 1.3 case: a normal structured submit_payment_reference
    tool call must populate `events` with a 'payment_reference' entry, the
    same way place_order/log_feedback already do."""
    order = await _place_unpaid_order(user_id=1, username="alice")

    responses = [
        _response(
            content=None,
            tool_calls=[_tool_call("call_1", "submit_payment_reference", {"reference": "TX12345"})],
        ),
        _response(content="Thanks! Your payment is under review.", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=1, username="alice", user_message="TX12345")

    assert len(events) == 1
    assert events[0]["type"] == "payment_reference"
    data = events[0]["data"]
    assert data["order_id"] == order["order_id"]
    assert data["username"] == "alice"
    assert data["payment_proof"] == "TX12345"
    assert data["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_structured_call_no_event_when_no_order_awaiting_payment(monkeypatch):
    """If submit_payment_reference fails (no order awaiting payment for this
    customer), no event should be emitted — mirrors how place_order/
    log_feedback only append on their success status."""
    responses = [
        _response(
            content=None,
            tool_calls=[_tool_call("call_1", "submit_payment_reference", {"reference": "TX999"})],
        ),
        _response(content="I don't see an order waiting on payment for you.", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=2, username="bob", user_message="TX999")

    assert events == []


# --- native-function-call recovery path (BadRequestError branch) ---------

@pytest.mark.asyncio
async def test_native_recovery_emits_payment_reference_event(monkeypatch):
    """A submit_payment_reference call recovered from a native
    <function=...> tag (via a BadRequestError) must emit the event too —
    recovery paths must mirror the structured-call path exactly, per the
    project's 'silent gap' principle."""
    order = await _place_unpaid_order(user_id=3, username="carol")

    async def raise_native_tag(*args, **kwargs):
        raise _bad_request_error(
            '<function=submit_payment_reference{"reference": "TX777"}</function>'
        )

    responses_sequence = [
        raise_native_tag,
        lambda *a, **k: _response(content="Got it, thanks!", tool_calls=None),
    ]
    call_count = {"n": 0}

    async def side_effect(*a, **k):
        fn = responses_sequence[call_count["n"]]
        call_count["n"] += 1
        result = fn(*a, **k)
        if hasattr(result, "__await__"):
            return await result
        return result

    fake_client = FakeClient([])
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=3, username="carol", user_message="TX777")

    assert len(events) == 1
    assert events[0]["type"] == "payment_reference"
    assert events[0]["data"]["order_id"] == order["order_id"]
    assert events[0]["data"]["payment_proof"] == "TX777"


# --- leaked-plain-text-JSON recovery path ---------------------------------

@pytest.mark.asyncio
async def test_leaked_json_recovery_emits_payment_reference_event(monkeypatch):
    """Same as above, but for the leaked-plain-text-JSON recovery path (no
    API error at all — the model just wrote the JSON as normal reply
    content)."""
    order = await _place_unpaid_order(user_id=4, username="dave")

    leaked = json.dumps({
        "name": "submit_payment_reference",
        "parameters": {"reference": "TX555"},
    })
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Thanks, we'll review it shortly.", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=4, username="dave", user_message="TX555")

    assert len(events) == 1
    assert events[0]["type"] == "payment_reference"
    assert events[0]["data"]["order_id"] == order["order_id"]
    assert events[0]["data"]["payment_proof"] == "TX555"


# --- regression: existing event types unaffected --------------------------

@pytest.mark.asyncio
async def test_place_order_and_log_feedback_events_unaffected(monkeypatch):
    """Sanity check that adding the new event type didn't disturb the
    existing place_order/log_feedback event tracking in the structured-call
    path."""
    await cart_manager.set_cart(5, [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}])

    responses = [
        _response(content=None, tool_calls=[_tool_call("call_1", "place_order", {})]),
        _response(content="Great, that's checked out. Please send your payment proof.", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=5, username="erin", user_message="checkout")

    assert len(events) == 1
    assert events[0]["type"] == "order"
    assert events[0]["data"]["status"] == "pending_confirmation"


def main():
    """Lets this be run directly with `python test_payment_reference_notification.py`,
    matching the style of the other root-level test_*.py files."""
    import asyncio
    import inspect

    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]

    class _Monkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    passed = 0
    for name, fn in test_fns:
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(ONE_PRODUCT_INVENTORY, f)
        with open(config.ORDERS_FILE, "w") as f:
            json.dump([], f)
        with open(config.FEEDBACK_FILE, "w") as f:
            json.dump([], f)
        with open(config.CARTS_FILE, "w") as f:
            json.dump({}, f)
        for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
            f.unlink()
        inventory_manager._cache["mtime"] = None

        mp = _Monkeypatch()
        try:
            asyncio.run(fn(mp))
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
        finally:
            mp.undo()

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
