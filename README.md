# AI Store Assistant

An `aiogram` 3 Telegram bot powered by an LLM tool-calling agent. Customers chat naturally to search inventory, check stock, manage a cart, place orders, and submit payment proof. The seller manages inventory by directly editing a JSON file and confirms or rejects orders via Telegram commands in an admin group. 

By design, this is a lightweight, small-scale project that uses plain JSON files for persistence, eliminating the need for a traditional database and keeping the inventory fully editable by the seller.

---

## 1. Project Overview

The AI Store Assistant solves the problem of managing a small Telegram-based store without complex backend infrastructure or manual order-taking. Instead of navigating rigid menus, customers interact with a conversational AI agent that understands natural language, queries the store's inventory, and guides the user through a complete checkout flow. The main goal is to provide a reliable, self-hosted, and easily configurable sales agent that requires minimal maintenance.

---

## 2. Features

The following features are fully implemented and operational:

- **Natural Language Search**: Customers can browse or search for products by name or category.
- **Real-Time Availability Checking**: Validates stock levels for specific variants (e.g., size, color) before allowing cart additions.
- **Persistent Shopping Cart**: Carts are saved to disk, surviving bot restarts without losing customer state.
- **Order Placement & Payment Proof**: Customers can finalize orders and submit transaction references or payment screenshots.
- **Admin Management**: Sellers receive notifications in a designated Telegram group and can manage orders using `/pending_orders`, `/confirm <id>`, and `/reject <id>`.
- **Live Inventory Reloading**: The bot automatically detects and applies changes to `data/inventory.json` without requiring a restart.
- **Robust Error Recovery**: Built-in guards prevent LLM hallucinations, recover from malformed tool calls, and safely manage conversation history.
- **Swappable LLM Backend**: Seamlessly switch between local development (Ollama) and production (Groq) via environment variables.

---

## 3. Architecture

The system follows a modular, event-driven architecture centered around the LLM tool-calling loop.

```text
Telegram User / Admin
        ↓
aiogram Dispatcher (bot.py)
        ↓
Handlers (user_handlers.py / admin_handlers.py)
        ↓
AI Agent Loop (agent/agent.py)
        ↓
Tool Execution (agent/tools.py)
        ↓
Business Logic & Persistence (utils/*.py)
        ↓
JSON Data Store (data/*.json)
```

---

## 4. Project Structure

```text
ai-store-assistant/
├── agent/                      # AI agent core logic
│   ├── agent.py                # Tool-calling loop and recovery guards
│   ├── llm_client.py           # Swappable LLM provider (Ollama/Groq)
│   ├── prompts.py              # System prompt rules and constraints
│   └── tools.py                # Tool schemas (OpenAI format) and implementations
├── handlers/                   # Telegram message routing
│   ├── admin_handlers.py       # Admin commands (/pending_orders, etc.)
│   └── user_handlers.py        # User message routing and payment screenshot handling
├── utils/                      # Business logic and data management
│   ├── cart_manager.py         # Persistent cart storage
│   ├── conversation_manager.py # History load, save, and safe-trimming
│   ├── inventory_manager.py    # Live-reloading inventory reads
│   └── order_manager.py        # Order and feedback persistence with async locks
├── data/                       # JSON data storage (gitignored in production)
│   ├── inventory.json          # Seller-edited product catalog
│   ├── orders.json             # Append-only order log
│   ├── carts.json              # Persistent user carts
│   ├── feedback.json           # Customer compliments/complaints log
│   └── conversations/          # Per-user chat history (JSON)
├── tests/                      # Test suite directory
├── bot.py                      # Application entrypoint
├── config.py                   # Settings loaded from .env
├── requirements.txt            # Python dependencies
└── .env.example                # Environment variable template
```

---

## 5. How It Works

1. **Startup**: `bot.py` initializes the `aiogram` Dispatcher, loads configuration from `.env`, and registers the admin and user routers.
2. **User Interaction**: A user sends a message. `user_handlers.py` intercepts it and passes the text to `run_agent` in `agent.py`.
3. **Context Building**: `conversation_manager.py` loads the user's recent history, safely trimmed to avoid orphaned tool calls, and appends the new message.
4. **LLM Decision**: The agent sends the context and `TOOL_SCHEMAS` to the configured LLM. 
5. **Tool Execution**: If the LLM requests a tool, `agent.py` executes it via `call_tool` in `tools.py`. The tool interacts with the `utils` layer (e.g., checking `inventory_manager`), returns a structured result, and the loop repeats.
6. **Final Response**: Once the LLM returns a text response without tool calls, it is sent back to the user, and the turn is saved to disk.
7. **Admin Flow**: When an order is placed, the admin group receives a notification. The seller uses `/confirm` or `/reject` to update the order status, which automatically DMs the customer.

---

## 6. Technologies Used

- **Python 3.11 / 3.12**: Required runtime. (Avoid Python 3.14+ due to missing `pydantic-core` prebuilt wheels).
- **aiogram (3.x)**: Asynchronous Telegram Bot API framework for routing and message handling.
- **openai (Python SDK)**: Used to interact with both Groq and Ollama, as both expose OpenAI-compatible API endpoints.
- **python-dotenv**: Loads environment variables from `.env` securely.
- **JSON**: File-based persistence for all state, chosen for simplicity and direct seller editability.

