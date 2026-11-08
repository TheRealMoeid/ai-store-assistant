# Shopmate — AI Sales Agent for Telegram

## User Interaction
- The user's name is Moeid.
- Begin every response by addressing the user as "Moeid".

## What this is
An aiogram 3 Telegram bot backed by an LLM tool-calling agent. Customers chat
naturally; the agent searches inventory, checks stock, manages a cart, places
orders, collects payment proof, and logs feedback. The seller manages
inventory by hand-editing a JSON file and confirms/rejects orders via
Telegram commands in an admin group. No database — everything is JSON files
under `data/`, by design (small-scale project, seller-editable inventory).

## Architecture
```
bot.py                    entrypoint, wires up routers (admin router before user router)
config.py                 all settings, loaded from .env
agent/
  llm_client.py           returns (client, model) for whichever provider is configured
  tools.py                tool schemas (OpenAI function-calling format) + implementations
  agent.py                the tool-calling loop — most of the interesting logic lives here
  prompts.py               SYSTEM_PROMPT — see "Prompt rules" below, this file is load-bearing
handlers/
  user_handlers.py        text messages -> agent; photo messages -> payment screenshot handler
  admin_handlers.py       /pending_orders, /confirm <id>, /reject <id> — gated by AdminOnlyMiddleware;
                           /reject also restores stock (see "Stock reversal on rejection" below);
                           /pending_orders renders full order detail, chunked across messages
                           (see "Pending orders detail" below)
utils/
  inventory_manager.py    reads data/inventory.json, reloads on mtime change; owns both stock-writing
                           paths — decrement_stock_for_cart (at place_order) and restore_stock_for_order
                           (at /reject). Imports order_manager (one-directional — see constraint below).
  cart_manager.py          per-user cart, persisted to data/carts.json so carts survive restarts
                           (checkout atomicity itself lives in agent/tools.py's _checkout_lock —
                           see "Checkout atomicity" below, not in this file)
  conversation_manager.py per-user chat history, persisted in full but trimmed for LLM context;
                           guarded by a single asyncio.Lock (see "Conversation history locking" below)
  order_manager.py        orders.json + feedback.json read/write, order status lifecycle, including
                           the atomic set_order_status_if_not() used for idempotent rejection
  file_io.py               shared async/atomic JSON read-write helpers used by all four managers above
                           (see "Async file I/O" below)
data/
  inventory.json           seller edits this directly
  orders.json               order status: pending_confirmation -> awaiting_review -> confirmed|rejected
  carts.json                 persisted carts, keyed by user_id (string)
  feedback.json
  conversations/{user_id}.json
```

