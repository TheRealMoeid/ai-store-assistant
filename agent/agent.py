"""
The actual agent loop: send the conversation + tool schemas to the LLM,
execute whatever tools it calls, feed results back, repeat until it gives
a final text answer.
"""
import json
import logging
from agent.llm_client import get_client
from agent.tools import TOOL_SCHEMAS, call_tool
from agent.prompts import SYSTEM_PROMPT
from utils import conversation_manager

logger = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 5  # safety cap so a confused model can't loop forever


async def run_agent(user_id: int, username: str, user_message: str) -> tuple[str, list[dict]]:
    """
    Returns (reply_text, events). `events` collects things like a placed
    order or logged feedback, so the caller (handlers) can notify the
    admin group without agent.py needing to know about Telegram.
    """
    client, model = get_client()

    conversation_manager.save_message(user_id, "user", user_message)
    history = conversation_manager.get_recent_for_llm(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    events: list[dict] = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
        )
        choice = response.choices[0].message

        if not choice.tool_calls:
            reply = choice.content or "..."
            logger.info("NO TOOL CALL — model answered directly: %r", reply)
            conversation_manager.save_message(user_id, "assistant", reply)
            return reply, events

        # Model wants to call one or more tools — execute each, append results,
        # and loop back so it can use them.
        messages.append(choice.model_dump(exclude_unset=True))

        for tool_call in choice.tool_calls:
            name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = call_tool(name, args, user_id, username)
            logger.info("TOOL CALL: %s(%s) -> %s", name, args, result)

            if name == "place_order" and result.get("status") == "order_placed":
                events.append({"type": "order", "data": result["order"]})
            if name == "log_feedback" and result.get("status") == "logged":
                events.append({"type": "feedback", "data": result["feedback"]})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    # Hit the safety cap — fall back gracefully instead of hanging.
    fallback = "Sorry, I'm having trouble finishing that request — could you try rephrasing?"
    conversation_manager.save_message(user_id, "assistant", fallback)
    return fallback, events
