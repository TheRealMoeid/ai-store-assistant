# test/test_recovery_tool_call_ids.py
"""
Covers backlog Issue 2.1: "Hardcoded recovery tool_call_ids".

The bug: both malformed-tool-call recovery paths in agent/agent.py (the
BadRequestError/native-tag path, and the leaked-plain-text-JSON path)
synthesized a fake assistant `tool_calls` entry + matching `tool` result
using a single hardcoded literal ID for every recovery event in the
process's lifetime — "recovered_1" for the native-tag path, and
"recovered_json_1" for the leaked-JSON path. If recovery fired more than
once for the same user within the conversation-history trimming window
(MAX_HISTORY_MESSAGES), the persisted/sent history could contain two
messages sharing the same tool_call_id, which OpenAI-compatible APIs may
reject — the same failure class this recovery logic exists to work
around.

The fix: `_new_recovery_tool_call_id(prefix)` generates a unique ID per
recovery event via `uuid.uuid4().hex`, keeping the recognizable
"recovered_"/"recovered_json_" prefix for debuggability. The ID is
generated exactly once per recovery event and reused for both the
synthetic assistant `tool_calls[].id` and the following `tool` message's
`tool_call_id`, since the two must correlate.

This suite is deliberately narrow and additive to the existing recovery
coverage in test_leaked_json_recovery.py / test_nested_json_regex.py: it
only asserts on ID uniqueness and correlation, not on the recovery/business
logic those files already cover.

Sandboxes config paths to test_data/, same pattern as the rest of the
suite, so real data/ is never touched. Only agent.agent.get_client is
mocked — call_tool and the real business logic run for real.
"""
import sys
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

from utils import inventory_manager, cart_manager
import agent.agent as agent_module
from agent.agent import run_agent, _new_recovery_tool_call_id

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
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _response(content: str | None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=_message(content, tool_calls))])


def _bad_request_error(failed_generation: str) -> openai.BadRequestError:
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


def _drive_with_sequence(monkeypatch, responses_sequence):
    """Like FakeClient but allows a mix of raise-callables and response
    objects in order, for tests that interleave BadRequestError recovery
    rounds with normal responses."""
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
    return fake_client


# --- unit-level: the generator itself --------------------------------------

def test_generator_produces_unique_ids_with_recognizable_prefix():
    ids = {_new_recovery_tool_call_id("recovered") for _ in range(100)}
    assert len(ids) == 100, "expected 100 distinct calls to produce 100 distinct IDs"
    for i in ids:
        assert i.startswith("recovered_")
        assert i != "recovered_1", "must never fall back to the old static literal"


def test_generator_respects_the_prefix_passed_in():
    native_id = _new_recovery_tool_call_id("recovered")
    leaked_id = _new_recovery_tool_call_id("recovered_json")
    assert native_id.startswith("recovered_") and not native_id.startswith("recovered_json_")
    assert leaked_id.startswith("recovered_json_")


# --- core fix: two recovery events in one conversation get different IDs --

