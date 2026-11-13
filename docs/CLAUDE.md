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
bot.py                    entrypoint, wires up routers (admin, then channel images, then user)
config.py                 all settings, loaded from .env
agent/
  llm_client.py           returns (client, model) for whichever provider is configured;
                           both clients built with httpx.AsyncClient(trust_env=False)
  tools.py                tool schemas (OpenAI function-calling format) + implementations
  agent.py                the tool-calling loop — most of the interesting logic lives here
  prompts.py              SYSTEM_PROMPT — see "Prompt rules" below, this file is load-bearing
handlers/
  user_handlers.py        text messages -> rate limit -> agent; photo messages -> payment screenshot;
                           sends product photos to customers
  admin_handlers.py       /pending_orders, /confirm, /reject, /ship, /deliver — gated by AdminOnlyMiddleware
  channel_images.py       product-images channel listener (channel_post) — default and per-color photos
utils/
  inventory_manager.py    reads data/inventory.json (mtime-cached); owns all stock-writing paths and
                           image storage; fuzzy fallback search. Imports order_manager
                           (one-directional — see constraint below).
  cart_manager.py         per-user cart, persisted to data/carts.json so carts survive restarts
                           (checkout atomicity itself lives in agent/tools.py's _checkout_lock)
  conversation_manager.py per-user chat history, persisted in full but trimmed for LLM context;
                           guarded by a single asyncio.Lock
  order_manager.py        orders.json + feedback.json read/write, full status lifecycle including
                           the atomic set_order_status_if_not() and shipped/delivered
  rate_limiter.py         per-user sliding-window message limit (in memory, not persisted)
  file_io.py              shared async/atomic JSON read-write helpers used by all managers above
data/
  inventory.json           seller edits this directly (may include image / images_by_color fields)
  orders.json               pending_confirmation -> awaiting_review -> confirmed -> shipped ->
                             delivered (or rejected)
  carts.json                 persisted carts, keyed by user_id (string)
  feedback.json
  conversations/{user_id}.json
.github/workflows/
  tests.yml               CI: pytest on ubuntu-latest + windows-latest, Python 3.11 + 3.12
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

## LLM client, dependency pins, and the proxy guard (agent/llm_client.py, requirements.txt)
`agent/llm_client.py` constructs both the Groq and Ollama `AsyncOpenAI`
clients with an explicit `httpx.AsyncClient(trust_env=False)`, so neither
client ever looks at `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` — connections
always go straight to the configured `base_url` regardless of what's set in
the shell the bot is launched from. The reason this matters at all is the
VPN/SOCKS-proxy/`httpx2` investigation documented in full under "Known
environment gotchas" below.

**`test/test_llm_client.py`** checks this in CI without any network call. It
builds the client for both providers via `get_client()`, reaches into the
`AsyncOpenAI` instance's private `_client` attribute, and asserts it's an
`httpx.AsyncClient` with `trust_env` set to `False`. It also checks the
Ollama base URL is respected and that an unknown `LLM_PROVIDER` raises a
clear `ValueError`. 4 tests, all passing without Ollama or a Groq key
present.

**Dependency pins, and why they're now explicit (not left to a floor
version):**
- `openai==3.0.0`. A CI run previously resolved to `openai` 3.50.0 purely
  because `requirements.txt` only specified `openai>=1.55.0` — a much newer
  major version than the one actually developed and tested against. Pinning
  the exact version means CI and the local venv test the same SDK, not just
  "a version that satisfies the same inequality."
- `httpx>=0.27`. `agent/llm_client.py` imports `httpx` directly to build the
  client above, so it's a real, direct dependency — not an incidental one
  that happens to come along for the ride via `openai`. Before this was
  added explicitly, CI failed to collect five test modules with
  `ModuleNotFoundError: No module named 'httpx'`, because `openai` 3.x pulls
  in a *different* package (`httpx2`, not `httpx`) as its own HTTP
  dependency — see "Known environment gotchas" for the full trace of how
  this was diagnosed.

Verified directly (not just inferred) on `openai==3.0.0`: the inner client
really is `httpx.AsyncClient`, `trust_env` really is `False`, and a live
`client.models.list()` call against a running Ollama instance succeeds. If
the `openai` pin is ever bumped, re-run both that live check and
`test/test_llm_client.py` before merging — nothing guarantees a future
`openai` release keeps accepting an `httpx.AsyncClient` the same way.

## Cart persistence (utils/cart_manager.py)
Carts used to live only in memory and were lost on every restart. They're
now persisted to `data/carts.json`, keyed by `str(user_id)`, guarded by an
`asyncio.Lock` matching the read-modify-write pattern already used in
`order_manager.py`. An empty cart is removed from the file entirely rather
than stored as `[]`, to keep the file small. Covered by
`test_cart_persistence.py`. The actual file I/O inside `_carts_lock` goes
through `utils/file_io.py` (offloaded to a worker thread, atomic write) —
see "Async file I/O" below; the locking and empty-cart-removal behavior
described above are unchanged.

## Conversation history locking (utils/conversation_manager.py) — fixed, was Issue 3.3
A single module-level `_history_lock = asyncio.Lock()` guards every read and
write to a user's conversation file, matching `cart_manager.py`/
`order_manager.py`'s existing single-lock-per-file pattern. `asyncio.Lock`
is not reentrant, so the module splits each operation into an unlocked sync
core (`_load_history_sync`, `_append_messages_sync`) and a locked async
wrapper (`load_history`, `append_messages`, `get_recent_for_llm`), each of
which acquires `_history_lock` exactly once. `load_history`,
`append_messages`, and `get_recent_for_llm` are all `async`.

