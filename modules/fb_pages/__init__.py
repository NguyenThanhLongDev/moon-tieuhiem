from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from functools import wraps

import requests
from flask import (Blueprint, flash, redirect, render_template,
                   request, session, url_for)

logger = logging.getLogger(__name__)

fb_pages_bp = Blueprint(
    "fb_pages", __name__,
    template_folder="templates",
    url_prefix="/fb-pages",
)

# ── Facebook App credentials ─────────────────────────────────────────
def _cfg(key: str) -> str:
    """Đọc cấu hình từ DB app_config (ưu tiên) → fallback env. Cho phép NHẬP App ID/
    Secret/Base URL trên giao diện Cài đặt (mỗi khách app FB riêng, khỏi sửa env)."""
    try:
        from app_ctx import load_config
        v = str((load_config() or {}).get(key.lower(), "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return ""

def _fb_app_id() -> str:
    return _cfg("facebook_app_id") or str(os.getenv("FACEBOOK_APP_ID", "") or "").strip()

def _enc_key() -> bytes:
    """Khóa mã hóa per-instance (suy từ DATABASE_URL → mỗi bản moon khóa riêng)."""
    import hashlib, base64
    raw = (os.getenv("APP_ENC_KEY") or os.getenv("DATABASE_URL") or "cpqc-default").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())

def enc_secret(plain: str) -> str:
    """Mã hóa App Secret trước khi lưu DB (tránh lộ khi dump DB)."""
    if not plain:
        return ""
    try:
        from cryptography.fernet import Fernet
        return "enc:" + Fernet(_enc_key()).encrypt(plain.encode()).decode()
    except Exception:
        return plain  # cryptography lỗi → lưu thường (fallback)

def dec_secret(val: str) -> str:
    """Giải mã App Secret đọc từ DB. Giá trị cũ (chưa mã hóa) trả nguyên."""
    val = str(val or "")
    if not val.startswith("enc:"):
        return val
    try:
        from cryptography.fernet import Fernet
        return Fernet(_enc_key()).decrypt(val[4:].encode()).decode()
    except Exception:
        return ""

def _fb_app_secret() -> str:
    return dec_secret(_cfg("facebook_app_secret")) or str(os.getenv("FACEBOOK_APP_SECRET", "") or "").strip()

def _redirect_uri() -> str:
    base = (_cfg("app_base_url") or str(os.getenv("APP_BASE_URL", "") or "")).rstrip("/")
    if not base:
        dev_domain = str(os.getenv("REPLIT_DEV_DOMAIN", "") or "").strip()
        if dev_domain:
            base = f"https://{dev_domain}:8000"
        else:
            base = "https://tieuhiem.com"
    return f"{base}/fb-pages/auth/callback"

GRAPH = "https://graph.facebook.com/v20.0"

# ── Auth guard ───────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect(f"/login?next=/fb-pages/")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect(f"/login?next=/fb-pages/")
        role = session.get("role", "staff")
        if role not in ("admin", "superadmin", "manager"):
            flash("Bạn không có quyền truy cập chức năng này.", "danger")
            return redirect(url_for("fb_pages.pages_list"))
        return f(*args, **kwargs)
    return decorated

# ── Migrations ──────────────────────────────────────────────────────
_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS fb_pages (
    id          BIGSERIAL PRIMARY KEY,
    page_id     VARCHAR(100) UNIQUE NOT NULL,
    page_name   VARCHAR(255) NOT NULL,
    page_category VARCHAR(255),
    picture_url TEXT,
    access_token TEXT,
    fb_user_id  VARCHAR(100),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fb_page_assignments (
    id          BIGSERIAL PRIMARY KEY,
    page_id     VARCHAR(100) NOT NULL REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (page_id, user_id)
);

CREATE TABLE IF NOT EXISTS fb_page_spend_declarations (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) NOT NULL REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    spend_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    amount          NUMERIC(18,2) NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fb_page_ad_account_map (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) NOT NULL UNIQUE REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    ad_account_id   VARCHAR(100) NOT NULL,
    ad_account_name VARCHAR(255),
    shop_key        VARCHAR(100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_MIGRATION_ALTER_SQL = """
DO $$
BEGIN
    -- user_ad_account_map: employee → their own ad accounts (pre-mapped by admin)
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_name='user_ad_account_map') THEN
        CREATE TABLE user_ad_account_map (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ad_account_id   VARCHAR(100) NOT NULL,
            ad_account_name VARCHAR(255),
            assigned_by     BIGINT REFERENCES users(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, ad_account_id)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='ad_account_id') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN ad_account_id VARCHAR(100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='ad_account_name') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN ad_account_name VARCHAR(255);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='phan_loai') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN phan_loai VARCHAR(20) DEFAULT 'ma_win';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='tien_chua_thue') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN tien_chua_thue NUMERIC(18,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='thue_rate') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN thue_rate NUMERIC(6,4) DEFAULT 0.061;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='fb_page_spend_declarations' AND column_name='thanh_tien') THEN
        ALTER TABLE fb_page_spend_declarations ADD COLUMN thanh_tien NUMERIC(18,2);
    END IF;
END$$;
"""

_migrated = False

def _migrated_flag_reset():
    global _migrated
    _migrated = False

def run_migrations():
    global _migrated
    if _migrated:
        return
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_MIGRATION_SQL)
                cur.execute(_MIGRATION_ALTER_SQL)
        _migrated = True
        logger.info("fb_pages: migrations OK")
    except Exception as exc:
        logger.warning("fb_pages migration warning: %s", exc)

# ── Token store (ad accounts) ────────────────────────────────────────
def _load_ad_accounts():
    store_path = Path(__file__).resolve().parents[2] / "facebook_ads_tokens_store.json"
    if not store_path.exists():
        return []
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        records = data.get("records", []) if isinstance(data, dict) else data
        seen, out = set(), []
        for r in records:
            aid = str(r.get("ad_account_id", "") or "").strip()
            if aid and aid not in seen:
                seen.add(aid)
                out.append({
                    "ad_account_id": aid,
                    "shop_key": r.get("shop_key", ""),
                    "note": r.get("note", ""),
                    "label": r.get("note") or f"act_{aid}",
                })
        return out
    except Exception as exc:
        logger.warning("fb_pages: cannot load token store: %s", exc)
        return []

# ── Graph API helpers ────────────────────────────────────────────────
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

def _paginate_all(url: str, params: dict, timeout: int = 20) -> list:
    """Follow Facebook cursor pagination and return all data items."""
    results = []
    while url:
        resp = requests.get(url, params=params, timeout=timeout)
        if not resp.ok:
            logger.warning("_paginate_all %s → %s", url.split("?")[0], resp.text[:200])
            break
        data = resp.json()
        results.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}
    return results


def _collect_nested_pages(pages_node: dict, biz_id: str, endpoint: str,
                           token: str, seen: dict) -> int:
    """Given a nested pages node (with data + paging), collect all pages
    following the pagination. Returns count of new pages added."""
    new_count = 0
    items = pages_node.get("data", [])
    for p in items:
        if p.get("id") and p["id"] not in seen:
            seen[p["id"]] = p
            new_count += 1
    # Follow nested pagination if needed
    next_url = pages_node.get("paging", {}).get("next")
    while next_url:
        try:
            resp = requests.get(next_url, timeout=20)
            if not resp.ok:
                break
            data = resp.json()
            for p in data.get("data", []):
                if p.get("id") and p["id"] not in seen:
                    seen[p["id"]] = p
                    new_count += 1
            next_url = data.get("paging", {}).get("next")
        except Exception:
            break
    return new_count


def _get_pages(user_token: str) -> list:
    """Fetch ALL pages the user can manage — three sources merged & de-duplicated:
    1. /me/accounts (direct page admin roles)
    2. Each Business Manager portfolio: owned_pages + client_pages (nested pagination)
    """
    seen: dict = {}  # page_id → page dict

    # ── 1. Direct admin pages (/me/accounts) ─────────────────────────────
    logger.info("_get_pages: fetching /me/accounts ...")
    direct = _paginate_all(
        f"{GRAPH}/me/accounts",
        {"access_token": user_token, "fields": "id,name,category,picture", "limit": 200},
    )
    for p in direct:
        if p.get("id"):
            seen[p["id"]] = p
    logger.info("_get_pages: /me/accounts → %d pages", len(direct))

    # ── 2. Business Manager portfolios ───────────────────────────────────
    try:
        # Paginate through all businesses (user may have 50+)
        PAGE_FIELDS = "id,name,category,picture"
        biz_url = f"{GRAPH}/me/businesses"
        biz_params = {
            "access_token": user_token,
            "fields": f"id,name,owned_pages{{{PAGE_FIELDS}}},client_pages{{{PAGE_FIELDS}}}",
            "limit": 50,
        }
        biz_page = 0
        total_biz = 0
        while biz_url:
            resp = requests.get(biz_url, params=biz_params, timeout=20)
            if not resp.ok:
                logger.warning("_get_pages: /me/businesses error: %s", resp.text[:200])
                break
            data = resp.json()
            businesses = data.get("data", [])
            total_biz += len(businesses)
            biz_page += 1

            for biz in businesses:
                biz_id = biz["id"]
                for ep_key in ("owned_pages", "client_pages"):
                    node = biz.get(ep_key) or {}
                    if node:
                        added = _collect_nested_pages(node, biz_id, ep_key, user_token, seen)
                        if added:
                            logger.info("_get_pages: biz %s/%s → +%d new pages",
                                        biz_id, ep_key, added)

            biz_url = data.get("paging", {}).get("next")
            biz_params = {}

        logger.info("_get_pages: scanned %d business portfolio(s)", total_biz)
    except Exception as exc:
        logger.warning("_get_pages: Business Manager fetch failed: %s", exc)

    all_pages = list(seen.values())
    logger.info("_get_pages DONE: %d unique pages total", len(all_pages))
    return all_pages

def _get_fb_user_id(user_token: str) -> str:
    resp = requests.get(f"{GRAPH}/me", params={"access_token": user_token, "fields": "id"}, timeout=10)
    return resp.json().get("id", "")

# ── DB queries ───────────────────────────────────────────────────────
def _all_pages():
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.id, p.page_id, p.page_name, p.page_category, p.picture_url,
                           p.is_active, p.created_at,
                           m.ad_account_id, m.ad_account_name, m.shop_key,
                           COALESCE(
                               (SELECT json_agg(json_build_object('user_id', a.user_id, 'name', u.full_name, 'username', u.username))
                                FROM fb_page_assignments a JOIN users u ON u.id=a.user_id
                                WHERE a.page_id=p.page_id), '[]'::json
                           ) as assignees
                    FROM fb_pages p
                    LEFT JOIN fb_page_ad_account_map m ON m.page_id=p.page_id
                    ORDER BY p.page_name
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_all_pages error: %s", exc)
        return []

def _all_users():
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, username, full_name, role FROM users WHERE status='active' ORDER BY full_name")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_all_users error: %s", exc)
        return []

