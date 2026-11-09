"""
Reads data/inventory.json. The seller edits that file directly (no DB),
so we cache it in memory but check the file's mtime before every read and
reload if it changed. Cheap for a file this size, and means the bot never
needs restarting after an inventory edit.

decrement_stock_for_cart() and restore_stock_for_order() are the only two
places that *write* to inventory.json. decrement_stock_for_cart is called
from place_order once an order is placed; restore_stock_for_order is called
from the /reject admin command to give that reserved stock back. Both hold
_inventory_lock and validate before writing.

Issue #16 (bug-audit 3.4): the two writers below now build the new data in
memory (still inside _inventory_lock, synchronously -- all pure in-memory
dict mutation, no I/O) and then offload the actual disk write to a worker
thread via utils.file_io.write_json_atomic (atomic temp-file + os.replace),
so the event loop isn't blocked for the write's duration.

The read side (_load_if_changed, and everything built on it --
get_product_by_id, search_products, check_variant) deliberately stays
synchronous. Two reasons:
  1. These are called from many sync call sites throughout agent/tools.py
     and making them async would ripple through that whole file for a read
     that's already cheap (an os.stat() plus, only on a changed mtime, one
     JSON parse of a seller-sized catalog -- not the append-only,
     ever-growing files the other three managers deal with).
  2. Now that writes are atomic (temp file + os.replace), a read landing
     concurrently with a write can only ever see the fully-old or
     fully-new file, never a torn one -- the one hazard offloading would
     have otherwise introduced is already closed by the atomic write
     itself, so there's no correctness reason to also make the read async.
_cache itself is never actually racy: every mutation (both here in
_load_if_changed and the post-write invalidation in the two writers below)
happens on the event-loop thread, and there's no `await` between a write
completing and `_cache["mtime"] = None` running -- Python's single-threaded
coroutine execution means those two statements can never interleave with
another coroutine's code. No lock was ever needed around the dict itself.

The real hazard closed by this fix is one level down, in the actual file
read: _load_if_changed's open()/json.load() now goes through
file_io.read_json_sync() instead of a bare open() call, so it gets the
same bounded PermissionError retry as every other reader in this project
(see file_io.py's module docstring -- this function, called from many
places throughout agent/tools.py, IS the reader that retry exists for:
it's unlocked, called constantly, and can genuinely land mid-os.replace()
while decrement_stock_for_cart/restore_stock_for_order are writing on a
worker thread). Before this, routing the retry logic only into file_io.py
without ever calling it from here left this exact reader unprotected --
an unhandled PermissionError here propagates up through get_product_by_id/
search_products/check_variant into agent/tools.py, where call_tool()'s
broad except Exception catches it and returns a spurious "tool failed:
[Errno 13] ..." to the customer for what should have been an ordinary
read. Not a crash, but a reliability regression Issue #16 was supposed to
close under concurrent load, not reintroduce. The mtime check itself
(config.INVENTORY_FILE.stat().st_mtime) is unchanged -- only the inner file
read is now retry-protected.

Fuzzy search ("Not built yet" item 3, 2026-11-08): search_products() below
tries an exact (substring) match first, completely unchanged from before.
Only when that returns ZERO results for a non-empty query does it fall
back to a token-level fuzzy match via the stdlib difflib.SequenceMatcher
-- see _fuzzy_search()'s own docstring for the matching strategy and why
stdlib (no new dependency) is enough at this project's catalog size. This
means fuzzy matching can never demote or interfere with an exact hit; it
only ever fires on what would otherwise have been a dead-end "no results"
search.
"""
import logging
import asyncio
import string
from difflib import SequenceMatcher
import config
from utils import order_manager, file_io

logger = logging.getLogger("inventory_manager")

_cache = {"mtime": None, "products": []}
_inventory_lock = asyncio.Lock()

def _load_if_changed():
    mtime = config.INVENTORY_FILE.stat().st_mtime
    if mtime != _cache["mtime"]:
        data = file_io.read_json_sync(config.INVENTORY_FILE, {"products": []})
        _cache["products"] = data.get("products", [])
        _cache["mtime"] = mtime
    return _cache["products"]

def get_all_products() -> list[dict]:
    return _load_if_changed()

def get_product_by_id(product_id: str) -> dict | None:
    for p in _load_if_changed():
        if p["id"] == product_id:
            return p
    return None

def _haystack_for(product: dict) -> str:
    """The combined, lowercased searchable text for one product -- shared
    by both the exact-substring pass and the fuzzy fallback, so they stay
    consistent about what counts as 'the product's text'."""
    return f"{product.get('name', '')} {product.get('description', '')} {product.get('category', '')}".lower()

