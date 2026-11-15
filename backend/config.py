"""Environment-backed server configuration; secrets never enter browser code."""

import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent


def load_env_file(path=BACKEND_DIR / ".env"):
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def get_provider_config():
    load_env_file()
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "auto":
        provider = "openai" if os.getenv("OPENAI_API_KEY") else "xai"
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = "https://api.openai.com/v1/chat/completions"
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
    elif provider in ("xai", "grok"):
        provider = "xai"
        api_key = os.getenv("XAI_API_KEY", "").strip()
        base_url = "https://api.x.ai/v1/chat/completions"
        model = os.getenv("XAI_MODEL", "grok-3-mini").strip()
    else:
        raise ValueError("LLM_PROVIDER must be openai, xai, grok, or auto")
    if not api_key:
        env_name = "OPENAI_API_KEY" if provider == "openai" else "XAI_API_KEY"
        raise ValueError(f"{env_name} is not set in backend/.env")
    return {"provider": provider, "api_key": api_key, "base_url": base_url, "model": model}


def get_port():
    load_env_file()
    try:
        port = int(os.getenv("PORT", "8765"))
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return port
