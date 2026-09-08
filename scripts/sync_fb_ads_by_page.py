"""Sync Facebook Ads spend per page (not per account).

Approach — no manual page mapping needed:
  1. For each ad account token, fetch all ads + creatives.
  2. Extract page_id from creative.object_story_id  (format: "{page_id}_{post_id}").
  3. Fetch ad-level insights for target date(s).
  4. Join insights → creative map → group spend by page_id.
  5. Upsert into fb_ads_page_daily_spend.

Usage:
  python scripts/sync_fb_ads_by_page.py --date 2026-04-11
  python scripts/sync_fb_ads_by_page.py --date-from 2026-04-01 --date-to 2026-04-11
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn
from facebook_ads_tokens.client import FacebookAdsClient, normalize_ad_account_id
from facebook_ads_tokens.storage import load_store
from facebook_ads_tokens.resolve import fb_ads_tokens_store_path, try_store_access_token

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GRAPH_API_VERSION = os.getenv("FACEBOOK_GRAPH_API_VERSION", "v20.0")


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_table() -> None:
    """Create both fb_ads_page_daily_spend and fb_ad_account_info tables."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fb_ads_page_daily_spend (
                    id          BIGSERIAL PRIMARY KEY,
                    fb_ad_account_id VARCHAR NOT NULL,
                    page_id     VARCHAR NOT NULL,
                    page_name   VARCHAR,
                    metric_date DATE NOT NULL,
                    spend       NUMERIC DEFAULT 0,
                    impressions BIGINT  DEFAULT 0,
                    clicks      BIGINT  DEFAULT 0,
                    synced_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE(fb_ad_account_id, page_id, metric_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_fadps_date
                ON fb_ads_page_daily_spend(metric_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_fadps_page_date
                ON fb_ads_page_daily_spend(page_id, metric_date)
            """)
            # Account info cache table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fb_ad_account_info (
                    ad_account_id VARCHAR PRIMARY KEY,
                    account_name  VARCHAR,
                    updated_at    TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()


def upsert_account_info(ad_account_id: str, account_name: str, currency: str = "VND") -> None:
    """Store/update the display name + currency for an ad account."""
    if not account_name or not ad_account_id:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_ad_account_info (ad_account_id, account_name, currency, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (ad_account_id) DO UPDATE
                        SET account_name = EXCLUDED.account_name,
                            currency     = EXCLUDED.currency,
                            updated_at   = NOW()
                """, (ad_account_id, account_name, currency))
            conn.commit()
    except Exception as exc:
        logger.warning("upsert_account_info error: %s", exc)


def fetch_account_info_from_api(token: str, ad_account_id: str) -> Dict[str, Optional[str]]:
    """Fetch ad account name + currency from Graph API.
    Trả về {'name': ..., 'currency': 'USD'|'VND'|...}.
    """
    try:
        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{ad_account_id}"
        resp = requests.get(url, params={"fields": "name,currency", "access_token": token}, timeout=15)
        resp.raise_for_status()
        d = resp.json()
        return {"name": d.get("name"), "currency": (d.get("currency") or "VND").upper()}
    except Exception as exc:
        logger.warning("[%s] Could not fetch account info: %s", ad_account_id, exc)
        return {"name": None, "currency": "VND"}


def fetch_account_name_from_api(token: str, ad_account_id: str) -> Optional[str]:
    """Legacy helper — chỉ trả name. Code mới dùng fetch_account_info_from_api."""
    return fetch_account_info_from_api(token, ad_account_id).get("name")


def upsert_page_spend(
    fb_ad_account_id: str,
    page_id: str,
    page_name: Optional[str],
    metric_date: str,
    spend: float,
    impressions: int,
    clicks: int,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_ads_page_daily_spend
                    (fb_ad_account_id, page_id, page_name, metric_date, spend, impressions, clicks, synced_at)
                VALUES (%s, %s, %s, %s::date, %s, %s, %s, NOW())
                ON CONFLICT (fb_ad_account_id, page_id, metric_date)
                DO UPDATE SET
                    page_name   = COALESCE(EXCLUDED.page_name, fb_ads_page_daily_spend.page_name),
                    spend       = EXCLUDED.spend,
                    impressions = EXCLUDED.impressions,
                    clicks      = EXCLUDED.clicks,
                    synced_at   = NOW()
            """, (
                normalize_ad_account_id(fb_ad_account_id),
                page_id,
                page_name or "",
                metric_date,
                spend,
                impressions,
                clicks,
            ))
        conn.commit()


# ── Facebook API helpers ──────────────────────────────────────────────────────

def upsert_entity_daily(
    metric_date: str,
    account_id: str,
    row: Dict[str, Any],
    spend_vnd: float,
    page_id: str,
    post_id: str,
) -> None:
    """Marketing Brain GĐ1: persist ad-level insights row → mb_fb_entity_daily.

    Trước đây data này fetch về rồi vứt sau khi group theo page — giờ giữ lại.
    spend đã quy VND (cùng convention fb_ads_page_daily_spend, CHƯA VAT).
    """
    purchases, purchase_value = _extract_purchases(row)
    registrations = _extract_registrations(row)   # đơn Meta (form/mua) cho page TEST
    reach = row.get("reach")
    frequency = row.get("frequency")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mb_fb_entity_daily
                    (metric_date, account_id, campaign_id, campaign_name,
                     adset_id, adset_name, ad_id, ad_name, page_id, post_id,
                     spend, impressions, clicks, reach, frequency,
                     purchases, purchase_value, registrations)
                VALUES (%s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (metric_date, ad_id)
                DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    campaign_id = EXCLUDED.campaign_id,
                    campaign_name = EXCLUDED.campaign_name,
                    adset_id = EXCLUDED.adset_id,
                    adset_name = EXCLUDED.adset_name,
                    ad_name = EXCLUDED.ad_name,
                    page_id = CASE WHEN EXCLUDED.page_id <> '' THEN EXCLUDED.page_id ELSE mb_fb_entity_daily.page_id END,
                    post_id = CASE WHEN EXCLUDED.post_id <> '' THEN EXCLUDED.post_id ELSE mb_fb_entity_daily.post_id END,
                    spend = EXCLUDED.spend,
                    impressions = EXCLUDED.impressions,
                    clicks = EXCLUDED.clicks,
                    reach = EXCLUDED.reach,
                    frequency = EXCLUDED.frequency,
                    purchases = EXCLUDED.purchases,
                    purchase_value = EXCLUDED.purchase_value,
                    registrations = EXCLUDED.registrations,
                    updated_at = NOW()
            """, (
                metric_date,
                account_id,
                str(row.get("campaign_id") or ""),
                str(row.get("campaign_name") or "")[:512],
                str(row.get("adset_id") or ""),
                str(row.get("adset_name") or "")[:512],
                str(row.get("ad_id") or ""),
                str(row.get("ad_name") or "")[:512],
                page_id or "",
                post_id or "",
                spend_vnd,
                int(row.get("impressions", 0) or 0),
                int(row.get("clicks", 0) or 0),
                int(reach) if reach not in (None, "") else None,
                float(frequency) if frequency not in (None, "") else None,
                purchases,
                purchase_value,
                registrations,
            ))
        conn.commit()


def _upsert_ads_registry(account_id: str, ad_page_map: Dict[str, str], ad_post_map: Dict[str, str]) -> None:
    """Bulk upsert sổ ad→TK. ad_page_map chứa MỌI ad của TK (kể cả paused)."""
    if not ad_page_map:
        return
    rows = [
        (ad_id, account_id, page_id or "", ad_post_map.get(ad_id, ""))
        for ad_id, page_id in ad_page_map.items()
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            from psycopg2.extras import execute_values
            execute_values(cur, """
                INSERT INTO mb_fb_ads_registry (ad_id, account_id, page_id, post_id)
                VALUES %s
                ON CONFLICT (ad_id) DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    page_id = CASE WHEN EXCLUDED.page_id <> '' THEN EXCLUDED.page_id ELSE mb_fb_ads_registry.page_id END,
                    post_id = CASE WHEN EXCLUDED.post_id <> '' THEN EXCLUDED.post_id ELSE mb_fb_ads_registry.post_id END,
                    last_seen = NOW()
            """, rows)
        conn.commit()


# Ưu tiên action_type như sync_facebook_ads_to_db (§11.8 CLAUDE.md) — đừng đổi thứ tự
_PURCHASE_ACTION_PRIORITY = (
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
)

# "Đơn" cho page ladipage/chuyển đổi = điền form (complete_registration) hoặc mua.
# Ladipage đa số đếm bằng complete_registration, KHÔNG bắn purchase.
_REGISTRATION_ACTION_PRIORITY = (
    "offsite_conversion.fb_pixel_complete_registration",
    "omni_complete_registration",
    "complete_registration",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
)


def _extract_registrations(row: Dict[str, Any]):
    """Số "đơn" chuyển đổi Meta = form đặt hàng (complete_registration) ưu tiên,
    fallback purchase. Dùng cho số đơn page TEST (ladipage)."""
    by_type = {}
    for it in row.get("actions") or []:
        try:
            by_type[str(it.get("action_type") or "")] = float(it.get("value", 0) or 0)
        except (TypeError, ValueError):
            continue
    for atype in _REGISTRATION_ACTION_PRIORITY:
        if atype in by_type:
            return int(by_type[atype])
    return 0


def _extract_purchases(row: Dict[str, Any]):
    """Trả (purchase_count, purchase_value) từ actions[]/action_values[] của 1 insights row."""
    def _pick(items):
        by_type = {}
        for it in items or []:
            atype = str(it.get("action_type") or "")
            try:
                by_type[atype] = float(it.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
        for atype in _PURCHASE_ACTION_PRIORITY:
            if atype in by_type:
                return by_type[atype]
        return None

    count = _pick(row.get("actions"))
    value = _pick(row.get("action_values"))
    return (int(count) if count is not None else None,
            value if value is not None else None)


def build_ad_page_map(client: FacebookAdsClient, ad_account_id: str):
    """Return ({ad_id: page_id}, {ad_id: object_story_id}) by fetching all ads + creatives.

    Phân trang ĐÚNG bằng cursor `after`: mỗi vòng lấy trang KẾ TIẾP rồi advance
    cursor. (Bản cũ gọi lại trang đầu mỗi vòng + cursor không tiến → lặp vô tận
    với TK >2 trang ad, khiến sync treo và bảng page-level rỗng.)
    Có cap số trang để chống treo nếu Graph trả paging bất thường.
    """
    ad_map: Dict[str, str] = {}
    # Marketing Brain: giữ lại object_story_id ({page_id}_{post_id} — cùng format
    # post_id trong payload đơn Pancake) thay vì vứt sau khi split lấy page_id.
    ad_post_map: Dict[str, str] = {}
    aid = normalize_ad_account_id(ad_account_id)
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{aid}/ads"
    # Lấy thêm object_story_spec.page_id: ad "Lượt tương tác" (engagement) KHÔNG có
    # object_story_id, page_id nằm trong object_story_spec.page_id → phải đọc cả 2.
    base_params = {
        # effective_object_story_id: ad Advantage+/A+/dynamic thường KHÔNG có
        # object_story_id nhưng CÓ effective_object_story_id (cùng format {page_id}_{post_id})
        # → resolve thêm được nhiều ad, giảm spend rớt khỏi page-table.
        "fields": "id,name,status,creative{object_story_id,effective_object_story_id,object_story_spec}",
        "limit": 200,
    }
    after: Optional[str] = None
    fetched = 0
    MAX_PAGES = 500  # cap an toàn: 500 × 200 = 100k ad
    pages = 0
    for pages in range(1, MAX_PAGES + 1):
        params = dict(base_params)
        if after:
            params["after"] = after
        data = client._request("GET", url, params)
        ads = data.get("data", [])
        for ad in ads:
            creative = ad.get("creative") or {}
            story_id = str(creative.get("object_story_id") or "")
            if "_" not in story_id:
                # Advantage+/A+/dynamic: dùng effective_object_story_id (cùng format)
                story_id = str(creative.get("effective_object_story_id") or "")
            page_id = None
            if "_" in story_id:
                page_id = story_id.split("_", 1)[0]
                ad_post_map[str(ad["id"])] = story_id
            else:
                # Fallback: ad engagement/lượt tương tác → page_id trong object_story_spec
                spec = creative.get("object_story_spec") or {}
                if spec.get("page_id"):
                    page_id = str(spec.get("page_id"))
            if page_id:
                ad_map[str(ad["id"])] = page_id
        fetched += len(ads)

        paging = data.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if not ads or not paging.get("next") or not after:
            break
    else:
        logger.warning(
            "  ad_page_map: đạt cap %d trang cho act_%s — có thể chưa quét hết ad",
            MAX_PAGES, aid,
        )

    logger.info(
        "  ad_page_map: %d ad (qua %d trang) → %d ad có page → %d page riêng biệt",
        fetched, pages, len(ad_map), len(set(ad_map.values())),
    )
    return ad_map, ad_post_map


def _merge_registry_pages(account_id: str, ad_page_map: Dict[str, str]) -> Dict[str, str]:
    """Bù page cho ad mà creative API lần này KHÔNG resolve được, lấy từ
    mb_fb_ads_registry (đã tích luỹ ad→page các lần trước, ON CONFLICT preserve).

    Chỉ THÊM page cho ad còn thiếu (không ghi đè map vừa resolve). Sửa bug [CPQC #3]:
    creative endpoint chập chờn → ad orphan bị rớt khỏi fb_ads_page_daily_spend (page-table
    thiếu spend dù account-total đủ) → POS/Pancake gộp đủ nên lệch âm. Registry giữ page bền
    nên dùng làm fallback. KHÔNG cần restart web (subprocess sync re-import mỗi lần chạy).
    """
    filled = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ad_id, page_id FROM mb_fb_ads_registry "
                    "WHERE account_id = %s AND page_id <> ''",
                    (account_id,),
                )
                for ad_id, page_id in cur.fetchall():
                    if str(ad_id) not in ad_page_map:
                        ad_page_map[str(ad_id)] = str(page_id)
                        filled += 1
        if filled:
            logger.info("[%s] bù %d ad→page từ registry (creative miss)", account_id, filled)
    except Exception as exc:
        logger.warning("[%s] merge registry pages lỗi: %s", account_id, exc)
    return ad_page_map


def _guess_primary_page(account_id: str) -> Optional[str]:
    """Page CHÍNH của 1 TK QC — dùng để dồn spend ad không resolve được page.

    Ưu tiên: page có tổng spend cao nhất của TK trong 90 ngày gần đây
    (fb_ads_page_daily_spend). Đây là page TK chạy chủ yếu.
    Trả None nếu TK chưa từng có page nào (không dồn được → log cảnh báo).
    """
    aid = normalize_ad_account_id(account_id)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT page_id
                  FROM fb_ads_page_daily_spend
                 WHERE fb_ad_account_id = %s
                   AND metric_date >= (CURRENT_DATE - 90)
                   AND page_id <> ''
                 GROUP BY page_id
                 ORDER BY SUM(spend) DESC
                 LIMIT 1
                """,
                (aid,),
            )
            r = cur.fetchone()
            return str(r[0]) if r and r[0] else None
    except Exception as exc:
        logger.warning("[%s] _guess_primary_page lỗi: %s", aid, exc)
        return None


def fetch_page_names(token: str, page_ids: set) -> Dict[str, str]:
    """Try to get page names from fb_pages DB table first, then Graph API."""
    names: Dict[str, str] = {}
    # 1. Check local fb_pages table
    try:
        remaining = set(page_ids)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT page_id, page_name FROM fb_pages WHERE page_id = ANY(%s)",
                    (list(remaining),)
                )
                for row in cur.fetchall():
                    names[row[0]] = row[1]
                    remaining.discard(row[0])
        if names:
            logger.info("  page names from DB: %d found", len(names))
    except Exception as exc:
        logger.warning("  fb_pages lookup error: %s", exc)

    # 1b. Tra pa_pages (tên thật từ promote_pages backfill — giống tieuhiem)
    try:
        if remaining:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT page_id, page_name FROM pa_pages "
                        "WHERE page_id = ANY(%s) AND COALESCE(page_name,'') <> ''",
                        (list(remaining),)
                    )
                    for row in cur.fetchall():
                        names[row[0]] = row[1]
                        remaining.discard(row[0])
    except Exception:
        pass

    # 2. For remaining page IDs, try existing fb_ads_page_daily_spend (cached names)
    try:
        if remaining:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT DISTINCT page_id, page_name
                           FROM fb_ads_page_daily_spend
                           WHERE page_id = ANY(%s) AND page_name IS NOT NULL AND page_name != ''""",
                        (list(remaining),)
                    )
                    for row in cur.fetchall():
                        names[row[0]] = row[1]
                        remaining.discard(row[0])
    except Exception:
        pass

    # 3. Fall back to Graph API batch for any still-unknown pages
    if remaining:
        remaining_list = list(remaining)
        BATCH_SIZE = 50
        logger.info("  fetching %d page names from Graph API...", len(remaining_list))
        for i in range(0, len(remaining_list), BATCH_SIZE):
            chunk = remaining_list[i:i + BATCH_SIZE]
            batch = [{"method": "GET", "relative_url": f"{pid}?fields=name"} for pid in chunk]
            try:
                resp = requests.post(
                    f"https://graph.facebook.com/{GRAPH_API_VERSION}/",
                    data={"access_token": token, "batch": json.dumps(batch)},
                    timeout=30,
                )
                resp.raise_for_status()
                results = resp.json()
                for pid, item in zip(chunk, results):
                    if item and item.get("code") == 200:
                        body = json.loads(item.get("body", "{}"))
                        name = body.get("name")
                        if name:
                            names[pid] = name
                            remaining.discard(pid)
            except Exception as exc:
                logger.warning("  Graph API batch page name error: %s", exc)

    if remaining:
        logger.warning("  Could not resolve names for %d pages: %s", len(remaining), list(remaining)[:5])

    return names


def _load_campaign_page_history(ad_account_id: str) -> Dict[str, str]:
    """campaign_id -> page_id, suy từ ad CÙNG campaign từng resolve page (trong TK).
    Dùng gán page cho ad mồ côi (creative không trả page_id) — thay logic dồn cũ."""
    aid = normalize_ad_account_id(ad_account_id)
    out: Dict[str, str] = {}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT campaign_id, page_id, COUNT(*) c FROM mb_fb_entity_daily
                    WHERE account_id=%s AND campaign_id<>'' AND page_id IS NOT NULL AND page_id<>''
                    GROUP BY campaign_id, page_id""",
                (aid,),
            )
            tmp: Dict[str, Dict[str, int]] = defaultdict(dict)
            for cid, pid, c in cur.fetchall():
                tmp[str(cid)][str(pid)] = int(c)
            for cid, d in tmp.items():
                out[cid] = max(d, key=d.get)
    except Exception as exc:
        logger.warning("[%s] load campaign_page_history lỗi: %s", aid, exc)
    return out


def _load_account_page_names(ad_account_id: str) -> Dict[str, str]:
    """page_id -> page_name của các page TK này từng chạy (để khớp tên campaign)."""
    aid = normalize_ad_account_id(ad_account_id)
    out: Dict[str, str] = {}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT page_id, page_name FROM fb_ads_page_daily_spend
                    WHERE fb_ad_account_id=%s AND page_name<>''""",
                (aid,),
            )
            for pid, nm in cur.fetchall():
                out[str(pid)] = nm
    except Exception as exc:
        logger.warning("[%s] load account_page_names lỗi: %s", aid, exc)
    return out


