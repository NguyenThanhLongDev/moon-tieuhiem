"""Module Page & Tài khoản — quản lý BM, Pages, Ad Accounts qua Facebook."""
from __future__ import annotations

import logging
import re
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent

import requests
from flask import (Blueprint, flash, redirect, render_template,
                   request, session, url_for, jsonify, make_response)

from .pa_db import (
    run_migrations, upsert_token, get_all_tokens, get_token, delete_token,
    upsert_bm, upsert_page, upsert_ad_account,
    get_all_bms, get_pages, get_ad_accounts,
)
from .pa_fb_api import (
    get_user_info, get_businesses, get_bm_pages, get_bm_ad_accounts,
    get_user_ad_accounts, get_user_pages, get_account_promote_pages, get_page_basic,
    ACCOUNT_STATUS_LABEL,
)

# Link sản phẩm LadiPage phổ biến nhất theo page — dùng làm "sản phẩm" cho page
# CHƯA bán trên POS (page lạ / page test), để 2 bảng không bên có bên không.
_LADI_SP_SQL = """(
    SELECT DISTINCT ON (t.pid) t.pid, t.lp
      FROM (SELECT ads_page_id AS pid,
                   rtrim(lower(replace(split_part(split_part(regexp_replace(
                     COALESCE(source_url,''),'^https?://',''),'?',1),'#',1),'www.','')),'/') AS lp,
                   COUNT(*) AS n
              FROM ladipage_inbound_orders
             WHERE COALESCE(ads_page_id,'') <> '' AND COALESCE(source_url,'') <> ''
             GROUP BY 1,2) t
     ORDER BY t.pid, t.n DESC
)"""

# ── Tên sản phẩm suy từ TÊN CHIẾN DỊCH ────────────────────────────────────────
# Page lạ / page test chưa bán trên POS nên không có mã SP. Nhưng marketer luôn
# đặt tên chiến dịch theo mẫu "<tên NV> - <sản phẩm>" (Duyên - ladi thổ cẩm,
# Phương 17/8 áo sao, Duck- áo kali) nên tách được tên hàng ra cho dễ đọc.
_RE_NGAY_CAMP = re.compile(r"\b\d{1,2}\s*[/\-.]\s*\d{1,2}(?:\s*[/\-.]\s*\d{2,4})?\b")
_BO_TU_CAMP = {"ladi", "landing", "test", "camp", "new", "moi", "mới", "copy", "cpy",
               "ads", "ad", "quảng", "cáo", "qc", "mess", "ib"}
_TEN_NV_CACHE: dict = {}


def _ten_nv_set(cur) -> set:
    """Tập tên nhân viên (để bỏ phần đứng đầu tên chiến dịch)."""
    if _TEN_NV_CACHE.get("v") is not None:
        return _TEN_NV_CACHE["v"]
    ten = set()
    try:
        cur.execute("SELECT username, full_name FROM users")
        for u, f in cur.fetchall():
            if u:
                ten.add(str(u).strip().lower())
            if f:
                w = [x for x in str(f).split() if x]
                if w:
                    ten.add(w[-1].lower())
                    ten.add(w[0].lower())
    except Exception:
        pass
    _TEN_NV_CACHE["v"] = ten
    return ten


def _lam_sach_ten_sp(ten: str, ten_nv: set | None = None) -> str:
    s = (ten or "").strip()
    if not s:
        return ""
    # "Phương 17/8 áo sao" → tên NV đứng trước ngày, bỏ luôn
    if re.match(r"^\s*\S+\s+\d{1,2}\s*[/\-.]\s*\d{1,2}", s):
        s = s.split(None, 1)[1] if " " in s.strip() else s
    s = _RE_NGAY_CAMP.sub(" ", s)
    m = re.match(r"^\s*[^\s\-–—]{2,15}\s*[-–—]\s*(.+)$", s)
    if m:
        s = m.group(1)
    toks = [t for t in s.split() if t]
    while toks:
        d = toks[0].lower().strip(".,-–—:")
        if d in _BO_TU_CAMP or (ten_nv and d in ten_nv and len(toks) > 1):
            toks.pop(0)
            continue
        break
    s = re.sub(r"\s{2,}", " ", " ".join(toks)).strip(" -–—.,:")
    return s[:40]


def _sp_tu_camp(cur, page_ids: list, d_from, d_to, top: int = 4) -> dict:
    """{page_id: [tên SP theo mức chi giảm dần]} suy từ tên chiến dịch FB."""
    ids = [str(p) for p in page_ids if p]
    if not ids:
        return {}
    cur.execute("""
        SELECT page_id, campaign_name, SUM(spend) AS sp
          FROM mb_fb_entity_daily
         WHERE page_id = ANY(%s)
           AND metric_date BETWEEN %s AND %s
           AND COALESCE(campaign_name,'') <> ''
         GROUP BY 1, 2
    """, (ids, d_from, d_to))
    rows = cur.fetchall()          # phải lấy hết TRƯỚC khi cursor chạy query khác
    nv = _ten_nv_set(cur)
    gom: dict = {}
    for pid, camp, sp in rows:
        ten = _lam_sach_ten_sp(camp, nv)
        if not ten:
            continue
        b = gom.setdefault(str(pid), {})
        k = ten.lower()
        b[k] = (b.get(k) or [ten, 0.0])
        b[k][1] += float(sp or 0)
    out = {}
    for pid, b in gom.items():
        xs = sorted(b.values(), key=lambda x: -x[1])[:top]
        out[pid] = [x[0] for x in xs]
    return out


def _don_ladi_theo_page(cur, d_from, d_to) -> dict:
    """{page_id: số đơn LadiPage khách đặt trong kỳ} — loại đơn Bỏ qua."""
    cur.execute("""
        SELECT ads_page_id, COUNT(*)
          FROM ladipage_inbound_orders
         WHERE COALESCE(ads_page_id,'') <> ''
           AND COALESCE(match_status,'') <> 'bo_qua'
           AND (received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date BETWEEN %s AND %s
         GROUP BY 1
    """, (d_from, d_to))
    return {str(r[0]): int(r[1]) for r in cur.fetchall()}


logger = logging.getLogger(__name__)

# ══ ĐỊNH NGHĨA PAGE CÔNG TY — MỘT CHỖ DUY NHẤT (Long 07/09 "xử lý triệt để") ═══
# Trước đây SQL này chép tay ở 2 nơi (Kiểm soát + Danh sách page lạ) → 28/08 sửa
# 1 nơi lỡ bỏ điều kiện POS, 79 page sếp đã add thành "lạ". Giờ cả 2 dùng chung.
# Sếp Phong chốt 04/08 (memory project_moon_company_page_definition): page là
# CÔNG TY nếu thuộc 1 trong 5 nguồn. Trả về NHÃN nguồn để hiện tooltip cho sếp.
# {p} = biểu thức page_id (vd s.page_id).
_PAGE_CTY_NGUON_SQL = """CASE
    WHEN EXISTS (SELECT 1 FROM company_page_whitelist w WHERE w.page_id = {p}) THEN 'Ghi nhận tay'
    WHEN EXISTS (SELECT 1 FROM fb_pages f WHERE f.page_id = {p}) THEN 'Token FB'
    WHEN EXISTS (SELECT 1 FROM pa_pages pp WHERE pp.page_id = {p}) THEN 'Trong BM'
    WHEN EXISTS (SELECT 1 FROM pos_page_daily_metrics pm WHERE pm.page_id = {p}) THEN 'Đã add vào POS'
    WHEN EXISTS (SELECT 1 FROM fb_page_shop_binding b WHERE b.page_id = {p}) THEN 'Gán shop'
    -- Sếp Phong 07/09: "page chạy trong TK QC của công ty là page của công ty, page
    -- có trong BM là được" → TỰ NHẬN mọi page đã chạy ads trên TK thuộc BM công ty
    -- (pa_ad_accounts) hoặc TK đã gán cho NV (user_ad_account_map). Không tính TK
    -- đã loại tay (fb_ad_account_exclude).
    WHEN EXISTS (SELECT 1 FROM fb_ads_page_daily_spend sp
                  JOIN pa_ad_accounts a ON REPLACE(a.account_id,'act_','') = REPLACE(sp.fb_ad_account_id,'act_','')
                 WHERE sp.page_id = {p}
                   AND NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e WHERE e.ad_account_id = REPLACE(sp.fb_ad_account_id,'act_','')))
         THEN 'Chạy trên TK công ty (BM)'
    WHEN EXISTS (SELECT 1 FROM fb_ads_page_daily_spend sp
                  JOIN user_ad_account_map m ON REPLACE(m.ad_account_id,'act_','') = REPLACE(sp.fb_ad_account_id,'act_','')
                 WHERE sp.page_id = {p}
                   AND NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e WHERE e.ad_account_id = REPLACE(sp.fb_ad_account_id,'act_','')))
         THEN 'Chạy trên TK đã gán NV'
    ELSE NULL END"""


def _page_cty_nguon(p: str) -> str:
    return _PAGE_CTY_NGUON_SQL.replace("{p}", p)


page_account_bp = Blueprint(
    "page_account", __name__,
    template_folder="templates",
    url_prefix="/page-account",
)

GRAPH = "https://graph.facebook.com/v20.0"
_SCOPES = "business_management,pages_show_list,pages_read_engagement,ads_read"


# ── Credentials ──────────────────────────────────────────────────────────────
def _fb_app_id() -> str:
    return str(os.getenv("FACEBOOK_APP_ID", "") or "").strip()


def _fb_app_secret() -> str:
    return str(os.getenv("FACEBOOK_APP_SECRET", "") or "").strip()


def _redirect_uri() -> str:
    base = str(os.getenv("APP_BASE_URL", "") or "").rstrip("/")
    if not base:
        dev_domain = str(os.getenv("REPLIT_DEV_DOMAIN", "") or "").strip()
        base = f"https://{dev_domain}:8000" if dev_domain else "https://tieuhiem.com"
    return f"{base}/page-account/auth/callback"


# ── Auth guards ──────────────────────────────────────────────────────────────
def _login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect(f"/login?next=/page-account/")
        return f(*args, **kwargs)
    return wrapped


def _it_required(f):
    """Chỉ admin/superadmin/IT được vào."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect(f"/login?next=/page-account/")
        role = session.get("role", "staff")
        if role not in ("admin", "superadmin", "it"):
            flash("Chức năng này dành cho bộ phận kỹ thuật / admin.", "danger")
            return redirect(url_for("page_account.index"))
        return f(*args, **kwargs)
    return wrapped


# ── Token exchange helpers ────────────────────────────────────────────────────
def _exchange_code(code: str) -> str:
    resp = requests.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": _fb_app_id(),
        "client_secret": _fb_app_secret(),
        "redirect_uri": _redirect_uri(),
        "code": code,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json().get("access_token", "")


def _long_lived_token(short: str) -> str:
    resp = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": _fb_app_id(),
        "client_secret": _fb_app_secret(),
        "fb_exchange_token": short,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json().get("access_token", short)


def _inspect_token(token: str) -> dict:
    """Gọi /debug_token để lấy info CHÍNH XÁC về token.
    Trả về dict có: expires_at (int, 0 = không hết hạn), type, scopes, app_id, is_valid.
    Đây là cách duy nhất phân biệt System User Token (expires_at=0) vs User Token (>0).
    """
    app_token = f"{_fb_app_id()}|{_fb_app_secret()}"
    try:
        resp = requests.get(f"{GRAPH}/debug_token", params={
            "input_token": token,
            "access_token": app_token,
        }, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", {}) or {}
    except Exception as exc:
        logger.warning("[debug_token] Không gọi được: %s", exc)
        return {}


# ── Sync logic ────────────────────────────────────────────────────────────────
def _excluded_ad_accounts(conn) -> set:
    """TK bị loại tay khỏi mọi lần sync (không phải công ty dù chung token/BM)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ad_account_id FROM fb_ad_account_exclude")
            return {str(r[0]).replace("act_", "") for r in cur.fetchall()}
    except Exception:
        return set()


def _sync_all(conn, fb_user_id: str, token: str) -> dict:
    """Đồng bộ toàn bộ BM → pages → ad accounts từ token. Trả về stats."""
    businesses = get_businesses(token)
    stats = {"bm": 0, "pages": 0, "ad_accounts": 0, "errors": []}
    _excl = _excluded_ad_accounts(conn)

    for bm in businesses:
        bm_id = str(bm.get("id", ""))
        bm_name = bm.get("name", "")
        roles = bm.get("permitted_roles") or []
        if not bm_id:
            continue
        try:
            upsert_bm(conn, bm_id, bm_name, roles, fb_user_id)
            stats["bm"] += 1
        except Exception as exc:
            stats["errors"].append(f"BM {bm_name}: {exc}")
            continue

        # Pages
        try:
            pages = get_bm_pages(bm_id, token)
            for p in pages:
                upsert_page(conn, p, bm_id)
            stats["pages"] += len(pages)
        except Exception as exc:
            stats["errors"].append(f"Pages của {bm_name}: {exc}")

        # Ad Accounts
        try:
            accounts = [a for a in get_bm_ad_accounts(bm_id, token)
                       if str(a.get("id","")).replace("act_","") not in _excl]
            for a in accounts:
                upsert_ad_account(conn, a, bm_id)
            stats["ad_accounts"] += len(accounts)
        except Exception as exc:
            stats["errors"].append(f"Ad accounts của {bm_name}: {exc}")

    # ── Sync TRỰC TIẾP (không qua BM) — cần cho System User Token vì /me/businesses
    #    thường rỗng. Pages: /me/accounts, Ad accounts: /me/adaccounts. bm_id = NULL.
    # LUÔN chạy phần Ad accounts /me/adaccounts (kể cả khi CÓ BM): TK cá nhân được
    # ADD làm client vào BM chỉ hiện ở /me/adaccounts, KHÔNG ở /{bm}/owned_ad_accounts
    # → trước đây bị sót (vd BM Leo Leo 22 TK chỉ kéo 1). Suy BM từ field business.
    seen_bm: set[str] = set()
    _bm_da_quet = {str(b.get("id", "")) for b in businesses if b.get("id")}
    try:
        for a in get_user_ad_accounts(token):
            if str(a.get("id","")).replace("act_","") in _excl:
                continue
            biz = a.get("business") or {}
            bm_id2 = str(biz.get("id") or "").strip() or None
            if bm_id2 and bm_id2 not in seen_bm:
                try:
                    upsert_bm(conn, bm_id2, biz.get("name", "") or bm_id2, [], fb_user_id)
                    seen_bm.add(bm_id2)
                except Exception:
                    pass
            # GIỮ bm_id đã có (BM quản lý gán từ vòng BM/tay) — chỉ điền owner cho TK MỚI.
            with conn.cursor() as _cur:
                _cur.execute("""
                    INSERT INTO pa_ad_accounts
                      (account_id, account_name, account_status, currency,
                       timezone_name, bm_id, bm_relation, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (account_id) DO UPDATE SET
                      account_name=EXCLUDED.account_name,
                      account_status=EXCLUDED.account_status,
                      currency=EXCLUDED.currency, timezone_name=EXCLUDED.timezone_name,
                      synced_at=NOW()
                """, (str(a["id"]), a.get("name", ""), int(a.get("account_status") or 1),
                      a.get("currency", ""), a.get("timezone_name", ""), bm_id2, "client"))
            conn.commit()
            stats["ad_accounts"] += 1
    except Exception as exc:
        stats["errors"].append(f"Ad accounts /me/adaccounts: {exc}")

    # ── Quét TK của các BM SUY RA từ /me/adaccounts ─────────────────────────
    # /me/businesses trả RỖNG với nhiều token (user chỉ là partner, không phải
    # thành viên BM) → vòng lặp BM ở trên bị bỏ qua sạch, chỉ còn 8 TK gán trực
    # tiếp. Trong khi BM đó có 19 TK (1 owned + 18 client) — vd BM "Leo Leo"
    # chứa act_787392001085698 đang tiêu 26tr mà phần mềm không hề thấy.
    # → Với mỗi BM suy ra được, quét tiếp owned + client_ad_accounts.
    for _bm_id in sorted(seen_bm - _bm_da_quet):
        try:
            for a in get_bm_ad_accounts(_bm_id, token):
                if str(a.get("id","")).replace("act_","") in _excl:
                    continue
                upsert_ad_account(conn, a, _bm_id)
                stats["ad_accounts"] += 1
            stats["bm"] += 1
        except Exception as exc:
            stats["errors"].append(f"BM suy ra {_bm_id}: {exc}")

    #    Pages trực tiếp: chỉ khi KHÔNG có BM (token user thật thì BM đã bao hết).
    if stats["bm"] == 0:
        # Pages trực tiếp (không gắn BM được qua /me/accounts → bm_id NULL)
        try:
            for p in get_user_pages(token):
                upsert_page(conn, p, None)
                stats["pages"] += 1
        except Exception as exc:
            stats["errors"].append(f"Pages trực tiếp: {exc}")

    # ── Backfill TÊN + ẢNH page qua promote_pages của từng TK QC ──
    #    /act_<id>/promote_pages trả page mà ad account QUẢNG CÁO ĐƯỢC — kể cả page
    #    NGOÀI BM + KHÔNG trên POS (page test). Chỉ cần quyền ads_read (khác /{page_id}
    #    fields=name vốn bị #10 nếu page chưa gán token). Đây là cách tieuhiem lấy được.
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT account_id FROM pa_ad_accounts WHERE account_id IS NOT NULL")
            acc_ids = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT fb_ad_account_id FROM fb_ad_account_mappings WHERE status='active' AND fb_ad_account_id IS NOT NULL")
            acc_ids |= {r[0] for r in cur.fetchall()}
        seen_pg: set[str] = set()
        for aid in acc_ids:
            try:
                for pg in get_account_promote_pages(aid, token):
                    pid = str(pg.get("id") or "")
                    if pid and pid not in seen_pg:
                        upsert_page(conn, pg, None)
                        seen_pg.add(pid)
                        stats["pages"] += 1
            except Exception:
                continue
    except Exception as exc:
        stats["errors"].append(f"Backfill promote_pages: {exc}")

    # Fallback cuối: page có chi phí mà promote_pages cũng không có → thử /{page_id} (page đã gán token)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT s.page_id FROM fb_ads_page_daily_spend s
                LEFT JOIN pa_pages p ON p.page_id = s.page_id
                WHERE p.page_id IS NULL AND s.page_id IS NOT NULL AND s.page_id <> ''
            """)
            still_missing = [r[0] for r in cur.fetchall()]
        for pid in still_missing:
            try:
                info = get_page_basic(pid, token)
                if info and info.get("id"):
                    upsert_page(conn, info, None)
                    stats["pages"] += 1
            except Exception:
                continue
    except Exception as exc:
        stats["errors"].append(f"Backfill page /{{id}}: {exc}")

    # Đồng bộ pa_pages → fb_pages (báo cáo chi_phi_qc đọc fb_pages để lấy TÊN + ẢNH page).
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_pages (page_id, page_name, page_category, picture_url, is_active, created_at, updated_at)
                SELECT page_id, page_name, category, picture_url, TRUE, NOW(), NOW() FROM pa_pages
                ON CONFLICT (page_id) DO UPDATE
                  SET page_name=EXCLUDED.page_name, page_category=EXCLUDED.page_category,
                      picture_url=EXCLUDED.picture_url, updated_at=NOW()
            """)
        conn.commit()
    except Exception as exc:
        stats["errors"].append(f"Đồng bộ fb_pages: {exc}")

    return stats


