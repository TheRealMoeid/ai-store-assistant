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
    See bug-audit-evaluation.md Issue 2.1.
    """
    return f"{prefix}_{uuid.uuid4().hex}"

# Some smaller/local models occasionally "fake" a tool call by writing JSON
# as plain reply text instead of using the real function-calling protocol,
# e.g. {"name":"check_availability","parameters":{"product_id":"..."}} or
# {"type":"function","name":"...","parameters":{...}}. Key order and extra
# wrapper keys vary, so we parse structurally rather than regex-matching --
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
    match = re.search(r'<function=([\w_]+)\s*=?\s*\{', failed_generation)
    if not match:
        match = re.search(r'([\w_]+)\s*=\s*\{', failed_generation)

    if not match:
        return None

    name = match.group(1)
    json_start = match.end() - 1

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
    order, logged feedback, a submitted payment reference, or (new, see
    product-images feature) a product photo to send, so the caller
    (handlers) can notify the admin group / send the customer a photo
    without agent.py needing to know about Telegram.
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
                tool_choice="auto", # you may already have this implicitly -- make it explicit
                parallel_tool_calls=False,  # simpler for Llama/Groq -- fewer malformed multi-calls
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
                if name == "get_product_details" and isinstance(result, dict) and result.get("image"):
                    events.append({
                        "type": "product_image",
                        "data": {"image": result["image"], "name": result.get("name", ""), "color": result.get("image_color")},
                    })

                # Unique per recovery event -- see _new_recovery_tool_call_id
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
                    "'<function=...>' -- call one of the available tools directly."
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
                if name == "get_product_details" and isinstance(result, dict) and result.get("image"):
                    events.append({
                        "type": "product_image",
                        "data": {"image": result["image"], "name": result.get("name", ""), "color": result.get("image_color")},
                    })

                # Unique per recovery event -- see _new_recovery_tool_call_id
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
            commitment_phrases = [
                "i've added", "i have added", "i'll add", "i will add", "let me add",
                "added to your cart", "added to cart",
                "order is placed", "order placed", "order is confirmed", "order confirmed",
                "order is processing", "order processing"
            ]
            reply_lower = reply.lower()

            is_negative = any(neg in reply_lower for neg in [
                "not added", "haven't added", "didn't add",
                "not placed", "haven't placed", "didn't place",
                "not confirmed", "haven't confirmed", "didn't confirm",
                "can't add", "cannot add", "unable to add", "not able to add",
                "can't place", "cannot place", "unable to place", "not able to place",
                "can't confirm", "cannot confirm", "unable to confirm", "not able to confirm"
            ])

            if not is_negative and any(phrase in reply_lower for phrase in commitment_phrases):
                logger.warning("Model narrated action without tool call: %r", reply)
                messages.append({
                    "role": "user",
                    "content": (
                        "You claimed to have performed an action (like adding to cart or placing an order) "
                        "but you didn't actually call the required tool. You MUST call the appropriate tool "
                        "(e.g., add_to_cart, place_order) instead of just talking about it. Try again now."
                    )
                })
                continue

            logger.info("NO TOOL CALL -- model answered directly: %r", reply)
            await conversation_manager.append_messages(user_id, [{"role": "assistant", "content": reply}])
            return reply, events

        # Successful structured tool call -- reset the malformed-retry counter.
        malformed_retries = 0

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
            if name == "get_product_details" and isinstance(result, dict) and result.get("image"):
                events.append({
                    "type": "product_image",
                    "data": {"image": result["image"], "name": result.get("name", ""), "color": result.get("image_color")},
                })

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    # Hit the safety cap -- fall back gracefully instead of hanging.
    fallback = "Sorry, I'm having trouble finishing that request — could you try rephrasing?"
    turn_messages = messages[len(history) + 1:]
    turn_messages.append({"role": "assistant", "content": fallback})
    await conversation_manager.append_messages(user_id, turn_messages)
    return fallback, events