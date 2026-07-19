"""
AI client — uses GitHub Copilot API via your existing `gh` CLI token.
No separate API key required. Falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY
if the gh token is not available.

Supported models (GitHub Copilot): claude-sonnet-4.6, gpt-4o-2024-11-20, etc.
"""
from __future__ import annotations
import base64
import subprocess
from pathlib import Path
from typing import Optional
from functools import lru_cache

from lab_guide_automator.config import Settings


# ─────────────────────────────────────────────────────────────
# GitHub Copilot token resolution
# ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_gh_token() -> Optional[str]:
    """Retrieve GitHub token via `gh auth token`. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
        token = result.stdout.strip()
        return token if token else None
    except Exception:
        return None


_COPILOT_BASE_URL = "https://api.githubcopilot.com"
_COPILOT_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "editor-version": "vscode/1.95.0",
    "editor-plugin-version": "copilot-chat/0.22.4",
}

# Default model — same one powering this OpenCode session
_COPILOT_CHAT_MODEL = "claude-sonnet-4.6"
_COPILOT_VISION_MODEL = "claude-sonnet-4.6"  # supports vision

# Corporate CA bundle path (includes Cisco root CAs + standard CAs)
_CERT_BUNDLE = Path(__file__).parent.parent / "certs" / "system_ca.pem"


def _ssl_verify():
    """Return cert bundle path if available, else True (default verify)."""
    if _CERT_BUNDLE.exists():
        return str(_CERT_BUNDLE)
    return True


def _get_copilot_token() -> str:
    """Return the GitHub Copilot token, raising if unavailable."""
    token = _get_gh_token()
    if not token:
        raise RuntimeError("GitHub token not available. Run `gh auth login`.")
    return token


def _make_http_client():
    """Return a synchronous httpx.Client with the correct SSL bundle."""
    import httpx
    return httpx.Client(verify=_ssl_verify(), timeout=15)


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode()


# ─────────────────────────────────────────────────────────────
# GitHub Copilot (OpenAI-compatible, no API key needed)
# ─────────────────────────────────────────────────────────────

async def _copilot_chat(
    messages: list[dict],
    model: str = _COPILOT_CHAT_MODEL,
) -> str:
    from openai import AsyncOpenAI
    import httpx
    token = _get_gh_token()
    if not token:
        raise RuntimeError(
            "GitHub token not available. Run `gh auth login` to authenticate."
        )
    client = AsyncOpenAI(
        api_key=token,
        base_url=_COPILOT_BASE_URL,
        default_headers=_COPILOT_HEADERS,
        http_client=httpx.AsyncClient(verify=_ssl_verify()),
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


async def _copilot_vision(
    prompt: str,
    image_path: Path,
    model: str = _COPILOT_VISION_MODEL,
) -> str:
    from openai import AsyncOpenAI
    import httpx
    token = _get_gh_token()
    if not token:
        raise RuntimeError(
            "GitHub token not available. Run `gh auth login` to authenticate."
        )
    client = AsyncOpenAI(
        api_key=token,
        base_url=_COPILOT_BASE_URL,
        default_headers=_COPILOT_HEADERS,
        http_client=httpx.AsyncClient(verify=_ssl_verify()),
    )
    b64 = _encode_image(image_path)
    suffix = image_path.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/jpeg"
    resp = await client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{b64}",
                    "detail": "high",
                }},
            ],
        }],
        temperature=0.2,
        max_tokens=1024,
    )
    return resp.choices[0].message.content or ""


# ─────────────────────────────────────────────────────────────
# OpenAI fallback
# ─────────────────────────────────────────────────────────────

async def _openai_chat(settings: Settings, messages: list[dict]) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=settings.chat_model(),
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


async def _openai_vision(settings: Settings, prompt: str, image_path: Path) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    b64 = _encode_image(image_path)
    resp = await client.chat.completions.create(
        model=settings.vision_model(),
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "high",
                }},
            ],
        }],
        temperature=0.2,
        max_tokens=1024,
    )
    return resp.choices[0].message.content or ""


# ─────────────────────────────────────────────────────────────
# Anthropic fallback
# ─────────────────────────────────────────────────────────────

async def _anthropic_chat(settings: Settings, messages: list[dict]) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_msgs = [m for m in messages if m["role"] != "system"]
    resp = await client.messages.create(
        model=settings.chat_model(),
        max_tokens=4096,
        system=system,
        messages=user_msgs,
    )
    return resp.content[0].text if resp.content else ""


async def _anthropic_vision(settings: Settings, prompt: str, image_path: Path) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    b64 = _encode_image(image_path)
    suffix = image_path.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/jpeg"
    resp = await client.messages.create(
        model=settings.vision_model(),
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return resp.content[0].text if resp.content else ""


# ─────────────────────────────────────────────────────────────
# Ollama fallback
# ─────────────────────────────────────────────────────────────

async def _ollama_chat(settings: Settings, messages: list[dict]) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={"model": settings.chat_model(), "messages": messages, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


async def _ollama_vision(settings: Settings, prompt: str, image_path: Path) -> str:
    import httpx
    b64 = _encode_image(image_path)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": settings.vision_model(), "prompt": prompt, "images": [b64], "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["response"]


# ─────────────────────────────────────────────────────────────
# Provider selection: Copilot → OpenAI → Anthropic → Ollama
# ─────────────────────────────────────────────────────────────

def _resolve_provider(settings: Settings) -> str:
    """
    Auto-detect the best available provider.
    Priority: copilot (gh token) > explicit AI_PROVIDER setting > openai key > anthropic key > ollama
    """
    # GitHub Copilot is always preferred — uses existing gh auth, no separate key
    if _get_gh_token():
        return "copilot"
    # Respect explicit AI_PROVIDER if set
    if settings.ai_provider and settings.ai_provider != "copilot":
        return settings.ai_provider
    # Fall through by available key
    if settings.openai_api_key:
        return "openai"
    if settings.anthropic_api_key:
        return "anthropic"
    return "ollama"


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

async def chat(settings: Settings, messages: list[dict]) -> str:
    """Send a chat completion — automatically uses GitHub Copilot if available."""
    provider = _resolve_provider(settings)
    if provider == "copilot":
        return await _copilot_chat(messages)
    elif provider == "anthropic":
        return await _anthropic_chat(settings, messages)
    elif provider == "ollama":
        return await _ollama_chat(settings, messages)
    else:
        return await _openai_chat(settings, messages)


async def describe_screenshot(
    settings: Settings,
    image_path: Path,
    context: str = "",
) -> str:
    """
    Use vision model to describe what's happening in a screenshot
    in the context of a lab procedure.
    """
    prompt = (
        "You are analyzing a screenshot from a technical lab recording. "
        "Describe what you see in 2-4 sentences focusing on: "
        "what interface or tool is shown, what action appears to have just been performed, "
        "and what the current state is. Be specific about UI elements, commands, "
        "hostnames, IP addresses, or config values visible.\n"
    )
    if context:
        prompt += f"\nLab context: {context}"

    provider = _resolve_provider(settings)
    if provider == "copilot":
        return await _copilot_vision(prompt, image_path)
    elif provider == "anthropic":
        return await _anthropic_vision(settings, prompt, image_path)
    elif provider == "ollama":
        return await _ollama_vision(settings, prompt, image_path)
    else:
        return await _openai_vision(settings, prompt, image_path)
