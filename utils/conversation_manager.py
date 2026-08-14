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


def save_message(user_id: int, role: str, content: str):
    """Appends one message (role: 'user' | 'assistant') and persists to disk."""
    history = load_history(user_id)
    history.append({"role": role, "content": content})
    with open(_path(user_id), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_recent_for_llm(user_id: int) -> list[dict]:
    """Full history is on disk; only the tail goes into the LLM call."""
    history = load_history(user_id)
    return history[-config.MAX_HISTORY_MESSAGES :]