def _fuzzy_search(query: str, category: str) -> list[dict]:
    """
    Token-level fuzzy fallback, used ONLY when the plain substring match in
    search_products() finds zero results for a non-empty query. Stdlib
    difflib.SequenceMatcher, not a new dependency (rapidfuzz/fuzzywuzzy
    would scale better, but this project's catalog is seller-sized -- a
    handful to a few dozen products -- so stdlib is enough; same
    no-new-dependency reasoning file_io.py's own docstring already uses for
    choosing asyncio.to_thread() over aiofiles).

    Matching strategy: split the query into word tokens, and for each
    query token find its single best-matching word token anywhere in the
    product's haystack (name + description + category). A product's
    overall score is the AVERAGE of those per-token best-matches, not the
    max -- this means a one-word typo'd query ("jordam") scores purely on
    how well that one word matches, while a multi-word query only scores
    well if most/all of its words have a decent match somewhere in the
    product's text (so "shirt socks" doesn't fuzzy-match a product that
    only vaguely resembles one of those words).

    Punctuation stuck to a word (a trailing comma in a description, say)
    is stripped from both query and haystack tokens before scoring, so
    "hoodie," doesn't score worse than a clean "hoodie" would.

    A product is included only if its score clears config.FUZZY_SEARCH_THRESHOLD,
    and results are returned ranked by score (closest match first), not
    catalog order -- unlike the exact-substring path, where results are
    already naturally relevant and insertion order is fine.

    The `category` hard filter (a distinct argument from the free-text
    query) is applied here exactly the same way the exact-match path
    applies it -- category is a structured filter, not fuzzy-matched
    itself.
    """
    query_tokens = [t.strip(string.punctuation) for t in query.split()]
    query_tokens = [t for t in query_tokens if t]
    if not query_tokens:
        return []

    scored: list[tuple[float, dict]] = []
    for p in _load_if_changed():
        if category and category not in p.get("category", "").lower():
            continue

        # Trailing/leading punctuation (a comma after a word in a
        # description, say) would otherwise drag a token's match score
        # down for no real reason -- strip it so "hoodie," scores the
        # same as "hoodie".
        haystack_tokens = [t.strip(string.punctuation) for t in _haystack_for(p).split()]
        haystack_tokens = [t for t in haystack_tokens if t]
        if not haystack_tokens:
            continue

        token_scores = []
        for qt in query_tokens:
            best = 0.0
            for ht in haystack_tokens:
                ratio = SequenceMatcher(None, qt, ht).ratio()
                if ratio > best:
                    best = ratio
            token_scores.append(best)

        score = sum(token_scores) / len(token_scores)
        if score >= config.FUZZY_SEARCH_THRESHOLD:
            scored.append((score, p))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [p for _, p in scored]

def search_products(query: str = "", category: str = "") -> list[dict]:
    """
    Case-insensitive substring match on name/description/category, same as
    before this fix. If that returns no results for a non-empty query,
    falls back to _fuzzy_search() above -- a typo like "jordam" or "hoody"
    now finds the intended product instead of coming back empty. An empty
    query (browse-a-category) and a query that already has exact matches
    are completely unaffected -- fuzzy matching only ever fires on what
    would otherwise have been a dead end.
    """
    query = query.lower().strip()
    category = category.lower().strip()
    results = []
    for p in _load_if_changed():
        if category and category not in p.get("category", "").lower():
            continue
        if not query or query in _haystack_for(p):
            results.append(p)

    if query and not results:
        return _fuzzy_search(query, category)

    return results

def check_variant(product_id: str, size: str = "", color: str = "") -> list[dict]:
    """Returns matching variants (with stock count) for a product.

    Color matching is exact (case-insensitive), not substring. Substring
    matching (`"red" in "red/blue"`) let one customer-facing color like
    "red" match multiple distinct variants at once, which caused stock to
    be decremented across more than one variant for a single line item --
    see decrement_stock_for_cart()/restore_stock_for_order() for the full
    explanation of the resulting inventory drift on rejection (was a known
    limitation, bug-audit issue 3.2).
    """
    product = get_product_by_id(product_id)
    if not product:
        return []
    matches = []
    # Use .get() to safely handle missing variants
    for v in product.get("variants", []):
        if size and v.get("size", "").lower() != size.lower():
            continue
        if color and color.lower() != v.get("color", "").lower():
            continue
        matches.append(v)
    return matches

