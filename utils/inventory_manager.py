"""
Reads data/inventory.json. The seller edits that file directly (no DB),
so we cache it in memory but check the file's mtime before every read and
reload if it changed. Cheap for a file this size, and means the bot never
needs restarting after an inventory edit.

decrement_stock_for_cart(), restore_stock_for_order(), and
set_product_image() are the only three places that *write* to
inventory.json -- all hold _inventory_lock and validate before writing,
with the actual disk write offloaded via file_io.write_json_atomic
(Issue #16).

The read side (_load_if_changed, and everything built on it) stays
synchronous -- see the two reasons documented in earlier revisions of
this docstring (cheap reads from many sync call sites; atomic writes
already close the one torn-read hazard offloading would introduce).

Fuzzy search ("Not built yet" item 3): search_products() tries an exact
substring match first, falling back to a token-level
difflib.SequenceMatcher match only when that finds zero results for a
non-empty query -- see _fuzzy_search()'s own docstring.

Product images ("Not built yet" item 4, 2026-11-09 -- extended the same
day to support per-color photos, Moeid's follow-up): a product may have:
  - `image`: a Telegram file_id or plain URL -- the DEFAULT/fallback
    photo, shown when no color-specific photo exists or none was asked
    for. This is the original, simpler field from the first version of
    this feature; untouched in shape, just reinterpreted as "the
    fallback" now that a more specific option exists alongside it.
  - `images_by_color`: an optional dict, {color (lowercase, exact match
    against the product's own variant colors): file_id/URL}. Only
    consulted when a caller explicitly asks for a specific color (see
    get_image_for_color() below); a product with only one real color
    (like the seed sneaker data) never needs this field at all.
Both `image` values and `images_by_color` values are valid as Telegram's
send_photo `photo` parameter, file_id or URL alike -- no branching
anywhere a photo is actually sent, regardless of which field it came
from.

search_products() results carry a derived `has_image` boolean (true if
EITHER field is populated) and never the raw image data itself -- see
_strip_raw_image()'s docstring. get_product_by_id() (and therefore
get_all_products()) still returns the complete, raw stored dict,
`images_by_color` included -- callers that need to actually resolve an
image (agent/tools.py's get_product_details) read the raw dict, never a
search result's stripped copy.
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
    by both the exact-substring pass and the fuzzy fallback."""
    return f"{product.get('name', '')} {product.get('description', '')} {product.get('category', '')}".lower()

def _fuzzy_search(query: str, category: str) -> list[dict]:
    """
    Token-level fuzzy fallback, used ONLY when the plain substring match
    in search_products() finds zero results for a non-empty query. Stdlib
    difflib.SequenceMatcher, not a new dependency.
    """
    query_tokens = [t.strip(string.punctuation) for t in query.split()]
    query_tokens = [t for t in query_tokens if t]
    if not query_tokens:
        return []

    scored: list[tuple[float, dict]] = []
    for p in _load_if_changed():
        if category and category not in p.get("category", "").lower():
            continue

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

def _strip_raw_image(product: dict) -> dict:
    """
    Builds the dict a search result actually exposes: every field the
    stored product has EXCEPT the raw `image` and `images_by_color`
    values (neither has any use in a multi-item listing's context --
    they're only resolved by get_product_details, which reads the raw
    stored dict directly via get_product_by_id, never a search result).
    `has_image` is true if EITHER field is populated, so a product with
    only per-color photos and no default still shows as "has a photo" in
    a search listing.
    """
    copy = {k: v for k, v in product.items() if k not in ("image", "images_by_color")}
    copy["has_image"] = bool(product.get("image") or product.get("images_by_color"))
    return copy

