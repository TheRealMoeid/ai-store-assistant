# Shopmate an AI Sales Agent for Telegram

## 1. Project Overview

**Shopmate** (AI Store Assistant) is a lightweight, conversational AI sales agent for Telegram, powered by an LLM tool-calling loop. It allows customers to browse inventory, check stock, manage a shopping cart, place orders, track order status, and submit payment proof using natural language.

Designed specifically for small-scale, single-seller operations, the project intentionally avoids traditional databases. Instead, it uses flat JSON files for persistence, allowing the seller to manually edit `data/inventory.json` directly without needing an admin panel. Orders are confirmed, rejected, shipped, and delivered by the seller via Telegram commands in a dedicated admin group, and product photos are uploaded via a dedicated private Telegram channel.

---

## 2. Key Features

- **Natural Language Shopping**: Customers chat naturally to search products, check variant availability (size/color), and build a cart.
- **Typo-Tolerant Search**: Product search falls back to fuzzy matching when an exact search finds nothing, so common typos ("jordam", "hoody") still find the right item.
- **Product Photos**: Sellers upload product photos — a default photo per item, plus optional per-color photos — via a dedicated private Telegram channel. The bot automatically sends the right photo when a customer asks about an item, and mentions when photos are available during a search.
- **Persistent State**: Shopping carts and conversation histories are saved to disk, surviving bot restarts without losing customer context.
- **Per-User Rate Limiting**: A sliding-window limit on inbound messages protects the shop's daily LLM quota from being exhausted by any single customer.
- **Robust Admin Workflow**: Sellers receive detailed order notifications and manage fulfillment using `/pending_orders`, `/confirm <id>`, `/reject <id>`, `/ship <id>`, and `/deliver <id>`.
- **Order Fulfillment Tracking**: Orders can progress through `shipped` and `delivered` after confirmation, and customers can ask the bot for their order's current status at any time.
- **Automated Stock Management**: 
  - Stock is validated and decremented atomically at checkout.
  - Rejected orders automatically and safely restore reserved inventory.
- **Payment Proof Handling**: Customers can submit payment screenshots (processed directly) or type transaction references (processed via the LLM), both of which notify the admin group for review, and both support resubmission with a full audit trail.
- **Swappable LLM Backend**: Seamlessly switch between local development (Ollama) and cloud production (Groq) via environment variables.
- **Self-Correcting AI Agent**: Built-in structural guards intercept LLM hallucinations, recover from malformed tool calls, and force the model to retry if it "narrates" an action without actually executing it.

---

## 3. System Architecture

The system follows a modular, event-driven architecture centered around a highly defensive LLM tool-calling loop.

```text
Telegram User / Admin Group / Product-Images Channel
        ↓
aiogram 3 Dispatcher (bot.py)
        ↓
Handlers (user_handlers.py / admin_handlers.py / channel_images.py)
        ↓
AI Agent Loop (agent/agent.py)
        ↓
Tool Execution (agent/tools.py)
        ↓
Business Logic & Persistence (utils/*.py) ←→ asyncio.Lock
        ↓
JSON Data Store (data/*.json)
```

1. **User Interaction**: A user sends a message. `user_handlers.py` intercepts it, checks the per-user rate limit, and passes the text to `run_agent`.
2. **Context Building**: `conversation_manager` loads the user's recent history (safely trimmed to avoid orphaned tool calls) and appends the new message.
3. **LLM Decision**: The agent sends the context and tool schemas to the configured LLM.
4. **Tool Execution**: If the LLM requests a tool, `agent.py` executes it via `call_tool`. The tool interacts with the `utils` layer (e.g., checking `inventory_manager`), returns a structured result, and the loop repeats.
5. **Final Response**: Once the LLM returns a text response without tool calls, it is sent back to the user, and the turn is saved to disk. If the response included a product photo, that photo is sent to the customer alongside the text.
6. **Admin Flow**: When an order is placed or payment proof is submitted, the admin group receives a notification. The seller uses `/confirm`, `/reject`, `/ship`, or `/deliver` to update the order status, which automatically DMs the customer and (for `/reject`) adjusts inventory.
7. **Image Flow**: Sellers post product photos — optionally captioned with a color — in a dedicated private channel the bot is an admin of; the bot stores the photo for later use and confirms receipt in the channel.

