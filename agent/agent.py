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

MAX_TOOL_ROUNDS = 9
MAX_MALFORMED_RETRIES = 2

# Some smaller/local models occasionally "fake" a tool call by writing JSON
# as plain reply text instead of using the real function-calling protocol,
# e.g. {"name":"check_availability","parameters":{"product_id":"..."}} or
# {"type":"function","name":"...","parameters":{...}}. Key order and extra
# wrapper keys vary, so we parse structurally rather than regex-matching —
# and since the intended call is right there, we recover and execute it
# directly instead of burning another round-trip asking the model to retry.
def _parse_leaked_json_tool_call(text: str) -> tuple[str, dict] | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    args = obj.get("parameters") if isinstance(obj.get("parameters"), dict) else obj.get("arguments")
    if not isinstance(args, dict):
        return None
    return obj["name"], args

def _parse_native_function_call(failed_generation: str) -> tuple[str, dict] | None:
    """
    Llama sometimes emits its native tag format instead of a structured
    tool call: <function=name{"arg": "val"}</function> or name={"arg":...}
    This recovers (name, args) from that text so we don't have to retry.
    
    Uses json.JSONDecoder.raw_decode() to safely extract the JSON object,
    correctly handling nested braces and escaped characters inside strings.
    """
    # 1. Find the function name and the exact index where the JSON object starts
    # We look for the opening '{' without trying to match the closing '}'
    match = re.search(r'<function=([\w_]+)\s*=?\s*\{', failed_generation)
    if not match:
        # Fallback for the name={"arg": ...} format
        match = re.search(r'([\w_]+)\s*=\s*\{', failed_generation)
        
    if not match:
        return None
        
    name = match.group(1)
    json_start = match.end() - 1  # The exact index of the opening '{'
    
    # 2. Use raw_decode to parse the JSON object starting from that index.
    # This safely handles nested braces, arrays, and escaped quotes.
    decoder = json.JSONDecoder()
    try:
        args, _ = decoder.raw_decode(failed_generation, json_start)
        if not isinstance(args, dict):
            return None
        return name, args
    except json.JSONDecodeError:
        return None


async def run_agent(user_id: int, username: str, user_message: str) -> tuple[str, list[dict]]:
    """
    Returns (reply_text, events). `events` collects things like a placed
    order or logged feedback, so the caller (handlers) can notify the
    admin group without agent.py needing to know about Telegram.
    """
    client, model = get_client()

    conversation_manager.append_messages(user_id, [{"role": "user", "content": user_message}])
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
                result = await call_tool(name, args, user_id, username)

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
                turn_messages = messages[len(history) + 1:]
                turn_messages.append({"role": "assistant", "content": fallback})
                conversation_manager.append_messages(user_id, turn_messages)
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

            parsed = _parse_leaked_json_tool_call(reply)
            if parsed:

                continue

            # Structural guard: detect commitment language without a tool call.
            # Models sometimes narrate actions ("I've added that to your cart") 
            # without actually calling the tool, leaving the cart empty.
            commitment_phrases = [
                "i've added", "i have added", "i'll add", "i will add", "let me add",
                "added to your cart", "added to cart",
                "order is placed", "order placed", "order is confirmed", "order confirmed",
                "order is processing", "order processing"
            ]
            reply_lower = reply.lower()
            
            # Filter out negative contexts so we don't punish the model for saying 
            # "I haven't added it yet" or "I didn't place the order".
            is_negative = any(neg in reply_lower for neg in [
                "not added", "haven't added", "didn't add", 
                "not placed", "haven't placed", "didn't place",
                "not confirmed", "haven't confirmed", "didn't confirm"
            ])
            
            if not is_negative and any(phrase in reply_lower for phrase in commitment_phrases):
                logger.warning("Model narrated action without tool call: %r", reply)
                # Force a retry by injecting a correction message
                messages.append({
                    "role": "user",
                    "content": (
                        "You claimed to have performed an action (like adding to cart or placing an order) "
                        "but you didn't actually call the required tool. You MUST call the appropriate tool "
                        "(e.g., add_to_cart, place_order) instead of just talking about it. Try again now."
                    )
                })
                
                
                logger.info("NO TOOL CALL — model answered directly: %r", reply)
                turn_messages = messages[len(history) + 1:]
                conversation_manager.append_messages(user_id, turn_messages)
                return reply, events
        
                name, args = parsed
                logger.warning("Recovered leaked-JSON tool call from plain text: %s(%s)", name, args)
                result =await call_tool(name, args, user_id, username)

                if name == "place_order" and result.get("status") == "order_placed":
                    events.append({"type": "order", "data": result["order"]})
                if name == "log_feedback" and result.get("status") == "logged":
                    events.append({"type": "feedback", "data": result["feedback"]})

                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "recovered_json_1",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": "recovered_json_1",
                    "content": json.dumps(result, ensure_ascii=False),
                })
                continue

            logger.info("NO TOOL CALL — model answered directly: %r", reply)
            conversation_manager.append_messages(user_id, [{"role": "assistant", "content": reply}])
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

            result = await call_tool(name, args, user_id, username)
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
    turn_messages = messages[len(history) + 1:]
    turn_messages.append({"role": "assistant", "content": fallback})
    conversation_manager.append_messages(user_id, turn_messages)
    return fallback, events