def _recent_declarations(page_id=None, spend_date=None, limit=50):
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                where_clauses = []
                params = []
                if page_id:
                    where_clauses.append("d.page_id = %s")
                    params.append(page_id)
                if spend_date:
                    where_clauses.append("d.spend_date = %s")
                    params.append(spend_date)
                where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
                params.append(limit)
                cur.execute(f"""
                    SELECT d.id, d.page_id, p.page_name, d.spend_date,
                           d.amount, d.note, d.created_at,
                           u.full_name as user_name, u.username, d.user_id,
                           d.ad_account_id, d.ad_account_name, d.phan_loai,
                           d.tien_chua_thue, d.thue_rate, d.thanh_tien
                    FROM fb_page_spend_declarations d
                    JOIN fb_pages p ON p.page_id = d.page_id
                    JOIN users u ON u.id = d.user_id
                    {where_sql}
                    ORDER BY d.spend_date DESC, d.created_at DESC
                    LIMIT %s
                """, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_recent_declarations error: %s", exc)
        return []


def _page_ad_accounts(page_id: str):
    """Get mapped ad accounts for a specific page (from fb_page_ad_account_map)."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ad_account_id, ad_account_name, shop_key
                    FROM fb_page_ad_account_map
                    WHERE page_id = %s
                """, (page_id,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_page_ad_accounts error: %s", exc)
        return []


def _user_ad_accounts(user_id):
    """Get ad accounts mapped to a specific user (from user_ad_account_map)."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, ad_account_id, ad_account_name, created_at
                    FROM user_ad_account_map
                    WHERE user_id = %s
                    ORDER BY ad_account_name
                """, (user_id,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_user_ad_accounts error: %s", exc)
        return []


def _users_with_ad_accounts():
    """Return all active users with their mapped ad accounts aggregated."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        u.id, u.username, u.full_name, u.role,
                        COALESCE(
                            json_agg(
                                json_build_object(
                                    'id',              m.id,
                                    'ad_account_id',   m.ad_account_id,
                                    'ad_account_name', m.ad_account_name
                                ) ORDER BY m.ad_account_name
                            ) FILTER (WHERE m.id IS NOT NULL),
                            '[]'::json
                        ) AS ad_accounts
                    FROM users u
                    LEFT JOIN user_ad_account_map m ON m.user_id = u.id
                    WHERE u.status = 'active'
                    GROUP BY u.id, u.username, u.full_name, u.role
                    ORDER BY u.full_name
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("_users_with_ad_accounts error: %s", exc)
        return []

# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════

@fb_pages_bp.route("/")
@login_required
def pages_list():
    return redirect("/page-account/pages-cty")

# ── OAuth ────────────────────────────────────────────────────────────
@fb_pages_bp.route("/auth/start")
@admin_required
def auth_start():
    if not _fb_app_id():
        flash("Chưa cấu hình FACEBOOK_APP_ID. Liên hệ admin kỹ thuật.", "danger")
        return redirect(url_for("fb_pages.pages_list"))
    state = str(int(time.time()))
    session["fb_oauth_state"] = state
    scope = "ads_read,pages_show_list,pages_read_engagement,business_management"
    url = (
        f"https://www.facebook.com/v20.0/dialog/oauth"
        f"?client_id={_fb_app_id()}"
        f"&redirect_uri={_redirect_uri()}"
        f"&scope={scope}"
        f"&state={state}"
        f"&response_type=code"
    )
    return redirect(url)

@fb_pages_bp.route("/auth/callback")
@admin_required
def auth_callback():
    error = request.args.get("error")
    if error:
        flash(f"Facebook từ chối quyền: {request.args.get('error_description','')}", "danger")
        return redirect(url_for("fb_pages.pages_list"))

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if state != session.pop("fb_oauth_state", None):
        flash("State không khớp — thử lại.", "danger")
        return redirect(url_for("fb_pages.pages_list"))

    try:
        short_token = _exchange_code(code)
        long_token  = _long_lived_token(short_token)
        fb_user_id  = _get_fb_user_id(long_token)
        pages       = _get_pages(long_token)
    except Exception as exc:
        flash(f"Lỗi kết nối Facebook: {exc}", "danger")
        return redirect(url_for("fb_pages.pages_list"))

    if not pages:
        flash("Tài khoản Facebook này không quản lý page nào.", "warning")
        return redirect(url_for("fb_pages.pages_list"))

    saved = 0
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                for p in pages:
                    pic = (p.get("picture", {}) or {}).get("data", {}).get("url", "") if isinstance(p.get("picture"), dict) else ""
                    cur.execute("""
                        INSERT INTO fb_pages (page_id, page_name, page_category, picture_url, access_token, fb_user_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (page_id) DO UPDATE SET
                            page_name=EXCLUDED.page_name,
                            page_category=EXCLUDED.page_category,
                            picture_url=EXCLUDED.picture_url,
                            access_token=EXCLUDED.access_token,
                            fb_user_id=EXCLUDED.fb_user_id,
                            updated_at=NOW()
                    """, (p["id"], p["name"], p.get("category",""), pic, long_token, fb_user_id))
                    saved += 1
        flash(f"Đã đồng bộ {saved} Facebook Page thành công!", "success")
    except Exception as exc:
        flash(f"Lỗi lưu DB: {exc}", "danger")

    # ── Kéo luôn TÀI KHOẢN QUẢNG CÁO (TK QC) của user (scope ads_read) → pa_ad_accounts ──
    try:
        import requests as _rq
        rr = _rq.get(
            "https://graph.facebook.com/v20.0/me/adaccounts",
            params={"fields": "id,name,account_status,currency,timezone_name,business",
                    "access_token": long_token, "limit": 200},
            timeout=25,
        )
        accts = (rr.json() or {}).get("data", []) or []
        from modules.page_account.pa_db import (
            run_migrations as _pa_mig, upsert_ad_account as _up_acc, upsert_bm as _up_bm,
        )
        from db import get_conn as _gc
        n_acc = 0
        with _gc() as _conn2:
            _pa_mig(_conn2)
            for a in accts:
                biz = a.get("business") or {}
                bm_id = str(biz.get("id") or "").strip() or None
                if bm_id:
                    try:
                        _up_bm(_conn2, bm_id, biz.get("name", "") or bm_id, [], fb_user_id)
                    except Exception:
                        pass
                try:
                    _up_acc(_conn2, a, bm_id)
                    n_acc += 1
                except Exception:
                    pass
        if n_acc:
            flash(f"Đã đồng bộ {n_acc} tài khoản quảng cáo (TK QC).", "success")
    except Exception:
        pass

    # ── Lưu TOKEN vào kho để CRON tự kéo CHI PHÍ (insights) hằng ngày ──
    #    (giống luồng System User token: pa_fb_tokens + JSON store theo từng TK QC)
    try:
        from modules.page_account.pa_db import upsert_token as _up_tok, run_migrations as _pa_mig2
        from modules.page_account import _write_token_to_json_store as _wjs, _load_all_mappings as _lam
        from db import get_conn as _gc3
        with _gc3() as _c3:
            _pa_mig2(_c3)
            _up_tok(_c3, fb_user_id, "", long_token, "fb-login", token_type="user")
        try:
            _wjs(fb_user_id, long_token, _lam())
        except Exception:
            pass
    except Exception:
        pass

    return redirect(url_for("fb_pages.pages_list"))

# ── Assign page → user ───────────────────────────────────────────────
@fb_pages_bp.route("/assign", methods=["POST"])
@admin_required
def assign_page():
    page_id = request.form.get("page_id", "").strip()
    user_id = request.form.get("user_id", "").strip()
    action  = request.form.get("action", "add")

    if not page_id or not user_id:
        flash("Thiếu thông tin gán page.", "warning")
        return redirect(url_for("fb_pages.pages_list"))

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if action == "remove":
                    cur.execute("DELETE FROM fb_page_assignments WHERE page_id=%s AND user_id=%s",
                                (page_id, int(user_id)))
                    flash("Đã bỏ gán nhân viên khỏi page.", "info")
                else:
                    cur.execute("""
                        INSERT INTO fb_page_assignments (page_id, user_id, assigned_by)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (page_id, user_id) DO NOTHING
                    """, (page_id, int(user_id), session.get("user_id")))
                    flash("Đã gán nhân viên vào page.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("fb_pages.pages_list") + f"#page-{page_id}")

# ── Map page → ad account ────────────────────────────────────────────
@fb_pages_bp.route("/map-ad", methods=["POST"])
@admin_required
def map_ad_account():
    page_id       = request.form.get("page_id", "").strip()
    ad_account_id = request.form.get("ad_account_id", "").strip()
    shop_key      = request.form.get("shop_key", "").strip()
    ad_account_name = request.form.get("ad_account_name", "").strip()

    if not page_id:
        flash("Thiếu page_id.", "warning")
        return redirect(url_for("fb_pages.pages_list"))

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if not ad_account_id:
                    cur.execute("DELETE FROM fb_page_ad_account_map WHERE page_id=%s", (page_id,))
                    flash("Đã xoá mapping tài khoản quảng cáo.", "info")
                else:
                    cur.execute("""
                        INSERT INTO fb_page_ad_account_map
                            (page_id, ad_account_id, ad_account_name, shop_key)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (page_id) DO UPDATE SET
                            ad_account_id=EXCLUDED.ad_account_id,
                            ad_account_name=EXCLUDED.ad_account_name,
                            shop_key=EXCLUDED.shop_key,
                            updated_at=NOW()
                    """, (page_id, ad_account_id, ad_account_name, shop_key))
                    flash("Đã map Page với tài khoản quảng cáo.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("fb_pages.pages_list") + f"#page-{page_id}")

# ── Gán TK QC cho nhân viên (admin only) ────────────────────────────
@fb_pages_bp.route("/user-ad-map/add", methods=["POST"])
@admin_required
def user_ad_map_add():
    user_id         = request.form.get("user_id", "").strip()
    ad_account_id   = request.form.get("ad_account_id", "").strip()
    ad_account_name = request.form.get("ad_account_name", "").strip()

    if not user_id or not ad_account_id:
        flash("Thiếu thông tin.", "warning")
        return redirect(url_for("fb_pages.pages_list") + "#tab-user-ad")

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_ad_account_map
                        (user_id, ad_account_id, ad_account_name, assigned_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, ad_account_id) DO UPDATE SET
                        ad_account_name = EXCLUDED.ad_account_name,
                        assigned_by     = EXCLUDED.assigned_by
                """, (int(user_id), ad_account_id, ad_account_name or ad_account_id,
                      session.get("user_id")))
        flash("Đã gán tài khoản QC cho nhân viên.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("fb_pages.pages_list") + "#tab-user-ad")


@fb_pages_bp.route("/user-ad-map/remove", methods=["POST"])
@admin_required
def user_ad_map_remove():
    map_id = request.form.get("map_id", "").strip()
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_ad_account_map WHERE id=%s", (int(map_id),))
        flash("Đã xoá TK QC của nhân viên.", "info")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("fb_pages.pages_list") + "#tab-user-ad")


# ── API: get user's mapped ad accounts ──────────────────────────────
@fb_pages_bp.route("/api/user-ad-accounts")
@login_required
def api_user_ad_accounts():
    uid = request.args.get("user_id", "").strip()
    if not uid:
        uid = session.get("user_id")
    if not uid:
        return jsonify([])
    return jsonify(_user_ad_accounts(int(uid)))


# ── Toggle active ────────────────────────────────────────────────────
@fb_pages_bp.route("/toggle-active", methods=["POST"])
@admin_required
def toggle_active():
    page_id = request.form.get("page_id", "").strip()
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE fb_pages SET is_active = NOT is_active, updated_at=NOW()
                    WHERE page_id=%s
                """, (page_id,))
        flash("Đã cập nhật trạng thái page.", "info")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("fb_pages.pages_list"))


# ── Thêm page thủ công ──────────────────────────────────────────────
@fb_pages_bp.route("/add-manual", methods=["POST"])
@admin_required
def add_manual_page():
    page_name = request.form.get("page_name", "").strip()
    page_url  = request.form.get("page_url", "").strip()
    if not page_name:
        flash("Vui lòng nhập tên Page.", "warning")
        return redirect(url_for("fb_pages.pages_list"))

    import re, uuid
    page_id = ""
    if page_url:
        m = re.search(r'facebook\.com/(?:pages/[^/]+/|)(\d+)', page_url)
        if m:
            page_id = m.group(1)
    if not page_id:
        page_id = f"manual_{uuid.uuid4().hex[:12]}"

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_pages (page_id, page_name, page_category, picture_url)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (page_id) DO UPDATE SET page_name=EXCLUDED.page_name, updated_at=NOW()
                """, (page_id, page_name, "Thủ công", page_url or ""))
        flash(f"Đã thêm page '{page_name}'.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("fb_pages.pages_list"))

# ── API: lấy TK QC của page (cho modal JS) ────────────────────────────
@fb_pages_bp.route("/api/page-ad-accounts")
@login_required
def api_page_ad_accounts():
    from flask import jsonify
    page_id = request.args.get("page_id", "").strip()
    accounts = _page_ad_accounts(page_id) if page_id else []
    return jsonify(accounts)


# ── API: lấy khai báo của page trong ngày (cho modal JS) ─────────────
@fb_pages_bp.route("/api/declarations")
@login_required
def api_declarations():
    from flask import jsonify
    import decimal
    page_id    = request.args.get("page_id", "").strip()
    spend_date = request.args.get("spend_date", "").strip()
    if not page_id or not spend_date:
        return jsonify([])

    user_id      = session.get("user_id")
    current_role = session.get("role", "staff")

    decls = _recent_declarations(page_id=page_id, spend_date=spend_date, limit=100)

    # Apply role-based visibility consistent with report screen
    if current_role in ("admin", "superadmin", "manager", "ketoan"):
        visible = decls
    elif current_role == "leader":
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT u.id FROM users u
                        JOIN teams t ON t.id = u.team_id
                        WHERE t.leader_user_id = %s AND u.status = 'active'
                    """, (user_id,))
                    team_ids = {r[0] for r in cur.fetchall()}
            team_ids.add(user_id)
            visible = [d for d in decls if d["user_id"] in team_ids]
        except Exception:
            visible = [d for d in decls if d["user_id"] == user_id]
    else:
        visible = [d for d in decls if d["user_id"] == user_id]

    result = []
    for d in visible:
        row = {}
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
            elif isinstance(v, decimal.Decimal):
                row[k] = float(v)
            else:
                row[k] = v
        result.append(row)
    return jsonify(result)


# ── Khai báo chi tiêu ────────────────────────────────────────────────
@fb_pages_bp.route("/declare-spend", methods=["POST"])
@login_required
def declare_spend():
    page_id         = request.form.get("page_id", "").strip()
    spend_date      = request.form.get("spend_date", "").strip()
    ad_account_id   = request.form.get("ad_account_id", "").strip()
    ad_account_name = request.form.get("ad_account_name", "").strip()
    phan_loai       = request.form.get("phan_loai", "ma_win").strip()
    tien_chua_thue_raw = request.form.get("tien_chua_thue", "0").strip().replace(",", "")
    note            = request.form.get("note", "").strip()
    user_id         = session.get("user_id")

    if not page_id or not user_id:
        flash("Thiếu thông tin khai báo.", "warning")
        return redirect(url_for("fb_pages.pages_list"))

    if not ad_account_id and not ad_account_name:
        flash("Vui lòng chọn hoặc nhập tên tài khoản quảng cáo.", "warning")
        return redirect(url_for("fb_pages.pages_list") + f"#page-{page_id}")

    if phan_loai not in ("ma_win", "ma_test"):
        phan_loai = "ma_win"

    try:
        tien_chua_thue = float(tien_chua_thue_raw) if tien_chua_thue_raw else 0
    except ValueError:
        tien_chua_thue = 0

    if tien_chua_thue <= 0:
        flash("Vui lòng nhập tiền chưa VAT hợp lệ (> 0).", "warning")
        return redirect(url_for("fb_pages.pages_list") + f"#page-{page_id}")

    thue_rate = 0.061
    thanh_tien = round(tien_chua_thue * (1 + thue_rate), 2)

    try:
        from db import get_conn
        from datetime import date
        sdate = spend_date if spend_date else date.today().isoformat()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_page_spend_declarations
                        (page_id, user_id, spend_date, amount, note,
                         ad_account_id, ad_account_name, phan_loai,
                         tien_chua_thue, thue_rate, thanh_tien)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (page_id, user_id, sdate, thanh_tien, note,
                      ad_account_id or None, ad_account_name or None,
                      phan_loai, tien_chua_thue, thue_rate, thanh_tien))
        flash("Đã lưu khai báo chi tiêu.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("fb_pages.pages_list") + f"#page-{page_id}")

# ── Xoá khai báo chi tiêu ────────────────────────────────────────────
@fb_pages_bp.route("/delete-spend", methods=["POST"])
@login_required
def delete_spend():
    decl_id = request.form.get("decl_id", "").strip()
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if role in ("admin", "superadmin", "manager"):
                    cur.execute("DELETE FROM fb_page_spend_declarations WHERE id=%s", (decl_id,))
                else:
                    cur.execute("DELETE FROM fb_page_spend_declarations WHERE id=%s AND user_id=%s",
                                (decl_id, user_id))
        flash("Đã xoá khai báo.", "info")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("fb_pages.pages_list"))


# ── Báo cáo chi phí QC ───────────────────────────────────────────────
@fb_pages_bp.route("/bao-cao-chi-phi-qc")
@login_required
def bao_cao_chi_phi_qc():
    from datetime import date, timedelta
    from db import get_conn

    today = date.today()
    default_from = (today.replace(day=1)).isoformat()
    default_to   = today.isoformat()

    date_from = request.args.get("date_from", default_from).strip()
    date_to   = request.args.get("date_to",   default_to).strip()

    user_id      = session.get("user_id")
    current_role = session.get("role", "staff")

    rows = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Determine scope based on role
                if current_role in ("admin", "superadmin", "manager", "ketoan"):
                    # See all
                    user_filter_sql = ""
                    params = [date_from, date_to]
                elif current_role == "leader":
                    # See team members (users where team.leader_user_id = me)
                    cur.execute("""
                        SELECT u.id FROM users u
                        JOIN teams t ON t.id = u.team_id
                        WHERE t.leader_user_id = %s AND u.status = 'active'
                    """, (user_id,))
                    team_member_ids = [r[0] for r in cur.fetchall()]
                    if user_id not in team_member_ids:
                        team_member_ids.append(user_id)
                    placeholders = ",".join(["%s"] * len(team_member_ids))
                    user_filter_sql = f"AND d.user_id IN ({placeholders})"
                    params = [date_from, date_to] + team_member_ids
                else:
                    # staff: only self
                    user_filter_sql = "AND d.user_id = %s"
                    params = [date_from, date_to, user_id]

                cur.execute(f"""
                    SELECT
                        u.id as user_id,
                        u.full_name,
                        u.username,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win' THEN d.tien_chua_thue ELSE 0 END), 0) as win_chua_vat,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win' THEN d.thanh_tien ELSE 0 END), 0) as win_co_vat,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.tien_chua_thue ELSE 0 END), 0) as test_chua_vat,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.thanh_tien ELSE 0 END), 0) as test_co_vat,
                        COALESCE(SUM(d.thanh_tien), 0) as tong
                    FROM fb_page_spend_declarations d
                    JOIN users u ON u.id = d.user_id
                    WHERE d.spend_date BETWEEN %s AND %s
                    {user_filter_sql}
                    GROUP BY u.id, u.full_name, u.username
                    ORDER BY u.full_name
                """, params)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("bao_cao_chi_phi_qc error: %s", exc)
        flash(f"Lỗi truy vấn: {exc}", "danger")

    return render_template(
        "fb_pages/bao_cao_chi_phi_qc.html",
        rows=rows,
        date_from=date_from,
        date_to=date_to,
        current_role=current_role,
    )


def register_fb_pages_module(app):
    _migrated_flag_reset()
    run_migrations()
    app.register_blueprint(fb_pages_bp)
    logger.info("fb_pages module registered at /fb-pages/")