Covered by `test_conversation_lock.py` (7 tests): the core race (50
concurrent `append_messages()` calls for one user — zero messages lost);
per-user correctness under concurrent multi-user load; two deadlock-safety
tests; and 3 regression checks. Two of these tests take `monkeypatch` and
reset `_history_lock` to a fresh instance, to avoid a pytest-asyncio
cross-test event-loop-binding artifact — see "Known environment gotchas."

The actual file I/O inside `_history_lock` goes through `utils/file_io.py`
(offloaded to a worker thread, atomic write) — see "Async file I/O" below.

## Stock integrity at checkout (utils/inventory_manager.py)
`decrement_stock_for_cart()` is the write path at order placement time. It's
called from `_place_order` in `agent/tools.py` and is atomic per cart:
1. **Validate first, write second** — every line item in the cart is checked
   against current stock *before* anything is decremented. If any single item
   can't be fulfilled, the whole call returns an error and nothing on disk
   changes.
2. Guarded by an `asyncio.Lock` (`_inventory_lock`) so concurrent checkouts
   can't race each other's read-modify-write cycle.
3. `place_order` in `tools.py` also re-validates the cart against live stock
   immediately before calling this, since stock may have changed since the
   items were added to the cart.

Covered by `test_concurrency.py` (50 simulated concurrent order placements)
and `test/test_backend.py::test_atomic_stock_validation`.

✅ **Multi-item cart N-tuple stock decrement — fixed, was Issue #6 / bug-audit
1.1.** `_place_order` previously passed the whole cart to
`decrement_stock_for_cart` once *per item* in the cart instead of once
total, decrementing an N-item cart's stock N times. Fixed by removing the
wrapping loop. Covered by `test_multi_item_checkout.py` (6 tests).

## Checkout atomicity (agent/tools.py) — fixed, was Issue #10 / bug-audit 3.1
A single module-level `_checkout_lock = asyncio.Lock()` in `agent/tools.py`,
held for the *entire* `_place_order` body — read cart, validate stock,
decrement stock, create order, and clear cart all happen inside one
`async with _checkout_lock:` block. Deliberately a single global lock, not
per-user, matching `conversation_manager._history_lock`'s precedent. The
cart is only ever cleared as the very last step, after order creation has
already succeeded — this avoids a data-loss race against a concurrent
`add_to_cart` that an "atomic consume, then restore on failure" design
would have introduced.

Covered by `test/test_checkout_atomicity.py` (5 tests), with genuine
interleaving forced via a monkeypatched `await asyncio.sleep(0)` in
`cart_manager.get_cart` (plain `asyncio.gather()` alone doesn't reliably
trigger this class of race).

## Async file I/O (utils/file_io.py) — fixed, was Issue 3.4 / GitHub #16
All four managers (`cart_manager.py`, `order_manager.py`,
`conversation_manager.py`, `inventory_manager.py`) call
`utils/file_io.py`'s `read_json`/`write_json_atomic` instead of raw
`open()`/`json` calls, each still from inside its own existing lock. The
actual blocking work is offloaded to a worker thread via
`asyncio.to_thread()`, so a slow disk write never stalls `aiogram`'s polling
of every other user's updates. Writes are atomic (temp file + `os.replace`),
which matters specifically because, once writes run on a worker thread, a
read can genuinely overlap a write for the first time — a naive
truncate-then-write would let a reader observe a torn file mid-write.

`order_manager.get_order()` and `get_latest_unpaid_order()` are now async
and locked, mirroring `get_all_orders()`.

