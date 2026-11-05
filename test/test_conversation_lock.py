# test_conversation_lock.py
"""
Covers Issue 3.3 (bug-audit-evaluation.md numbering): "conversation_manager
has no asyncio.Lock."

The bug: utils/conversation_manager.py::append_messages() did a raw
read-modify-write (load_history -> extend -> json.dump) with no lock at
all, unlike cart_manager.py/order_manager.py, which both guard the same
shape of operation with asyncio.Lock. Two near-simultaneous
append_messages() calls for the same user could both read the same
on-disk snapshot, both extend() independently, and whichever write landed
second would silently overwrite (lose) the other's messages.

The fix (Approach A, per bug-audit-evaluation-discussion.md and the
follow-up implementation discussion): a single module-level
`_history_lock = asyncio.Lock()`, matching cart_manager.py/
order_manager.py's existing single-lock-per-file pattern exactly (no
per-user dict of locks). Because asyncio.Lock is not reentrant,
load_history()/append_messages()/get_recent_for_llm() are now async
wrappers around unlocked `_load_history_sync`/`_append_messages_sync`
helpers -- the wrappers acquire _history_lock exactly once each; the sync
helpers never acquire it themselves, avoiding the deadlock that would
occur if a locked function tried to call another locked function.

This suite covers three things:
  1. The core race-condition fix -- concurrent append_messages() calls for
     the same user must not lose any messages (mirrors test_concurrency.py's
     approach for order_manager.place_order, but targeting
     conversation_manager instead).
  2. The reentrancy-safety of the split design itself -- get_recent_for_llm()
     (which internally reads history while, in the fixed code, briefly
     holding the same lock append_messages() uses) must not deadlock when
     run concurrently with append_messages() calls.
  3. Regression checks: single-call behavior, and the existing safe-trim
     logic in get_recent_for_llm() are both unaffected by the locking change.

Sandboxes config.CONVERSATIONS_DIR to a throwaway test_data/conversations
directory, matching the project's established sandboxing pattern, so real
data/conversations/ is never touched.

Updated for Issue #16 (bug-audit 3.4, async/atomic file I/O). Before that
fix, _load_history_sync/_append_messages_sync were pure synchronous code
with no actual `await` inside the critical section, so even a "concurrent"
asyncio.gather() of many append_messages() calls never genuinely
interleaved on the lock -- each acquire()/do-work/release() ran as one
uninterrupted burst, since asyncio.Lock's uncontended fast path never
yields to the loop at all. (This project already hit the exact same gap
for _checkout_lock -- see test_checkout_atomicity.py's
_widen_race_window() docstring -- and had to force genuine interleaving
there with an artificial await point.) Post-Issue-#16, the sync helpers
now call `await asyncio.to_thread(...)` inside the lock -- a real yield
point -- so concurrent calls to append_messages()/load_history() genuinely
contend _history_lock for the first time.

That's a good thing (it's what makes the concurrency tests below actually
test something), but it surfaces the same pytest-asyncio cross-test
artifact already documented for _checkout_lock: asyncio.Lock only binds
itself to a specific running event loop the first time it's genuinely
contended (its fast, uncontended path never touches the loop). Since
_history_lock is a module-level singleton shared across every test
function in this file, and pytest-asyncio gives each test function its
own fresh event loop by default, a test that genuinely contends the lock
binds it to that test's loop -- and a *later* test that also genuinely
contends it raises "is bound to a different event loop" (confirmed live:
test_concurrent_appends_for_different_users_dont_interfere and
test_load_history_and_append_messages_interleaved_no_deadlock both failed
this way on a real `pytest . -v` run once Issue #16 landed, even though
both had always passed before -- their own logic didn't change, only
_history_lock's new genuine contention did). This is purely a
test-isolation artifact, not a real bug: the actual bot process has
exactly one long-lived event loop for its whole lifetime, so it never
occurs in production. The three tests that actually drive genuine
concurrent contention on _history_lock (via asyncio.gather of multiple
tasks that each hit a real await inside the lock) now take a `monkeypatch`
fixture and reset `conversation_manager._history_lock` to a fresh
`asyncio.Lock()` at the start of the test, exactly mirroring
test_checkout_atomicity.py's existing `_widen_race_window()` workaround
for `_checkout_lock`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncio
import json

import pytest

TEST_DATA_DIR = Path("test_data")
TEST_CONVERSATIONS_DIR = TEST_DATA_DIR / "conversations"
TEST_CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

import config
config.CONVERSATIONS_DIR = TEST_CONVERSATIONS_DIR
config.MAX_HISTORY_MESSAGES = 20

from utils import conversation_manager


@pytest.fixture(autouse=True)
def setup_test_data():
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()
    yield
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()


def _fresh_history_lock(monkeypatch):
    """
    Gives _history_lock a brand-new asyncio.Lock() instance bound to THIS
    test's event loop, instead of reusing the module-level singleton that
    may already be bound to an earlier test's (now-closed) loop. See the
    module docstring above for why this became necessary once Issue #16
    made the lock genuinely contendable. Only needed by tests that
    actually drive real concurrent contention on the lock (multiple tasks
    via asyncio.gather, each hitting a genuine await inside the locked
    section) -- single-call tests and tests that only await sequentially
    in a loop (no gather) never contend the lock for real and don't need
    this.
    """
    monkeypatch.setattr(conversation_manager, "_history_lock", asyncio.Lock())


def _msg(i: int) -> dict:
    return {"role": "user", "content": f"message {i}"}


# --- core race-condition fix ---------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_appends_lose_no_messages(monkeypatch):
    """
    Core Issue 3.3 case: fire off many concurrent append_messages() calls
    for the same user and confirm every single message survives to disk.
    Before the fix, no lock at all meant concurrent calls would race on the
    read-modify-write cycle and silently drop messages. Post-Issue-#16, this
    is also the first test in the file to genuinely contend _history_lock
    (asyncio.to_thread inside the lock is a real yield point) -- see the
    module docstring for why that needs a fresh per-test lock instance.
    """
    _fresh_history_lock(monkeypatch)
    user_id = 1
    num_appends = 50

    await asyncio.gather(*[
        conversation_manager.append_messages(user_id, [_msg(i)])
        for i in range(num_appends)
    ])

    history = await conversation_manager.load_history(user_id)
    assert len(history) == num_appends, (
        f"expected all {num_appends} concurrently-appended messages to survive, "
        f"got {len(history)} -- messages were lost to a write race"
    )

    # Every message must actually be present (not just the right count by
    # coincidence, e.g. from duplicated writes overwriting distinct ones).
    contents = {m["content"] for m in history}
    expected = {f"message {i}" for i in range(num_appends)}
    assert contents == expected


@pytest.mark.asyncio
async def test_concurrent_appends_for_different_users_dont_interfere(monkeypatch):
    """
    Sanity check that the single global lock (Approach A) still produces
    correct results across DIFFERENT users -- correctness must hold
    regardless of the throughput trade-off. Each user's file should contain
    exactly and only that user's messages. Genuinely contends
    _history_lock (via gather), so needs a fresh instance -- see module
    docstring.
    """
    _fresh_history_lock(monkeypatch)

    async def append_for_user(user_id: int, n: int):
        await asyncio.gather(*[
            conversation_manager.append_messages(user_id, [_msg(i)])
            for i in range(n)
        ])

    await asyncio.gather(
        append_for_user(10, 20),
        append_for_user(11, 15),
        append_for_user(12, 25),
    )

    history_10 = await conversation_manager.load_history(10)
    history_11 = await conversation_manager.load_history(11)
    history_12 = await conversation_manager.load_history(12)

    assert len(history_10) == 20
    assert len(history_11) == 15
    assert len(history_12) == 25


# --- reentrancy safety of the split sync/async design ---------------------

@pytest.mark.asyncio
async def test_get_recent_for_llm_does_not_deadlock_under_concurrent_writes():
    """
    get_recent_for_llm() internally acquires _history_lock (via the unlocked
    _load_history_sync helper) while append_messages() also acquires it.
    This must never deadlock -- if the sync/async split were done wrong
    (e.g. get_recent_for_llm calling the *locked* public load_history()
    from inside its own `async with _history_lock:` block), this test would
    hang instead of completing. asyncio.wait_for gives it a generous but
    finite timeout so a real deadlock fails loudly instead of hanging the
    whole test run.

    No fresh-lock workaround needed here: the calls below are sequential
    awaits inside one loop (no asyncio.gather of multiple tasks), so this
    test never genuinely contends _history_lock across overlapping tasks
    -- it's a deadlock check, not a contention/throughput check.
    """
    user_id = 2
    await conversation_manager.append_messages(user_id, [_msg(0)])

    async def run_many():
        for i in range(1, 30):
            await conversation_manager.append_messages(user_id, [_msg(i)])
            await conversation_manager.get_recent_for_llm(user_id)

    await asyncio.wait_for(run_many(), timeout=10)

    history = await conversation_manager.load_history(user_id)
    assert len(history) == 30


@pytest.mark.asyncio
async def test_load_history_and_append_messages_interleaved_no_deadlock(monkeypatch):
    """Same deadlock concern as above, but explicitly interleaving
    load_history() (not just get_recent_for_llm) with concurrent
    append_messages() calls via two gathered tasks, since load_history() is
    also a public locked wrapper now. This DOES genuinely contend
    _history_lock (two concurrent tasks via gather, each with a real await
    inside the lock post-Issue-#16), so it needs a fresh lock instance --
    see module docstring."""
    _fresh_history_lock(monkeypatch)
    user_id = 3

    async def writer():
        for i in range(20):
            await conversation_manager.append_messages(user_id, [_msg(i)])

    async def reader():
        for _ in range(20):
            await conversation_manager.load_history(user_id)

    await asyncio.wait_for(asyncio.gather(writer(), reader()), timeout=10)

    history = await conversation_manager.load_history(user_id)
    assert len(history) == 20


# --- regression: existing behavior unaffected ------------------------------

@pytest.mark.asyncio
async def test_single_append_and_load_roundtrip_unaffected():
    """Basic single-call behavior (no concurrency involved) must still work
    exactly as before -- this is what test_history_persistence.py already
    covers, repeated narrowly here as a direct regression check on the
    locked code path itself."""
    user_id = 4
    await conversation_manager.append_messages(user_id, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])

    history = await conversation_manager.load_history(user_id)
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


@pytest.mark.asyncio
async def test_get_recent_for_llm_safe_trim_still_works_under_lock():
    """
    The existing safe-trim logic (never orphan a tool_call/tool result pair)
    must be completely unaffected by the locking change -- this mirrors
    test_history_persistence.py's core assertion but exercises it through
    the now-async, now-locked get_recent_for_llm().
    """
    config.MAX_HISTORY_MESSAGES = 5
    user_id = 5

    messages = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Result 1"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]
    await conversation_manager.append_messages(user_id, messages)

    recent = await conversation_manager.get_recent_for_llm(user_id)

    first_role = recent[0].get("role")
    first_has_tools = bool(recent[0].get("tool_calls"))
    assert not (first_role == "tool" or (first_role == "assistant" and first_has_tools)), (
        "trimmed history must never start with an orphaned tool result or a "
        "tool-calling assistant turn"
    )

    config.MAX_HISTORY_MESSAGES = 20  # restore for any tests that run after


@pytest.mark.asyncio
async def test_load_history_returns_empty_list_for_new_user():
    """Regression: a user with no history file yet must still get []
    (not an error), same as before the fix."""
    history = await conversation_manager.load_history(999999)
    assert history == []


def main():
    """Lets this be run directly with `python test_conversation_lock.py`,
    matching the style of the other root-level test_*.py files."""
    import inspect

    class _Monkeypatch:
        """Minimal stand-in for pytest's `monkeypatch` fixture, matching
        the shim already used by test_leaked_json_recovery.py,
        test_narration_guard_loop.py, and test_checkout_atomicity.py for
        their own monkeypatch-taking tests when run outside pytest."""
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]
    passed = 0
    for name, fn in test_fns:
        for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
            f.unlink()

        needs_monkeypatch = "monkeypatch" in inspect.signature(fn).parameters
        mp = _Monkeypatch() if needs_monkeypatch else None
        try:
            if needs_monkeypatch:
                asyncio.run(fn(mp))
            else:
                asyncio.run(fn())
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
        finally:
            if mp:
                mp.undo()
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
