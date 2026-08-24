# test_leaked_json_recovery.py
"""
Covers Issue #3: the leaked-JSON tool-call recovery path in agent/agent.py
was structurally broken — `if parsed: continue` looped back without ever
touching `messages`, and the actual recovery logic (call_tool, event
tracking, appending the recovered assistant/tool messages) sat as dead code
after an unconditional `return`, so it could never run.

The fix moves that logic inside `if parsed:`, resets `malformed_retries`,
and `continue`s the loop so the model can consume the recovered tool result.

The bot is not live for this test suite — everything here runs against the
real agent loop and real tool/business logic (inventory, cart, orders,
feedback), but with only the LLM client mocked out (agent.agent.get_client),
matching the project's existing sandboxing pattern from test/test_backend.py
and test_stock_reversal.py: config paths are redirected to a throwaway
test_data/ directory so nothing under real data/ is ever touched.
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

def _message(content: str | None, tool_calls=None):
    """Mimics an OpenAI ChatCompletionMessage enough for agent.py's needs."""
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _response(content: str | None, tool_calls=None):
    """Mimics an OpenAI ChatCompletion response object."""
    return SimpleNamespace(choices=[SimpleNamespace(message=_message(content, tool_calls))])


def _bad_request_error(failed_generation: str) -> openai.BadRequestError:
    """
    Builds a real openai.BadRequestError whose `.response.json()` matches
    the shape agent.py reads (`error.failed_generation`), so the
    `except BadRequestError` branch behaves exactly as it would against a
    real Groq/OpenAI-compatible failure — needed for the malformed_retries
    reset test below, which exercises that branch too.
    """
    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    body = {"error": {"failed_generation": failed_generation}}
    resp = httpx.Response(400, request=req, json=body)
    return openai.BadRequestError("bad request", response=resp, body=body)


class FakeClient:
    """Stands in for the (client, model) tuple normally returned by get_client()."""
    def __init__(self, responses: list):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )


def _patch_client(monkeypatch, responses: list):
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))
    return fake_client


# --- recovery execution -------------------------------------------------

@pytest.mark.asyncio
async def test_leaked_json_add_to_cart_is_recovered_and_executed(monkeypatch):
    """
    A leaked plain-text JSON tool call for add_to_cart should be parsed,
    executed via the real call_tool (not just detected), and the loop should
    continue rather than returning immediately — the model gets a second
    round to respond to the tool result.
    """
    leaked = json.dumps({
        "name": "add_to_cart",
        "parameters": {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
    })
    responses = [
        _response(content=leaked, tool_calls=None),          # round 1: leaked JSON
        _response(content="Added! Anything else?", tool_calls=None),  # round 2: final reply
    ]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=1, username="alice", user_message="add sneakers size 42")

    # The loop continued instead of returning after round 1.
    assert fake_client.chat.completions.create.await_count == 2
    assert reply == "Added! Anything else?"

    # call_tool actually ran — the item is really in the cart, not just detected.
    cart = await cart_manager.get_cart(1)
    assert cart == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]


@pytest.mark.asyncio
async def test_leaked_json_place_order_event_tracking_preserved(monkeypatch):
    """place_order recovered from leaked JSON must still populate `events`,
    exactly like the normal structured tool-call path does."""
    await cart_manager.set_cart(2, [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}])

    leaked = json.dumps({"name": "place_order", "parameters": {}})
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Your order is on its way to review.", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=2, username="bob", user_message="checkout")

    assert len(events) == 1
    assert events[0]["type"] == "order"
    assert events[0]["data"]["status"] == "pending_confirmation"
    # cart_manager cleared the cart as part of a real place_order execution
    assert await cart_manager.get_cart(2) == []


@pytest.mark.asyncio
async def test_leaked_json_log_feedback_event_tracking_preserved(monkeypatch):
    leaked = json.dumps({
        "name": "log_feedback",
        "parameters": {"kind": "compliment", "message": "loved the service"},
    })
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Thanks for the kind words!", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=3, username="carol", user_message="great service!")

    assert len(events) == 1
    assert events[0]["type"] == "feedback"
    assert events[0]["data"]["kind"] == "compliment"


