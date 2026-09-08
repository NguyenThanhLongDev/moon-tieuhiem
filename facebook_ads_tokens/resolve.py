from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .storage import find_record_index, load_store

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def fb_ads_tokens_store_path() -> Path:
    """Same default as CLI: env FB_ADS_TOKENS_STORE or <repo>/facebook_ads_tokens_store.json."""
    raw = str(os.getenv("FB_ADS_TOKENS_STORE", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "facebook_ads_tokens_store.json"


def normalize_ad_account_id(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("act_"):
        text = text[4:]
    return text


def try_store_access_token(shop_key: str, fb_ad_account_id: str) -> Optional[str]:
    """
    Return access_token from JSON store for (shop_key, ad_account_id) or None.
    On store read errors, logs and returns None (caller may fall back to global token).
    """
    sk = str(shop_key or "").strip()
    aid = normalize_ad_account_id(fb_ad_account_id)
    try:
        path = fb_ads_tokens_store_path()
        records = load_store(path)
    except Exception as exc:
        logger.warning("FB token store read failed: %s", exc)
        return None
    idx = find_record_index(records, sk, aid)
    if idx is None:
        return None
    tok = str(records[idx].access_token or "").strip()
    return tok or None
