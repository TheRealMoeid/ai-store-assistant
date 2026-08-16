import pytest
import json
import os
from pathlib import Path

# Override config paths for testing to avoid touching real data
TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.ORDERS_FILE = TEST_DATA_DIR / "orders.json"
config.FEEDBACK_FILE = TEST_DATA_DIR / "feedback.json"

from utils import inventory_manager, order_manager
from agent.tools import call_tool

@pytest.fixture(autouse=True)
def setup_test_data():
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
    inventory_manager._cache["mtime"] = None  # Reset cache
    yield
    for f in TEST_DATA_DIR.glob("*.json"): f.unlink()

def test_shopping_flow():
    # 1. Search
    res = call_tool("search_products", {"query": "Sneakers"}, 123, "user")
    assert res["count"] == 1
    
    # 2. Add to cart
    res = call_tool("add_to_cart", {"product_id": "p001", "size": "42", "color": "black", "quantity": 1}, 123, "user")
    assert res["status"] == "added"
    
    # 3. Place Order
    res = call_tool("place_order", {}, 123, "user")
    assert res["status"] == "order_placed"
    order = res["order"]
    
    # 4. Submit Payment
    res = call_tool("submit_payment_reference", {"reference": "TX12345"}, 123, "user")
    assert res["status"] == "proof_submitted"
    
    # Verify persistence
    with open(config.ORDERS_FILE) as f:
        orders = json.load(f)
    assert orders[0]["payment_proof"] == "TX12345"