**Read-side retry:** a bounded, exponential-backoff retry around transient
Windows `PermissionError` (a concurrent `open(path, "r")` can transiently
fail while another thread's `os.replace()` is in flight) lives in
`file_io.read_json_sync()`. **Follow-up fix:** `inventory_manager.py`'s own
`_load_if_changed()` had a bare `open()`/`json.load()` call that never
actually routed through `read_json_sync()`, so it didn't get this
protection despite being the exact reader the retry was built for. Fixed by
routing it through `read_json_sync()`. Covered by
`test/test_inventory_read_retry.py` (7 tests) — see "Testing" below for a
note on one of these tests' own CI flakiness and fix.

Covered overall by `test/test_async_file_io.py` (8 tests). Full suite
verified at 93/93 at the time this issue was closed.

## Stock reversal on rejection (utils/inventory_manager.py, utils/order_manager.py) — fixed, was Issue #4
`inventory_manager.restore_stock_for_order(order_id)` looks up the order's
items and credits stock back per line item under `_inventory_lock`. A line
item whose product/variant no longer exists is skipped and reported rather
than raising. `order_manager.set_order_status_if_not(order_id, status,
forbidden_current_status)` is an atomic check-and-set that `/reject` uses so
a double `/reject` on an already-rejected order is a safe no-op instead of
crediting stock back twice.

Covered by `test_stock_reversal.py`.

## Color matching (utils/inventory_manager.py) — fixed, was Issue 3.2
`check_variant()`, `decrement_stock_for_cart()`, and
`restore_stock_for_order()` all match color by exact, case-insensitive
equality rather than substring containment. Substring matching let a
product with overlapping color strings (e.g. "red" and "red/blue") have a
single line item's quantity spread across more than one variant at
decrement time, which `restore_stock_for_order` then had no way to
correctly reverse. Not reachable against the real inventory (no overlapping
color names), but a real latent bug.

Covered by `test/test_color_exact_match.py` (10 tests), built around a
deliberately overlapping-color inventory.

## Admin authorization (handlers/admin_handlers.py) — fixed, was Issue #1
`AdminOnlyMiddleware` is registered once on `admin_handlers.router`, so
every handler on that router — including `/ship` and `/deliver`, added
later — is gated in one place. `_is_admin(message)` authorizes the
configured admin group or a whitelisted `ADMIN_USER_IDS` entry messaging
the bot privately. Unauthorized attempts are logged and silently dropped.

Covered by `test_admin_auth.py`.

## Pending orders detail (handlers/admin_handlers.py) — fixed, was Issue #5
`_format_order_detail(order)` resolves each line item to its product name,
size, color, and quantity, plus a formatted timestamp and `user_id`.
`_format_payment_status(order)` distinguishes every status, including the
`shipped`/`delivered` tail (see "Order status beyond confirmed/rejected"
below). An estimated total is shown from *current* inventory prices, not a
price-at-purchase snapshot. `_chunk_order_blocks(blocks)` splits the
rendered orders across multiple Telegram messages once the combined text
would approach the 4096-character API limit.

Covered by `test_pending_orders_detail.py` (14 cases).

## The tool-calling loop (agent/agent.py)
This is the most-edited file in the project. Key things to know before
touching it:

- `MAX_TOOL_ROUNDS = 9`.
- Two separate malformed-tool-call failure modes exist and are handled the
  same shape, in two different places in the file: an API-level
  `BadRequestError` native-tag recovery, and a leaked-plain-text-JSON
  recovery when no tool call was made at all. Both recovery paths run the
  parsed call through `call_tool()`, track events, append a synthetic
  `assistant`+`tool` message pair with a unique `tool_call_id` (see
  `_new_recovery_tool_call_id`, fixed as Issue 2.1), reset
  `malformed_retries = 0`, and `continue`.
- **Narration guard** — scans the model's plain-text reply for commitment
  phrases ("I've added that to your cart", "order is placed") when no tool
  call was made. If detected (and not a negative/refusal phrasing — the
  `is_negative` filter also recognizes `can't`/`cannot`/`unable to`/`not
  able to`, fixed as Issue 4.1), it injects a correction message and
  `continue`s the loop (fixed as Issue 1.2 — this used to `return`
  immediately, so the correction was built but never sent).
- `RateLimitError` is caught separately and returns immediately with a
  friendly message.
- **Event tracking (`events` list, returned from `run_agent`).** Tracked at
  all three places a tool result gets processed (structured call, native-tag
  recovery, leaked-JSON recovery): `place_order` -> `"order"`, `log_feedback`
  -> `"feedback"`, `submit_payment_reference` -> `"payment_reference"`
  (fixed as Issue 1.3 — this event type existed on the receiving end in
  `user_handlers.py` but nothing ever populated it), and `get_product_details`
  resolving an image -> `"product_image"` (customer-facing, not an admin
  notification — see "Product images" below).

## Prompt rules (agent/prompts.py)
Every rule in this file exists because of a specific observed failure —
treat it as a running log, not just instructions:
- "Never say you'll do something without calling the tool."
- "Never say 'confirmed'/'order' before place_order actually succeeds."
- "Don't re-verify already-confirmed items."
- "Never invent shipping times, tracking emails, etc."
- "Call check_order_status rather than invent a status, shipping date, or
  delivery window" (added alongside order status beyond confirmed/rejected).
- "Mention photo availability from search results, pass color when the
  customer names one, be honest when falling back to the general photo"
  (added alongside product images).

If a new failure mode shows up, the fix belongs here first (cheap, fast to
iterate) before reaching for a code-level workaround.

## Payment flow
No payment gateway (by design — see "Not built yet"). After `place_order`,
the customer sends either a screenshot (handled directly by
`handle_payment_screenshot`, bypassing the LLM) or types a transaction
reference (the agent calls `submit_payment_reference`). Either way, the
order moves to `awaiting_review` and the admin group is notified with
`/confirm <id>` or `/reject <id>` ready to copy-paste. `/confirm` updates
status and DMs the customer; `/reject` updates status, DMs the customer, and
restores stock.

Customers can resubmit payment proof (fixed, was Issue 4.2, Option B):
`order_manager.get_latest_unpaid_order()` also matches `awaiting_review`
orders, every submission is appended to a `payment_proof_history` audit
list, and both notification paths flag a resubmission. Covered by
`test/test_payment_resubmission.py`.

---

## Rate limiting per user (utils/rate_limiter.py)
Per-user sliding-window limit on inbound text messages, to bound how many
LLM calls (and therefore Groq token spend / Ollama CPU time) a single user
can trigger in a short window — a real risk given Groq's free-tier
100,000-token/day cap (see "LLM provider" above).

- `utils/rate_limiter.py`: an in-memory `deque` of allowed-message
  timestamps per `user_id`, guarded by a single `asyncio.Lock` — same
  single-lock-per-concern pattern as `_carts_lock`/`_orders_lock`/
  `_history_lock`/`_inventory_lock`/`_checkout_lock` elsewhere in this
  project. Deliberately **not persisted to disk**: losing counters on a
  restart just resets everyone's quota, which is harmless, and it avoids a
  fifth JSON file and a fifth async/atomic-write surface for state that
  doesn't need either.
- Configurable via `config.RATE_LIMIT_MESSAGES` / `RATE_LIMIT_WINDOW_SECONDS`
  (`.env`-overridable; default 10 messages per 60 seconds).
- Checked in `handlers/user_handlers.py::handle_message` **before** any LLM
  call is made, so a throttled user's message never spends LLM budget at
  all. A rejected check doesn't itself count against the user, so a
  throttled user's window clears out naturally as old timestamps age past
  the window. Photo messages (payment screenshots) bypass this entirely,
  since they never reach the LLM.

A related, independently-discovered fix landed alongside this work: the
VPN's SOCKS proxy was unnecessarily intercepting local Ollama traffic (see
"LLM client, dependency pins, and the proxy guard" above, and the full
investigation under "Known environment gotchas"). A second UX fix landed
too: `handlers/user_handlers.py` now keeps Telegram's "typing..." indicator
alive for the full duration of a turn (refreshed every 4s, since the
indicator itself expires after ~5s) via a background task cancelled in a
`finally` block.