# ── JSON store bridge ─────────────────────────────────────────────────────────
def _json_store_path():
    from facebook_ads_tokens.resolve import fb_ads_tokens_store_path
    return fb_ads_tokens_store_path()


def _write_token_to_json_store(fb_user_id: str, token: str, mappings: list[dict]) -> int:
    """Ghi token vào JSON store cho tất cả mappings có (shop_key, ad_account_id)."""
    from facebook_ads_tokens.storage import load_store, save_store, upsert_record
    from facebook_ads_tokens.models import TokenRecord
    try:
        path = _json_store_path()
        records = load_store(path) if path.exists() else []
        count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for m in mappings:
            sk = str(m.get("shop_key") or "").strip()
            aid = str(m.get("fb_ad_account_id") or "").strip()
            if not sk or not aid:
                continue
            rec = TokenRecord(
                shop_key=sk,
                facebook_user_id=fb_user_id,
                ad_account_id=aid,
                access_token=token,
                issued_at=now_iso,
                refresh_status="ok",
                note="sync từ page_account",
            )
            records, _ = upsert_record(records, rec)
            count += 1
        if count:
            save_store(path, records)
        return count
    except Exception as exc:
        logger.warning("_write_token_to_json_store error: %s", exc)
        return 0


def _load_json_store_tokens() -> list[dict]:
    """Đọc tất cả entries từ JSON store để hiển thị."""
    from facebook_ads_tokens.storage import load_store
    try:
        path = _json_store_path()
        if not path.exists():
            return []
        records = load_store(path)
        seen: dict[str, dict] = {}
        for r in records:
            uid = r.facebook_user_id or ""
            if uid and uid not in seen:
                seen[uid] = {
                    "fb_user_id": uid,
                    "fb_user_name": "",
                    "source": "json_store",
                    "ad_account_count": 0,
                }
            if uid:
                seen[uid]["ad_account_count"] += 1
        return list(seen.values())
    except Exception as exc:
        logger.warning("_load_json_store_tokens error: %s", exc)
        return []


def _load_all_mappings() -> list[dict]:
    """Load fb_ad_account_mappings (shop → ad_account) từ DB."""
    try:
        from db import get_conn
        from repositories.admin_repo import list_fb_ad_account_mappings
        with get_conn() as conn:
            with conn.cursor() as cur:
                return list_fb_ad_account_mappings(cur)
    except Exception as exc:
        logger.warning("_load_all_mappings error: %s", exc)
        return []


def _load_shops() -> list[dict]:
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, shop_key, shop_name FROM shops WHERE status='active' ORDER BY shop_name")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def _load_spend_today(date_from=None, date_to=None) -> dict:
    """Chi phí QC theo (TK × shop) — source = page-level + 2-tier mapping fallback.

    Mapping shop ưu tiên:
      1. `fb_page_shop_binding` (page → shop, do admin/NV gán tay) — TRUTH.
      2. Fallback: nếu TK QC chỉ map vào ĐÚNG 1 shop trong `fb_ad_account_mappings` →
         auto-detect shop đó (TK đơn shop, không ambiguous).
      3. Nếu cả 2 đều không có → bucket "⚠ Chưa gán shop" để admin xử lý tay.

    Tổng các bucket = tổng spend TK = đúng FB API trả (no double-count, no thất thoát).
    NV column join theo time-window của `user_ad_account_assignments`.
    """
    import datetime as _dt
    today = _dt.date.today()
    if not date_from:
        date_from = today
    if not date_to:
        date_to = today
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH tk_single_shop AS (
                        -- TK QC chỉ map vào ĐÚNG 1 shop trong fb_ad_account_mappings → có thể auto-detect
                        SELECT fb_ad_account_id, MIN(shop_id) AS shop_id
                          FROM fb_ad_account_mappings
                         WHERE assigned_to IS NULL
                         GROUP BY fb_ad_account_id
                        HAVING COUNT(*) = 1
                    )
                    SELECT
                        s.fb_ad_account_id,
                        COALESCE(ai.account_name, s.fb_ad_account_id) AS account_name,
                        -- Tier 1: explicit page binding. Tier 2: TK đơn shop. Else NULL.
                        COALESCE(b.pos_shop_id, t.shop_id) AS pos_shop_id,
                        COALESCE(sh_b.shop_name, sh_t.shop_name) AS shop_name,
                        COALESCE(sh_b.shop_key,  sh_t.shop_key)  AS shop_key,
                        -- Source: 'manual' = page binding, 'auto' = TK đơn shop, NULL = chưa gán
                        CASE
                            WHEN b.pos_shop_id IS NOT NULL THEN 'manual'
                            WHEN t.shop_id     IS NOT NULL THEN 'auto'
                            ELSE NULL
                        END AS shop_source,
                        u.full_name     AS employee_name,
                        u.username      AS employee_username,
                        SUM(s.spend)    AS spend_today,
                        SUM(s.impressions) AS impressions,
                        SUM(s.clicks)      AS clicks,
                        COUNT(DISTINCT s.page_id) AS page_count
                    FROM fb_ads_page_daily_spend s
                    LEFT JOIN fb_page_shop_binding b
                           ON b.page_id = s.page_id
                          AND s.metric_date >= b.assigned_from
                          AND s.metric_date <= COALESCE(b.assigned_to, DATE '9999-12-31')
                    LEFT JOIN shops sh_b ON sh_b.id = b.pos_shop_id
                    LEFT JOIN tk_single_shop t ON t.fb_ad_account_id = s.fb_ad_account_id
                    LEFT JOIN shops sh_t ON sh_t.id = t.shop_id
                    LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
                    LEFT JOIN user_ad_account_assignments a
                           ON a.ad_account_id = s.fb_ad_account_id
                          AND s.metric_date >= a.assigned_from
                          AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                    LEFT JOIN users u ON u.id = a.user_id
                    WHERE s.metric_date BETWEEN %s AND %s
                    GROUP BY s.fb_ad_account_id, ai.account_name,
                             b.pos_shop_id, t.shop_id,
                             sh_b.shop_name, sh_b.shop_key,
                             sh_t.shop_name, sh_t.shop_key,
                             u.full_name, u.username
                    ORDER BY spend_today DESC NULLS LAST
                """, (date_from, date_to))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                for r in rows:
                    r["spend_today"] = float(r["spend_today"] or 0)
                    r["impressions"] = int(r["impressions"] or 0)
                    r["clicks"]      = int(r["clicks"] or 0)
                    r["is_unbound"] = (r["pos_shop_id"] is None)
                    r["is_auto"]    = (r.get("shop_source") == "auto")
                    if r["is_unbound"]:
                        r["shop_name"] = "⚠ Chưa gán shop"
                        r["shop_key"]  = "_unbound"
                total = sum(r["spend_today"] for r in rows)
                unbound_total = sum(r["spend_today"] for r in rows if r["is_unbound"])
                if date_from == date_to:
                    date_str = date_from.strftime("%d/%m/%Y")
                else:
                    date_str = f"{date_from.strftime('%d/%m/%Y')} → {date_to.strftime('%d/%m/%Y')}"
                return {
                    "accounts": rows,
                    "total": total,
                    "unbound_total": unbound_total,
                    "date": date_str,
                    "count": len(rows),
                }
    except Exception as exc:
        logger.error("_load_spend_today error: %s", exc)
        return {"accounts": [], "total": 0, "unbound_total": 0, "date": "", "count": 0}


def _load_pa_insights_today(date_from=None, date_to=None) -> dict:
    """
    Chi phí cho pa_ad_accounts (TK chưa gán shop) theo khoảng ngày.
    Mặc định: chỉ hôm nay.
    """
    import datetime as _dt
    today = _dt.date.today()
    if not date_from:
        date_from = today
    if not date_to:
        date_to = today
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        pai.account_id,
                        COALESCE(NULLIF(a.account_name,''), a.account_id) AS account_name,
                        b.bm_name,
                        SUM(pai.spend) AS spend
                    FROM pa_account_insights pai
                    JOIN pa_ad_accounts a ON a.account_id = 'act_' || pai.account_id
                    LEFT JOIN pa_business_managers b ON b.bm_id = a.bm_id
                    LEFT JOIN fb_ad_account_mappings m
                      ON m.fb_ad_account_id = pai.account_id AND m.status = 'active'
                    WHERE pai.metric_date BETWEEN %s AND %s
                      AND pai.spend > 0
                      AND m.id IS NULL
                    GROUP BY pai.account_id, a.account_name, a.account_id, b.bm_name
                    ORDER BY spend DESC
                """, (date_from, date_to))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                for r in rows:
                    r["spend"] = float(r["spend"] or 0)
                total = sum(r["spend"] for r in rows)
                if date_from == date_to:
                    date_str = date_from.strftime("%d/%m/%Y")
                else:
                    date_str = f"{date_from.strftime('%d/%m/%Y')} → {date_to.strftime('%d/%m/%Y')}"
                return {"accounts": rows, "total": total, "date": date_str, "count": len(rows)}
    except Exception as exc:
        logger.error("_load_pa_insights_today error: %s", exc)
        return {"accounts": [], "total": 0, "date": "", "count": 0}


