#!/usr/bin/env python3
"""Enrich fb_pages với TÊN + ẢNH avatar cho các page có spend nhưng chưa có thông tin.

- Tên: lấy qua Graph batch `{page_id}?fields=name` bằng token của TK QC sở hữu page
  (resolve_working_token theo từng ad account → token đọc được page đó).
- Ảnh: dùng URL avatar ỔN ĐỊNH của FB `https://graph.facebook.com/<ver>/<page_id>/picture`
  (redirect tới CDN, không cần token, không hết hạn) → hiển thị trực tiếp trong <img>.

Lưu vào fb_pages (ON CONFLICT page_id DO UPDATE). Idempotent. Chạy được CLI + scheduler.

Usage:
  python scripts/sync_fb_page_info.py [--since 2026-05-01] [--all]
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn
from facebook_ads_tokens.client import normalize_ad_account_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_fb_page_info")

GRAPH_API_VERSION = "v20.0"


def _picture_url(page_id: str) -> str:
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/picture?type=square&width=100&height=100"


def _pages_missing_info(since: str, fetch_all: bool):
    """Trả ({ad_account_id: set(page_id)}, {page_id: s_name}) cho page thiếu tên/ảnh.
    s_name = tên tốt nhất đang có trong fb_ads_page_daily_spend (fallback khi FB ko trả tên)."""
    cond = "" if fetch_all else "AND s.metric_date >= %s"
    params: list = [] if fetch_all else [since]
    by_acct: Dict[str, set] = defaultdict(set)
    s_names: Dict[str, str] = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.page_id, s.fb_ad_account_id,
                       MAX(NULLIF(s.page_name, '')) AS s_name
                  FROM fb_ads_page_daily_spend s
                  LEFT JOIN fb_pages p ON p.page_id = s.page_id
                 WHERE s.page_id IS NOT NULL AND s.page_id <> ''
                   {cond}
                   AND (p.page_id IS NULL OR p.page_name IS NULL OR p.page_name = ''
                        OR p.picture_url IS NULL OR p.picture_url = '')
                 GROUP BY s.page_id, s.fb_ad_account_id
                """,
                params,
            )
            for pid, aid, s_name in cur.fetchall():
                pid = str(pid)
                by_acct[normalize_ad_account_id(aid)].add(pid)
                if s_name and pid not in s_names:
                    s_names[pid] = s_name
    return by_acct, s_names


def _fetch_names(token: str, page_ids: List[str]) -> Dict[str, str]:
    """Graph batch lấy tên page (50/lần)."""
    names: Dict[str, str] = {}
    for i in range(0, len(page_ids), 50):
        chunk = page_ids[i:i + 50]
        batch = [{"method": "GET", "relative_url": f"{pid}?fields=name"} for pid in chunk]
        try:
            resp = requests.post(
                f"https://graph.facebook.com/{GRAPH_API_VERSION}/",
                data={"access_token": token, "batch": json.dumps(batch)},
                timeout=30,
            )
            resp.raise_for_status()
            for pid, item in zip(chunk, resp.json() or []):
                if item and item.get("code") == 200:
                    name = (json.loads(item.get("body", "{}")) or {}).get("name")
                    if name:
                        names[pid] = name
        except Exception as exc:
            logger.warning("  batch name error (acct chunk): %s", str(exc)[:100])
    return names


def _upsert_pages(rows: List[tuple]) -> int:
    """rows: [(page_id, page_name_or_None, picture_url), ...]"""
    if not rows:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = 0
            for pid, name, pic in rows:
                cur.execute(
                    """
                    INSERT INTO fb_pages (page_id, page_name, picture_url, is_active, created_at, updated_at)
                    VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                    ON CONFLICT (page_id) DO UPDATE SET
                        page_name   = COALESCE(NULLIF(EXCLUDED.page_name, ''), fb_pages.page_name),
                        picture_url = COALESCE(NULLIF(EXCLUDED.picture_url, ''), fb_pages.picture_url),
                        updated_at  = NOW()
                    """,
                    (pid, name, pic),
                )
                n += 1
        conn.commit()
    return n


def _accounts_with_page_spend(since: str, fetch_all: bool) -> List[str]:
    """Các ad account có page-level spend (cần lấy tên page)."""
    cond = "" if fetch_all else "WHERE metric_date >= %s"
    params: list = [] if fetch_all else [since]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT fb_ad_account_id FROM fb_ads_page_daily_spend {cond}",
                params,
            )
            return [normalize_ad_account_id(r[0]) for r in cur.fetchall() if r[0]]


def _promote_pages(token: str, aid: str) -> Dict[str, dict]:
    """Tên + ảnh page qua endpoint /act_<id>/promote_pages (dùng quyền TK QC,
    KHÔNG cần pages_read_engagement). Trả {page_id: {name, picture}}."""
    out: Dict[str, dict] = {}
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{normalize_ad_account_id(aid)}/promote_pages"
    params = {"fields": "id,name,picture{url}", "limit": 200, "access_token": token}
    after = None
    for _ in range(50):
        p = dict(params)
        if after:
            p["after"] = after
        try:
            d = requests.get(url, params=p, timeout=30).json()
        except Exception:
            break
        if not isinstance(d, dict) or d.get("error"):
            break
        for pg in d.get("data", []) or []:
            pid = str(pg.get("id") or "")
            if pid and pg.get("name"):
                pic = (((pg.get("picture") or {}).get("data") or {}).get("url"))
                out[pid] = {"name": pg["name"], "picture": pic}
        cur_after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
        if not (d.get("paging") or {}).get("next") or not cur_after:
            break
        after = cur_after
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-05-01", help="Chỉ page có spend từ ngày này")
    ap.add_argument("--all", action="store_true", help="Quét tất cả page có spend (bỏ filter ngày)")
    args = ap.parse_args()

    from scripts.sync_fb_ads_by_page import resolve_working_token

    accts = _accounts_with_page_spend(args.since, args.all)
    logger.info("Quét tên/ảnh page qua promote_pages — %d ad account", len(accts))

    all_rows: List[tuple] = []
    ok_acc = no_tok = 0
    seen_pid: set = set()
    for aid in accts:
        tok = resolve_working_token(aid, "")
        if not tok:
            no_tok += 1
            continue
        pages = _promote_pages(tok, aid)
        if pages:
            ok_acc += 1
        for pid, info in pages.items():
            if pid in seen_pid:
                continue
            seen_pid.add(pid)
            # Ảnh: ưu tiên URL ổn định (redirect, không hết hạn) thay vì CDN url
            all_rows.append((pid, info["name"], _picture_url(pid)))

    n = _upsert_pages(all_rows)
    logger.info("=== DONE: %d ad account OK (%d không token) | upsert %d page có tên thật ===",
                ok_acc, no_tok, n)


if __name__ == "__main__":
    main()
