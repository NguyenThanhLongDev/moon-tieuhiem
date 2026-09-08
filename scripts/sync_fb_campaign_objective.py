#!/usr/bin/env python3
"""Sync objective (mục tiêu) của từng campaign FB → bảng fb_campaign_objective.

Dùng để phân loại chi tiêu: OUTCOME_SALES/OUTCOME_LEADS = chạy ladipage (chuyển đổi);
OUTCOME_ENGAGEMENT/OUTCOME_AWARENESS/... = tương tác/tin nhắn (không phải ladipage).

Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/sync_fb_campaign_objective.py
"""
from __future__ import annotations
import os, sys, json, logging, urllib.request, urllib.parse, urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_fb_campaign_objective")

GRAPH = "https://graph.facebook.com/v20.0"


def _fetch_campaigns(aid: str, token: str) -> list:
    out, url = [], (f"{GRAPH}/act_{aid}/campaigns?" + urllib.parse.urlencode(
        {"fields": "id,name,objective", "limit": "300", "access_token": token}))
    while url:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.load(resp)
        out.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
        if len(out) > 5000:
            break
    return out


def main() -> None:
    from db import get_conn
    from scripts.sync_fb_ads_by_page import _build_acct_token_map

    acct_tokens = _build_acct_token_map()
    logger.info("=== sync objective: %d TK đọc được ===", len(acct_tokens))
    total = err = 0
    with get_conn() as conn:
        for aid, token in acct_tokens.items():
            try:
                camps = _fetch_campaigns(aid, token)
            except Exception as exc:
                logger.warning("act_%s: lỗi (%s)", aid, str(exc)[:70])
                err += 1
                continue
            with conn.cursor() as cur:
                for c in camps:
                    cur.execute("""
                        INSERT INTO fb_campaign_objective (campaign_id, account_id, name, objective, updated_at)
                        VALUES (%s,%s,%s,%s,NOW())
                        ON CONFLICT (campaign_id) DO UPDATE SET
                            account_id=EXCLUDED.account_id, name=EXCLUDED.name,
                            objective=EXCLUDED.objective, updated_at=NOW()
                    """, (str(c.get("id")), aid, c.get("name", ""), c.get("objective", "")))
                    total += 1
            conn.commit()
    logger.info("=== DONE: %d campaign, %d TK lỗi ===", total, err)


if __name__ == "__main__":
    main()