def _match_page_by_campaign_name(campaign_name: str, page_names: Dict[str, str]) -> Optional[str]:
    """Khớp tên page với tên campaign (page name là tiền tố / nằm trong campaign), lấy match DÀI nhất.
    Vd campaign 'Đồ cơ khí giá rẻ - thước đo' -> page 'Đồ cơ khí giá rẻ'."""
    camp = (campaign_name or "").strip().lower()
    if not camp:
        return None
    best, blen = None, -1
    for pid, nm in page_names.items():
        nml = (nm or "").strip().lower()
        if nml and (camp.startswith(nml) or nml in camp) and len(nml) > blen:
            best, blen = pid, len(nml)
    return best


def sync_account_for_dates(
    token: str,
    ad_account_id: str,
    dates: List[str],
) -> Dict[str, int]:
    """Sync page-level spend for one ad account across given dates.
    Returns {date: page_count} summary."""
    client = FacebookAdsClient(token)
    aid = normalize_ad_account_id(ad_account_id)

    # Step 0: Fetch and cache the account info (name + currency)
    acc_info = fetch_account_info_from_api(token, aid)
    acc_name = acc_info.get("name")
    acc_currency = (acc_info.get("currency") or "VND").upper()
    if acc_name:
        logger.info("[%s] Account: %s (%s)", aid, acc_name, acc_currency)
        upsert_account_info(aid, acc_name, acc_currency)

    # Step 1: Build ad → page map (once per account)
    logger.info("[%s] Building ad→page map...", aid)
    try:
        ad_page_map, ad_post_map = build_ad_page_map(client, aid)
    except Exception as exc:
        logger.error("[%s] Failed to fetch ads/creatives: %s", aid, exc)
        return {}

    # [CPQC #3] Bù page thiếu từ registry TRƯỚC khi group spend → ad creative chập chờn
    # không làm rớt spend khỏi page-table (account-total đủ nhưng page-split thiếu).
    ad_page_map = _merge_registry_pages(aid, ad_page_map)

    # Marketing Brain/Lương: lưu sổ đăng ký ad→TK (kể cả ad đã tắt) — để tra
    # ngược NV cho đơn về trễ sau khi ad ngừng spend.
    try:
        _upsert_ads_registry(aid, ad_page_map, ad_post_map)
    except Exception as exc:
        logger.warning("[%s] mb_fb_ads_registry upsert lỗi: %s", aid, exc)

    if not ad_page_map:
        logger.warning("[%s] No ads with page info found", aid)
        return {}

    # Step 2: Fetch page names (batch)
    unique_page_ids = set(ad_page_map.values())
    logger.info("[%s] Fetching names for %d pages...", aid, len(unique_page_ids))
    try:
        page_names = fetch_page_names(token, unique_page_ids)
    except Exception as exc:
        logger.warning("[%s] Could not fetch page names: %s", aid, exc)
        page_names = {}

    # Sổ tra page cho ad mồ côi (creative API không trả page_id) — KHÔNG dồn đại nữa.
    camp_page_hist = _load_campaign_page_history(aid)
    acct_page_names = _load_account_page_names(aid)

    # Step 3: For each date, get ad-level insights and group by page
    results: Dict[str, int] = {}
    for target_date in dates:
        logger.info("[%s] Fetching ad-level insights for %s...", aid, target_date)
        try:
            # Marketing Brain: thêm campaign/adset/reach/actions vào CÙNG call level=ad
            # sẵn có — FB trả kèm, KHÔNG tốn thêm request.
            data = client.get_ad_level_insights(
                aid, target_date, target_date,
                fields=(
                    "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
                    "spend,impressions,clicks,reach,frequency,actions,action_values,date_start"
                ),
            )
        except Exception as exc:
            logger.error("[%s] Insights error for %s: %s", aid, target_date, exc)
            results[target_date] = 0
            continue

        rows = data.get("data", [])
        if not rows:
            logger.info("[%s] No insight rows for %s", aid, target_date)
            results[target_date] = 0
            continue

        # Group spend by page_id
        from fb_currency import convert_to_vnd as _cvt_entity
        page_spend: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"spend": 0.0, "impressions": 0, "clicks": 0})

        # Sổ campaign->page từ ad resolve được TRONG ngày này (ưu tiên cao nhất).
        camp_page_today: Dict[str, str] = {}
        for _row in rows:
            _pid = ad_page_map.get(str(_row.get("ad_id", "")))
            _cid = str(_row.get("campaign_id") or "")
            if _pid and _cid:
                camp_page_today.setdefault(_cid, _pid)

        def _resolve_page(row) -> Optional[str]:
            """Page của ad: ưu tiên creative; ad mồ côi → suy theo campaign
            (cùng ngày → lịch sử TK → khớp tên page). KHÔNG dồn đại sang page cao nhất."""
            pid = ad_page_map.get(str(row.get("ad_id", "")))
            if pid:
                return pid
            cid = str(row.get("campaign_id") or "")
            return (camp_page_today.get(cid)
                    or camp_page_hist.get(cid)
                    or _match_page_by_campaign_name(row.get("campaign_name"), acct_page_names))

        # Bucket spend ad vẫn không suy được page → xử lý cuối vòng (KHÔNG dồn đại).
        unmapped = {"spend": 0.0, "impressions": 0, "clicks": 0}
        entity_saved = 0
        page_campaign_name: Dict[str, str] = {}  # [CPQC] page_id -> tên campaign (suy tên page mồ côi)
        for row in rows:
            ad_id = str(row.get("ad_id", ""))
            page_id = _resolve_page(row)
            if page_id and page_id not in page_campaign_name:
                _cn = str(row.get("campaign_name") or "").strip()
                if _cn:
                    page_campaign_name[page_id] = _cn
            # Marketing Brain: persist ad-level row (kể cả ad không resolve được page)
            if ad_id:
                try:
                    upsert_entity_daily(
                        metric_date=target_date,
                        account_id=aid,
                        row=row,
                        spend_vnd=_cvt_entity(float(row.get("spend", 0) or 0), acc_currency),
                        page_id=page_id or "",
                        post_id=ad_post_map.get(ad_id, ""),
                    )
                    entity_saved += 1
                except Exception as exc:
                    logger.warning("  [%s] mb_entity upsert %s lỗi: %s", aid, ad_id, exc)
            bucket = page_spend[page_id] if page_id else unmapped
            bucket["spend"]      += float(row.get("spend", 0) or 0)
            bucket["impressions"] += int(row.get("impressions", 0) or 0)
            bucket["clicks"]      += int(row.get("clicks", 0) or 0)
        if entity_saved:
            logger.info("  [%s] mb_fb_entity_daily: %d ad rows cho %s", aid, entity_saved, target_date)

        # Spend ad vẫn KHÔNG suy được page (campaign lạ, chưa có page khớp tên).
        # KHÔNG dồn sang page cao nhất (bug cũ [CPQC #4] làm 1 page phình + lệch Facebook).
        # Chỉ gộp khi KHÔNG THỂ nhầm: TK đúng 1 page, hoặc cả ngày không page nào resolve.
        if unmapped["spend"] > 0:
            target_page = None
            if len(page_spend) == 1:
                target_page = next(iter(page_spend))      # TK 1 page → chắc chắn đúng
            elif not page_spend:
                target_page = _guess_primary_page(aid)     # cả ngày không resolve → page chính TK
            if target_page:
                page_spend[target_page]["spend"]      += unmapped["spend"]
                page_spend[target_page]["impressions"] += unmapped["impressions"]
                page_spend[target_page]["clicks"]      += unmapped["clicks"]
            else:
                logger.warning(
                    "  [%s] %s: %.0f spend mồ côi đa-page KHÔNG suy được page → KHÔNG dồn đại, "
                    "cần map campaign→page (kiểm tên campaign vs tên page)",
                    aid, target_date, unmapped["spend"],
                )

        # Step 4: Upsert to DB — convert spend USD→VND nếu cần
        from fb_currency import convert_to_vnd as _cvt
        for page_id, metrics in page_spend.items():
            pname = page_names.get(page_id, "")
            if not pname:
                # [CPQC] Giống tieuhiem: page không đọc được tên FB (ngoài BM/không quyền)
                # → suy tên từ TÊN CAMPAIGN. 2 quy ước đặt tên:
                #   A) "Tên Page - ngày - sản phẩm"  → tên page ở ĐẦU
                #      vd "Tiện ích 4.0 - 17/6 - TÚI"            → "Tiện ích 4.0"
                #   B) "ngày - sản phẩm - Tên Page"  → tên page ở CUỐI
                #      vd "20/6 - dụng cụ vặn ốc - sửa chữa đa năng" → "Sửa Chữa Đa Năng"
                # Phân biệt bằng khúc ĐẦU: nếu là ngày → kiểu B (lấy CUỐI), ngược lại kiểu A (lấy ĐẦU).
                _camp = page_campaign_name.get(page_id, "")
                if _camp:
                    _segs = [s.strip() for s in _camp.split(" - ") if s.strip()]
                    _is_date = lambda s: bool(re.match(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$", s))
                    _cand = ""
                    if _segs and _is_date(_segs[0]):
                        if len(_segs) >= 2:
                            _cand = _segs[-1]          # kiểu B: tên page ở cuối
                    elif _segs and not _segs[0].isdigit():
                        _cand = _segs[0]               # kiểu A: tên page ở đầu
                    if _cand and not _is_date(_cand) and not _cand.isdigit():
                        # Campaign hay viết thường → chuẩn hoá hoa đầu chữ cho giống tên page
                        if _cand == _cand.lower():
                            _cand = _cand.title()
                        pname = _cand[:255]
            spend_vnd = _cvt(metrics["spend"], acc_currency)
            upsert_page_spend(
                fb_ad_account_id=aid,
                page_id=page_id,
                page_name=pname,
                metric_date=target_date,
                spend=spend_vnd,
                impressions=metrics["impressions"],
                clicks=metrics["clicks"],
            )
            logger.info(
                "  [%s] page=%s (%s) date=%s spend=%.0f impressions=%d clicks=%d",
                aid, page_id, pname or "?", target_date,
                metrics["spend"], metrics["impressions"], metrics["clicks"],
            )

        results[target_date] = len(page_spend)
        logger.info("[%s] %s: %d pages written", aid, target_date, len(page_spend))

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def resolve_dates(args: argparse.Namespace) -> List[str]:
    one = str(getattr(args, "date", "") or "").strip()
    df  = str(getattr(args, "date_from", "") or "").strip()
    dt  = str(getattr(args, "date_to", "") or "").strip()
    if one:
        datetime.strptime(one, "%Y-%m-%d")
        return [one]
    if df and dt:
        a = datetime.strptime(df, "%Y-%m-%d").date()
        b = datetime.strptime(dt, "%Y-%m-%d").date()
        if a > b:
            a, b = b, a
        out, cur = [], a
        while cur <= b:
            out.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return out
    raise SystemExit("Need --date YYYY-MM-DD or --date-from + --date-to")


def _get_global_fb_token() -> str:
    """Get the most recent valid access token from fb_pages table (used for BM-linked accounts)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT access_token FROM fb_pages "
                    "WHERE access_token IS NOT NULL AND access_token != '' "
                    "ORDER BY updated_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
                # [CPQC] fallback: token System User nhúng qua Page & Tài khoản (pa_fb_tokens)
                cur.execute(
                    "SELECT access_token FROM pa_fb_tokens "
                    "WHERE access_token IS NOT NULL AND access_token != '' "
                    "ORDER BY updated_at DESC LIMIT 1"
                )
                row2 = cur.fetchone()
                return row2[0] if row2 else ""
    except Exception as exc:
        logger.warning("Could not load global fb_pages token: %s", exc)
        return ""


def _get_db_mapped_accounts() -> List[Dict[str, str]]:
    """Load active ad accounts from fb_ad_account_mappings joined with shops."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.fb_ad_account_id, m.account_name, s.shop_key
                    FROM fb_ad_account_mappings m
                    JOIN shops s ON s.id = m.shop_id
                    WHERE m.status = 'active'
                    ORDER BY m.fb_ad_account_id
                """)
                return [
                    {"ad_account_id": r[0], "account_name": r[1], "shop_key": r[2]}
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.warning("Could not load db mapped accounts: %s", exc)
        return []


def load_all_tokens() -> List[Dict[str, str]]:
    """Load all tokens from the store file, merged with DB-mapped accounts using global fb_pages token."""
    path = fb_ads_tokens_store_path()
    store = load_store(path)
    results = []
    seen_accounts: set = set()

    for rec in (store if isinstance(store, list) else store.get("records", [])):
        token = getattr(rec, "access_token", None) or (rec.get("access_token") if isinstance(rec, dict) else None)
        acc   = getattr(rec, "ad_account_id", None) or (rec.get("ad_account_id") if isinstance(rec, dict) else None)
        key   = getattr(rec, "shop_key", None) or (rec.get("shop_key") if isinstance(rec, dict) else "")
        if token and acc:
            norm = normalize_ad_account_id(acc)
            results.append({"token": token, "ad_account_id": norm, "shop_key": key})
            seen_accounts.add(norm)

    # Also include accounts from fb_ad_account_mappings not already in the store
    db_accounts = _get_db_mapped_accounts()
    if db_accounts:
        global_token = _get_global_fb_token()
        if global_token:
            for acct in db_accounts:
                norm = normalize_ad_account_id(acct["ad_account_id"])
                if norm not in seen_accounts:
                    results.append({
                        "token": global_token,
                        "ad_account_id": norm,
                        "shop_key": acct["shop_key"],
                    })
                    seen_accounts.add(norm)
                    logger.info("Added DB-mapped account act_%s (shop=%s)", norm, acct["shop_key"])
        else:
            logger.warning("No global fb_pages token found; DB-mapped accounts will be skipped")

    # Cũng include TK gán cho NV qua user_ad_account_map (dù CHƯA map shop) —
    # nếu không, TK gán-trực-tiếp-không-shop sẽ không bao giờ được cron sync.
    # Token thật sẽ được resolve_working_token() chọn lại trong main() (thử shop
    # tokens + global env). Ở đây chỉ cần TK xuất hiện trong danh sách.
    try:
        fallback_tok = _get_global_fb_token() or _global_env_token() or ""
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT ad_account_id FROM user_ad_account_map WHERE ad_account_id IS NOT NULL")
                for (acc,) in cur.fetchall():
                    norm = normalize_ad_account_id(acc)
                    if norm and norm not in seen_accounts:
                        results.append({"token": fallback_tok, "ad_account_id": norm, "shop_key": ""})
                        seen_accounts.add(norm)
                        logger.info("Added user-mapped account act_%s (chưa shop)", norm)
    except Exception as exc:
        logger.warning("Could not load user_ad_account_map accounts: %s", exc)

    # [MỚI] Include MỌI TK mà token đọc được (cache token↔TK) — kể cả TK CHƯA gán
    # shop/NV → trang Tài khoản QC hiện chi phí cho cả TK chưa gán (biết tiêu bao nhiêu).
    try:
        for aid, tok in _build_acct_token_map().items():
            if aid and aid not in seen_accounts:
                results.append({"token": tok, "ad_account_id": aid, "shop_key": ""})
                seen_accounts.add(aid)
        logger.info("Tổng TK sẽ sync (gồm chưa gán): %d", len(seen_accounts))
    except Exception as exc:
        logger.warning("Could not load token-cache accounts: %s", exc)

    # TK bị loại tay (không phải công ty — vd token/BM chung nhưng TK là của
    # người khác, Long 29/08) — lọc RA CUỐI để chặn mọi nguồn thêm ở trên.
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT ad_account_id FROM fb_ad_account_exclude")
            excluded = {normalize_ad_account_id(r[0]) for r in cur.fetchall()}
        if excluded:
            before = len(results)
            results = [r for r in results if normalize_ad_account_id(r["ad_account_id"]) not in excluded]
            if before != len(results):
                logger.info("Loại %d TK theo fb_ad_account_exclude", before - len(results))
    except Exception as exc:
        logger.warning("Could not load fb_ad_account_exclude: %s", exc)

    return results


_LOCK_FILE = "/tmp/cpqc_sync_fb_ads_by_page.lock"  # [CPQC] lock RIÊNG, không dùng chung với tieuhiem


def _acquire_lock_or_exit() -> "object":
    """File-lock chống chạy chồng. Nếu instance khác đang chạy → exit code 0 (không phải lỗi).
    Lock tự release khi process kết thúc (kernel close fd)."""
    import fcntl
    fp = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("[lock] Một sync_fb_ads_by_page khác đang chạy — bỏ qua lần này.")
        sys.exit(0)
    fp.write(str(os.getpid()))
    fp.flush()
    return fp  # giữ reference để fd không bị close, tránh GC


def _shop_keys_for_account(ad_account_id: str) -> List[str]:
    """Tất cả shop_key (active) map vào 1 TK — 1 TK có thể gắn nhiều shop, mỗi shop 1 token."""
    aid = normalize_ad_account_id(ad_account_id)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT s.shop_key
                    FROM fb_ad_account_mappings m
                    JOIN shops s ON s.id = m.shop_id
                    WHERE m.fb_ad_account_id = %s AND m.status = 'active' AND s.shop_key IS NOT NULL
                """, (aid,))
                return [r[0] for r in cur.fetchall() if r[0]]
    except Exception as exc:
        logger.warning("  _shop_keys_for_account(%s) error: %s", aid, exc)
        return []


def _global_env_tokens() -> List[str]:
    """DANH SÁCH token global — mỗi token phủ 1 nhóm TK khác nhau, sync thử lần lượt.

    Nguồn (gộp, khử trùng giữ thứ tự):
      - env FACEBOOK_ACCESS_TOKENS  (nhiều token, ngăn cách dấu phẩy/xuống dòng)
      - env FACEBOOK_ACCESS_TOKEN   (1 token — tương thích cũ)
      - config.json facebook_access_token
    """
    raw = []
    multi = str(os.getenv("FACEBOOK_ACCESS_TOKENS", "") or "")
    for part in multi.replace("\n", ",").split(","):
        p = part.strip()
        if p:
            raw.append(p)
    one = str(os.getenv("FACEBOOK_ACCESS_TOKEN", "") or "").strip()
    if one:
        raw.append(one)
    cfg = BASE_DIR / "config.json"
    if cfg.exists():
        try:
            c = str((json.loads(cfg.read_text(encoding="utf-8")) or {}).get("facebook_access_token", "")).strip()
            if c:
                raw.append(c)
        except Exception:
            pass
    out = []
    for t in raw:
        if t not in out:
            out.append(t)
    return out


def _global_env_token() -> str:
    toks = _global_env_tokens()
    return toks[0] if toks else ""


# ── Cache token↔TK: mỗi token hỏi /me/adaccounts 1 lần → bản đồ {TK: token} ──
# → resolve dùng thẳng token đúng, KHÔNG thử lần lượt → nhanh + tránh rate-limit khi NHIỀU token.
_ACCT_TOKEN_MAP = None  # type: Optional[dict]


def _build_acct_token_map() -> dict:
    import requests as _rq
    m: dict = {}
    for t in _global_env_tokens():
        url = "https://graph.facebook.com/v20.0/me/adaccounts"
        params = {"fields": "account_id", "limit": 200, "access_token": t}
        pages = 0
        while url and pages < 20:
            try:
                r = _rq.get(url, params=params, timeout=25)
                js = r.json()
            except Exception:
                break
            for a in (js.get("data") or []):
                aid = normalize_ad_account_id(str(a.get("account_id") or ""))
                if aid and aid not in m:   # token đầu tiên thấy TK này → giữ
                    m[aid] = t
            url = (js.get("paging") or {}).get("next")
            params = None
            pages += 1
    logger.info("Cache token↔TK: %d tài khoản map được token", len(m))
    return m


def _token_for_account(aid: str) -> Optional[str]:
    global _ACCT_TOKEN_MAP
    if _ACCT_TOKEN_MAP is None:
        _ACCT_TOKEN_MAP = _build_acct_token_map()
    return _ACCT_TOKEN_MAP.get(normalize_ad_account_id(aid))


def resolve_working_token(ad_account_id: str, fallback_token: str = "") -> Optional[str]:
    """Trả token ĐANG GỌI ĐƯỢC cho TK này.

    Giống account-level: thử `try_store_access_token(shop_key, TK)` cho TỪNG shop
    map vào TK (vì 1 TK có thể có shop token-chết + shop token-sống), cộng token
    GLOBAL (env/config) và token fallback từ store. Test mỗi ứng viên bằng 1 call
    /ads nhẹ, lấy cái đầu tiên OK.
    """
    aid = normalize_ad_account_id(ad_account_id)
    candidates: List[str] = []
    # Token đã map sẵn cho TK này (cache /me/adaccounts) → thử ĐẦU TIÊN, gần như luôn trúng
    mapped = _token_for_account(aid)
    if mapped:
        candidates.append(mapped)
    for sk in _shop_keys_for_account(aid):
        try:
            tok = try_store_access_token(sk, aid)
        except Exception:
            tok = None
        if tok and tok not in candidates:
            candidates.append(tok)
    # NHIỀU token global — mỗi token phủ 1 nhóm TK; thử lần lượt tới khi đọc được
    for gtok in _global_env_tokens():
        if gtok and gtok not in candidates:
            candidates.append(gtok)
    if fallback_token and fallback_token not in candidates:
        candidates.append(fallback_token)

    for tok in candidates:
        try:
            FacebookAdsClient(tok).get_ads_with_creatives(aid, limit=1)
            return tok
        except Exception as exc:
            logger.info("  [%s] token ...%s không gọi được (%s) — thử token khác",
                        aid, tok[-6:], str(exc)[:60])
            continue
    return None


def main() -> None:
    _lock_fp = _acquire_lock_or_exit()  # noqa: F841 — giữ trong scope để lock còn hiệu lực

    parser = argparse.ArgumentParser(description="Sync FB Ads spend per page.")
    parser.add_argument("--date", default="")
    parser.add_argument("--date-from", dest="date_from", default="")
    parser.add_argument("--date-to",   dest="date_to",   default="")
    parser.add_argument("--ad-account-id", default="", help="Sync one account only")
    args = parser.parse_args()

    dates = resolve_dates(args)
    logger.info("=== sync_fb_ads_by_page: %d date(s) ===", len(dates))

    ensure_table()

    tokens = load_all_tokens()
    if not tokens:
        logger.error("No tokens found in store. Add tokens via Settings → Facebook Ads.")
        sys.exit(1)

    # Filter by account if specified
    if args.ad_account_id:
        filter_id = normalize_ad_account_id(args.ad_account_id)
        tokens = [t for t in tokens if normalize_ad_account_id(t["ad_account_id"]) == filter_id]
        if not tokens:
            logger.error("No token found for account %s", filter_id)
            sys.exit(1)

    # Deduplicate accounts (keep first token per account_id)
    seen: set = set()
    unique_tokens = []
    for t in tokens:
        aid = normalize_ad_account_id(t["ad_account_id"])
        if aid not in seen:
            seen.add(aid)
            unique_tokens.append(t)

    logger.info("Accounts to sync: %d", len(unique_tokens))
    total_ok, total_fail, total_notoken = 0, 0, 0

    for t in unique_tokens:
        aid = normalize_ad_account_id(t["ad_account_id"])
        logger.info("\n--- Account: act_%s (shop=%s) ---", aid, t.get("shop_key", ""))
        # Lấy token ĐANG SỐNG (thử mọi shop của TK), fallback token store như trước.
        token = resolve_working_token(aid, t.get("token", ""))
        if not token:
            logger.error("  KHÔNG có token gọi được cho act_%s — bỏ qua (cần kiểm tra token các shop của TK này)", aid)
            total_notoken += 1
            continue
        try:
            results = sync_account_for_dates(token, aid, dates)
            for d, cnt in results.items():
                logger.info("  %s → %d pages", d, cnt)
            total_ok += 1
        except Exception as exc:
            logger.error("  FAILED: %s", exc)
            total_fail += 1

    logger.info("\n=== DONE: %d OK, %d failed, %d không có token sống ===",
                total_ok, total_fail, total_notoken)


if __name__ == "__main__":
    main()
