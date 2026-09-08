"""Helper unified đọc API key của các AI provider.

Ưu tiên: app_config (DB) → env var → None.
Để IT update key qua UI Settings → AI Models mà không cần restart.
"""
from __future__ import annotations

import os
from typing import Optional


def get_ai_key(provider: str) -> Optional[str]:
    """provider ∈ {deepseek, gemini, openai, anthropic}.

    DB key 'deepseek_api_key' / 'gemini_api_key' / ...
    Env fallback: DEEPSEEK_API_KEY / GEMINI_API_KEY / ...
    """
    db_key = f"{provider}_api_key"
    env_key = f"{provider.upper()}_API_KEY"
    try:
        from app_ctx import load_config
        v = (load_config() or {}).get(db_key)
        if v:
            return str(v).strip()
    except Exception:
        pass
    v = os.environ.get(env_key)
    if v:
        return v.strip()
    return None


def get_gemini_model() -> str:
    try:
        from app_ctx import load_config
        v = (load_config() or {}).get("gemini_model")
        if v:
            return str(v).strip()
    except Exception:
        pass
    return os.environ.get("GEMINI_MODEL", "gemini-2.0-pro-exp")
