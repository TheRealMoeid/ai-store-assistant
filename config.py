"""
Central place for all configuration. Every other module reads settings
from here instead of touching os.environ directly, so switching providers
or tweaking behavior later is a one-file change.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONVERSATIONS_DIR = DATA_DIR / "conversations"

INVENTORY_FILE = DATA_DIR / "inventory.json"
ORDERS_FILE = DATA_DIR / "orders.json"
FEEDBACK_FILE = DATA_DIR / "feedback.json"
CARTS_FILE = DATA_DIR / "carts.json"
ORDERS_FILE = DATA_DIR / "orders.json"
# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))

# Optional: comma-separated Telegram user IDs allowed to run admin commands
# from a private chat with the bot, in addition to the admin group itself.
# e.g. ADMIN_USER_IDS=111111111,222222222
# Non-numeric entries (e.g. a stray trailing comment) are skipped rather
# than crashing startup.
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
    if uid.strip().lstrip("-").isdigit()
}

# --- LLM provider ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()  # "ollama" | "groq"

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# --- Conversation memory ---
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

# Make sure the data folders exist on first run
DATA_DIR.mkdir(exist_ok=True)
CONVERSATIONS_DIR.mkdir(exist_ok=True)