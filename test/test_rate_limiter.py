# test/test_rate_limiter.py
"""
Covers the new per-user rate limiter (utils/rate_limiter.py), added to
close the "Not built yet" gap flagged as top priority by Moeid on
2026-11-08: agent/agent.py previously let any single user trigger
unlimited LLM calls, and Groq's free tier caps at 100,000 tokens/day (see
CLAUDE.md's "LLM provider" section) — one chatty or malicious user could
exhaust the whole shop's daily quota and lock out every other customer.

The limiter is a simple in-memory sliding-window log per user_id, guarded
by a single asyncio.Lock (same single-lock-per-concern pattern as
_carts_lock/_orders_lock/_history_lock/_inventory_lock/_checkout_lock
elsewhere in this project). Deliberately NOT persisted to disk — see the
module's own docstring for why that's an intentional choice, not an
oversight.

This suite covers the limiter in isolation (utils/rate_limiter.py) only.
It does not yet cover the handlers/user_handlers.py integration point
(the throttled-reply UX) since that wiring is a separate, smaller change
layered on top of this module.

Sandboxes config.RATE_LIMIT_MESSAGES / config.RATE_LIMIT_WINDOW_SECONDS to
small, test-friendly values so tests run fast and deterministically,
matching the project's existing config-override sandboxing pattern (e.g.
test_history_persistence.py overriding MAX_HISTORY_MESSAGES).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import pytest

import config
config.RATE_LIMIT_MESSAGES = 3
config.RATE_LIMIT_WINDOW_SECONDS = 60

from utils import rate_limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter_state():
    """Each test gets a clean slate — the module-level _hits dict is
    otherwise shared across every test in the process, same class of
    concern as inventory_manager._cache needing a reset between tests."""
    rate_limiter._hits.clear()
    yield
    rate_limiter._hits.clear()


# --- core behavior ----------------------------------------------------

@pytest.mark.asyncio
async def test_allows_messages_up_to_the_limit():
    """The first RATE_LIMIT_MESSAGES calls for a user must all be allowed."""
    for _ in range(config.RATE_LIMIT_MESSAGES):
        assert await rate_limiter.check_rate_limit(1) is True


@pytest.mark.asyncio
async def test_blocks_once_the_limit_is_exceeded():
    """The (N+1)th call within the window must be rejected."""
    for _ in range(config.RATE_LIMIT_MESSAGES):
        assert await rate_limiter.check_rate_limit(2) is True

    assert await rate_limiter.check_rate_limit(2) is False


@pytest.mark.asyncio
async def test_rejected_calls_do_not_further_count_against_the_user():
    """
    A throttled (False) call must not itself occupy a slot in the window —
    otherwise repeatedly hammering a throttled endpoint would never let the
    window clear out even after the original messages age past
    RATE_LIMIT_WINDOW_SECONDS, since each rejected attempt would keep
    re-filling the deque with a fresh timestamp.
    """
    for _ in range(config.RATE_LIMIT_MESSAGES):
        await rate_limiter.check_rate_limit(3)

    # Hammer it several more times while throttled.
    for _ in range(5):
        assert await rate_limiter.check_rate_limit(3) is False

    # The deque must still only hold the original N allowed timestamps,
    # not N + 5.
    assert len(rate_limiter._hits[3]) == config.RATE_LIMIT_MESSAGES


@pytest.mark.asyncio
async def test_old_timestamps_age_out_and_free_up_capacity(monkeypatch):
    """
    Once enough simulated time has passed that earlier timestamps fall
    outside the window, capacity must free up again — the window is
    sliding, not a fixed per-process quota that never resets.
    """
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: fake_now["t"])

    for _ in range(config.RATE_LIMIT_MESSAGES):
        assert await rate_limiter.check_rate_limit(4) is True
    assert await rate_limiter.check_rate_limit(4) is False

    # Advance time past the window.
    fake_now["t"] += config.RATE_LIMIT_WINDOW_SECONDS + 1

    assert await rate_limiter.check_rate_limit(4) is True


@pytest.mark.asyncio
async def test_partial_aging_frees_exactly_the_expired_slots(monkeypatch):
    """
    Finer-grained version of the aging test: advancing time past only the
    FIRST allowed message's timestamp (not all of them) must free exactly
    one slot, not the whole window — confirms the sliding-window behavior
    is per-timestamp, not an all-or-nothing reset.
    """
    fake_now = {"t": 0.0}
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: fake_now["t"])

    await rate_limiter.check_rate_limit(5)  # t=0
    fake_now["t"] = 10.0
    await rate_limiter.check_rate_limit(5)  # t=10
    fake_now["t"] = 20.0
    await rate_limiter.check_rate_limit(5)  # t=20 -> limit (3) now reached
    assert await rate_limiter.check_rate_limit(5) is False

    # Advance just past the first timestamp's window expiry (t=0 expires
    # once now > 0 + WINDOW), but the other two (t=10, t=20) are still
    # within their own window.
    fake_now["t"] = config.RATE_LIMIT_WINDOW_SECONDS + 1

    # Exactly one slot freed -> exactly one more allowed call.
    assert await rate_limiter.check_rate_limit(5) is True
    assert await rate_limiter.check_rate_limit(5) is False


# --- per-user isolation -------------------------------------------------

@pytest.mark.asyncio
async def test_different_users_have_independent_limits():
    """One user hitting their limit must not affect any other user —
    this is a fairness mechanism, so per-user isolation is the whole
    point (unlike the project's other, deliberately-global locks, which
    exist for correctness rather than fairness)."""
    for _ in range(config.RATE_LIMIT_MESSAGES):
        assert await rate_limiter.check_rate_limit(10) is True
    assert await rate_limiter.check_rate_limit(10) is False

    # A completely different user must still have their full quota.
    for _ in range(config.RATE_LIMIT_MESSAGES):
        assert await rate_limiter.check_rate_limit(11) is True


# --- concurrency ---------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checks_for_the_same_user_never_overshoot_the_limit():
    """
    Core correctness guarantee under concurrency: firing many simultaneous
    check_rate_limit() calls for one user must never allow more than
    RATE_LIMIT_MESSAGES of them through, even though they're all racing
    for the same deque — this is exactly the kind of read-modify-write
    race _lock exists to prevent, mirroring the concurrency guarantees
    already tested for cart_manager/order_manager/conversation_manager
    elsewhere in this project.
    """
    results = await asyncio.gather(*[
        rate_limiter.check_rate_limit(20) for _ in range(50)
    ])

    allowed = [r for r in results if r is True]
    assert len(allowed) == config.RATE_LIMIT_MESSAGES, (
        f"expected exactly {config.RATE_LIMIT_MESSAGES} of 50 concurrent "
        f"calls to be allowed, got {len(allowed)} — the limit was overshot"
    )


def main():
    """Lets this be run directly with `python test/test_rate_limiter.py`,
    matching the style of the other test_*.py files in this project."""
    import inspect

    class _Monkeypatch:
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
        rate_limiter._hits.clear()
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
            rate_limiter._hits.clear()
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