---

## 4. Project Structure

```text
ai-store-assistant/
├── agent/                      # AI agent core logic
│   ├── llm_client.py           # Swappable LLM provider (Ollama/Groq)
│   ├── prompts.py               # System prompt rules and constraints
│   ├── agent.py                 # Tool-calling loop, recovery guards, narration guard
│   └── tools.py                 # Tool schemas (OpenAI format) and implementations
├── handlers/                    # Telegram message routing
│   ├── user_handlers.py         # User message routing, typing-indicator refresh, payment screenshots
│   ├── admin_handlers.py        # /pending_orders, /confirm, /reject, /ship, /deliver — gated by AdminOnlyMiddleware
│   └── channel_images.py        # Product-images channel listener (default + per-color photo uploads)
├── utils/                       # Business logic and data management
│   ├── cart_manager.py          # Persistent cart storage with async locks
│   ├── conversation_manager.py  # History load, save, safe-trimming, and async locks
│   ├── inventory_manager.py     # Live-reloading inventory reads, fuzzy search, stock writes, image storage
│   ├── order_manager.py         # Order and feedback persistence, full status lifecycle including shipped/delivered
│   ├── rate_limiter.py          # Per-user sliding-window rate limiting (in-memory)
│   └── file_io.py               # Shared async/atomic JSON read-write helpers used by all managers above
├── data/                        # JSON data storage
│   ├── inventory.json           # Seller-edited product catalog (incl. optional image fields)
│   ├── orders.json              # Order status: pending_confirmation → awaiting_review → confirmed → shipped → delivered | rejected
│   ├── carts.json               # Persisted user carts
│   ├── feedback.json
│   ├── conversations/{user_id}.json
│── test/                        # Pytest test suite
├── bot.py                       # Application entrypoint
├── config.py                    # Settings loaded from .env
├── requirements.txt             # Python dependencies
└── .env.example                 # Environment variable template
```

---

## 5. Technologies and Dependencies

- **Python 3.11 / 3.12**: Required runtime. *(Note: Python 3.14+ is currently unsupported due to missing prebuilt wheels for `pydantic-core`, an `aiogram`/`openai` dependency).*
- **aiogram (3.x)**: Asynchronous Telegram Bot API framework for routing and message handling.
- **openai (Python SDK)**: Used to interact with both Groq and Ollama, as both expose OpenAI-compatible API endpoints.
- **python-dotenv**: Loads environment variables from `.env` securely.
- **pytest / pytest-asyncio**: For running the automated test suite.
- **JSON**: File-based persistence for all state, chosen for simplicity and direct seller editability.
- No new dependencies were added for fuzzy search (stdlib `difflib`) or rate limiting (stdlib `collections.deque`) — both deliberately avoid adding to this list.

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

# Required for product photos: chat_id of a private channel the bot is an ADMIN of
PRODUCT_IMAGES_CHANNEL_ID=

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

# --- Rate limiting ---
RATE_LIMIT_MESSAGES=10          # Max messages per user per window before throttling
RATE_LIMIT_WINDOW_SECONDS=60

