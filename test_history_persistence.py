# test_history_persistence.py
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils import conversation_manager

# Override MAX_HISTORY_MESSAGES for testing to force a trim
config.MAX_HISTORY_MESSAGES = 5

async def main():
    print("Starting history persistence and safe-trim test...")
    
    user_id = 99999
    # Clear existing history for a clean test
    path = conversation_manager._path(user_id)
    if path.exists():
        path.unlink()
        
    # Simulate a long conversation with tool calls
    messages = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Result 1"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]
    
    conversation_manager.append_messages(user_id, messages)
    print("1. Saved 8 messages to history.")
    
    # Now get recent for LLM (limit is 5)
    recent = conversation_manager.get_recent_for_llm(user_id)
    
    print(f"2. Retrieved {len(recent)} messages for LLM.")
    print("3. Messages retrieved:")
    for m in recent:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            print(f"   - {role} (with tool_calls)")
        elif role == "tool":
            print(f"   - {role} (result)")
        else:
            print(f"   - {role}: {m.get('content')}")
            
    # Verify no orphaned tool calls
    # The first message should NOT be a 'tool' message or an 'assistant' with tool_calls
    first_role = recent[0].get("role")
    first_has_tools = bool(recent[0].get("tool_calls"))
    
    if first_role == "tool" or (first_role == "assistant" and first_has_tools):
        print("❌ FAILURE: Orphaned tool call detected at the start of the trimmed history!")
    else:
        print("✅ SUCCESS: History safely trimmed without orphaning tool calls!")

if __name__ == "__main__":
    asyncio.run(main())