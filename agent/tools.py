"""
Everything the LLM is allowed to *do*, defined as OpenAI-style function schemas,
plus the Python implementations behind them.

Carts are kept in memory per user_id (ephemeral — cleared once an order is
placed). Orders and feedback are persisted via utils/order_manager.py.

user_id / username are never taken from the model's arguments — they're
injected by dispatch() from the real Telegram context, so the model can't
spoof who's ordering.
"""
from utils import inventory_manager, order_manager

# user_id -> list of {product_id, size, color, quantity}
_carts: dict[int, list[dict]] = {}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the store's inventory by keyword and/or category. Use this whenever the customer asks what's available, browses, or mentions an item by name.",
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
            "description": "Get full details (description, price, all variants) for one specific product by its id.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
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
                    "product_id": {"type": "string"},
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
                    "product_id": {"type": "string"},
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
]


def _search_products(args: dict, ctx: dict) -> dict:
    results = inventory_manager.search_products(args.get("query", ""), args.get("category", ""))
    return {"count": len(results), "products": results}


def _get_product_details(args: dict, ctx: dict) -> dict:
    product = inventory_manager.get_product_by_id(args["product_id"])
    return product or {"error": "product not found"}


def _check_availability(args: dict, ctx: dict) -> dict:
    variants = inventory_manager.check_variant(
        args["product_id"], args.get("size", ""), args.get("color", "")
    )
    return {"variants": variants}


def _add_to_cart(args: dict, ctx: dict) -> dict:
    variants = inventory_manager.check_variant(args["product_id"], args["size"], args["color"])
    in_stock = any(v["stock"] > 0 for v in variants)
    if not variants:
        return {"error": "no such variant exists"}
    if not in_stock:
        return {"error": "out of stock for that size/color"}

    cart = _carts.setdefault(ctx["user_id"], [])
    cart.append(
        {
            "product_id": args["product_id"],
            "size": args["size"],
            "color": args["color"],
            "quantity": args["quantity"],
        }
    )
    return {"status": "added", "cart": cart}


def _view_cart(args: dict, ctx: dict) -> dict:
    return {"cart": _carts.get(ctx["user_id"], [])}


def _place_order(args: dict, ctx: dict) -> dict:
    cart = _carts.get(ctx["user_id"], [])
    if not cart:
        return {"error": "cart is empty"}
    order = order_manager.place_order(ctx["user_id"], ctx["username"], cart)
    _carts[ctx["user_id"]] = []
    return {"status": "order_placed", "order": order}


def _log_feedback(args: dict, ctx: dict) -> dict:
    entry = order_manager.log_feedback(ctx["user_id"], ctx["username"], args["kind"], args["message"])
    return {"status": "logged", "feedback": entry}


_DISPATCH = {
    "search_products": _search_products,
    "get_product_details": _get_product_details,
    "check_availability": _check_availability,
    "add_to_cart": _add_to_cart,
    "view_cart": _view_cart,
    "place_order": _place_order,
    "log_feedback": _log_feedback,
}


def call_tool(name: str, args: dict, user_id: int, username: str) -> dict:
    ctx = {"user_id": user_id, "username": username}
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(args, ctx)
    except KeyError as e:
        # The model called the tool but left out a required argument.
        # Return this as a normal tool result (not a crash) so the model
        # sees it and can retry with the missing field or ask the customer.
        return {"error": f"missing required argument: {e}"}
    except Exception as e:  # last-resort guard around any tool bug
        return {"error": f"tool failed: {e}"}
