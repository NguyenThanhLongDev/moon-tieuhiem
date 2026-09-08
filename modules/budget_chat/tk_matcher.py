"""Fuzzy match tk_name NV gõ → fb_ad_account_id từ pa_ad_accounts.

Pattern thường thấy:
  "Dang25.3"     → match "DangTH25.3 - A+ 5.2 - ..." trong account_name
  "TuyetTH22.1"  → match thẳng prefix account_name
  "Huy18.3"      → match "Huy18.3 - ..." prefix

Algorithm:
  1. Normalize: lowercase, bỏ space, dấu cách
  2. Trong pa_ad_accounts, lấy phần TRƯỚC dấu "-" của account_name (vd "DangTH25.3")
  3. Match exact first, sau đó match starts-with, sau đó difflib ratio
  4. Return (fb_ad_account_id, confidence 0..1)
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

# Cache TK list — refresh khi cần
_CACHE: dict = {"rows": [], "loaded_at": 0}
_CACHE_TTL_SEC = 300


def _normalize(s: str) -> str:
    """Bỏ dấu, space, lowercase. KHÔNG bỏ chữ số/dấu chấm."""
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "", s)
    return s


def _short_name(account_name: str) -> str:
    """Lấy phần đầu trước ' - ' của account_name. VD:
    'DangTH25.3 - A+ 5.2 - 35 - TIEUHIEM' → 'DangTH25.3'
    """
    if not account_name:
        return ""
    first = account_name.split(" - ")[0].strip()
    return first


def _load_all_accounts() -> list[dict]:
    """Load TK list từ pa_ad_accounts (cache 5 phút)."""
    import time
    now = time.time()
    if _CACHE["rows"] and (now - _CACHE["loaded_at"]) < _CACHE_TTL_SEC:
        return _CACHE["rows"]
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT account_id, account_name
                      FROM pa_ad_accounts
                     ORDER BY account_id
                """)
                rows = []
                for aid, name in cur.fetchall():
                    rows.append({
                        "ad_account_id": aid.replace("act_", "") if aid else "",
                        "raw_account_id": aid,
                        "account_name": name or "",
                        "short": _short_name(name or ""),
                        "short_norm": _normalize(_short_name(name or "")),
                    })
                _CACHE["rows"] = rows
                _CACHE["loaded_at"] = now
                return rows
    except Exception as exc:
        logger.warning("_load_all_accounts error: %s", exc)
        return []


def match_tk_name(tk_name_raw: str) -> tuple[Optional[str], float, Optional[str]]:
    """Trả (fb_ad_account_id, confidence, account_name).

    fb_ad_account_id KHÔNG có 'act_' prefix (dùng cho fb_ad_account_mappings).
    confidence:
      1.0 = exact match short_name
      0.9 = starts-with match
      0.7-0.89 = fuzzy ratio cao
      < 0.7 = không tin cậy → trả None
    """
    if not tk_name_raw:
        return None, 0.0, None
    q = _normalize(tk_name_raw)
    if not q:
        return None, 0.0, None

    accounts = _load_all_accounts()
    if not accounts:
        return None, 0.0, None

    # 1) Exact match short_name (normalized)
    for a in accounts:
        if a["short_norm"] == q:
            return a["ad_account_id"], 1.0, a["account_name"]

    # 2) Starts-with match
    starts = [a for a in accounts if a["short_norm"].startswith(q) or q.startswith(a["short_norm"])]
    if len(starts) == 1:
        a = starts[0]
        return a["ad_account_id"], 0.9, a["account_name"]
    if len(starts) > 1:
        # Pick best by length similarity
        best = max(starts, key=lambda a: SequenceMatcher(None, q, a["short_norm"]).ratio())
        ratio = SequenceMatcher(None, q, best["short_norm"]).ratio()
        if ratio >= 0.7:
            return best["ad_account_id"], min(ratio, 0.89), best["account_name"]

    # 3) Fuzzy ratio across all
    best = None
    best_ratio = 0.0
    for a in accounts:
        r = SequenceMatcher(None, q, a["short_norm"]).ratio()
        if r > best_ratio:
            best_ratio = r
            best = a
    if best and best_ratio >= 0.7:
        return best["ad_account_id"], best_ratio, best["account_name"]

    return None, 0.0, None


def invalidate_cache():
    """Force reload — gọi sau khi sync pa_ad_accounts."""
    _CACHE["rows"] = []
    _CACHE["loaded_at"] = 0