def _load_active_users() -> list[dict]:
    """Load danh sách nhân viên active để hiển thị dropdown gán TK QC."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, full_name, role
                    FROM users WHERE status='active'
                    ORDER BY full_name
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def _load_all_ad_accounts_activity(days: int = 3, date_from=None, date_to=None) -> list[dict]:
    """
    Trả về 2 nhóm:
    - is_mapped=True : từ fb_ad_account_mappings + fb_ad_account_info + spend (29 TK đang sync)
    - is_mapped=False: từ pa_ad_accounts chưa có trong mappings (BM sync)
    2 nguồn dùng format ID khác nhau nên KHÔNG join chéo.

    Spend tính theo khoảng ngày [date_from, date_to] nếu được truyền (đồng nhất với Page→Shop);
    nếu không thì fallback cửa sổ N ngày gần nhất (days).
    """
    use_range = bool(date_from and date_to)
    # Điều kiện ngày + tham số tương ứng (SQL tĩnh, an toàn injection)
    if use_range:
        s_cond   = "s.metric_date BETWEEN %s AND %s"
        pai_cond = "pai.metric_date BETWEEN %s AND %s"
        date_args = [date_from, date_to]
    else:
        s_cond   = "s.metric_date >= CURRENT_DATE - %s"
        pai_cond = "pai.metric_date >= CURRENT_DATE - %s"
        date_args = [days]
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:

                # ── Nhóm 1: đã gán shop (fb_ad_account_mappings) ──────────────
                # M:N: 1 ad account có thể gán nhiều shop + nhiều NV.
                # GROUP BY fb_ad_account_id, dùng json_agg để gom shops + users thành list.
                cur.execute(f"""
                    SELECT
                        m.fb_ad_account_id  AS raw_id,
                        COALESCE(MAX(ai.account_name), MAX(m.account_name), m.fb_ad_account_id) AS display_name,
                        COALESCE((
                            SELECT SUM(s.spend) FROM fb_ads_page_daily_spend s
                            WHERE s.fb_ad_account_id = m.fb_ad_account_id
                              AND {s_cond}
                        ), 0) AS spend_nd,
                        (
                            SELECT MAX(s.metric_date) FROM fb_ads_page_daily_spend s
                            WHERE s.fb_ad_account_id = m.fb_ad_account_id
                              AND {s_cond}
                        ) AS last_seen,
                        json_agg(DISTINCT jsonb_build_object(
                            'mapping_id',      m.id,
                            'shop_id',         m.shop_id,
                            'shop_name',       sh.shop_name,
                            'shop_key',        sh.shop_key,
                            'pancake_shop_id', sh.pancake_shop_id
                        )) AS shops_json,
                        COALESCE((
                            SELECT json_agg(jsonb_build_object(
                                'map_id',    uam.id,
                                'user_id',   uam.user_id,
                                'full_name', u.full_name
                            ) ORDER BY u.full_name)
                            FROM user_ad_account_map uam
                            LEFT JOIN users u ON u.id = uam.user_id::bigint
                            WHERE uam.ad_account_id = m.fb_ad_account_id
                        ), '[]'::json) AS users_json
                    FROM fb_ad_account_mappings m
                    LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = m.fb_ad_account_id
                    LEFT JOIN shops sh ON sh.id = m.shop_id
                    WHERE m.assigned_to IS NULL   -- chỉ gán ĐANG hiệu lực (bỏ dòng đã đóng → tránh hiện trùng shop)
                    GROUP BY m.fb_ad_account_id
                    ORDER BY spend_nd DESC NULLS LAST, display_name
                """, (*date_args, *date_args))
                cols = [d[0] for d in cur.description]
                mapped = []
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    r["spend_nd"]   = float(r["spend_nd"] or 0)
                    r["is_active"]  = r["spend_nd"] > 0
                    r["is_mapped"]  = True
                    r["bm_name"]    = None
                    r["shops"]      = r.pop("shops_json") or []
                    r["users"]      = r.pop("users_json") or []
                    mapped.append(r)

                # ── Nhóm 2: chưa gán shop ──
                # Spend lấy từ fb_ads_page_daily_spend theo fb_ad_account_id (CÙNG nguồn với
                # nhóm đã gán + Page→Shop) thay vì pa_account_insights — vì nhiều TK có spend ở
                # bảng này nhưng không có ở pa_account_insights (vd MLINH 4.08M) → trước đây ra "—".
                mapped_raw_ids = [r["raw_id"] for r in mapped]
                cur.execute(f"""
                    SELECT
                        REPLACE(a.account_id, 'act_', '')                           AS raw_id,
                        COALESCE(NULLIF(a.account_name,''), a.account_id)            AS display_name,
                        b.bm_name,
                        COALESCE((
                            SELECT SUM(s.spend) FROM fb_ads_page_daily_spend s
                            WHERE s.fb_ad_account_id = REPLACE(a.account_id, 'act_', '')
                              AND {s_cond}
                        ), 0) AS spend_nd,
                        (
                            SELECT MAX(s.metric_date) FROM fb_ads_page_daily_spend s
                            WHERE s.fb_ad_account_id = REPLACE(a.account_id, 'act_', '')
                              AND {s_cond}
                        ) AS last_seen,
                        COALESCE((
                            SELECT json_agg(jsonb_build_object(
                                'map_id',    uam.id,
                                'user_id',   uam.user_id,
                                'full_name', u.full_name
                            ) ORDER BY u.full_name)
                            FROM user_ad_account_map uam
                            LEFT JOIN users u ON u.id = uam.user_id::bigint
                            WHERE uam.ad_account_id = REPLACE(a.account_id, 'act_', '')
                        ), '[]'::json) AS users_json
                    FROM pa_ad_accounts a
                    LEFT JOIN pa_business_managers b ON b.bm_id = a.bm_id
                    WHERE REPLACE(a.account_id, 'act_', '') != ALL(%s)
                    GROUP BY a.account_id, a.account_name, b.bm_name
                    ORDER BY spend_nd DESC NULLS LAST, a.account_name
                """, (*date_args, *date_args, mapped_raw_ids or ['__none__'],))
                cols2 = [d[0] for d in cur.description]
                unmapped = []
                for row in cur.fetchall():
                    r = dict(zip(cols2, row))
                    r["spend_nd"]  = float(r["spend_nd"] or 0)
                    r["is_active"] = r["spend_nd"] > 0
                    r["is_mapped"] = False
                    r["shops"]     = []
                    r["users"]     = r.pop("users_json") or []
                    unmapped.append(r)

                return mapped + unmapped
    except Exception as exc:
        logger.error("_load_all_ad_accounts_activity error: %s", exc)
        return []


def _load_ad_account_assignments() -> dict[str, dict]:
    """Load user_ad_account_map → dict[ad_account_id → {user_id, map_id, full_name}]."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.id, m.ad_account_id, m.user_id, u.full_name, u.username
                    FROM user_ad_account_map m
                    LEFT JOIN users u ON u.id = m.user_id
                    ORDER BY m.ad_account_id
                """)
                result = {}
                for row in cur.fetchall():
                    result[str(row[1])] = {
                        "map_id": row[0],
                        "user_id": row[2],
                        "full_name": row[3] or row[4] or str(row[2]),
                        "username": row[4],
                    }
                return result
    except Exception:
        return {}


# ── Routes ────────────────────────────────────────────────────────────────────
@page_account_bp.route("/")
@_login_required
def index():
    from db import get_conn
    with get_conn() as conn:
        bms = get_all_bms(conn)
        tokens = get_all_tokens(conn)
    mappings = _load_all_mappings()
    shops = _load_shops()
    return render_template("page_account/index.html",
                           bms=bms, tokens=tokens,
                           mappings=mappings, shops=shops)


@page_account_bp.route("/bm/<bm_id>/pages")
@_login_required
def bm_pages(bm_id: str):
    from db import get_conn
    with get_conn() as conn:
        pages = get_pages(conn, bm_id)
    return jsonify(pages)


@page_account_bp.route("/bm/<bm_id>/ad-accounts")
@_login_required
def bm_ad_accounts(bm_id: str):
    from db import get_conn
    with get_conn() as conn:
        accounts = get_ad_accounts(conn, bm_id)
    return jsonify(accounts)


@page_account_bp.route("/pages")
@_login_required
def all_pages():
    from db import get_conn
    with get_conn() as conn:
        pages = get_pages(conn)
        bms = get_all_bms(conn)
    mappings = _load_all_mappings()
    shops = _load_shops()
    return render_template("page_account/index.html",
                           bms=bms, pages=pages, tab="pages",
                           mappings=mappings, shops=shops)


@page_account_bp.route("/page-breakdown")
@_login_required
def page_breakdown():
    """Chiều NGƯỢC: từ PAGE → các TKQC + campaign đang chạy trên page đó.

    Nguồn: mb_fb_entity_daily (page_id ↔ account_id ↔ campaign ↔ spend), lọc theo
    khoảng ngày. Bao phủ MỌI page có quảng cáo (không giới hạn ở pa_pages).
    """
    from datetime import date, timedelta
    from tz_utils import today_hcm

    today = today_hcm()
    if not isinstance(today, date):
        try:
            today = date.fromisoformat(str(today))
        except Exception:
            today = date.today()
    default_to = today
    default_from = today - timedelta(days=7)

    def _parse(d, fallback):
        s = (d or "").strip()
        if not s:
            return fallback
        try:
            return date.fromisoformat(s)
        except Exception:
            return fallback

    date_from = _parse(request.args.get("date_from"), default_from)
    date_to = _parse(request.args.get("date_to"), default_to)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    q = (request.args.get("q") or "").strip()
    team_f = (request.args.get("team") or "").strip()
    nv_f = (request.args.get("nv") or "").strip()
    status_f = (request.args.get("status") or "all").strip()  # all|active|paused|deleted

    def _vn_status(s):
        s = (s or "").upper()
        if s == "ACTIVE":
            return ("Đang chạy", "active")
        if "PAUSED" in s:
            return ("Tạm dừng", "paused")
        if s in ("DELETED", "ARCHIVED"):
            return ("Đã xoá / lưu trữ", "deleted")
        if s in ("DISAPPROVED", "WITH_ISSUES", "PENDING_REVIEW"):
            return ("Có vấn đề", "paused")
        if not s:
            return ("Không còn trên FB (nghi xoá)", "deleted")
        return (s.title(), "other")

    pages: List[Dict[str, Any]] = []
    nv_options: List[Dict[str, Any]] = []
    team_options: List[str] = []
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT ON (page_id) page_id, page_name
                         FROM fb_ads_page_daily_spend
                        WHERE page_name <> ''
                        ORDER BY page_id, metric_date DESC"""
                )
                page_name_map = {str(r[0]): r[1] for r in cur.fetchall()}
                # Tên page THẬT (scrape m.facebook.com) — ưu tiên hơn tên campaign
                try:
                    cur.execute("SELECT page_id, name FROM fb_page_names")
                    for r in cur.fetchall():
                        page_name_map[str(r[0])] = r[1]
                except Exception:
                    pass
                cur.execute("SELECT REPLACE(account_id,'act_',''), account_name FROM pa_ad_accounts")
                acc_name_map = {str(r[0]): (r[1] or "") for r in cur.fetchall()}
                cur.execute(
                    """SELECT m.ad_account_id, u.id, u.username, COALESCE(t.team_name,'')
                         FROM user_ad_account_assignments m
                         JOIN users u ON u.id = m.user_id
                         LEFT JOIN teams t ON t.id = u.team_id
                        WHERE m.assigned_to IS NULL"""
                )
                acc_owner = {}
                for aid_, uid_, uname_, team_ in cur.fetchall():
                    acc_owner[str(aid_)] = {"nv_id": str(uid_), "nv": uname_, "team": team_ or ""}
                cur.execute(
                    """SELECT u.id, u.username, COALESCE(t.team_name,'')
                         FROM users u LEFT JOIN teams t ON t.id = u.team_id
                        WHERE u.status='active' ORDER BY u.username"""
                )
                _teams = set()
                for uid_, uname_, team_ in cur.fetchall():
                    nv_options.append({"id": str(uid_), "name": uname_, "team": team_ or ""})
                    if team_:
                        _teams.add(team_)
                team_options = sorted(_teams)
                # Chọn team → dropdown NV chỉ hiện NV thuộc team đó
                if team_f:
                    nv_options = [u for u in nv_options if u["team"] == team_f]
                cur.execute("SELECT campaign_id, account_id, effective_status FROM fb_campaign_status")
                camp_status = {}
                synced_accounts = set()
                for _cr in cur.fetchall():
                    camp_status[str(_cr[0])] = _cr[2]
                    synced_accounts.add(str(_cr[1]))
                # Kết quả (FB Result) cộng dồn theo khoảng ngày
                cur.execute(
                    """SELECT campaign_id, SUM(results), MAX(result_type)
                         FROM fb_campaign_results_daily
                        WHERE metric_date BETWEEN %s AND %s
                        GROUP BY campaign_id""",
                    (date_from.isoformat(), date_to.isoformat()),
                )
                camp_results = {str(r[0]): (int(r[1] or 0), r[2] or "") for r in cur.fetchall()}
                cur.execute(
                    """SELECT page_id, account_id, campaign_id, campaign_name,
                              SUM(spend), SUM(COALESCE(purchases,0))
                         FROM mb_fb_entity_daily
                        WHERE metric_date BETWEEN %s AND %s AND page_id <> ''
                        GROUP BY page_id, account_id, campaign_id, campaign_name""",
                    (date_from.isoformat(), date_to.isoformat()),
                )
                rows = cur.fetchall()
        pmap: Dict[str, Dict[str, Any]] = {}
        for pid, aid, cid, camp, spend, purch in rows:
            pid = str(pid); aid = str(aid); cid = str(cid or "")
            spend = float(spend or 0); purch = int(purch or 0)
            an = acc_name_map.get(aid) or aid
            owner = acc_owner.get(aid) or {"nv_id": "", "nv": "", "team": ""}
            if cid and cid in camp_status:
                slabel, sbucket = _vn_status(camp_status[cid])
            elif aid in synced_accounts:
                # TK đã đồng bộ mà campaign biến mất → bị xoá thật
                slabel, sbucket = ("Đã xoá (không còn trên FB)", "deleted")
            else:
                # TK chưa đồng bộ được (token không phủ) → không kết luận
                slabel, sbucket = ("Chưa rõ (TK chưa đồng bộ)", "unknown")
            if team_f and owner["team"] != team_f:
                continue
            if nv_f and owner["nv_id"] != nv_f:
                continue
            if status_f != "all" and sbucket != status_f:
                continue
            p = pmap.setdefault(pid, {
                "page_id": pid,
                "page_name": page_name_map.get(pid) or "(chưa rõ tên)",
                "total_spend": 0.0, "accounts": {}, "campaigns": [],
            })
            p["total_spend"] += spend
            a = p["accounts"].setdefault(aid, {"account_id": aid, "name": an, "spend": 0.0})
            a["spend"] += spend
            _res, _rtype = camp_results.get(cid, (0, ""))
            p["campaigns"].append({
                "account_id": aid, "account_name": an, "campaign": camp or "(không tên)",
                "spend": spend, "results": _res,
                "nv": owner["nv"], "team": owner["team"],
                "status_label": slabel, "status_bucket": sbucket,
                "cp_per_result": round(spend / _res, 0) if _res > 0 else 0,
            })
        pages = sorted(pmap.values(), key=lambda x: -x["total_spend"])
        # Tên page tự đặt (alias) — admin đặt cho dễ hiểu, ưu tiên hơn tên FB/campaign
        alias_map = {}
        try:
            from db import get_conn as _gc_a
            with _gc_a() as _ca:
                with _ca.cursor() as _cua:
                    _cua.execute("SELECT page_id, alias FROM pa_page_alias")
                    alias_map = {str(r[0]): r[1] for r in _cua.fetchall()}
        except Exception:
            alias_map = {}
        for p in pages:
            p["accounts"] = sorted(p["accounts"].values(), key=lambda x: -x["spend"])
            p["campaigns"] = sorted(p["campaigns"], key=lambda x: -x["spend"])
            p["has_deleted"] = any(c["status_bucket"] == "deleted" for c in p["campaigns"])
            p["name_raw"] = p["page_name"]
            if alias_map.get(p["page_id"]):
                p["page_name"] = alias_map[p["page_id"]]
                p["has_alias"] = True
        if q:
            ql = q.lower()
            pages = [p for p in pages
                     if ql in p["page_name"].lower() or ql in p["page_id"]
                     or any(ql in c["campaign"].lower() or ql in c["account_name"].lower()
                            for c in p["campaigns"])]
    except Exception as e:
        logger.warning("page_breakdown error: %s", e)

    bms = []
    mappings = _load_all_mappings()
    try:
        from db import get_conn as _gc
        with _gc() as _c:
            bms = get_all_bms(_c)
    except Exception:
        bms = []
    return render_template("page_account/page_breakdown.html",
                           pages=pages, date_from=date_from.isoformat(),
                           date_to=date_to.isoformat(), q=q,
                           team_f=team_f, nv_f=nv_f, status_f=status_f,
                           nv_options=nv_options, team_options=team_options,
                           bms=bms, mappings=mappings)


@page_account_bp.route("/page-alias", methods=["POST"])
@_login_required
def page_alias():
    """Admin tự đặt tên dễ hiểu cho 1 page (override tên FB/campaign khó hiểu)."""
    page_id = (request.form.get("page_id") or "").strip()
    alias = (request.form.get("alias") or "").strip()
    user = (session.get("username") or "")
    if page_id:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if alias:
                    cur.execute(
                        """INSERT INTO pa_page_alias (page_id, alias, updated_by, updated_at)
                           VALUES (%s,%s,%s, now())
                           ON CONFLICT (page_id) DO UPDATE SET
                             alias=EXCLUDED.alias, updated_by=EXCLUDED.updated_by, updated_at=now()""",
                        (page_id, alias, user),
                    )
                else:
                    cur.execute("DELETE FROM pa_page_alias WHERE page_id=%s", (page_id,))
            conn.commit()
    return redirect(request.referrer or url_for("page_account.page_breakdown"))


@page_account_bp.route("/ad-accounts")
@_login_required
def all_ad_accounts():
    from db import get_conn
    from datetime import date, timedelta
    from tz_utils import today_hcm

    # Lọc theo khoảng ngày — đồng nhất với Page→Shop (mặc định 8 ngày gần nhất)
    today = today_hcm()
    if not isinstance(today, date):
        try:
            today = date.fromisoformat(str(today))
        except Exception:
            today = date.today()
    default_to = today
    default_from = today - timedelta(days=7)

    def _parse(d, fallback):
        s = (d or "").strip()
        if not s:
            return fallback
        try:
            return date.fromisoformat(s)
        except Exception:
            return fallback

    date_from = _parse(request.args.get("date_from"), default_from)
    date_to   = _parse(request.args.get("date_to"),   default_to)
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    with get_conn() as conn:
        accounts = get_ad_accounts(conn)
        bms = get_all_bms(conn)
    mappings = _load_all_mappings()
    shops = _load_shops()
    active_users = _load_active_users()
    ad_account_assignments = _load_ad_account_assignments()
    all_activity = _load_all_ad_accounts_activity(date_from=date_from, date_to=date_to)
    return render_template("page_account/index.html",
                           bms=bms, ad_accounts=accounts, tab="ad_accounts",
                           account_status_label=ACCOUNT_STATUS_LABEL,
                           mappings=mappings, shops=shops,
                           active_users=active_users,
                           ad_account_assignments=ad_account_assignments,
                           mapped_activity=all_activity,
                           date_from=date_from.isoformat(),
                           date_to=date_to.isoformat(),
                           date_days=(date_to - date_from).days + 1,
                           # Nút nhanh 1n/3n/7n/14n — tính sẵn ngày để bấm phát ra ngay
                           quick_ranges=[{"n": n,
                                          "f": (date_to - timedelta(days=n - 1)).isoformat(),
                                          "t": date_to.isoformat(),
                                          "on": (date_to - date_from).days + 1 == n}
                                         for n in (1, 3, 7, 14)])


