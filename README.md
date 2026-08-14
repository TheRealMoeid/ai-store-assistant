# Store Bot — AI Sales Agent for Telegram

An aiogram 3 Telegram bot that connects customers to an LLM-powered sales
agent. The agent can search products, check stock/size/color, take orders,
and log compliments/complaints — all backed by a plain JSON inventory file
(no database).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: add your BOT_TOKEN, ADMIN_GROUP_ID, and LLM settings
```

**Getting ADMIN_GROUP_ID:** add the bot to your admin Telegram group, send
any message, then check `https://api.telegram.org/bot<TOKEN>/getUpdates` —
the group's `chat.id` (a negative number) is your `ADMIN_GROUP_ID`.

### Local development (Ollama)
Make sure Ollama is running (`ollama serve`) with the model pulled
(`ollama pull llama3.2`). In `.env`:
```
LLM_PROVIDER=ollama
```

### Going live (Groq)
Get a free API key at console.groq.com, then in `.env`:
```
LLM_PROVIDER=groq
GROQ_API_KEY=...
```
No other code changes needed — `agent/llm_client.py` handles the switch.

## Run

```bash
python bot.py
```

## Editing inventory

Just edit `data/inventory.json` directly — no restart needed, the bot
reloads it automatically when the file changes. Each product looks like:

```json
{
  "id": "p003",
  "name": "...",
  "category": "sneakers",
  "price": 1000000,
  "description": "...",
  "variants": [
    {"size": "42", "color": "black", "material": "leather", "stock": 5}
  ]
}
```

## What's scaffolded vs. what's next

Done: bot skeleton, provider-swappable LLM client, inventory manager with
live reload, full agent tool-calling loop, core tools (search / details /
availability / cart / order / feedback), per-user conversation persistence,
admin group notifications, `/pending_orders` admin command.

Worth doing next: rate limiting per user, order status updates
(confirm/cancel commands for the seller), richer product search (fuzzy
matching), image support for products, and testing tool-calling reliability
specifically on Llama 3.2 via Ollama vs. Groq's larger models before going
live — smaller local models are sometimes less reliable at calling tools
correctly.

## Project structure

```
store_bot/
├── bot.py                      # entrypoint
├── config.py                   # all settings, loaded from .env
├── data/
│   ├── inventory.json          # seller edits this directly
│   ├── orders.json             # append-only order log
│   ├── feedback.json           # compliments/complaints log
│   └── conversations/          # per-user chat history (JSON)
├── agent/
│   ├── llm_client.py           # Ollama/Groq-swappable client
│   ├── tools.py                # tool schemas + implementations
│   ├── agent.py                # the tool-calling loop
│   └── prompts.py              # system prompt
├── handlers/
│   ├── user_handlers.py        # routes messages to the agent
│   └── admin_handlers.py       # /pending_orders
└── utils/
    ├── inventory_manager.py    # live-reloading inventory reads
    ├── conversation_manager.py # history load/save/trim
    └── order_manager.py        # order + feedback persistence
```