Covered by `test/test_rate_limiter.py` (7 tests): limit enforcement,
rejected calls not counting further against the user, sliding-window aging
(full and partial), per-user isolation, and concurrency safety (50
simultaneous calls for one user never overshoot the limit).

---

## Order status beyond confirmed/rejected (utils/order_manager.py, handlers/admin_handlers.py, agent/tools.py)
An optional status tail on top of the already-hardened
`pending_confirmation -> awaiting_review -> confirmed/rejected` lifecycle:

```
confirmed -> shipped -> delivered
```

- `utils/order_manager.py`: `advance_order_status(order_id, new_status)` is
  a forward-only transition guard mirroring `set_order_status_if_not`'s
  atomic check-and-set shape (`_orders_lock`). Refuses skipping a step
  (confirmed straight to delivered), acting on an order in the wrong state
  (shipping something still awaiting payment), or double-advancing an
  already-shipped order — returns a clear reason instead of a silent no-op.
  `get_latest_order_for_user(user_id)` is an unfiltered-by-status lookup
  (unlike `get_latest_unpaid_order`) so a customer can ask about an order
  that's already past the payment stage.
- `handlers/admin_handlers.py`: `/ship <order_id>` and `/deliver <order_id>`,
  gated by the existing `AdminOnlyMiddleware` for free. Each DMs the
  customer on success; a refused transition sends no customer DM.
  `_format_payment_status` labels `shipped` ("📦 Shipped") and `delivered`
  ("✅ Delivered") explicitly. `/pending_orders` stays scoped to
  `pending_confirmation`/`awaiting_review` only, by design — a
  shipped/delivered order drops out of that list and is only visible via
  individual lookup (`check_order_status` on the customer side).
- `agent/tools.py`: `check_order_status` so the agent can answer "where's my
  order?" instead of guessing. Includes an explicit ownership check — a
  model-supplied `order_id` is verified to actually belong to the requesting
  `user_id` before anything is returned, so a hallucinated or guessed id
  can't leak another customer's order.
- `agent/prompts.py`: directs the model to call `check_order_status` rather
  than invent a status, shipping date, or delivery window.

Covered by `test/test_order_status.py` (20 tests): the transition guard
(happy path, every invalid-transition case, double-advance refusal,
order-not-found), `get_latest_order_for_user`, both admin commands
end-to-end, the new status labels, and `check_order_status` (own order by
id, latest order with no id given, the cross-user ownership block, no
orders, unknown id).

---

## Fuzzy product search (utils/inventory_manager.py)
`search_products()` now tolerates typos ("jordam", "hoody") instead of
returning zero results.

- Tries the existing exact substring match first, completely unchanged.
  Only when that returns zero results for a non-empty query does it fall
  back to a new token-level fuzzy match (`_fuzzy_search()`), so an exact
  hit can never be demoted or interfered with.
- Stdlib `difflib.SequenceMatcher` — no new dependency. Each query word is
  scored against its single best-matching word in a product's combined
  name/description/category text; a product's overall score is the average
  of its per-word best matches, and results are ranked by score.
  Punctuation attached to a word is stripped from both query and haystack
  tokens before scoring.
- `config.FUZZY_SEARCH_THRESHOLD` (default `0.7`, `.env`-tunable) is the
  minimum per-word score to count as a match — calibrated against real typo
  patterns (single-letter substitutions, transpositions, missing/extra
  letters score 0.73-0.92) while keeping genuinely unrelated words (~0.5 or
  below) safely excluded.

Covered by `test/test_fuzzy_search.py` (16 tests): exact-match priority and
regression, core typo cases, threshold precision, category-filter
interaction, result ranking, and punctuation-stripping.

---

## Product images (handlers/channel_images.py, utils/inventory_manager.py)
A private, bot-admin Telegram channel used as a seller-facing image-upload
gallery, supporting both a default photo per product and per-color photos.

- `handlers/channel_images.py` (new): posting a photo captioned with a
  product id ("p001" or "#p001") sets that product's default photo; adding
  a color as a second caption word ("p002 black") sets that color's
  specific photo instead. The color is validated against the product's own
  real variant colors (exact, case-insensitive match) and rejected with the
  valid colors listed if it doesn't match. One photo per post only —
  media-group/album posts are ignored with a warning. Last write always
  wins per field; no history/versioning. Trust model: Telegram only
  delivers `channel_post` updates for channels the bot is an ADMIN of, and
  only channel admins/owners can post there to begin with —
  `config.PRODUCT_IMAGES_CHANNEL_ID` is a belt-and-suspenders identity
  check, not the actual security boundary.