@page_account_bp.route("/page-cty/add", methods=["POST"])
@_it_required
def page_cty_add():
    """Sếp bấm '✓ Ghi nhận page công ty' trên bảng Kiểm soát / Danh sách page lạ
    (Long 07/09 "xử lý triệt để" — sếp tự sửa page còn sai, không phải nhờ dev).
    Ghi company_page_whitelist với note 'tay: <user> <ngày>' để phân biệt với auto cũ."""
    page_id = (request.form.get("page_id") or "").strip()
    ten = (request.form.get("page_name") or "").strip()[:80]
    if not page_id:
        flash("Thiếu page_id.", "warning")
        return redirect(request.referrer or url_for("page_account.kiem_soat"))
    from db import get_conn
    from datetime import date
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO company_page_whitelist (page_id, note)
            VALUES (%s, %s)
            ON CONFLICT (page_id) DO UPDATE SET note = EXCLUDED.note
        """, (page_id, f"tay: {session.get('username','?')} {date.today().isoformat()} — {ten}"))
        conn.commit()
    flash(f"Đã ghi nhận '{ten or page_id}' là page công ty.", "success")
    return redirect(request.referrer or url_for("page_account.kiem_soat"))


@page_account_bp.route("/page-cty/remove", methods=["POST"])
@_it_required
def page_cty_remove():
    """Bỏ ghi nhận tay (chỉ xoá dòng whitelist — không đụng POS/token/BM)."""
    page_id = (request.form.get("page_id") or "").strip()
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM company_page_whitelist WHERE page_id = %s", (page_id,))
        n = cur.rowcount
        conn.commit()
    flash("Đã bỏ ghi nhận tay." if n else "Page này không nằm trong danh sách ghi nhận tay.", "info")
    return redirect(request.referrer or url_for("page_account.kiem_soat"))


@page_account_bp.route("/ad-account/set-shops", methods=["POST"])
@_it_required
def set_shops():
    """Set danh sách shop cho 1 TK QC (override hoàn toàn).

    Form: ad_account_id, shop_ids[] (có thể rỗng).
    Close mọi mapping active hiện tại của TK + open mới cho `shop_ids`.
    `derived_from_user_id` = NV đang phụ trách TK (nếu có).
    """
    ad_account_id = request.form.get("ad_account_id", "").strip()
    account_name = request.form.get("account_name", "").strip() or ad_account_id
    if not ad_account_id:
        flash("Thiếu ad_account_id.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    raw_shop_ids = request.form.getlist("shop_ids")
    try:
        shop_ids = [int(x) for x in raw_shop_ids if x.strip()]
    except (TypeError, ValueError):
        shop_ids = []

    try:
        from db import get_conn
        from repositories.admin_repo import set_shop_mappings_for_account, get_active_assignment
        with get_conn() as conn:
            with conn.cursor() as cur:
                active = get_active_assignment(cur, ad_account_id)
                user_id = active["user_id"] if active else None
                # Validate: shop_ids phải nằm trong assigned_shops của NV active
                if user_id and shop_ids:
                    cur.execute(
                        "SELECT shop_id FROM user_shop_assignments WHERE user_id = %s AND assigned_to IS NULL",
                        (user_id,),
                    )
                    allowed = {int(r[0]) for r in cur.fetchall()}
                    invalid = [s for s in shop_ids if s not in allowed]
                    if invalid:
                        flash(
                            f"Shop {invalid} không nằm trong assigned_shops của NV phụ trách. Bị bỏ qua.",
                            "warning",
                        )
                        shop_ids = [s for s in shop_ids if s in allowed]
                res = set_shop_mappings_for_account(
                    cur, ad_account_id, account_name, user_id, shop_ids,
                )
            conn.commit()
        flash(f"Đã cập nhật shop của TK: close {res['closed']} mapping cũ, mở {res['opened']} mapping mới.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("page_account.all_ad_accounts"))


@page_account_bp.route("/api/user/<int:user_id>/shops")
@_login_required
def api_user_shops(user_id: int):
    """API JSON: danh sách shop của 1 NV (cho multi-select trong modal).

    Trả về [{"shop_id": int, "shop_name": str, "shop_key": str}, ...]
    """
    from flask import jsonify
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id, s.shop_name, s.shop_key
                      FROM user_shop_assignments usa
                      JOIN shops s ON s.id = usa.shop_id
                     WHERE usa.user_id = %s AND usa.assigned_to IS NULL AND s.status = 'active'
                     ORDER BY s.shop_name
                    """,
                    (user_id,),
                )
                rows = [
                    {"shop_id": int(r[0]), "shop_name": r[1] or "", "shop_key": r[2] or ""}
                    for r in cur.fetchall()
                ]
        return jsonify({"shops": rows})
    except Exception as exc:
        return jsonify({"error": str(exc), "shops": []}), 500


@page_account_bp.route("/api/ad-account/<ad_account_id>/page-spend")
@_login_required
def api_account_page_spend(ad_account_id: str):
    """API JSON: chi phí từng PAGE đã chạy dưới 1 TK QC trong khoảng ngày.

    Query: ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
    Trả về {ok, account_id, date_from, date_to, total, page_count, pages:[{page_id, page_name, spend, impressions, clicks}]}
    Nguồn: fb_ads_page_daily_spend (cùng nguồn với cột Chi phí + Page→Shop).
    """
    from flask import jsonify
    from datetime import date, timedelta

    def _parse(d, fb):
        s = (d or "").strip()
        try:
            return date.fromisoformat(s)
        except Exception:
            return fb

    today = date.today()
    date_from = _parse(request.args.get("date_from"), today - timedelta(days=7))
    date_to   = _parse(request.args.get("date_to"),   today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    -- Kèm SỐ ĐƠN: page đã lên POS lấy đơn chốt POS; page TEST
                    -- (chưa có doanh thu POS) lấy LƯỢT MUA/đăng ký bên Meta —
                    -- sếp Phong 20/08: "page test không có thì dựa vào lượt mua".
                    SELECT s.page_id,
                           COALESCE(MAX(NULLIF(fpn.name,'')), MAX(NULLIF(fp.page_name,'')),
                                    MAX(NULLIF(s.page_name,'')), '#' || s.page_id) AS page_name,
                           SUM(s.spend)        AS spend,
                           SUM(s.impressions)  AS impressions,
                           SUM(s.clicks)       AS clicks,
                           COALESCE(MAX(pos.chot), 0)  AS don_pos,
                           COALESCE(MAX(pos.rev), 0)   AS rev_pos,
                           COALESCE(MAX(mt.don), 0)    AS don_meta,
                           MAX(tp.product_name)        AS san_pham,
                           MAX(tp.product_image)       AS sp_anh,
                           MAX(lsp.lp)                 AS lp_ladi
                      FROM fb_ads_page_daily_spend s
                      LEFT JOIN fb_pages fp ON fp.page_id = s.page_id
                      LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                      LEFT JOIN (SELECT page_id, SUM(success_order_count) AS chot,
                                        SUM(revenue) AS rev
                                   FROM pos_page_daily_metrics
                                  WHERE metric_date BETWEEN %s AND %s
                                  GROUP BY page_id) pos ON pos.page_id = s.page_id
                      LEFT JOIN (SELECT page_id,
                                        GREATEST(COALESCE(SUM(purchases),0),
                                                 COALESCE(SUM(registrations),0)) AS don
                                   FROM mb_fb_entity_daily
                                  WHERE metric_date BETWEEN %s AND %s
                                    AND COALESCE(page_id,'') <> ''
                                  GROUP BY page_id) mt ON mt.page_id = s.page_id
                      LEFT JOIN pos_page_top_product tp ON tp.page_id = s.page_id
                      LEFT JOIN """ + _LADI_SP_SQL + """ lsp ON lsp.pid = s.page_id
                     WHERE s.fb_ad_account_id = %s
                       AND s.metric_date BETWEEN %s AND %s
                     GROUP BY s.page_id
                     HAVING SUM(s.spend) > 0
                     ORDER BY spend DESC
                    """,
                    (date_from, date_to, date_from, date_to,
                     ad_account_id, date_from, date_to),
                )
                pages = []
                for r in cur.fetchall():
                    sp = float(r[2] or 0)
                    don_pos = int(r[5] or 0)
                    rev_pos = float(r[6] or 0)
                    don_meta = int(r[7] or 0)
                    # Page đã bán trên POS → dùng đơn POS. Page TEST (chưa có
                    # doanh thu POS) → dùng lượt mua Meta, ghi rõ nguồn để khỏi
                    # nhầm 2 loại đơn với nhau.
                    if rev_pos > 0 or don_pos > 0:
                        don, nguon = don_pos, "POS"
                    else:
                        don, nguon = don_meta, "Meta"
                    pages.append({
                        "page_id":     r[0],
                        "page_name":   r[1],
                        "spend":       sp,
                        "impressions": int(r[3] or 0),
                        "clicks":      int(r[4] or 0),
                        "don":         don,
                        "don_pos":     don_pos,
                        "don_meta":    don_meta,
                        "don_nguon":   nguon,
                        "ads_don":     (sp / don) if don else None,
                        "loai":        "WIN" if rev_pos > 0 else "TEST",
                        "san_pham":    r[8] or "",
                        "sp_anh":      r[9] or "",
                        "sp_nguon":    "POS" if r[8] else "",
                        "_sp_pos":     r[8] or "",
                        "_lp_ladi":    r[10] or "",
                    })
                # đơn Ladi theo page + KẾT QUẢ = Meta + Ladi (sếp 28/08)
                _ladi = _don_ladi_theo_page(cur, date_from, date_to)
                for p in pages:
                    p["don_ladi"] = _ladi.get(str(p["page_id"]), 0)
                    _kq = int(p["don_meta"] or 0) + p["don_ladi"]
                    p["ket_qua"] = _kq
                    p["ads_don"] = (p["spend"] / _kq) if _kq else None
                # sản phẩm: chiến dịch (Ads, tiếng Việt) > LadiPage > POS (mã)
                _camp = _sp_tu_camp(cur, [p["page_id"] for p in pages], date_from, date_to)
                for p in pages:
                    _cs = _camp.get(str(p["page_id"])) or []
                    if _cs:
                        p["san_pham"], p["sp_nguon"] = ", ".join(_cs), "Ads"
                    elif p["_lp_ladi"]:
                        p["san_pham"], p["sp_nguon"] = p["_lp_ladi"], "Ladi"
                    elif p["_sp_pos"]:
                        p["san_pham"], p["sp_nguon"] = p["_sp_pos"], "POS"
                    p.pop("_sp_pos", None)
                    p.pop("_lp_ladi", None)
        total = sum(p["spend"] for p in pages)
        return jsonify({
            "ok": True,
            "account_id": ad_account_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "total": total,
            "page_count": len(pages),
            "pages": pages,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "pages": [], "total": 0}), 500


@page_account_bp.route("/api/page/<page_id>/account-spend")
@_login_required
def api_page_account_spend(page_id: str):
    """API JSON (ĐẢO NGƯỢC api_account_page_spend): 1 PAGE chạy trên những TK QC nào.

    Query: ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
    Trả về {ok, page_id, page_name, date_from, date_to, total, account_count,
            accounts:[{account_id, account_name, spend, impressions, clicks}]}
    Nguồn: fb_ads_page_daily_spend (cùng nguồn với chiều page-spend).
    """
    from flask import jsonify
    from datetime import date, timedelta

    def _parse(d, fb):
        s = (d or "").strip()
        try:
            return date.fromisoformat(s)
        except Exception:
            return fb

    today = date.today()
    date_from = _parse(request.args.get("date_from"), today - timedelta(days=7))
    date_to   = _parse(request.args.get("date_to"),   today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.fb_ad_account_id,
                           COALESCE(MAX(NULLIF(ai.account_name,'')), s.fb_ad_account_id) AS account_name,
                           SUM(s.spend)        AS spend,
                           SUM(s.impressions)  AS impressions,
                           SUM(s.clicks)       AS clicks,
                           MAX(NULLIF(s.page_name,'')) AS page_name
                      FROM fb_ads_page_daily_spend s
                      LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
                     WHERE s.page_id = %s
                       AND s.metric_date BETWEEN %s AND %s
                     GROUP BY s.fb_ad_account_id
                     ORDER BY spend DESC
                    """,
                    (page_id, date_from, date_to),
                )
                accounts = []
                page_name = None
                for r in cur.fetchall():
                    accounts.append({
                        "account_id":   r[0],
                        "account_name": r[1],
                        "spend":        float(r[2] or 0),
                        "impressions":  int(r[3] or 0),
                        "clicks":       int(r[4] or 0),
                    })
                    if not page_name and r[5]:
                        page_name = r[5]
        total = sum(a["spend"] for a in accounts)
        return jsonify({
            "ok": True,
            "page_id": page_id,
            "page_name": page_name or ("#" + str(page_id)),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "total": total,
            "account_count": len(accounts),
            "accounts": accounts,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "accounts": [], "total": 0}), 500


@page_account_bp.route("/api/ad-account/<ad_account_id>/shops")
@_login_required
def api_account_shops(ad_account_id: str):
    """API JSON: danh sách shop active của 1 TK QC (để pre-fill modal sửa shop).

    Trả về {"current_shop_ids": [int, ...], "user_id": int|null}.
    """
    from flask import jsonify
    try:
        from db import get_conn
        from repositories.admin_repo import get_active_assignment
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT shop_id FROM fb_ad_account_mappings WHERE fb_ad_account_id=%s AND assigned_to IS NULL",
                    (ad_account_id,),
                )
                current = [int(r[0]) for r in cur.fetchall()]
                active = get_active_assignment(cur, ad_account_id)
        return jsonify({
            "current_shop_ids": current,
            "user_id": active["user_id"] if active else None,
            "user_name": (active["full_name"] or active["username"]) if active else None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "current_shop_ids": [], "user_id": None}), 500


@page_account_bp.route("/huong-dan")
@_login_required
def huong_dan():
    """Trang hướng dẫn sử dụng — gán TK QC + Page → Shop."""
    return render_template("page_account/huong_dan.html")


