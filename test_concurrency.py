# test_concurrency.py
import asyncio
import json
import os
from datetime import datetime, timezone

# Ensure we are using the project's config
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils import order_manager

async def place_mock_order(user_id: int):
    """Simulates a concurrent order placement."""
    items = [{"product_id": "p001", "size": "42", "color": "black", "quantity": 1}]
    return await order_manager.place_order(user_id, f"user_{user_id}", items)

async def main():
    print("Starting concurrency test...")
    
    # Clear existing orders for a clean test
    with open(config.ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)

    num_users = 50
    tasks = [place_mock_order(i) for i in range(num_users)]
    
    # Execute all 50 orders concurrently
    results = await asyncio.gather(*tasks)
    
    # Verify results
    with open(config.ORDERS_FILE, "r", encoding="utf-8") as f:
        saved_orders = json.load(f)
        
    print(f"Attempted to place {num_users} orders concurrently.")
    print(f"Orders successfully saved to file: {len(saved_orders)}")
    
    if len(saved_orders) == num_users:
        print("✅ SUCCESS: No data loss! All concurrent writes were safely locked.")
    else:
        print(f"❌ FAILURE: Data loss detected. Expected {num_users}, got {len(saved_orders)}.")

if __name__ == "__main__":
    asyncio.run(main())