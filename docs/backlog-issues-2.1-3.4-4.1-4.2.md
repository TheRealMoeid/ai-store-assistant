# Shopmate Agent — Remaining Backlog Issues

Source: `bug-audit-evaluation.md` / `original-audit-report.md`, cross-checked against the current repository state. These are the four items still open after Issues 1.2, 1.3, 1.1, 3.3, 3.1, and 3.2 were fixed and merged. All four are classified **low priority** by the original audit — none are silent-failure or data-corruption bugs like the ones already fixed; they're either narrow edge cases, minor UX gaps, or scale-related concerns that don't bite at the project's current single-seller size.

---

## Issue 2.1 — Hardcoded recovery `tool_call_id`s

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

**Location:** All of `utils/cart_manager.py`, `utils/order_manager.py`, `utils/conversation_manager.py`, and `utils/inventory_manager.py`.

**Current code:** Every read/write path in these modules uses plain blocking calls inside `async def` functions, e.g.:
```python
def _save(carts: dict[str, list[dict]]):
    with open(config.CARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(carts, f, ensure_ascii=False, indent=2)
```
This is called from inside `async def set_cart(...)` while holding `_carts_lock` — the disk write itself is synchronous.

**Root cause:** Python's `asyncio` event loop is single-threaded; when a coroutine calls a blocking function like `open()`/`json.dump()`, control does not return to the event loop until that call completes. Every other coroutine scheduled on the loop — other users' Telegram updates, other in-flight tool calls — has to wait.

**Why it matters:** In `aiogram`'s polling model, all incoming Telegram updates and the bot's own outgoing calls (typing indicators, message sends) run on the same event loop as the business logic. A slow disk write (or a moment of disk contention) during, say, `order_manager.place_order()` would stall Telegram's polling loop for every other concurrent user for the duration of that write, not just the user placing the order. At the file sizes and write frequency this project has today, individual writes are fast enough (milliseconds) that this is unlikely to be user-noticeable, but it doesn't scale gracefully if inventory/orders/conversation files grow substantially, or if the underlying storage is ever slower (e.g. a networked or throttled disk in a hosted environment).

**Severity:** Low at current scale. Explicitly judged "acceptable" in `CLAUDE.md` given the project's small-scale, single-seller, JSON-file-by-design architecture. This is a scalability/robustness concern, not a correctness bug — nothing is lost or corrupted, requests just queue up.

**Proposed fix (per the original audit, Option A — no new dependency):** Wrap each blocking file operation in `asyncio.to_thread()`, which runs it in a background thread pool and lets the event loop continue servicing other coroutines while the write/read completes:
```python
async def _save(carts: dict[str, list[dict]]):
    await asyncio.to_thread(_save_sync, carts)
```
This would need to be threaded through the sync/async split each manager already has (the project already separates "sync core" helpers from "locked async wrapper" functions for `conversation_manager.py`, so the pattern is established — this would extend that same shape to actual thread offloading, not just lock management).

An alternative (rejected by the original audit) is adopting `aiofiles` for native async file I/O — cleaner code, but adds a new dependency for a benefit the project doesn't currently need.

**Effort / risk:** Medium. Touches every manager module across the codebase (cart, order, conversation, inventory), so it's a wider-reaching change than the other three backlog items even though each individual edit is mechanical. Because every one of these modules already holds an `asyncio.Lock` around its critical section, care would be needed to make sure `asyncio.to_thread()` calls happen *inside* the lock (to preserve the existing mutual-exclusion guarantees) rather than accidentally letting two threads race on the same file. Would need the full test suite re-run afterward given the breadth of files touched, even though no single change is complex.

---

## Issue 4.1 — Narration guard's negative-phrase filter is incomplete

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

| Issue | Area | Severity | Effort | Touches |
|---|---|---|---|---|
| 2.1 | Recovery `tool_call_id`s | Medium (rare trigger) | Very low | `agent/agent.py` only |
| 3.4 | Sync file I/O | Low (current scale) | Medium (broad but mechanical) | All 4 `utils/*_manager.py` files |
| 4.1 | Narration guard negative-phrase gap | Low | Very low | `agent/agent.py` only |
| 4.2 | Can't resubmit payment proof | Low-to-moderate UX | Low (Option A) / Medium (Option B) | `utils/order_manager.py`, possibly `handlers/*` |

**Suggested order if tackled together:** 2.1 → 4.1 (both trivial, same file, could even be one combined small PR) → 4.2 (contained but needs an explicit approach decision first) → 3.4 (broadest change, best done as its own dedicated pass with full regression testing given how many files it touches).
