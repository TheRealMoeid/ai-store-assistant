# test/test_llm_client.py
"""
Guards agent/llm_client.py's proxy handling, which CI could not previously
catch. Background: the VPN exposes a SOCKS proxy via HTTP(S)_PROXY, and
httpx trusts those env vars by default. Both LLM clients must be built with
httpx.AsyncClient(trust_env=False) so local Ollama traffic never takes the
proxy detour. See docs/CLAUDE.md's "Known environment gotchas" entry.

This suite inspects the constructed client only. It makes no network
calls, so it runs in CI without Ollama or a Groq key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

import config
from agent import llm_client


@pytest.fixture
def ollama_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:8b")


@pytest.fixture
def groq_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "groq")
    # A placeholder key is enough: the client is only constructed, never called.
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "GROQ_MODEL", "llama-3.3-70b-versatile")


def _inner_http_client(client):
    # AsyncOpenAI keeps the httpx client it was given on a private attribute.
    # The openai SDK is pinned in requirements.txt, so this attribute name is
    # stable for this project. If an upgrade renames it, this test fails
    # loudly, which is the intended behavior.
    return client._client


@pytest.mark.parametrize("provider_fixture", ["ollama_provider", "groq_provider"])
def test_both_providers_build_httpx_client_with_trust_env_disabled(request, provider_fixture):
    request.getfixturevalue(provider_fixture)

    client, model = llm_client.get_client()
    inner = _inner_http_client(client)

    assert isinstance(inner, httpx.AsyncClient), (
        f"expected an httpx.AsyncClient inside AsyncOpenAI, got {type(inner).__name__}"
    )
    assert inner._trust_env is False, (
        "trust_env must be False so HTTP(S)_PROXY / the VPN's SOCKS proxy "
        "is never applied to LLM traffic"
    )
    assert model  # provider's model name is returned alongside the client


def test_ollama_client_points_at_configured_base_url(ollama_provider):
    client, model = llm_client.get_client()
    assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"
    assert model == "qwen3:8b"


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "not-a-real-provider")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        llm_client.get_client()


def main():
    """Lets this be run directly with `python test/test_llm_client.py`."""
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))


if __name__ == "__main__":
    main()
