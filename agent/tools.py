"""
Everything the LLM is allowed to *do*, defined as OpenAI-style function schemas,
plus the Python implementations behind them.

Carts are persisted to disk via utils/cart_manager.py so they survive restarts.
Orders and feedback are persisted via utils/order_manager.py.

user_id / username are never taken from the model's arguments -- they're
injected by dispatch() from the real Telegram context, so the model can't
spoof who's ordering.
"""
import json
import asyncio
from utils import inventory_manager, order_manager, cart_manager

# Serializes the entire read -> validate -> decrement -> create-order -> clear
# sequence in _place_order across ALL users. See the full docstring history
# in CLAUDE.md ("Checkout atomicity", Issue #10 / bug-audit 3.1) for why
# this is a single global lock held for the whole sequence, with the cart
# cleared only on success.
_checkout_lock = asyncio.Lock()

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the store's inventory by keyword and/or category. Use this whenever the customer asks what's available, browses, or mentions an item by name. Always call this first to get a product's exact id before calling get_product_details or check_availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search, e.g. product name or keyword. Leave empty to browse a whole category."},
                    "category": {"type": "string", "description": "e.g. 'sneakers', 'clothing'. Leave empty to search all categories."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get full details (description, price, all variants) for one specific product by its id. If the customer wants to see a specific color's photo, pass it as `color` -- otherwise the product's general photo (if any) is used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The exact 'id' field from search_products results, e.g. 'p001'. Never the product name -- call search_products first if you don't have the id.",
                    },
                    "color": {
                        "type": "string",
                        "description": "Optional. A specific color the customer wants to see a photo of (must be one of the product's real colors). Leave empty for the product's general photo.",
                    },
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check stock for a product, optionally filtered by size and/or color. Use this before promising an item is in stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The exact 'id' field from search_products results, e.g. 'p001'. Never the product name.",
                    },
                    "size": {"type": "string", "description": "Optional. Leave empty to see all sizes."},
                    "color": {"type": "string", "description": "Optional. Leave empty to see all colors."},
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Add an item to the customer's cart after confirming it's in stock with check_availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The exact 'id' field from search_products results, e.g. 'p001'. Never the product name.",
                    },
                    "size": {"type": "string"},
                    "color": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["product_id", "size", "color", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_cart",
            "description": "See everything currently in the customer's cart.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": "Finalize and submit the customer's current cart as an order. Only call this after the customer explicitly confirms they want to check out.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_payment_reference",
            "description": "Record a payment transaction ID/reference the customer typed after checkout, attaching it to their most recent order awaiting payment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reference": {"type": "string", "description": "The transaction ID or payment reference text, in the customer's own words."},
                },
                "required": ["reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_feedback",
            "description": "Record a compliment or complaint from the customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["compliment", "complaint"]},
                    "message": {"type": "string", "description": "The feedback in the customer's own words (translate to a clear summary if needed)."},
                },
                "required": ["kind", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the status of the customer's own order (e.g. awaiting payment, confirmed, shipped, delivered, rejected). Use this whenever the customer asks about their order, where it is, tracking, or delivery status -- never guess or invent a status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Optional. The specific order id to check, if the customer gave one (e.g. 'ord_...'). Leave empty to look up their most recent order.",
                    },
                },
            },
        },
    },
]

def _resolve_product_id(raw_id: str) -> tuple[dict | None, dict | None]:
    """
    Looks up a product by id. If that fails, tries treating raw_id as a
    name/keyword instead (models sometimes pass the product name). Returns
    (product, error) -- exactly one of which is non-None.

    NOTE: the product dict this returns, when resolved via the
    search_products fallback below, is a search-result shallow copy with
    raw image data already stripped (see inventory_manager._strip_raw_image)
    -- callers that need the real `image`/`images_by_color` fields (i.e.
    _get_product_details) must re-fetch by id via get_product_by_id rather
    than trust this return value directly. _get_product_details does this;
    see its own comment for why.
    """
    product = inventory_manager.get_product_by_id(raw_id)
    if product:
        return product, None

    matches = inventory_manager.search_products(query=raw_id)
    if len(matches) == 1:
        return matches[0], None

    if len(matches) > 1:
        return None, {
            "error": "ambiguous product_id",
            "hint": (
                f"'{raw_id}' matched multiple products. Call search_products to see "
                "them and use the exact 'id' field of the one the customer means."
            ),
            "candidates": [{"id": p["id"], "name": p["name"]} for p in matches],
        }

    return None, {
        "error": "product not found",
        "hint": (
            f"'{raw_id}' is not a valid product id. product_id must be the exact "
            "'id' field from search_products results (e.g. 'p001'), not the product "
            "name. Call search_products first to get the correct id."
        ),
    }

