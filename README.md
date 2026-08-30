# Shopmate an AI Sales Agent for Telegram

## 1. Project Overview

**Shopmate** (AI Store Assistant) is a lightweight, conversational AI sales agent for Telegram, powered by an LLM tool-calling loop. It allows customers to browse inventory, check stock, manage a shopping cart, place orders, and submit payment proof using natural language. 

Designed specifically for small-scale, single-seller operations, the project intentionally avoids traditional databases. Instead, it uses flat JSON files for persistence, allowing the seller to manually edit `data/inventory.json` directly without needing an admin panel. Orders are confirmed or rejected by the seller via Telegram commands in a dedicated admin group.

---

## 2. Key Features

- **Natural Language Shopping**: Customers chat naturally to search products, check variant availability (size/color), and build a cart.
- **Persistent State**: Shopping carts and conversation histories are saved to disk, surviving bot restarts without losing customer context.
- **Robust Admin Workflow**: Sellers receive detailed order notifications and manage fulfillment using `/pending_orders`, `/confirm <id>`, and `/reject <id>`.
- **Automated Stock Management**: 
  - Stock is validated and decremented atomically at checkout.
  - Rejected orders automatically and safely restore reserved inventory.
- **Payment Proof Handling**: Customers can submit payment screenshots (processed directly) or type transaction references (processed via the LLM), both of which notify the admin group for review.
- **Swappable LLM Backend**: Seamlessly switch between local development (Ollama) and cloud production (Groq) via environment variables.
- **Self-Correcting AI Agent**: Built-in structural guards intercept LLM hallucinations, recover from malformed tool calls, and force the model to retry if it "narrates" an action without actually executing it.

---

## 3. System Architecture

The system follows a modular, event-driven architecture centered around a highly defensive LLM tool-calling loop.

```text
Telegram User / Admin Group
        ↓
aiogram 3 Dispatcher (bot.py)
        ↓
Handlers (user_handlers.py / admin_handlers.py)
        ↓
AI Agent Loop (agent/agent.py)
        ↓
Tool Execution (agent/tools.py)
        ↓
Business Logic & Persistence (utils/*.py) ←→ asyncio.Lock
        ↓
JSON Data Store (data/*.json)
```

1. **User Interaction**: A user sends a message. `user_handlers.py` intercepts it and passes the text to `run_agent`.
2. **Context Building**: `conversation_manager` loads the user's recent history (safely trimmed to avoid orphaned tool calls) and appends the new message.
3. **LLM Decision**: The agent sends the context and tool schemas to the configured LLM.
4. **Tool Execution**: If the LLM requests a tool, `agent.py` executes it via `call_tool`. The tool interacts with the `utils` layer (e.g., checking `inventory_manager`), returns a structured result, and the loop repeats.
5. **Final Response**: Once the LLM returns a text response without tool calls, it is sent back to the user, and the turn is saved to disk.
6. **Admin Flow**: When an order is placed or payment proof is submitted, the admin group receives a notification. The seller uses `/confirm` or `/reject` to update the order status, which automatically DMs the customer and adjusts inventory.

---

## 4. Project Structure

```text
ai-store-assistant/
├── agent/                      # AI agent core logic
│   ├── agent.py                # Tool-calling loop, recovery guards, narration guard
│   ├── llm_client.py           # Swappable LLM provider (Ollama/Groq)
│   ├── prompts.py              # System prompt rules and constraints
│   └── tools.py                # Tool schemas (OpenAI format) and implementations
├── handlers/                   # Telegram message routing
│   ├── admin_handlers.py       # Admin commands (/pending_orders, /confirm, /reject)
│   └── user_handlers.py        # User message routing and payment screenshot handling
├── utils/                      # Business logic and data management
│   ├── cart_manager.py         # Persistent cart storage with async locks
│   ├── conversation_manager.py # History load, save, safe-trimming, and async locks
│   ├── inventory_manager.py    # Live-reloading inventory reads and atomic stock writes
│   └── order_manager.py        # Order and feedback persistence with async locks
├── data/                       # JSON data storage
│   ├── inventory.json          # Seller-edited product catalog
│   ├── orders.json             # Append-only order log
│   ├── carts.json              # Persistent user carts
│   ├── feedback.json           # Customer compliments/complaints log
│── test/                       # Pytest test suite
├── bot.py                      # Application entrypoint
├── config.py                   # Settings loaded from .env
├── requirements.txt            # Python dependencies
└── .env.example                # Environment variable template
```

