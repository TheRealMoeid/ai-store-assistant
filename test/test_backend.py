# test/test_backend.py
import sys
from pathlib import Path

# ✅ CRITICAL FIX: Add the project root to sys.path so 'config' and 'utils' can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import json
import os

# Override config paths for testing to avoid touching real data
TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.ORDERS_FILE = TEST_DATA_DIR / "orders.json"
config.FEEDBACK_FILE = TEST_DATA_DIR / "feedback.json"
config.CARTS_FILE = TEST_DATA_DIR / "carts.json"

from utils import inventory_manager, order_manager, cart_manager
from agent.tools import call_tool

@pytest.fixture(autouse=True)
def setup_test_data():
    # 1. Setup mock data
    inv = {
        "products": [
            {
                "id": "p001", "name": "Test Sneakers", "category": "sneakers",
                "variants": [{"size": "42", "color": "black", "stock": 2}]
            }
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f: json.dump(inv, f)
    with open(config.ORDERS_FILE, "w") as f: json.dump([], f)
    with open(config.FEEDBACK_FILE, "w") as f: json.dump([], f)
    with open(config.CARTS_FILE, "w") as f: json.dump({}, f)
    
    # Reset in-memory caches
    inventory_manager._cache["mtime"] = None
    
    yield
    
    # 2. Teardown (clean up test files)
    for f in TEST_DATA_DIR.glob("*.json"): 
        f.unlink()

@pytest.mark.asyncio
async def test_shopping_flow():
    # 1. Search
    res = await call_tool("search_products", {"query": "Sneakers"}, 123, "user")
    # ✅ DEBUG: Print the error if the tool failed
    if "error" in res:
        print(f"\n❌ TOOL ERROR: {res['error']}\n")
    assert res["count"] == 1
    
    # 2. Add to cart
    res = await call_tool("add_to_cart", {"product_id": "p001", "size": "42", "color": "black", "quantity": 1}, 123, "user")
    assert res["status"] == "added"
    
    # 3. Place Order
    res = await call_tool("place_order", {}, 123, "user")
    assert res["status"] == "order_placed"
    order = res["order"]
    
    # 4. Submit Payment
    res = await call_tool("submit_payment_reference", {"reference": "TX12345"}, 123, "user")
    assert res["status"] == "proof_submitted"
    
    # Verify persistence
    with open(config.ORDERS_FILE) as f:
        orders = json.load(f)
    assert orders[0]["payment_proof"] == "TX12345"

@pytest.mark.asyncio
async def test_atomic_stock_validation():
    """Test that if a multi-item order fails on item 2, item 1's stock is NOT lost."""
    inv = {
        "products": [
            {"id": "p001", "name": "Sneakers", "category": "sneakers", "variants": [{"size": "42", "color": "black", "stock": 2}]},
            {"id": "p002", "name": "Shirt", "category": "clothing", "variants": [{"size": "M", "color": "red", "stock": 0}]}
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f: json.dump(inv, f)
    inventory_manager._cache["mtime"] = None

    await cart_manager.set_cart(123, [
        {"product_id": "p001", "size": "42", "color": "black", "quantity": 1},
        {"product_id": "p002", "size": "M", "color": "red", "quantity": 1}
    ])

    # Try to place order. It should FAIL because p002 has 0 stock.
    res = await call_tool("place_order", {}, 123, "user")
    assert "error" in res
    
    # CRITICAL CHECK: p001 stock must still be 2 (not lost)
    with open(config.INVENTORY_FILE) as f:
        inv_after = json.load(f)
    p001_stock_after = inv_after["products"][0]["variants"][0]["stock"]
    
    assert p001_stock_after == 2, f"Stock was lost! Expected 2, got {p001_stock_after}"