# --- Fuzzy search ---
FUZZY_SEARCH_THRESHOLD=0.7      # Minimum per-word match score (0.0-1.0) to count as a typo match
```

*Note on `ADMIN_GROUP_ID`*: Add the bot to your Telegram group, send any message, and visit `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` (ensure the bot script is **not** running while you check this). The group's `chat.id` (a negative integer) is your `ADMIN_GROUP_ID`.

*Note on `PRODUCT_IMAGES_CHANNEL_ID`*: Create a private Telegram channel and add the bot as an **admin** (not just a member — Telegram only delivers channel posts to admins). Fetch its chat_id the same way as `ADMIN_GROUP_ID` above.

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
*(On Windows, use `python -m pytest . -v` rather than bare `pytest . -v` — see `docs/CLAUDE.md`'s "Known environment gotchas" for why. Also confirm you're running inside your activated venv on Python 3.11/3.12: a fresh terminal session can silently fall back to a different global Python install, which breaks `pytest-asyncio` with no obvious error beyond a wave of failing async tests.)*

**Test Coverage Highlights:**
- **Concurrency & Atomicity**: Verifies that `asyncio.Lock` prevents race conditions during simultaneous order placements, checkouts, and multi-item stock decrements.
- **Persistence**: Confirms carts and conversation history survive restarts and lock correctly under concurrent load, with all file I/O async and atomic.
- **Agent Guards**: Validates that the narration guard intercepts hallucinated actions, malformed JSON tool calls are safely recovered, and payment reference events correctly trigger admin notifications.
- **Stock Reversal**: Ensures rejected orders correctly restore inventory atomically.
- **Order Fulfillment**: Validates the forward-only `confirmed → shipped → delivered` transition guard and both admin commands end-to-end.
- **Fuzzy Search**: Confirms typo tolerance without ever demoting an exact match, with threshold-precision and category-interaction coverage.
- **Product Images**: Confirms default and per-color photo storage/resolution, the channel upload listener, and that the agent correctly sends (and is honest about) the right photo.
- **Rate Limiting**: Confirms per-user sliding-window enforcement holds under concurrency without affecting other users.

*Warning: Some standalone root-level scripts (e.g., `test_concurrency.py`) write directly to real `data/` JSON files when run outside the sandboxed `pytest` suite. Always check `git status` before committing to avoid accidentally committing synthetic test data.*

---

## 10. AI Agent & Reliability Engineering

The AI agent is engineered for maximum reliability with smaller, local models, incorporating several defensive patterns:

- **Tool-Calling Loop**: Managed in `agent/agent.py`, it iterates up to `MAX_TOOL_ROUNDS`, sending context and tool schemas to the LLM, executing requested tools, and feeding results back until a final text response is generated.
- **Malformed Call Recovery**: 
  - *Leaked JSON*: If the model outputs tool call JSON as plain text, `_parse_leaked_json_tool_call` structurally parses and executes it.
  - *Native Tags*: If the model outputs proprietary tags (e.g., `<function=...>`), `_parse_native_function_call` uses `json.JSONDecoder.raw_decode()` to safely extract nested arguments without fragile regex matching.
- **Narration Guard**: A structural guard scans the model's text output for commitment phrases (e.g., "I added that to your cart"). If detected without a corresponding tool call, the agent injects a corrective system message and loops back, forcing the model to retry with the actual tool instead of returning a false confirmation to the user.
- **Concurrency Safety**: All persistence managers (`cart`, `order`, `inventory`, `conversation`) use `asyncio.Lock` to prevent race conditions during read-modify-write cycles, with disk I/O offloaded to worker threads and writes atomic (temp-file + replace) so a concurrent read never observes a torn file.
- **Secure Context Injection**: `user_id` and `username` are injected into the tool execution context by the Telegram dispatcher, never parsed from the LLM's arguments, preventing spoofing. The same principle extends to `check_order_status`: a model-supplied `order_id` is verified to actually belong to the requesting user before any data is returned.
- **Safe History Trimming**: `conversation_manager.py` persists the full conversation to disk but intelligently trims the payload sent to the LLM, ensuring trimming never splits a `tool_call` and its corresponding `tool` result.
- **Per-User Rate Limiting**: Checked before any LLM call is made, so a throttled user's message never spends LLM budget at all.

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
- ✅ Admin notifications for typed payment references, including resubmissions with a full audit trail.
- ✅ All file I/O is async and atomic (offloaded to worker threads, temp-file + replace writes, retry-protected reads) — no disk operation blocks the event loop.
- ✅ Per-user rate limiting on inbound messages.
- ✅ Order fulfillment tracking (`shipped`/`delivered`) with a forward-only transition guard.
- ✅ Typo-tolerant ("fuzzy") product search, falling back only when an exact match finds nothing.
- ✅ Product photos — a default photo plus optional per-color photos — uploaded via a dedicated Telegram channel and sent automatically when relevant.

### By Design
- **No Database**: Flat JSON files are used intentionally so the seller can edit `inventory.json` directly via text editor.
- **No Payment Gateway**: Relies on manual screenshot/reference review by the seller — a deliberate choice for a single-seller shop, not a missing feature. Revisiting this would be the single biggest scope/risk item on the roadmap (real money, PCI-adjacent concerns, webhook security) and is intentionally deferred unless the business outgrows manual review.
- **Basic Order Status**: `pending_confirmation`, `awaiting_review`, `confirmed`, `shipped`, `delivered`, and `rejected` — no partial shipment, returns, or cancellation states.

### Backlog / Known Limitations
None outstanding from the original bug-audit or the prioritized feature backlog — see "Fixed in Recent Passes" below for what's landed.

### Fixed in Recent Passes (previously listed here)
- ✅ **Atomic cart-consumption** (Issue 3.1): the checkout sequence is atomic; no same-user race is possible.
- ✅ **Substring color matching** (Issue 3.2): variant matching now uses exact color comparison, eliminating inventory drift on rejection with overlapping color names.
- ✅ **Hardcoded recovery IDs** (Issue 2.1): synthetic recovery `tool_call_id`s are unique per event (`uuid.uuid4().hex`).
- ✅ **Narration guard negative-phrase gap** (Issue 4.1): the guard's negative filter also recognizes `can't / cannot / unable to / not able to` constructions.
- ✅ **Payment proof resubmission** (Issue 4.2, Option B): customers can replace a submitted proof; every submission is appended to a per-order audit list, and admin notifications flag resubmissions.
- ✅ **Synchronous file I/O** (Issue 3.4 / GitHub #16): all four managers' disk access is now offloaded to worker threads with atomic writes and retry-protected reads — the one previously-open item from the original bug audit, now closed.
- ✅ **Rate limiting per user**: a sliding-window limiter protects the shop's daily LLM quota from a single user exhausting it.
- ✅ **Order status beyond confirmed/rejected**: `/ship` and `/deliver` admin commands, plus a `check_order_status` agent tool.
- ✅ **Fuzzy product search**: typo-tolerant fallback matching, stdlib-only.
- ✅ **Product images**: default and per-color photos via a dedicated upload channel.

---

## 12. Development Conventions

If you wish to extend or modify the project, follow these established guidelines:

1. **Prompt-Level Fixes First**: If the LLM exhibits a new failure mode (e.g., hallucinating shipping times), update `agent/prompts.py` first. Prompt-level fixes are faster and cheaper than code-level workarounds.
2. **Whole-File Replacement**: When making multi-line changes, prefer regenerating and handing over the full file content over patch/diff files, which frequently fail to apply cleanly against working trees with local drift.
3. **Never Trust the LLM for Context**: Never parse `user_id` or `username` from model tool-call arguments. Always rely on the context injected by the Telegram handler — and apply the same skepticism to any other identifier the model supplies as an argument (e.g. `order_id`), verifying ownership server-side rather than trusting it at face value.
4. **Minimal, Structurally-Scoped Fixes**: Prefer small fixes that don't touch already-working, already-tested code paths unless deduplication is strictly required to solve the issue at hand.
5. **Separate Unrelated Changes**: Never bundle unrelated local edits (e.g., stray type-checker suppressions) into a feature branch commit.
6. **No New Dependencies Without Real Need**: Prefer solving a problem with the stdlib (as fuzzy search and rate limiting both do) over adding a new package, unless the stdlib genuinely can't do the job at this project's scale.

---

## 13. License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.