---

## 5. Technologies and Dependencies

- **Python 3.11 / 3.12**: Required runtime. *(Note: Python 3.14+ is currently unsupported due to missing prebuilt wheels for `pydantic-core`, an `aiogram`/`openai` dependency).*
- **aiogram (3.x)**: Asynchronous Telegram Bot API framework for routing and message handling.
- **openai (Python SDK)**: Used to interact with both Groq and Ollama, as both expose OpenAI-compatible API endpoints.
- **python-dotenv**: Loads environment variables from `.env` securely.
- **pytest / pytest-asyncio**: For running the automated test suite.
- **JSON**: File-based persistence for all state, chosen for simplicity and direct seller editability.

---

## 6. Setup and Installation

### Prerequisites
- Python 3.11 or 3.12 installed.
- For local development: [Ollama](https://ollama.com/) installed and running.

### Steps

1. **Clone the repository:**
   ```bash
   git clone https://github.com/TheRealMoeid/ai-store-assistant.git
   cd ai-store-assistant
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your specific values (see Configuration section below).

---

## 7. Configuration

Edit the `.env` file. Never commit actual secrets or real user IDs to version control.

```env
# --- Telegram ---
BOT_TOKEN=your_telegram_bot_token_here
ADMIN_GROUP_ID=-1001234567890   # Must be a negative number (the chat_id of your admin group)

# Optional: comma-separated user_ids allowed to run admin commands from a private DM
ADMIN_USER_IDS=

# --- LLM Provider: "ollama" or "groq" ---
LLM_PROVIDER=ollama

# Used when LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen3:8b           # Recommended for local tool-calling reliability

# Used when LLM_PROVIDER=groq
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile

# --- History ---
MAX_HISTORY_MESSAGES=20         # Number of recent messages sent to the LLM to bound context size
```

*Note on `ADMIN_GROUP_ID`*: Add the bot to your Telegram group, send any message, and visit `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` (ensure the bot script is **not** running while you check this). The group's `chat.id` (a negative integer) is your `ADMIN_GROUP_ID`.*

---

## 8. Running the Project

Ensure your virtual environment is activated and your `.env` is configured.

**For Local Development (Ollama)**:
1. Start Ollama: `ollama serve`
2. Pull the recommended model: `ollama pull qwen3:8b`
3. Run the bot:
   ```bash
   python bot.py
   ```

**For Production (Groq)**:
1. Ensure `LLM_PROVIDER=groq` and `GROQ_API_KEY` are set in `.env`.
2. Run the bot:
   ```bash
   python bot.py
   ```

---

## 9. Running Tests

The project includes a comprehensive suite of targeted tests to verify critical architectural guarantees, concurrency safety, and agent guardrails. Tests are located in the `test/` directory and root directory.

To run the full test suite:
```bash
pytest . -v
```
*(Note: On Windows PowerShell, use `pytest . -v` instead of glob patterns like `pytest test_*.py` to ensure proper test discovery.)*

**Test Coverage Highlights:**
- **Concurrency & Atomicity**: Verifies that `asyncio.Lock` prevents race conditions during simultaneous order placements (`test_concurrency.py`) and multi-item stock decrements (`test_multi_item_checkout.py`).
- **Persistence**: Confirms carts and conversation history survive restarts and lock correctly under concurrent load.
- **Agent Guards**: Validates that the narration guard intercepts hallucinated actions, malformed JSON tool calls are safely recovered, and payment reference events correctly trigger admin notifications.
- **Stock Reversal**: Ensures rejected orders correctly restore inventory atomically.

*Warning: Some standalone root-level scripts (e.g., `test_concurrency.py`) write directly to real `data/` JSON files when run outside the sandboxed `pytest` suite. Always check `git status` before committing to avoid accidentally committing synthetic test data.*

---

## 10. AI Agent & Reliability Engineering

The AI agent is engineered for maximum reliability with smaller, local models, incorporating several defensive patterns:

- **Tool-Calling Loop**: Managed in `agent/agent.py`, it iterates up to `MAX_TOOL_ROUNDS`, sending context and tool schemas to the LLM, executing requested tools, and feeding results back until a final text response is generated.
- **Malformed Call Recovery**: 
  - *Leaked JSON*: If the model outputs tool call JSON as plain text, `_parse_leaked_json_tool_call` structurally parses and executes it.
  - *Native Tags*: If the model outputs proprietary tags (e.g., `<function=...>`), `_parse_native_function_call` uses `json.JSONDecoder.raw_decode()` to safely extract nested arguments without fragile regex matching.
- **Narration Guard**: A structural guard scans the model's text output for commitment phrases (e.g., "I added that to your cart"). If detected without a corresponding tool call, the agent injects a corrective system message and loops back (`continue`), forcing the model to retry with the actual tool instead of returning a false confirmation to the user.
- **Concurrency Safety**: All persistence managers (`cart`, `order`, `inventory`, `conversation`) use `asyncio.Lock` to prevent race conditions during read-modify-write cycles.
- **Secure Context Injection**: `user_id` and `username` are injected into the tool execution context by the Telegram dispatcher, never parsed from the LLM's arguments, preventing spoofing.
- **Safe History Trimming**: `conversation_manager.py` persists the full conversation to disk but intelligently trims the payload sent to the LLM. It ensures trimming never splits a `tool_call` and its corresponding `tool` result, preventing orphaned API errors.

---

## 11. Current Status and Known Limitations

The core architecture is stable and heavily hardened against common LLM failure modes. 

### Fully Implemented & Hardened
- ✅ Concurrent read-modify-write race conditions prevented via `asyncio.Lock` across all managers.
- ✅ Ephemeral in-memory carts replaced with persistent disk storage.
- ✅ Malformed tool call recovery uses robust JSON decoding.
- ✅ Narration guard correctly forces self-correction loops.
- ✅ Admin authorization gates all sensitive commands.
- ✅ Stock reversal on order rejection.
- ✅ Atomic multi-item stock decrement at checkout.
- ✅ Admin notifications for typed payment references.

### By Design
- **No Database**: Flat JSON files are used intentionally so the seller can edit `inventory.json` directly via text editor.
- **No Payment Gateway**: Relies on manual screenshot/reference review by the seller.
- **Keyword Search**: Product search is substring/keyword-based; fuzzy matching and product images are not yet implemented.
- **Basic Order Status**: Limited to `pending_confirmation`, `awaiting_review`, `confirmed`, and `rejected`.

### Backlog / Known Limitations
- **Atomic Cart-Consumption**: The full `_place_order` sequence (read cart → validate → decrement stock → create order → clear cart) is not wrapped in a single overarching lock, meaning near-simultaneous checkout requests from the *same* user could theoretically race.
- **Substring Color Matching**: Color matching uses `in` rather than exact `==`, which could cause inventory drift on rejection if overlapping color names exist (e.g., "Red" vs "Red/Blue").
- **Hardcoded Recovery IDs**: Synthetic `tool_call_id`s used in malformed-call recovery are static strings; multiple recovery events in one conversation could theoretically collide.
- **Sync File I/O**: JSON reads/writes use synchronous `open()` inside `async` functions, which blocks the event loop (acceptable at current single-seller scale).
- **Payment Proof Resubmission**: Customers cannot easily overwrite a wrong payment screenshot once an order moves to `awaiting_review`.

---

## 12. Development Conventions

If you wish to extend or modify the project, follow these established guidelines:

1. **Prompt-Level Fixes First**: If the LLM exhibits a new failure mode (e.g., hallucinating shipping times), update `agent/prompts.py` first. Prompt-level fixes are faster and cheaper than code-level workarounds.
2. **Whole-File Replacement**: When making multi-line changes, prefer regenerating and handing over the full file content over patch/diff files, which frequently fail to apply cleanly against working trees with local drift.
3. **Never Trust the LLM for Context**: Never parse `user_id` or `username` from model tool-call arguments. Always rely on the context injected by the Telegram handler.
4. **Minimal, Structurally-Scoped Fixes**: Prefer small fixes that don't touch already-working, already-tested code paths unless deduplication is strictly required to solve the issue at hand.
5. **Separate Unrelated Changes**: Never bundle unrelated local edits (e.g., stray type-checker suppressions) into a feature branch commit.

---

## 13. License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