@page_account_bp.route("/page-binding")
@_login_required
def page_binding():
    """Trang quản lý Page → POS shop binding.

    Liệt kê pages có spend trong range [date_from, date_to] + binding hiện tại.
    Pages được gom theo **TK QC chính** (account có spend cao nhất cho page đó trong range)
    → IT chọn shop ở header nhóm rồi tick các page cần bind → bấm "Gán cả nhóm".
    """
    from datetime import date, timedelta
    from tz_utils import today_hcm

    today = today_hcm()
    if not isinstance(today, date):
        try:
            today = date.fromisoformat(str(today))
        except Exception:
            today = date.today()
    default_to = today
    default_from = today - timedelta(days=7)

    def _parse(d, fallback):
        s = (d or "").strip()
        if not s:
            return fallback
        try:
            return date.fromisoformat(s)
        except Exception:
            return fallback

    date_from = _parse(request.args.get("date_from"), default_from)
    date_to   = _parse(request.args.get("date_to"),   default_to)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    filter_mode = request.args.get("filter", "all")  # all | unbound | zombie | test

    pages: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    total_spend = 0
    unbound_count = 0
    unbound_spend = 0
    zombie_count = 0
    test_count = 0
    bound_count = 0
    shops: List[Dict[str, Any]] = []
    try:
        from db import get_conn
        from repositories.admin_repo import list_pages_with_spend_range
        with get_conn() as conn:
            with conn.cursor() as cur:
                pages = list_pages_with_spend_range(
                    cur,
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                    only_unbound=False,
                )
                # Tên page THẬT (scrape) + tự đặt tay → ưu tiên hơn tên campaign
                cur.execute("SELECT page_id, name FROM fb_page_names")
                _pn = {str(r[0]): r[1] for r in cur.fetchall()}
                cur.execute("SELECT page_id, alias FROM pa_page_alias")
                _al = {str(r[0]): r[1] for r in cur.fetchall()}
                for _p in pages:
                    _pid = str(_p.get("page_id") or "")
                    if _al.get(_pid):
                        _p["page_name"] = _al[_pid]
                    elif _pn.get(_pid):
                        _p["page_name"] = _pn[_pid]
        if filter_mode == "unbound":
            pages = [p for p in pages if not p["has_binding_row"]]
        elif filter_mode == "zombie":
            pages = [p for p in pages if p["is_zombie"]]
        elif filter_mode == "test":
            pages = [p for p in pages if p["is_reviewed_test"]]
        total_spend     = sum(p["total_spend"] for p in pages)
        unbound_count   = sum(1 for p in pages if not p["has_binding_row"])
        unbound_spend   = sum(p["total_spend"] for p in pages if not p["has_binding_row"])
        zombie_count    = sum(1 for p in pages if p["is_zombie"])
        test_count      = sum(1 for p in pages if p["is_reviewed_test"])
        bound_count     = sum(1 for p in pages if p["binding_pos_shop_id"])

        # Gom theo TK QC chính (account đầu tiên trong list — đã sort theo spend DESC từ repo)
        groups_dict: Dict[str, Dict[str, Any]] = {}
        for p in pages:
            acc_list = p.get("accounts") or []
            if acc_list:
                primary = acc_list[0]
                key = primary.get("id") or "_unknown"
                label = primary.get("name") or key
            else:
                key = "_no_account"
                label = "(Không có TK QC)"
            g = groups_dict.get(key)
            if not g:
                g = {
                    "account_id": key,
                    "account_name": label,
                    "pages": [],
                    "total_spend": 0.0,
                    "bound_count": 0,
                    "unbound_count": 0,
                }
                groups_dict[key] = g
            g["pages"].append(p)
            g["total_spend"] += p["total_spend"]
            if p["binding_pos_shop_id"]:
                g["bound_count"] += 1
            elif not p["has_binding_row"]:
                g["unbound_count"] += 1
        # Sắp xếp groups theo total_spend DESC
        groups = sorted(groups_dict.values(), key=lambda x: x["total_spend"], reverse=True)
        shops = _load_shops()
    except Exception as exc:
        flash(f"Lỗi load page binding: {exc}", "danger")

    days_range = (date_to - date_from).days + 1
    # Số liệu cho badge thanh tab dùng chung (_pa_tabbar.html)
    try:
        from db import get_conn
        with get_conn() as _c:
            _bm_count = len(get_all_bms(_c))
        _mapping_count = len(_load_all_mappings())
    except Exception:
        _bm_count = 0
        _mapping_count = 0
    return render_template(
        "page_account/page_binding.html",
        pages=pages, groups=groups, shops=shops,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        days=days_range,
        filter_mode=filter_mode,
        total_spend=total_spend,
        unbound_count=unbound_count,
        unbound_spend=unbound_spend,
        zombie_count=zombie_count,
        test_count=test_count,
        bound_count=bound_count,
        bm_count=_bm_count,
        mapping_count=_mapping_count,
    )


@page_account_bp.route("/page-binding/bulk-set", methods=["POST"])
@_login_required
def page_binding_bulk_set():
    """Gán nhiều page về cùng 1 shop trong 1 lượt.

    Form fields:
      - pos_shop_id: int hoặc "" / "_test" (mark page test/không thuộc shop)
      - page_ids: list (multiple) — page_id cần bind
      - assigned_from: optional YYYY-MM-DD (mặc định hôm nay)
      - note, reason: optional
    """
    page_ids = [x.strip() for x in request.form.getlist("page_ids") if x.strip()]
    raw_shop = request.form.get("pos_shop_id", "").strip()
    reason = request.form.get("reason", "").strip() or None
    note = request.form.get("note", "").strip() or None
    assigned_from_raw = request.form.get("assigned_from", "").strip()
    effective_date = assigned_from_raw if assigned_from_raw else None

    date_from = request.form.get("date_from", "").strip()
    date_to   = request.form.get("date_to", "").strip()
    filter_mode = request.form.get("filter", "all")

    if not page_ids:
        flash("Chưa tick page nào để gán.", "warning")
        return redirect(url_for("page_account.page_binding",
                                date_from=date_from, date_to=date_to, filter=filter_mode))

    pos_shop_id: Optional[int]
    if raw_shop == "" or raw_shop == "_test":
        pos_shop_id = None
    else:
        try:
            pos_shop_id = int(raw_shop)
        except (TypeError, ValueError):
            pos_shop_id = None

    user_role = (session.get("role") or "").lower()
    user_id_sess = session.get("user_id")
    PRIV_ROLES = {"admin", "superadmin", "manager", "accountant", "ketoan", "it", "leader", "sale_leader"}
    is_priv = user_role in PRIV_ROLES
    if not is_priv:
        if pos_shop_id is None:
            flash("Chỉ admin/IT/leader được mark Page test.", "warning")
            return redirect(url_for("page_account.page_binding",
                                    date_from=date_from, date_to=date_to, filter=filter_mode))
        try:
            uid_int = int(user_id_sess) if user_id_sess is not None else 0
        except (TypeError, ValueError):
            uid_int = 0
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM user_shop_assignments WHERE user_id=%s AND shop_id=%s AND assigned_to IS NULL",
                        (uid_int, pos_shop_id),
                    )
                    if not cur.fetchone():
                        flash("Bạn không có quyền gán vào shop này.", "warning")
                        return redirect(url_for("page_account.page_binding",
                                                date_from=date_from, date_to=date_to, filter=filter_mode))
        except Exception as exc:
            flash(f"Lỗi kiểm tra quyền: {exc}", "danger")
            return redirect(url_for("page_account.page_binding",
                                    date_from=date_from, date_to=date_to, filter=filter_mode))

    ok = 0; fail = 0
    try:
        assigned_by = session.get("user_id")
        try:
            assigned_by = int(assigned_by) if assigned_by is not None else None
        except (TypeError, ValueError):
            assigned_by = None
        from db import get_conn
        from repositories.admin_repo import set_page_shop_binding
        with get_conn() as conn:
            with conn.cursor() as cur:
                for pid in page_ids:
                    try:
                        set_page_shop_binding(
                            cur,
                            page_id=pid,
                            pos_shop_id=pos_shop_id,
                            assigned_by=assigned_by,
                            reason=reason,
                            note=note,
                            effective_date=effective_date,
                        )
                        ok += 1
                    except Exception:
                        fail += 1
            conn.commit()
        label = "Page test" if pos_shop_id is None else "shop"
        msg = f"Đã gán {ok}/{len(page_ids)} page vào {label}."
        if fail:
            msg += f" Lỗi {fail} page."
        flash(msg, "success" if fail == 0 else "warning")
    except Exception as exc:
        flash(f"Lỗi bulk bind: {exc}", "danger")

    return redirect(url_for("page_account.page_binding",
                            date_from=date_from, date_to=date_to, filter=filter_mode))


