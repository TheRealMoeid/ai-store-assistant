# test_nested_json_regex.py
import json
import re

# Paste the exact function here to test it in isolation
def _parse_native_function_call(failed_generation: str) -> tuple[str, dict] | None:
    match = re.search(r'<function=([\w_]+)\s*=?\s*\{', failed_generation)
    if not match:
        match = re.search(r'([\w_]+)\s*=\s*\{', failed_generation)
        
    if not match:
        return None
        
    name = match.group(1)
    json_start = match.end() - 1  
    
    decoder = json.JSONDecoder()
    try:
        args, _ = decoder.raw_decode(failed_generation, json_start)
        if not isinstance(args, dict):
            return None
        return name, args
    except json.JSONDecodeError:
        return None

def main():
    print("Testing nested JSON and escaped character recovery...")
    
    # 1. Standard flat JSON (Original behavior must still work)
    text1 = '<function=search_products{"query": "shoes"}</function>'
    assert _parse_native_function_call(text1) == ("search_products", {"query": "shoes"})
    
    # 2. Nested JSON (The Bug #6 case that previously broke)
    text2 = '<function=place_order{"items": [{"id": "p1", "qty": 2}], "total": 50}</function>'
    assert _parse_native_function_call(text2) == ("place_order", {"items": [{"id": "p1", "qty": 2}], "total": 50})
    
    # 3. Escaped quotes inside strings (Another edge case regex struggles with)
    text3 = 'search_products={"query": "red \\"special\\" shoes"}'
    assert _parse_native_function_call(text3) == ("search_products", {"query": "red \"special\" shoes"})
    
    # 4. Invalid JSON should safely return None
    text4 = 'search_products={"query": "shoes"' # Missing closing brace
    assert _parse_native_function_call(text4) is None
    
    # 5. Completely unrelated text should safely return None
    text5 = "Hello, how can I help you today?"
    assert _parse_native_function_call(text5) is None
    
    print("✅ SUCCESS: Nested JSON and edge cases handled perfectly!")

if __name__ == "__main__":
    main()