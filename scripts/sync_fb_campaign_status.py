#!/usr/bin/env python3
"""Kéo TRẠNG THÁI campaign từ Facebook về bảng fb_campaign_status.

Lấy MỌI campaign (gồm ACTIVE / PAUSED / DELETED / ARCHIVED) của từng TK QC →
dùng cho tính năng "Page → TK & Camp" để hiện trạng thái + bắt campaign bị xoá.

Token: env FACEBOOK_ACCESS_TOKEN (token global user phong 2). Account: pa_ad_accounts.
Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/sync_fb_campaign_status.py
"""
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_fb_campaign_status")

GRAPH = "https://graph.facebook.com/v20.0"
STATUSES = ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED",
            "CAMPAIGN_PAUSED", "ADSET_PAUSED", "DISAPPROVED", "PENDING_REVIEW"]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {}
    except Exception as e:
        return {"_err": str(e)}


def fetch_campaigns(token, account_id):
    """Trả [(campaign_id, name, effective_status)] cho 1 TK (gồm cả đã xoá/lưu trữ)."""
    flt = json.dumps([{"field": "effective_status", "operator": "IN", "value": STATUSES}])
    out = []
    url = (f"{GRAPH}/act_{account_id}/campaigns?fields=name,effective_status"
           f"&filtering={urllib.parse.quote(flt)}&limit=200&access_token={urllib.parse.quote(token)}")
    pages = 0
    while url and pages < 30:
        js = _get(url)
        if "data" not in js:
            err = js.get("error", {}).get("message", "")
            if err:
                logger.info("  [%s] %s", account_id, err[:70])
            break
        for c in js["data"]:
            out.append((str(c.get("id", "")), c.get("name", ""), c.get("effective_status", "")))
        url = (js.get("paging", {}).get("next"))
        pages += 1
    return out


def main():
    token = (os.getenv("FACEBOOK_ACCESS_TOKEN", "") or "").strip()
    if not token:
        logger.error("Thiếu FACEBOOK_ACCESS_TOKEN trong env.")
        sys.exit(1)

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT REPLACE(account_id,'act_','') FROM pa_ad_accounts")
            accounts = [str(r[0]) for r in cur.fetchall()]

    logger.info("=== sync campaign status: %d TK ===", len(accounts))
    total = 0
    ok_acct = 0
    with get_conn() as conn:
        for aid in accounts:
            camps = fetch_campaigns(token, aid)
            if not camps:
                continue
            ok_acct += 1
            with conn.cursor() as cur:
                for cid, name, status in camps:
                    if not cid:
                        continue
                    cur.execute(
                        """INSERT INTO fb_campaign_status (campaign_id, account_id, name, effective_status, updated_at)
                           VALUES (%s,%s,%s,%s, now())
                           ON CONFLICT (campaign_id) DO UPDATE SET
                             account_id=EXCLUDED.account_id, name=EXCLUDED.name,
                             effective_status=EXCLUDED.effective_status, updated_at=now()""",
                        (cid, aid, name[:512], status),
                    )
                    total += 1
            conn.commit()
    logger.info("=== DONE: %d campaign (status) từ %d/%d TK gọi được ===", total, ok_acct, len(accounts))


if __name__ == "__main__":
    main()
