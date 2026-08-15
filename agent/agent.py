"""
The actual agent loop: send the conversation + tool schemas to the LLM,
execute whatever tools it calls, feed results back, repeat until it gives
a final text answer.
"""
import json
import logging
import re
from openai import BadRequestError
from agent.llm_client import get_client
from agent.tools import TOOL_SCHEMAS, call_tool
from agent.prompts import SYSTEM_PROMPT
from utils import conversation_manager

logger = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 5
MAX_MALFORMED_RETRIES = 2


def _parse_native_function_call(failed_generation: str) -> tuple[str, dict] | None:
    """
    Llama sometimes emits its native tag format instead of a structured
    tool call: <function=name{"arg": "val"}</function> or name={"arg":...}
    This recovers (name, args) from that text so we don't have to retry.
    """
    match = re.search(r'<function=([\w_]+)\s*=?\s*(\{.*?\})\s*/?>?', failed_generation, re.DOTALL)
    if not match:
        return None
    name, raw_args = match.group(1), match.group(2)
    try:
        return name, json.loads(raw_args)
    except json.JSONDecodeError:
        return None


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
    malformed_retries = 0

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto", # you may already have this implicitly — make it explicit
                parallel_tool_calls=False,  # simpler for Llama/Groq — fewer malformed multi-calls
            )
        except BadRequestError as e:
            malformed_retries += 1
            body = e.response.json() if hasattr(e, "response") else {}
            failed_gen = body.get("error", {}).get("failed_generation", "")

            parsed = _parse_native_function_call(failed_gen)
            if parsed:
                name, args = parsed
                logger.info("Recovered malformed tool call via regex: %s(%s)", name, args)
                result = call_tool(name, args, user_id, username)

                if name == "place_order" and result.get("status") == "order_placed":
                    events.append({"type": "order", "data": result["order"]})
                if name == "log_feedback" and result.get("status") == "logged":
                    events.append({"type": "feedback", "data": result["feedback"]})

                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "recovered_1",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": "recovered_1",
                    "content": json.dumps(result, ensure_ascii=False),
                })
                malformed_retries = 0  # successful recovery, don't count it against the cap
                continue

            # Recovery failed — log the raw text so we can see WHY the regex missed it
            logger.warning(
                "Could not recover malformed tool call (attempt %d/%d). Raw failed_generation: %r",
                malformed_retries, MAX_MALFORMED_RETRIES, failed_gen,
            )

            if malformed_retries > MAX_MALFORMED_RETRIES:
                fallback = "Sorry, I got a bit confused there — could you rephrase that?"
                conversation_manager.save_message(user_id, "assistant", fallback)
                return fallback, events

            messages.append({
                "role": "user",
                "content": (
                    "Your previous response was not a valid tool call. "
                    "Use the proper function-calling mechanism, not text like "
                    "'<function=...>' — call one of the available tools directly."
                ),
            })
            continue

        choice = response.choices[0].message

        if not choice.tool_calls:
            reply = choice.content or "..."
            logger.info("NO TOOL CALL — model answered directly: %r", reply)
            conversation_manager.save_message(user_id, "assistant", reply)
            return reply, events

        # Successful structured tool call — reset the malformed-retry counter.
        malformed_retries = 0

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