def search_products(query: str = "", category: str = "") -> list[dict]:
    """
    Case-insensitive substring match on name/description/category, falling
    back to fuzzy matching when that finds nothing for a non-empty query.
    Each returned product is a shallow copy with raw image data stripped
    and `has_image` added -- see _strip_raw_image().
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
        results = _fuzzy_search(query, category)

    return [_strip_raw_image(p) for p in results]

def check_variant(product_id: str, size: str = "", color: str = "") -> list[dict]:
    """Returns matching variants (with stock count) for a product.
    Color matching is exact (case-insensitive) -- see bug-audit issue 3.2.
    """
    product = get_product_by_id(product_id)
    if not product:
        return []
    matches = []
    for v in product.get("variants", []):
        if size and v.get("size", "").lower() != size.lower():
            continue
        if color and color.lower() != v.get("color", "").lower():
            continue
        matches.append(v)
    return matches

def get_image_for_color(product: dict, color: str | None) -> tuple[str | None, str | None]:
    """
    Resolves which photo to show for a product, optionally for one
    specific color. Takes the RAW stored product dict (as returned by
    get_product_by_id) -- never a search_products result, which has both
    image fields stripped.

    Returns (image, resolved_color):
      - If `color` is given and that exact color (case-insensitive) has
        its own entry in the product's `images_by_color`, returns that
        image and the matched color string -- so the caller can tell the
        customer specifically which color's photo they're seeing.
      - Otherwise (no color given, or no color-specific photo exists for
        the one requested) falls back to the product's default `image`,
        with resolved_color None -- this return shape (an image present,
        but resolved_color absent) is exactly how agent/tools.py tells
        the model "a photo exists, but it's the general one, not
        necessarily the exact color that was asked for."
      - If there's no image at all (neither a match nor a default),
        returns (None, None).
    """
    if color:
        images_by_color = product.get("images_by_color") or {}
        match = images_by_color.get(color.strip().lower())
        if match:
            return match, color.strip().lower()
    return product.get("image"), None

async def decrement_stock_for_cart(cart_items: list[dict]) -> dict:
    """
    Atomically decrements stock for an entire cart.
    Validates all items in memory first. If any item lacks sufficient stock,
    the operation aborts and NO changes are written to disk.
    """
    async with _inventory_lock:
        data = await file_io.read_json(config.INVENTORY_FILE, {"products": []})

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

        await file_io.write_json_atomic(config.INVENTORY_FILE, data)
        _cache["mtime"] = None
        return {"status": "ok"}


async def restore_stock_for_order(order_id: str) -> dict:
    """
    Gives back the stock reserved by decrement_stock_for_cart when an order
    is rejected. See bug-audit issue 3.2 for why color matching is exact.
    """
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
            _cache["mtime"] = None

        return {"status": "ok", "restored": restored, "skipped": skipped}


async def set_product_image(product_id: str, image: str, color: str | None = None) -> dict:
    """
    Sets a product's photo -- either the default/fallback `image` field
    (color=None, the original behavior), or one color's entry in
    `images_by_color` (color given). Last write always wins for whichever
    field is targeted; no history/versioning.

    When `color` is given, it's validated against the product's OWN real
    variant colors (exact match, case-insensitive -- same convention as
    check_variant/decrement_stock_for_cart/restore_stock_for_order, see
    bug-audit issue 3.2) before being accepted. This is deliberate: without
    it, a seller's typo ("blak" instead of "black") would silently create
    an images_by_color entry that can never actually be reached by
    get_image_for_color (which only ever looks up real variant colors a
    customer could plausibly ask for), with no feedback that anything was
    wrong. Rejecting it up front, with the valid colors listed, matches
    how every other color-matching spot in this project already handles
    the same class of mistake.

    Locked and atomic like decrement_stock_for_cart/restore_stock_for_order
    above.

    Returns {"status": "ok", "product_name": ..., "color": <matched color
    or None>} on success, or {"error": "..."} if the product doesn't exist
    or the given color isn't one of its real variant colors.
    """
    async with _inventory_lock:
        data = await file_io.read_json(config.INVENTORY_FILE, {"products": []})
        product = next((p for p in data.get("products", []) if p.get("id") == product_id), None)
        if not product:
            return {"error": f"no product with id '{product_id}'"}

        if color:
            color_norm = color.strip().lower()
            variant_colors = {v.get("color", "").strip().lower() for v in product.get("variants", []) if v.get("color")}
            if color_norm not in variant_colors:
                valid = ", ".join(sorted(variant_colors)) if variant_colors else "none on file"
                return {"error": f"'{color}' isn't a color {product_id} comes in (valid: {valid})"}
            product.setdefault("images_by_color", {})[color_norm] = image
            await file_io.write_json_atomic(config.INVENTORY_FILE, data)
            _cache["mtime"] = None
            return {"status": "ok", "product_name": product.get("name", product_id), "color": color_norm}

        product["image"] = image
        await file_io.write_json_atomic(config.INVENTORY_FILE, data)
        _cache["mtime"] = None
        return {"status": "ok", "product_name": product.get("name", product_id), "color": None}