- `utils/inventory_manager.py`: products may have `image` (the
  default/fallback photo) and `images_by_color` (a dict of
  color -> file_id/URL). Both a Telegram file_id and a plain URL work
  identically anywhere a photo is sent. `set_product_image(product_id,
  image, color=None)` (locked, atomic) and `get_image_for_color(product,
  color)`, which resolves an exact color match if one exists, else the
  default, else nothing — and tells the caller which case occurred.
  `search_products()` results carry a derived `has_image` boolean (true if
  either field is set) and never the raw image data itself.
- `agent/tools.py`: `get_product_details` gains an optional `color`
  argument. Fixes a real latent bug: a product resolved through the
  name/fuzzy-search fallback path (rather than a direct id lookup) is a
  search-result shallow copy with image fields already stripped —
  `_get_product_details` now re-fetches the raw stored product by its
  resolved id before resolving a photo, so a product found by name doesn't
  incorrectly appear imageless.
- `agent/agent.py`: a `product_image` event (customer-facing, not an admin
  notification) fires when `get_product_details` resolves an image,
  carrying the image, product name, and resolved color (if any).
- `handlers/user_handlers.py`: the customer-facing photo caption includes
  the color when one was actually matched ("Test Hoodie (black)"), falling
  back to just the product name otherwise. Failure-tolerant — a
  broken/expired file_id is caught and logged, never crashes the turn.
- `agent/prompts.py`: tells the model to mention photo availability when
  search results include `has_image` items, to pass `color` when the
  customer names one, and to be explicit when a result's image has no
  matching `image_color` rather than implying a match that didn't happen.
- `config.py`: `PRODUCT_IMAGES_CHANNEL_ID`, same shape as `ADMIN_GROUP_ID`.

Covered by `test/test_product_images.py` (~35 tests): caption parsing for
both formats, default vs. color-specific storage and validation,
`get_image_for_color`'s resolution/fallback logic, `has_image` without
leaking raw image data, the channel handler end-to-end, `get_product_details`'s
color resolution including the name-fallback re-fetch regression test, the
customer-facing send's color-aware caption, and full `run_agent`
integration confirming the `product_image` event carries the resolved
color end to end.

---

## Continuous Integration (.github/workflows/tests.yml)
Every push and PR to `main` runs the full suite automatically, closing a
real gap: before this, the only verification was Moeid manually running
`python -m pytest . -v` on his own Windows venv and remembering to do it.

- Triggers on `push`/`pull_request` to `main`, plus manual
  `workflow_dispatch`. A `concurrency` group cancels a run that's superseded
  by a newer push to the same branch/PR.
- **Matrix: `ubuntu-latest` and `windows-latest` × Python 3.11 and 3.12** (4
  jobs). The Windows leg specifically matters here — this project has
  several tests whose own behavior (file-locking retries, `os.replace`
  contention) is Windows-specific, and a prior bare-`pytest` invocation
  genuinely failed to collect `test/test_admin_auth.py` on Windows with
  `ModuleNotFoundError: No module named 'config'` (see "Known environment
  gotchas"), which CI would now catch automatically.
- Runs `python -m pytest . -v` — not bare `pytest` — matching this file's
  own documented recommendation for exactly that reason.
- **A dedicated step fails the build if the test run modifies tracked
  `data/*.json` files** (`git diff --exit-code -- data/`). This project has
  a real history of standalone/manual test runs accidentally polluting real
  `data/orders.json` (see "Known environment gotchas") — every current test
  file sandboxes its own config paths to a throwaway `test_data/` directory,
  so a clean run should leave `data/` completely untouched. This step turns
  a future test that forgets to sandbox into an immediate, loud CI failure
  instead of a silent data-loss incident discovered later.
- **No secrets required.** Every test mocks `agent.agent.get_client` (or
  otherwise avoids a live LLM/Telegram call), so the workflow needs no
  `BOT_TOKEN`, `GROQ_API_KEY`, or similar — safe to run on PRs from forks
  with no risk of leaking credentials.

**First real catch:** the very first CI run failed on all four jobs with
`ModuleNotFoundError: No module named 'httpx'`. `requirements.txt` had never
declared `httpx` directly — it worked locally only because `openai`'s own
dependency tree happened to pull in *something* under that import path in
the exact versions already installed in the dev venv. CI, resolving fresh,
pulled a newer `openai` whose own HTTP dependency is a *different* package
entirely (`httpx2`), exposing that `httpx` was never actually a declared
dependency. See "LLM client, dependency pins, and the proxy guard" above
for the full fix (`httpx>=0.27` added, `openai` pinned to the exact
developed-against version) and "Known environment gotchas" for the
diagnostic trail.

**Second catch, on the Windows/3.12 leg specifically:**
`test_load_if_changed_still_reloads_on_real_inventory_edit` failed once with
`assert 5 == 999` — a genuine CI-only flake caused by filesystem mtime
granularity (see "Known environment gotchas"), fixed by having the test set
the file's mtime explicitly via `os.utime()` rather than relying on two
rapid writes landing on different timestamps.

Once both were fixed, all four jobs passed, including the "accidental
data/ mutations" guard.

## Known environment gotchas (all cost real debugging time — check these first)
- **`ADMIN_GROUP_ID` stops working silently** if the group converts to a
  supergroup, or sometimes even after privacy-mode changes. Fix: remove the
  bot from the group and re-add it, then re-fetch the chat_id via a fresh
  message + `getUpdates` (bot must NOT be running when you check manually).
- If Telegram (app or web) doesn't load without a VPN, the bot won't connect
  either — a full system-wide VPN client needs to be connected before
  running the bot.
- **`python-dotenv` doesn't reliably strip inline comments on an otherwise
  empty value.** Keep comments for blank-by-default `.env` values on their
  own line, not trailing on the same line.
- **`git am` can fail on Windows** against a working tree with local drift;
  fall back to whole-file replacement instead of re-patching. **`git apply`
  only writes to the working directory** — it does not commit; always
  follow with `git status` then explicit `git add`/`git commit`.
- **Unrelated local edits can silently ride along into a feature branch.**
  Check `git status`/`git diff` before staging, every time.
- **`pytest test_*.py test/ -v` doesn't work in PowerShell** (glob
  expansion). Use `pytest . -v` or list files explicitly.
