# Shopmate Agent — Remaining Backlog Issues

Source: `bug-audit-evaluation.md` / `original-audit-report.md`, cross-checked against the current repository state. These are the four items still open after Issues 1.2, 1.3, 1.1, 3.3, 3.1, and 3.2 were fixed and merged. All four are classified **low priority** by the original audit — none are silent-failure or data-corruption bugs like the ones already fixed; they're either narrow edge cases, minor UX gaps, or scale-related concerns that don't bite at the project's current single-seller size.

> **Status update (2026-11-06):** All four items are now fixed and merged into `main`. Issues **2.1, 4.1, and 4.2** landed 2026-10-29; **Issue 3.4** (tracked as GitHub Issue #16) landed 2026-11-06. The sections below are preserved for history; each fixed issue is marked with its resolution.

---

## Issue 2.1 — Hardcoded recovery `tool_call_id`s

> **✅ FIXED (2026-10-29).** Recovery IDs are now unique per event via `_new_recovery_tool_call_id()` (`uuid.uuid4().hex`), applied at both the native-tag and leaked-JSON recovery paths. Covered by `test/test_recovery_tool_call_ids.py`.

**Location:** `agent/agent.py`, in both malformed-tool-call recovery paths.

**Current code:**
```python
# Native-tag (BadRequestError) recovery path
messages.append({
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "recovered_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }],
})
messages.append({
    "role": "tool",
    "tool_call_id": "recovered_1",
    ...
})
```
The leaked-JSON recovery path uses the same pattern with the literal string `"recovered_json_1"`.

**Root cause:** When the agent recovers a malformed tool call (either from an API-level `BadRequestError` with a native `<function=...>` tag, or from plain-text leaked JSON), it has to synthesize a fake `tool_calls` entry so the conversation history stays shaped like a normal OpenAI-style tool-call turn. Rather than generating a unique ID for that synthetic call, the code just reuses the same static string every time recovery happens.

**Why it matters:** The OpenAI-compatible API (and by extension Groq/Ollama) expects every `tool_call_id` within a single conversation payload sent to the model to be unique — it's used to correlate an `assistant` turn's `tool_calls` entries with the corresponding `tool` role results. If recovery fires more than once in the same persisted conversation history (e.g. a customer triggers the native-tag recovery once this session, then triggers it again — or triggers both the native-tag and leaked-JSON paths — before enough turns have passed for `conversation_manager`'s trimming to drop the earlier one), the message history sent on a later API call will contain two messages with `tool_call_id: "recovered_1"`. Depending on the provider's strictness, this can produce a `BadRequestError` at the API level — ironically, the exact class of error the recovery logic exists to work around — which would crash that turn's agent loop for the user.

**Severity:** Medium in theory, but rare in practice. It requires the *same user* to trigger recovery more than once within the `MAX_HISTORY_MESSAGES` window (20 messages by default) before the older recovered turn ages out of context. Weaker local models (llama3.2) are the ones most likely to trigger recovery at all, and repeated triggering within one 20-message window is plausible during a rough session but not guaranteed.

**Proposed fix:** Generate a unique ID per recovery event instead of a static string:
```python
import uuid
...
call_id = f"recovered_{uuid.uuid4().hex}"
```
Apply this at both recovery sites (native-tag and leaked-JSON), replacing `"recovered_1"` and `"recovered_json_1"` respectively. This is a minimal, structurally-scoped change — no behavior change to anything else, no new dependency (`uuid` is stdlib).

**Effort / risk:** Very low. A few-line change in one file, no schema or API surface impact. A dedicated test (mirroring the project's existing pattern) would trigger recovery twice in one simulated conversation and assert the two generated IDs are non-equal, plus a regression check that a single recovery still works end-to-end.

---

## Issue 3.4 — Synchronous file I/O blocks the event loop

> **✅ FIXED (2026-11-06), tracked as GitHub Issue #16.** Implemented as Option A from this doc (`asyncio.to_thread()`, no new dependency), extended with an atomic-write layer that Option A's original text didn't call out but that turned out to be required for correctness — see "What changed from the original proposal" below. Covered by `test/test_async_file_io.py` plus `await`-only updates to the call sites and tests that used the two `order_manager` reads this fix made async. Full suite verified at 93/93 (`python -m pytest . -v`) on Moeid's Windows venv, no regressions. **Follow-up (Nov 2026):** post-merge review found `inventory_manager.py::_load_if_changed()` — the exact reader the read-side retry below was written for — had never actually been wired up to use it (still a bare `open()` call). Fixed as a small separate patch; see `docs/CLAUDE.md`'s "Async file I/O" section and `test/test_inventory_read_retry.py` for the full write-up.

**Location:** All of `utils/cart_manager.py`, `utils/order_manager.py`, `utils/conversation_manager.py`, and `utils/inventory_manager.py`.

**Original code:** Every read/write path in these modules used plain blocking calls inside `async def` functions, e.g.:
```python
def _save(carts: dict[str, list[dict]]):
    with open(config.CARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(carts, f, ensure_ascii=False, indent=2)
```
This was called from inside `async def set_cart(...)` while holding `_carts_lock` — the disk write itself was synchronous.

**Root cause:** Python's `asyncio` event loop is single-threaded; when a coroutine calls a blocking function like `open()`/`json.dump()`, control does not return to the event loop until that call completes. Every other coroutine scheduled on the loop — other users' Telegram updates, other in-flight tool calls — has to wait.

**Why it mattered:** In `aiogram`'s polling model, all incoming Telegram updates and the bot's own outgoing calls (typing indicators, message sends) run on the same event loop as the business logic. A slow disk write (or a moment of disk contention) during, say, `order_manager.place_order()` would stall Telegram's polling loop for every other concurrent user for the duration of that write, not just the user placing the order. At the file sizes and write frequency this project had, individual writes were fast enough (milliseconds) that this was unlikely to be user-noticeable, but it didn't scale gracefully — see "Severity" below for why this stayed low-priority until now.

**Severity (at the time this was filed):** Low at current scale. Explicitly judged "acceptable" in `CLAUDE.md` given the project's small-scale, single-seller, JSON-file-by-design architecture. A scalability/robustness concern, not a correctness bug — nothing was lost or corrupted, requests just queued up.

**What changed from the original proposal.** This doc's original text (quoted below for history) assumed a plain `asyncio.to_thread()` wrap was sufficient and explicitly said "nothing is lost or corrupted" under that design. That held only because every read and write ran on the single event-loop thread before this fix — a read could never physically overlap a write. Once writes move to a worker thread, that stops being true for any file with an *unlocked* reader: `inventory_manager`'s `get_product_by_id`/`search_products`/`check_variant` (built on `_load_if_changed`) are deliberately synchronous and unlocked (see that module's own docstring), and `order_manager.get_order`/`get_latest_unpaid_order` were plain synchronous unlocked reads before this fix. A naive `to_thread()` wrap of the writes alone would have let those unlocked reads observe a half-written (torn) file mid-write — a new hazard this fix introduces by moving work off the loop thread, not one Option A's original text accounted for. Implementation therefore added one piece beyond the original proposal:

- **`utils/file_io.py` (new):** `read_json`/`write_json_atomic` (async, offload to `asyncio.to_thread`) and their `_sync` counterparts. Writes go to a temp file in the same directory, then `os.replace()` into place — any reader sees either the fully-old or fully-new file, never a torn one. `order_manager.get_order` and `get_latest_unpaid_order` were additionally converted to locked, async functions (mirroring `get_all_orders`, which was already both), since they're the only unlocked readers outside `inventory_manager` that needed closing this way.

A second, Windows-specific refinement was needed after an initial live `pytest . -v` run: even with the write atomic, a concurrent `open(path, "r")` could transiently raise `PermissionError` while another thread's `os.replace()` was in flight (observed directly: 3 failures out of 80 concurrent operations in the new stress test, `test/test_async_file_io.py::test_concurrent_readers_and_writers_never_see_a_torn_file`, on a Windows venv). This is a sharing-violation window, not data corruption — the atomic write was never actually torn — so the fix was a short, bounded retry on the read side, mirroring the retry the write side already had around `os.replace()`. Confirmed resolved on a second live run (93/93).

A live test run after the initial patch also surfaced two issues unrelated to `file_io.py` itself, both now fixed: `test/test_stock_reversal.py` had two direct (unawaited) calls to the newly-async `order_manager.get_order()` that were missed during the initial patch — a straightforward oversight, fixed by adding `await`. Separately, `test/test_conversation_lock.py`'s two genuinely-concurrent tests (driving real `asyncio.gather()` contention on `_history_lock`) started failing with "is bound to a different event loop" — not a regression, but the *first* time `_history_lock` was ever genuinely contended across concurrent tasks: before this fix, the locked sections were pure sync code with no real `await` inside, so `asyncio.Lock`'s fast uncontended path never yielded and never bound itself to a loop. Post-fix, `asyncio.to_thread()` inside the lock is a real yield point, so concurrency became real for the first time — which tripped the exact pytest-asyncio cross-test event-loop-binding artifact already documented and worked around for `_checkout_lock` in `test/test_checkout_atomicity.py` (Issue 3.1 / GitHub #10). Fixed the same way: the two affected tests take `monkeypatch` and reset `_history_lock` to a fresh instance at the start of the test.

**Original proposed fix (Option A — no new dependency), preserved for history:**
```python
async def _save(carts: dict[str, list[dict]]):
    await asyncio.to_thread(_save_sync, carts)
```
This needed to be threaded through the sync/async split each manager already had (the project already separates "sync core" helpers from "locked async wrapper" functions for `conversation_manager.py`, so the pattern was established — this extended that same shape to actual thread offloading, not just lock management). As noted above, implementation also required the atomic-write layer and the two read-side fixes this section describes, which the original proposal didn't anticipate.

An alternative (rejected by the original audit, and still not adopted) was `aiofiles` for native async file I/O — cleaner code, but adds a new dependency for a benefit the project doesn't currently need; `utils/file_io.py` covers the same need with `asyncio.to_thread()` and the stdlib.

**Effort / risk (actual):** Medium-to-high — broader than the original "Medium" estimate, since it touched every manager module (as expected) plus `agent/tools.py` and `handlers/user_handlers.py` (await sites for the two newly-async `order_manager` reads) plus four existing test files, and surfaced two real issues only visible under a live `pytest . -v` run rather than at review time (the missed `await` in `test_stock_reversal.py`, and the `_history_lock` cross-test binding artifact in `test_conversation_lock.py`). Full suite verified at 93/93 after both rounds of fixes, no regressions.

---

## Issue 4.1 — Narration guard's negative-phrase filter is incomplete

> **✅ FIXED (2026-10-29).** The `is_negative` filter was expanded with the backlog-proposed `can't / cannot / unable to` constructions × `add / place / confirm`, plus `not able to`. The mirrored logic in `test/test_narration_guard.py` was updated in lockstep, with new overlap-case asserts (commitment phrase + refusal in one reply) that fail on the pre-fix wordlist. Residual known gap: `couldn't` / `wasn't able to` phrasings are not yet covered — to be refined from real observed model output if seen in practice.

**Location:** `agent/agent.py`, inside the narration guard, in the `is_negative` check.

**Current code:**
```python
is_negative = any(neg in reply_lower for neg in [
    "not added", "haven't added", "didn't add",
    "not placed", "haven't placed", "didn't place",
    "not confirmed", "haven't confirmed", "didn't confirm"
])
```

**Root cause:** The narration guard exists to catch the model *falsely claiming* an action succeeded without calling the tool (e.g., "I've added that to your cart!" with no `add_to_cart` call). To avoid punishing the model for legitimately and correctly declining an action, the guard first checks whether the reply is phrased negatively. The current negative-phrase list only recognizes "not X" / "haven't X" / "didn't X" constructions — it doesn't recognize other common ways a model might correctly say it *can't* or *won't* do something, such as "I can't add that right now" or "unable to place the order."

**Why it matters:** If the model says something like *"I can't add that to your cart — it's out of stock,"* the reply contains the phrase **"cart"** near commitment-adjacent language, but more importantly, this specific phrasing wouldn't trip `commitment_phrases` at all (since "can't add" isn't in the `commitment_phrases` list either) — so today this exact example is actually safe. The real risk case is a phrasing that *does* overlap with a commitment phrase while still being a negative statement the current filter misses, e.g. combining "order" language ambiguously. Concretely, imagine the model says: *"I'm unable to place the order since the item's sold out."* — this contains **"order"** but not a `commitment_phrases` match either (the commitment list requires "order is placed"/"order placed"/etc., not just the word "order"), so this too currently passes safely by luck of the specific wordlists chosen, rather than by a robust general filter. The gap is real but narrower in practice than it first appears, precisely because the two lists (`commitment_phrases` and `is_negative`) are independently curated and don't perfectly compose — a future new commitment phrase or a differently-phrased model refusal could still fall through the gap and cause a false-positive retry (the guard incorrectly thinks the model made a false claim, injects an unnecessary correction message, and burns a round of `MAX_TOOL_ROUNDS` for no reason).

**Severity:** Low. Worst case is a wasted retry round (annoying, consumes budget, but not user-facing incorrect data) rather than a false success claim reaching the customer. Local/weaker models are the most likely to produce phrasing this list doesn't anticipate.

**Proposed fix:** Expand the `is_negative` phrase list to include broader negation patterns:
```python
is_negative = any(neg in reply_lower for neg in [
    "not added", "haven't added", "didn't add",
    "not placed", "haven't placed", "didn't place",
    "not confirmed", "haven't confirmed", "didn't confirm",
    "can't add", "cannot add", "unable to add",
    "can't place", "cannot place", "unable to place",
    "can't confirm", "cannot confirm", "unable to confirm",
])
```
The exact wording list is a judgment call and could be refined further (e.g. "won't be able to", "not able to") based on real observed model outputs rather than guessing exhaustively up front.

**Effort / risk:** Very low — a list expansion in one file, no structural change. Best verified with unit tests against `check_narration()`-style logic (mirroring the existing `test_narration_guard.py` unit-test style) covering the new phrases, plus a regression check that the existing positive/negative cases are unaffected.

---

## Issue 4.2 — Can't resubmit payment proof once an order is `awaiting_review`

> **✅ FIXED (2026-10-29) — Option B chosen, with one deliberate deviation.** Implemented: a per-order `payment_proof_history` list (entries of `{proof, submitted_at}`) appended on every submission via `setdefault` — no migration needed for pre-existing orders; the order's `payment_proof` still holds the latest value; both admin notification paths (typed reference and screenshot) append a ⚠️ RESUBMISSION flag when history length > 1; the customer gets an explicit "replaces the previous one" confirmation on resubmission. **Deviation from the text below:** the backlog's Option B said to "keep the status filter narrow," but taken literally that leaves resubmission unreachable (nothing to track). The lookup therefore also matches `awaiting_review` — as any working fix must; Option B's actual differentiator (audit trail over silent overwrite) is fully implemented. Covered by `test/test_payment_resubmission.py`; validated end-to-end live on 2026-10-29 (reference resubmission, screenshot resubmission with admin flag, `/pending_orders` reflection, `/confirm` after resubmission).

**Location:** `utils/order_manager.py::get_latest_unpaid_order()`.

**Current code:**
```python
def get_latest_unpaid_order(user_id: int) -> dict | None:
    matches = [
        o for o in _load(config.ORDERS_FILE)
        if o["user_id"] == user_id and o["status"] == "pending_confirmation"
    ]
    return matches[-1] if matches else None
```

**Root cause:** This function is what both `submit_payment_reference` (in `agent/tools.py`) and the photo-screenshot handler (in `handlers/user_handlers.py::handle_payment_screenshot`) use to find "the order this customer is currently trying to pay for." It only matches orders still in `pending_confirmation` status. As soon as a payment proof (screenshot or typed reference) is successfully attached, `order_manager.attach_payment_proof()` moves the order to `awaiting_review` — at which point this lookup function stops finding it entirely.

**Why it matters:** If a customer submits the wrong screenshot, a mistyped transaction reference, or simply wants to send a clearer proof, they currently have no way to do so through the bot. Once the order is `awaiting_review`:
- Typing a new reference: `submit_payment_reference` calls `get_latest_unpaid_order`, gets `None`, and returns `{"error": "no order awaiting payment for this customer"}` — the correction is silently rejected from the tool's perspective (the LLM would need to explain this to the customer, and the customer's actual correction never gets recorded).
- Sending a new screenshot: `handle_payment_screenshot` in `user_handlers.py` calls the same function, gets `None`, and replies *"I don't see an order waiting on payment for you right now..."* — which is confusing to a customer who knows they do have an order pending, just already-submitted.

The seller's only real recourse today is to notice the mismatch manually (e.g. during `/pending_orders` review) and resolve it out-of-band (asking the customer to message differently, or handling it manually outside the bot).

**Severity:** Low-to-moderate UX gap, not a data-integrity issue — no stock or order data is corrupted, but it's a real, reachable customer-facing dead end that could require manual seller intervention to unblock.

**Proposed fix — two viable approaches, worth deciding between explicitly rather than picking one by default:**

**Option A — Widen the status filter (simplest):**
```python
def get_latest_unpaid_order(user_id: int) -> dict | None:
    matches = [
        o for o in _load(config.ORDERS_FILE)
        if o["user_id"] == user_id and o["status"] in ("pending_confirmation", "awaiting_review")
    ]
    return matches[-1] if matches else None
```
And in `attach_payment_proof`, allow overwriting an existing `payment_proof` value rather than only setting it once. This lets a customer's new screenshot or reference simply replace the old one, keeping status at (or resetting to) `awaiting_review`.
- *Pros:* Minimal, single-function change, matches the project's "minimal, structurally-scoped fixes" convention.
- *Cons:* Silently overwrites the old proof with no audit trail of the correction having happened — if the seller already glanced at the first (wrong) screenshot, there's no record that a second one came in to replace it, beyond noticing the `payment_proof` field changed.

**Option B — Explicit resubmission tracking:**
Keep the status filter narrow, but add a small history/log of proof submissions per order (e.g. a `payment_proof_history` list field appended to rather than overwritten) alongside the single "current" `payment_proof` field, and have admin notifications flag when a *resubmission* (not a first submission) occurs so the seller knows to look again.
- *Pros:* Preserves an audit trail, gives the seller an explicit signal that something changed rather than silently updating a field they may have already acted on.
- *Cons:* Touches the order schema (adding a new field), which the project's conventions (see the Issue #5 "price-at-purchase" discussion in `CLAUDE.md`) treat as higher-risk and something to weigh carefully before doing — schema changes need a decision on how to handle pre-existing orders that lack the new field.

**Recommendation:** Given the project's stated preference for minimal, schema-avoiding fixes (the same reasoning that shaped the Issue 3.2 color-matching fix and the Issue #5 price-snapshot deferral), **Option A** is more consistent with established conventions, with the audit-trail gap explicitly accepted as a known trade-off rather than silently overlooked.

**Effort / risk:** Low-to-medium depending on which option is chosen. Option A is a small, contained change to `order_manager.py` (touching `get_latest_unpaid_order` and `attach_payment_proof`); Option B is meaningfully larger since it touches the order schema and both admin-notification code paths. Either way, this would need new tests covering: submitting a second reference/screenshot after the first is already `awaiting_review`, confirming the update is reflected in `/pending_orders`, and a regression check that first-time submission is unaffected.

---

## Summary table

| Issue | Area | Severity | Effort | Touches | Status |
|---|---|---|---|---|---|
| 2.1 | Recovery `tool_call_id`s | Medium (rare trigger) | Very low | `agent/agent.py` only | ✅ Fixed (2026-10-29) |
| 3.4 | Sync file I/O | Low (current scale) | Medium → actually Medium-high (atomic writes + two read-side fixes required) | All 4 `utils/*_manager.py` files, new `utils/file_io.py`, `agent/tools.py`, `handlers/user_handlers.py`, 4 existing test files, 1 new test file | ✅ Fixed (2026-11-06, GitHub Issue #16) |
| 4.1 | Narration guard negative-phrase gap | Low | Very low | `agent/agent.py` only | ✅ Fixed (2026-10-29) |
| 4.2 | Can't resubmit payment proof | Low-to-moderate UX | Low (Option A) / Medium (Option B) | `utils/order_manager.py`, `handlers/user_handlers.py` | ✅ Fixed (2026-10-29, Option B) |

**All four backlog items are now closed.** See `docs/CLAUDE.md`'s "Stock integrity" / "The tool-calling loop" / "Payment flow" sections for 2.1/4.1/4.2, and its new "Async file I/O" section for 3.4 / Issue #16.