---

## 7. Installation

### Prerequisites
- Python 3.11 or 3.12
- `pip` and `venv`
- For local development: [Ollama](https://ollama.com/) installed and running.

### Setup Steps
1. Clone the repository:
   ```bash
   git clone https://github.com/TheRealMoeid/ai-store-assistant.git
   cd ai-store-assistant
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your specific values (see Configuration section).

---

## 8. Configuration

Edit the `.env` file. Never commit actual secrets to version control.

```env
# --- Telegram ---
BOT_TOKEN=your_telegram_bot_token_here
ADMIN_GROUP_ID=-1001234567890   # Must be a negative number; the chat_id of your admin group

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

*Note on `ADMIN_GROUP_ID`*: Add the bot to your Telegram group, send any message, and visit `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`. The group's `chat.id` (a negative integer) is your `ADMIN_GROUP_ID`.

---

## 9. Running the Project

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

## 10. Testing

The project includes a suite of targeted tests to verify critical architectural guarantees, located in the root directory (e.g., `test_concurrency.py`, `test_cart_persistence.py`, `test_narration_guard.py`).

To run the tests:
```bash
pytest test_*.py -v
```

**Test Coverage**:
- **Concurrency**: Verifies that `asyncio.Lock` prevents race conditions during simultaneous order placements.
- **Persistence**: Confirms that carts and conversation history survive bot restarts.
- **Agent Guards**: Validates that the narration guard intercepts hallucinated actions and that malformed JSON tool calls are safely recovered using `json.JSONDecoder.raw_decode()`.

*Limitation*: Tests currently focus on unit-level logic and agent guards. End-to-end Telegram integration testing is not yet automated.

---

## 11. AI Agent Architecture

The AI agent is designed for maximum reliability with smaller, local models, incorporating several defensive patterns:

- **Tool-Calling Loop**: Managed in `agent/agent.py`. It iterates up to `MAX_TOOL_ROUNDS`, sending context and tool schemas to the LLM, executing requested tools, and feeding results back until a final text response is generated.
- **Context Management**: `conversation_manager.py` persists the full conversation to disk but intelligently trims the payload sent to the LLM. It ensures trimming never splits a `tool_call` and its corresponding `tool` result, preventing orphaned `tool_call_id` API errors.
- **Malformed Call Recovery**: 
  - *Leaked JSON*: If the model outputs tool call JSON as plain text, `_parse_leaked_json_tool_call` structurally parses and executes it.
  - *Native Tags*: If the model outputs proprietary tags (e.g., `<function=...>`), `_parse_native_function_call` uses `json.JSONDecoder.raw_decode()` to safely extract nested arguments without fragile regex matching.
- **Narration Guard**: A structural guard scans the model's text output for commitment phrases (e.g., "I added that to your cart"). If detected without a corresponding tool call, the agent injects a corrective system message forcing the model to retry with the actual tool.
- **Secure Context Injection**: `user_id` and `username` are injected into the tool execution context by the dispatcher, never parsed from the LLM's arguments, preventing spoofing.

---

## 12. Current Project Status

The core architecture is stable and hardened. Recent updates (August 2026) successfully resolved critical P0/P1 bugs identified in early audits:
- ✅ Concurrent read-modify-write race conditions in orders/feedback are now prevented via `asyncio.Lock`.
- ✅ Ephemeral in-memory carts have been replaced with persistent disk storage (`data/carts.json`).
- ✅ History trimming is now "safe," preventing API crashes from orphaned tool calls.
- ✅ Malformed tool call recovery uses robust JSON decoding instead of fragile regex.

**Known Limitations (By Design or Backlog)**:
- No real payment gateway integration (relies on manual screenshot/reference review).
- Product search is keyword/category-based; fuzzy matching is not yet implemented.
- Order status is limited to `pending_confirmation`, `awaiting_review`, `confirmed`, and `rejected` (no "shipped" or "delivered" states yet).

---

## 13. Development

If you wish to extend or modify the project, follow these guidelines:

- **Adding New Tools**: Define the OpenAI-compatible schema in `agent/tools.py` under `TOOL_SCHEMAS`, and implement the logic in the `_DISPATCH` dictionary. Ensure the implementation handles missing arguments gracefully.
- **Modifying Agent Behavior**: Always update `agent/prompts.py` first if the LLM exhibits a new failure mode (e.g., hallucinating shipping times). Prompt-level fixes are faster and cheaper than code-level workarounds. Modify the loop logic in `agent/agent.py` only if structural changes are required.
- **Business Logic**: Keep all data manipulation (reading/writing JSON, validating stock) inside the `utils/` directory. 
- **Testing**: Add new test files prefixed with `test_` in the root directory to cover new tools or guards. Run `pytest` before committing.
- **Architectural Convention**: Never trust the LLM to provide the `user_id`. Always rely on the context injected by the Telegram handler.

---

## 14. License

No license is specified in this repository. All rights are reserved by the author.