async def decrement_stock_for_cart(cart_items: list[dict]) -> dict:
    """
    Atomically decrements stock for an entire cart.
    Validates all items in memory first. If any item lacks sufficient stock,
    the operation aborts and NO changes are written to disk.
    """
    async with _inventory_lock:
        # 1. Read current state from disk. Reading inline here (not via
        # _load_if_changed's cache) matches the pre-Issue-#16 behavior --
        # this function always wants the absolute latest on-disk state
        # right before a write, not a possibly-stale cached copy.
        data = await file_io.read_json(config.INVENTORY_FILE, {"products": []})

        # 2. Phase 1: Validate all items in the cart
        for item in cart_items:
            product = next((p for p in data.get("products", []) if p.get("id") == item.get("product_id")), None)
            if not product:
                return {"error": f"product '{item.get('product_id')}' not found during checkout"}

            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower() and item.get("color", "").lower() == v.get("color", "").lower()
            ]
            if not matches:
                return {"error": f"variant {item.get('size')}/{item.get('color')} not found for {item.get('product_id')}"}

            total_stock = sum(v.get("stock", 0) for v in matches)
            if total_stock < item.get("quantity", 0):
                return {
                    "error": "insufficient stock at checkout",
                    "hint": f"Not enough stock for {product.get('name')} ({item.get('size')}/{item.get('color')}). Requested {item.get('quantity')}, available {total_stock}.",
                    "shortfall": item
                }

        # 3. Phase 2: All items passed validation. Now actually decrement.
        for item in cart_items:
            product = next(p for p in data.get("products", []) if p.get("id") == item.get("product_id"))
            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower() and item.get("color", "").lower() == v.get("color", "").lower()
            ]

            remaining = item.get("quantity", 0)
            for v in matches:
                if remaining <= 0:
                    break
                take = min(v.get("stock", 0), remaining)
                v["stock"] = v.get("stock", 0) - take
                remaining -= take

        # 4. Write the fully updated inventory back to disk -- offloaded to a
        # worker thread and atomic (temp file + os.replace), so the event
        # loop isn't blocked and a concurrent reader can never see a torn
        # file (Issue #16).
        await file_io.write_json_atomic(config.INVENTORY_FILE, data)

        # Invalidate cache so next read gets the fresh data. Done back on
        # the event-loop thread, after the to_thread() write has returned --
        # _cache is never touched from the worker thread itself.
        _cache["mtime"] = None
        return {"status": "ok"}


async def restore_stock_for_order(order_id: str) -> dict:
    """
    Gives back the stock reserved by decrement_stock_for_cart when an order
    is rejected. Re-runs the exact same size/color matching used to decrement,
    so stock is credited back to precisely the variant it was taken from.

    Color matching is exact (case-insensitive) -- see check_variant()'s
    docstring. Previously it was substring-based, which meant a line item's
    size/color could match more than one variant at decrement time (e.g.
    color "red" matching both a "red" and a "red/blue" variant), spreading
    the decrement across them -- but the order record only ever stored
    product_id/size/color/quantity, not which specific variant(s) absorbed
    it, so this function had no way to know the original split and credited
    the full quantity back to just the first match, silently drifting stock
    on every such rejection (bug-audit issue 3.2, "Substring Color Matching
    Causes Inventory Drift on Rejection"). With exact matching, a line item
    can only ever match at most one variant, so decrement and restore are
    now structurally guaranteed to agree -- "credit the (only) match" is
    correct by construction rather than a documented limitation.

    Returns a dict:
      {"status": "ok", "restored": [items], "skipped": [items]}
    or {"error": "..."} if the order itself can't be found.

    An item whose product or variant no longer exists in inventory.json
    (e.g. the seller deleted/renamed it since the order was placed) is
    logged and added to "skipped" rather than raising -- the caller
    (admin_handlers.reject_order) surfaces "skipped" to the admin so the
    seller can reconcile it by hand instead of it silently vanishing.
    """
    # order_manager.get_order is now async + locked (Issue #16) -- awaited
    # here instead of called directly.
    order = await order_manager.get_order(order_id)
    if not order:
        return {"error": f"order '{order_id}' not found"}

    items = order.get("items", [])
    if not items:
        return {"status": "ok", "restored": [], "skipped": []}

    async with _inventory_lock:
        data = await file_io.read_json(config.INVENTORY_FILE, {"products": []})

        restored = []
        skipped = []

        for item in items:
            product = next(
                (p for p in data.get("products", []) if p.get("id") == item.get("product_id")),
                None,
            )
            if not product:
                logger.warning(
                    "restore_stock_for_order(%s): product '%s' no longer exists -- skipping restoration for this item",
                    order_id, item.get("product_id"),
                )
                skipped.append(item)
                continue

            matches = [
                v for v in product.get("variants", [])
                if v.get("size", "").lower() == item.get("size", "").lower()
                and item.get("color", "").lower() == v.get("color", "").lower()
            ]
            if not matches:
                logger.warning(
                    "restore_stock_for_order(%s): variant %s/%s no longer exists for product '%s' -- skipping restoration for this item",
                    order_id, item.get("size"), item.get("color"), item.get("product_id"),
                )
                skipped.append(item)
                continue

            matches[0]["stock"] = matches[0].get("stock", 0) + item.get("quantity", 0)
            restored.append(item)

        if restored:
            await file_io.write_json_atomic(config.INVENTORY_FILE, data)
            # Invalidate cache so next read gets the fresh data.
            _cache["mtime"] = None

        return {"status": "ok", "restored": restored, "skipped": skipped}