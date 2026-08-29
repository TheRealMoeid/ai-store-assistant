# test_narration_guard_loop.py
"""
Covers the fix for the narration-guard early-return bug (bug-audit-evaluation.md
item 1.2, HIGHEST PRIORITY): when the model narrates an action ("I've added
that to your cart!") without actually calling the tool, the guard in
agent/agent.py detects the commitment phrase and appends a correction message
to `messages` — but previously followed that with `return reply, events`
immediately, so the correction was never sent to the model, the customer
received the false confirmation as-is, and the cart/order never actually
changed.

The fix replaces that `return` with `continue`, so the loop repeats and the
model gets a real chance to call the tool. This suite exercises the full
`run_agent` loop with the LLM client mocked (matching the sandboxing pattern
already used by test_leaked_json_recovery.py): config paths are redirected to
a throwaway test_data/ directory so real data/ is never touched, and only
`agent.agent.get_client` is mocked — everything else (call_tool, cart_manager,
inventory_manager, order_manager, conversation_manager) is the real business
logic.

Sanity-checked against the pre-fix code (the `return` version) to confirm
these tests actually fail there before confirming they pass on the fix.
"""
import sys
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

from utils import inventory_manager, order_manager, cart_manager, conversation_manager
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
    `choice.model_dump(exclude_unset=True)` to append a successful
    structured tool-call turn to `messages`, which a plain SimpleNamespace
    doesn't support.
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
    """Mimics an OpenAI ChatCompletionMessageToolCall enough for agent.py's needs."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class FakeClient:
    def __init__(self, responses: list):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )


def _patch_client(monkeypatch, responses: list):
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))
    return fake_client


# --- core fix: guard loops back instead of returning early ---------------

@pytest.mark.asyncio
async def test_narration_guard_loops_back_and_tool_actually_runs(monkeypatch):
    """
    Round 1: model narrates "I've added that to your cart!" with no tool
    call. Round 2 (only reachable if the guard actually `continue`s instead
    of `return`ing): the model, having seen the correction message, calls
    add_to_cart for real. Round 3: model gives a clean final reply.

    Before the fix, this would never reach round 2 at all — the mock's
    side_effect list would be under-consumed and the agent would return the
    round-1 narration text with an empty cart.
    """
    responses = [
        _response(content="I've added that to your cart!", tool_calls=None),  # round 1: narration, no tool call
        _response(
            content=None,
            tool_calls=[_tool_call("call_1", "add_to_cart", {
                "product_id": "p001", "size": "42", "color": "black", "quantity": 1,
            })],
        ),  # round 2: model self-corrects and calls the real tool
        _response(content="All set! Anything else?", tool_calls=None),  # round 3: final reply
    ]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=1, username="alice", user_message="add sneakers size 42")

    # All three rounds were actually consumed — proves the loop continued
    # past the guard instead of returning after round 1.
    assert fake_client.chat.completions.create.await_count == 3
    assert reply == "All set! Anything else?"

    # The tool call really ran — the item is genuinely in the cart, not
    # just claimed to be.
    cart = await cart_manager.get_cart(1)
    assert cart == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]


@pytest.mark.asyncio
async def test_narration_guard_correction_message_reaches_the_model(monkeypatch):
    """
    Verifies the correction message the guard appends is actually present
    in the `messages` payload sent to the model on the *next* call — i.e.
    it isn't just appended and then discarded, which was the actual bug.
    """
    responses = [
        _response(content="Your order is placed!", tool_calls=None),  # round 1: narration
        _response(content="Sorry, what would you like to order?", tool_calls=None),  # round 2
    ]
    fake_client = _patch_client(monkeypatch, responses)

    await run_agent(user_id=2, username="bob", user_message="checkout")

    assert fake_client.chat.completions.create.await_count == 2
    second_call_kwargs = fake_client.chat.completions.create.await_args_list[1].kwargs
    second_messages = second_call_kwargs["messages"]

    correction_texts = [
        m["content"] for m in second_messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert any("MUST call the appropriate tool" in text for text in correction_texts), (
        "the guard's correction message must actually be part of the next "
        "request to the model, not silently dropped"
    )


@pytest.mark.asyncio
async def test_narrated_reply_is_not_persisted_to_conversation_history(monkeypatch):
    """
    The narrated (unverified) reply from round 1 is an internal retry step,
    not a real completed turn, so it must not be written to persisted
    conversation history — only the eventual genuine final reply should be.
    """
    responses = [
        _response(content="I've added that to your cart!", tool_calls=None),  # round 1: narration
        _response(content="Sure — what size and color?", tool_calls=None),  # round 2: real reply
    ]
    _patch_client(monkeypatch, responses)

    reply, _ = await run_agent(user_id=3, username="carol", user_message="add sneakers")

    assert reply == "Sure — what size and color?"

    history = await conversation_manager.load_history(3)
    assistant_messages = [m["content"] for m in history if m.get("role") == "assistant"]

    assert "I've added that to your cart!" not in assistant_messages
    assert assistant_messages == ["Sure — what size and color?"]


# --- guard firing repeatedly must still degrade gracefully ---------------

@pytest.mark.asyncio
async def test_repeated_narration_hits_safety_cap_without_crashing(monkeypatch):
    """
    If the model keeps narrating every round (never actually calls a tool),
    the loop must not hang forever — it should exhaust MAX_TOOL_ROUNDS and
    fall back to the generic message, same as any other exhausted-rounds
    case. Confirms `continue` doesn't turn a persistent narration failure
    mode into an infinite loop; MAX_TOOL_ROUNDS is temporarily lowered so
    the test doesn't need 9+ mocked responses.
    """
    monkeypatch.setattr(agent_module, "MAX_TOOL_ROUNDS", 3)

    responses = [
        _response(content="I've added that to your cart!", tool_calls=None),
        _response(content="Your order is confirmed!", tool_calls=None),
        _response(content="I'll add that to your cart.", tool_calls=None),
    ]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=4, username="dave", user_message="add sneakers")

    assert fake_client.chat.completions.create.await_count == 3
    assert reply == "Sorry, I'm having trouble finishing that request — could you try rephrasing?"
    assert events == []
    # Cart must still be empty — nothing was ever actually added.
    assert await cart_manager.get_cart(4) == []


# --- regression: legitimate non-narration paths must be unaffected -------

@pytest.mark.asyncio
async def test_negative_phrasing_is_not_treated_as_narration(monkeypatch):
    """
    "I haven't added it yet" must NOT trigger the guard — this is the
    existing is_negative filter, unchanged by this fix. Should return
    immediately in a single round, exactly as before.
    """
    responses = [_response(content="I haven't added it to your cart yet — what size?", tool_calls=None)]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=5, username="erin", user_message="add sneakers")

    assert fake_client.chat.completions.create.await_count == 1
    assert reply == "I haven't added it to your cart yet — what size?"
    assert events == []


@pytest.mark.asyncio
async def test_plain_reply_without_commitment_language_unaffected(monkeypatch):
    """Ordinary conversational text with no commitment phrases must still
    return immediately, same as before the fix."""
    responses = [_response(content="Sure, what size are you looking for?", tool_calls=None)]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=6, username="frank", user_message="I want sneakers")

    assert fake_client.chat.completions.create.await_count == 1
    assert reply == "Sure, what size are you looking for?"
    assert events == []


@pytest.mark.asyncio
async def test_narration_guard_place_order_flow_reaches_real_event_tracking(monkeypatch):
    """
    Same shape as the add_to_cart case above but for place_order, to confirm
    the fix works for the checkout-narration failure mode too (the one
    called out explicitly in prompts.py: "never say order is confirmed/
    placed before place_order actually succeeds"), and that the `events`
    list — used to notify the admin group — only gets populated once the
    real tool call happens in the follow-up round.
    """
    await cart_manager.set_cart(7, [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}])

    responses = [
        _response(content="Your order is confirmed! Thanks for shopping with us.", tool_calls=None),  # narration
        _response(
            content=None,
            tool_calls=[_tool_call("call_1", "place_order", {})],
        ),  # self-correction: real tool call
        _response(content="Thanks! Please send your payment proof to finish up.", tool_calls=None),  # final reply
    ]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=7, username="gina", user_message="checkout please")

    assert fake_client.chat.completions.create.await_count == 3
    assert len(events) == 1
    assert events[0]["type"] == "order"
    assert events[0]["data"]["status"] == "pending_confirmation"
    # Cart really cleared as part of a genuine place_order execution.
    assert await cart_manager.get_cart(7) == []


def main():
    """Lets this be run directly with `python test_narration_guard_loop.py`,
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
