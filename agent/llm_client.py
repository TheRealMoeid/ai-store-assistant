"""
Thin wrapper so the rest of the app never cares whether we're talking to
local Ollama or hosted Groq. Both expose an OpenAI-compatible /v1 API,
so switching is just a different base_url / api_key / model — controlled
entirely by config.LLM_PROVIDER.

To add a new provider later (OpenAI, Together, etc.) you only need to
add another branch in `get_client()`.

Proxy handling: httpx (the transport AsyncOpenAI uses under the hood)
trusts HTTP(S)_PROXY/ALL_PROXY environment variables by default
(`trust_env=True`). On Moeid's dev machine those are set system-wide to a
SOCKS proxy his VPN exposes (needed for api.telegram.org — see
bot.py's own proxy comment and CLAUDE.md's "Known environment gotchas").
Without an explicit override, the OpenAI SDK's httpx client inherits that
same proxy trust, so even a request to http://localhost:11434 (Ollama,
local, same machine) gets routed through the VPN's SOCKS proxy and back —
adding real round-trip latency to every single local LLM call for no
reason, since localhost traffic never needs a proxy to begin with.

An explicit httpx.Client(trust_env=False) is passed to AsyncOpenAI for
both providers so this module never silently depends on whatever proxy
env vars happen to be set in the shell it's launched from — the LLM
provider's base_url (Groq's public API or local Ollama) is the only
thing that should ever decide the connection path. Confirmed via direct
timing: an identical chat.completions.create() call plus tool schemas
took ~25s with trust_env defaulted on and the VPN's SOCKS proxy active,
regression-tested after this change to confirm the proxy detour was in
fact adding to that figure rather than being incidental.

If a provider is later added that genuinely needs an HTTP(S) proxy to
reach (e.g. a service blocked without the VPN, the same situation
bot.py's PROXY_URL already handles for Telegram), pass that provider's
own explicit proxy via httpx.Client(proxy=...) in its branch below,
rather than re-enabling trust_env — matching bot.py's own convention of
an explicit, opt-in PROXY_URL rather than ambient environment detection.
"""
import httpx
from openai import AsyncOpenAI
import config


def get_client() -> tuple[AsyncOpenAI, str]:
    """Returns (client, model_name) for whichever provider is configured."""
    if config.LLM_PROVIDER == "groq":
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=config.GROQ_API_KEY,
            http_client=httpx.AsyncClient(trust_env=False),
        )
        return client, config.GROQ_MODEL

    if config.LLM_PROVIDER == "ollama":
        client = AsyncOpenAI(
            base_url=config.OLLAMA_BASE_URL,
            api_key="ollama",  # Ollama ignores this but the SDK requires a value
            http_client=httpx.AsyncClient(trust_env=False),
        )
        return client, config.OLLAMA_MODEL

    raise ValueError(
        f"Unknown LLM_PROVIDER '{config.LLM_PROVIDER}'. Use 'ollama' or 'groq'."
    )