#!/usr/bin/env python3
"""Sync LINK LANDING PAGE (ladipage) của từng ad → bảng fb_ad_landing_links.

Ad chạy CHUYỂN ĐỔI/website có link đích trong creative (link_data.link /
asset_feed_spec.link_urls / call_to_action.value.link). Ad chạy TIN NHẮN không
có link → bỏ qua (đúng scope trang "Landing Pages").

Quét mọi TK đọc được bằng token cache (như sync_fb_ads_by_page). Chỉ upsert ad
CÓ link. Số liệu chi tiêu KHÔNG lưu ở đây — trang web join mb_fb_entity_daily
theo ad_id.

Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/sync_fb_ad_landing_links.py
"""
import json
import logging
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_fb_ad_landing_links")

GRAPH = "https://graph.facebook.com/v20.0"
FIELDS = ("name,status,campaign{id,name},adset{id,name},"
          "creative{object_story_id,object_story_spec,asset_feed_spec,link_url,url_tags}")


def ensure_table() -> None:
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fb_ad_landing_links (
                ad_id         VARCHAR(64) PRIMARY KEY,
                account_id    VARCHAR(64) NOT NULL,
                ad_name       TEXT        DEFAULT '',
                status        VARCHAR(32) DEFAULT '',
                campaign_id   VARCHAR(64) DEFAULT '',
                campaign_name TEXT        DEFAULT '',
                link          TEXT        NOT NULL,
                domain        TEXT        DEFAULT '',
                url_tags      TEXT        DEFAULT '',
                synced_at     TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE fb_ad_landing_links ADD COLUMN IF NOT EXISTS page_id VARCHAR(64) DEFAULT '';
            CREATE INDEX IF NOT EXISTS idx_fall_account ON fb_ad_landing_links(account_id);
            CREATE INDEX IF NOT EXISTS idx_fall_domain  ON fb_ad_landing_links(domain);
        """)
        conn.commit()


def _extract_page_id(creative: dict) -> str:
    """Page chạy ad: object_story_spec.page_id, fallback prefix của object_story_id."""
    spec = creative.get("object_story_spec") or {}
    pid = str(spec.get("page_id") or "")
    if not pid:
        story = str(creative.get("object_story_id") or "")
        if "_" in story:
            pid = story.split("_")[0]
    return pid


def _extract_link(creative: dict) -> str:
    """Rút link đích từ creative — thứ tự ưu tiên như đã test thật 2026-06-29."""
    spec = creative.get("object_story_spec") or {}
    ld = spec.get("link_data") or {}
    vd = spec.get("video_data") or {}
    afs = creative.get("asset_feed_spec") or {}
    link_urls = afs.get("link_urls") or []
    return (
        ld.get("link")
        or creative.get("link_url")
        or ((ld.get("call_to_action") or {}).get("value") or {}).get("link")
        or ((vd.get("call_to_action") or {}).get("value") or {}).get("link")
        or (link_urls[0].get("website_url") if link_urls else None)
        or ""
    )


def _fetch_ads(aid: str, token: str) -> list:
    """Toàn bộ ads của TK (paginate)."""
    out, url = [], (f"{GRAPH}/act_{aid}/ads?" + urllib.parse.urlencode(
        {"fields": FIELDS, "limit": "200", "access_token": token}))
    while url:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.load(resp)
        out.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
        if len(out) > 5000:   # chặn TK bất thường
            break
    return out


def main() -> None:
    from db import get_conn
    from scripts.sync_fb_ads_by_page import _build_acct_token_map

    ensure_table()
    acct_tokens = _build_acct_token_map()
    logger.info("=== sync landing links: %d TK đọc được ===", len(acct_tokens))

    total_ads = total_links = err = 0
    with get_conn() as conn:
        for aid, token in acct_tokens.items():
            try:
                ads = _fetch_ads(aid, token)
            except Exception as exc:
                logger.warning("act_%s: lỗi fetch (%s)", aid, str(exc)[:70])
                err += 1
                continue
            n_link = 0
            with conn.cursor() as cur:
                for ad in ads:
                    total_ads += 1
                    cr = ad.get("creative") or {}
                    link = _extract_link(cr)
                    if not link:
                        continue
                    camp = ad.get("campaign") or {}
                    dom = (urlparse(link).netloc or "").lower().removeprefix("www.")
                    cur.execute("""
                        INSERT INTO fb_ad_landing_links
                            (ad_id, account_id, ad_name, status, campaign_id,
                             campaign_name, link, domain, url_tags, page_id, synced_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (ad_id) DO UPDATE SET
                            account_id=EXCLUDED.account_id, ad_name=EXCLUDED.ad_name,
                            status=EXCLUDED.status, campaign_id=EXCLUDED.campaign_id,
                            campaign_name=EXCLUDED.campaign_name, link=EXCLUDED.link,
                            domain=EXCLUDED.domain, url_tags=EXCLUDED.url_tags,
                            page_id=CASE WHEN EXCLUDED.page_id <> '' THEN EXCLUDED.page_id
                                         ELSE fb_ad_landing_links.page_id END,
                            synced_at=NOW()
                    """, (str(ad.get("id")), aid, ad.get("name", ""),
                          ad.get("status", ""), str(camp.get("id", "")),
                          camp.get("name", ""), link, dom,
                          (cr.get("url_tags") or ""), _extract_page_id(cr)))
                    n_link += 1
            conn.commit()
            total_links += n_link
            if n_link:
                logger.info("act_%s: %d/%d ad có link", aid, n_link, len(ads))
    logger.info("=== DONE: %d ad quét, %d ad có link landing, %d TK lỗi ===",
                total_ads, total_links, err)


if __name__ == "__main__":
    main()