@page_account_bp.route("/page-binding/set", methods=["POST"])
@_login_required
def page_binding_set():
    """Set page → POS shop binding. pos_shop_id rỗng = mark là test/zombie (NULL).

    Permission:
      - admin/superadmin/manager/accountant/ketoan/it/leader: gán tự do.
      - NV thường: chỉ được gán page vào shop nằm trong `assigned_shops` của họ
        (NV tự xử lý page họ nhận diện được).
      - Mark "Page test" (NULL shop): chỉ admin/IT/leader (không cho NV thường).
    """
    page_id = request.form.get("page_id", "").strip()
    raw_shop = request.form.get("pos_shop_id", "").strip()
    reason = request.form.get("reason", "").strip() or None
    note = request.form.get("note", "").strip() or None
    redirect_to = request.form.get("redirect_to", "").strip()
    # FIX 2026-05-14: cho phép admin chọn ngày áp dụng (assigned_from) — để binding
    # áp dụng cho data lịch sử (vd page chạy từ 1/5 mà admin gán ngày 14/5 → muốn
    # kế toán xem báo cáo 1/5 thấy binding này).
    assigned_from_raw = request.form.get("assigned_from", "").strip()
    effective_date = assigned_from_raw if assigned_from_raw else None
    if not page_id:
        flash("Thiếu page_id.", "danger")
        return redirect(redirect_to or url_for("page_account.page_binding"))

    pos_shop_id: Optional[int]
    if raw_shop == "" or raw_shop == "_test":
        pos_shop_id = None
    else:
        try:
            pos_shop_id = int(raw_shop)
        except (TypeError, ValueError):
            pos_shop_id = None

    # ─── Permission check ───
    user_role = (session.get("role") or "").lower()
    user_id_sess = session.get("user_id")
    PRIV_ROLES = {"admin", "superadmin", "manager", "accountant", "ketoan", "it", "leader", "sale_leader"}
    is_priv = user_role in PRIV_ROLES
    if not is_priv:
        # NV thường: chỉ gán cho shop của mình; cấm mark "Page test"
        if pos_shop_id is None:
            flash("Chỉ admin/IT/leader được mark Page test. Bạn chỉ có thể gán page vào shop của mình.", "warning")
            return redirect(redirect_to or url_for("page_account.page_binding"))
        try:
            uid_int = int(user_id_sess) if user_id_sess is not None else 0
        except (TypeError, ValueError):
            uid_int = 0
        if uid_int <= 0:
            flash("Phiên đăng nhập không hợp lệ.", "danger")
            return redirect(redirect_to or url_for("page_account.page_binding"))
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM user_shop_assignments WHERE user_id=%s AND shop_id=%s AND assigned_to IS NULL",
                        (uid_int, pos_shop_id),
                    )
                    if not cur.fetchone():
                        flash("Bạn không có quyền gán page vào shop này (shop không nằm trong assigned_shops của bạn).", "warning")
                        return redirect(redirect_to or url_for("page_account.page_binding"))
        except Exception as exc:
            flash(f"Lỗi kiểm tra quyền: {exc}", "danger")
            return redirect(redirect_to or url_for("page_account.page_binding"))

    try:
        assigned_by = session.get("user_id")
        try:
            assigned_by = int(assigned_by) if assigned_by is not None else None
        except (TypeError, ValueError):
            assigned_by = None
        from db import get_conn
        from repositories.admin_repo import set_page_shop_binding
        with get_conn() as conn:
            with conn.cursor() as cur:
                set_page_shop_binding(
                    cur,
                    page_id=page_id,
                    pos_shop_id=pos_shop_id,
                    assigned_by=assigned_by,
                    reason=reason,
                    note=note,
                    effective_date=effective_date,
                )
            conn.commit()
        if pos_shop_id is None:
            flash(f"Đã đánh dấu page {page_id} là page test/không thuộc shop.", "info")
        else:
            flash(f"Đã gán page {page_id} vào shop.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    # Redirect: nếu form có redirect_to (vd từ trang chi-phi-qc) → quay lại đó
    if redirect_to:
        return redirect(redirect_to)
    days = request.form.get("days", "7")
    filter_mode = request.form.get("filter", "all")
    return redirect(url_for("page_account.page_binding", days=days, filter=filter_mode))


@page_account_bp.route("/page-binding/<page_id>/history")
@_login_required
def page_binding_history(page_id: str):
    page_id = (page_id or "").strip()
    if not page_id:
        flash("Thiếu page_id.", "danger")
        return redirect(url_for("page_account.page_binding"))
    history = []
    page_name = page_id
    try:
        from db import get_conn
        from repositories.admin_repo import list_page_binding_history
        with get_conn() as conn:
            with conn.cursor() as cur:
                history = list_page_binding_history(cur, page_id)
                cur.execute(
                    "SELECT COALESCE(NULLIF(page_name,''), %s) FROM fb_pages WHERE page_id=%s "
                    "UNION ALL "
                    "SELECT COALESCE(NULLIF(page_name,''), %s) FROM pa_pages WHERE page_id=%s "
                    "LIMIT 1",
                    (page_id, page_id, page_id, page_id),
                )
                row = cur.fetchone()
                if row:
                    page_name = str(row[0])
    except Exception as exc:
        flash(f"Lỗi load history: {exc}", "danger")
    return render_template(
        "page_account/page_binding_history.html",
        page_id=page_id, page_name=page_name, history=history,
    )


@page_account_bp.route("/ad-account/<ad_account_id>/history")
@_login_required
def ad_account_history(ad_account_id: str):
    """Lịch sử phụ trách 1 TK QC (audit log)."""
    ad_account_id = (ad_account_id or "").strip()
    if not ad_account_id:
        flash("Thiếu ad_account_id.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))
    from db import get_conn
    from repositories.admin_repo import list_account_history, get_active_assignment
    history = []
    active = None
    account_name = ad_account_id
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                history = list_account_history(cur, ad_account_id)
                active = get_active_assignment(cur, ad_account_id)
                cur.execute(
                    "SELECT account_name FROM fb_ad_account_info WHERE ad_account_id=%s",
                    (ad_account_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    account_name = str(row[0])
                # Lấy shop mappings history
                cur.execute(
                    """
                    SELECT m.id, s.shop_name, s.shop_key, m.assigned_from, m.assigned_to,
                           u.username AS derived_from_username
                      FROM fb_ad_account_mappings m
                      JOIN shops s ON s.id = m.shop_id
                      LEFT JOIN users u ON u.id = m.derived_from_user_id
                     WHERE m.fb_ad_account_id = %s
                     ORDER BY m.assigned_from DESC, m.id DESC
                    """,
                    (ad_account_id,),
                )
                shop_history = [
                    {
                        "id": int(r[0]),
                        "shop_name": r[1],
                        "shop_key": r[2],
                        "assigned_from": r[3],
                        "assigned_to": r[4],
                        "derived_from_username": r[5] or "",
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        flash(f"Lỗi load lịch sử: {exc}", "danger")
        shop_history = []
    return render_template(
        "page_account/ad_account_history.html",
        ad_account_id=ad_account_id,
        account_name=account_name,
        history=history,
        shop_history=shop_history,
        active=active,
    )


@page_account_bp.route("/pages-cty")
@_login_required
def pages_cty():
    """Tab 'Pages công ty' — reuse toàn bộ logic từ fb_pages module."""
    from datetime import date
    from modules.fb_pages import (
        _all_pages, _all_users, _load_ad_accounts,
        _recent_declarations, _users_with_ad_accounts,
    )
    pages = _all_pages()
    users = _all_users()
    ad_accounts = _load_ad_accounts()
    declarations = _recent_declarations()
    users_with_ad_accounts = _users_with_ad_accounts()
    today = date.today().isoformat()
    current_user_id = session.get("user_id")
    current_role = session.get("role", "staff")
    # Badge cho thanh tab dùng chung (_pa_tabbar): số BM + số TK đã gán
    from db import get_conn
    try:
        with get_conn() as conn:
            bm_count = len(get_all_bms(conn))
        mapping_count = len(_load_all_mappings())
    except Exception:
        bm_count = mapping_count = 0
    return render_template("fb_pages/pages.html",
                           pages=pages, users=users,
                           ad_accounts=ad_accounts,
                           declarations=declarations,
                           users_with_ad_accounts=users_with_ad_accounts,
                           today=today,
                           current_user_id=current_user_id,
                           current_role=current_role,
                           bm_count=bm_count, mapping_count=mapping_count,
                           _embedded_in_pa=True)


@page_account_bp.route("/chi-phi-hom-nay")
@_login_required
def chi_phi_hom_nay():
    import datetime as _dt
    today = _dt.date.today()
    # Parse date_from / date_to từ query string
    def _parse_date(s, default):
        try:
            return _dt.date.fromisoformat(s)
        except Exception:
            return default
    date_from = _parse_date(request.args.get("date_from",""), today)
    date_to   = _parse_date(request.args.get("date_to",""),   today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    data    = _load_spend_today(date_from, date_to)
    pa_data = _load_pa_insights_today(date_from, date_to)
    return render_template("page_account/index.html",
                           tab="chi_phi", spend_today=data,
                           pa_insights_today=pa_data,
                           date_from=date_from.isoformat(),
                           date_to=date_to.isoformat(),
                           today=today.isoformat(),
                           bms=[], mappings=[], shops=[])


@page_account_bp.route("/kiem-soat")
@_login_required
def kiem_soat():
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if date_from and date_to:
        try:
            _df = date.fromisoformat(date_from)
            _dt = date.fromisoformat(date_to)
            days = max(1, (_dt - _df).days + 1)
        except ValueError:
            days = int(request.args.get("days", 7))
    else:
        days = int(request.args.get("days", 7))
    days = min(max(days, 1), 90)
    ks_view = (request.args.get("view") or "byacc").strip()  # byacc | pagela
    data = _load_kiem_soat_data(days)
    page_la = _load_page_la_list(days) if ks_view == "pagela" else []
    mappings = _load_all_mappings()
    shops = _load_shops()
    active_users = _load_active_users()
    return render_template("page_account/index.html",
                           tab="kiem_soat", kiem_soat=data,
                           kiem_soat_days=days, ks_view=ks_view, page_la=page_la,
                           date_from=date_from, date_to=date_to,
                           mappings=mappings, shops=shops,
                           active_users=active_users,
                           current_role=session.get("role", "staff"),
                           bms=[])


def _load_page_la_list(days: int = 7) -> list[dict]:
    """Danh sách TOÀN BỘ page lạ (không thuộc công ty) — gộp theo page, kèm các
    TK QC + NV đang chạy ads trên page đó, tổng chi phí, lần cuối."""
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT s.page_id,
                       COALESCE(NULLIF(fpn.name,''), NULLIF(s.page_name,''), s.page_id) AS page_name,
                       SUM(s.spend) AS total_spend,
                       MAX(s.metric_date) AS last_seen,
                       count(DISTINCT s.fb_ad_account_id) AS n_acc,
                       array_agg(DISTINCT COALESCE(ai.account_name, s.fb_ad_account_id)) AS accounts,
                       array_agg(DISTINCT u.username) FILTER (WHERE u.username IS NOT NULL) AS nvs
                  FROM fb_ads_page_daily_spend s
                  LEFT JOIN fb_pages fp ON fp.page_id = s.page_id
                  LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                  LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
                  LEFT JOIN user_ad_account_map uam ON uam.ad_account_id = s.fb_ad_account_id
                  LEFT JOIN users u ON u.id = uam.user_id::bigint
                 WHERE s.metric_date >= CURRENT_DATE - %s
                   AND COALESCE(s.page_id,'') <> ''
                   -- Page LẠ = KHÔNG thuộc công ty ở BẤT KỲ nguồn nào — dùng CHUNG
                   -- _PAGE_CTY_NGUON_SQL với tab Kiểm soát (1 định nghĩa, hết lệch nhau).
                   AND (""" + _page_cty_nguon("s.page_id") + """) IS NULL
                 GROUP BY s.page_id, fpn.name, s.page_name
                 ORDER BY SUM(s.spend) DESC
            """, (days,))
            out = []
            for r in cur.fetchall():
                out.append({
                    "page_id": r[0], "page_name": r[1],
                    "total_spend": float(r[2] or 0), "last_seen": r[3],
                    "n_acc": r[4], "accounts": [a for a in (r[5] or []) if a],
                    "nvs": [n for n in (r[6] or []) if n],
                })
            return out
    except Exception as exc:
        logger.error("_load_page_la_list error: %s", exc)
        return []


def _load_kiem_soat_data(days: int = 7) -> list[dict]:
    """
    Với mỗi ad account, liệt kê các pages nó đã chạy trong `days` ngày gần nhất.
    Cross-reference với fb_pages (company pages) để phát hiện page lạ.
    Cross-reference với user_ad_account_map để biết nhân viên phụ trách.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        s.fb_ad_account_id,
                        COALESCE(ai.account_name, s.fb_ad_account_id) AS account_name,
                        s.page_id,
                        COALESCE(
                            NULLIF(fpn.name, ''),
                            NULLIF(fp.page_name, ''),
                            NULLIF(pa_p.page_name, ''),
                            NULLIF(s.page_name, ''),
                            s.page_id
                        ) AS page_name,
                        COALESCE(fp.picture_url, pa_p.picture_url) AS picture_url,
                        -- Page CÔNG TY: dùng CHUNG _PAGE_CTY_NGUON_SQL (1 định nghĩa duy nhất,
                        -- xem đầu file) — trả nhãn nguồn để hiện tooltip cho sếp.
                        (""" + _page_cty_nguon("s.page_id") + """) IS NOT NULL AS is_company_page,
                        (""" + _page_cty_nguon("s.page_id") + """) AS cty_nguon,
                        MAX(s.metric_date) AS last_seen,
                        SUM(s.spend) AS total_spend,
                        -- Số đơn + sản phẩm để biết page lạ này thực sự bán gì
                        COALESCE(MAX(pos.chot), 0) AS don_pos,
                        COALESCE(MAX(pos.rev), 0)  AS rev_pos,
                        COALESCE(MAX(mt.don), 0)   AS don_meta,
                        MAX(tp.product_name)       AS san_pham,
                        MAX(lsp.lp)                AS lp_ladi,
                        u.full_name AS employee_name,
                        u.username  AS employee_username,
                        uam.id      AS map_id
                    FROM fb_ads_page_daily_spend s
                    LEFT JOIN (SELECT page_id, SUM(success_order_count) AS chot, SUM(revenue) AS rev
                                 FROM pos_page_daily_metrics
                                WHERE metric_date >= CURRENT_DATE - %s
                                GROUP BY page_id) pos ON pos.page_id = s.page_id
                    LEFT JOIN (SELECT page_id, GREATEST(COALESCE(SUM(purchases),0),
                                                        COALESCE(SUM(registrations),0)) AS don
                                 FROM mb_fb_entity_daily
                                WHERE metric_date >= CURRENT_DATE - %s AND COALESCE(page_id,'')<>''
                                GROUP BY page_id) mt ON mt.page_id = s.page_id
                    LEFT JOIN pos_page_top_product tp ON tp.page_id = s.page_id
                    LEFT JOIN """ + _LADI_SP_SQL + """ lsp ON lsp.pid = s.page_id
                    LEFT JOIN fb_pages fp ON fp.page_id = s.page_id
                    LEFT JOIN pa_pages pa_p ON pa_p.page_id = s.page_id
                    LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                    LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
                    LEFT JOIN user_ad_account_map uam ON uam.ad_account_id = s.fb_ad_account_id
                    LEFT JOIN users u ON u.id = uam.user_id::bigint
                    WHERE s.metric_date >= CURRENT_DATE - %s
                      AND NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e
                                       WHERE e.ad_account_id = REPLACE(s.fb_ad_account_id,'act_',''))
                    GROUP BY s.fb_ad_account_id, ai.account_name,
                             s.page_id, s.page_name, fpn.name, fp.page_name, fp.picture_url,
                             fp.page_id, pa_p.page_name, pa_p.picture_url,
                             u.full_name, u.username, uam.id
                    ORDER BY s.fb_ad_account_id, is_company_page ASC, total_spend DESC
                """, (days, days, days))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                # tên SP suy từ tên chiến dịch (phải lấy khi cursor còn mở)
                _camp = _sp_tu_camp(cur, [r["page_id"] for r in rows],
                                    date.today() - timedelta(days=days), date.today())
                _ladi = _don_ladi_theo_page(cur, date.today() - timedelta(days=days), date.today())

        # Group by ad_account_id
        grouped: dict[str, dict] = {}
        for r in rows:
            aid = r["fb_ad_account_id"]
            if aid not in grouped:
                grouped[aid] = {
                    "ad_account_id": aid,
                    "account_name": r["account_name"],
                    "employee_name": r["employee_name"],
                    "employee_username": r["employee_username"],
                    "map_id": r.get("map_id"),
                    "pages": [],
                    "unknown_count": 0,
                    "company_count": 0,
                    "total_spend": 0,
                }
            g = grouped[aid]
            is_company = bool(r["is_company_page"])
            g["pages"].append({
                "page_id": r["page_id"],
                "page_name": r["page_name"],
                "picture_url": r["picture_url"],
                "is_company_page": is_company,
                "cty_nguon": r.get("cty_nguon") or "",
                "last_seen": r["last_seen"],
                "total_spend": float(r["total_spend"] or 0),
                "don_pos": int(r["don_pos"] or 0),
                "don_meta": int(r["don_meta"] or 0),
                "don": (int(r["don_pos"] or 0) if (float(r["rev_pos"] or 0) > 0 or int(r["don_pos"] or 0) > 0)
                        else int(r["don_meta"] or 0)),
                "don_nguon": ("POS" if (float(r["rev_pos"] or 0) > 0 or int(r["don_pos"] or 0) > 0) else "Meta"),
                "san_pham": "",
                "sp_nguon": "",
                "_sp_pos": r["san_pham"] or "",
                "_lp_ladi": r.get("lp_ladi") or "",
            })
            _d = g["pages"][-1]
            _d["ads_don"] = (_d["total_spend"] / _d["don"]) if _d["don"] else None
            g["total_spend"] += float(r["total_spend"] or 0)
            if is_company:
                g["company_count"] += 1
            else:
                g["unknown_count"] += 1

        # Đơn LadiPage theo page + KẾT QUẢ = đơn Meta + đơn Ladi (sếp 28/08)
        for g in grouped.values():
            for p in g["pages"]:
                p["don_ladi"] = _ladi.get(str(p["page_id"]), 0)
                _kq = p["don_meta"] + p["don_ladi"]
                p["ket_qua"] = _kq
                p["ads_don"] = (p["total_spend"] / _kq) if _kq else None
        # sản phẩm: chiến dịch (Ads, tiếng Việt) > LadiPage > POS (mã)
        for g in grouped.values():
            for p in g["pages"]:
                _cs = _camp.get(str(p["page_id"])) or []
                if _cs:
                    p["san_pham"], p["sp_nguon"] = ", ".join(_cs), "Ads"
                elif p["_lp_ladi"]:
                    p["san_pham"], p["sp_nguon"] = p["_lp_ladi"], "Ladi"
                elif p["_sp_pos"]:
                    p["san_pham"], p["sp_nguon"] = p["_sp_pos"], "POS"
                p.pop("_sp_pos", None); p.pop("_lp_ladi", None)

        result = list(grouped.values())
        # Sort: accounts with unknown pages first
        result.sort(key=lambda x: (-x["unknown_count"], -x["total_spend"]))
        return result
    except Exception as exc:
        logger.error("_load_kiem_soat_data error: %s", exc)
        return []


# ── Background sync state ─────────────────────────────────────────────────────
_sync_status: dict = {}  # fb_user_id → {"status": "running"|"done"|"error", "msg": str}
_sync_lock = threading.Lock()


def _bg_sync(fb_user_id: str, fb_user_name: str, raw_token: str, added_by: str) -> None:
    with _sync_lock:
        _sync_status[fb_user_id] = {"status": "running", "msg": "Đang sync..."}
    try:
        from db import get_conn
        with get_conn() as conn:
            stats = _sync_all(conn, fb_user_id, raw_token)
        mappings = _load_all_mappings()
        store_count = _write_token_to_json_store(fb_user_id, raw_token, mappings)
        msg = (f"Hoàn thành: {stats['bm']} BM, {stats['pages']} pages, "
               f"{stats['ad_accounts']} TK QC.")
        if store_count:
            msg += f" {store_count} mapping đã cập nhật."
        if stats["errors"]:
            msg += f" ({len(stats['errors'])} lỗi nhỏ)"
        with _sync_lock:
            _sync_status[fb_user_id] = {"status": "done", "msg": msg}
        logger.info("bg_sync done for %s: %s", fb_user_name, msg)
    except Exception as exc:
        with _sync_lock:
            _sync_status[fb_user_id] = {"status": "error", "msg": str(exc)}
        logger.error("bg_sync error for %s: %s", fb_user_name, exc)


# ── Token thủ công (paste từ Graph API Explorer) ──────────────────────────────
@page_account_bp.route("/token/add", methods=["POST"])
@_it_required
def token_add():
    raw_token = request.form.get("access_token", "").strip()
    if not raw_token:
        flash("Vui lòng nhập Access Token.", "danger")
        return redirect(url_for("page_account.index"))

    # Validate token ngay (gọi /me — nhanh)
    try:
        user_info = get_user_info(raw_token)
        fb_user_id = str(user_info.get("id", ""))
        fb_user_name = user_info.get("name", "")
        if not fb_user_id:
            _err = user_info.get("error", {}) or {}
            # (#4) Application request limit reached / is_transient = FB chặn TẠM
            # do app gọi API quá nhiều (thường do job sync đang chạy) — KHÔNG phải
            # token sai. Báo đúng bản chất để khỏi tưởng token hỏng (sếp 12/08).
            if _err.get("code") == 4 or _err.get("is_transient"):
                flash("⏳ Facebook đang CHẶN TẠM do gọi API quá nhiều "
                      "(lỗi #4 — không phải token sai). Token của bạn vẫn dùng được: "
                      "đợi ~15-60 phút cho job đồng bộ chạy xong rồi bấm Thêm Token lại.",
                      "warning")
                return redirect(url_for("page_account.index"))
            raise ValueError(_err.get("message", "Token không hợp lệ"))
    except Exception as exc:
        _msg = str(exc)
        if "request limit" in _msg.lower() or "#4" in _msg:
            flash("⏳ Facebook đang CHẶN TẠM do gọi API quá nhiều (lỗi #4). "
                  "Token vẫn OK — đợi ~15-60 phút rồi thêm lại.", "warning")
        else:
            flash(f"Token không hợp lệ: {exc}", "danger")
        return redirect(url_for("page_account.index"))

    added_by = session.get("username") or str(session.get("user_id", ""))

    # Phân biệt CHÍNH XÁC bằng /debug_token (không dùng heuristic exchange nữa).
    # System User Token: data.expires_at == 0 (vĩnh viễn).
    # User Token thường: data.expires_at > 0 (timestamp Unix).
    info = _inspect_token(raw_token)
    expires_at = int(info.get("expires_at") or 0)
    data_access_expires_at = int(info.get("data_access_expires_at") or 0)
    token_to_save = raw_token

    if expires_at == 0:
        # Vĩnh viễn — đây là System User Token. KHÔNG exchange (sẽ làm hỏng).
        token_type = "system_user"
        logger.info("[token_add] %s là System User Token (vĩnh viễn)", fb_user_name)
    else:
        # User Token — thử exchange lên long-lived (60 ngày) nếu chưa
        token_type = "user"
        # Nếu hạn còn < 7 ngày → là short-lived (~1h), exchange lên long-lived
        from time import time as _now_ts
        if expires_at - _now_ts() < 7 * 86400:
            try:
                token_to_save = _long_lived_token(raw_token)
                # re-inspect để cập nhật expires_at thực tế
                info2 = _inspect_token(token_to_save)
                expires_at = int(info2.get("expires_at") or expires_at)
                data_access_expires_at = int(info2.get("data_access_expires_at") or data_access_expires_at)
                logger.info("[token_add] Đã exchange short-lived → long-lived cho %s", fb_user_name)
            except Exception as ex:
                logger.warning("[token_add] Không exchange được long-lived cho %s: %s", fb_user_name, ex)
        else:
            logger.info("[token_add] %s đã là long-lived User Token (~60 ngày)", fb_user_name)

    # Lưu token vào DB (kèm expires_at để hiển thị ngày hết hạn chính xác)
    try:
        from db import get_conn
        with get_conn() as conn:
            upsert_token(
                conn, fb_user_id, fb_user_name, token_to_save, added_by, token_type,
                expires_at=expires_at,
                data_access_expires_at=data_access_expires_at,
            )
    except Exception as exc:
        flash(f"Lỗi lưu token: {exc}", "danger")
        return redirect(url_for("page_account.index"))

    # Chạy sync trong background — tránh timeout Cloudflare
    t = threading.Thread(
        target=_bg_sync,
        args=(fb_user_id, fb_user_name, token_to_save, added_by),
        daemon=True,
    )
    t.start()

    if token_type == "system_user":
        type_label = "System User Token (vĩnh viễn — không hết hạn)"
    else:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            exp_vn = _dt.fromtimestamp(expires_at, _tz.utc) + _td(hours=7)
            days_left = max(0, (expires_at - int(__import__("time").time())) // 86400)
            type_label = f"User Token (hết hạn {exp_vn.strftime('%d/%m/%Y')} — còn {days_left} ngày)"
        except Exception:
            type_label = "User Token (~60 ngày)"
    flash(f"Đã lưu token {type_label} của {fb_user_name}. Đang đồng bộ BM/Pages/TK QC trong nền — "
          f"vui lòng refresh sau 1-2 phút.", "info")
    return redirect(url_for("page_account.index"))


@page_account_bp.route("/sync-status")
@_login_required
def sync_status():
    """Trả về JSON trạng thái sync hiện tại."""
    with _sync_lock:
        data = dict(_sync_status)
    return jsonify(data)


def _bg_sync_all_tokens() -> None:
    """Background: sync tất cả tokens + refresh JSON store."""
    try:
        from db import get_conn
        with get_conn() as conn:
            all_tokens = get_all_tokens(conn)
        mappings = _load_all_mappings()
        for t in all_tokens:
            with get_conn() as conn:
                tok = get_token(conn, t["fb_user_id"])
            if not tok:
                continue
            _bg_sync(t["fb_user_id"], t.get("fb_user_name", ""), tok, "")
            _write_token_to_json_store(t["fb_user_id"], tok, mappings)
        logger.info("bg_sync_all_tokens done")
    except Exception as exc:
        logger.error("bg_sync_all_tokens error: %s", exc)


@page_account_bp.route("/sync", methods=["POST"])
@_it_required
def sync():
    """Re-sync tất cả tokens trong nền."""
    threading.Thread(target=_bg_sync_all_tokens, daemon=True).start()
    flash("Đang sync lại tất cả BM/Pages/TK QC trong nền — refresh sau 1-2 phút.", "info")
    return redirect(url_for("page_account.index"))


@page_account_bp.route("/sync-spend", methods=["POST"])
@_login_required
def sync_spend():
    """Kích hoạt sync chi phí QC (fb_ads_page_daily_spend) trong nền."""
    import datetime as _dt
    import subprocess as _sp
    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)

    def _run_spend():
        try:
            python = _python_for_subjobs()
            env = os.environ.copy()
            cmd = [python, "scripts/sync_fb_ads_by_page.py",
                   "--date-from", yesterday.strftime("%Y-%m-%d"),
                   "--date-to",   today.strftime("%Y-%m-%d")]
            logger.info("[sync_spend] START %s → %s", yesterday, today)
            result = _sp.run(cmd, cwd=str(BASE_DIR), env=env,
                             capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                logger.info("[sync_spend] OK")
            else:
                logger.warning("[sync_spend] exit=%s\nSTDOUT: %s\nSTDERR: %s",
                               result.returncode, result.stdout[-1000:], result.stderr[-2000:])
        except Exception as exc:
            logger.error("[sync_spend] ERROR: %s", exc)

    threading.Thread(target=_run_spend, daemon=True).start()
    flash("Đang sync chi phí QC hôm nay + hôm qua trong nền — refresh sau 1-2 phút.", "info")
    redirect_to = request.referrer or url_for("page_account.kiem_soat")
    return redirect(redirect_to)


@page_account_bp.route("/sync-pa-insights", methods=["POST"])
@_login_required
def sync_pa_insights():
    """Sync spend từ FB API cho tất cả pa_ad_accounts (dùng token Trần Huy)."""
    days = int(request.form.get("days", 3))

    def _run():
        try:
            python = _python_for_subjobs()
            env = os.environ.copy()
            cmd = [python, "scripts/sync_pa_account_insights.py", "--days", str(days)]
            logger.info("[sync_pa_insights] START days=%s", days)
            import subprocess as _sp
            result = _sp.run(cmd, cwd=str(BASE_DIR), env=env,
                             capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("[sync_pa_insights] OK: %s", result.stdout[-500:])
            else:
                logger.warning("[sync_pa_insights] exit=%s\n%s", result.returncode, result.stderr[-1000:])
        except Exception as exc:
            logger.error("[sync_pa_insights] ERROR: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
    flash(f"Đang sync spend {days} ngày cho tất cả TK từ FB API — refresh sau 1-2 phút.", "info")
    return redirect(request.referrer or url_for("page_account.all_ad_accounts"))


def _python_for_subjobs() -> str:
    """Chọn Python interpreter cho subprocess."""
    env_py = os.environ.get("SCHEDULER_PYTHON", "").strip()
    if env_py and os.path.isfile(env_py):
        return env_py
    venv_py = BASE_DIR / ".venv" / "bin" / "python3"
    if venv_py.is_file():
        return str(venv_py)
    import sys
    return sys.executable


@page_account_bp.route("/ad-account/map-shop", methods=["POST"])
@_it_required
def map_shop():
    """Gán ad_account_id → shop_id, đồng thời ghi token vào JSON store."""
    ad_account_id = request.form.get("ad_account_id", "").strip()
    account_name = request.form.get("account_name", "").strip()
    shop_id = request.form.get("shop_id", "").strip()
    # Ngày hiệu lực gán shop (date picker). Rỗng → CURRENT_DATE.
    assigned_from = request.form.get("assigned_from", "").strip() or None

    if not ad_account_id or not shop_id:
        flash("Thiếu ad_account_id hoặc shop_id.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    try:
        from db import get_conn
        from repositories.admin_repo import upsert_fb_ad_account_mapping
        with get_conn() as conn:
            with conn.cursor() as cur:
                upsert_fb_ad_account_mapping(
                    cur, int(shop_id), ad_account_id, account_name or ad_account_id,
                    assigned_from=assigned_from,
                )
            conn.commit()
    except Exception as exc:
        flash(f"Lỗi gán shop: {exc}", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    # Cập nhật JSON store ngay
    mappings = _load_all_mappings()
    try:
        from db import get_conn
        with get_conn() as conn:
            all_tokens = get_all_tokens(conn)
        for t in all_tokens:
            from db import get_conn
            with get_conn() as conn:
                tok = get_token(conn, t["fb_user_id"])
            if tok:
                _write_token_to_json_store(t["fb_user_id"], tok, mappings)
    except Exception as exc:
        logger.warning("map_shop json store update error: %s", exc)

    flash(f"Đã gán tài khoản {account_name or ad_account_id} vào shop.", "success")
    return redirect(url_for("page_account.all_ad_accounts"))


@page_account_bp.route("/ad-account/unmap-shop", methods=["POST"])
@_it_required
def unmap_shop():
    """Hủy gán ad_account_id ↔ shop_id, đẩy về danh sách "Chưa gán shop"."""
    mapping_id = request.form.get("mapping_id", "").strip()
    if not mapping_id:
        flash("Thiếu mapping_id.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    try:
        from db import get_conn
        from repositories.admin_repo import delete_fb_ad_account_mapping
        with get_conn() as conn:
            with conn.cursor() as cur:
                delete_fb_ad_account_mapping(cur, int(mapping_id))
            conn.commit()
    except Exception as exc:
        flash(f"Lỗi hủy gán shop: {exc}", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    flash("Đã hủy gán shop. Tài khoản đã chuyển về 'Chưa gán shop'.", "success")
    return redirect(url_for("page_account.all_ad_accounts"))


@page_account_bp.route("/ad-account/assign-user", methods=["POST"])
@_it_required
def assign_user():
    """Gán NV phụ trách TK QC (versioned).

    - Ghi `user_ad_account_assignments` (lịch sử có audit). Nếu TK đã có NV
      đang phụ trách: tự động close khoảng cũ + mở khoảng mới từ hôm nay.
    - Cascade: close shop mappings active của TK + sinh shop mappings mới
      từ `user_shop_assignments` của NV mới.
    - Sync legacy `user_ad_account_map` (dual-write) để code đọc cũ vẫn chạy.
    """
    ad_account_id = request.form.get("ad_account_id", "").strip()
    account_name = request.form.get("account_name", "").strip()
    user_id_str = request.form.get("user_id", "").strip()
    reason = request.form.get("reason", "").strip() or None
    # Date picker (2026-05-15): IT có thể backdate khi gán → báo cáo cũ thấy NV.
    # Form gửi <input type="date"> dạng YYYY-MM-DD. Rỗng → mặc định CURRENT_DATE.
    assigned_from = request.form.get("assigned_from", "").strip() or None

    if not ad_account_id or not user_id_str:
        flash("Thiếu thông tin.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    try:
        user_id = int(user_id_str)
        assigned_by = session.get("user_id")
        try:
            assigned_by = int(assigned_by) if assigned_by is not None else None
        except (TypeError, ValueError):
            assigned_by = None

        # Parse shop_ids[] (multi-select). None = không đụng shop mappings;
        # [] = close hết (TK chưa biết shop); [..] = set tường minh.
        raw_shop_ids = request.form.getlist("shop_ids")
        shop_ids_param: Optional[List[int]]
        if raw_shop_ids is None or len(raw_shop_ids) == 0:
            shop_ids_param = []  # default: gán NV nhưng chưa pick shop
        else:
            try:
                shop_ids_param = [int(x) for x in raw_shop_ids if x.strip()]
            except (TypeError, ValueError):
                shop_ids_param = []

        from db import get_conn
        from repositories.admin_repo import assign_account_to_user
        with get_conn() as conn:
            with conn.cursor() as cur:
                assign_account_to_user(
                    cur,
                    ad_account_id=ad_account_id,
                    account_name=account_name or ad_account_id,
                    user_id=user_id,
                    assigned_by=assigned_by,
                    reason=reason,
                    effective_date=assigned_from,
                    shop_ids=shop_ids_param,
                )
                # Dual-write legacy uam: xoá row của NV cũ (nếu có) + upsert NV mới
                cur.execute(
                    "DELETE FROM user_ad_account_map WHERE ad_account_id=%s AND user_id <> %s",
                    (ad_account_id, str(user_id)),
                )
                cur.execute(
                    """
                    INSERT INTO user_ad_account_map
                        (user_id, ad_account_id, ad_account_name, assigned_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, ad_account_id) DO UPDATE SET
                        ad_account_name = EXCLUDED.ad_account_name,
                        assigned_by     = EXCLUDED.assigned_by
                    """,
                    (str(user_id), ad_account_id,
                     account_name or ad_account_id,
                     str(assigned_by) if assigned_by else None),
                )
            conn.commit()
        flash("Đã gán nhân viên phụ trách TK QC (đã cập nhật cả lịch sử).", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("page_account.all_ad_accounts"))


@page_account_bp.route("/ad-account/unassign-user", methods=["POST"])
@_it_required
def unassign_user():
    """Đóng phụ trách hiện tại của TK QC (versioned).

    - Set `assigned_to = hôm nay` cho uaa active.
    - Cascade: close shop mappings active (giữ history).
    - Xoá legacy `user_ad_account_map` row.
    """
    ad_account_id = request.form.get("ad_account_id", "").strip()
    reason = request.form.get("reason", "").strip() or None
    # Backward compat: form cũ có thể gửi `map_id` (legacy) — lookup ad_account_id từ đó
    if not ad_account_id:
        map_id = request.form.get("map_id", "").strip()
        if map_id:
            try:
                from db import get_conn
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT ad_account_id FROM user_ad_account_map WHERE id=%s", (int(map_id),))
                        row = cur.fetchone()
                        if row:
                            ad_account_id = str(row[0])
            except Exception:
                pass

    if not ad_account_id:
        flash("Thiếu ad_account_id.", "danger")
        return redirect(url_for("page_account.all_ad_accounts"))

    try:
        from db import get_conn
        from repositories.admin_repo import unassign_account
        with get_conn() as conn:
            with conn.cursor() as cur:
                unassign_account(cur, ad_account_id=ad_account_id, reason=reason)
                cur.execute("DELETE FROM user_ad_account_map WHERE ad_account_id=%s", (ad_account_id,))
            conn.commit()
        flash("Đã đóng phụ trách hiện tại. Lịch sử vẫn được lưu.", "info")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("page_account.all_ad_accounts"))


@page_account_bp.route("/token/delete", methods=["POST"])
@_it_required
def token_delete():
    fb_user_id = request.form.get("fb_user_id", "").strip()
    if not fb_user_id:
        flash("Thiếu fb_user_id.", "danger")
        return redirect(url_for("page_account.index"))
    try:
        from db import get_conn
        with get_conn() as conn:
            delete_token(conn, fb_user_id)
        flash("Đã xóa token.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("page_account.index"))


# ── Landing Pages: TK chạy chuyển đổi → link ladipage + số liệu per ad ───────
@page_account_bp.route("/landing-pages")
@_login_required
def landing_pages():
    """Trang riêng cho TK chạy LANDING PAGE (chuyển đổi/website).

    Nguồn link: fb_ad_landing_links (sync_fb_ad_landing_links.py — chỉ ad CÓ link).
    Số liệu: join mb_fb_entity_daily theo ad_id trong khoảng ngày.
    Gom: TK → link (1 ladipage nhiều ad → cộng dồn) → từng ad con.
    ?export=csv → tải danh sách phẳng (TK, NV, link, ad, chi tiêu, click, mua).
    """
    from datetime import date, timedelta
    from tz_utils import today_hcm

    today = today_hcm()
    if not isinstance(today, date):
        try:
            today = date.fromisoformat(str(today))
        except Exception:
            today = date.today()

    def _parse(d, fallback):
        s = (d or "").strip()
        if not s:
            return fallback
        try:
            return date.fromisoformat(s)
        except Exception:
            return fallback

    date_from = _parse(request.args.get("date_from"), today - timedelta(days=7))
    date_to = _parse(request.args.get("date_to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    q = (request.args.get("q") or "").strip().lower()
    acc_f = (request.args.get("acc") or "").strip()
    nv_f = (request.args.get("nv") or "").strip()
    alive_f = (request.args.get("alive") or "").strip()   # ''=tất cả | live | dead

    link_ads: list = []
    acc_name_map: dict = {}
    acc_owner: dict = {}
    nv_options: list = []
    stats: dict = {}
    page_name_map: dict = {}
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT ad_id, account_id, ad_name, status, campaign_name,
                                      link, domain, link_alive, link_status, link_checked_at,
                                      COALESCE(page_id,'')
                                 FROM fb_ad_landing_links ORDER BY account_id, link""")
                link_ads = [
                    {"ad_id": str(r[0]), "account_id": str(r[1]), "ad_name": r[2] or "",
                     "status": r[3] or "", "campaign_name": r[4] or "",
                     "link": r[5] or "", "domain": r[6] or "",
                     "alive": r[7], "http_status": r[8],
                     "checked_at": r[9], "page_id": str(r[10] or "")}
                    for r in cur.fetchall()
                ]
                # Tên page (fb_page_names ưu tiên, fallback tên trong bảng spend)
                _pg_ids = list({a["page_id"] for a in link_ads if a["page_id"]})
                if _pg_ids:
                    cur.execute("""SELECT page_id, MAX(COALESCE(NULLIF(page_name,''),''))
                                     FROM fb_ads_page_daily_spend
                                    WHERE page_id = ANY(%s) GROUP BY page_id""", (_pg_ids,))
                    for pid_, nm_ in cur.fetchall():
                        if nm_:
                            page_name_map[str(pid_)] = nm_
                    cur.execute("SELECT page_id, name FROM fb_page_names WHERE page_id = ANY(%s)", (_pg_ids,))
                    for pid_, nm_ in cur.fetchall():
                        if nm_:
                            page_name_map[str(pid_)] = nm_
                # số liệu per ad trong khoảng ngày
                ad_ids = [a["ad_id"] for a in link_ads]
                if ad_ids:
                    cur.execute("""
                        SELECT ad_id, SUM(spend), SUM(COALESCE(impressions,0)),
                               SUM(COALESCE(clicks,0)), SUM(COALESCE(purchases,0))
                          FROM mb_fb_entity_daily
                         WHERE ad_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                         GROUP BY ad_id
                    """, (ad_ids, date_from.isoformat(), date_to.isoformat()))
                    for aid_, sp, im, cl, pu in cur.fetchall():
                        stats[str(aid_)] = {"spend": float(sp or 0), "imp": int(im or 0),
                                            "clicks": int(cl or 0), "purchases": int(pu or 0)}
                    # tổng CẢ KỲ ĐÃ SYNC (không lọc ngày) — ad cũ ngoài khoảng chọn
                    # vẫn thấy đã tiêu bao nhiêu, khỏi tưởng 0đ
                    cur.execute("""
                        SELECT ad_id, SUM(spend) FROM mb_fb_entity_daily
                         WHERE ad_id = ANY(%s) GROUP BY ad_id
                    """, (ad_ids,))
                    for aid_, sp in cur.fetchall():
                        stats.setdefault(str(aid_), {"spend": 0.0, "imp": 0,
                                                     "clicks": 0, "purchases": 0})
                        stats[str(aid_)]["spend_all"] = float(sp or 0)
                cur.execute("SELECT REPLACE(account_id,'act_',''), account_name FROM pa_ad_accounts")
                acc_name_map = {str(r[0]): (r[1] or "").strip() for r in cur.fetchall()}
                cur.execute("""SELECT m.ad_account_id, u.id, u.username, COALESCE(t.team_name,'')
                                 FROM user_ad_account_assignments m
                                 JOIN users u ON u.id = m.user_id
                                 LEFT JOIN teams t ON t.id = u.team_id
                                WHERE m.assigned_to IS NULL""")
                for aid_, uid_, uname_, team_ in cur.fetchall():
                    acc_owner[str(aid_)] = {"nv_id": str(uid_), "nv": uname_, "team": team_ or ""}
                cur.execute("""SELECT u.id, u.username FROM users u
                                WHERE u.status='active' ORDER BY u.username""")
                nv_options = [{"id": str(r[0]), "name": r[1]} for r in cur.fetchall()]
    except Exception as exc:
        logger.error("landing_pages error: %s", exc)

    # gom TK → link → ads. Bỏ link trỏ về FB/Messenger — không phải landing page.
    _FB_DOMAINS = {"fb.com", "facebook.com", "m.me", "messenger.com", "fb.me", "instagram.com"}
    # Pass 1: áp mọi filter TRỪ trạng thái link → đếm số link sống/chết cho nút pill
    base_ads: list = []
    for a in link_ads:
        if a["domain"] in _FB_DOMAINS:
            continue
        aid = a["account_id"]
        owner = acc_owner.get(aid) or {"nv_id": "", "nv": "", "team": ""}
        if acc_f and aid != acc_f:
            continue
        if nv_f and owner["nv_id"] != nv_f:
            continue
        if q and q not in a["link"].lower() and q not in a["ad_name"].lower() \
                and q not in a["domain"].lower():
            continue
        a["_owner"] = owner
        base_ads.append(a)
    _links_alive: dict = {}
    for a in base_ads:
        _links_alive[a["link"]] = a["alive"]
    pill_counts = {
        "all": len(_links_alive),
        "live": sum(1 for v in _links_alive.values() if v is True),
        "dead": sum(1 for v in _links_alive.values() if v is False),
    }

    acc_map: dict = {}
    for a in base_ads:
        if alive_f == "live" and a["alive"] is not True:
            continue
        if alive_f == "dead" and a["alive"] is not False:
            continue
        aid = a["account_id"]
        owner = a["_owner"]
        st = stats.get(a["ad_id"], {"spend": 0.0, "imp": 0, "clicks": 0, "purchases": 0})
        st.setdefault("spend_all", st["spend"])
        acc = acc_map.setdefault(aid, {
            "account_id": aid,
            "account_name": acc_name_map.get(aid) or ("act_" + aid),
            "nv": owner["nv"], "team": owner["team"],
            "spend": 0.0, "purchases": 0, "links": {},
        })
        lk = acc["links"].setdefault(a["link"], {
            "link": a["link"], "domain": a["domain"],
            "alive": a["alive"], "http_status": a["http_status"],
            "checked_at": a["checked_at"],
            "spend": 0.0, "spend_all": 0.0, "imp": 0, "clicks": 0,
            "purchases": 0, "ads": [], "pages": {},
        })
        if a["page_id"]:
            lk["pages"][a["page_id"]] = page_name_map.get(a["page_id"]) or ("Page " + a["page_id"][-8:])
        a["page_name"] = page_name_map.get(a["page_id"]) or (("Page " + a["page_id"][-8:]) if a["page_id"] else "")
        lk["ads"].append({**a, **st})
        lk["spend"] += st["spend"]; lk["spend_all"] += st["spend_all"]
        lk["imp"] += st["imp"]
        lk["clicks"] += st["clicks"]; lk["purchases"] += st["purchases"]
        acc["spend"] += st["spend"]; acc["purchases"] += st["purchases"]

    accounts = []
    for acc in acc_map.values():
        links = sorted(acc["links"].values(), key=lambda x: -x["spend"])
        for lk in links:
            lk["ads"].sort(key=lambda x: -x["spend"])
            # Link CHẾT mà còn ad ACTIVE → đang đốt tiền vào trang chết, cảnh báo đỏ
            lk["active_dead"] = (lk["alive"] is False and
                                 any(ad["status"] == "ACTIVE" for ad in lk["ads"]))
            lk["pages"] = [{"id": k, "name": v} for k, v in lk["pages"].items()]
        acc["links"] = links
        accounts.append(acc)
    accounts.sort(key=lambda x: -x["spend"])

    _all_links = [lk for a in accounts for lk in a["links"]]

    # ── Chi tiêu LADIPAGE = MỌI campaign TRỪ tin nhắn thuần (MESSAGES) ──
    # Công ty chạy 100% ladipage: kể cả campaign ENGAGEMENT/video tương tác vẫn trỏ về
    # ladipage. Gồm CẢ ad video dark-post không dò được link (link nằm trong post, FB chặn
    # đọc). Chỉ loại objective 'MESSAGES' (TK chạy tin nhắn inbox — không có link đích).
    # → "Chi tiêu ladipage (kỳ)" khớp tổng CP QC.
    _NON_LADIPAGE_OBJ = ["MESSAGES"]
    spend_ladipage = 0.0
    try:
        from db import get_conn as _gc_lp
        with _gc_lp() as _c_lp, _c_lp.cursor() as _cur_lp:
            _cur_lp.execute("""
                SELECT m.account_id, COALESCE(SUM(m.spend), 0)
                  FROM mb_fb_entity_daily m
                  LEFT JOIN fb_campaign_objective o ON o.campaign_id = m.campaign_id
                 WHERE m.metric_date BETWEEN %s AND %s
                   AND NOT (COALESCE(o.objective, '') = ANY(%s))
                 GROUP BY m.account_id
            """, (date_from.isoformat(), date_to.isoformat(), _NON_LADIPAGE_OBJ))
            for _aid_lp, _sp_lp in _cur_lp.fetchall():
                _aid_lp = str(_aid_lp)
                if acc_f and _aid_lp != acc_f:
                    continue
                if nv_f and (acc_owner.get(_aid_lp) or {}).get("nv_id") != nv_f:
                    continue
                spend_ladipage += float(_sp_lp or 0)
    except Exception as _e_lp:
        logger.warning("spend_ladipage error: %s", _e_lp)

    totals = {
        "spend": sum(a["spend"] for a in accounts),
        "spend_ladipage": round(spend_ladipage),
        "links": len(_all_links),
        "links_live": sum(1 for lk in _all_links if lk["alive"] is True),
        "links_dead": sum(1 for lk in _all_links if lk["alive"] is False),
        "active_dead": sum(1 for lk in _all_links if lk["active_dead"]),
        "accounts": len(accounts),
        "purchases": sum(a["purchases"] for a in accounts),
    }

    # xuất CSV phẳng
    if (request.args.get("export") or "") == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["TK QC", "Tên TK", "NV", "Link landing", "Link sống?", "Page",
                    "Ad", "Trạng thái", "Chi tiêu", "Hiển thị", "Click", "Mua hàng"])
        for acc in accounts:
            for lk in acc["links"]:
                _lk_st = ("SỐNG" if lk["alive"] is True
                          else ("CHẾT" if lk["alive"] is False else "chưa check"))
                for ad in lk["ads"]:
                    w.writerow([acc["account_id"], acc["account_name"], acc["nv"],
                                lk["link"], _lk_st, ad.get("page_name", ""),
                                ad["ad_name"], ad["status"],
                                int(ad["spend"]), ad["imp"], ad["clicks"], ad["purchases"]])
        out = "﻿" + buf.getvalue()   # BOM để Excel đọc tiếng Việt
        resp = make_response(out)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=landing_pages_{date_from}_{date_to}.csv")
        return resp

    acc_options = sorted(
        ({"id": a["account_id"], "name": a["account_name"]} for a in acc_map.values()),
        key=lambda x: x["name"])
    return render_template("page_account/landing_pages.html",
                           accounts=accounts, totals=totals,
                           date_from=date_from.isoformat(), date_to=date_to.isoformat(),
                           q=request.args.get("q") or "", acc_f=acc_f, nv_f=nv_f,
                           alive_f=alive_f, pill_counts=pill_counts,
                           acc_options=acc_options, nv_options=nv_options)


# ── Trang GỘP "Page & Ads": 1 URL, 3 chế độ (pills chuyển view) ───────────────
@page_account_bp.route("/page-ads")
@_login_required
def page_ads():
    """Gộp 3 tab cũ thành 1 trang: ?view=pages|tkcamp|landing.

    Dispatch thẳng sang view function tương ứng (chúng tự đọc request.args
    nên bộ lọc ngày/TK/NV hoạt động y như trước). Form lọc trong template có
    hidden input `view` để giữ chế độ khi submit.
    """
    view = (request.args.get("view") or "pages").strip()
    if view == "tkcamp":
        return page_breakdown()
    if view == "landing":
        return landing_pages()
    if view == "pageshop":
        return page_binding()
    return all_pages()


# ── Jinja filter + context cho hiển thị token expiry ──────────────────────────
def _fb_ts_to_vn(ts) -> str:
    """Format Unix timestamp thành dd/mm/yyyy giờ VN (UTC+7)."""
    try:
        ts = int(ts or 0)
        if ts <= 0:
            return ""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        return (_dt.fromtimestamp(ts, _tz.utc) + _td(hours=7)).strftime("%d/%m/%Y")
    except Exception:
        return ""


@page_account_bp.context_processor
def _inject_token_helpers():
    import time as _t
    return {"now_ts": int(_t.time())}


# ── Register ──────────────────────────────────────────────────────────────────
def register_page_account_module(app):
    from db import get_conn
    with get_conn() as conn:
        run_migrations(conn)
    app.jinja_env.filters["fb_ts_to_vn"] = _fb_ts_to_vn
    app.register_blueprint(page_account_bp)
    logger.info("page_account module registered")


@page_account_bp.route("/api/ad-account/<ad_account_id>/campaigns")
@_login_required
def api_account_campaigns(ad_account_id: str):
    """Chi tiết CAMPAIGN của 1 TK QC trong khoảng ngày (kiểu tieuhiem.com).

    Mỗi campaign: page đang chạy · chi phí · số kết quả (lượt mua/đăng ký Meta)
    · chi phí mỗi kết quả. Nguồn: mb_fb_entity_daily.
    """
    from flask import jsonify
    from datetime import date, timedelta

    def _p(d, fb):
        try:
            return date.fromisoformat((d or "").strip())
        except Exception:
            return fb

    today = date.today()
    date_from = _p(request.args.get("date_from"), today - timedelta(days=7))
    date_to = _p(request.args.get("date_to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    acc = str(ad_account_id).replace("act_", "")

    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT e.campaign_id,
                       COALESCE(MAX(NULLIF(e.campaign_name,'')), '(không tên)') AS ten,
                       MAX(e.page_id) AS page_id,
                       COALESCE(MAX(NULLIF(n.name,'')), MAX(NULLIF(e.page_id,''))) AS page_ten,
                       SUM(e.spend) AS chi,
                       GREATEST(COALESCE(SUM(e.purchases),0),
                                COALESCE(SUM(e.registrations),0)) AS kq,
                       SUM(e.impressions) AS hien_thi,
                       SUM(e.clicks) AS click,
                       MAX(tp.product_name) AS san_pham,
                       MAX(lsp.lp) AS lp_ladi
                  FROM mb_fb_entity_daily e
                  LEFT JOIN fb_page_names n ON n.page_id = e.page_id
                  LEFT JOIN pos_page_top_product tp ON tp.page_id = e.page_id
                  LEFT JOIN """ + _LADI_SP_SQL + """ lsp ON lsp.pid = e.page_id
                 WHERE REPLACE(e.account_id,'act_','') = %s
                   AND e.metric_date BETWEEN %s AND %s
                   AND COALESCE(e.campaign_id,'') <> ''
                 GROUP BY e.campaign_id
                 HAVING SUM(e.spend) > 0
                 ORDER BY SUM(e.spend) DESC
            """, (acc, date_from, date_to))
            camps = []
            _crows = cur.fetchall()
            # Đơn POS theo PAGE trong kỳ (POS không biết campaign — hiện mức page
            # để sếp so "đơn POS hụt bao nhiêu so với Meta + Ladi", sếp 28/08)
            cur.execute("""
                SELECT page_id, SUM(success_order_count) FROM pos_page_daily_metrics
                 WHERE metric_date BETWEEN %s AND %s GROUP BY page_id
            """, (date_from, date_to))
            _pos_page = {str(r[0]): int(r[1] or 0) for r in cur.fetchall()}
            # Đơn LadiPage khách đặt THẬT về theo từng campaign (utm_campaign/utm_id
            # trong link landing) — sếp Phong 25/08: "thêm 1 cột đơn ladi nữa vào".
            cur.execute(r"""
                SELECT camp, COUNT(*) FROM (
                  SELECT COALESCE(substring(source_url from 'utm_campaign=(\d{6,})'),
                                  substring(source_url from 'utm_id=(\d{6,})')) AS camp
                    FROM ladipage_inbound_orders
                   WHERE (received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
                         BETWEEN %s AND %s
                     AND COALESCE(match_status,'') <> 'bo_qua'
                ) t WHERE camp IS NOT NULL GROUP BY 1
            """, (date_from, date_to))
            _ladi_map = {str(r[0]): int(r[1]) for r in cur.fetchall()}
            # sản phẩm page-level: top chiến dịch theo chi phí (khớp với Kiểm soát)
            _camp = _sp_tu_camp(cur, [r[2] for r in _crows if r[2]], date_from, date_to)
            for r in _crows:
                chi = float(r[4] or 0)
                kq = int(r[5] or 0)
                # dùng sản phẩm top campaign của page (không phải campaign row này)
                _page_sp = ", ".join(_camp.get(str(r[2])) or [])
                if _page_sp:
                    _sp, _ng = _page_sp, "Ads"
                elif r[9]:
                    _sp, _ng = r[9], "Ladi"
                elif r[8]:
                    _sp, _ng = r[8], "POS"
                else:
                    _sp, _ng = "", ""
                _dl = _ladi_map.get(str(r[0]), 0)
                _kq_tong = kq + _dl          # KẾT QUẢ = đơn Meta + đơn Ladi (sếp 28/08)
                camps.append({
                    "campaign_id": r[0], "ten": r[1], "page_id": r[2] or "",
                    "page_ten": r[3] or "", "chi": chi, "kq": kq,
                    "don_ladi": _dl,
                    "don_pos": _pos_page.get(str(r[2] or ""), 0),
                    "kq_tong": _kq_tong,
                    "cp_kq": (chi / _kq_tong) if _kq_tong else None,
                    "hien_thi": int(r[6] or 0), "clicks": int(r[7] or 0),
                    "san_pham": _sp,
                    "sp_nguon": _ng,
                })
        return jsonify({"ok": True, "account_id": acc,
                        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                        "total": sum(c["chi"] for c in camps),
                        "tong_kq": sum(c["kq"] for c in camps),
                        "tong_ladi": sum(c["don_ladi"] for c in camps),
                        "tong_kq_tong": sum(c["kq_tong"] for c in camps),
                        "count": len(camps), "camps": camps})
    except Exception as exc:
        logger.error("api_account_campaigns error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