@pytest.mark.asyncio
async def test_two_native_tag_recoveries_in_one_conversation_get_different_ids(monkeypatch):
    """
    Core Issue 2.1 case: fire the native-tag (BadRequestError) recovery
    path twice within the same run_agent call and confirm the two
    synthetic tool_call_ids written to messages/history are different —
    before the fix, both would have been the literal "recovered_1".
    """
    responses_sequence = [
        lambda *a, **k: (_ for _ in ()).throw(
            _bad_request_error('<function=check_availability{"product_id": "p001"}</function>')
        ),
        lambda *a, **k: (_ for _ in ()).throw(
            _bad_request_error('<function=check_availability{"product_id": "p001", "size": "42"}</function>')
        ),
        lambda *a, **k: _response(content="Here's what's available.", tool_calls=None),
    ]

    async def side_effect(*a, **k):
        fn = responses_sequence[side_effect.n]
        side_effect.n += 1
        gen = fn(*a, **k)
        if hasattr(gen, "__await__"):
            return await gen
        return gen
    side_effect.n = 0

    fake_client = FakeClient([])
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=1, username="alice", user_message="what's available in 42?")

    assert reply == "Here's what's available."
    assert fake_client.chat.completions.create.await_count == 3

    # Inspect the actual `messages` payload sent on the *third* call — it
    # must contain both recovered assistant tool_calls entries, each with
    # its own tool_call_id.
    third_call_messages = fake_client.chat.completions.create.await_args_list[2].kwargs["messages"]
    recovered_ids = [
        m["tool_calls"][0]["id"]
        for m in third_call_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(recovered_ids) == 2, "expected two recovered assistant tool_calls entries"
    assert recovered_ids[0] != recovered_ids[1], (
        "two recovery events in the same conversation must not reuse the same tool_call_id"
    )
    assert all(i.startswith("recovered_") for i in recovered_ids)


@pytest.mark.asyncio
async def test_two_leaked_json_recoveries_in_one_conversation_get_different_ids(monkeypatch):
    """Same core case as above, but for the leaked-plain-text-JSON recovery
    path — two leaked-JSON tool calls in one conversation must not reuse
    the old static "recovered_json_1" literal for both."""
    leaked_1 = json.dumps({"name": "check_availability", "parameters": {"product_id": "p001"}})
    leaked_2 = json.dumps({"name": "check_availability", "parameters": {"product_id": "p001", "size": "42"}})

    responses = [
        _response(content=leaked_1, tool_calls=None),
        _response(content=leaked_2, tool_calls=None),
        _response(content="All set.", tool_calls=None),
    ]
    fake_client = _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=2, username="bob", user_message="check stock twice")

    assert reply == "All set."
    assert fake_client.chat.completions.create.await_count == 3

    third_call_messages = fake_client.chat.completions.create.await_args_list[2].kwargs["messages"]
    recovered_ids = [
        m["tool_calls"][0]["id"]
        for m in third_call_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(recovered_ids) == 2
    assert recovered_ids[0] != recovered_ids[1]
    assert all(i.startswith("recovered_json_") for i in recovered_ids)


# --- correlation: assistant tool_calls[].id must match the tool result ----

@pytest.mark.asyncio
async def test_native_recovery_assistant_and_tool_message_ids_correlate(monkeypatch):
    """
    The synthetic assistant message's tool_calls[0].id and the following
    tool message's tool_call_id must be the exact same generated value —
    not two independently-generated IDs that happen to both be unique but
    fail to correlate with each other.
    """
    responses_sequence = [
        lambda *a, **k: (_ for _ in ()).throw(
            _bad_request_error('<function=log_feedback{"kind": "compliment", "message": "great!"}</function>')
        ),
        lambda *a, **k: _response(content="Thanks!", tool_calls=None),
    ]

    async def side_effect(*a, **k):
        fn = responses_sequence[side_effect.n]
        side_effect.n += 1
        gen = fn(*a, **k)
        if hasattr(gen, "__await__"):
            return await gen
        return gen
    side_effect.n = 0

    fake_client = FakeClient([])
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    await run_agent(user_id=3, username="carol", user_message="great store!")

    second_call_messages = fake_client.chat.completions.create.await_args_list[1].kwargs["messages"]

    assistant_msg = next(m for m in second_call_messages if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")

    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]
    assert assistant_msg["tool_calls"][0]["id"].startswith("recovered_")


@pytest.mark.asyncio
async def test_leaked_json_recovery_assistant_and_tool_message_ids_correlate(monkeypatch):
    leaked = json.dumps({"name": "log_feedback", "parameters": {"kind": "compliment", "message": "nice!"}})
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Appreciate it!", tool_calls=None),
    ]
    fake_client = _patch_client(monkeypatch, responses)

    await run_agent(user_id=4, username="dave", user_message="nice shop!")

    second_call_messages = fake_client.chat.completions.create.await_args_list[1].kwargs["messages"]

    assistant_msg = next(m for m in second_call_messages if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")

    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]
    assert assistant_msg["tool_calls"][0]["id"].startswith("recovered_json_")


# --- regression: a single recovery event still works end-to-end -----------

@pytest.mark.asyncio
async def test_single_native_recovery_still_works_end_to_end(monkeypatch):
    """Regression check: a lone native-tag recovery (no second recovery
    event) must still execute the tool for real and reach a clean final
    reply, unaffected by switching from a static ID to a generated one."""
    responses_sequence = [
        lambda *a, **k: (_ for _ in ()).throw(
            _bad_request_error('<function=add_to_cart{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}</function>')
        ),
        lambda *a, **k: _response(content="All set — anything else?", tool_calls=None),
    ]

    async def side_effect(*a, **k):
        fn = responses_sequence[side_effect.n]
        side_effect.n += 1
        gen = fn(*a, **k)
        if hasattr(gen, "__await__"):
            return await gen
        return gen
    side_effect.n = 0

    fake_client = FakeClient([])
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=5, username="erin", user_message="add sneakers size 42")

    assert reply == "All set — anything else?"
    cart = await cart_manager.get_cart(5)
    assert cart == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]


@pytest.mark.asyncio
async def test_single_leaked_json_recovery_still_works_end_to_end(monkeypatch):
    """Same regression check for the leaked-JSON path."""
    leaked = json.dumps({
        "name": "add_to_cart",
        "parameters": {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
    })
    responses = [
        _response(content=leaked, tool_calls=None),
        _response(content="Added! Anything else?", tool_calls=None),
    ]
    _patch_client(monkeypatch, responses)

    reply, events = await run_agent(user_id=6, username="frank", user_message="add sneakers")

    assert reply == "Added! Anything else?"
    cart = await cart_manager.get_cart(6)
    assert cart == [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]


def main():
    """Lets this be run directly with `python test/test_recovery_tool_call_ids.py`,
    matching the style of the other test_*.py files in this project."""
    import asyncio
    import inspect

    sync_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn)
    ]
    async_test_fns = [
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
    total = len(sync_test_fns) + len(async_test_fns)

    for name, fn in sync_test_fns:
        try:
            fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for name, fn in async_test_fns:
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
    print(f"\n{passed}/{total} passed")


if __name__ == "__main__":
    main()
