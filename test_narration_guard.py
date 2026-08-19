# test_narration_guard.py

def check_narration(reply: str) -> bool:
    """Returns True if the reply triggers the narration guard (i.e., it's a 'lie')."""
    commitment_phrases = [
        "i've added", "i have added", "i'll add", "i will add", "let me add",
        "added to your cart", "added to cart",
        "order is placed", "order placed", "order is confirmed", "order confirmed",
        "order is processing", "order processing"
    ]
    reply_lower = reply.lower()
    is_negative = any(neg in reply_lower for neg in [
        "not added", "haven't added", "didn't add", 
        "not placed", "haven't placed", "didn't place",
        "not confirmed", "haven't confirmed", "didn't confirm"
    ])
    
    return not is_negative and any(phrase in reply_lower for phrase in commitment_phrases)

def main():
    print("Testing narration guard logic...")
    
    # 1. These SHOULD trigger the guard (The model is lying/narrating)
    assert check_narration("I've added the sneakers to your cart!") == True
    assert check_narration("Okay, I'll add that to your cart.") == True
    assert check_narration("Your order is placed. Please wait.") == True
    assert check_narration("The order is processing now.") == True
    assert check_narration("I have added it to cart.") == True
    
    # 2. These SHOULD NOT trigger the guard (Truths, Denials, or Questions)
    assert check_narration("I haven't added it to your cart yet.") == False
    assert check_narration("I didn't place the order because it's out of stock.") == False
    assert check_narration("What would you like to order?") == False
    assert check_narration("The sneakers are available in size 42.") == False
    assert check_narration("I can't add it to your cart right now.") == False
    
    print("✅ SUCCESS: Narration guard correctly identifies lies and ignores denials!")

if __name__ == "__main__":
    main()