"""
Thin wrapper so the rest of the app never cares whether we're talking to
local Ollama or hosted Groq. Both expose an OpenAI-compatible /v1 API,
so switching is just a different base_url / api_key / model — controlled
entirely by config.LLM_PROVIDER.

To add a new provider later (OpenAI, Together, etc.) you only need to
add another branch in `get_client()`.
"""
from openai import AsyncOpenAI
import config


def get_client() -> tuple[AsyncOpenAI, str]:
    """Returns (client, model_name) for whichever provider is configured."""
    if config.LLM_PROVIDER == "groq":
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=config.GROQ_API_KEY,
        )
        return client, config.GROQ_MODEL

    if config.LLM_PROVIDER == "ollama":
        client = AsyncOpenAI(
            base_url=config.OLLAMA_BASE_URL,
            api_key="ollama",  # Ollama ignores this but the SDK requires a value
        )
        return client, config.OLLAMA_MODEL

    raise ValueError(
        f"Unknown LLM_PROVIDER '{config.LLM_PROVIDER}'. Use 'ollama' or 'groq'."
    )
