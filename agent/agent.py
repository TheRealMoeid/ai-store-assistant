"""
The actual agent loop: send the conversation + tool schemas to the LLM,
execute whatever tools it calls, feed results back, repeat until it gives
a final text answer.
"""
import json
import logging
import re
import uuid
from openai import BadRequestError
from agent.llm_client import get_client
from agent.tools import TOOL_SCHEMAS, call_tool
from agent.prompts import SYSTEM_PROMPT
from utils import conversation_manager

logger = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 9
MAX_MALFORMED_RETRIES = 2


def _new_recovery_tool_call_id(prefix: str) -> str:
    """
    Generates a unique synthetic tool_call_id for a recovered (malformed)
    tool call, e.g. "recovered_3f9a1c2b8e4d4a1b9c0e1f2a3b4c5d6e".

    Previously both recovery paths reused a single hardcoded literal
    ("recovered_1" / "recovered_json_1") for every recovery event in the
    process's lifetime. The OpenAI-compatible API expects every
    tool_call_id within one conversation payload to be unique — it's how
    an assistant turn's tool_calls entries are correlated with their
    matching tool-role results. If recovery fired more than once for the
    same user within the conversation-history trimming window
    (MAX_HISTORY_MESSAGES), the persisted history could end up containing
    two messages with the same tool_call_id, which could itself trigger a
    BadRequestError on a later call — ironically the exact failure class
    this recovery logic exists to work around. See bug-audit-evaluation.md
    Issue 2.1.

    Keeps the recognizable "recovered_"/"recovered_json_" prefix so
    synthetic recovery IDs stay easy to spot in logs/persisted history
    during debugging, per the fix's own agreed shape — just no longer a
    static, reused string.

    Note: this only prevents *future* collisions. It does not retroactively
    rewrite already-persisted conversation JSON files that may still
    contain duplicate static "recovered_1"/"recovered_json_1" IDs from
    before this fix — acceptable at this project's scale (see CLAUDE.md's
    "minimal, structurally-scoped fixes" convention), since old history
    naturally ages out of the MAX_HISTORY_MESSAGES trim window over time.
    """
    return f"{prefix}_{uuid.uuid4().hex}"

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

    await conversation_manager.append_messages(user_id, [{"role": "user", "content": user_message}])
    history = await conversation_manager.get_recent_for_llm(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    events: list[dict] = []
    malformed_retries = 0

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_SCHEMAS, # type: ignore
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
                if name == "submit_payment_reference" and result.get("status") == "proof_submitted":
                    events.append({"type": "payment_reference", "data": result["order"]})

                # Unique per recovery event — see _new_recovery_tool_call_id
                # docstring (Issue 2.1). Generated once and reused for both
                # the synthetic assistant tool_calls entry and the matching
                # tool result below, since the two must correlate.
                recovery_id = _new_recovery_tool_call_id("recovered")
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": recovery_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": recovery_id,
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
                await conversation_manager.append_messages(user_id, turn_messages)
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
                name, args = parsed
                logger.warning("Recovered leaked-JSON tool call from plain text: %s(%s)", name, args)
                result = await call_tool(name, args, user_id, username)

                if name == "place_order" and result.get("status") == "order_placed":
                    events.append({"type": "order", "data": result["order"]})
                if name == "log_feedback" and result.get("status") == "logged":
                    events.append({"type": "feedback", "data": result["feedback"]})
                if name == "submit_payment_reference" and result.get("status") == "proof_submitted":
                    events.append({"type": "payment_reference", "data": result["order"]})

                # Unique per recovery event — see _new_recovery_tool_call_id
                # docstring (Issue 2.1).
                recovery_id = _new_recovery_tool_call_id("recovered_json")
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": recovery_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": recovery_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                malformed_retries = 0  # successful recovery, don't count it against the cap
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
                # Force a retry by injecting a correction message and looping
                # back so the model actually sees it and gets a chance to call
                # the real tool. Previously this branch appended the
                # correction and then `return`ed immediately — the correction
                # was silently discarded, the customer received the false
                # "added"/"placed" claim as-is, and the cart/order never
                # actually changed. This turn is intentionally NOT persisted
                # to conversation history here (unlike the final-return path
                # below): it's an internal retry step, not a real completed
                # turn — only the eventual real outcome (tool result + final
                # reply, or the MAX_TOOL_ROUNDS fallback) gets saved.
                messages.append({
                    "role": "user",
                    "content": (
                        "You claimed to have performed an action (like adding to cart or placing an order) "
                        "but you didn't actually call the required tool. You MUST call the appropriate tool "
                        "(e.g., add_to_cart, place_order) instead of just talking about it. Try again now."
                    )
                })
                continue

            logger.info("NO TOOL CALL — model answered directly: %r", reply)
            await conversation_manager.append_messages(user_id, [{"role": "assistant", "content": reply}])
            return reply, events

        # Successful structured tool call — reset the malformed-retry counter.
        malformed_retries = 0

        # Model wants to call one or more tools — execute each, append results,
        # and loop back so it can use them.
        messages.append(choice.model_dump(exclude_unset=True))

        for tool_call in choice.tool_calls:
            name = tool_call.function.name #type: ignore
            try:
                args = json.loads(tool_call.function.arguments or "{}") # type: ignore
            except json.JSONDecodeError:
                args = {}

            result = await call_tool(name, args, user_id, username)
            logger.info("TOOL CALL: %s(%s) -> %s", name, args, result)

            if name == "place_order" and result.get("status") == "order_placed":
                events.append({"type": "order", "data": result["order"]})
            if name == "log_feedback" and result.get("status") == "logged":
                events.append({"type": "feedback", "data": result["feedback"]})
            if name == "submit_payment_reference" and result.get("status") == "proof_submitted":
                events.append({"type": "payment_reference", "data": result["order"]})

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
    await conversation_manager.append_messages(user_id, turn_messages)
    return fallback, events