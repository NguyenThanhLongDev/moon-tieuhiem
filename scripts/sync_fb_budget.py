#!/usr/bin/env python3
"""Quét NGÂN SÁCH (spend_cap) + ĐÃ TIÊU (amount_spent) các TK QC đuôi "TH" → snapshot/ngày.

Mục đích: tối trước 19h30 IT/admin xem TK nào sắp cạn giới hạn chi tiêu để nạp/nâng limit.
Lấy qua Graph API `/me/adaccounts?fields=spend_cap,amount_spent,...` (token global đọc
được các TK trong BM Tiểu Hiềm). Map TK → NV → team. Lưu vào fb_ad_account_budget_snapshot
(UNIQUE snapshot_date, ad_account_id) — idempotent, chạy lại trong ngày sẽ ghi đè.

Usage:
  python scripts/sync_fb_budget.py            # snapshot hôm nay
  python scripts/sync_fb_budget.py --date 2026-05-27
"""
from __future__ import annotations
import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn
from scripts.sync_fb_ads_by_page import _global_env_token, resolve_working_token
from facebook_ads_tokens.client import normalize_ad_account_id as _norm

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_fb_budget")

GRAPH_API_VERSION = "v20.0"


def _is_th_account(name: str) -> bool:
    """TK đuôi TH chuẩn của Tiểu Hiềm: tên chứa TIEUHIEM / A+ 5. / dạng <tên>TH<số>."""
    n = name or ""
    if "TIEUHIEM" in n or "A+ 5." in n:
        return True
    return bool(re.search(r"[A-Za-zÀ-ỹ]+[Tt][Hh]\d", n))


def _short_name(name: str) -> str:
    return (name or "").split(" - A+")[0].split(" - TIEUHIEM")[0].strip()


def _fetch_th_accounts(token: str) -> List[dict]:
    """Lấy mọi TK token thấy qua /me/adaccounts, lọc TK đuôi TH."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/adaccounts"
    params = {
        "fields": "name,currency,spend_cap,amount_spent,account_status",
        "limit": 200,
        "access_token": token,
    }
    out: List[dict] = []
    after: Optional[str] = None
    for _ in range(30):
        p = dict(params)
        if after:
            p["after"] = after
        d = requests.get(url, params=p, timeout=40).json()
        if d.get("error"):
            logger.error("/me/adaccounts error: %s", d["error"].get("message"))
            break
        out += d.get("data", []) or []
        pg = d.get("paging", {})
        after = pg.get("cursors", {}).get("after")
        if not pg.get("next") or not after:
            break
    return [a for a in out if _is_th_account(a.get("name", ""))]


def _build_team_map(account_ids: List[str]) -> Dict[str, dict]:
    """{norm_account_id: {team_id, team_name, user_name}} qua uaa active, fallback mapping→shop→NV."""
    res: Dict[str, dict] = {}
    if not account_ids:
        return res
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.ad_account_id, u.team_id, t.team_name, u.full_name
              FROM user_ad_account_assignments a
              JOIN users u ON u.id = a.user_id
              LEFT JOIN teams t ON t.id = u.team_id
             WHERE a.assigned_to IS NULL AND a.ad_account_id = ANY(%s)
            """,
            (account_ids,),
        )
        for aid, tid, tname, nv in cur.fetchall():
            res[_norm(aid)] = {"team_id": tid, "team_name": tname or "(chưa rõ team)", "user_name": nv or ""}
        miss = [i for i in account_ids if i not in res]
        if miss:
            cur.execute(
                """
                SELECT m.fb_ad_account_id, MIN(u.team_id), MIN(t.team_name), MIN(u.full_name)
                  FROM fb_ad_account_mappings m
                  JOIN user_shop_assignments usa ON usa.shop_id = m.shop_id
                       AND usa.assigned_to IS NULL
                  JOIN users u ON u.id = usa.user_id
                  LEFT JOIN teams t ON t.id = u.team_id
                 WHERE m.status = 'active' AND m.fb_ad_account_id = ANY(%s)
                 GROUP BY m.fb_ad_account_id
                """,
                (miss,),
            )
            for aid, tid, tname, nv in cur.fetchall():
                res.setdefault(_norm(aid), {"team_id": tid, "team_name": tname or "(chưa rõ team)", "user_name": nv or ""})
    return res


def _upsert(snap_date: str, rows: List[tuple]) -> int:
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        # Xoá snapshot cũ của ngày này rồi ghi lại (tránh TK đã đổi tệp còn sót)
        cur.execute("DELETE FROM fb_ad_account_budget_snapshot WHERE snapshot_date = %s", (snap_date,))
        for r in rows:
            cur.execute(
                """
                INSERT INTO fb_ad_account_budget_snapshot
                    (snapshot_date, ad_account_id, account_name, short_name, team_id, team_name,
                     user_name, currency, spend_cap, amount_spent, remaining, account_status, synced_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """,
                r,
            )
        conn.commit()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="Ngày snapshot YYYY-MM-DD (mặc định hôm nay)")
    args = ap.parse_args()
    snap_date = args.date or date.today().isoformat()

    gtok = _global_env_token()
    if not gtok:
        logger.error("Không có token global.")
        sys.exit(1)

    accts = _fetch_th_accounts(gtok)
    logger.info("Tìm thấy %d TK đuôi TH", len(accts))
    ids = [_norm(a.get("account_id") or a.get("id", "")) for a in accts]
    team_map = _build_team_map(ids)

    rows: List[tuple] = []
    for a in accts:
        aid = _norm(a.get("account_id") or a.get("id", ""))
        cap = float(a.get("spend_cap") or 0)
        spent = float(a.get("amount_spent") or 0)
        remaining = (cap - spent) if cap > 0 else None
        tm = team_map.get(aid, {})
        rows.append((
            snap_date, aid, a.get("name", ""), _short_name(a.get("name", "")),
            tm.get("team_id"), tm.get("team_name"), tm.get("user_name"),
            a.get("currency", "VND"), cap, spent, remaining, a.get("account_status"),
        ))
    n = _upsert(snap_date, rows)
    logger.info("=== DONE: snapshot ngày %s — %d TK ===", snap_date, n)


if __name__ == "__main__":
    main()