**Import constraint:** `inventory_manager.py` imports `order_manager` (to look
up an order's items when restoring stock). `order_manager.py` must never
import `inventory_manager` back — that would create a circular import. If a
future change needs order_manager to touch inventory, invert the dependency
or pass data in as plain arguments instead.

## Running it
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, ADMIN_GROUP_ID, ADMIN_USER_IDS, LLM_PROVIDER
python bot.py
```
Windows note: use a Python 3.11/3.12 venv, not 3.14 — `pydantic-core` (an
aiogram/openai dependency) doesn't have prebuilt wheels for 3.14 yet and
fails to compile from source without a Rust toolchain installed.

## LLM provider
Swappable via `LLM_PROVIDER=ollama|groq` in `.env` — `agent/llm_client.py`
is the only place that knows the difference; everything else just calls
`get_client()`. Both are OpenAI-compatible APIs.

**Model reliability, ranked from testing so far (worst to best):**
1. `llama3.2` (3B, local) — frequently hallucinates product IDs, invents cart/order
   state that was never actually written, narrates actions without calling the
   tool ("I'll add that to your cart" with no tool call), leaks fake tool-call
   JSON as plain reply text in multiple different shapes.
2. `qwen3:8b` (local, ~5GB) — meaningfully more reliable tool selection and
   argument correctness. Still occasionally guesses a wrong product ID before
   self-correcting via search_products (handled gracefully, see
   `_resolve_product_id` in tools.py). Current default for local dev.
3. Groq-hosted `llama-3.3-70b-versatile` — best tool-calling behavior observed,
   but Groq's **free tier caps at 100,000 tokens/day** — heavy testing burns
   through this fast, especially with malformed-call retries roughly doubling
   request cost. When it hits the cap, `RateLimitError` is caught and returns
   a friendly message rather than crashing (see agent.py).

**Do not assume a new API key resets the Groq quota** — the limit is per
account/org, not per key.

## Cart persistence (utils/cart_manager.py)
Carts used to live only in memory and were lost on every restart. They're
now persisted to `data/carts.json`, keyed by `str(user_id)`, guarded by an
`asyncio.Lock` matching the read-modify-write pattern already used in
`order_manager.py`. An empty cart is removed from the file entirely rather
than stored as `[]`, to keep the file small. Covered by
`test_cart_persistence.py`. The actual file I/O inside `_carts_lock` now
goes through `utils/file_io.py` (offloaded to a worker thread, atomic write)
— see "Async file I/O" below; the locking and empty-cart-removal behavior
described above are unchanged.

## Conversation history locking (utils/conversation_manager.py) — fixed, was Issue 3.3
**Previously:** `append_messages()` did a raw read-modify-write
(`load_history` -> `.extend()` -> `json.dump`) with **no lock at all** —
unlike `cart_manager.py`/`order_manager.py`, which both guard the exact same
read-modify-write shape with `asyncio.Lock`. Two near-simultaneous calls for
the same user (e.g. two rapid messages arriving close together) could both
read the same on-disk snapshot, both `.extend()` independently, and
whichever write landed second would silently overwrite — losing — the
other's messages. In the worst case, one write landing mid-read could even
produce a `JSONDecodeError` from a torn/partially-written file, confirmed
via a direct thread-pool-based repro against the pre-fix code during this
fix's development.

**Now:**
- A single module-level `_history_lock = asyncio.Lock()` — **Approach A**
  from the fix discussion, matching `cart_manager.py`/`order_manager.py`'s
  existing single-lock-per-file pattern exactly. A per-user dict of locks
  (Approach B) was explicitly considered and rejected: this project's own
  "minimal, structurally-scoped fixes" principle favors matching an already
  proven pattern over introducing new architectural surface (a dict of
  locks with unbounded per-user growth) for a small-scale, single-seller
  bot where the extra cross-user contention from a single lock is
  negligible in practice.
- **Reentrancy trap avoided by construction:** `asyncio.Lock` is not
  reentrant, so a naive "just wrap the existing functions" fix would
  deadlock the moment one locked function called another (e.g.
  `append_messages` calling `load_history`, both trying to acquire the same
  lock). The fix splits each function into an **unlocked sync core**
  (`_load_history_sync`, `_append_messages_sync` — pure file I/O, never
  touch the lock) and a **locked async wrapper** (`load_history`,
  `append_messages`, `get_recent_for_llm` — each acquires `_history_lock`
  exactly once, then calls the sync core internally). `get_recent_for_llm`
  releases the lock before its (pure in-memory, no I/O) trim logic runs, so
  the lock is only ever held across the actual file read/write.
- `load_history`, `append_messages`, and `get_recent_for_llm` are all now
  `async` — a required, correctness-necessary side effect of adding the
  lock, not scope creep. All 5 call sites in `agent/agent.py` (all already
  inside the async `run_agent`) now `await` them, as do the call sites in
  `test_history_persistence.py`, `test_leaked_json_recovery.py`, and
  `test_narration_guard_loop.py`.

Covered by `test_conversation_lock.py` (7 tests): the core race (50
concurrent `append_messages()` calls for one user — zero messages lost);
per-user correctness under concurrent multi-user load (proves the single
global lock still produces correct per-user results despite serializing
globally); two deadlock-safety tests exercising `get_recent_for_llm`/
`load_history` interleaved with concurrent writes under an
`asyncio.wait_for` timeout (would fail loudly instead of hanging if the
sync/async split were done wrong); and 3 regression checks (single-call
roundtrip, the existing safe-trim logic that avoids orphaning a
`tool_call`/`tool` result pair, empty history for a brand-new user).
Sanity-checked two ways: the new tests can't even run against the pre-fix
synchronous API (`TypeError` on all 7, since they `await` functions that
weren't coroutines yet); and a direct thread-pool-based concurrent-append
reproduction against the literal pre-fix file surfaced a real
`JSONDecodeError`, confirming the race is real, not just theoretical. Full
suite verified at 56/56 (`pytest . -v`) on a branch containing `main` +
only this fix, no regressions. Branch `issue-3.3-conversation-lock`, merged
to `main` (Aug 2026). Verified independently by Moeid on his own Windows
venv (both `pytest . -v` and the standalone
`python test_conversation_lock.py` runner passed) and manually against the
live bot.

**Update (Issue #16, Nov 2026):** `_load_history_sync`/
`_append_messages_sync` now delegate their actual file I/O to
`utils/file_io.py` (offloaded to a worker thread, atomic write) instead of
calling `open()`/`json` directly — see "Async file I/O" below for why, and
for the pytest-asyncio cross-test lock-binding fix this required in
`test_conversation_lock.py`'s two genuinely-concurrent tests. The locking
design and sync/async split described above are otherwise unchanged.

## Stock integrity at checkout (utils/inventory_manager.py)
`decrement_stock_for_cart()` is the write path at order placement time. It's
called from `_place_order` in `agent/tools.py` and is atomic per cart:
1. **Validate first, write second** — every line item in the cart is checked
   against current stock *before* anything is decremented. If any single item
   can't be fulfilled, the whole call returns an error and nothing on disk
   changes — no partial orders, no lost stock on item 1 because item 2 ran out.
2. Guarded by an `asyncio.Lock` (`_inventory_lock`) so concurrent checkouts
   can't race each other's read-modify-write cycle.
3. `place_order` in `tools.py` also re-validates the cart against live stock
   immediately before calling this, since stock may have changed since the
   items were added to the cart — a customer holding a stale cart gets a
   clear "stock changed" message instead of a silent overcommit.

Covered by `test_concurrency.py` (50 simulated concurrent order placements,
asserts no data loss) and `test/test_backend.py::test_atomic_stock_validation`
(asserts a failed multi-item order doesn't partially decrement stock).

✅ **Multi-item cart N-tuple stock decrement — fixed, was Issue #6 / bug-audit
1.1.** `_place_order` in `agent/tools.py` had a loop bug — `for item in
cart: result = await inventory_manager.decrement_stock_for_cart(cart)`
passed the **whole cart** to `decrement_stock_for_cart` on every loop
iteration instead of calling it once. For an N-item cart, the entire cart's
stock got decremented N times instead of once. Single-item carts were
unaffected (the loop only ran once), which is why this wasn't caught
earlier — it only showed up with 2+ line items in one order. Found while
testing the Issue #4 fix (Aug 2026); tracked separately as GitHub Issue #6
and, under the bug-audit numbering (see "Bug-audit fixes status" below), as
Issue 1.1.

**Fix:** removed the wrapping `for item in cart:` loop and call
`decrement_stock_for_cart(cart)` exactly once, matching the function's
existing atomic, whole-cart contract — it already iterates every line item
internally and validates-then-writes atomically (see above), so the bug was
purely in how `_place_order` called an already-correct function. No changes
to `inventory_manager.py`, `order_manager.py`, or `cart_manager.py`.

Covered by `test_multi_item_checkout.py` (6 tests): a two-product cart
decrements each product by exactly its ordered quantity (not double); a
three-line-item cart across two products/variants, to rule out a fix that
only handles the 2-item case; a single-item-cart regression check (already
correct pre-fix); multi-item with quantity > 1 per line; an
insufficient-stock atomicity check (nothing decrements anywhere if one line
item in a multi-item cart can't be fulfilled — the atomicity guarantee this
fix must not weaken); and an order-persistence sanity check. Sanity-checked
against the pre-fix code — 3/6 correctly failed there (the three
genuinely-multi-item cases); the other 3 (single-item, insufficient-stock
atomicity, order persistence) were already correct pre-fix and serve as
regression checks. Full suite verified at 55/55 (`pytest . -v`), no
regressions. Branch `issue-6-Multi-item-Cart`, merged to `main` (Aug 2026).
Verified independently by Moeid on his own Windows venv (`pytest . -v` →
all passed) and manually against the live bot.

**Update (Issue #16, Nov 2026):** the actual disk read/write inside
`decrement_stock_for_cart`/`restore_stock_for_order` now goes through
`utils/file_io.py` (offloaded to a worker thread, atomic write) instead of
calling `open()`/`json` directly inside `_inventory_lock`. The sync,
unlocked read helpers (`get_product_by_id`/`search_products`/
`check_variant`, built on `_load_if_changed`) deliberately stay
synchronous — see "Async file I/O" below for the full reasoning, including
why those specific unlocked reads were the main motivation for adding
atomic writes in the first place.

## Checkout atomicity (agent/tools.py) — fixed, was Issue #10 / bug-audit 3.1
**Previously:** `_place_order` read the cart, validated stock, decremented
stock, created the order, and cleared the cart across two separate
`cart_manager` lock acquisitions (`get_cart` at the top, `set_cart([])` at
the bottom), with no single critical section spanning the whole sequence.
Two near-simultaneous `place_order` calls for the *same* user (a double-tap
on "checkout" in Telegram, a client retry, etc.) could both read the same
not-yet-cleared cart, both pass validation, and each independently
decrement stock and create its own order — a duplicate order (and doubled
stock loss) for what the customer intended as a single checkout. Every
individual primitive involved (`cart_manager`, `inventory_manager`,
`order_manager`) was already internally atomic and correct; the bug was
purely that nothing serialized the *orchestration* of all of them together
across one checkout attempt.

**Now:**
- A single module-level `_checkout_lock = asyncio.Lock()` in `agent/tools.py`,
  held for the *entire* `_place_order` body — read cart, validate stock,
  decrement stock, create order, and clear cart all happen inside one
  `async with _checkout_lock:` block. A second concurrent call for the same
  user simply waits for the lock, then sees the cart the first call already
  cleared and hits the existing "cart is empty" error path — no duplicate
  order, no duplicate decrement.
- **Deliberately a single global lock, not per-user** — same precedent as
  `conversation_manager._history_lock` (Issue 3.3): this project's own
  "minimal, structurally-scoped fixes" principle favors a proven
  single-lock pattern over new architectural surface like a per-user dict
  of locks, especially since checkouts are rare, high-value events for a
  small-scale, single-seller bot. The cross-user contention from
  serializing all checkouts globally is negligible in practice, and it
  avoids introducing any lock-ordering coupling between `cart_manager`,
  `inventory_manager`, and `order_manager` — the lock lives in the
  orchestration layer (`agent/tools.py`), not inside any of those modules,
  so their own internal locks are still acquired and released independently
  and sequentially, exactly as before.
- **The cart is only ever cleared as the very last step**, after order
  creation has already succeeded. This was a deliberate design choice over
  an alternative "atomically consume the cart up front (read + clear in one
  step), then restore it if a later validation/decrement step fails"
  approach — that alternative was considered and rejected specifically
  because it opens a new data-loss race: if the cart is cleared before
  validation, and a concurrent `add_to_cart` writes a new item into the
  now-empty cart before the failed checkout's restore step runs, that
  restore would blindly overwrite the disk and silently delete the item
  the customer just added — a worse bug than the one being fixed. Holding
  `_checkout_lock` for the whole sequence and never mutating the cart until
  success means there is no failure path that ever needs to restore
  anything, and therefore no such window exists.

Covered by `test/test_checkout_atomicity.py` (5 tests): 50 concurrent
`place_order` calls for the same user create exactly one order, both in a
stock-constrained case (stock=1, so a naive fix might look correct only
because insufficient stock — not the lock — happens to block extra
decrements) and a stock-abundant case (stock=10, ruling that out); 50
concurrent checkouts across 5 *different* users each succeed independently
under the one global lock (proving the single-lock design doesn't drop or
merge unrelated users' legitimate orders); a failed checkout (forced stock
shortfall) leaves the cart completely untouched, confirming the "no restore
needed" design actually holds; and a plain single-call regression check.

**Testing note — forcing the race to actually reproduce:** plain
`asyncio.gather()` alone was *not* sufficient to reliably trigger this race
in a test, even against the pre-fix code. CPython's `asyncio.Lock.acquire()`
returns immediately without yielding to the event loop at all when
uncontended, so 50 "concurrent" `place_order` calls could each run
start-to-finish in one uninterrupted burst with no real interleaving ever
happening — the same gap this project already hit with `conversation_manager`
(Issue 3.3 needed a separate thread-pool repro for the same reason).
`test/test_checkout_atomicity.py` works around this by monkeypatching
`cart_manager.get_cart` to insert a genuine `await asyncio.sleep(0)` yield
point for its concurrency tests, which reliably forces the event loop to
interleave the gathered calls and reproduces the duplicate-order race
against the pre-fix code (confirmed: multiple duplicate orders from one
intended checkout, pre-fix). A related, purely test-isolation artifact this
surfaced: because `_checkout_lock` is a shared module-level singleton and
`pytest-asyncio` gives each test function its own fresh event loop by
default, a lock that becomes genuinely contended in one test binds itself
to that test's loop (`asyncio.Lock` only touches the running loop on its
*contended* path, not its fast uncontended path) and then raises "is bound
to a different event loop" if a *later* test also drives contention on the
same object. Each concurrency test in the suite works around this by
monkeypatching `agent.tools._checkout_lock` to a fresh `asyncio.Lock()`
instance for the duration of that test — this is purely a pytest
multi-event-loop artifact, not a real issue: the actual bot process has
exactly one long-lived event loop for its whole lifetime, so it never
occurs in production.

Sanity-checked against the pre-fix code: the 3 concurrency-specific tests
correctly fail there (2 on the duplicate-order assertion, 1 because
`_checkout_lock` doesn't exist pre-fix); the 2 non-concurrency tests
(failed-checkout-leaves-cart-untouched, single-call regression) pass
regardless of the fix, serving as regression checks rather than bug
detectors. Full suite verified at 67/67 (`pytest . -v`), no regressions.
Branch `11-issue-10-no-atomic-cart-consume-across-_place_order`, merged to
`main` (Aug 2026). Verified independently by Moeid on his own Windows venv
(`python -m pytest . -v` → 67/67, and the standalone
`python test\test_checkout_atomicity.py` runner → 5/5).

**Approaches considered and why this one was chosen over the alternatives:**
1. **Atomic `consume_cart(user_id)` in `cart_manager` + restore-on-failure**
   (read+clear the cart in one lock acquisition, `set_cart` the items back
   if a later step fails) — rejected for the data-loss race against a
   concurrent `add_to_cart` described above. This looked like the more
   "encapsulated" fix at first (matches the `set_order_status_if_not`
   atomic-check-and-act shape already used elsewhere in this project), but
   the restore step it requires is a strictly worse trade than the problem
   it solves.
2. **Hold `cart_manager._carts_lock` across the whole `_place_order`
   sequence** (expose the existing cart lock instead of adding a new one)
   — rejected because it would hold that lock across disk I/O in two other
   modules (`inventory_manager`'s decrement, `order_manager`'s order write)
   for the full checkout duration, blocking *every* other user's
   `view_cart`/`add_to_cart` calls, not just the checking-out user's — a
   much bigger contention surface for no benefit over a dedicated
   orchestration-layer lock. It would also mix a third lock into the
   existing informal "inventory-then-orders" ordering convention in a way
   that needs care to avoid future deadlock risk.
3. **A new per-user "checkout in progress" guard** (e.g. a per-`user_id`
   `asyncio.Lock` dict scoped just to placing orders) — rejected as
   unnecessary new architectural surface, the same trade-off this project
   already explicitly considered and passed over for `conversation_manager`
   in Issue 3.3 in favor of a single global lock.

The chosen design (**global `_checkout_lock` in `agent/tools.py`, held for
the whole `_place_order` body, cart cleared only on success**) is what's
implemented above.

## Async file I/O (utils/file_io.py) — fixed, was Issue 3.4 / GitHub #16
**Previously:** all four managers (`cart_manager.py`, `order_manager.py`,
`conversation_manager.py`, `inventory_manager.py`) called plain, blocking
`open()`/`json.load`/`json.dump` directly inside `async def` functions,
e.g.:
```python
def _save(carts: dict[str, list[dict]]):
    with open(config.CARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(carts, f, ensure_ascii=False, indent=2)
```
Python's `asyncio` event loop is single-threaded: when a coroutine calls a
blocking function like this, control doesn't return to the loop until the
call completes. Because `aiogram`'s polling model runs all incoming
Telegram updates (and the bot's own outgoing calls — typing indicators,
message sends) on that same loop, a slow disk write during, say,
`order_manager.place_order()` would stall every other concurrent user's
updates for the duration of that write, not just the user placing the
order. At this project's file sizes, individual writes were fast enough
(milliseconds) that this was unlikely to be user-noticeable, so it stayed a
documented, low-priority backlog item (bug-audit Issue 3.4) rather than an
urgent fix — see `docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the original
write-up and cost/benefit discussion.

**Now:**
- A new shared module, `utils/file_io.py`, provides `read_json`/
  `write_json_atomic` (async, offload the actual blocking work to a worker
  thread via `asyncio.to_thread()`) and their `_sync` counterparts (for
  callers — like `conversation_manager.py`'s existing sync-core helpers —
  that already run inside `asyncio.to_thread()` themselves). All four
  managers now call these instead of `open()`/`json` directly, each still
  from *inside* its own existing lock (`_carts_lock`, `_orders_lock`,
  `_history_lock`, `_inventory_lock`) — the locks and the sync-core/
  locked-async-wrapper split each manager already had are completely
  unchanged; only the I/O *inside* those critical sections moved off the
  event-loop thread.
- **Writes are atomic**, not a plain truncate-then-write: `write_json_atomic`
  serializes to a temp file in the same directory, then `os.replace()`s it
  into place. This is not optional polish — it's required *because* of the
  move to threads. Before this fix, every read and every write ran on the
  single event-loop thread, so a read could never physically overlap a
  write; a plain truncate-then-write was safe by construction. Once writes
  run on a worker thread while the loop thread keeps servicing other
  coroutines (including other reads), that guarantee disappears — a naive
  write would let a concurrent reader observe a half-written (torn) file
  mid-write. Atomic replace means a reader always sees either the fully-old
  or fully-new file, never a partial one.
- **`order_manager.get_order()` and `get_latest_unpaid_order()` are now
  async and locked**, mirroring `get_all_orders()` (which already was
  both). They were plain synchronous, unlocked reads before this fix — safe
  only because, again, nothing could run concurrently with them on the
  single event-loop thread. All three call sites that used the old sync
  signature were updated to `await`: `agent/tools.py::_submit_payment_reference`,
  `handlers/user_handlers.py::handle_payment_screenshot`, and two direct
  calls inside `test/test_stock_reversal.py`.
- `cart_manager.py`'s and `conversation_manager.py`'s reads already always
  went through the same lock as their writes (`get_cart`/`set_cart` both
  hold `_carts_lock`; `load_history`/`append_messages`/`get_recent_for_llm`
  all hold `_history_lock`), so for those two files there was never a
  genuinely unlocked reader — the atomic-write layer closes a hazard that,
  for those two, is mostly theoretical. The real motivation for atomic
  writes is **`inventory_manager.py`**: `get_product_by_id`/
  `search_products`/`check_variant` (built on the mtime-cached
  `_load_if_changed`) are *deliberately* synchronous and unlocked — they're
  called from many places throughout `agent/tools.py` for cheap reads, and
  making them async/locked would ripple through that whole file for a read
  that's normally just an `os.stat()`. Those unlocked reads can now
  genuinely race `decrement_stock_for_cart`'s/`restore_stock_for_order`'s
  locked, threaded writes — atomic replace is what keeps that race safe
  (old-or-new, never torn) rather than requiring those reads to be locked
  too.

**Read-side retry — found via live testing, not anticipated up front.**
A first live `pytest . -v` run on Moeid's Windows venv surfaced a real gap
the design above didn't account for: even with writes atomic, a concurrent
`open(path, "r")` can transiently raise `PermissionError` on Windows
(`OSError` errno 13, "the process cannot access the file because it is
being used by another process") while another thread's `os.replace()` is
in flight — a sharing-violation window, not data corruption (the write was
never actually torn). This showed up as 3 failures out of 80 concurrent
operations in `test/test_async_file_io.py`'s stress test. The write side
already retried around `os.replace()` for the same class of Windows
contention; the read side didn't. Fixed by adding the same short,
bounded, exponential-backoff retry to `read_json_sync` — confirmed resolved
on a second live run (93/93, full suite).

**Follow-up fix (Nov 2026): the retry logic was built but not fully wired
up.** A post-merge review (after the 93/93 run above, on `main`) traced
every call site that touches inventory's `_cache` and found that
`_load_if_changed()` — `inventory_manager.py`'s own mtime-cached read
helper, underneath `get_product_by_id`/`search_products`/`check_variant`
— still had its own bare `open()`/`json.load()` call instead of going
through `read_json_sync()`. This is the *exact* reader the read-side-retry
fix above was written for (unlocked, called constantly from
`agent/tools.py`, genuinely races `decrement_stock_for_cart`'s/
`restore_stock_for_order`'s threaded writes) — the retry logic existed in
`file_io.py`, but this one caller never actually used it, so it remained
exposed to the same transient Windows `PermissionError` the stress test
had already caught and "fixed" everywhere else. An unhandled hit here
doesn't crash the bot — `call_tool()`'s broad `except Exception` catches
it — but it surfaces as a spurious `"tool failed: [Errno 13] ..."` to the
customer for what should have been an ordinary read, exactly the kind of
concurrent-load flakiness Issue #16 was meant to close. Confirmed real (not
theoretical) by temporarily reverting `_load_if_changed()` to its
pre-follow-up form and rerunning the new test below against it: 4 of 7
cases failed there with the literal `PermissionError`.

Fixed by routing `_load_if_changed()`'s inner read through
`file_io.read_json_sync()`, same as every other reader. The outer mtime
check (`config.INVENTORY_FILE.stat().st_mtime`) is unchanged. As a side
note from tracing this: `_cache`'s own dict mutations were never actually
racy in the first place — every mutation (here and in the two writers'
post-write invalidation) runs on the event-loop thread with no `await`
between a write completing and `_cache["mtime"] = None` running, so they
can never interleave with another coroutine. The gap was specifically the
unprotected file read, not the cache dict.

Covered by `test/test_inventory_read_retry.py` (7 tests): the core case
(`_load_if_changed()` survives a simulated transient `PermissionError`,
same shape as the Windows race in `test_async_file_io.py`'s stress test);
the same race exercised through `get_product_by_id`/`search_products`/
`check_variant` — the actual call sites `agent/tools.py` uses, since
that's what a customer-facing failure looks like in practice; a
regression check that the error still propagates once `file_io`'s retry
budget is genuinely exhausted (this fix must not turn a permanently-locked
file into a silently-swallowed error); and two regression checks for
plain uncontended reads and the existing mtime-based cache invalidation
(editing `inventory.json` by hand is still picked up on the next call).
Sanity-checked against the pre-follow-up code — 4/7 correctly failed
there. No branch yet at time of writing; apply alongside
`utils/inventory_manager.py`'s updated `_load_if_changed()`.

**Two more live-only findings, both fixed, neither in `file_io.py` itself:**
- `test/test_stock_reversal.py` had two direct calls to
  `order_manager.get_order(...)` that were never updated to `await` in the
  initial patch — a plain oversight (every other call in that file already
  went through async APIs, so this one slipped past review). Confirmed via
  `pytest`'s own `TypeError: 'coroutine' object is not subscriptable` plus
  a `RuntimeWarning: coroutine 'get_order' was never awaited`. Fixed by
  adding `await` at both sites.
- `test/test_conversation_lock.py`'s two genuinely-concurrent tests
  (`test_concurrent_appends_for_different_users_dont_interfere`,
  `test_load_history_and_append_messages_interleaved_no_deadlock`) started
  failing with `RuntimeError: ... is bound to a different event loop`. This
  is **not a regression** — it's the exact same pytest-asyncio cross-test
  lock-binding artifact already documented above under "Checkout atomicity"
  for `_checkout_lock`, now showing up for `_history_lock` for the first
  time. Before this fix, `_load_history_sync`/`_append_messages_sync` were
  pure synchronous code with no real `await` inside the locked section, so
  even a genuinely-concurrent `asyncio.gather()` of many `append_messages()`
  calls never actually interleaved on the lock — `asyncio.Lock`'s
  uncontended fast path never yields to the loop or binds itself to it
  (this project hit the exact same gap originally during Issue 3.3's own
  development, which is why that fix needed a separate thread-pool repro
  rather than a plain `asyncio.gather()` to prove the race was real — see
  above). Post-Issue-#16, `asyncio.to_thread()` inside the lock is a real
  yield point, so `_history_lock` is genuinely contended across concurrent
  tasks for the first time — which is a *correctness improvement* to the
  tests (they now test real interleaving instead of a sequential burst that
  merely looked concurrent), but it also means `_history_lock`, a
  module-level singleton shared across every test function in the file,
  can now bind itself to one test's event loop and then clash with a
  later test's different loop. Fixed the same way as `_checkout_lock`: the
  two affected tests now take `monkeypatch` and reset
  `conversation_manager._history_lock` to a fresh `asyncio.Lock()` instance
  at the start of the test.

Covered by `test/test_async_file_io.py` (8 tests): the core Issue #16 case
— a coroutine ticking on the event loop keeps running (confirmed via tick
count) while a patched, deliberately slow `write_json_atomic_sync` sleeps
in a worker thread, proving the write no longer blocks the loop; a
concurrent-readers-and-writers stress test (40 writers + 40 readers, one
shared file) asserting zero `JSONDecodeError`/malformed-read errors — this
is what caught the Windows `PermissionError` read-side gap above; atomic-
write unit tests (no leftover temp files, a later write fully replaces
earlier content rather than merging, retries past a transient
`PermissionError` on `os.replace()`, and still raises — without leaving a
temp file behind — if every retry is exhausted); and a regression check
that a missing file still returns the caller's default, both sync and
async. Also required `await`-only updates (no logic changes) in
`agent/tools.py`, `handlers/user_handlers.py`, and the two existing test
files noted above wherever they called the newly-async
`order_manager.get_order`/`get_latest_unpaid_order`. Full suite verified at
93/93 (`python -m pytest . -v`) on Moeid's Windows venv across two live
runs (the second confirming the read-retry and the two test-file fixes),
no regressions. Branch `fix/issue-16-async-file-io`.

## Stock reversal on rejection (utils/inventory_manager.py, utils/order_manager.py) — fixed, was Issue #4
**Previously:** `/reject` only flipped the order's status to `"rejected"`.
The stock reserved by `decrement_stock_for_cart` at `place_order` time was
never given back, so rejected orders caused permanent inventory drift —
`inventory.json` would eventually under-report real stock.

**Now:**
- `inventory_manager.restore_stock_for_order(order_id)` looks up the order's
  items, locks with the existing `_inventory_lock`, and re-runs the same
  size/color matching `decrement_stock_for_cart` used, crediting stock back
  per line item. Returns `{"status": "ok", "restored": [...], "skipped":
  [...]}` (or `{"error": ...}` if the order itself isn't found) — a
  structured result rather than a bare bool, so the caller can report exactly
  which items couldn't be restored instead of that failing silently.
- A line item whose product/variant no longer exists in `inventory.json`
  (e.g. the seller hand-edited it between order placement and rejection) is
  logged and added to `skipped` rather than raising. `admin_handlers.py`
  surfaces each skipped item as a `⚠️` line in the `/reject` reply, naming
  the product/size/color, so the seller can reconcile it by hand instead of
  the stock silently vanishing.
- `order_manager.set_order_status_if_not(order_id, status,
  forbidden_current_status)` is an atomic check-and-set under `_orders_lock`.
  `/reject` uses this instead of the plain `set_order_status` so that only
  the call which actually transitions an order to `"rejected"` is allowed to
  restore its stock — a double `/reject` (admin double-tap, retry, etc.) on
  an already-rejected order is a safe no-op ("was already rejected — no
  changes made") instead of crediting stock back twice.
- **Known limitation — fixed, was Issue 3.2 (see "Color matching" below).**
  Previously: if a line item's size/color substring-matched more than one
  variant at decrement time, the original decrement may have spread across
  multiple variants — but the order record only stores
  product_id/size/color/quantity, not which specific variant(s) absorbed it.
  `restore_stock_for_order` credited the full quantity back to the first
  matching variant rather than reconstructing the original split, causing
  inventory drift on rejection whenever colors overlapped as substrings
  (e.g. "red" vs "red/blue"). Not reachable with real inventories in this
  project (one variant per size/color combo, no overlapping color names),
  but a real latent bug — see "Color matching" below for the fix.

Covered by `test_stock_reversal.py`: restore after a single- and multi-item
order, order-not-found, the deleted-variant skip path (both at the
`inventory_manager` layer and the full `reject_order` handler), and the
double-reject idempotency guarantee.

**Update (Issue #16, Nov 2026):** `order_manager.get_order()` is now async
and locked — see "Async file I/O" above. The two direct calls to it in this
file's own test suite (`test_reject_order_handler_restores_stock`,
`test_reject_order_handler_warns_on_skipped_variant`) now `await` it; no
change to the production logic described above.

## Color matching (utils/inventory_manager.py) — fixed, was Issue 3.2
**Previously:** `check_variant()`, `decrement_stock_for_cart()`, and
`restore_stock_for_order()` all matched a cart/order line item's color
against a variant's color with substring containment
(`item_color.lower() in variant_color.lower()`) rather than exact equality.
If a product had two variants whose color strings happened to overlap
(e.g. "red" and "red/blue"), a line item requesting "red" matched *both*
variants, and `decrement_stock_for_cart` could spread a single line item's
quantity across more than one variant. The order record only ever stores a
flat `product_id`/`size`/`color`/`quantity` per item — not which specific
variant(s) absorbed the decrement — so `restore_stock_for_order` (run on
`/reject`) had no way to reconstruct the original split and always
credited the *full* quantity back to just the *first* matching variant.
Net effect: rejecting an order for an overlapping-color product silently
over-credited one variant and under-credited (or entirely missed) the
other(s) — permanent inventory drift. Already documented as a "known
limitation" under "Stock reversal on rejection" above before this fix;
this closes it rather than just documenting around it. Not reachable
against the real `data/inventory.json` (colors are "cool grey", "black",
"cream" — none a substring of another), so the bug was latent rather than
observed in production, which is why it stayed a documented limitation
through several earlier fix rounds instead of getting picked up sooner.

**Now:**
- All three match sites changed from `item.get("color", "").lower() in
  v.get("color", "").lower()` to `item.get("color", "").lower() ==
  v.get("color", "").lower()` — exact, case-insensitive equality. A line
  item's color can now only ever match a single variant, so decrement and
  restore are structurally guaranteed to agree — restore's "credit the
  (only) match" is correct by construction rather than a documented
  limitation.
- **Approach chosen (A) over the alternative considered:** reworking the
  order schema to record a per-variant decrement breakdown (so restore
  replays the exact recorded split instead of re-deriving it) was
  considered and rejected — it would touch the order-placement write path
  (the same one already hardened by the Issue #10/3.1 `_checkout_lock`
  fix) and require a fallback branch for every existing order in
  `data/orders.json` that predates the new field, for meaningfully more
  risk and effort than the root cause required. Approach A stays entirely
  inside `inventory_manager.py`, touches no schema, and matches this
  project's "minimal, structurally-scoped fixes" convention.
- **Deliberate behavior change, flagged explicitly during approach
  selection:** a genuinely partial color that isn't an exact match to any
  variant (e.g. a customer/LLM saying "blue" when only "red/blue" exists as
  a color) no longer fuzzy-matches — it now returns no variant instead of
  matching by substring. Not considered a real regression given the real
  inventory's color names ("cool grey", "black", "cream") aren't
  partial-match-friendly to begin with, but worth watching for if a future
  seller-edited inventory introduces genuinely fragment-style color names.

Covered by `test/test_color_exact_match.py` (10 tests), built around a
deliberately overlapping-color inventory ("red"/"red/blue"): `check_variant`
no longer substring-matches (still case-insensitive); `check_variant` still
matches the longer overlapping string exactly on its own; a genuinely
partial color now correctly returns no match; `decrement_stock_for_cart`
only touches the exact variant ordered, not the overlapping one;
insufficient-stock validation no longer borrows stock from an overlapping
variant to make a request look fulfillable; the full place-then-reject
round trip leaves both variants at their correct stock levels, in both
directions (ordering the shorter and the longer overlapping string); and a
regression check against non-overlapping colors (the shape of the real
`data/inventory.json`). Sanity-checked against the pre-fix code: 5/10 tests
correctly fail there (the ones asserting exact match-count or
no-borrowing behavior); the other 5 pass regardless, since a single line
item that doesn't require spreading across variants happened to work under
the old first-match-priority decrement even pre-fix — documented inline in
the test file. Full suite verified at 77/77 (`pytest . -v`), no
regressions. Branch `fix/issue-3.2-color-exact-match`. Verified
independently by Moeid on his own Windows venv (`python -m pytest . -v` →
77/77, and `python -m pytest test/test_color_exact_match.py -v` → 10/10).

## Admin authorization (handlers/admin_handlers.py) — fixed, was Issue #1
**Previously:** `/pending_orders`, `/confirm`, `/reject` had zero access
control — any Telegram user who could message the bot could run them.
`ADMIN_GROUP_ID` was only ever read on the *outbound* notification side, never
checked on the way in.

**Now:** `AdminOnlyMiddleware` is registered once on `admin_handlers.router`,
so every handler on that router — current and future — is gated in one place
rather than needing a repeated per-handler check. `_is_admin(message)`
authorizes if either:
- `message.chat.id == config.ADMIN_GROUP_ID` (message from the admin group), or
- `message.from_user.id in config.ADMIN_USER_IDS` (a whitelisted admin
  messaging the bot privately)

Unauthorized attempts are logged (`logger.warning`) and silently dropped —
no reply is sent, so a random user can't even confirm the admin commands
exist. `ADMIN_USER_IDS` is a new optional `.env` setting: comma-separated
Telegram user IDs, parsed defensively in `config.py` so a stray non-numeric
entry (e.g. an inline comment that `python-dotenv` didn't strip) is skipped
rather than crashing startup instead of being silently included.
**`.env.example` should never contain a real user ID** — it's a committed
template; real IDs belong only in the untracked `.env`.

Covered by `test_admin_auth.py`: admin-group messages pass, whitelisted
private-chat users pass, an unauthorized chat/user is blocked and the
underlying handler is never invoked.

## Pending orders detail (handlers/admin_handlers.py) — fixed, was Issue #5
**Previously:** `/pending_orders` rendered each order as a single line —
`order_id`, `@username`, a bare item *count*, and status. There was no way
to see what was actually in an order, when it was placed, or how far along
payment was, without opening `data/orders.json` by hand.

**Now:**
- `_format_order_detail(order)` resolves each line item to its product name
  (via `inventory_manager.get_product_by_id`), size, color, and quantity —
  not just a count — plus a formatted `created_at` timestamp and `user_id`
  alongside `@username` (usernames are optional/unreliable on Telegram, so
  `user_id` is the reliable identifier).
- If a line item's product/variant no longer exists in `inventory.json`
  (same edge case `restore_stock_for_order` already handles for `/reject`),
  the block falls back to showing the raw `product_id` with a "no longer in
  inventory" note instead of crashing or silently omitting it.
- `_format_payment_status(order)` distinguishes "no proof yet"
  (`pending_confirmation`) from "proof submitted, awaiting review"
  (`awaiting_review`), and names whether the proof was a screenshot or a
  typed reference.
- An estimated total is shown, computed from **current** `inventory.json`
  prices and explicitly labeled as an estimate — `orders.json` doesn't store
  a price-at-purchase snapshot, so this can drift from what the customer
  actually paid if prices changed since. Capturing a real price-at-purchase
  snapshot on the order record itself was considered and deliberately left
  out of this fix's scope (it would touch the order-placement/schema path,
  which is higher-risk and orthogonal to what Issue #5 asked for — a future
  issue if price drift over time turns out to matter).
- `_chunk_order_blocks(blocks)` splits the rendered orders across multiple
  Telegram messages once the combined text would approach the 4096-character
  API limit (chunked at 3500 chars for safety margin), instead of risking a
  single oversized message. Verified against the real (pre-cleanup)
  `data/orders.json` of 50 orders: correctly split into 4 messages.

Covered by `test_pending_orders_detail.py` (14 cases): item-detail
formatting (single and multi-item orders), timestamp/user_id, estimated
total math, the deleted-product fallback, all three payment-status
variants, chunk-boundary behavior, and the end-to-end handler (no-pending
message unchanged, confirmed/rejected orders still excluded, chunking
across many orders). Display-layer only — no changes to order/inventory
schema, no new locks, nothing on the `place_order`/`/reject` concurrency
path touched. Verified live against the running bot in addition to the
test suite.

**Update (Issue #16, Nov 2026):** this test file's own `_seed_order()`
helper reaches into `order_manager._load()`/`_save()` directly (there's no
public API for directly overwriting an order's status/proof outside the
real transitions) — both are now async (see "Async file I/O" above), so
the helper now awaits them. No change to `_format_order_detail`/
`_format_payment_status`/`_chunk_order_blocks` or the `pending_orders()`
handler itself.

## The tool-calling loop (agent/agent.py)
This is the most-edited file in the project. Key things to know before
touching it:

- `MAX_TOOL_ROUNDS = 9` — was 5 originally, raised because malformed-call
  recovery burns a round just like a real tool call, and multi-item orders
  (search + N availability checks + N cart adds) can legitimately need most
  of the budget on their own.
- Two separate malformed-tool-call failure modes exist and are handled
  the same shape, in two different places in the file:
  1. **API-level rejection** (`BadRequestError`, e.g. Groq's `tool_use_failed`) —
     the model emitted something like `<function=name={"arg":"val"}</function>`
     instead of a real tool call. `_parse_native_function_call()` uses
     `json.JSONDecoder.raw_decode()` (not regex-matched braces) to safely
     recover the intended call from `failed_generation` in the error body,
     correctly handling nested objects/arrays and escaped quotes, and
     executes it directly.
  2. **Leaked plain-text JSON** — no API error at all, the model just wrote
     `{"name": "...", "parameters": {...}}` (or `{"type": "function", "name": ...}`
     — key order varies) as normal reply content, inside `if not
     choice.tool_calls:`. `_parse_leaked_json_tool_call()` parses this
     structurally rather than via regex.

  Both recovery paths follow the same shape once a call is parsed: run it
  through `call_tool()`, track `place_order`/`log_feedback` events, append a
  synthetic `assistant` message (with a fake `tool_calls` entry) + the
  matching `tool` result message, reset `malformed_retries = 0`, and
  `continue` the loop so the model can see the tool result on the next round.

  ✅ **Issue #3 — fixed, merged to `main`.** The leaked-JSON path (`if
  parsed:` inside `if not choice.tool_calls:`) used to just `continue`
  without ever touching `messages`, so a leaked-JSON reply either silently
  repeated until `MAX_TOOL_ROUNDS` ran out, or fell through into the
  narration-guard check below it and returned early. The actual recovery
  logic (`call_tool`, event tracking, message appends) sat *after* that
  early `return` and was unreachable dead code — only the
  `BadRequestError`/native-tag path was actually doing what leaked-JSON
  recovery was meant to do. Fixed by moving the recovery logic directly
  inside `if parsed:`, mirroring the native-function-call recovery shape,
  and deleting the dead code. The narration guard and the genuine
  plain-text final-return path immediately below it are unchanged in
  behavior — this was a minimal, structurally scoped fix (no shared helper
  extracted between the two recovery paths, to avoid touching the
  already-working, already-tested native-recovery code for pure
  deduplication). Branch `fix/issue-3-leaked-json-recovery`, merged to
  `main` (Aug 2026) — verified via the full test suite plus
  `test_leaked_json_recovery.py` (sandboxed, LLM client mocked; no live bot
  was available at fix time), and independently re-run by Moeid on his own
  machine (8/8 passed).
- **Narration guard (`if not is_negative and any(phrase in reply_lower for
  phrase in commitment_phrases):`)** — a structural guard scans the model's
  plain-text reply for commitment phrases ("I've added that to your cart",
  "order is placed", etc.) when no tool call was made, since smaller/local
  models sometimes narrate an action instead of actually calling the tool
  for it. When it fires, it injects a correction message telling the model
  to actually call the tool and retries.

  ✅ **Issue 1.2 — fixed, merged to `main` (Aug 2026), HIGHEST PRIORITY per
  the bug-audit-evaluation.** The guard correctly detected the narration and
  appended a correction message to `messages` — but then immediately
  executed `return reply, events`, so the correction was never actually
  sent to the model. The customer received the false "added"/"placed"
  claim as-is, the cart/order never changed, and the guard's entire purpose
  (self-correction) never happened; it only ever *logged* the problem. Same
  failure shape as Issue #3's leaked-JSON dead-code bug: a correctly-built
  recovery path that never actually executed because of an early return in
  the wrong place. Fixed with a one-line change — `return reply, events` ->
  `continue` — so the loop repeats with the correction already in
  `messages` and the model gets a real chance to call the tool. The
  narrated (unverified) turn is intentionally **not** persisted to
  conversation history on this path (unlike the genuine final-return path
  right below it) — it's an internal retry step, not a completed turn; only
  the eventual real outcome (a genuine tool result + final reply, or the
  `MAX_TOOL_ROUNDS` fallback) gets saved. `MAX_TOOL_ROUNDS` already acts as
  the backstop if the model narrates repeatedly instead of ever calling a
  real tool. Branch `fix/issue-1-2-narration-guard-continue`, merged to
  `main` (Aug 2026) — covered by `test_narration_guard_loop.py` (7 new
  tests: the core continue-vs-return fix, correction-message delivery,
  narrated-turn-not-persisted, repeated-narration hits the `MAX_TOOL_ROUNDS`
  safety cap without hanging, and 3 regression checks for the
  negative-phrasing filter / plain replies / the `place_order` narration
  case). Sanity-checked against the pre-fix code (5/7 correctly failed
  there; the 2 that still passed were the untouched regression checks).
  This fix also required a small addendum: `test_leaked_json_recovery.py`'s
  `test_narration_guard_still_triggers_when_parsed_is_falsy` had encoded
  the *old* return-immediately behavior as its expected outcome (single
  mocked LLM call), so it started failing with `StopAsyncIteration` once
  the guard correctly tried a second round. Updated (not deleted) to mock
  a second round and assert the corrected behavior — still verifies its
  original, narrower purpose (the leaked-JSON `parsed` check doesn't
  swallow/short-circuit the narration guard). Full suite verified at 44/44
  after both patches (`pytest . -v`).
- `RateLimitError` is caught separately and returns immediately with a
  friendly message — no retry, since more tokens won't fix a quota problem.
- `call_tool()` in tools.py catches `KeyError` (missing required arg) and any
  other exception, returning `{"error": ...}` instead of crashing — a model
  forgetting an argument should never take down the whole update handler.
- **Event tracking (`events` list, returned from `run_agent` for the caller
  to notify the admin group).** Three tool outcomes are tracked, at all
  three places a tool result gets processed (the normal structured tool-call
  path, the native-function-call recovery path, and the leaked-JSON
  recovery path) — `place_order` -> `{"type": "order", ...}`, `log_feedback`
  -> `{"type": "feedback", ...}`, and `submit_payment_reference` ->
  `{"type": "payment_reference", ...}`.

  ✅ **Issue 1.3 — fixed, merged to `main` (Aug 2026), High priority per the
  bug-audit-evaluation.** `submit_payment_reference` outcomes were never
  tracked into `events` at all — only `place_order`/`log_feedback` were.
  Meanwhile `handlers/user_handlers.py::_notify_admin` already had a fully
  built `"payment_reference"` branch, ready and waiting, that nothing ever
  triggered — dead code on the receiving end. Effect: a customer typing a
  transaction reference (as opposed to sending a payment screenshot, which
  is caught by the separate non-LLM photo handler) got their reference
  correctly recorded in `orders.json` and the order correctly moved to
  `awaiting_review` — but the admin group was never told, so the seller had
  no way to know a payment needed reviewing; the order just sat there.
  Fixed by adding `if name == "submit_payment_reference" and
  result.get("status") == "proof_submitted": events.append({"type":
  "payment_reference", "data": result["order"]})` at all three call sites,
  mirroring the existing `place_order`/`log_feedback` shape exactly — a
  recovered call (native-tag or leaked-JSON) needed the same notification
  as a clean structured call, so all three sites were fixed together rather
  than just the main path (same "silent gap" principle as elsewhere in this
  doc — recovery paths must mirror the structured-call path, not lag
  behind it). `result["order"]` already contains `order_id`/`username`/
  `payment_proof`, exactly what `_notify_admin`'s existing branch expects,
  so no changes were needed on the `user_handlers.py` side. Branch
  `fix/issue-1-3-payment-reference-notification`, merged to `main` (Aug
  2026) — covered by `test_payment_reference_notification.py` (5 tests: the
  event firing at each of the three call sites, the no-event case when
  `submit_payment_reference` fails, and a regression check that
  `place_order` event tracking is unaffected). Sanity-checked against the
  pre-fix code (3/5 correctly failed there; the 2 that still passed were
  the untouched regression/failure-case checks). Full suite verified after
  merge (`pytest . -v`).
- **Recovery `tool_call_id`s are unique per event — fixed, was Issue 2.1.**
  See `docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the full write-up;
  `_new_recovery_tool_call_id()` (`uuid.uuid4().hex`) replaced the two
  hardcoded literals (`"recovered_1"`, `"recovered_json_1"`) at both
  recovery sites. Fixed and merged 2026-10-29. Covered by
  `test/test_recovery_tool_call_ids.py`.
- A handful of `# type: ignore` / `# pyright: ignore[...]` comments were
  added in `agent/agent.py` and `agent/tools.py` (Aug 2026, on `main`
  directly, unrelated to any numbered issue) purely to suppress static-type
  warnings from the OpenAI SDK's typing (e.g. `tool_call.function.name`,
  dict-subscripting a value typed as possibly `None` after an `if error:`
  guard already ruled that out at runtime). No behavior change — these are
  editor/type-checker noise suppressions only.
- `agent/tools.py::_submit_payment_reference` now `await`s
  `order_manager.get_latest_unpaid_order` — see "Async file I/O" above
  (Issue #16); no other change to this file.

## Prompt rules (agent/prompts.py)
Every rule in this file exists because of a specific observed failure —
treat it as a running log, not just instructions:
- "Never say you'll do something without calling the tool" — models were
  narrating actions ("I'll add that to your cart") without ever calling
  `add_to_cart`, leaving the cart empty while claiming otherwise.
- "Never say 'confirmed'/'order' before place_order actually succeeds" —
  models were using order-confirmation language right after `add_to_cart`,
  before any order existed.
- "Don't re-verify already-confirmed items" — models were redundantly
  re-calling `check_availability`/`add_to_cart` on items already added,
  burning the round budget without progressing the conversation.
- "Never invent shipping times, tracking emails, etc." — models fabricated
  plausible-sounding fulfillment details that this system doesn't actually
  provide. Only `place_order` and `submit_payment_reference` are real;
  nothing sends a customer email.

If a new failure mode shows up, the fix belongs here first (cheap, fast to
iterate) before reaching for a code-level workaround.

## Payment flow
No payment gateway (by design). After `place_order`, the customer sends
either a screenshot (caught directly by `handle_payment_screenshot` in
user_handlers.py — bypasses the LLM entirely, more reliable than hoping the
model handles an image message correctly) or types a transaction reference
(agent calls `submit_payment_reference`). Either way, the order moves to
`awaiting_review` and the admin group gets a message with `/confirm <id>` or
`/reject <id>` ready to copy-paste — **typed references now notify the admin
group too, matching the screenshot path** (see the Issue 1.3 fix under "The
tool-calling loop" above; previously only screenshots did, and a typed
reference silently vanished with no seller-facing signal). `/confirm`
updates status and DMs the customer; `/reject` updates status, DMs the
customer, **and now also restores stock** (see "Stock reversal on
rejection" above). Both commands require admin authorization (see "Admin
authorization" above) to invoke. `/pending_orders` shows full order detail
per line item, not just a count (see "Pending orders detail" above).

**Customers can now resubmit payment proof — fixed, was Issue 4.2.** See
`docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the full write-up (Option B:
explicit `payment_proof_history` audit trail, not a silent overwrite).
`order_manager.get_latest_unpaid_order()` now also matches `awaiting_review`
orders (not just `pending_confirmation`), every submission is appended to
`payment_proof_history`, and both `handle_payment_screenshot` and the admin
notification in `user_handlers.py::_notify_admin` flag a resubmission with
a ⚠️ RESUBMISSION notice. Fixed and merged 2026-10-29. Covered by
`test/test_payment_resubmission.py`. `get_latest_unpaid_order` is also now
async/locked as of Issue #16 — see "Async file I/O" above; this fix's own
logic is otherwise unchanged by that.

## Known environment gotchas (all cost real debugging time — check these first)
- **`ADMIN_GROUP_ID` stops working silently** if the group converts to a
  supergroup, or sometimes even after privacy-mode changes that don't
  retroactively apply to an already-joined bot. If admin notifications stop
  arriving with `TelegramBadRequest: chat not found`, the fix that's worked
  twice: remove the bot from the group and re-add it, then re-fetch the
  chat_id via a fresh message + `getUpdates` (bot must NOT be running when
  you check `getUpdates` manually — it conflicts with the bot's own polling
  and returns an empty result). Current supergroup (Topics/is_forum enabled)
  chat ID: `-1003995783289`.
- If Telegram (app or web) doesn't load without a VPN, the bot won't connect
  either — `aiohttp` doesn't pick up system VPN/proxy settings automatically.
  A full system-wide VPN client (not a local SOCKS/HTTP proxy app) needs to
  simply be connected before running the bot; no code-level proxy config
  needed in that case.
- **`python-dotenv` doesn't reliably strip inline comments on an otherwise
  empty value.** `ADMIN_USER_IDS=   # optional` parses as the literal string
  `"# optional"`, not empty — this crashed startup once. Keep comments for
  blank-by-default `.env` values on their own line above the variable, not
  trailing on the same line. `config.py`'s parsing of `ADMIN_USER_IDS` is
  defensive against this now (`.isdigit()` filter), but the `.env.example`
  formatting itself still matters for anyone hand-editing their `.env`.
- **`git am` can fail with `error: <file>: does not match index`** on
  Windows if the target file already has local, uncommitted changes (e.g.
  from a previous partial patch attempt) — the patch's context lines no
  longer match. `git am --abort` and either resolve the local diff first or
  fall back to whole-file replacement instead of re-patching (see "Whole-file
  replacement" principle below). Also, on Windows PowerShell, `git am` needs
  a real path (relative like `.\file.patch` or a full absolute path) — a
  placeholder like `/path/to/file.patch` will just fail with "no such file
  or directory"; make sure the patch file actually exists at the path passed.
- **`git apply` (unlike `git am`) only writes to the working directory — it
  does not create a commit.** Discovered during the Issue #5 fix: the patch
  applied cleanly, but `git show <branch> --stat` still pointed at the old
  commit because nothing had actually been committed yet. Always follow
  `git apply` with `git status` to confirm the files really changed, then
  `git add` + `git commit` explicitly — don't assume `git apply` succeeding
  means the branch history was updated.
- **Unrelated local edits can silently ride along into a feature branch.**
  During the Issue #5 fix, `agent/agent.py` and `agent/tools.py` had
  uncommitted local `# type: ignore` edits sitting in the working tree
  (unrelated warning suppressions) at the same time the Issue #5 patch was
  applied. `git status` before committing caught this. Fix: `git stash push
  <file1> <file2> -m "..."` to set the unrelated files aside, commit the
  actual fix cleanly, then `git checkout main && git stash pop` to land the
  unrelated edits on `main` as their own separate commit instead of bundling
  them into the feature branch. Always check `git status` (and `git diff`
  on anything unexpected) before staging, especially on a long-running local
  checkout where multiple things may have been edited between sessions.
  **Issue #16 repeated this lesson in a different shape:** `git status`
  after the fix showed `data/inventory.json` and `data/orders.json` as
  modified from running the suite/manual testing during development — same
  class of issue as the `data/*.json` gotcha below, caught the same way
  (`git status` before staging, `git checkout -- data/...` to discard).
- **`pytest test_*.py test/ -v` doesn't work in PowerShell.** PowerShell
  doesn't expand `*` globs before handing args to external commands the way
  bash does, so the glob is passed to pytest literally and it reports "file
  or directory not found: test_*.py". Use `pytest . -v` instead (pytest's
  own recursive discovery finds every `test_*.py` file and already ignores
  `venv/` by default), or list the files explicitly.
- **Bare `pytest . -v` can fail to collect `test/test_admin_auth.py` on
  Windows** with `ModuleNotFoundError: No module named 'config'`, even
  though the file's own `sys.path.insert(0, str(Path(__file__).resolve().parent))`
  is supposed to make the repo root importable. Confirmed during the Issue
  #10 verification (Aug 2026) on Moeid's Windows venv — the same command
  worked fine in the sandboxed Linux CI-style environment used during
  development, so this is Windows/invocation-specific, not a code bug.
  **Fix:** run pytest as a module instead — `python -m pytest . -v` — which
  guarantees the current directory is actually on `sys.path` the way the
  test files' manual `sys.path.insert` calls expect, regardless of how the
  `pytest` executable itself was resolved/invoked. Prefer `python -m pytest
  . -v` over bare `pytest . -v` on Windows going forward. (This is also why
  Issue #16's own live verification runs both used `python -m pytest . -v`.)
- **pytest only discovers files actually named `test_*.py`.** A file saved
  as `testFoo.py` or `testFoo_bar.py` (missing the underscore right after
  `test`) runs fine with `python testFoo.py` if it has its own `main()`
  guard, but `pytest . -v` silently skips it — no error, it just isn't in
  the collected item count. Worth eyeballing the filename after any
  save-as/rename on Windows, and comparing the "collected N items" count
  against the number of test files you expect. (A stray duplicate,
  `testLeaked_json_recovery.py`, showed up in the working tree during the
  Issue #5 session for exactly this reason — deleted, not committed.)
- **`data/*.json` files are tracked in git and are easy to accidentally
  overwrite or lose.** Several root-level test scripts (`test_concurrency.py`,
  `test_cart_persistence.py`) write directly to whatever `config.ORDERS_FILE`
  / `config.CARTS_FILE` point to with **no sandboxing** — unlike
  `test/test_backend.py`, `test_stock_reversal.py`, and
  `test_leaked_json_recovery.py`, which redirect
  `config.INVENTORY_FILE`/`config.ORDERS_FILE`/`config.CONVERSATIONS_DIR`/etc.
  to a throwaway `test_data/` directory before running. Running those two
  scripts directly (`python test_concurrency.py`), or even just using the
  bot for manual testing, modifies the real `data/` files. **Always run `git
  status` before committing and check whether `data/` changes are
  intentional** — a stray `git add .` after a test/manual-testing session
  can silently wipe real order history (this happened once: 50 pre-existing
  orders got reduced to 1 in a single careless commit, recovered from git
  history afterward). If the changes aren't intentional, `git checkout --
  data/` discards them before committing.
  **Confirmed root cause (Aug 2026):** the 50 synthetic `user_0`..`user_49`
  orders that were sitting in real `data/orders.json` were exactly this —
  `test_concurrency.py` had been run standalone against real data at some
  point rather than through the sandboxed suite. Once `/pending_orders`
  started showing full detail (Issue #5), this stale test data became
  obviously visible for the first time. Cleaned up via a dedicated commit
  (`Clear synthetic test orders from data/orders.json`, reset to `[]`)
  directly on `main`, separate from the Issue #5 code fix.
- **A module-level `asyncio.Lock()` singleton can only ever be genuinely
  contended once a real `await` sits inside its critical section — and
  pytest-asyncio's one-event-loop-per-test-function default means that,
  once it is, the SAME lock object can clash across tests with "is bound to
  a different event loop."** First hit for `_checkout_lock` (Issue 3.1,
  "Checkout atomicity" above); hit again for `_history_lock` the moment
  Issue #16 added a genuine `asyncio.to_thread()` yield point inside
  `conversation_manager`'s locked sections (see "Async file I/O" above).
  Watch for this specifically whenever a fix adds a *real* await (a genuine
  I/O call, not just another coroutine call that resolves synchronously)
  inside an existing module-level lock's critical section — any
  concurrency test for that lock that uses `asyncio.gather()` across
  multiple tasks may need the same fix: take `monkeypatch` and reset the
  lock to a fresh `asyncio.Lock()` instance at the top of the test. Purely
  a test-isolation artifact (the real bot process has one long-lived event
  loop for its entire lifetime), never a reason to change the production
  locking design itself.

  ## Known environment gotchas (continued) — SOCKS proxy / httpx2 / national bandwidth throttling (Nov 2026)

**Symptom:** `python bot.py` crashed immediately on the first inbound
message with:
```
ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
```
raised from inside `agent/llm_client.py::get_client()` when constructing
the `AsyncOpenAI` client — well before `_checkout_lock`, rate limiting, or
any of this project's own code ran.

**Root cause, layer 1 — the SOCKS proxy itself.** Moeid's dev machine runs
a VPN client that exposes a local SOCKS5 proxy
(`socks5://127.0.0.1:10808`) and sets `HTTP_PROXY`/`HTTPS_PROXY` system-wide
to point at it — needed because `api.telegram.org` is unreachable from his
location without it (see the existing "If Telegram (app or web) doesn't
load without a VPN..." gotcha above, which predates this one). `httpx`
(the transport the OpenAI SDK uses) trusts those env vars by default
(`trust_env=True`), so even the local Ollama call
(`http://localhost:11434`) — which never needs a proxy — got routed
through the SOCKS proxy. `httpx`'s SOCKS support needs the separate
`socksio` package, which wasn't installed, hence the crash.

**Root cause, layer 2 — a second, unexplained `httpx2` package.**
Tracebacks and later request logs showed the actual failing/succeeding
module as `httpx2` (e.g. `File "...venv\Lib\site-packages\httpx2\_client.py"`,
`INFO:httpx2:HTTP Request: ...`), not plain `httpx` — a separate installed
package (version 2.10.0 at the time, with its own `httpcore2`,
`truststore`, etc.) that was somehow the one actually being imported and
used, despite nothing in this project's own code referencing it.
**Root cause not fully identified** — most likely either a transitive
dependency of `openai`/`aiogram` pulling in a differently-named fork/alias,
or a leftover from an earlier `pip install` in this venv. Flagged here
rather than resolved, since the fix below (explicit `trust_env=False` on
the client this project constructs) sidesteps the question without
needing to answer it: regardless of which package ends up handling the
request, an explicit `httpx.AsyncClient(trust_env=False)` passed into
`AsyncOpenAI(http_client=...)` is what's actually imported and used (later
logs correctly showed `INFO:httpx:` instead of `INFO:httpx2:` once this
was in place), confirming the fix took effect at the right layer. If
`httpx2` resurfaces in a future `pip install -r requirements.txt` on a
fresh venv, it's worth a `pip show httpx2` / dependency-tree check
(`pip install pipdeptree && pipdeptree -p httpx2`) to actually trace it,
since this investigation never got to a definitive answer.

**Also hit, unrelated to the proxy itself but blocking every `pip install`
attempt along the way:** the venv's own `pip.exe`/`python.exe` launcher
executables had a stale, broken absolute path baked in
(`C:\Users\VAHABI\Desktop\...\store_bot\venv\Scripts\python.exe`) from
when the venv was originally created at a different location before the
project moved to `D:\Me\Projects\Telegram Store Bot\store_bot`. Every
`pip ...` invocation failed with `Fatal error in launcher: Unable to
create process using ...`. **Fix: use `python -m pip ...` instead of the
bare `pip`/`pip.exe` launcher** — this calls pip through the already-active
interpreter rather than through the broken launcher shim, and worked
immediately. (If `python -m pip` also fails the same way, the venv itself
is unrecoverably tied to the old path and needs recreating — see the
general advice already given above for `venv`/`.venv` issues in "Setup and
Installation.") Worth remembering any time this venv is moved, copied, or
restored from a backup to a different path.

**Fix for the proxy/SOCKS issue — explicit `trust_env=False`, not
installing `socksio`.** Installing `socksio` (once `python -m pip`
unblocked installs) and setting `NO_PROXY=localhost,127.0.0.1` were both
tried first and **neither fixed the underlying latency problem** — `httpx`/
`httpx2`'s proxy auto-detection apparently didn't respect `NO_PROXY` the
way plain `requests`-style tooling does, or built its client before
`NO_PROXY` was consulted. The fix that actually worked: `agent/llm_client.py`
now constructs both the Groq and Ollama `AsyncOpenAI` clients with an
explicit `http_client=httpx.AsyncClient(trust_env=False)`, so neither
client ever looks at `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` —
connections always go straight to the configured `base_url`
(`api.groq.com` or `localhost:11434`) with zero proxy involvement,
regardless of what's set in the shell the bot is launched from. This
matches `bot.py`'s own existing convention for Telegram (an explicit,
opt-in `PROXY_URL`, never ambient env-var detection) — if a future LLM
provider genuinely needs a proxy to reach, pass it explicitly via
`httpx.AsyncClient(proxy=...)` in that provider's own branch, rather than
re-enabling `trust_env`.

**The real remaining cause of slow turns: national internet throttling,
not a bug.** After the `trust_env=False` fix, Ollama's own latency
(confirmed via direct, bot-bypassing timing: `ollama run qwen3:8b "say
hi"` ≈ 10s; the same call through `agent/llm_client.py`'s real code path
with the full `SYSTEM_PROMPT` + `TOOL_SCHEMAS` ≈ 25s) was no longer the
dominant cost — but full bot turns were still taking 30-120+ seconds,
inconsistently. Direct testing of the Telegram leg alone
(`bot.get_me()`) intermittently failed outright with
`ClientConnectorDNSError` (DNS couldn't resolve `api.telegram.org`) on one
run and `ConnectionRefusedError`/"remote computer refused the network
connection" on another, both while the VPN showed as "connected." This
traces to the VPN connection itself being degraded, not flaky
configuration: Moeid's country throttled international/VPN-routed
bandwidth nationwide around this time (see
https://radar.cloudflare.com/traffic/ir for the traffic pattern). Every
Telegram API call (`send_chat_action`, `message.answer`, and implicitly
aiogram's own long-polling `getUpdates`) depends on that same degraded
VPN path, so this adds real, unpredictable, sometimes very large latency
to every single turn — on top of, not instead of, the LLM call itself.
**This is an infrastructure/network constraint outside the application's
control**, not something further code changes can fix.

**Mitigation shipped for this: a periodic typing-indicator refresh.**
Telegram's "typing..." indicator expires client-side after ~5 seconds — a
single `send_chat_action()` call at the top of a turn (the previous
behavior) goes stale and silently disappears partway through any turn
that takes longer than that, which is now routine rather than exceptional
given the network conditions above. `handlers/user_handlers.py::handle_message`
now runs a background task (`_keep_typing`) that re-sends the typing
action every `_TYPING_REFRESH_SECONDS` (4s) for the full duration of the
`run_agent()` call, cancelled in a `finally` block so it never outlives
the real reply — whether `run_agent` returns normally or raises. A
failure inside a single typing-refresh send (e.g. a transient Telegram
API hiccup, itself more likely under the current network conditions) is
caught and logged rather than allowed to crash the user-facing turn — the
customer's actual reply must never be lost because a cosmetic "still
typing" pulse failed to send.

Both fixes (`trust_env=False` and the typing-indicator refresh) are
unconditionally correct improvements and worth keeping even after/if the
national bandwidth throttling eases — the first removes a genuinely
unnecessary proxy hop for local traffic, and the second makes any
longer-than-5-second turn (slow model, multi-round tool call, network
hiccup, or just a big order) show honest "still working" feedback instead
of going silently dead.

## Testing
```bash
pytest . -v
```
(On Windows PowerShell, use this form rather than `pytest test_*.py test/
-v` — see "Known environment gotchas" above for why the glob form fails.)

- `test_concurrency.py` — 50 simulated concurrent `place_order` calls, asserts
  no data loss under the `asyncio.Lock`. **Writes directly to the real
  `data/orders.json`** when run standalone — see the git-workflow gotcha above.
- `test_cart_persistence.py` — cart survives a simulated restart via disk reload.
  Also writes directly to real `data/carts.json` when run standalone.
- `test_history_persistence.py` — conversation trimming never orphans a
  `tool_call`/`tool` result pair. Updated for the Issue 3.3 fix (Aug 2026):
  `append_messages`/`get_recent_for_llm` are now `async`, so its two calls
  to them are `await`ed; no behavioral change to the test itself.
- `test_conversation_lock.py` — Issue 3.3 coverage: the
  `conversation_manager` `asyncio.Lock` fix. Sandboxes
  `config.CONVERSATIONS_DIR` to a throwaway `test_data/conversations/`
  directory, same sandboxing spirit as the other isolated suites, so real
  `data/conversations/` is never touched. 7 tests: the core race (50
  concurrent `append_messages()` calls for one user lose zero messages);
  per-user correctness under concurrent multi-user load; two
  deadlock-safety tests for `get_recent_for_llm`/`load_history` interleaved
  with concurrent writes, wrapped in `asyncio.wait_for(..., timeout=10)` so
  a wrong sync/async split fails loudly instead of hanging the run; and 3
  regression checks (single-call roundtrip, the existing safe-trim logic,
  empty history for a new user). Sanity-checked against the pre-fix code —
  all 7 fail there with `TypeError` (awaiting functions that weren't
  coroutines pre-fix); a separate direct thread-pool-based repro against
  the literal pre-fix file additionally surfaced a real `JSONDecodeError`
  from a torn read/write, confirming the race is real. 7/7 passed via both
  `pytest` and the standalone `python test_conversation_lock.py` runner.
  Independently re-run by Moeid on his own Windows venv (both forms
  passed) and verified manually against the live bot.
  **Updated for Issue #16 (Nov 2026):** two of the seven tests
  (`test_concurrent_appends_for_different_users_dont_interfere`,
  `test_load_history_and_append_messages_interleaved_no_deadlock`) now take
  `monkeypatch` and reset `_history_lock` to a fresh instance — see "Async
  file I/O" above and the new "Known environment gotchas" entry on
  module-level lock singletons. Re-verified at 7/7 (standalone runner, run
  from this sandbox) and as part of the full 93/93 live suite.
- `test_narration_guard.py` — unit tests the commitment-phrase detection logic
  used in `agent.py`'s narration guard, independent of a live model. Not
  pytest-discovered (no `test_`-prefixed functions, only `main()`) — run
  directly with `python test_narration_guard.py`.
- `test_nested_json_regex.py` — unit tests `_parse_native_function_call`
  against nested JSON, escaped quotes, and malformed input. Also not
  pytest-discovered; run directly with `python test_nested_json_regex.py`.
- `test_admin_auth.py` — admin middleware authorization (see above).
- `test_stock_reversal.py` — Issue #4 coverage: restore-on-reject (single-
  and multi-item orders), order-not-found, the deleted-variant skip path at
  both the `inventory_manager` and `admin_handlers` layers, and the
  double-reject idempotency guarantee. Sandboxes `config` paths to
  `test_data/`, same pattern as `test/test_backend.py`, so it never touches
  real `data/`. **Updated for Issue #16 (Nov 2026):** two direct calls to
  `order_manager.get_order(...)` (now async/locked) now `await` it — see
  "Async file I/O" above.
- `test/test_color_exact_match.py` — Issue 3.2 coverage: the
  exact-vs-substring color matching fix across `check_variant()`,
  `decrement_stock_for_cart()`, and `restore_stock_for_order()`. Sandboxes
  `config` paths to `test_data/`, same pattern as `test_stock_reversal.py`,
  built around a deliberately overlapping-color inventory ("red"/
  "red/blue") rather than the real seed data, since the real inventory has
  no overlapping colors to exercise the bug with. 10 tests: `check_variant`
  no longer substring-matches while staying case-insensitive, exact match
  on the longer overlapping string still works, a genuinely partial color
  now correctly returns no match; `decrement_stock_for_cart` only touches
  the exact variant ordered and insufficient-stock validation no longer
  borrows from the overlapping variant; the full place-then-reject round
  trip leaves both variants at their correct stock levels in both
  directions (ordering the shorter and the longer string); and a
  regression check against non-overlapping colors (the real
  `data/inventory.json`'s shape). Sanity-checked against the pre-fix code —
  5/10 correctly failed there (the ones asserting exact match-count or
  no-borrowing); the other 5 pass regardless since a single line item that
  doesn't need to spread across variants happened to work under the old
  first-match-priority decrement even pre-fix. 10/10 passed via both
  `pytest` and the standalone `python test/test_color_exact_match.py`
  runner. Independently re-run by Moeid on his own Windows venv
  (`python -m pytest . -v` → 77/77 full suite).
- `test_leaked_json_recovery.py` — Issue #3 coverage: leaked-JSON recovery
  for `add_to_cart`/`place_order`/`log_feedback`, executed against real
  `call_tool`/business logic (sandboxed `test_data/`, including
  `CONVERSATIONS_DIR`) with only the LLM client mocked
  (`agent.agent.get_client`); confirms the loop `continue`s instead of
  returning early, the recovered `assistant`+`tool` messages are actually
  persisted, `place_order`/`log_feedback` event tracking is preserved, and
  `malformed_retries` resets on a successful recovery (verified against a
  real `openai.BadRequestError` round-trip, not just a mock). Also confirms
  non-JSON and invalid-JSON plain text still fall through unaffected, and
  the narration guard still fires when nothing was parsed. Sanity-checked
  against the pre-fix code to confirm the tests actually fail there (4/8
  failed on the old `agent.py`) before confirming they pass on the fix.
  Verified independently by Moeid on his own Windows venv (`python
  test_leaked_json_recovery.py` → 8/8 passed) in addition to the sandboxed
  CI-style run. **Updated for the Issue 1.2 fix** (Aug 2026):
  `test_narration_guard_still_triggers_when_parsed_is_falsy` originally
  encoded the pre-1.2-fix return-immediately behavior as its expected
  outcome; now mocks a second LLM round and asserts the corrected
  continue-based behavior instead, while still verifying its original,
  narrower purpose (the leaked-JSON `parsed` check doesn't
  swallow/short-circuit the narration guard).
- `test_narration_guard_loop.py` — Issue 1.2 coverage: the narration guard's
  `return reply, events` -> `continue` fix. Executed against the real agent
  loop with only the LLM client mocked, same sandboxing pattern as
  `test_leaked_json_recovery.py`. 7 tests: the core fix (guard fires, loop
  continues, model self-corrects with a real `add_to_cart` call, cart is
  genuinely populated — proven by the mock's response list actually being
  fully consumed); the correction message is verifiably present in the
  *next* request's `messages` payload (not just appended and discarded);
  the narrated (unverified) turn is never persisted to conversation
  history; repeated narration hits the `MAX_TOOL_ROUNDS` safety cap
  gracefully instead of hanging (`MAX_TOOL_ROUNDS` temporarily lowered via
  monkeypatch so the test doesn't need 9+ mocked responses); the same
  self-correction flow for `place_order`, confirming `events` (used for
  admin notification) only populates once the real tool call happens; and
  two regression checks (negative-phrasing filter, plain replies with no
  commitment language) confirming those paths are untouched. Sanity-checked
  against the pre-fix code — 5/7 correctly failed there (the 2 that still
  passed were the untouched regression checks). Independently re-run by
  Moeid on his own Windows venv (`pytest test_narration_guard_loop.py -v` →
  7/7 passed).
- `test_payment_reference_notification.py` — Issue 1.3 coverage: the
  `submit_payment_reference` -> `events` gap. Same sandboxing pattern
  (`test_data/`, `CONVERSATIONS_DIR`, only `agent.agent.get_client`
  mocked). 5 tests: the event firing correctly from the normal structured
  tool-call path, from the native-function-call (`BadRequestError`)
  recovery path, and from the leaked-plain-text-JSON recovery path — each
  asserting the `payment_reference` event's `data` matches the real order
  (`order_id`, `username`, `payment_proof`); the no-event case when
  `submit_payment_reference` fails (no order awaiting payment for that
  customer); and a regression check that `place_order` event tracking is
  unaffected by the new branch. Sanity-checked against the pre-fix (but
  post-1.2) code — 3/5 correctly failed there (the 2 that still passed
  were the untouched no-event/regression checks). Independently re-run by
  Moeid on his own Windows venv (`pytest test_payment_reference_notification.py -v`
  → 5/5 passed).
- `test_pending_orders_detail.py` — Issue #5 coverage: item-detail
  formatting (single/multi-item orders, deleted-product fallback), all three
  payment-status variants, estimated-total math, `_chunk_order_blocks`
  boundary behavior, and the end-to-end `pending_orders()` handler (empty
  state unchanged, confirmed/rejected orders still excluded, chunking across
  many orders). Sandboxes `config` paths to `test_data/`, same pattern as
  `test_stock_reversal.py`. 14/14 passed via both `pytest` and the standalone
  `python test_pending_orders_detail.py` runner; also verified live against
  the running bot's real `/pending_orders` output. **Updated for Issue #16
  (Nov 2026):** the `_seed_order()` test helper now awaits
  `order_manager._load()`/`_save()` (now async) — see "Async file I/O"
  above.
- `test_multi_item_checkout.py` — Issue #6 / bug-audit 1.1 coverage: the
  multi-item cart N-tuple stock decrement fix in `agent/tools.py::_place_order`.
  Drives the fix through the real `agent.tools.call_tool` public surface
  (`add_to_cart` × N → `place_order`), not by calling
  `inventory_manager.decrement_stock_for_cart` directly, since the bug lived
  in how `_place_order` called that function rather than in the function
  itself. Sandboxes `config` paths to `test_data/`, same pattern as
  `test_stock_reversal.py`. 6 tests: a two-product cart decrements each
  product by exactly its ordered quantity; a three-line-item cart across two
  products/variants; a single-item-cart regression check; multi-item with
  quantity > 1 per line; an insufficient-stock atomicity check (nothing
  decrements anywhere if one line item can't be fulfilled); and an
  order-persistence sanity check. Sanity-checked against the pre-fix code —
  3/6 correctly failed there (the three genuinely-multi-item cases).
  6/6 passed via both `pytest` and the standalone
  `python test_multi_item_checkout.py` runner. Independently re-run by
  Moeid on his own Windows venv (`pytest . -v` → full suite passed) and
  verified manually against the live bot.
- `test/test_backend.py` — end-to-end tool-call flow (search → add_to_cart →
  place_order → submit_payment_reference) plus the atomic stock-validation
  guarantee, against isolated test-only JSON files (never touches `data/`).
- `test/test_checkout_atomicity.py` — Issue #10 / bug-audit 3.1 coverage:
  the `_checkout_lock` fix in `agent/tools.py::_place_order`. Drives the fix
  through the real `agent.tools.call_tool` public surface (concurrent
  `place_order` calls), same sandboxing pattern as
  `test_multi_item_checkout.py`. 5 tests: 50 concurrent checkouts for the
  same user create exactly one order (both stock-constrained and
  stock-abundant variants); 50 concurrent checkouts across 5 different
  users each succeed independently; a failed checkout (forced stock
  shortfall) leaves the cart completely untouched; and a plain single-call
  regression check. Forces genuine interleaving via a monkeypatched
  `await asyncio.sleep(0)` in `cart_manager.get_cart` (plain
  `asyncio.gather()` alone doesn't reliably trigger this class of race —
  see "Checkout atomicity" above for why), and gives each concurrency test
  a fresh `_checkout_lock` instance via monkeypatch to avoid a
  pytest-asyncio cross-test event-loop-binding artifact (also detailed
  above). Sanity-checked against the pre-fix code — 3/5 correctly failed
  there (the 2 that passed regardless are the non-concurrency regression
  checks). 5/5 passed via both `pytest` and the standalone
  `python test\test_checkout_atomicity.py` runner. Independently re-run by
  Moeid on his own Windows venv (`python -m pytest . -v` → full suite
  67/67).
- `test/test_recovery_tool_call_ids.py` — Issue 2.1 coverage: unique
  recovery `tool_call_id`s. Fixed and merged 2026-10-29; see
  `docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the full write-up.
- `test/test_payment_resubmission.py` — Issue 4.2 coverage: payment-proof
  resubmission with `payment_proof_history` audit tracking. Fixed and
  merged 2026-10-29; see `docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the
  full write-up. **Updated for Issue #16 (Nov 2026):** `order_manager.get_order`/
  `get_latest_unpaid_order` are now async/locked — this file's calls to
  them are updated to `await` accordingly (no logic change).
- `test/test_async_file_io.py` — **new, Issue #16 / bug-audit 3.4 coverage**
  (`utils/file_io.py`): the event loop stays responsive during a slow write
  (the core bug this issue describes); a concurrent-readers-and-writers
  stress test (the hazard atomic writes exist to close, and the one that
  caught the Windows read-side `PermissionError` gap — see "Async file I/O"
  above); atomic-write unit tests (no leftover temp files, full
  replace-not-merge on overwrite, retry past a transient `PermissionError`,
  raise-and-clean-up if retries are exhausted); and a missing-file-returns-
  default regression check, both sync and async. 8/8 passed as part of the
  full 93/93 live suite (`python -m pytest . -v`, Moeid's Windows venv) and
  independently re-run via its standalone `python test_async_file_io.py`
  runner in the sandbox used to develop the fix.
- `test/test_inventory_read_retry.py` — **new, Issue #16 follow-up**
  (`utils/inventory_manager.py::_load_if_changed`): covers the gap found
  in post-merge review where this specific reader never actually used the
  read-side retry `file_io.py` was built to provide — see the "Follow-up
  fix" note under "Async file I/O" above. 7 tests: the core retry case at
  the `_load_if_changed()` level and again through the public
  `get_product_by_id`/`search_products`/`check_variant` call sites; a
  retries-exhausted regression check (a genuinely broken file must still
  raise, not be silently swallowed); and uncontended-read / mtime-reload
  regression checks. Sandboxes `config.INVENTORY_FILE` to `test_data/`,
  same pattern as the other isolated suites. Sanity-checked against the
  pre-follow-up code — 4/7 correctly failed there. Expected to land as
  part of `python -m pytest . -v` → 100/100 (93 existing + 7 new) once
  applied alongside the `inventory_manager.py` fix.

## GitHub issue tracker status
Bug tracking has moved from an informal `BUGS.md` to GitHub Issues on the
repo. **Note:** the GitHub issue/PR *numbers* don't line up with the
"Issue #N" numbering used in titles and in this doc — e.g. the issue titled
"Issue #4: No Stock Reversal on Order Rejection" is actually GitHub issue
**#5**, because PR #4 (the Issue #1 fix) consumed number 4 first. When
looking something up on GitHub, search by title/keyword rather than assuming
the URL number matches the "Issue #N" title number.

As of the last update:
- **Issue #1** (Missing Authorization for Admin Commands) — **fixed, merged**
  via PR #4. See "Admin authorization" above.
- **Issue #2** (`/reject` missing `await`, causes `TypeError` crash) —
  **closed**, fixed prior to #1.
- **Issue #3** (Broken leaked-JSON tool-call recovery, dead code in
  `agent.py`) — **fixed, merged** to `main` (Aug 2026). See "The
  tool-calling loop" above.
- **Issue #4** (No Stock Reversal on Order Rejection) — **fixed, merged** to
  `main` (Aug 2026). See "Stock reversal on rejection" above.
- **Issue #5** (Sparse Order Details in `/pending_orders`) — **fixed,
  merged** to `main` (Aug 2026). Branch `fix/issue-5-pending-orders-detail`,
  merge commit on `main`. Verified via `pytest . -v` (37/37, full suite, no
  regressions), the standalone `test_pending_orders_detail.py` runner
  (14/14), and manually against the live bot. See "Pending orders detail"
  above. A related but out-of-scope data hygiene issue was found and cleaned
  up alongside this (not part of the code fix itself): `data/orders.json`
  contained 50 synthetic test orders from a standalone `test_concurrency.py`
  run — see "Known environment gotchas" above.
- **Issue #6** (Multi-item Cart N-tuple Stock Decrement) — **fixed, merged**
  to `main` (Aug 2026). Branch `issue-6-Multi-item-Cart`. Verified via
  `pytest . -v` (55/55, full suite, no regressions), the standalone
  `test_multi_item_checkout.py` runner (6/6), and manually against the live
  bot. See "Stock integrity at checkout" above. Same underlying bug as
  bug-audit Issue 1.1 (see "Bug-audit fixes status" below) — discovered
  independently while testing the Issue #4 fix, then rediscovered by the
  separate bug-audit round; both numbering schemes point at the same fix.
- **Issue #10** (No atomic cart-consume across `_place_order`) — **fixed,
  merged** to `main` (Aug 2026). Branch
  `11-issue-10-no-atomic-cart-consume-across-_place_order`. Verified via
  `pytest . -v` (67/67, full suite, no regressions) and the standalone
  `test_checkout_atomicity.py` runner (5/5), both independently re-run by
  Moeid on his own Windows venv. Same underlying issue as bug-audit 3.1
  (see "Bug-audit fixes status" below) — this is the GitHub issue number
  for that fix. See "Checkout atomicity" above.
- **Issue #12** (Substring Color Matching Causes Inventory Drift on
  Rejection) — **fixed, closed.** Same underlying issue as bug-audit 3.2.
  See "Color matching" above.
- **Issue #13** (Hardcoded recovery `tool_call_id`s) — **fixed, closed**
  (2026-10-29). Same underlying issue as bug-audit 2.1. See "The
  tool-calling loop" above and `docs/backlog-issues-2.1-3.4-4.1-4.2.md`.
- **Issue #16** (Synchronous file I/O blocks the event loop) — **fixed,
  merged** to `main` (2026-11-06). Branch `fix/issue-16-async-file-io`.
  Verified via `python -m pytest . -v` (93/93, full suite, no regressions,
  across two live runs on Moeid's Windows venv) and the standalone
  `test_async_file_io.py`/`test_conversation_lock.py` runners. Same
  underlying issue as bug-audit 3.4 — this is the GitHub issue number for
  that fix. See "Async file I/O" above and
  `docs/backlog-issues-2.1-3.4-4.1-4.2.md`.
  **Follow-up (Nov 2026, same issue, landed as a small patch after the
  main merge):** `inventory_manager.py::_load_if_changed()` was found in
  post-merge review to still be using a bare `open()` call instead of the
  retry-protected `file_io.read_json_sync()` — see the "Follow-up fix"
  note under "Async file I/O" above and
  `test/test_inventory_read_retry.py`.

## Bug-audit fixes status (bug-audit-evaluation.md numbering)
A separate audit round (Aug 2026) — an AI model given the repo + this doc,
asked to find bugs, cross-checked in `bug-audit-evaluation.md` — used its
own "Issue N.N" numbering (e.g. "1.2", "3.3") that is **unrelated** to the
GitHub issue numbers above; don't conflate the two. None of these are filed
as GitHub issues yet, except where noted below (2.1, 3.2, 3.4 have since
been filed and fixed as GitHub Issues #13, #12, and #16 respectively).
Agreed priority order and current status:

1. **Issue 1.2** (narration guard `return` instead of `continue`) —
   **fixed, merged** to `main` (Aug 2026), HIGHEST PRIORITY per the audit
   evaluation. See "The tool-calling loop" above.
2. **Issue 1.3** (no admin notification for typed `submit_payment_reference`
   calls) — **fixed, merged** to `main` (Aug 2026), High priority. See "The
   tool-calling loop" and "Payment flow" above.
3. **Issue 1.1** (multi-item cart N× stock decrement) — **fixed, merged** to
   `main` (Aug 2026), tracked as GitHub Issue #6. Already documented above
   under "Stock integrity at checkout" as a known bug before this round; the
   audit rediscovered it rather than found something new, but this fix
   closes both. See "Stock integrity at checkout" above.
4. **Issue 3.3** (`conversation_manager` has no `asyncio.Lock`, unlike
   `cart_manager`/`order_manager`) — **fixed, merged** to `main` (Aug 2026).
   Branch `issue-3.3-conversation-lock`. Fixed with Approach A (single
   global `asyncio.Lock`, matching `cart_manager.py`/`order_manager.py`'s
   existing pattern) — a per-user dict of locks (Approach B) was
   considered and explicitly rejected as unnecessary new architectural
   surface for this project's scale. Verified via `pytest . -v` (56/56,
   full suite, no regressions on a branch containing `main` + only this
   fix), the standalone `test_conversation_lock.py` runner (7/7), and
   manually against the live bot. See "Conversation history locking"
   above.
5. **Issue 3.1** (no atomic cart-consume across `_place_order` — read/
   validate/decrement/create-order/clear-cart isn't one locked operation) —
   **fixed, merged** to `main` (Aug 2026), tracked as GitHub Issue #10.
   Implemented as a single global `_checkout_lock` in `agent/tools.py`
   (Approach D — chosen over an atomic-consume-then-restore design, which
   was rejected after identifying a new data-loss race it would introduce
   against concurrent `add_to_cart` calls; see "Checkout atomicity" above
   for the full comparison of all three approaches considered). Verified
   via `pytest . -v` (67/67, full suite, no regressions), the standalone
   `test_checkout_atomicity.py` runner (5/5), both independently re-run by
   Moeid on his own Windows venv. See "Checkout atomicity" above.
6. **Issue 3.2** (substring color matching can cause inventory drift on
   rejection) — **fixed, merged** to `main` (Aug 2026), tracked as GitHub
   Issue #12. Already documented above under "Stock reversal on rejection"
   as a known limitation before this round — the audit rediscovered it
   rather than found something new, but this fix closes it. Fixed with
   Approach A (exact case-insensitive color matching, replacing substring
   containment, in all three match sites in `inventory_manager.py`) — a
   schema change to record a per-variant decrement breakdown (so restore
   could replay the exact original split) was considered and rejected as
   touching the order-placement write path and requiring a legacy-order
   fallback for meaningfully more risk than the root cause required.
   Verified via `pytest . -v` (77/77, full suite, no regressions), the
   standalone `test/test_color_exact_match.py` runner (10/10), independently
   re-run by Moeid on his own Windows venv. See "Color matching" above.
7. **Issue 2.1** (hardcoded recovery `tool_call_id`s could collide across
   multiple recovery events in one conversation) — **fixed, merged** to
   `main` (2026-10-29), tracked as GitHub Issue #13. Unique IDs via
   `_new_recovery_tool_call_id()` (`uuid.uuid4().hex`). See "The
   tool-calling loop" above and `docs/backlog-issues-2.1-3.4-4.1-4.2.md`.
8. **Issue 4.1** (narration guard's negative-phrase filter didn't cover
   "can't"/"unable to") — **fixed, merged** to `main` (2026-10-29). See
   `docs/backlog-issues-2.1-3.4-4.1-4.2.md` for the full write-up.
9. **Issue 4.2** (can't resubmit payment proof once an order is
   `awaiting_review`) — **fixed, merged** to `main` (2026-10-29), Option B
   (explicit `payment_proof_history` audit trail). See "Payment flow" above
   and `docs/backlog-issues-2.1-3.4-4.1-4.2.md`.
10. **Issue 3.4** (synchronous file I/O blocks the event loop) — **fixed,
    merged** to `main` (2026-11-06), tracked as GitHub Issue #16, with a
    small follow-up patch (Nov 2026) for an unprotected reader
    (`inventory_manager._load_if_changed`) found in post-merge review. See
    "Async file I/O" above and `docs/backlog-issues-2.1-3.4-4.1-4.2.md`.

**All ten items from the original bug-audit-evaluation.md are now closed.**

## Not built yet
Rate limiting per user, fuzzy product search, product images, order status
beyond confirmed/rejected (e.g. shipped/delivered), any real payment gateway
integration, LangGraph or similar orchestration (deliberately not adopted —
current failures have all been model-reliability issues, not
orchestration-complexity issues, so a framework wouldn't have prevented any
of today's bugs).

**Prioritized order for tackling the above** (decided 2026-11-08, Moeid):
1. **Rate limiting per user** — highest priority. Unlike the rest of this
   list, this is an active reliability/cost risk today, not just a missing
   feature: `agent/agent.py` currently lets any single user burn unlimited
   LLM calls, and Groq's free tier caps at 100,000 tokens/day (see "LLM
   provider" above) — one chatty or malicious user can exhaust the whole
   shop's daily quota and lock out every other customer. Needs a decision
   on where the limiter lives (`agent/tools.py` vs `agent/agent.py` vs a
   new `utils/rate_limiter.py`), per-user vs. global budget, and failure
   mode (friendly message vs. silent drop).
2. **Order status beyond confirmed/rejected** (e.g. shipped/delivered) —
   extends the already-hardened order lifecycle (`pending_confirmation` ->
   `awaiting_review` -> `confirmed`/`rejected`) that Issues #4, #10, 3.1,
   3.2, and 4.2 made atomic and correct. Real operational gap for a live
   store (sellers can't tell a customer "shipped" today), and reuses
   existing `order_manager`/admin-command machinery rather than new infra.
3. **Fuzzy product search** — `search_products` is currently plain
   substring match (documented "by design" limitation, see "By Design" in
   the README). Real UX papercut, but the LLM's `_resolve_product_id`
   fallback already compensates reasonably well, so lower urgency than 1-2.
4. **Product images** — presentational only, no correctness/reliability
   payoff. Would need a schema change (`image_url` field on products),
   which this project's conventions treat as higher-risk (see the Issue #5
   price-snapshot-deferral precedent above).
5. **Real payment gateway integration** — biggest scope and risk (money
   changes hands), and explicitly "by design" deferred — the project
   intentionally relies on manual screenshot/reference review for a
   single-seller shop. Arguably out of scope unless the business scales
   past manual review; last on the list, possibly indefinitely.

## Working conventions
- **Whole-file replacement over diff-patching.** Patches (`git am`, inline
  diffs) have repeatedly failed to apply cleanly against a working tree with
  any local drift — CRLF line endings on Windows, a prior partial patch
  attempt, etc. When making a multi-line change, prefer regenerating and
  handing over the full file content over a patch/diff.
- **Prompt-level fixes before code-level ones** for model-reliability issues
  — see "Prompt rules" above.
- **Never trust the LLM to provide `user_id`/`username`** — always injected
  from the real Telegram context in `agent/tools.py`'s `call_tool()`, never
  parsed from model tool-call arguments.
- **Minimal, structurally-scoped fixes over opportunistic refactors.** When
  a bug fix could be generalized (e.g. extracting a shared helper for two
  near-identical recovery blocks, as considered for Issue #3), prefer the
  smaller fix that doesn't touch already-working, already-tested code paths
  unless the dedup is actually required to solve the issue at hand. Reduces
  regression surface and keeps PRs reviewable against the issue they claim
  to fix. Issue #5 followed this too: price-at-purchase snapshotting was
  considered and deliberately deferred rather than folded in, since it would
  have touched the order-placement path instead of staying display-only.
  Issue 1.2 is the clearest example yet: the entire fix was a one-line
  `return reply, events` -> `continue` swap, with no refactor of the
  surrounding guard logic even though it would have been tempting to
  "clean up" the narration-guard block while in there. Issue #16 is a
  partial exception worth noting: the scope genuinely grew beyond the
  original backlog proposal (plain `asyncio.to_thread()`) once the atomic-
  write requirement and the two read-side/lock-binding issues surfaced
  through actual testing — but the *shape* of the fix stayed minimal
  throughout (one new shared helper module, no changes to any manager's
  locking design or public behavior beyond the two newly-async
  `order_manager` reads that correctness required).
- **Unrelated local changes get separated out, not bundled into a feature
  branch.** If `git status`/`git diff` on a feature branch shows edits
  unrelated to the issue being fixed (e.g. stray type-checker suppression
  comments), stash just those files, commit the actual fix cleanly, then pop
  the stash on `main` (or wherever they actually belong) as their own
  separate commit. See the Issue #5 entry in "Known environment gotchas."
- Verification command after significant changes:
  ```bash
  python -c "from agent import tools, prompts, agent; from utils import order_manager, inventory_manager, file_io; from handlers import admin_handlers, user_handlers; print('all imports OK')"
  ```
- **Git branching/merge workflow for issue fixes** (established during the
  Issue #4 fix, repeated for Issues #3, #5, #10, #16, and — under the
  separate bug-audit "1.N" numbering, see "Bug-audit fixes status" above —
  1.2, 1.3, 2.1, 3.2, 4.1, and 4.2):
  1. `git checkout -b fix/issue-N-short-description` off `main` — never
     commit directly to `main`.
  2. Implement the fix, add tests, run the full suite (`pytest . -v`).
  3. Sanity-check new tests against the pre-fix code when practical (e.g.
     temporarily restore the old file and confirm the new tests fail there)
     — confirms the tests actually exercise the bug rather than passing
     vacuously. Done for Issue #3 (`test_leaked_json_recovery.py`, 4/8
     failed on the old code), Issue 1.2 (`test_narration_guard_loop.py`,
     5/7 failed on the old code), and Issue 1.3
     (`test_payment_reference_notification.py`, 3/5 failed on the old
     code).
  4. Manually verify against a live bot before considering it done, when a
     live bot is available — unit tests alone didn't catch everything (e.g.
     the idempotency requirement for `/reject` was verified live, not just
     in `test_stock_reversal.py`; Issue #5's chunking and formatting were
     also confirmed live). If the bot isn't live at fix time (as for Issue
     #3 originally), rely on sandboxed/mocked tests instead and note the gap
     — don't fabricate or assume a live-verification result that didn't
     happen. **Issue #16 is the clearest case yet of this step earning its
     keep at the unit-test level, not just the live-bot level:** the first
     `python -m pytest . -v` run against the real Windows venv caught a
     genuine Windows-only read-side race, a missed `await` in
     `test_stock_reversal.py`, and a lock-binding artifact in
     `test_conversation_lock.py` — none of which were visible from
     sandboxed review/testing alone (no real filesystem contention, no
     Windows-specific `PermissionError` semantics, and no genuinely
     interleaved `asyncio.gather()` since the sandbox's own smoke tests
     didn't reproduce the exact pytest-asyncio per-test-loop conditions).
     Always run the real suite on the real target OS before calling a fix
     done, even when a sandboxed review/test pass already looked clean.
  5. Generate a `.patch` file for review: `git diff main...fix/issue-N-...
     > issue-N.patch`. Verify it applies cleanly to a fresh checkout of
     `main` (`git apply --check`) before handing it over.
  6. Apply via `git apply path\to\issue-N.patch`. **Remember this only
     modifies the working directory — it does not commit.** Always run `git
     status` immediately after to confirm the files actually changed, then
     explicitly `git add` + `git commit`. (`git am` is an alternative that
     does commit directly, but has repeatedly failed on Windows against a
     working tree with any local drift — see "Known environment gotchas.")
  7. Before committing, check `git status`/`git diff` for **anything
     unrelated** sitting in the working tree (stray local edits, leftover
     patch files, duplicate/misnamed test files) and set it aside (`git
     stash push <file> ...` if it's real work worth keeping elsewhere, `git
     checkout -- <file>` or delete if it's not) so the commit only contains
     the issue's actual fix.
  8. Merge into `main` with `git merge --no-ff fix/issue-N-...` (not a
     fast-forward) so the merge shows up as its own point in history, same
     as PR #4's merge commit for Issue #1.
  9. **Before the final commit/push, check `git status` for unintended
     `data/*.json` changes** from manual bot testing (see "Known environment
     gotchas" above) — `git checkout -- data/` to discard them if so. If
     stale/test data is discovered in real `data/*.json` (as happened with
     Issue #5), clean it up as its own separate, clearly-labeled commit —
     don't fold a data cleanup into a code-fix commit. **Issue #16 hit this
     same check** — `data/inventory.json`/`data/orders.json` showed as
     modified from suite/manual-testing runs during development, discarded
     with `git checkout -- data/inventory.json data/orders.json` before
     staging the actual fix.
  10. `git push origin main`, then delete the merged local branch
      (`git branch -d fix/issue-N-...`) and the remote branch if it was ever
      pushed (`git push origin --delete fix/issue-N-...` — harmless "remote
      ref does not exist" error if it was only ever local, as happened for
      Issue #5).
