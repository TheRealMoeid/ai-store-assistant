"""
Per-user rate limiting for inbound text messages, to bound how many LLM
calls (and therefore Groq token spend / Ollama CPU time) any single user
can trigger in a short window.

Unlike cart_manager/order_manager/conversation_manager/inventory_manager,
this state is deliberately NOT persisted to disk:
  - Rate-limit counters are inherently short-lived (a sliding window of
    RATE_LIMIT_WINDOW_SECONDS); losing them on a bot restart just means
    everyone's quota resets, which is harmless (arguably desirable) rather
    than a data-loss bug.
  - Avoids adding a fifth JSON file and a fifth async/atomic-write surface
    for state that doesn't need either.

This keeps the per-user counters in memory, guarded by a single
asyncio.Lock — same single-lock-per-concern pattern already used
throughout this project (_carts_lock, _orders_lock, _history_lock,
_inventory_lock, _checkout_lock) rather than a per-user dict of locks,
for the same "minimal, structurally-scoped" reasons documented for those.

Algorithm: a simple sliding-window log per user_id — a deque of the
timestamps (monotonic clock) of that user's recent allowed messages.
On each check, timestamps older than RATE_LIMIT_WINDOW_SECONDS are
dropped from the front of the deque; if what remains is already at
RATE_LIMIT_MESSAGES, the new message is rejected. Otherwise the new
timestamp is recorded and the message is allowed.

Memory is bounded naturally: a deque only ever holds at most
RATE_LIMIT_MESSAGES timestamps per user, and users who stop messaging
simply stop growing their entry (the per-user dict itself is never
pruned, which is an accepted trade-off at this project's scale — a
single-seller shop's lifetime customer count is small; see
"Not built yet" in CLAUDE.md for where this sits in the backlog).
"""
import asyncio
import time
from collections import deque

import config

_lock = asyncio.Lock()
_hits: dict[int, deque] = {}


async def check_rate_limit(user_id: int) -> bool:
    """
    Records this message attempt and returns True if the user is within
    their rate limit (RATE_LIMIT_MESSAGES per RATE_LIMIT_WINDOW_SECONDS),
    False if they should be throttled.

    Call this once per inbound message, before any LLM call is made —
    the whole point is to avoid spending the call, not to spend it and
    apologize afterward. A rejected call (False) does NOT count against
    the user further; only allowed messages are recorded, so a throttled
    user's window clears out naturally as old timestamps age past
    RATE_LIMIT_WINDOW_SECONDS.
    """
    now = time.monotonic()
    async with _lock:
        hits = _hits.setdefault(user_id, deque())

        # Drop timestamps that have aged out of the window.
        while hits and now - hits[0] > config.RATE_LIMIT_WINDOW_SECONDS:
            hits.popleft()

        if len(hits) >= config.RATE_LIMIT_MESSAGES:
            return False

        hits.append(now)
        return True