- **Bare `pytest . -v` can fail to collect `test/test_admin_auth.py` on
  Windows** with `ModuleNotFoundError: No module named 'config'`, even
  though the file's own `sys.path.insert` is supposed to make the repo root
  importable. **Fix: run `python -m pytest . -v`** instead of the bare
  `pytest` executable — this is now also why CI's own workflow invokes
  pytest this exact way (see "Continuous Integration" above).
- **pytest only discovers files actually named `test_*.py`** — a file
  missing the underscore after `test` is silently skipped, no error.
- **`data/*.json` files are tracked in git and are easy to accidentally
  overwrite or lose.** Several root-level test scripts write directly to
  real `data/` paths with no sandboxing when run standalone. Always run
  `git status` before committing. CI now has its own automated version of
  this exact check — see "Continuous Integration" above — but it only
  covers CI runs, not local manual testing, so this habit still matters
  locally too.
- **A module-level `asyncio.Lock()` singleton can only ever be genuinely
  contended once a real `await` sits inside its critical section** — and
  pytest-asyncio's one-event-loop-per-test-function default means that,
  once it is, the SAME lock object can clash across tests with "is bound to
  a different event loop." Fix: the affected tests take `monkeypatch` and
  reset the lock to a fresh `asyncio.Lock()` instance at the top of the
  test. Purely a test-isolation artifact, never a reason to change the
  production locking design.