async def _search_products(args: dict, ctx: dict) -> dict:
    results = inventory_manager.search_products(args.get("query", ""), args.get("category", ""))
    return {"count": len(results), "products": results}

async def _get_product_details(args: dict, ctx: dict) -> dict:
    product, error = _resolve_product_id(args.get("product_id", ""))
    if error:
        return error

    # _resolve_product_id may have resolved via the search_products
    # fallback path, whose results have `image`/`images_by_color` already
    # stripped (they're search-listing shallow copies, not the raw stored
    # product -- see inventory_manager._strip_raw_image). Re-fetching by
    # the now-known real id guarantees we're resolving a photo against the
    # genuine stored data regardless of which path got us here -- without
    # this, a product found via the name-fallback path would always look
    # imageless even when it has real photos on file.
    raw_product = inventory_manager.get_product_by_id(product["id"]) # pyright: ignore[reportOptionalSubscript]
    if not raw_product:
        return product  # shouldn't happen (resolved a moment ago), but don't crash if it does

    requested_color = (args.get("color") or "").strip()
    image, resolved_color = inventory_manager.get_image_for_color(raw_product, requested_color or None)

    result = {k: v for k, v in raw_product.items() if k != "images_by_color"}
    if image:
        result["image"] = image
        if resolved_color:
            result["image_color"] = resolved_color
        # image present but no image_color: the customer gets the
        # product's GENERAL photo, not necessarily the exact color they
        # may have asked for -- agent/prompts.py tells the model to be
        # honest about that distinction rather than imply a color match
        # that didn't happen.
    else:
        result.pop("image", None)

    return result

async def _check_availability(args: dict, ctx: dict) -> dict:
    product, error = _resolve_product_id(args.get("product_id", ""))
    if error:
        return error
    variants = inventory_manager.check_variant(
        product["id"], args.get("size", ""), args.get("color", "") # type: ignore
    )
    if not variants:
        return {
            "variants": [],
            "hint": f"No variants matched that size/color for '{product['name']}'. Try omitting size or color to see all options.", # pyright: ignore[reportOptionalSubscript]
        }
    return {"variants": variants}

async def _add_to_cart(args: dict, ctx: dict) -> dict:
    product, error = _resolve_product_id(args.get("product_id", ""))
    if error:
        return error

    variants = inventory_manager.check_variant(product["id"], args["size"], args["color"]) # type: ignore
    if not variants:
        return {
            "error": "no such variant exists",
            "hint": f"No '{args['size']}' / '{args['color']}' combo found for '{product['name']}'. Call check_availability to see valid options.", # type: ignore
        }

    requested_qty = args["quantity"]
    total_stock = sum(v["stock"] for v in variants)
    if total_stock < requested_qty:
        return {
            "error": "insufficient stock",
            "hint": (
                f"Only {total_stock} unit(s) of '{product['name']}' available in " # type: ignore
                f"'{args['size']}' / '{args['color']}', but the customer wants {requested_qty}. "
                "Tell them how many are actually available and ask if they'd like to adjust the quantity."
            ),
        }

    cart = await cart_manager.get_cart(ctx["user_id"])

    for item in cart:
        if (item["product_id"], item["size"], item["color"]) == (product["id"], args["size"], args["color"]): # type: ignore
            combined_qty = item["quantity"] + requested_qty
            if combined_qty > total_stock:
                return {
                    "error": "insufficient stock",
                    "hint": (
                        f"'{item['quantity']}' of this item is already in the cart, and adding "
                        f"{requested_qty} more would need {combined_qty}, but only {total_stock} "
                        "are available in total. Tell the customer the cart already has some, and "
                        "how many more (if any) they can add."
                    ),
                }
            item["quantity"] = combined_qty
            await cart_manager.set_cart(ctx["user_id"], cart)
            return {
                "status": "already_in_cart",
                "hint": f"This item was already in the cart -- quantity increased to {item['quantity']}.",
                "cart": cart,
            }

    cart.append(
        {
            "product_id": product["id"], # type: ignore
            "size": args["size"],
            "color": args["color"],
            "quantity": requested_qty,
        }
    )
    await cart_manager.set_cart(ctx["user_id"], cart)
    return {"status": "added", "cart": cart}