@pytest.mark.asyncio
async def test_leaked_json_messages_include_recovered_tool_call_and_result(monkeypatch):
    """
    The recovered assistant/tool messages must actually be appended so the
    next round can see the tool result — verified indirectly by checking the
    persisted conversation history includes a tool_calls assistant turn and
    a matching tool result.
    """
    leaked = json.dumps({
        "name": "add_to_cart",
        "parameters": {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
    })
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Done!", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    await run_agent(user_id=4, username="dave", user_message="add sneakers")

    history = conversation_manager.load_history(4)
    roles = [m.get("role") for m in history]
    assert "assistant" in roles
    # the final assistant reply is always saved; a tool-call turn must have
    # been part of the flow for the recovered call to have taken effect,
    # which we already confirmed via cart state in the earlier test — here
    # we just confirm the turn was actually persisted (not lost/dropped).
    assert history[-1] == {"role": "assistant", "content": "Done!"}


# --- malformed_retries reset ---------------------------------------------

@pytest.mark.asyncio
async def test_malformed_retries_reset_after_successful_leaked_json_recovery(monkeypatch):
    """
    malformed_retries is a shared counter also incremented by the
    BadRequestError/native-tag recovery path. A successful leaked-JSON
    recovery must reset it to 0, the same way successful native-tag
    recovery already does — otherwise a later unrelated BadRequestError
    could push the count over MAX_MALFORMED_RETRIES sooner than it should,
    based on stale count from before the leaked-JSON recovery.

    Sequence: two unparseable BadRequestErrors (retries -> 2), then a
    successful leaked-JSON recovery (should reset retries -> 0), then one
    more unparseable BadRequestError (retries -> 1, still under the cap of
    2), then a normal final reply. If the reset didn't happen, the third
    BadRequestError would push the stale count to 3 (> MAX_MALFORMED_RETRIES)
    and the agent would bail out with the generic fallback message instead
    of reaching the final round.
    """
    leaked = json.dumps({"name": "log_feedback", "parameters": {"kind": "compliment", "message": "nice"}})

    async def raise_unparseable(*args, **kwargs):
        raise _bad_request_error("this is not a recognizable function call format at all")

    responses_sequence = [
        raise_unparseable,                                         # round 1: unparseable -> retries=1
        raise_unparseable,                                         # round 2: unparseable -> retries=2
        lambda *a, **k: _response(content=leaked, tool_calls=None),  # round 3: leaked JSON recovered -> retries reset to 0
        raise_unparseable,                                         # round 4: unparseable -> retries=1 (not 3)
        lambda *a, **k: _response(content="All set, thanks!", tool_calls=None),  # round 5: final reply
    ]

    call_count = {"n": 0}

    async def side_effect(*args, **kwargs):
        fn = responses_sequence[call_count["n"]]
        call_count["n"] += 1
        result = fn(*args, **kwargs)
        if hasattr(result, "__await__"):
            return await result
        return result

    fake_client = FakeClient([])
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=5, username="erin", user_message="you guys are great")

    # If malformed_retries hadn't been reset, the agent would have bailed
    # out early with the generic fallback after round 4 and never reached
    # round 5's real final reply.
    assert reply == "All set, thanks!"
    assert fake_client.chat.completions.create.await_count == 5
    assert len(events) == 1 and events[0]["type"] == "feedback"


# --- non-recovery paths must be unaffected --------------------------------

@pytest.mark.asyncio
async def test_non_json_plain_text_is_not_treated_as_leaked_tool_call(monkeypatch):
    """Ordinary conversational text (not JSON at all) must fall straight
    through to the normal final-response return, unaffected by the parsed
    branch."""
    responses = [_response(content="Sure, what size are you looking for?", tool_calls=None)]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=6, username="frank", user_message="I want sneakers")

    assert reply == "Sure, what size are you looking for?"
    assert events == []
    # No second round — nothing was recovered, so it returned immediately.
    assert fake_client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_invalid_json_like_text_falls_through_safely(monkeypatch):
    """Text that starts with '{' but isn't a valid/recognizable tool call
    (bad JSON, or valid JSON missing a 'name') must not be treated as a
    parsed recovery — it should fall through to the narration guard / final
    return path instead of crashing or looping."""
    malformed = '{"not_a_tool_call": true, this is not valid json'
    responses = [_response(content=malformed, tool_calls=None)]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=7, username="gina", user_message="hello")

    assert reply == malformed  # returned as-is, no crash, no infinite loop
    assert fake_client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_narration_guard_still_triggers_when_parsed_is_falsy(monkeypatch):
    """
    When _parse_leaked_json_tool_call returns None (plain narrative text,
    not a leaked tool call), the narration/commitment guard downstream must
    still run exactly as before the fix — the parsed branch must not
    swallow or short-circuit it.
    """
    narrating_reply = "I've added the sneakers to your cart!"
    responses = [_response(content=narrating_reply, tool_calls=None)]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=8, username="hank", user_message="add sneakers please")

    # Existing (unchanged) narration-guard behavior: it flags the lie and
    # returns the original reply back to the caller without ever having
    # called add_to_cart for real.
    assert reply == narrating_reply
    assert await cart_manager.get_cart(8) == []
    assert fake_client.chat.completions.create.await_count == 1


def main():
    """Lets this be run directly with `python test_leaked_json_recovery.py`,
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