- **`openai` 3.x depends on `httpx2`, a separate package from `httpx`,
  despite the similar name.** `pip show openai` on this project's pinned
  version (`3.0.0`) lists `httpx2` under `Requires:`, and `httpx` shows no
  `Required-by:` at all — confirming `httpx` is not pulled in
  transitively and must be declared directly if any of this project's own
  code imports it (as `agent/llm_client.py` does). This is almost certainly
  the origin of the separately-documented `httpx2` package seen earlier in
  this file's VPN/proxy investigation below — not a stray leftover install,
  but `openai`'s own real dependency. Confirmed by direct inspection:
  `openai/_base_client.py` does `import httpx2` and
  `from ._httpx2 import (..., is_httpx2_sync_client, normalize_httpx2_auth)`.
  Despite this, a plain `httpx.AsyncClient` passed into `AsyncOpenAI(
  http_client=...)` on `openai==3.0.0` is still accepted and used correctly
  — verified live (inner client type, `trust_env` value, and a real
  `models.list()` call all checked directly; see "LLM client, dependency
  pins, and the proxy guard" above). Worth re-verifying this specific point
  again on any future `openai` version bump, since nothing guarantees it
  stays true.
- **Filesystem mtime resolution can make a test that depends on "the file
  changed" flaky across platforms.** `inventory_manager._load_if_changed()`
  only reloads when `config.INVENTORY_FILE.stat().st_mtime` changes.
  `test_load_if_changed_still_reloads_on_real_inventory_edit` rewrote the
  file immediately after warming the cache, with nothing ensuring the two
  writes' mtimes actually differed — on the Windows CI runner the two
  writes could land within the same timestamp tick, leaving the cache
  stale and failing the assertion (`assert 5 == 999`) even though the exact
  same code passed locally every time. This is a test bug, not a
  production one: a human hand-editing `inventory.json` takes far longer
  than any filesystem's timestamp granularity. Fixed by having the test
  call `os.utime()` to explicitly set the rewritten file's mtime two
  seconds past the original, removing the dependency on real elapsed time
  or timestamp resolution entirely. Worth the same treatment for any future
  test whose correctness depends on two file writes happening fast enough
  to risk landing in the same mtime tick.

  ## Known environment gotchas (continued) — SOCKS proxy / httpx2 / national bandwidth throttling (Nov 2026)

**Symptom:** `python bot.py` crashed immediately on the first inbound
message with:
```
ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
```
raised from inside `agent/llm_client.py::get_client()`.

**Root cause, layer 1 — the SOCKS proxy itself.** Moeid's dev machine runs
a VPN client that exposes a local SOCKS5 proxy
(`socks5://127.0.0.1:10808`) and sets `HTTP_PROXY`/`HTTPS_PROXY`
system-wide. `httpx` trusts those env vars by default (`trust_env=True`),
so even the local Ollama call (`http://localhost:11434`) got routed
through the SOCKS proxy, and `httpx`'s SOCKS support needs the separate
`socksio` package, which wasn't installed.

**Root cause, layer 2 — a second, unexplained `httpx2` package.**
Tracebacks showed the actual module as `httpx2`, not plain `httpx`. Later
confirmed (see the dedicated gotcha entry above) that this is `openai`
3.x's own real HTTP dependency, not an unrelated stray install.

**Also hit, unrelated to the proxy itself:** the venv's own
`pip.exe`/`python.exe` launcher executables had a stale, broken absolute
path baked in from when the venv was created at a different project
location. **Fix: use `python -m pip ...`** instead of the bare launcher.

**Fix for the proxy/SOCKS issue — explicit `trust_env=False`, not
installing `socksio`.** `agent/llm_client.py` constructs both the Groq and
Ollama `AsyncOpenAI` clients with an explicit
`http_client=httpx.AsyncClient(trust_env=False)`, so neither client ever
looks at proxy env vars — connections always go straight to the configured
`base_url` with zero proxy involvement. This matches `bot.py`'s own
existing convention for Telegram (an explicit, opt-in `PROXY_URL`, never
ambient env-var detection).

**The real remaining cause of slow turns: national internet throttling,
not a bug.** After the `trust_env=False` fix, full bot turns were still
taking 30-120+ seconds, inconsistently, traced to Moeid's country
throttling international/VPN-routed bandwidth nationwide — an
infrastructure constraint outside the application's control, not something
further code changes can fix.

**Mitigation shipped for this: a periodic typing-indicator refresh.**
`handlers/user_handlers.py::handle_message` runs a background task
(`_keep_typing`) that re-sends the typing action every 4 seconds for the
full duration of the `run_agent()` call, cancelled in a `finally` block.

Both fixes (`trust_env=False` and the typing-indicator refresh) are
unconditionally correct improvements and worth keeping even after/if the
national bandwidth throttling eases.

## Testing
```bash
pytest . -v
```
(On Windows PowerShell, use this form rather than `pytest test_*.py test/
-v` — see "Known environment gotchas" above for why the glob form fails.
CI runs `python -m pytest . -v` for the same reason.)

Full suite: **185 passing**, across every file below plus the ones listed
in each feature section above.

- `test_concurrency.py` — 50 simulated concurrent `place_order` calls.
  **Writes directly to real `data/orders.json`** when run standalone.
- `test_cart_persistence.py` — cart survives a simulated restart.
- `test_history_persistence.py` — conversation trimming never orphans a
  `tool_call`/`tool` result pair.
- `test_conversation_lock.py` — Issue 3.3 coverage (7 tests).
- `test_narration_guard.py` — unit tests the commitment-phrase detection
  logic in isolation. Not pytest-discovered; run directly.
- `test_nested_json_regex.py` — unit tests `_parse_native_function_call`.
  Not pytest-discovered; run directly.
- `test_admin_auth.py` — admin middleware authorization.
- `test_stock_reversal.py` — Issue #4 coverage.
- `test/test_color_exact_match.py` — Issue 3.2 coverage (10 tests).
- `test_leaked_json_recovery.py` — Issue #3 coverage (8 tests).
- `test_narration_guard_loop.py` — Issue 1.2 coverage (7 tests).
- `test_payment_reference_notification.py` — Issue 1.3 coverage (5 tests).
- `test_pending_orders_detail.py` — Issue #5 coverage (14 cases).
- `test_multi_item_checkout.py` — Issue #6 / bug-audit 1.1 coverage (6 tests).
- `test/test_backend.py` — end-to-end tool-call flow plus atomic
  stock-validation.
- `test/test_checkout_atomicity.py` — Issue #10 / bug-audit 3.1 coverage
  (5 tests).
- `test/test_recovery_tool_call_ids.py` — Issue 2.1 coverage.
- `test/test_payment_resubmission.py` — Issue 4.2 coverage.
- `test/test_async_file_io.py` — Issue #16 / bug-audit 3.4 coverage
  (8 tests).
- `test/test_inventory_read_retry.py` — Issue #16 follow-up coverage
  (7 tests). One of these (`test_load_if_changed_still_reloads_on_real_
  inventory_edit`) was found to be intermittently flaky under CI due to
  filesystem mtime granularity and fixed via explicit `os.utime()` — see
  "Known environment gotchas."
- `test/test_rate_limiter.py` — rate limiting per user coverage (7 tests).
- `test/test_order_status.py` — order status (shipped/delivered) coverage
  (20 tests).
- `test/test_fuzzy_search.py` — fuzzy product search coverage (16 tests).
- `test/test_product_images.py` — product images coverage (~35 tests).
- `test/test_llm_client.py` — proxy-handling regression guard for
  `agent/llm_client.py` (4 tests). Makes no network calls; checks the
  constructed client's inner `httpx.AsyncClient` has `trust_env=False` for
  both providers, that the Ollama base URL is respected, and that an
  unknown provider raises a clear error. See "LLM client, dependency pins,
  and the proxy guard" above.

All work was also manually verified against the live bot (not just the
sandboxed suite) before being considered done, per this project's own
"Git branching/merge workflow for issue fixes" convention.

## GitHub issue tracker status
Bug tracking has moved from an informal `BUGS.md` to GitHub Issues on the
repo. The GitHub issue/PR *numbers* don't line up with the "Issue #N"
numbering used in titles and in this doc. When looking something up on
GitHub, search by title/keyword rather than assuming the URL number
matches the "Issue #N" title number.

As of the last update:
- **Issue #1** (Missing Authorization for Admin Commands) — fixed, merged.
- **Issue #2** (`/reject` missing `await`) — closed, fixed prior to #1.
- **Issue #3** (Broken leaked-JSON tool-call recovery) — fixed, merged.
- **Issue #4** (No Stock Reversal on Order Rejection) — fixed, merged.
- **Issue #5** (Sparse Order Details in `/pending_orders`) — fixed, merged.
- **Issue #6** (Multi-item Cart N-tuple Stock Decrement) — fixed, merged.
- **Issue #10** (No atomic cart-consume across `_place_order`) — fixed,
  merged.
- **Issue #12** (Substring Color Matching) — fixed, closed.
- **Issue #13** (Hardcoded recovery `tool_call_id`s) — fixed, closed.
- **Issue #16** (Synchronous file I/O blocks the event loop) — fixed,
  merged, with a follow-up patch for `inventory_manager._load_if_changed`.
- **CI workflow added** (not a numbered GitHub issue) — `.github/workflows/
  tests.yml` runs the full suite on `ubuntu-latest` and `windows-latest`
  across Python 3.11 and 3.12 on every push/PR to `main`. First run caught
  a real, previously-undeclared `httpx` dependency gap (see "Continuous
  Integration" above) and a CI-only flaky test, both now fixed. All four
  jobs pass.

## Bug-audit fixes status (bug-audit-evaluation.md numbering)
A separate audit round — an AI model given the repo + this doc, asked to
find bugs, cross-checked in `bug-audit-evaluation.md` — used its own
"Issue N.N" numbering that is **unrelated** to the GitHub issue numbers
above; don't conflate the two.

1. **Issue 1.2** (narration guard `return` instead of `continue`) — fixed.
2. **Issue 1.3** (no admin notification for typed `submit_payment_reference`
   calls) — fixed.
3. **Issue 1.1** (multi-item cart N× stock decrement) — fixed, GitHub #6.
4. **Issue 3.3** (`conversation_manager` has no `asyncio.Lock`) — fixed.
5. **Issue 3.1** (no atomic cart-consume across `_place_order`) — fixed,
   GitHub #10.
6. **Issue 3.2** (substring color matching) — fixed, GitHub #12.
7. **Issue 2.1** (hardcoded recovery `tool_call_id`s) — fixed, GitHub #13.
8. **Issue 4.1** (narration guard's negative-phrase filter gap) — fixed.
9. **Issue 4.2** (can't resubmit payment proof) — fixed, Option B.
10. **Issue 3.4** (synchronous file I/O blocks the event loop) — fixed,
    GitHub #16, with a follow-up patch.

**All ten items from the original bug-audit-evaluation.md are closed.**

## Not built yet
Real payment gateway integration. **Explicitly decided out of scope
(2026-11-11, Moeid).** The project intentionally relies on manual
screenshot/reference review for a single-seller shop, and a real gateway
integration would be the biggest-scope, highest-risk item on the original
list by a wide margin — real money changing hands, PCI-adjacent concerns,
webhook signature verification and replay protection, and a genuine
architectural departure from this project's "no backend beyond flat JSON
files, manual review" design philosophy that every other fix in this
project has deliberately preserved. Worth revisiting only if the business
scales past what manual review can reasonably handle.

All other items from the original "Not built yet" list — rate limiting per
user, order status beyond confirmed/rejected, fuzzy product search, and
product images — have been built; see their own sections above for each.

## Working conventions
- **Whole-file replacement over diff-patching.** When making a multi-line
  change, prefer regenerating and handing over the full file content over
  a patch/diff.
- **Prompt-level fixes before code-level ones** for model-reliability
  issues — see "Prompt rules" above.
- **Never trust the LLM to provide `user_id`/`username`** — always injected
  from the real Telegram context in `agent/tools.py`'s `call_tool()`. The
  same skepticism applies to any other identifier the model supplies as an
  argument (e.g. `order_id`), verified ownership server-side rather than
  trusted at face value.
- **Minimal, structurally-scoped fixes over opportunistic refactors.**
  Prefer the smaller fix that doesn't touch already-working, already-tested
  code paths unless the dedup is actually required to solve the issue at
  hand.
- **Unrelated local changes get separated out, not bundled into a feature
  branch.**
- **No new dependencies without real need.** Fuzzy search (stdlib
  `difflib`) and rate limiting (stdlib `collections.deque`) both
  deliberately avoided adding a dependency. `httpx` and the `openai` pin
  were the exception — both are genuinely required, direct dependencies
  that needed to be declared explicitly rather than left implicit.
- Verification command after significant changes:
  ```bash
  python -c "from agent import tools, prompts, agent; from utils import order_manager, inventory_manager, file_io; from handlers import admin_handlers, user_handlers; print('all imports OK')"
  ```
- **Git branching/merge workflow for issue fixes:**
  1. `git checkout -b fix/issue-N-short-description` off `main` — never
     commit directly to `main`.
  2. Implement the fix, add tests, run the full suite (`python -m pytest .
     -v`).
  3. Sanity-check new tests against the pre-fix code when practical.
  4. Manually verify against a live bot before considering it done, when a
     live bot is available.
  5. Generate a `.patch` file for review, verify it applies cleanly to a
     fresh checkout of `main` (`git apply --check`).
  6. Apply via `git apply`. **This only modifies the working directory —
     it does not commit.** Always run `git status` immediately after, then
     explicitly `git add` + `git commit`.
  7. Before committing, check `git status`/`git diff` for anything
     unrelated sitting in the working tree.
  8. Merge into `main` with `git merge --no-ff fix/issue-N-...`.
  9. **Before the final commit/push, check `git status` for unintended
     `data/*.json` changes** from manual bot testing — `git checkout --
     data/` to discard them if so.
  10. `git push origin main`, then delete the merged local branch.
  11. **Now that CI exists, also confirm all four CI jobs pass on the
      push** before considering the work done — a clean local run alone is
      no longer sufficient, since CI has already caught real issues (a
      missing dependency, a platform-specific test flake) that a local
      Windows venv run didn't surface.