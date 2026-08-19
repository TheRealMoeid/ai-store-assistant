# test_cart_persistence.py
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils import cart_manager

async def main():
    print("Starting cart persistence test...")
    
    # 1. Clear existing carts for a clean test
    if config.CARTS_FILE.exists():
        config.CARTS_FILE.unlink()
        
    user_id = 12345
    item = {"product_id": "p001", "size": "42", "color": "black", "quantity": 1}
    
    # 2. Add an item to cart (saves to disk)
    await cart_manager.set_cart(user_id, [item])
    print("1. Added item to cart and saved to disk.")
    
    # 3. Verify it's physically on the disk
    with open(config.CARTS_FILE, "r", encoding="utf-8") as f:
        disk_data = json.load(f)
    print(f"2. Data physically on disk: {disk_data}")
    
    # 4. Simulate a "restart" by loading it back from disk
    loaded_cart = await cart_manager.get_cart(user_id)
    print(f"3. Loaded cart after simulated restart: {loaded_cart}")
    
    if loaded_cart == [item]:
        print("✅ SUCCESS: Cart survived simulated restart!")
    else:
        print("❌ FAILURE: Cart was lost.")

if __name__ == "__main__":
    asyncio.run(main())