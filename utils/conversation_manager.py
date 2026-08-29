"""
Stores full conversation history per user as JSON (data/conversations/{id}.json)
so the seller can review chats later. Only the last MAX_HISTORY_MESSAGES are
sent to the LLM on each call, to keep latency and context size bounded.

Guarded by a single module-level asyncio.Lock, matching the read-modify-write
pattern already used in cart_manager.py/order_manager.py — see Issue 3.3.
Public functions are async wrappers that acquire _history_lock; the actual
file I/O lives in unlocked `_*_sync` helpers. This split exists because
asyncio.Lock is not reentrant: append_messages()/get_recent_for_llm() both
need to read the current history as part of their own locked operation, and
if the public load_history() tried to re-acquire the same lock from inside
another locked function, it would deadlock. The sync helpers are the only
things that touch the filesystem directly and must never acquire the lock
themselves — only the public async wrappers do that, each exactly once.
"""
import asyncio
import json
import config

_history_lock = asyncio.Lock()

def _path(user_id: int):
    return config.CONVERSATIONS_DIR / f"{user_id}.json"

def _load_history_sync(user_id: int) -> list[dict]:
    """Unlocked file read. Never call this directly outside this module —
    always go through load_history() (or another locked wrapper) so a read
    can't race a concurrent write. Kept separate purely to avoid the
    reentrancy deadlock described above."""
    path = _path(user_id)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _append_messages_sync(user_id: int, messages: list[dict]):
    """Unlocked read-modify-write. Same rule as _load_history_sync — only
    called from within a function that already holds _history_lock."""
    history = _load_history_sync(user_id)
    history.extend(messages)
    with open(_path(user_id), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return history

async def load_history(user_id: int) -> list[dict]:
    async with _history_lock:
        return _load_history_sync(user_id)

async def append_messages(user_id: int, messages: list[dict]):
    """Appends a list of messages (user, assistant, tool, etc.) and persists to disk."""
    async with _history_lock:
        _append_messages_sync(user_id, messages)

async def get_recent_for_llm(user_id: int) -> list[dict]:
    """
    Full history is on disk; only the tail goes into the LLM call.
    We trim based on MAX_HISTORY_MESSAGES, but ensure we don't cut 
    in the middle of a tool call and its result (which would cause 
    orphaned tool_call_ids and API errors).
    """
    async with _history_lock:
        history = _load_history_sync(user_id)

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