#!/usr/bin/env python3
"""Chỉ kéo tên page từ FB Graph API (KHÔNG sync spend, không lấy từ POS).

Logic:
  1. Gom UNIQUE token từ JSON store + pa_fb_tokens.
  2. Sort token theo "độ tốt" (quản nhiều page nhất qua /me/accounts).
  3. Với mỗi token, batch fetch tên TẤT CẢ page chưa resolve.
  4. Token nào resolve được thì update DB, page đó loại khỏi remaining.
  5. Lặp đến hết token hoặc hết page.

Chạy: python scripts/quick_fetch_page_names.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Dict, List, Set

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_conn  # noqa: E402
from facebook_ads_tokens.storage import load_store  # noqa: E402
from facebook_ads_tokens.resolve import fb_ads_tokens_store_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v20.0"
BATCH_SIZE = 50


def load_all_tokens() -> List[str]:
    """Trả về list token DEDUPED, không sort."""
    toks: List[str] = []
    seen: Set[str] = set()

    # 1. JSON store
    try:
        records = load_store(fb_ads_tokens_store_path())
        for r in records:
            t = (getattr(r, "access_token", "") or "").strip()
            if t and t not in seen:
                seen.add(t)
                toks.append(t)
    except Exception as exc:
        logger.warning("Token store fail: %s", exc)

    # 2. pa_fb_tokens (global)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT access_token FROM pa_fb_tokens WHERE access_token <> ''")
                for (t,) in cur.fetchall():
                    t = (t or "").strip()
                    if t and t not in seen:
                        seen.add(t)
                        toks.append(t)
    except Exception:
        pass

    return toks


def is_token_alive(token: str) -> bool:
    """Test token bằng /me — pass nếu trả 200."""
    try:
        r = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/me",
            params={"access_token": token, "fields": "id"},
            timeout=10,
        )
        return r.status_code == 200 and "error" not in r.json()
    except Exception:
        return False


def find_missing_pages() -> Set[str]:
    """Trả Set[page_id] có spend nhưng chưa có page_name."""
    out: Set[str] = set()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT page_id FROM fb_ads_page_daily_spend
                WHERE (page_name IS NULL OR page_name = '')
            """)
            for (pid,) in cur.fetchall():
                if pid:
                    out.add(str(pid))
    return out


def batch_fetch(token: str, page_ids: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for i in range(0, len(page_ids), BATCH_SIZE):
        chunk = page_ids[i:i + BATCH_SIZE]
        batch = [{"method": "GET", "relative_url": f"{pid}?fields=name"} for pid in chunk]
        try:
            resp = requests.post(
                f"https://graph.facebook.com/{GRAPH_API_VERSION}/",
                data={"access_token": token, "batch": json.dumps(batch)},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            results = resp.json()
            for pid, item in zip(chunk, results):
                if not item or item.get("code") != 200:
                    continue
                try:
                    body = json.loads(item.get("body") or "{}")
                    name = body.get("name")
                    if name:
                        names[pid] = name
                except Exception:
                    pass
        except Exception:
            continue
    return names


def update_db(name_map: Dict[str, str]) -> int:
    if not name_map:
        return 0
    updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for pid, name in name_map.items():
                cur.execute("""
                    UPDATE fb_ads_page_daily_spend
                    SET page_name = %s
                    WHERE page_id = %s AND (page_name IS NULL OR page_name = '')
                """, (name, pid))
                cur.execute("""
                    INSERT INTO fb_pages (page_id, page_name)
                    VALUES (%s, %s)
                    ON CONFLICT (page_id) DO UPDATE
                      SET page_name = EXCLUDED.page_name
                      WHERE fb_pages.page_name IS NULL OR fb_pages.page_name = ''
                """, (pid, name))
                updated += 1
        conn.commit()
    return updated


def main():
    t0 = time.time()
    remaining = find_missing_pages()
    total = len(remaining)
    logger.info("Cần fetch %d page.", total)
    if not remaining:
        return

    all_tokens = load_all_tokens()
    logger.info("Có %d unique token. Test xem token nào còn sống...", len(all_tokens))
    live_tokens = [t for t in all_tokens if is_token_alive(t)]
    logger.info("Token còn sống: %d/%d", len(live_tokens), len(all_tokens))

    all_resolved: Dict[str, str] = {}
    for idx, tok in enumerate(live_tokens, 1):
        if not remaining:
            break
        logger.info("[Token %d/%d ...%s] thử %d page...",
                    idx, len(live_tokens), tok[-8:], len(remaining))
        names = batch_fetch(tok, list(remaining))
        if names:
            logger.info("  → resolved %d page", len(names))
            all_resolved.update(names)
            remaining -= set(names.keys())

    updated = update_db(all_resolved)
    logger.info("=== DONE %.1fs ===", time.time() - t0)
    logger.info("  Resolved %d/%d page (%.0f%%)",
                len(all_resolved), total,
                100 * len(all_resolved) / total if total else 0)
    logger.info("  DB updated: %d dòng", updated)
    if remaining:
        logger.warning("  Còn %d page KHÔNG token nào lấy được tên:", len(remaining))
        logger.warning("  → Page có thể bị xoá / không thuộc app nào của các token này.")
        # In ra max 10 page ID để debug
        sample = list(remaining)[:10]
        logger.warning("  Sample: %s%s", sample, "..." if len(remaining) > 10 else "")


if __name__ == "__main__":
    main()
