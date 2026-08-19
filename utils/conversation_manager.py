"""
Stores full conversation history per user as JSON (data/conversations/{id}.json)
so the seller can review chats later. Only the last MAX_HISTORY_MESSAGES are
sent to the LLM on each call, to keep latency and context size bounded.
"""
import json
import config

def _path(user_id: int):
    return config.CONVERSATIONS_DIR / f"{user_id}.json"

def load_history(user_id: int) -> list[dict]:
    path = _path(user_id)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def append_messages(user_id: int, messages: list[dict]):
    """Appends a list of messages (user, assistant, tool, etc.) and persists to disk."""
    history = load_history(user_id)
    history.extend(messages)
    with open(_path(user_id), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def get_recent_for_llm(user_id: int) -> list[dict]:
    """
    Full history is on disk; only the tail goes into the LLM call.
    We trim based on MAX_HISTORY_MESSAGES, but ensure we don't cut 
    in the middle of a tool call and its result (which would cause 
    orphaned tool_call_ids and API errors).
    """
    history = load_history(user_id)
    if len(history) <= config.MAX_HISTORY_MESSAGES:
        return history
    
    # Take the last MAX_HISTORY_MESSAGES
    trimmed = history[-config.MAX_HISTORY_MESSAGES:]
    
    # If the first message is a tool result or an assistant message with tool_calls,
    # it's orphaned. Drop messages from the front until we hit a clean start
    # (a 'user' message, or an 'assistant' message without tool_calls).
    while trimmed:
        first = trimmed[0]
        role = first.get("role")
        has_tool_calls = bool(first.get("tool_calls"))
        
        if role == "tool" or (role == "assistant" and has_tool_calls):
            trimmed.pop(0)
        else:
            break
            
    return trimmed