async def _view_cart(args: dict, ctx: dict) -> dict:
    cart = await cart_manager.get_cart(ctx["user_id"])
    return {"cart": cart}

async def _place_order(args: dict, ctx: dict) -> dict:
    async with _checkout_lock:
        cart = await cart_manager.get_cart(ctx["user_id"])
        if not cart:
            return {
                "error": "cart is empty",
                "hint": "You haven't actually called add_to_cart yet -- do that first with the item(s) the customer wants, then call place_order again.",
            }

        shortfalls = []
        for item in cart:
            variants = inventory_manager.check_variant(item["product_id"], item["size"], item["color"])
            available = sum(v["stock"] for v in variants)
            if available < item["quantity"]:
                shortfalls.append(
                    {
                        "product_id": item["product_id"],
                        "size": item["size"],
                        "color": item["color"],
                        "requested": item["quantity"],
                        "available": available,
                    }
                )

        if shortfalls:
            return {
                "error": "stock changed since items were added to cart",
                "hint": (
                    "One or more cart items no longer have enough stock. Tell the customer "
                    "exactly which item(s) and how many are actually available now, and ask "
                    "them how they'd like to adjust their cart. Do not place the order."
                ),
                "shortfalls": shortfalls,
            }

        result = await inventory_manager.decrement_stock_for_cart(cart)
        if result.get("error"):
            return {
                "error": "could not reserve stock while placing order",
                "hint": f"Stock changed unexpectedly. {result.get('hint', 'Please try adjusting your cart.')}",
                "detail": result,
            }

        order = await order_manager.place_order(ctx["user_id"], ctx["username"], cart)
        await cart_manager.set_cart(ctx["user_id"], [])

        return {"status": "order_placed", "order": order}

async def _submit_payment_reference(args: dict, ctx: dict) -> dict:
    order = await order_manager.get_latest_unpaid_order(ctx["user_id"])
    if not order:
        return {"error": "no order awaiting payment for this customer"}
    updated = await order_manager.attach_payment_proof(order["order_id"], args["reference"])
    return {"status": "proof_submitted", "order": updated}

async def _log_feedback(args: dict, ctx: dict) -> dict:
    entry = await order_manager.log_feedback(ctx["user_id"], ctx["username"], args["kind"], args["message"])
    return {"status": "logged", "feedback": entry}

async def _check_order_status(args: dict, ctx: dict) -> dict:
    """
    Looks up either a specific order (by id) or the customer's most recent
    order overall. Security: an order_id supplied by the model is still
    verified to belong to THIS user_id before anything is returned -- see
    full reasoning in this function's earlier version (Issue: order status).
    """
    order_id = (args.get("order_id") or "").strip()

    if order_id:
        order = await order_manager.get_order(order_id)
        if not order or order.get("user_id") != ctx["user_id"]:
            return {"error": "no order found with that id for this customer"}
    else:
        order = await order_manager.get_latest_order_for_user(ctx["user_id"])
        if not order:
            return {"error": "this customer has no orders yet"}

    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "items": order["items"],
        "created_at": order.get("created_at"),
    }

_DISPATCH = {
    "search_products": _search_products,
    "get_product_details": _get_product_details,
    "check_availability": _check_availability,
    "add_to_cart": _add_to_cart,
    "view_cart": _view_cart,
    "place_order": _place_order,
    "submit_payment_reference": _submit_payment_reference,
    "log_feedback": _log_feedback,
    "check_order_status": _check_order_status,
}

async def call_tool(name: str, args: dict, user_id: int, username: str) -> dict:
    ctx = {"user_id": user_id, "username": username}
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool '{name}'"}
    try:
        return await fn(args, ctx)
    except KeyError as e:
        return {"error": f"missing required argument: {e}"}
    except Exception as e:
        return {"error": f"tool failed: {e}"}