from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from flask import abort, render_template_string, request, url_for, redirect, session, g, has_request_context, send_file, jsonify
from openpyxl import Workbook

from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm, HCM_TZ
import perm_utils
import pancake_auth as _pancake_auth

try:
    from db import get_conn as get_db_conn
except Exception:
    get_db_conn = None

try:
    from repositories.admin_repo import (
        delete_fb_ad_account_mapping as repo_delete_fb_ad_account_mapping,
        get_shop_id_by_key as repo_get_shop_id_by_key,
        get_fb_ad_account_mappings as repo_get_fb_ad_account_mappings,
        list_fb_ad_account_mappings as repo_list_fb_ad_account_mappings,
        toggle_fb_ad_account_mapping_status as repo_toggle_fb_ad_account_mapping_status,
        upsert_fb_ad_account_mapping as repo_upsert_fb_ad_account_mapping,
    )
except Exception:
    repo_delete_fb_ad_account_mapping = None
    repo_get_shop_id_by_key = None
    repo_get_fb_ad_account_mappings = None
    repo_list_fb_ad_account_mappings = None
    repo_toggle_fb_ad_account_mapping_status = None
    repo_upsert_fb_ad_account_mapping = None

from app_constants import (
    BASE_DIR, DATA_GLOBS, SHOPS_FILE, CONFIG_FILE, USERS_FILE, WEBSITES_FILE,
    LIVE_STATUS_CACHE_FILE, DASHBOARD_WEB_VERSION, DASHBOARD_USERNAME,
    DASHBOARD_PASSWORD, PAGE_TEMPLATE, LOW_STOCK_THRESHOLD, SLOW_DAYS,
    MIN_OLD_QTY, MAX_SOLD_IN_7D, MONTH_LOSS_WARN, MONTH_LOSS_SEVERE,
)

# ---------------------------------------------------------------------------
# Thread state globals (lines 93-102 of original web_app.py)
# ---------------------------------------------------------------------------
SYNC_IN_PROGRESS_DATES: set[str] = set()
SYNC_IN_PROGRESS_LOCK = threading.Lock()
AUTO_SYNC_MISSING_DATE_FAILED: set[str] = set()
# Cooldown: không sync lại cùng 1 ngày trong vòng 10 phút
_AUTO_SYNC_BG_LAST_RUN: Dict[str, float] = {}
_AUTO_SYNC_BG_COOLDOWN = 10 * 60  # 10 phút
HOME_ALERT_COUNTS_CACHE: Dict[str, Dict[str, Any]] = {}
HOME_ALERT_COUNTS_CACHE_LOCK = threading.Lock()
HOME_CARRIER_PICKUP_CACHE: Dict[str, Dict[str, Any]] = {}
HOME_CARRIER_PICKUP_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# login_required decorator (lines 307-325 of original web_app.py)
# ---------------------------------------------------------------------------
def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("auth.login", next=next_url))
        # Luôn đồng bộ role từ users.json để đổi role (vd. lên leader) có hiệu lực không cần đăng xuất.
        username = str(session.get("username", "")).strip()
        user = find_user(username) if username else None
        if user:
            session["role"] = str(user.get("role", "staff")).strip()
        elif username == DASHBOARD_USERNAME:
            session["role"] = "admin"
        elif not session.get("role"):
            session["role"] = "staff"
        return view_func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Module routing helpers (lines 327-351 of original web_app.py)
# ---------------------------------------------------------------------------
_MODULE_HOME_URLS = {
    "dashboard":    "/",
    "stock":        "/stock",
    "slow":         "/slow",
    "sent_items":   "/sent-items",
    "export_items": "/export-items-v2",
    "ads_kpi":      "/ads-kpi",
    "ads_delay":    "/ads-delay",
    "loss_report":  "/loss-day-alert",
    "chi_phi_qc":   "/chi-phi-qc/",
    "fb_pages":     "/fb-pages/",
    "kho_vat_ly":   "/kho-vat-ly/",
    "salary":       "/salary",
    "settings":     "/settings",
    "cham_cong":    "/cham-cong/",
}
_MODULE_PRIORITY = [
    "dashboard", "kho_vat_ly", "stock", "slow", "chi_phi_qc",
    "sent_items", "export_items", "salary", "settings", "cham_cong"
]

def _find_home_url(allowed: set) -> str:
    for key in _MODULE_PRIORITY:
        if key in allowed:
            return _MODULE_HOME_URLS.get(key, "/")
    return "/login"


# ---------------------------------------------------------------------------
# Helper functions (lines 4380-8808 of original web_app.py)
# ---------------------------------------------------------------------------
def format_money(value: float) -> str:
    if value is None:
        return "₫0"
    if abs(value) >= 1_000_000_000:
        return f"₫{value / 1_000_000_000:.1f}B"
    try:
        return f"{int(round(value)):,}".replace(",", ".")
    except Exception:
        return "0"

def format_int(x):
    try:
        return "{:,.0f}".format(float(x)).replace(",", ".")
    except Exception:
        return "0"

def money_class(value: float) -> str:
    return "money-loss" if value < 0 else "money-good"

def status_class(status: str) -> str:
    if status == "Lỗ":
        return "tag-loss"
    if status == "Tốt" or status == "Ổn định":
        return "tag-good"
    return "tag-warn"

def discover_files(allowed_shop_keys: Optional[set] = None) -> List[str]:
    all_files_cached = _get_request_cache("discover_files_all")
    if isinstance(all_files_cached, list):
        unique_files = all_files_cached
    else:
        files: List[str] = []
        for pattern in DATA_GLOBS:
            files.extend(glob.glob(pattern))
        unique_files = sorted(set(files))
        _set_request_cache("discover_files_all", unique_files)

    if allowed_shop_keys is None:
        return unique_files

    scope_key = ",".join(sorted(str(x) for x in allowed_shop_keys))
    scoped_cache_key = f"discover_files_scope::{scope_key}"
    scoped_cached = _get_request_cache(scoped_cache_key)
    if isinstance(scoped_cached, list):
        return scoped_cached

    filtered = []
    for path in unique_files:
        shop_key = extract_shop_key_from_filename(path)
        if shop_key in allowed_shop_keys:
            filtered.append(path)
    _set_request_cache(scoped_cache_key, filtered)
    return filtered

def try_parse_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_request_cache(key: str) -> Any:
    if not has_request_context():
        return None
    cache = getattr(g, "_web_request_cache", None)
    if not isinstance(cache, dict):
        return None
    return cache.get(key)


def _set_request_cache(key: str, value: Any) -> None:
    if not has_request_context():
        return
    cache = getattr(g, "_web_request_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(g, "_web_request_cache", cache)
    cache[key] = value


def _load_shop_meta_from_db() -> Optional[Dict[str, Dict[str, str]]]:
    """Đọc shops từ DB. Trả về None nếu DB không khả dụng hoặc trống."""
    if not get_db_conn:
        return None
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.shop_key, s.pancake_shop_id, s.shop_name,
                       s.status::text, t.team_code,
                       COALESCE(s.business_type, 'cty') AS business_type
                FROM shops s
                LEFT JOIN teams t ON t.id = s.team_id
            """)
            rows = cur.fetchall()
            if not rows:
                return None
            result: Dict[str, Dict[str, str]] = {}
            for shop_key, pancake_id, shop_name, status, team_code, business_type in rows:
                if shop_key:
                    result[shop_key] = {
                        "shop_key": shop_key,
                        "shop_id": pancake_id or "",
                        "shop_name": shop_name or shop_key,
                        "status": status or "active",
                        "team_id": team_code or "",
                        "business_type": (business_type or "cty").lower(),
                    }
            return result
    except Exception:
        return None


def invalidate_shop_meta_cache() -> None:
    """Gọi sau khi sửa bảng shops / wh_shops trong Cài đặt — tránh Redis giữ shop_meta_map cũ (TTL 5 phút)."""
    try:
        from redis_cache import cache_delete

        cache_delete("shop_meta_map")
    except Exception:
        pass


def load_shop_name_map() -> Dict[str, str]:
    meta = load_shop_meta_map()
    return {k: v.get("shop_name", k) for k, v in meta.items()}


def load_shop_meta_map() -> Dict[str, Dict[str, str]]:
    # Request-level cache (trong 1 request)
    cached = _get_request_cache("shop_meta_map")
    if cached is not None:
        return cached

    # Redis cross-worker cache (TTL 5 phút — shops ít thay đổi)
    from redis_cache import cache_get, cache_set as _rc_set
    _redis_key = "shop_meta_map"
    redis_cached = cache_get(_redis_key)
    if redis_cached is not None:
        _set_request_cache("shop_meta_map", redis_cached)
        return redis_cached

    db_result = _load_shop_meta_from_db()
    if db_result is not None:
        _rc_set(_redis_key, db_result, ttl=300)
        _set_request_cache("shop_meta_map", db_result)
        return db_result

    result: Dict[str, Dict[str, str]] = {}
    if not os.path.exists(SHOPS_FILE):
        _set_request_cache("shop_meta_map", result)
        return result
    try:
        data = try_parse_json(SHOPS_FILE)
    except Exception:
        _set_request_cache("shop_meta_map", result)
        return result

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            shop_key = str(item.get("shop_key", "")).strip()
            shop_id = str(item.get("shop_id", "")).strip()
            shop_name = str(item.get("shop_name", "")).strip()
            status = str(item.get("status", "")).strip()
            team_id = str(item.get("team_id", "")).strip()
            business_type = str(item.get("business_type") or "cty").strip().lower()
            if business_type not in ("cty", "hkd", "pos3"):
                business_type = "cty"
            if shop_key:
                result[shop_key] = {
                    "shop_key": shop_key,
                    "shop_id": shop_id,
                    "shop_name": shop_name or shop_key,
                    "status": status,
                    "team_id": team_id,
                    "business_type": business_type,
                }
    _set_request_cache("shop_meta_map", result)
    return result

def build_shop_id_to_meta_map() -> Dict[str, Dict[str, str]]:
    meta_map = get_visible_shop_meta_map()
    result: Dict[str, Dict[str, str]] = {}
    for _, meta in meta_map.items():
        shop_id = meta.get("shop_id", "")
        if shop_id:
            result[shop_id] = meta
    return result

def load_active_shop_options() -> List[Dict[str, str]]:
    meta_map = get_visible_shop_meta_map()
    opts = []
    for _, meta in meta_map.items():
        if meta.get("status") == "active":
            opts.append({
                "shop_key": meta["shop_key"],
                "shop_name": meta["shop_name"],
                "shop_id": meta["shop_id"],
            })
    opts.sort(key=lambda x: x["shop_name"])
    return opts

def _load_config_from_db() -> Optional[Dict[str, Any]]:
    if not get_db_conn:
        return None
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM app_config")
            rows = cur.fetchall()
            if not rows:
                return None
            result: Dict[str, Any] = {}
            for key, value in rows:
                try:
                    result[key] = json.loads(value)
                except Exception:
                    result[key] = value
            return result
    except Exception:
        return None


def save_config_key(key: str, value: Any) -> bool:
    """Lưu 1 key vào app_config DB (và JSON backup)."""
    if get_db_conn:
        try:
            with get_db_conn() as conn:
                cur = conn.cursor()
                val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                cur.execute("""
                    INSERT INTO app_config (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """, (key, val_str))
                conn.commit()
        except Exception as e:
            logging.getLogger(__name__).error("save_config_key DB error: %s", e)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = try_parse_json(CONFIG_FILE) or {}
            cfg[key] = value
            save_json_file(CONFIG_FILE, cfg)
        except Exception:
            pass
    if has_request_context():
        cached = _get_request_cache("config_json") or {}
        cached[key] = value
        _set_request_cache("config_json", cached)
    return True


def load_config() -> Dict[str, Any]:
    cached = _get_request_cache("config_json")
    if cached is not None:
        return cached

    db_cfg = _load_config_from_db()
    if db_cfg is not None:
        _set_request_cache("config_json", db_cfg)
        return db_cfg

    if not os.path.exists(CONFIG_FILE):
        _set_request_cache("config_json", {})
        return {}
    try:
        result = try_parse_json(CONFIG_FILE)
        _set_request_cache("config_json", result)
        return result
    except Exception:
        _set_request_cache("config_json", {})
        return {}


def save_json_file(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _hash_password(plain: str) -> str:
    import bcrypt as _bcrypt
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(12)).decode()


def _check_user_password(entered: str, user: Dict) -> bool:
    """Check entered password against bcrypt hash or legacy plaintext."""
    pw_hash = user.get("password_hash", "")
    if pw_hash and pw_hash.startswith("$2"):
        import bcrypt as _bcrypt
        try:
            return _bcrypt.checkpw(entered.encode(), pw_hash.encode())
        except Exception:
            return False
    return entered == str(user.get("password", ""))


def _load_users_from_db() -> Optional[List[Dict]]:
    """Read users from PostgreSQL. Returns None if DB unavailable or empty."""
    if not get_db_conn:
        return None
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.username, u.password_hash, u.full_name,
                       u.role::text, u.status::text, t.team_code
                FROM users u
                LEFT JOIN teams t ON t.id = u.team_id
            """)
            rows = cur.fetchall()
            if not rows:
                return None
            cur.execute("""
                SELECT usa.user_id, s.shop_key, usa.assigned_from
                FROM user_shop_assignments usa
                JOIN shops s ON s.id = usa.shop_id
                WHERE usa.assigned_to IS NULL
            """)
            shops_by_user: Dict[int, List[str]] = {}
            assign_from_by_user: Dict[int, Any] = {}
            for user_id, shop_key, afrom in cur.fetchall():
                shops_by_user.setdefault(user_id, []).append(shop_key)
                if afrom and (user_id not in assign_from_by_user or afrom > assign_from_by_user[user_id]):
                    assign_from_by_user[user_id] = afrom
            users = []
            for uid, username, pw_hash, full_name, role, status, team_code in rows:
                assigned = shops_by_user.get(uid, [])
                if role in ("admin", "superadmin", "manager", "accountant", "it", "sale", "sale_leader") and not assigned:
                    assigned = ["*"]
                _af = assign_from_by_user.get(uid)
                users.append({
                    "id": uid,
                    "username": username,
                    "password_hash": pw_hash or "",
                    "password": "",
                    "full_name": full_name or "",
                    "role": role,
                    "status": status,
                    "team_id": team_code or "",
                    "assigned_shops": assigned,
                    "assigned_from": _af.isoformat() if _af else None,
                })
            return users
    except Exception:
        return None


def load_users() -> List[Dict[str, Any]]:
    db_users = _load_users_from_db()
    if db_users is not None:
        return db_users
    if os.path.exists(USERS_FILE):
        try:
            data = try_parse_json(USERS_FILE)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    default_admin = [{
        "id": "u-admin",
        "username": DASHBOARD_USERNAME,
        "password": DASHBOARD_PASSWORD,
        "role": "admin",
        "team_id": "",
        "assigned_shops": ["*"],
        "status": "active",
    }]
    save_json_file(USERS_FILE, default_admin)
    return default_admin


def save_users(users: List[Dict[str, Any]]) -> None:
    save_json_file(USERS_FILE, users)
    if not get_db_conn:
        return
    _log = logging.getLogger(__name__)
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT team_code, id FROM teams")
            team_map = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT shop_key, id FROM shops")
            shop_map = {r[0]: r[1] for r in cur.fetchall()}
            for u in users:
                username = str(u.get("username", "")).strip()
                if not username:
                    continue
                role = str(u.get("role", "staff"))
                if role not in {"admin", "leader", "staff", "accountant", "kho", "manager", "it", "superadmin", "sale", "sale_leader"}:
                    role = "staff"
                status = str(u.get("status", "active"))
                if status not in ("active", "inactive"):
                    status = "active"
                team_code = str(u.get("team_id", "")).strip()
                team_db_id = team_map.get(team_code) if team_code else None
                full_name = str(u.get("full_name") or u.get("username", ""))
                plain = str(u.get("password", "")).strip()
                pw_hash = u.get("password_hash", "")
                cur.execute("""
                    INSERT INTO users (username, password_hash, full_name, role, team_id, status)
                    VALUES (%s, %s, %s, %s::user_role, %s, %s::record_status)
                    ON CONFLICT (username) DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        role      = EXCLUDED.role,
                        team_id   = EXCLUDED.team_id,
                        status    = EXCLUDED.status
                    RETURNING id
                """, (username, pw_hash or _hash_password("1221"), full_name, role, team_db_id, status))
                db_id = cur.fetchone()[0]
                if plain:
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE id = %s",
                        (_hash_password(plain), db_id),
                    )
                assigned = u.get("assigned_shops", [])
                eff = (str(u.get("assign_from") or "").strip()) or None
                if "*" not in assigned:
                    want_ids = {shop_map[k] for k in assigned if shop_map.get(k)}
                    cur.execute(
                        "SELECT shop_id FROM user_shop_assignments"
                        " WHERE user_id = %s AND assigned_to IS NULL", (db_id,))
                    have_ids = {r[0] for r in cur.fetchall()}
                    removed = list(have_ids - want_ids)
                    if removed:
                        cur.execute(
                            "DELETE FROM user_shop_assignments WHERE user_id = %s"
                            " AND shop_id = ANY(%s) AND assigned_to IS NULL"
                            " AND assigned_from >= CURRENT_DATE", (db_id, removed))
                        cur.execute(
                            "UPDATE user_shop_assignments SET assigned_to = CURRENT_DATE - 1"
                            " WHERE user_id = %s AND shop_id = ANY(%s) AND assigned_to IS NULL",
                            (db_id, removed))
                    for shop_db_id in want_ids - have_ids:
                        cur.execute(
                            "INSERT INTO user_shop_assignments (user_id, shop_id, assigned_from)"
                            " VALUES (%s, %s, COALESCE(%s::date, CURRENT_DATE)) ON CONFLICT DO NOTHING",
                            (db_id, shop_db_id, eff))
                    try:
                        from repositories.admin_repo import sync_shop_mappings_for_user
                        sync_shop_mappings_for_user(cur, int(db_id), effective_date=eff)
                    except Exception as exc:
                        _log.warning("sync_shop_mappings_for_user error: %s", exc)
            conn.commit()
    except Exception as e:
        _log.error("save_users DB error: %s", e)

    try:
        from modules.cham_cong.cc_db import sync_cc_role_from_web_role
        with get_db_conn() as conn2:
            cur2 = conn2.cursor()
            for u in users:
                username = str(u.get("username", "")).strip()
                if not username:
                    continue
                role = str(u.get("role", "staff"))
                cur2.execute("SELECT id::text FROM users WHERE username=%s", (username,))
                row = cur2.fetchone()
                if row:
                    sync_cc_role_from_web_role(row[0], role)
    except Exception as e:
        _log.warning("save_users cc_role sync error: %s", e)


def load_websites() -> List[Dict[str, Any]]:
    if not os.path.exists(WEBSITES_FILE):
        save_json_file(WEBSITES_FILE, [])
        return []
    try:
        data = try_parse_json(WEBSITES_FILE)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_websites(websites: List[Dict[str, Any]]) -> None:
    save_json_file(WEBSITES_FILE, websites)


def find_user(username: str) -> Optional[Dict[str, Any]]:
    uname = str(username or "").strip()
    for user in load_users():
        if str(user.get("username", "")).strip() == uname:
            return user
    return None


def current_user() -> Optional[Dict[str, Any]]:
    uname = session.get("username", "")
    if not uname:
        return None
    return find_user(uname)


def is_admin_user() -> bool:
    return str(session.get("role", "")).strip() in {"admin", "superadmin", "manager", "it"}


def is_leader_user() -> bool:
    return str(session.get("role", "")).strip() in {"leader", "sale_leader"}


def is_accountant_user() -> bool:
    return str(session.get("role", "")).strip() == "accountant"


def can_view_ads_global() -> bool:
    return is_admin_user() or is_accountant_user()



# Note: _enforce_admin_only_sections registered in web_app.py via app.before_request
def _enforce_admin_only_sections() -> None:
    """
    Hạn chế các khu vực chỉ cho admin:
    - Phân bổ Ads (/ads-allocation)
    - Lương / KPI (/salary...)
    """
    path = request.path or ""
    # Để các route login / static xử lý bình thường.
    if not path.startswith("/ads-allocation") and not path.startswith("/salary"):
        return
    # Nếu chưa đăng nhập, để decorator @login_required xử lý.
    if not session.get("username"):
        return
    if not is_admin_user():
        abort(403)


def validate_settings_tab_access(tab: str) -> bool:
    restricted = {"staff", "shop_web", "facebook_ads", "facebook_tokens", "telegram", "accounts"}
    if tab not in restricted:
        return True
    if tab == "staff" and is_leader_user():
        return True
    if tab in restricted and not is_admin_user():
        return False
    return True


def _collect_user_shop_keys_for_team_member(
    user: Dict[str, Any], shop_meta: Dict[str, Dict[str, str]], team_id: str
) -> set:
    """Trong phạm vi team: '*' chỉ mở các shop có team_id khớp, không mở toàn bộ hệ thống."""
    assigned = normalize_assigned_shops(user.get("assigned_shops", ["*"]))
    team_keys = {sk for sk, m in shop_meta.items() if str(m.get("team_id", "")).strip() == team_id}
    if "*" in assigned:
        return set(team_keys)
    return {k for k in assigned if k in shop_meta}


def expand_team_shop_scope(team_id: str, shop_meta: Dict[str, Dict[str, str]], users: List[Dict[str, Any]]) -> set:
    """All shop keys visible to a team: shops tagged with team_id + every active member's assigned scope."""
    if not team_id:
        return set()
    keys = {sk for sk, m in shop_meta.items() if str(m.get("team_id", "")).strip() == team_id}
    for u in users:
        if str(u.get("status", "active")).strip() != "active":
            continue
        if str(u.get("team_id", "")).strip() != team_id:
            continue
        role = str(u.get("role", "staff")).strip()
        if role in {"admin", "superadmin", "manager", "accountant", "it"}:
            continue
        keys |= _collect_user_shop_keys_for_team_member(u, shop_meta, team_id)
    return keys


def get_team_owned_shop_keys(team_id: str, shop_meta: Dict[str, Dict[str, str]]) -> set:
    """Shop keys owned by a team strictly via shop.team_id."""
    if not team_id:
        return set()
    return {sk for sk, m in shop_meta.items() if str(m.get("team_id", "")).strip() == team_id}


def get_leader_manageable_shop_keys(
    leader_user: Dict[str, Any], shop_meta: Dict[str, Dict[str, str]], users: List[Dict[str, Any]]
) -> set:
    """Leader assigns shops inside team personnel scope (not raw shop.team_id tags)."""
    team_id = str(leader_user.get("team_id", "")).strip()
    if not team_id:
        return set()
    keys: set = set()
    team_usernames: set = set()
    for u in users:
        if str(u.get("status", "active")).strip() != "active":
            continue
        if str(u.get("team_id", "")).strip() != team_id:
            continue
        role = str(u.get("role", "staff")).strip()
        if role in {"admin", "superadmin", "manager", "accountant", "it"}:
            continue
        username = str(u.get("username", "")).strip().lower()
        if username:
            team_usernames.add(username)
        assigned = normalize_assigned_shops(u.get("assigned_shops", []))
        if "*" in assigned:
            continue
        keys |= {k for k in assigned if k in shop_meta}

    # Keep each team member's default personal shop visible (shop_name == username),
    # so leader can restore a staff member back to their original assigned shop.
    for sk, meta in shop_meta.items():
        shop_name = str(meta.get("shop_name", "")).strip().lower()
        if shop_name and shop_name in team_usernames:
            keys.add(sk)
    return keys


def normalize_assigned_shops(value: Any) -> List[str]:
    if isinstance(value, list):
        cleaned = [str(x).strip() for x in value if str(x).strip()]
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            cleaned = []
        elif raw == "*":
            cleaned = ["*"]
        else:
            cleaned = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        cleaned = []
    return cleaned or ["*"]


def get_allowed_shop_keys_for_current_user() -> Optional[set]:
    if is_admin_user() or is_accountant_user():
        return None
    user = current_user()
    if not user:
        return set()
    role = str(user.get("role", "staff")).strip()
    team_id = str(user.get("team_id", "")).strip()
    shop_meta = load_shop_meta_map()
    assigned = normalize_assigned_shops(user.get("assigned_shops", ["*"]))
    if role == "staff":
        # Nhân viên: chỉ shop được gán cụ thể; '*' không được quyền xem toàn hệ thống.
        if "*" in assigned:
            return set()
        return {k for k in assigned if k in shop_meta}
    if role == "leader":
        if team_id:
            return get_leader_manageable_shop_keys(user, shop_meta, load_users())
        if "*" in assigned:
            return set()
        return {k for k in assigned if k in shop_meta}
    if "*" in assigned:
        return set(shop_meta.keys())
    return {k for k in assigned if k in shop_meta}


def is_shop_allowed_for_current_user(shop_key: str) -> bool:
    allowed = get_allowed_shop_keys_for_current_user()
    if allowed is None:
        return True
    return shop_key in allowed


def get_visible_shop_meta_map() -> Dict[str, Dict[str, str]]:
    meta_map = load_shop_meta_map()
    allowed_shop_keys = get_allowed_shop_keys_for_current_user()
    if allowed_shop_keys is None:
        return meta_map
    return {k: v for k, v in meta_map.items() if k in allowed_shop_keys}


def get_shops_missing_team_id() -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for meta in load_shop_meta_map().values():
        if str(meta.get("team_id", "")).strip():
            continue
        result.append({
            "shop_key": str(meta.get("shop_key", "")).strip(),
            "shop_name": str(meta.get("shop_name", "")).strip() or str(meta.get("shop_key", "")).strip(),
        })
    result.sort(key=lambda x: x["shop_key"])
    return result


def load_shop_team_map_from_users() -> Dict[str, str]:
    users = load_users()
    result: Dict[str, str] = {}
    for u in users:
        team = str(u.get("team_id", "")).strip()
        if not team:
            continue
        for sk in u.get("assigned_shops", []):
            sk = str(sk).strip()
            if sk and sk != "*":
                result[sk] = team
    return result


def load_shop_user_team_map_for_range(date_from: str, date_to: str):
    """(shop_key → set(team_code), username → set(shop_key)) theo NGƯỜI GIỮ SHOP TRONG KỲ.

    Versioned usa (migration 058): shop chuyển chủ giữa 2 NV/team vẫn nằm đúng
    team của kỳ đang xem — vd Ken → Minh Đạt hiệu lực 7/6 thì lọc tháng 5
    shop vẫn thuộc team-ken với đầy đủ doanh thu cũ.
    """
    shop_team: Dict[str, set] = {}
    staff_shops: Dict[str, set] = {}
    with get_db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.shop_key, u.username, t.team_code
            FROM user_shop_assignments usa
            JOIN shops s ON s.id = usa.shop_id
            JOIN users u ON u.id = usa.user_id
            LEFT JOIN teams t ON t.id = u.team_id
            WHERE usa.assigned_from <= %s::date
              AND (usa.assigned_to IS NULL OR usa.assigned_to >= %s::date)
        """, (date_to, date_from))
        for shop_key, username, team_code in cur.fetchall():
            if not shop_key:
                continue
            if team_code:
                shop_team.setdefault(shop_key, set()).add(team_code)
            if username:
                staff_shops.setdefault(username, set()).add(shop_key)
    return shop_team, staff_shops


def load_team_list_for_filter() -> List[Dict[str, str]]:
    mapping = load_shop_team_map_from_users()
    codes = sorted(set(mapping.values()))
    return [{"code": c, "name": c} for c in codes]


def load_staff_list_for_filter() -> List[Dict[str, str]]:
    users = load_users()
    result: List[Dict[str, str]] = []
    for u in users:
        role = str(u.get("role", "")).strip()
        if role in ("admin", "superadmin", "manager", "accountant", "it"):
            continue
        username = str(u.get("username", "")).strip()
        if not username:
            continue
        shops = [str(s).strip() for s in u.get("assigned_shops", []) if str(s).strip() and str(s).strip() != "*"]
        if not shops:
            continue
        result.append({
            "username": username,
            "team_id": str(u.get("team_id", "")).strip(),
            "shops": ",".join(shops),
        })
    result.sort(key=lambda x: x["username"])
    return result


def load_daily_revenue_map_date_range_from_db(
    date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Dict[str, Dict[str, float]]:
    if not date_from or not date_to or not get_db_conn:
        return {}
    query = """
        SELECT
            s.shop_key,
            COALESCE(SUM(m.gross_revenue), 0),
            COALESCE(SUM(m.order_count), 0),
            COALESCE(SUM(m.ads_cost), 0),
            COALESCE(SUM(m.pos_profit_loss), 0),
            COALESCE(
                SUM(m.pos_avg_profit_per_order * m.order_count)
                / NULLIF(SUM(m.order_count), 0),
                0
            )
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE m.metric_date >= %s::date
          AND m.metric_date <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"

    result: Dict[str, Dict[str, float]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for row in cur.fetchall():
                    shop_key = str(row[0] or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = {
                        "revenue": float(row[1] or 0),
                        "orders": float(row[2] or 0),
                        "ads_cost": float(row[3] or 0),
                        "profit": float(row[4] or 0),
                        "avg_profit": float(row[5] or 0),
                    }
    except Exception:
        return {}
    return result


def load_order_status_summary_date_range_from_db(
    date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, int]]:
    if not date_from or not date_to or not get_db_conn:
        return None
    query = """
        SELECT o.order_status, COUNT(1)
        FROM orders o
        JOIN shops s ON s.id = o.shop_id
        WHERE DATE(o.created_at_pos) >= %s::date
          AND DATE(o.created_at_pos) <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {"new": 0, "confirmed": 0, "sent": 0, "received": 0, "returning": 0, "returned": 0, "cancelled": 0}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY o.order_status"

    totals = {"new": 0, "confirmed": 0, "sent": 0, "received": 0, "returning": 0, "returned": 0, "cancelled": 0}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for status, cnt in cur.fetchall():
                    c = int(cnt or 0)
                    s = str(status or "").strip()
                    if s == "new":
                        totals["new"] += c
                    elif s == "confirmed":
                        totals["confirmed"] += c
                    elif s == "shipping":
                        totals["sent"] += c
                    elif s == "delivered":
                        totals["received"] += c
                    elif s == "returned":
                        totals["returned"] += c
                    elif s == "returning":
                        totals["returning"] += c
                    elif s == "cancelled":
                        totals["cancelled"] += c
        return totals
    except Exception:
        return None


def is_db_daily_dashboard_enabled() -> bool:
    return str(os.getenv("WEB_DAILY_DASHBOARD_DB_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def is_db_order_status_enabled() -> bool:
    return str(os.getenv("WEB_DASHBOARD_ORDER_STATUS_DB_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def is_dashboard_lightweight_home_enabled() -> bool:
    return str(os.getenv("WEB_DASHBOARD_LIGHTWEIGHT_HOME_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _home_alert_counts_scope_key(allowed_shop_keys: Optional[set]) -> str:
    if allowed_shop_keys is None:
        return "*"
    return ",".join(sorted(str(x) for x in allowed_shop_keys))


def clear_home_carrier_pickup_cache() -> None:
    from redis_cache import cache_delete_pattern
    cache_delete_pattern("carrier_pickup|*")
    with HOME_CARRIER_PICKUP_CACHE_LOCK:
        HOME_CARRIER_PICKUP_CACHE.clear()


def get_home_alert_counts_cached(selected_month: str, allowed_shop_keys: Optional[set] = None) -> Dict[str, Any]:
    """Cache build_ads_delay_data + build_monthly_loss_data vào Redis (shared across workers).
    Fallback: in-process dict nếu Redis không có.
    """
    from redis_cache import cache_get, cache_set
    ttl_seconds = max(10, int(str(os.getenv("WEB_HOME_ALERT_CACHE_TTL_SECONDS", "600")).strip() or "600"))
    cache_key = f"home_alert|{selected_month}|{_home_alert_counts_scope_key(allowed_shop_keys)}"

    cached = cache_get(cache_key)
    if cached:
        return {
            "ads_delay_count": int(cached.get("ads_delay_count", 0) or 0),
            "monthly_loss_count": int(cached.get("monthly_loss_count", 0) or 0),
            "monthly_total_loss_fmt": str(cached.get("monthly_total_loss_fmt", "--")),
        }

    ads_delay_data = build_ads_delay_data(allowed_shop_keys=allowed_shop_keys)
    monthly_loss_data = build_monthly_loss_data(selected_month, allowed_shop_keys=allowed_shop_keys)
    result = {
        "ads_delay_count": len(ads_delay_data["affected_shops"]),
        "monthly_loss_count": len(monthly_loss_data["losing_shops"]),
        "monthly_total_loss_fmt": monthly_loss_data["monthly_total_loss_fmt"],
    }
    cache_set(cache_key, result, ttl=ttl_seconds)
    return result


def clear_home_alert_counts_cache() -> None:
    from redis_cache import cache_delete_pattern
    cache_delete_pattern("home_alert|*")
    # Backward compat: xóa luôn in-process cache cũ
    with HOME_ALERT_COUNTS_CACHE_LOCK:
        HOME_ALERT_COUNTS_CACHE.clear()


def load_daily_fb_ads_cost_map_from_db(selected_date: Optional[str], allowed_shop_keys: Optional[set] = None) -> Dict[str, float]:
    if not selected_date or not get_db_conn:
        return {}
    query = """
        SELECT s.shop_key, COALESCE(SUM(f.spend), 0)
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        JOIN fb_ad_account_mappings m
          ON m.shop_id = f.shop_id
         AND m.fb_ad_account_id = f.fb_ad_account_id
         AND m.status = 'active'
        WHERE f.metric_date = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [selected_date]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"
    result: Dict[str, float] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, spend_raw in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = float(spend_raw or 0)
    except Exception:
        return {}
    return result


def load_daily_fb_ads_cost_map_from_db_range(
    date_from: Optional[str], date_to: Optional[str], allowed_shop_keys: Optional[set] = None
) -> Dict[str, float]:
    if not date_from or not date_to or not get_db_conn:
        return {}
    query = """
        SELECT s.shop_key, COALESCE(SUM(f.spend), 0)
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        JOIN fb_ad_account_mappings m
          ON m.shop_id = f.shop_id
         AND m.fb_ad_account_id = f.fb_ad_account_id
         AND m.status = 'active'
        WHERE f.metric_date >= %s::date AND f.metric_date <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"
    result: Dict[str, float] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, spend_raw in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = float(spend_raw or 0)
    except Exception:
        return {}
    return result


def load_daily_shop_ads_pos_map_from_db_range(
    date_from: Optional[str], date_to: Optional[str], allowed_shop_keys: Optional[set] = None
) -> Dict[str, float]:
    if not date_from or not date_to or not get_db_conn:
        return {}
    query = """
        SELECT s.shop_key, COALESCE(SUM(m.ads_cost), 0)
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE m.metric_date >= %s::date AND m.metric_date <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"
    result: Dict[str, float] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, ads_raw in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = float(ads_raw or 0)
    except Exception:
        return {}
    return result


def list_fb_ad_mappings_for_settings() -> List[Dict[str, Any]]:
    if not get_db_conn or not repo_list_fb_ad_account_mappings:
        return []
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                return repo_list_fb_ad_account_mappings(cur)
    except Exception:
        return []


def load_fb_ad_mapping_map_for_dashboard(allowed_shop_keys: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    if not get_db_conn or not repo_list_fb_ad_account_mappings:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                rows = repo_list_fb_ad_account_mappings(cur)
                for item in rows:
                    shop_key = str(item.get("shop_key", "")).strip()
                    if not shop_key:
                        continue
                    if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
                        continue
                    bucket = result.setdefault(
                        shop_key,
                        {
                            "fb_ad_account_count": 0,
                            "fb_mapping_status": "inactive",
                        },
                    )
                    bucket["fb_ad_account_count"] = int(bucket.get("fb_ad_account_count", 0) or 0) + 1
                    if str(item.get("status", "")).strip() == "active":
                        bucket["fb_mapping_status"] = "active"
    except Exception:
        return {}
    return result


def get_fb_ads_account_breakdown_for_shop_date(
    shop_key: str, selected_date: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Any]]:
    if not get_db_conn or not shop_key or not selected_date:
        return None
    if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
        return None
    query_full = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks,
            f.message_count AS message_count,
            f.purchase_count AS purchase_count,
            f.purchase_cpa AS purchase_cpa
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks,
                SUM(message_count) AS message_count,
                SUM(purchase_count) AS purchase_count,
                MAX(purchase_cpa) AS purchase_cpa
            FROM fb_ads_daily_metrics
            WHERE metric_date = %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    query_with_messages = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks,
            f.message_count AS message_count
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks,
                SUM(message_count) AS message_count
            FROM fb_ads_daily_metrics
            WHERE metric_date = %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    query_legacy = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks
            FROM fb_ads_daily_metrics
            WHERE metric_date = %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    rows: Optional[List[Any]] = None
    variant = "legacy"
    for q, v in (
        (query_full, "full"),
        (query_with_messages, "msg"),
        (query_legacy, "legacy"),
    ):
        try:
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(q, (selected_date, shop_key))
                    rows = cur.fetchall()
            variant = v
            break
        except Exception as exc:
            text = str(exc).lower()
            # query_full thiếu cột (migrate chưa đủ) → thử msg → legacy.
            if v == "full" and "does not exist" in text and (
                "column" in text
                or "message_count" in text
                or "purchase_count" in text
                or "purchase_cpa" in text
            ):
                continue
            if v == "msg" and "message_count" in text and "does not exist" in text:
                continue
            return None
    try:
        if not rows:
            shop_name = shop_key
            try:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT shop_name FROM shops WHERE shop_key = %s AND status = 'active' LIMIT 1",
                            (shop_key,),
                        )
                        one = cur.fetchone()
                        if one:
                            shop_name = str(one[0] or shop_key)
            except Exception:
                pass
            return {
                "shop_key": shop_key,
                "shop_name": shop_name,
                "account_count": 0,
                "total_spend": 0.0,
                "total_spend_fmt": format_money(0),
                "accounts": [],
            }
        shop_name = str(rows[0][0] or shop_key)
        accounts: List[Dict[str, Any]] = []
        total_spend = 0.0
        for row in rows:
            message_count = None
            purchase_count = None
            purchase_cpa = None
            if variant == "full":
                _, fb_ad_account_id, account_name, spend, impressions, clicks, message_count, purchase_count, purchase_cpa = row
            elif variant == "msg":
                _, fb_ad_account_id, account_name, spend, impressions, clicks, message_count = row
            else:
                _, fb_ad_account_id, account_name, spend, impressions, clicks = row
            spend_value = float(spend or 0)
            total_spend += spend_value
            clicks_i = int(clicks or 0)
            imps_i = int(impressions or 0)
            if message_count is None:
                messages_fmt = "-"
                cost_per_message_fmt = "-"
            else:
                mc_int = int(message_count or 0)
                messages_fmt = format_int(mc_int)
                if mc_int > 0:
                    cost_per_message_fmt = format_money(spend_value / mc_int)
                else:
                    cost_per_message_fmt = "-"
            if purchase_count is None:
                purchases_fmt = "-"
                cost_per_purchase_fmt = "-"
            else:
                pur_i = int(purchase_count or 0)
                purchases_fmt = format_int(pur_i)
                if pur_i <= 0:
                    cost_per_purchase_fmt = "-"
                else:
                    pcpa_f = float(purchase_cpa) if purchase_cpa is not None else 0.0
                    if pcpa_f > 0:
                        cost_per_purchase_fmt = format_money(pcpa_f)
                    else:
                        cost_per_purchase_fmt = format_money(spend_value / pur_i)
            cpc_fmt = format_money(spend_value / clicks_i) if clicks_i > 0 else "-"
            cpm_fmt = format_money((spend_value / imps_i) * 1000.0) if imps_i > 0 else "-"
            accounts.append({
                "fb_ad_account_id": str(fb_ad_account_id or "").strip() or "-",
                "account_name": str(account_name or "").strip() or "-",
                "spend": spend_value,
                "spend_fmt": format_money(spend_value),
                "impressions_fmt": format_int(impressions or 0),
                "clicks_fmt": format_int(clicks or 0),
                "messages_fmt": messages_fmt,
                "cost_per_message_fmt": cost_per_message_fmt,
                "purchases_fmt": purchases_fmt,
                "cost_per_purchase_fmt": cost_per_purchase_fmt,
                "cpc_fmt": cpc_fmt,
                "cpm_fmt": cpm_fmt,
            })
        return {
            "shop_key": shop_key,
            "shop_name": shop_name,
            "account_count": len(accounts),
            "total_spend": total_spend,
            "total_spend_fmt": format_money(total_spend),
            "accounts": accounts,
        }
    except Exception:
        return None


def get_fb_ads_account_breakdown_for_shop_range(
    shop_key: str, date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Any]]:
    if not get_db_conn or not shop_key or not date_from or not date_to:
        return None
    if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
        return None
    query_full = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks,
            f.message_count AS message_count,
            f.purchase_count AS purchase_count,
            f.purchase_cpa AS purchase_cpa
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks,
                SUM(message_count) AS message_count,
                SUM(purchase_count) AS purchase_count,
                MAX(purchase_cpa) AS purchase_cpa
            FROM fb_ads_daily_metrics
            WHERE metric_date >= %s::date AND metric_date <= %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    query_with_messages = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks,
            f.message_count AS message_count
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks,
                SUM(message_count) AS message_count
            FROM fb_ads_daily_metrics
            WHERE metric_date >= %s::date AND metric_date <= %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    query_legacy = """
        SELECT
            s.shop_name,
            m.fb_ad_account_id,
            COALESCE(NULLIF(m.account_name, ''), '') AS account_name,
            COALESCE(f.spend, 0) AS spend,
            COALESCE(f.impressions, 0) AS impressions,
            COALESCE(f.clicks, 0) AS clicks
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        LEFT JOIN (
            SELECT
                shop_id,
                fb_ad_account_id,
                COALESCE(SUM(spend), 0) AS spend,
                COALESCE(SUM(impressions), 0) AS impressions,
                COALESCE(SUM(clicks), 0) AS clicks
            FROM fb_ads_daily_metrics
            WHERE metric_date >= %s::date AND metric_date <= %s::date
            GROUP BY shop_id, fb_ad_account_id
        ) f
          ON f.shop_id = m.shop_id
         AND f.fb_ad_account_id = m.fb_ad_account_id
        WHERE s.shop_key = %s
          AND s.status = 'active'
          AND m.status = 'active'
        ORDER BY m.fb_ad_account_id
    """
    rows: Optional[List[Any]] = None
    variant = "legacy"
    for q, v in (
        (query_full, "full"),
        (query_with_messages, "msg"),
        (query_legacy, "legacy"),
    ):
        try:
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(q, (date_from, date_to, shop_key))
                    rows = cur.fetchall()
            variant = v
            break
        except Exception as exc:
            text = str(exc).lower()
            if v == "full" and "does not exist" in text and (
                "column" in text
                or "message_count" in text
                or "purchase_count" in text
                or "purchase_cpa" in text
            ):
                continue
            if v == "msg" and "message_count" in text and "does not exist" in text:
                continue
            return None
    try:
        if not rows:
            shop_name = shop_key
            try:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT shop_name FROM shops WHERE shop_key = %s AND status = 'active' LIMIT 1",
                            (shop_key,),
                        )
                        one = cur.fetchone()
                        if one:
                            shop_name = str(one[0] or shop_key)
            except Exception:
                pass
            return {
                "shop_key": shop_key,
                "shop_name": shop_name,
                "account_count": 0,
                "total_spend": 0.0,
                "total_spend_fmt": format_money(0),
                "accounts": [],
            }
        shop_name = str(rows[0][0] or shop_key)
        accounts: List[Dict[str, Any]] = []
        total_spend = 0.0
        for row in rows:
            message_count = None
            purchase_count = None
            purchase_cpa = None
            if variant == "full":
                _, fb_ad_account_id, account_name, spend, impressions, clicks, message_count, purchase_count, purchase_cpa = row
            elif variant == "msg":
                _, fb_ad_account_id, account_name, spend, impressions, clicks, message_count = row
            else:
                _, fb_ad_account_id, account_name, spend, impressions, clicks = row
            spend_value = float(spend or 0)
            total_spend += spend_value
            clicks_i = int(clicks or 0)
            imps_i = int(impressions or 0)
            if message_count is None:
                messages_fmt = "-"
                cost_per_message_fmt = "-"
            else:
                mc_int = int(message_count or 0)
                messages_fmt = format_int(mc_int)
                if mc_int > 0:
                    cost_per_message_fmt = format_money(spend_value / mc_int)
                else:
                    cost_per_message_fmt = "-"
            if purchase_count is None:
                purchases_fmt = "-"
                cost_per_purchase_fmt = "-"
            else:
                pur_i = int(purchase_count or 0)
                purchases_fmt = format_int(pur_i)
                if pur_i <= 0:
                    cost_per_purchase_fmt = "-"
                else:
                    pcpa_f = float(purchase_cpa) if purchase_cpa is not None else 0.0
                    if pcpa_f > 0:
                        cost_per_purchase_fmt = format_money(pcpa_f)
                    else:
                        cost_per_purchase_fmt = format_money(spend_value / pur_i)
            cpc_fmt = format_money(spend_value / clicks_i) if clicks_i > 0 else "-"
            cpm_fmt = format_money((spend_value / imps_i) * 1000.0) if imps_i > 0 else "-"
            accounts.append({
                "fb_ad_account_id": str(fb_ad_account_id or "").strip() or "-",
                "account_name": str(account_name or "").strip() or "-",
                "spend": spend_value,
                "spend_fmt": format_money(spend_value),
                "impressions_fmt": format_int(impressions or 0),
                "clicks_fmt": format_int(clicks or 0),
                "messages_fmt": messages_fmt,
                "cost_per_message_fmt": cost_per_message_fmt,
                "purchases_fmt": purchases_fmt,
                "cost_per_purchase_fmt": cost_per_purchase_fmt,
                "cpc_fmt": cpc_fmt,
                "cpm_fmt": cpm_fmt,
            })
        return {
            "shop_key": shop_key,
            "shop_name": shop_name,
            "account_count": len(accounts),
            "total_spend": total_spend,
            "total_spend_fmt": format_money(total_spend),
            "accounts": accounts,
        }
    except Exception:
        return None


def save_fb_ad_mapping(shop_key: str, fb_ad_account_id: str, account_name: str, status: str) -> Tuple[bool, str]:
    if not get_db_conn or not repo_get_shop_id_by_key or not repo_upsert_fb_ad_account_mapping:
        return False, "DB Facebook Ads chưa sẵn sàng."
    normalized_status = "active" if status == "active" else "inactive"
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                shop_id = repo_get_shop_id_by_key(cur, shop_key)
                if not shop_id:
                    return False, "Không tìm thấy shop để gán tài khoản Facebook Ads."
                repo_upsert_fb_ad_account_mapping(cur, shop_id, fb_ad_account_id.strip(), account_name.strip(), normalized_status)
        return True, "Đã lưu mapping Facebook Ads."
    except Exception as exc:
        return False, f"Lưu mapping Facebook Ads thất bại: {exc}"


def toggle_fb_ad_mapping(mapping_id: str) -> Tuple[bool, str]:
    if not get_db_conn or not repo_toggle_fb_ad_account_mapping_status:
        return False, "DB Facebook Ads chưa sẵn sàng."
    try:
        mapping_int = int(str(mapping_id).strip())
    except Exception:
        return False, "mapping_id không hợp lệ."
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                repo_toggle_fb_ad_account_mapping_status(cur, mapping_int)
        return True, "Đã đổi trạng thái mapping Facebook Ads."
    except Exception as exc:
        return False, f"Đổi trạng thái mapping thất bại: {exc}"


def delete_fb_ad_mapping(mapping_id: str) -> Tuple[bool, str]:
    if not get_db_conn or not repo_delete_fb_ad_account_mapping:
        return False, "DB Facebook Ads chưa sẵn sàng."
    try:
        mapping_int = int(str(mapping_id).strip())
    except Exception:
        return False, "mapping_id không hợp lệ."
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                repo_delete_fb_ad_account_mapping(cur, mapping_int)
        return True, "Đã xoá mapping Facebook Ads."
    except Exception as exc:
        return False, f"Xoá mapping Facebook Ads thất bại: {exc}"


def get_facebook_access_token() -> str:
    token = str(os.getenv("FACEBOOK_ACCESS_TOKEN", "")).strip()
    if token:
        return token
    config = load_config()
    return str(config.get("facebook_access_token", "")).strip()


def save_facebook_access_token(token: str) -> Tuple[bool, str]:
    token = str(token or "").strip()
    if not token:
        return False, "Facebook access token không được để trống."
    save_config_key("facebook_access_token", token)
    return True, "Đã lưu Facebook access token."


def mask_secret(value: str, keep_start: int = 8, keep_end: int = 4) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep_start + keep_end:
        return "*" * len(raw)
    return raw[:keep_start] + ("*" * (len(raw) - keep_start - keep_end)) + raw[-keep_end:]


def _fb_ads_tokens_store_path() -> Path:
    from facebook_ads_tokens.resolve import fb_ads_tokens_store_path

    return fb_ads_tokens_store_path()


def fb_token_store_has_usable_row() -> bool:
    """True if JSON store exists and has at least one non-empty access_token."""
    try:
        from facebook_ads_tokens.storage import load_store

        path = _fb_ads_tokens_store_path()
        for rec in load_store(path):
            if str(rec.access_token or "").strip():
                return True
    except Exception:
        pass
    return False


def load_fb_token_rows_for_settings() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Build table rows for settings UI; on error return ([], error_message). Never raises."""
    try:
        from facebook_ads_tokens.health import get_token_health_status
        from facebook_ads_tokens.security import mask_token
        from facebook_ads_tokens.storage import load_store

        path = _fb_ads_tokens_store_path()
        records = load_store(path)
        rows: List[Dict[str, Any]] = []
        for r in records:
            rows.append(
                {
                    "shop_key": r.shop_key,
                    "facebook_user_id": r.facebook_user_id,
                    "ad_account_id": r.ad_account_id,
                    "token_masked": mask_token(r.access_token),
                    "token_type": r.token_type,
                    "expires_at": r.expires_at,
                    "last_refresh_at": r.last_refresh_at,
                    "refresh_status": r.refresh_status,
                    "note": r.note,
                    "health": get_token_health_status(r, expiring_within_days=7),
                }
            )
        return rows, None
    except Exception as exc:
        logging.exception("load_fb_token_rows_for_settings")
        return [], str(exc)


def run_facebook_ads_sync(selected_date: str, shop_key: str = "") -> Tuple[bool, str]:
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except Exception:
        return False, "Ngày sync Facebook Ads không hợp lệ."
    token = get_facebook_access_token()
    if not token and not fb_token_store_has_usable_row():
        return False, "Thiếu token sync Facebook Ads: cần FACEBOOK_ACCESS_TOKEN (hoặc config) hoặc ít nhất một token trong kho JSON (Cài đặt → FB token kho)."

    script_path = os.path.join(BASE_DIR, "scripts", "sync_facebook_ads_to_db.py")
    cmd = [sys.executable, script_path, "--date", selected_date]
    if shop_key.strip():
        cmd.extend(["--shop-key", shop_key.strip()])
    env = os.environ.copy()
    env["FACEBOOK_ACCESS_TOKEN"] = token
    try:
        completed = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            check=True,
            timeout=300,
            env=env,
            capture_output=True,
            text=True,
        )
        clear_home_alert_counts_cache()
        summary_lines = [line.strip() for line in (completed.stdout or "").strip().splitlines() if line.strip()]
        summary_line = next((line for line in reversed(summary_lines) if line.startswith("SUMMARY ")), "")
        if summary_line:
            failed_match = re.search(r"failed=(\d+)", summary_line)
            if failed_match and int(failed_match.group(1)) > 0:
                return False, summary_line
            _trigger_page_spend_sync_bg(selected_date)
            return True, summary_line
        tail = summary_lines[-1] if summary_lines else "Facebook Ads sync completed."
        _trigger_page_spend_sync_bg(selected_date)
        return True, tail
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.stdout or "").strip()
        tail = stderr.splitlines()[-1] if stderr else str(exc)
        return False, f"Sync Facebook Ads thất bại: {tail}"
    except Exception as exc:
        return False, f"Sync Facebook Ads thất bại: {exc}"


_PAGE_SPEND_SYNC_RUNNING: set = set()   # dates đang có sync đang chạy
_PAGE_SPEND_SYNC_LOCK = threading.Lock()
_PAGE_SPEND_SYNC_LAST: dict = {}        # date → timestamp lần chạy cuối
_PAGE_SPEND_SYNC_COOLDOWN = 300         # 5 phút cooldown giữa 2 lần sync cùng date

def _trigger_page_spend_sync_bg(target_date: str, ad_account_id: str = "") -> None:
    """Run the per-page spend sync script in a background daemon thread.

    Called automatically after account-level FB Ads sync completes, or when
    a new token is mapped, so page/post spend data stays in sync without
    any manual action.

    FIX: stdout/stderr redirect to DEVNULL (không buffer vào RAM worker).
         Cooldown 5 phút để tránh spawn nhiều thread cho cùng 1 ngày.
    """
    script_path = os.path.join(BASE_DIR, "scripts", "sync_fb_ads_by_page.py")
    if not os.path.exists(script_path):
        return

    import time as _t
    cache_key = f"{target_date}|{ad_account_id}"
    with _PAGE_SPEND_SYNC_LOCK:
        # Đã có sync đang chạy cho ngày này → bỏ qua
        if cache_key in _PAGE_SPEND_SYNC_RUNNING:
            return
        # Cooldown: chạy không quá 1 lần / 5 phút mỗi (date+account)
        last = _PAGE_SPEND_SYNC_LAST.get(cache_key, 0)
        if _t.time() - last < _PAGE_SPEND_SYNC_COOLDOWN:
            return
        _PAGE_SPEND_SYNC_RUNNING.add(cache_key)
        _PAGE_SPEND_SYNC_LAST[cache_key] = _t.time()

    def _run():
        try:
            cmd = [sys.executable, script_path, "--date", target_date]
            if ad_account_id:
                cmd.extend(["--ad-account-id", ad_account_id])
            subprocess.run(
                cmd,
                cwd=BASE_DIR,
                timeout=300,
                stdout=subprocess.DEVNULL,   # KHÔNG buffer vào RAM
                stderr=subprocess.DEVNULL,   # KHÔNG buffer vào RAM
                env=os.environ,              # dùng env gốc, không copy
            )
        except Exception:
            pass  # best-effort; errors logged inside the script
        finally:
            with _PAGE_SPEND_SYNC_LOCK:
                _PAGE_SPEND_SYNC_RUNNING.discard(cache_key)

    threading.Thread(target=_run, daemon=True, name=f"page-sync-{target_date}").start()


def get_fb_ads_spend_for_shop_date(shop_key: str, selected_date: str) -> Optional[float]:
    if not get_db_conn or not shop_key or not selected_date:
        return None
    query = """
        SELECT COALESCE(SUM(f.spend), 0)
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        WHERE s.shop_key = %s
          AND f.metric_date = %s::date
    """
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (shop_key, selected_date))
                row = cur.fetchone()
                return float(row[0] or 0) if row else 0.0
    except Exception:
        return None


def get_fb_ads_sync_meta_for_date(selected_date: str, allowed_shop_keys: Optional[set] = None) -> Dict[str, Any]:
    if not get_db_conn or not selected_date:
        return {"last_sync_text": "-", "summary_text": "Chưa có dữ liệu sync."}
    query = """
        SELECT
            COALESCE(COUNT(1), 0),
            COALESCE(COUNT(DISTINCT s.shop_key), 0),
            COALESCE(SUM(CASE WHEN COALESCE(f.spend, 0) = 0 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(f.spend), 0),
            MAX(f.updated_at)
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        JOIN fb_ad_account_mappings m
          ON m.shop_id = f.shop_id
         AND m.fb_ad_account_id = f.fb_ad_account_id
         AND m.status = 'active'
        WHERE f.metric_date = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [selected_date]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {"last_sync_text": "-", "summary_text": "Không có shop trong phạm vi hiện tại."}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
                row_count = int((row or [0])[0] or 0)
                shop_count = int((row or [0, 0])[1] or 0)
                zero_spend_count = int((row or [0, 0, 0])[2] or 0)
                spend_sum = float((row or [0, 0, 0, 0])[3] or 0)
                updated_at = (row or [None, None, None, None, None])[4]
                last_sync_text = fmt_hcm(updated_at, "%d/%m/%Y %H:%M:%S") if updated_at else "-"
                summary_text = (
                    f"Accounts: {row_count} | Shops: {shop_count} | "
                    f"Tổng CP Ads FB: {format_money(spend_sum)} | Zero spend: {zero_spend_count}"
                )
                return {"last_sync_text": last_sync_text, "summary_text": summary_text}
    except Exception:
        return {"last_sync_text": "-", "summary_text": "Không đọc được trạng thái sync Facebook Ads."}


def get_fb_ads_sync_meta_for_range(
    date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Dict[str, Any]:
    if not get_db_conn or not date_from or not date_to:
        return {"last_sync_text": "-", "summary_text": "Chưa có dữ liệu sync."}
    query = """
        SELECT
            COALESCE(COUNT(1), 0),
            COALESCE(COUNT(DISTINCT s.shop_key), 0),
            COALESCE(SUM(CASE WHEN COALESCE(f.spend, 0) = 0 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(f.spend), 0),
            MAX(f.updated_at)
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        JOIN fb_ad_account_mappings m
          ON m.shop_id = f.shop_id
         AND m.fb_ad_account_id = f.fb_ad_account_id
         AND m.status = 'active'
        WHERE f.metric_date >= %s::date AND f.metric_date <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {"last_sync_text": "-", "summary_text": "Không có shop trong phạm vi hiện tại."}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
                row_count = int((row or [0])[0] or 0)
                shop_count = int((row or [0, 0])[1] or 0)
                zero_spend_count = int((row or [0, 0, 0])[2] or 0)
                spend_sum = float((row or [0, 0, 0, 0])[3] or 0)
                updated_at = (row or [None, None, None, None, None])[4]
                last_sync_text = fmt_hcm(updated_at, "%d/%m/%Y %H:%M:%S") if updated_at else "-"
                d0 = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d/%m/%Y")
                d1 = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d/%m/%Y")
                summary_text = (
                    f"Khoảng {d0} – {d1} | Accounts (dòng ngày): {row_count} | Shops: {shop_count} | "
                    f"Tổng CP Ads FB: {format_money(spend_sum)} | Zero spend: {zero_spend_count}"
                )
                return {"last_sync_text": last_sync_text, "summary_text": summary_text}
    except Exception:
        return {"last_sync_text": "-", "summary_text": "Không đọc được trạng thái sync Facebook Ads (khoảng ngày)."}


def _build_shop_employee_mapping_for_ads() -> Dict[str, Dict[str, str]]:
    shop_meta = load_shop_meta_map()
    users = load_users()
    shop_to_users: Dict[str, List[Dict[str, str]]] = {}
    for user in users:
        if str(user.get("status", "active")).strip() != "active":
            continue
        role = str(user.get("role", "staff")).strip()
        if role in {"admin", "accountant"}:
            continue
        username = str(user.get("username", "")).strip()
        team_id = str(user.get("team_id", "")).strip()
        if not username:
            continue
        assigned = normalize_assigned_shops(user.get("assigned_shops", ["*"]))
        if "*" in assigned:
            continue
        for shop_key in assigned:
            if shop_key not in shop_meta:
                continue
            shop_to_users.setdefault(shop_key, []).append({"username": username, "team_id": team_id})

    result: Dict[str, Dict[str, str]] = {}
    for shop_key, meta in shop_meta.items():
        linked = shop_to_users.get(shop_key, [])
        shop_team = str(meta.get("team_id", "")).strip()
        if len(linked) == 1:
            result[shop_key] = {
                "employee": linked[0]["username"],
                "team": linked[0]["team_id"] or shop_team or "-",
                "note": "",
            }
        else:
            result[shop_key] = {
                "employee": "UNMAPPED_OR_MULTI",
                "team": shop_team or "-",
                "note": "Shop không có hoặc có nhiều employee được gán; tiền giữ 1 dòng để tránh double count.",
            }
    return result


def _query_ads_export_base_rows(
    date_from: str,
    date_to: str,
    shop_filter_key: str = "",
    allowed_shop_keys: Optional[set] = None,
) -> List[Dict[str, Any]]:
    if not get_db_conn:
        return []
    query = """
        WITH fb AS (
            SELECT
                f.shop_id,
                f.metric_date,
                COALESCE(SUM(spend), 0) AS fb_spend,
                STRING_AGG(DISTINCT COALESCE(f.fb_ad_account_id, ''), ', ' ORDER BY COALESCE(f.fb_ad_account_id, '')) AS fb_account_ids,
                STRING_AGG(
                    DISTINCT COALESCE(NULLIF(m.account_name, ''), NULLIF(f.account_name, ''), f.fb_ad_account_id, ''),
                    ', '
                    ORDER BY COALESCE(NULLIF(m.account_name, ''), NULLIF(f.account_name, ''), f.fb_ad_account_id, '')
                ) AS fb_account_names
            FROM fb_ads_daily_metrics f
            JOIN fb_ad_account_mappings m
              ON m.shop_id = f.shop_id
             AND m.fb_ad_account_id = f.fb_ad_account_id
             AND m.status = 'active'
            WHERE f.metric_date BETWEEN %s::date AND %s::date
            GROUP BY f.shop_id, f.metric_date
        ),
        pos AS (
            SELECT shop_id, metric_date, COALESCE(SUM(ads_cost), 0) AS pos_ads_cost
            FROM daily_shop_metrics
            WHERE metric_date BETWEEN %s::date AND %s::date
            GROUP BY shop_id, metric_date
        ),
        d AS (
            SELECT shop_id, metric_date FROM fb
            UNION
            SELECT shop_id, metric_date FROM pos
        )
        SELECT
            s.shop_key,
            s.shop_name,
            COALESCE(s.team_id::text, '') AS team_id,
            d.metric_date,
            COALESCE(fb.fb_spend, 0) AS cp_ads_fb,
            COALESCE(pos.pos_ads_cost, 0) AS cp_ads_pos,
            COALESCE(fb.fb_account_ids, '') AS fb_account_ids,
            COALESCE(fb.fb_account_names, '') AS fb_account_names
        FROM d
        JOIN shops s ON s.id = d.shop_id
        LEFT JOIN fb ON fb.shop_id = d.shop_id AND fb.metric_date = d.metric_date
        LEFT JOIN pos ON pos.shop_id = d.shop_id AND pos.metric_date = d.metric_date
        WHERE s.status = 'active'
    """
    params: List[Any] = [date_from, date_to, date_from, date_to]
    if shop_filter_key:
        query += " AND s.shop_key = %s"
        params.append(shop_filter_key)
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return []
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " ORDER BY d.metric_date, s.shop_key"
    rows: List[Dict[str, Any]] = []
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key, shop_name, team_id, metric_date, cp_ads_fb, cp_ads_pos, fb_account_ids, fb_account_names in cur.fetchall():
                    fb_value = float(cp_ads_fb or 0)
                    vat_value = fb_value * 0.113
                    fb_vat_value = fb_value * 1.113
                    pos_value = float(cp_ads_pos or 0)
                    rows.append({
                        "shop_key": str(shop_key or "").strip(),
                        "shop_name": str(shop_name or "").strip(),
                        "team": str(team_id or "").strip() or "-",
                        "date": str(metric_date),
                        "cp_ads_fb": fb_value,
                        "vat_amount": vat_value,
                        "cp_ads_fb_vat": fb_vat_value,
                        "cp_ads_pos": pos_value,
                        # CP POS và CP FB cùng mô tả MỘT khoản spend (2 cột đối chiếu) —
                        # cộng chồng = ~2× (audit 12/06). Tổng = FB đã VAT (nguồn API chuẩn),
                        # fallback POS khi FB chưa kéo được TK đó.
                        "total_ads_cost": fb_vat_value if fb_value > 0 else pos_value,
                        "fb_account_ids": str(fb_account_ids or "").strip() or "-",
                        "fb_account_names": str(fb_account_names or "").strip() or "-",
                    })
    except Exception:
        return []
    return rows


def build_ads_export_rows(
    date_from: str,
    date_to: str,
    mode: str,
    group_by: str,
    shop_filter_key: str = "",
    employee_filter: str = "",
    allowed_shop_keys: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    base_rows = _query_ads_export_base_rows(
        date_from=date_from,
        date_to=date_to,
        shop_filter_key=shop_filter_key,
        allowed_shop_keys=allowed_shop_keys,
    )
    shop_employee_map = _build_shop_employee_mapping_for_ads()
    warnings: List[str] = []
    detail_rows: List[Dict[str, Any]] = []
    for row in base_rows:
        shop_key = row["shop_key"]
        mapped = shop_employee_map.get(shop_key, {"employee": "UNMAPPED_OR_MULTI", "team": row["team"], "note": ""})
        out = {
            "employee": mapped.get("employee", "UNMAPPED_OR_MULTI"),
            "team": mapped.get("team", row["team"] or "-"),
            "shop": row["shop_name"] or shop_key,
            "date": row["date"],
            "cp_ads_fb": row["cp_ads_fb"],
            "vat_amount": row["vat_amount"],
            "cp_ads_fb_vat": row["cp_ads_fb_vat"],
            "cp_ads_pos": row["cp_ads_pos"],
            "total_ads_cost": row["total_ads_cost"],
            "fb_account_ids": row.get("fb_account_ids", "-"),
            "fb_account_names": row.get("fb_account_names", "-"),
            "note": mapped.get("note", ""),
        }
        if out["employee"] == "UNMAPPED_OR_MULTI" and out["note"]:
            warnings.append(f"{shop_key}: {out['note']}")
        if employee_filter and out["employee"] != employee_filter:
            continue
        detail_rows.append(out)

    if mode != "summary":
        return detail_rows, sorted(set(warnings))

    safe_group = group_by if group_by in {"employee", "team"} else "employee"
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in detail_rows:
        key = str(row.get(safe_group, "-"))
        item = grouped.setdefault(
            key,
            {
                "employee": row["employee"] if safe_group == "employee" else "-",
                "team": row["team"] if safe_group == "team" else "-",
                "shop": "-",
                "date": f"{date_from}..{date_to}",
                "cp_ads_fb": 0.0,
                "vat_amount": 0.0,
                "cp_ads_fb_vat": 0.0,
                "cp_ads_pos": 0.0,
                "total_ads_cost": 0.0,
                "fb_account_ids": "-",
                "fb_account_names": "-",
                "note": "",
            },
        )
        item["cp_ads_fb"] += float(row["cp_ads_fb"])
        item["vat_amount"] += float(row["vat_amount"])
        item["cp_ads_fb_vat"] += float(row["cp_ads_fb_vat"])
        item["cp_ads_pos"] += float(row["cp_ads_pos"])
        item["total_ads_cost"] += float(row["total_ads_cost"])

    return list(grouped.values()), sorted(set(warnings))


def save_ads_export_excel(
    date_from: str,
    date_to: str,
    mode: str,
    group_by: str,
    shop_filter_key: str,
    employee_filter: str,
    allowed_shop_keys: Optional[set] = None,
) -> Tuple[str, str, int, List[str]]:
    rows, warnings = build_ads_export_rows(
        date_from=date_from,
        date_to=date_to,
        mode=mode,
        group_by=group_by,
        shop_filter_key=shop_filter_key,
        employee_filter=employee_filter,
        allowed_shop_keys=allowed_shop_keys,
    )
    exports_dir = os.path.join(BASE_DIR, "exports")
    os.makedirs(exports_dir, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Ads Report"
    ws.append([
        "Employee",
        "Team",
        "Shop",
        "Date",
        "FB Ad Account IDs",
        "FB Ad Account Names",
        "CP Ads FB",
        "VAT (11.3%)",
        "CP Ads FB da VAT",
        "CP Ads POS",
        "Total Ads Cost",
    ])
    for row in rows:
        ws.append([
            row["employee"],
            row["team"],
            row["shop"],
            row["date"],
            row.get("fb_account_ids", "-"),
            row.get("fb_account_names", "-"),
            float(row["cp_ads_fb"]),
            float(row["vat_amount"]),
            float(row["cp_ads_fb_vat"]),
            float(row["cp_ads_pos"]),
            float(row["total_ads_cost"]),
        ])

    # Accounting number format for money columns (G..K)
    money_columns = [7, 8, 9, 10, 11]
    for r in range(2, ws.max_row + 1):
        for c in money_columns:
            ws.cell(row=r, column=c).number_format = '#,##0.00'

    stamp = now_hcm().strftime("%Y%m%d_%H%M%S")
    filename = f"ads_report_{mode}_{group_by}_{date_from}_{date_to}_{stamp}.xlsx"
    filepath = os.path.join(exports_dir, filename)
    wb.save(filepath)
    return filepath, filename, len(rows), warnings


def load_daily_orders_revenue_map_from_db(selected_date: Optional[str], allowed_shop_keys: Optional[set] = None) -> Dict[str, Dict[str, float]]:
    if not selected_date:
        return {}
    if not get_db_conn:
        return {}
    query = """
        SELECT
            s.shop_key,
            COALESCE(SUM(m.gross_revenue), 0),
            COALESCE(SUM(m.order_count), 0),
            COALESCE(SUM(m.ads_cost), 0),
            COALESCE(SUM(m.pos_profit_loss), 0),
            COALESCE(
                SUM(m.pos_avg_profit_per_order * m.order_count)
                / NULLIF(SUM(m.order_count), 0),
                0
            ),
            COALESCE(SUM(m.total_order_count), 0)
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE m.metric_date = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [selected_date]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"

    result: Dict[str, Dict[str, float]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for row in cur.fetchall():
                    shop_key = str(row[0] or "").strip()
                    if not shop_key:
                        continue
                    revenue = float(row[1] or 0)
                    orders = float(row[2] or 0)
                    ads_cost = float(row[3] or 0)
                    profit = float(row[4] or 0)
                    avg_profit = float(row[5] or 0)
                    total_order_count = int(row[6] or 0)
                    result[shop_key] = {
                        "revenue": revenue,
                        "orders": orders,
                        "ads_cost": ads_cost,
                        "profit": profit,
                        "avg_profit": avg_profit,
                        "total_order_count": total_order_count,
                    }
    except Exception:
        return {}
    return result


def refresh_daily_metrics_from_orders_for_date(target_date_str: str) -> Tuple[bool, str]:
    """DEPRECATED and disabled.

    Previously aggregated orders into daily_shop_metrics with ads_cost / pos_profit_loss forced to 0,
    which violates POS-origin truth. daily_shop_metrics must be filled from POS analytics
    (e.g. sync_pos JSON + bootstrap_daily_shop_metrics_from_json), not from this path.
    """
    if not target_date_str:
        return False, "target date is empty"
    return (
        False,
        "refresh_daily_metrics_from_orders_for_date is disabled: "
        "use POS analytics bootstrap for daily_shop_metrics (not order aggregation).",
    )


def should_auto_sync_missing_order_status_date(
    target_date_str: str, allowed_shop_keys: Optional[set] = None
) -> bool:
    if not target_date_str or not get_db_conn:
        return False
    query_daily = """
        SELECT COALESCE(SUM(m.order_count), 0)
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE m.metric_date = %s::date
          AND s.status = 'active'
    """
    query_orders = """
        SELECT COALESCE(COUNT(1), 0)
        FROM orders o
        JOIN shops s ON s.id = o.shop_id
        WHERE DATE(o.created_at_pos) = %s::date
          AND s.status = 'active'
    """
    params_daily: List[Any] = [target_date_str]
    params_orders: List[Any] = [target_date_str]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return False
        query_daily += " AND s.shop_key = ANY(%s)"
        query_orders += " AND s.shop_key = ANY(%s)"
        keys = list(allowed_shop_keys)
        params_daily.append(keys)
        params_orders.append(keys)
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query_daily, tuple(params_daily))
                expected_orders = int((cur.fetchone() or [0])[0] or 0)
                cur.execute(query_orders, tuple(params_orders))
                actual_orders = int((cur.fetchone() or [0])[0] or 0)
        return expected_orders > 0 and actual_orders == 0
    except Exception:
        return False


def auto_sync_missing_order_status_for_date(target_date_str: str) -> Tuple[bool, str]:
    """
    Kích hoạt sync bổ sung dữ liệu đơn hàng cho ngày thiếu — chạy NGẦM (background thread),
    trả về ngay lập tức để trang không bị chờ.
    Cooldown 10 phút/ngày để tránh re-trigger mỗi page load.
    """
    import time as _t
    if target_date_str in AUTO_SYNC_MISSING_DATE_FAILED:
        return False, "Auto-sync đã thất bại trước đó, chờ lần chạy thủ công."
    if not os.getenv("DATABASE_URL", "").strip():
        return False, "Thiếu DATABASE_URL."
    # Cooldown: bỏ qua nếu đã trigger trong vòng 10 phút
    _last = _AUTO_SYNC_BG_LAST_RUN.get(target_date_str, 0)
    if _t.time() - _last < _AUTO_SYNC_BG_COOLDOWN:
        return True, "Dữ liệu đang được cập nhật ngầm."
    with SYNC_IN_PROGRESS_LOCK:
        if target_date_str in SYNC_IN_PROGRESS_DATES:
            return True, "Đang có tiến trình sync ngầm, vui lòng chờ."
        SYNC_IN_PROGRESS_DATES.add(target_date_str)
    _AUTO_SYNC_BG_LAST_RUN[target_date_str] = _t.time()

    def _run_bg():
        try:
            script_path = os.path.join(BASE_DIR, "scripts", "sync_orders_order_items_to_db.py")
            subprocess.run(
                [sys.executable, script_path, "--date", target_date_str],
                cwd=BASE_DIR,
                check=True,
                timeout=600,
                env=os.environ.copy(),
            )
            clear_home_alert_counts_cache()
            AUTO_SYNC_MISSING_DATE_FAILED.discard(target_date_str)
        except Exception as exc:
            AUTO_SYNC_MISSING_DATE_FAILED.add(target_date_str)
        finally:
            with SYNC_IN_PROGRESS_LOCK:
                SYNC_IN_PROGRESS_DATES.discard(target_date_str)

    threading.Thread(target=_run_bg, daemon=True, name=f"auto-sync-{target_date_str}").start()
    return True, "Đang tự động cập nhật dữ liệu đơn hàng ngầm."


def get_order_status_summary_by_date_from_db(
    target_date_str: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, int]]:
    if not target_date_str or not get_db_conn:
        return None
    query = """
        SELECT o.order_status, COUNT(1)
        FROM orders o
        JOIN shops s ON s.id = o.shop_id
        WHERE DATE(o.created_at_pos) = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [target_date_str]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {"new": 0, "confirmed": 0, "sent": 0, "received": 0, "returning": 0, "returned": 0, "cancelled": 0}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY o.order_status"

    totals = {"new": 0, "confirmed": 0, "sent": 0, "received": 0, "returning": 0, "returned": 0, "cancelled": 0}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
                for status, cnt in rows:
                    c = int(cnt or 0)
                    s = str(status or "").strip()
                    if s == "new":
                        totals["new"] += c
                    elif s == "confirmed":
                        totals["confirmed"] += c
                    elif s == "shipping":
                        totals["sent"] += c
                    elif s == "delivered":
                        totals["received"] += c
                    elif s == "returned":
                        totals["returned"] += c
                    elif s == "returning":
                        totals["returning"] += c
                    elif s == "cancelled":
                        totals["cancelled"] += c
        return totals
    except Exception:
        return None


def load_order_status_map_by_shop_from_db(
    target_date_str: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Dict[str, int]]]:
    if not target_date_str or not get_db_conn:
        return None
    query = """
        SELECT s.shop_key, o.order_status, COUNT(1)
        FROM orders o
        JOIN shops s ON s.id = o.shop_id
        WHERE DATE(o.created_at_pos) = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [target_date_str]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key, o.order_status"

    result: Dict[str, Dict[str, int]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, status_raw, cnt in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    if shop_key not in result:
                        result[shop_key] = {
                            "new_orders": 0,
                            "confirmed_orders": 0,
                            "sent_orders": 0,
                            "received_orders": 0,
                            "returning_orders": 0,
                            "returned_orders": 0,
                            "cancelled_orders": 0,
                        }
                    c = int(cnt or 0)
                    status = str(status_raw or "").strip()
                    if status == "new":
                        result[shop_key]["new_orders"] += c
                    elif status == "confirmed":
                        result[shop_key]["confirmed_orders"] += c
                    elif status == "shipping":
                        result[shop_key]["sent_orders"] += c
                    elif status == "delivered":
                        result[shop_key]["received_orders"] += c
                    elif status == "returning":
                        result[shop_key]["returning_orders"] += c
                    elif status == "returned":
                        result[shop_key]["returned_orders"] += c
                    elif status == "cancelled":
                        result[shop_key]["cancelled_orders"] += c
        return result
    except Exception:
        return None

def load_order_status_map_by_shop_date_range_from_db(
    date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Dict[str, int]]]:
    if not date_from or not date_to or not get_db_conn:
        return None
    query = """
        SELECT s.shop_key, o.order_status, COUNT(1)
        FROM orders o
        JOIN shops s ON s.id = o.shop_id
        WHERE DATE(o.created_at_pos) >= %s::date
          AND DATE(o.created_at_pos) <= %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key, o.order_status"

    result: Dict[str, Dict[str, int]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, status_raw, cnt in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    if shop_key not in result:
                        result[shop_key] = {
                            "new_orders": 0, "confirmed_orders": 0, "sent_orders": 0,
                            "received_orders": 0, "returning_orders": 0, "returned_orders": 0,
                            "cancelled_orders": 0,
                        }
                    c = int(cnt or 0)
                    status = str(status_raw or "").strip()
                    if status == "new":
                        result[shop_key]["new_orders"] += c
                    elif status == "confirmed":
                        result[shop_key]["confirmed_orders"] += c
                    elif status == "shipping":
                        result[shop_key]["sent_orders"] += c
                    elif status == "delivered":
                        result[shop_key]["received_orders"] += c
                    elif status == "returning":
                        result[shop_key]["returning_orders"] += c
                    elif status == "returned":
                        result[shop_key]["returned_orders"] += c
                    elif status == "cancelled":
                        result[shop_key]["cancelled_orders"] += c
        return result
    except Exception:
        return None


def load_order_status_from_status_cache_range(
    date_from: str, date_to: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Dict[str, int]]]:
    """Đọc SUM order status từ shop_order_status_cache cho 1 khoảng ngày."""
    if not date_from or not date_to or not get_db_conn:
        return None
    query = """
        SELECT s.shop_key,
               SUM(c.new_orders)::int, SUM(c.confirmed_orders)::int, SUM(c.sent_orders)::int,
               SUM(c.received_orders)::int, SUM(c.returning_orders)::int, SUM(c.returned_orders)::int,
               SUM(c.cancelled_orders)::int,
               MAX(COALESCE(c.total_active_returning, 0))::int,
               MAX(COALESCE(c.total_returned_all, 0))::int
        FROM shop_order_status_cache c
        JOIN shops s ON s.id = c.shop_id
        WHERE c.metric_date BETWEEN %s::date AND %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [date_from, date_to]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"
    result: Dict[str, Dict[str, int]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for row in cur.fetchall():
                    shop_key = str(row[0] or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = {
                        "new_orders": int(row[1] or 0),
                        "confirmed_orders": int(row[2] or 0),
                        "sent_orders": int(row[3] or 0),
                        "received_orders": int(row[4] or 0),
                        "returning_orders": int(row[5] or 0),
                        "returned_orders": int(row[6] or 0),
                        "cancelled_orders": int(row[7] or 0),
                        "total_active_returning": int(row[8] or 0),
                        "total_returned_all": int(row[9] or 0),
                    }
        return result if result else None
    except Exception:
        return None


def load_order_status_from_status_cache(
    target_date_str: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, Dict[str, int]]]:
    """Đọc aggregate order status từ bảng shop_order_status_cache (nhẹ, không query orders table).
    Trả về dict {shop_key: {new_orders, confirmed_orders, ...}} hoặc None nếu bảng chưa có dữ liệu."""
    if not target_date_str or not get_db_conn:
        return None
    query = """
        SELECT s.shop_key,
               c.new_orders, c.confirmed_orders, c.sent_orders,
               c.received_orders, c.returning_orders, c.returned_orders,
               c.cancelled_orders,
               COALESCE(c.total_active_returning, 0),
               COALESCE(c.total_returned_all, 0)
        FROM shop_order_status_cache c
        JOIN shops s ON s.id = c.shop_id
        WHERE c.metric_date = %s::date
          AND s.status = 'active'
    """
    params: List[Any] = [target_date_str]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))

    result: Dict[str, Dict[str, int]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for row in cur.fetchall():
                    shop_key = str(row[0] or "").strip()
                    if not shop_key:
                        continue
                    result[shop_key] = {
                        "new_orders": int(row[1] or 0),
                        "confirmed_orders": int(row[2] or 0),
                        "sent_orders": int(row[3] or 0),
                        "received_orders": int(row[4] or 0),
                        "returning_orders": int(row[5] or 0),
                        "returned_orders": int(row[6] or 0),
                        "cancelled_orders": int(row[7] or 0),
                        "total_active_returning": int(row[8] or 0),
                        "total_returned_all": int(row[9] or 0),
                    }
        return result if result else None
    except Exception:
        return None


def extract_shop_key_from_filename(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].replace("data_", "")

def normalize_date_value(value: str) -> str:
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    if re.match(r"^\d{2}/\d{2}/\d{4}$", text):
        d, m, y = text.split("/")
        return f"{y}-{m}-{d}"
    if re.match(r"^\d{2}-\d{2}-\d{4}$", text):
        d, m, y = text.split("-")
        return f"{y}-{m}-{d}"
    return text

def pick_row_from_data_list(full_data: Any, selected_date: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    rows = full_data.get("data", []) if isinstance(full_data, dict) else []
    if not isinstance(rows, list) or not rows:
        return {}, None

    normalized_target = normalize_date_value(selected_date) if selected_date else None
    chosen = None

    if normalized_target:
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = normalize_date_value(row.get("Time.day", ""))
            if day == normalized_target:
                chosen = row
                break

    # When a specific date is requested, do not fallback to another day.
    # This prevents previous-day accumulation in today's dashboard.
    if chosen is None and not normalized_target:
        valid_rows = [r for r in rows if isinstance(r, dict) and r.get("Time.day")]
        if valid_rows:
            valid_rows.sort(key=lambda r: normalize_date_value(r.get("Time.day", "")), reverse=True)
            chosen = valid_rows[0]

    if not chosen:
        return {}, None

    return chosen.get("result", {}), normalize_date_value(chosen.get("Time.day", ""))

def parse_shop_file(path: str, selected_date: Optional[str], name_map: Dict[str, str]) -> Dict[str, Any]:
    full_data = try_parse_json(path)
    shop_key = extract_shop_key_from_filename(path)
    shop_name = name_map.get(shop_key, shop_key)

    result_data, actual_date = pick_row_from_data_list(full_data, selected_date)

    revenue = float(result_data.get("revenue", 0) or 0)
    orders = float(result_data.get("order_count", 0) or 0)
    avg_profit = float(result_data.get("avg_profit", 0) or 0)
    profit = float(result_data.get("profit", 0) or 0)
    ads_cost = float(result_data.get("ads_amount", 0) or 0)

    status = "Ổn định"
    if profit < 0:
        status = "Lỗ"
    elif revenue <= 0 or orders <= 0:
        status = "Cảnh báo"
    elif revenue < 1_000_000:
        status = "Cần xem"
    elif revenue >= 10_000_000:
        status = "Tốt"

    return {
        "shop_key": shop_key,
        "shop_name": shop_name,
        "filename": os.path.basename(path),
        "filepath": path,
        "date_key": actual_date,
        "revenue": revenue,
        "orders": orders,
        "avg_profit": avg_profit,
        "profit": profit,
        "ads_cost": ads_cost,
        "revenue_fmt": format_money(revenue),
        "orders_fmt": format_int(orders),
        "avg_profit_fmt": format_money(avg_profit),
        "profit_fmt": format_money(profit),
        "ads_cost_fmt": format_money(ads_cost),
        "revenue_raw": f"{revenue} (result.revenue)",
        "orders_raw": f"{orders} (result.order_count)",
        "avg_profit_raw": f"{avg_profit} (result.avg_profit)",
        "profit_raw": f"{profit} (result.profit)",
        "ads_cost_raw": f"{ads_cost} (result.ads_amount)",
        "profit_class": money_class(profit),
        "avg_profit_class": money_class(avg_profit),
        "status": status,
        "status_class": status_class(status),
    }


def parse_iso_date_from_any(value: str) -> Optional[datetime.date]:
    normalized = normalize_date_value(value)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", normalized):
        return None
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except Exception:
        return None


def get_latest_ad_date_from_shop_file(path: str) -> Optional[datetime.date]:
    try:
        full_data = try_parse_json(path)
    except Exception:
        return None

    rows = full_data.get("data", []) if isinstance(full_data, dict) else []
    if not isinstance(rows, list):
        return None

    latest_date: Optional[datetime.date] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed_day = parse_iso_date_from_any(str(row.get("Time.day", "")).strip())
        if not parsed_day:
            continue
        if latest_date is None or parsed_day > latest_date:
            latest_date = parsed_day
    return latest_date


def load_latest_metric_date_by_shop_from_db(allowed_shop_keys: Optional[set] = None) -> Optional[Dict[str, Optional[str]]]:
    if not get_db_conn:
        return None
    query = """
        SELECT s.shop_key, MAX(m.metric_date)::text
        FROM shops s
        LEFT JOIN daily_shop_metrics m ON m.shop_id = s.id
        WHERE s.status = 'active'
    """
    params: List[Any] = []
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " GROUP BY s.shop_key"
    result: Dict[str, Optional[str]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, latest_raw in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    latest = str(latest_raw or "").strip() or None
                    result[shop_key] = latest
    except Exception:
        return None
    return result


def get_business_metrics_by_date_from_db(shop_key: str, target_date: Optional[str]) -> Optional[Dict[str, Any]]:
    if not get_db_conn or not target_date:
        return None
    query = """
        SELECT COALESCE(SUM(m.gross_revenue), 0), COALESCE(SUM(m.order_count), 0)
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE s.shop_key = %s
          AND m.metric_date = %s::date
    """
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (shop_key, target_date))
                row = cur.fetchone()
                if not row:
                    return {"revenue": 0.0, "orders": 0.0}
                return {"revenue": float(row[0] or 0), "orders": float(row[1] or 0)}
    except Exception:
        return None


def parse_daily_finance_rows_from_db(
    selected_month: str, allowed_shop_keys: Optional[set] = None
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    if not get_db_conn:
        return None
    query = """
        SELECT
            s.shop_key,
            s.shop_name,
            m.metric_date::text,
            COALESCE(m.gross_revenue, 0),
            COALESCE(m.ads_cost, 0),
            COALESCE(m.pos_profit_loss, 0)
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        WHERE s.status = 'active'
          AND TO_CHAR(m.metric_date, 'YYYY-MM') = %s
    """
    params: List[Any] = [selected_month]
    if allowed_shop_keys is not None:
        if not allowed_shop_keys:
            return {}
        query += " AND s.shop_key = ANY(%s)"
        params.append(list(allowed_shop_keys))
    query += " ORDER BY s.shop_key, m.metric_date"
    result: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for shop_key_raw, shop_name_raw, day_raw, revenue_raw, ads_raw, profit_raw in cur.fetchall():
                    shop_key = str(shop_key_raw or "").strip()
                    if not shop_key:
                        continue
                    rows = result.setdefault(shop_key, [])
                    rows.append({
                        "date": str(day_raw or "").strip(),
                        "revenue": float(revenue_raw or 0),
                        "ads": float(ads_raw or 0),
                        "profit": float(profit_raw or 0),
                        "shop_name": str(shop_name_raw or "").strip() or shop_key,
                    })
    except Exception:
        return None
    return result


def build_ads_delay_data(allowed_shop_keys: Optional[set] = None) -> Dict[str, Any]:
    meta_map = get_visible_shop_meta_map()
    db_latest_map = load_latest_metric_date_by_shop_from_db(allowed_shop_keys=allowed_shop_keys)
    if db_latest_map is not None:
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
        cutoff_date = today - timedelta(days=2)
        affected_shops: List[Dict[str, Any]] = []
        for shop_key, meta in meta_map.items():
            if meta.get("status") != "active":
                continue
            if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
                continue
            shop_name = meta.get("shop_name") or shop_key
            latest_text = str(db_latest_map.get(shop_key) or "").strip()
            if not latest_text:
                affected_shops.append({
                    "shop_key": shop_key,
                    "shop_name": shop_name,
                    "latest_ad_date": "-",
                    "missing_days": "-",
                    "missing_days_value": 10**9,
                    "status": "Chậm chưa có dữ liệu QC",
                })
                continue
            latest_day = parse_iso_date_from_any(latest_text)
            if latest_day is None:
                continue
            if latest_day < cutoff_date:
                missing_days = (today - latest_day).days
                affected_shops.append({
                    "shop_key": shop_key,
                    "shop_name": shop_name,
                    "latest_ad_date": latest_day.strftime("%Y-%m-%d"),
                    "missing_days": str(missing_days),
                    "missing_days_value": missing_days,
                    "status": f"Chậm {missing_days} ngày",
                })
        affected_shops.sort(
            key=lambda x: (-int(x.get("missing_days_value", 0)), str(x.get("shop_name", "")))
        )
        return {
            "today_str": today.strftime("%Y-%m-%d"),
            "cutoff_date_str": cutoff_date.strftime("%Y-%m-%d"),
            "affected_shops": affected_shops,
        }

    shop_file_map: Dict[str, str] = {}
    for path in discover_files(allowed_shop_keys=allowed_shop_keys):
        shop_key = extract_shop_key_from_filename(path)
        if shop_key:
            shop_file_map[shop_key] = path

    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    cutoff_date = today - timedelta(days=2)

    affected_shops: List[Dict[str, Any]] = []
    for shop_key, meta in meta_map.items():
        if meta.get("status") != "active":
            continue

        shop_name = meta.get("shop_name") or shop_key
        shop_path = shop_file_map.get(shop_key)
        latest_ad_date = get_latest_ad_date_from_shop_file(shop_path) if shop_path else None

        if latest_ad_date is None:
            affected_shops.append({
                "shop_key": shop_key,
                "shop_name": shop_name,
                "latest_ad_date": "-",
                "missing_days": "-",
                "missing_days_value": 10**9,
                "status": "Chậm chưa có dữ liệu QC",
            })
            continue

        if latest_ad_date < cutoff_date:
            missing_days = (today - latest_ad_date).days
            affected_shops.append({
                "shop_key": shop_key,
                "shop_name": shop_name,
                "latest_ad_date": latest_ad_date.strftime("%Y-%m-%d"),
                "missing_days": str(missing_days),
                "missing_days_value": missing_days,
                "status": f"Chậm {missing_days} ngày",
            })

    affected_shops.sort(
        key=lambda x: (
            -int(x.get("missing_days_value", 0)),
            str(x.get("shop_name", "")),
        )
    )
    return {
        "today_str": today.strftime("%Y-%m-%d"),
        "cutoff_date_str": cutoff_date.strftime("%Y-%m-%d"),
        "affected_shops": affected_shops,
    }


def get_shop_path_map(allowed_shop_keys: Optional[set] = None) -> Dict[str, str]:
    shop_file_map: Dict[str, str] = {}
    scoped_keys = allowed_shop_keys if allowed_shop_keys is not None else get_allowed_shop_keys_for_current_user()
    for path in discover_files(allowed_shop_keys=scoped_keys):
        shop_key = extract_shop_key_from_filename(path)
        if shop_key:
            shop_file_map[shop_key] = path
    return shop_file_map


def get_business_metrics_by_date(path: Optional[str], target_date: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {"revenue": 0.0, "orders": 0.0}
    try:
        full_data = try_parse_json(path)
    except Exception:
        return {"revenue": 0.0, "orders": 0.0}

    result_data, _ = pick_row_from_data_list(full_data, target_date)
    revenue = float(result_data.get("revenue", 0) or 0)
    orders = float(result_data.get("order_count", 0) or 0)
    return {"revenue": revenue, "orders": orders}


def classify_ads_delay_business(revenue: float, orders: float) -> Dict[str, str]:
    if orders <= 0:
        return {"business_status": "Không có đơn", "status_class": "tag-muted"}
    if revenue < 400000:
        return {"business_status": "Đơn nhỏ / test", "status_class": "tag-warn"}
    return {"business_status": "Cảnh báo nghiêm trọng", "status_class": "tag-loss"}


def build_ads_delay_detail(shop_key: str, affected_shops: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    selected = next((s for s in affected_shops if s.get("shop_key") == shop_key), None)
    if not selected:
        return None

    shop_path = get_shop_path_map().get(shop_key)
    latest_ad_date_text = str(selected.get("latest_ad_date", "")).strip()
    target_date = latest_ad_date_text if latest_ad_date_text and latest_ad_date_text != "-" else None
    metrics = get_business_metrics_by_date_from_db(shop_key, target_date) or get_business_metrics_by_date(shop_path, target_date)
    classified = classify_ads_delay_business(metrics["revenue"], metrics["orders"])

    target_date_label = target_date or "Không có dữ liệu QC"
    return {
        "shop_key": shop_key,
        "shop_name": selected.get("shop_name", shop_key),
        "target_date_label": target_date_label,
        "revenue_fmt": format_money(metrics["revenue"]),
        "orders_fmt": format_int(metrics["orders"]),
        "business_status": classified["business_status"],
        "status_class": classified["status_class"],
    }


def parse_month_value(month_value: Optional[str]) -> str:
    value = str(month_value or "").strip()
    if re.match(r"^\d{4}-\d{2}$", value):
        return value
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m")


def parse_daily_finance_rows(path: str, selected_month: str) -> List[Dict[str, Any]]:
    try:
        full_data = try_parse_json(path)
    except Exception:
        return []

    rows = full_data.get("data", []) if isinstance(full_data, dict) else []
    if not isinstance(rows, list):
        return []

    daily_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = normalize_date_value(row.get("Time.day", ""))
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            continue
        if not day.startswith(selected_month + "-"):
            continue
        result = row.get("result", {}) if isinstance(row.get("result", {}), dict) else {}
        revenue = float(result.get("revenue", 0) or 0)
        ads_raw = result.get("ads_amount")
        ads = float(ads_raw or 0) if ads_raw is not None else None
        # POS daily profit/loss is source of truth for loss-day alert.
        profit_raw = result.get("profit")
        if profit_raw is None:
            continue
        profit = float(profit_raw or 0)
        daily_rows.append({
            "date": day,
            "revenue": revenue,
            "ads": ads,
            "profit": profit,
        })

    daily_rows.sort(key=lambda x: x["date"])
    return daily_rows


def classify_monthly_loss(profit_value: float) -> Dict[str, str]:
    if profit_value <= MONTH_LOSS_SEVERE:
        return {"status_label": "Lỗ nghiêm trọng", "status_class": "tag-loss"}
    if profit_value <= MONTH_LOSS_WARN:
        return {"status_label": "Lỗ đáng chú ý", "status_class": "tag-warn"}
    return {"status_label": "Lỗ nhẹ", "status_class": "tag-muted"}


def build_monthly_loss_data(selected_month: str, allowed_shop_keys: Optional[set] = None) -> Dict[str, Any]:
    selected_month = parse_month_value(selected_month)
    db_daily_rows_map = parse_daily_finance_rows_from_db(selected_month, allowed_shop_keys=allowed_shop_keys)
    meta_map = load_shop_meta_map()
    shop_file_map = get_shop_path_map(allowed_shop_keys=allowed_shop_keys)
    losing_shops: List[Dict[str, Any]] = []

    for shop_key, meta in meta_map.items():
        if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
            continue
        if meta.get("status") != "active":
            continue
        if db_daily_rows_map is not None:
            daily_rows = db_daily_rows_map.get(shop_key, [])
        else:
            shop_path = shop_file_map.get(shop_key)
            if not shop_path:
                continue
            daily_rows = parse_daily_finance_rows(shop_path, selected_month)
        if not daily_rows:
            continue

        monthly_revenue = sum(r["revenue"] for r in daily_rows)
        monthly_ads = sum(r["ads"] for r in daily_rows)
        loss_rows = [r for r in daily_rows if r["profit"] < 0]
        if not loss_rows:
            continue
        total_loss_amount = sum(r["profit"] for r in loss_rows)
        latest_loss_date = max((r["date"] for r in loss_rows), default="-")
        cls = classify_monthly_loss(total_loss_amount)
        losing_shops.append({
            "shop_key": shop_key,
            "shop_name": meta.get("shop_name", shop_key),
            "monthly_revenue": monthly_revenue,
            "monthly_ads": monthly_ads,
            "loss_days": len(loss_rows),
            "latest_loss_date": latest_loss_date,
            "loss_dates": [r["date"] for r in loss_rows],
            "loss_by_day": loss_rows,
            "loss_days_detail": [
                {"date": r["date"], "amount_fmt": format_money(r["profit"]),
                 "revenue_fmt": format_money(r.get("revenue", 0))}
                for r in sorted(loss_rows, key=lambda x: x["date"])
            ],
            "total_loss_amount": total_loss_amount,
            "total_loss_amount_fmt": format_money(total_loss_amount),
            "status_label": cls["status_label"],
            "status_class": cls["status_class"],
        })

    losing_shops.sort(key=lambda x: (x["total_loss_amount"], x["shop_name"]))
    monthly_total_loss = sum(s["total_loss_amount"] for s in losing_shops)
    return {
        "selected_month": selected_month,
        "losing_shops": losing_shops,
        "monthly_total_loss": monthly_total_loss,
        "monthly_total_loss_fmt": format_money(monthly_total_loss),
    }


def build_monthly_loss_detail(shop_key: str, losing_shops: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    selected = next((s for s in losing_shops if s.get("shop_key") == shop_key), None)
    if not selected:
        return None

    loss_dates = selected.get("loss_dates", [])

    loss_by_day = []
    for d in selected.get("loss_by_day", []):
        ads_value = d.get("ads")
        loss_by_day.append({
            "date": d.get("date", "-"),
            "revenue_fmt": format_money(d.get("revenue", 0)),
            "ads_fmt": format_money(ads_value) if ads_value is not None else "-",
            "pos_profit_fmt": format_money(float(d.get("profit", 0) or 0)),
        })

    return {
        "shop_key": shop_key,
        "shop_name": selected.get("shop_name", shop_key),
        "total_loss_amount_fmt": selected.get("total_loss_amount_fmt", "0"),
        "loss_days": selected.get("loss_days", 0),
        "latest_loss_date": selected.get("latest_loss_date", "-"),
        "loss_dates_text": ", ".join(loss_dates) or "-",
        "loss_by_day": loss_by_day,
    }

def get_order_status_for_shop_by_date(shop_id: str, target_date_str: Optional[str]):
    if not target_date_str:
        return {
            "new_orders": 0,
            "confirmed_orders": 0,
            "sent_orders": 0,
            "received_orders": 0,
            "returning_orders": 0,
            "returned_orders": 0,
        }

    if not os.path.exists(CONFIG_FILE):
        return {
            "new_orders": 0,
            "confirmed_orders": 0,
            "sent_orders": 0,
            "received_orders": 0,
            "returning_orders": 0,
            "returned_orders": 0,
        }

    try:
        config = try_parse_json(CONFIG_FILE)
    except Exception:
        return {
            "new_orders": 0,
            "confirmed_orders": 0,
            "sent_orders": 0,
            "received_orders": 0,
            "returning_orders": 0,
            "returned_orders": 0,
        }

    data = request_order_status_aggs(shop_id, target_date_str)
    buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
    bucket_map = {}

    for bucket in buckets:
        bucket_map[str(bucket.get("key"))] = int(bucket.get("doc_count", 0))

    return {
        "new_orders": bucket_map.get("0", 0),
        "confirmed_orders": bucket_map.get("9", 0),
        "sent_orders": bucket_map.get("2", 0),
        "received_orders": bucket_map.get("3", 0),
        "returning_orders": bucket_map.get("4", 0),
        "returned_orders": bucket_map.get("5", 0),
    }


def load_shops_for_ads_kpi_range(
    date_from: str,
    date_to: str,
    allowed_shop_keys: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Tổng hợp CP Ads POS + CP Ads FB theo khoảng ngày (DB), dùng cho trang KPI quảng cáo."""
    shops: List[Dict[str, Any]] = []
    meta_map = get_visible_shop_meta_map()
    name_map = load_shop_name_map()
    fb_mapping_map = load_fb_ad_mapping_map_for_dashboard(allowed_shop_keys=allowed_shop_keys)
    db_fb_ads_map = load_daily_fb_ads_cost_map_from_db_range(date_from, date_to, allowed_shop_keys=allowed_shop_keys)
    db_pos_map = load_daily_shop_ads_pos_map_from_db_range(date_from, date_to, allowed_shop_keys=allowed_shop_keys)
    for shop_key, shop_meta in meta_map.items():
        if shop_meta.get("status") != "active":
            continue
        if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
            continue
        shop_name = str(shop_meta.get("shop_name", "")).strip() or name_map.get(shop_key, shop_key)
        fb_ads_cost = float(db_fb_ads_map.get(shop_key, 0) or 0)
        ads_cost = float(db_pos_map.get(shop_key, 0) or 0)
        fb_ads_vat_amount = fb_ads_cost * 0.113
        fb_ads_cost_after_vat = fb_ads_cost * 1.113
        fb_mapping = fb_mapping_map.get(shop_key, {})
        shops.append({
            "shop_key": shop_key,
            "shop_name": shop_name,
            "filename": "",
            "filepath": "",
            "date_key": f"{date_from}..{date_to}",
            "revenue": 0.0,
            "orders": 0.0,
            "ads_cost": ads_cost,
            "fb_ads_cost": fb_ads_cost,
            "fb_ads_vat_amount": fb_ads_vat_amount,
            "fb_ads_cost_after_vat": fb_ads_cost_after_vat,
            "profit": 0.0,
            "avg_profit": 0.0,
            "ads_cost_fmt": format_money(ads_cost),
            "fb_ads_cost_fmt": format_money(fb_ads_cost),
            "fb_ads_vat_amount_fmt": format_money(fb_ads_vat_amount),
            "fb_ads_cost_after_vat_fmt": format_money(fb_ads_cost_after_vat),
            "ads_cost_raw": f"{ads_cost} (SUM daily_shop_metrics.ads_cost {date_from}..{date_to})",
            "fb_ads_cost_raw": f"{fb_ads_cost} (SUM fb_ads_daily_metrics.spend {date_from}..{date_to})",
            "fb_ad_account_count": int(fb_mapping.get("fb_ad_account_count", 0) or 0),
            "fb_mapping_status": str(fb_mapping.get("fb_mapping_status", "")).strip() or "-",
        })
    shops.sort(key=lambda x: x.get("fb_ads_cost", 0), reverse=True)
    return shops


def load_all_shops(selected_date: Optional[str], allowed_shop_keys: Optional[set] = None, cumulative_status: bool = False) -> List[Dict[str, Any]]:
    shops: List[Dict[str, Any]] = []
    name_map = load_shop_name_map()
    meta_map = get_visible_shop_meta_map()
    db_fb_ads_map = load_daily_fb_ads_cost_map_from_db(selected_date, allowed_shop_keys=allowed_shop_keys)
    fb_mapping_map = load_fb_ad_mapping_map_for_dashboard(allowed_shop_keys=allowed_shop_keys)
    db_daily_map = (
        load_daily_orders_revenue_map_from_db(selected_date, allowed_shop_keys=allowed_shop_keys)
        if is_db_daily_dashboard_enabled()
        else {}
    )
    db_order_status_map = (
        load_order_status_map_by_shop_from_db(selected_date, allowed_shop_keys=allowed_shop_keys)
        if selected_date and is_db_order_status_enabled()
        else None
    )
    # Lightweight cache: aggregate counts synced every ~12 min (không query orders table riêng lẻ)
    status_cache_map = (
        load_order_status_from_status_cache(selected_date, allowed_shop_keys=allowed_shop_keys)
        if selected_date
        else None
    )
    # Tải per-shop live counts (status 0-9) từ live_pos_status.json để fill sent/received/confirmed
    # khi daily cache không có dữ liệu (không chọn ngày → cumulative mode)
    _live_pos_json = load_live_pos_status()
    live_shop_counts_map: Dict[str, Dict[int, int]] = {
        r["shop_key"]: {int(k): int(v) for k, v in r.get("counts", {}).items()}
        for r in _live_pos_json.get("shop_results", [])
        if r.get("ok")
    }
    # Tải carrier pickup per-shop từ DB (ĐVVC lấy hàng trong ngày)
    carrier_pickup_map = load_carrier_pickup_orders_map_from_db(selected_date, meta_map)

    # DB-first fast path for main dashboard: avoid heavy JSON parsing per shop.
    # Keep fallback path below for safety when DB mode is off.
    if selected_date and is_db_daily_dashboard_enabled():
        for shop_key, shop_meta in meta_map.items():
            if shop_meta.get("status") != "active":
                continue
            if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
                continue
            shop_name = str(shop_meta.get("shop_name", "")).strip() or name_map.get(shop_key, shop_key)
            db_daily = db_daily_map.get(shop_key, {})
            revenue = float(db_daily.get("revenue", 0) or 0)
            orders = float(db_daily.get("orders", 0) or 0)
            ads_cost = float(db_daily.get("ads_cost", 0) or 0)
            fb_ads_cost = float(db_fb_ads_map.get(shop_key, 0) or 0)
            fb_ads_vat_amount = fb_ads_cost * 0.113
            fb_ads_cost_after_vat = fb_ads_cost * 1.113
            profit = float(db_daily.get("profit", 0) or 0)
            avg_profit = float(db_daily.get("avg_profit", 0) or 0)

            if profit < 0:
                status = "Lỗ"
            elif revenue <= 0 or orders <= 0:
                status = "Cảnh báo"
            elif revenue < 1_000_000:
                status = "Cần xem"
            elif revenue >= 10_000_000:
                status = "Tốt"
            else:
                status = "Ổn định"

            # Ưu tiên orders table (dữ liệu real-time từ wh_sync_orders)
            # Nếu shop chưa có trong orders table → fallback sang shop_order_status_cache (Pancake aggs)
            if db_order_status_map is not None and shop_key in db_order_status_map:
                status_data = db_order_status_map[shop_key]
            elif status_cache_map is not None:
                status_data = status_cache_map.get(shop_key, {})
            else:
                status_data = {}
            fb_mapping = fb_mapping_map.get(shop_key, {})
            _lc = live_shop_counts_map.get(shop_key, {})
            if cumulative_status:
                # Tích lũy (không có ?date=) → dùng live_pos_status.json per-shop (giống tab bar)
                _new_s    = _lc.get(0, 0)
                _conf_s   = _lc.get(9, 0)
                _sent_s   = _lc.get(2, 0)
                _recv_s   = _lc.get(3, 0)
                _ret_s    = _lc.get(4, 0)
                _retd_s   = _lc.get(5, 0)
                _cancel_s = _lc.get(6, 0)
            else:
                # Chọn ngày → dùng daily cache thuần
                _new_s    = int(status_data.get("new_orders", 0) or 0)
                # "Chờ chuyển hàng" (Pancake status 9): orders table map 9→"returning" (SAI)
                # nên confirmed_orders ở đó =0 → ưu tiên shop_order_status_cache (Pancake aggs
                # theo ngày, chuẩn). Giống cách xử lý "Đã hoàn" bên dưới.
                _conf_s   = int((status_cache_map or {}).get(shop_key, {}).get("confirmed_orders", 0) or 0) \
                            or int(status_data.get("confirmed_orders", 0) or 0)
                _sent_s   = int(status_data.get("sent_orders", 0) or 0)
                _recv_s   = int(status_data.get("received_orders", 0) or 0)
                _ret_s    = int(status_data.get("returning_orders", 0) or 0)
                # Đơn HOÀN (status 5): orders table real-time không track status 5 →
                # ưu tiên shop_order_status_cache (Pancake aggs theo ngày) cho cột "Đã hoàn".
                _retd_s   = int((status_cache_map or {}).get(shop_key, {}).get("returned_orders", 0) or 0) \
                            or int(status_data.get("returned_orders", 0) or 0)
                _cancel_s = max(
                    int(status_data.get("cancelled_orders", 0) or 0),
                    int((status_cache_map or {}).get(shop_key, {}).get("cancelled_orders", 0) or 0),
                )
            shops.append({
                "shop_key": shop_key,
                "shop_name": shop_name,
                "business_type": (shop_meta.get("business_type") or "cty").lower(),
                "filename": "",
                "filepath": "",
                "date_key": selected_date,
                "revenue": revenue,
                "orders": orders,
                "new_orders": _new_s + _conf_s + _sent_s + _recv_s + _ret_s + _retd_s + _cancel_s,
                "confirmed_orders": _conf_s,
                "sent_orders": _sent_s,
                "received_orders": _recv_s,
                "returning_orders": _ret_s,
                "returned_orders": _retd_s,
                # Tích lũy: ưu tiên status_cache_map, fallback sang db_order_status_map (date-range)
                "total_active_returning": int(
                    (status_cache_map or {}).get(shop_key, {}).get("total_active_returning") or
                    (db_order_status_map or {}).get(shop_key, {}).get("total_active_returning") or 0
                ),
                "total_returned_all": int(
                    (status_cache_map or {}).get(shop_key, {}).get("total_returned_all") or
                    (db_order_status_map or {}).get(shop_key, {}).get("total_returned_all") or 0
                ),
                "cancelled_orders": _cancel_s,
                "new_status_orders": _new_s,
                "total_created_today": int(db_daily.get("total_order_count", 0) or 0),
                "carrier_pickup_orders": carrier_pickup_map.get(shop_key, 0),
                "avg_profit": avg_profit,
                "profit": profit,
                "ads_cost": ads_cost,
                "fb_ads_cost": fb_ads_cost,
                "fb_ads_vat_amount": fb_ads_vat_amount,
                "fb_ads_cost_after_vat": fb_ads_cost_after_vat,
                "revenue_fmt": format_money(revenue),
                "orders_fmt": format_int(orders),
                "avg_profit_fmt": format_money(avg_profit),
                "profit_fmt": format_money(profit),
                "ads_cost_fmt": format_money(ads_cost),
                "fb_ads_cost_fmt": format_money(fb_ads_cost),
                "fb_ads_vat_amount_fmt": format_money(fb_ads_vat_amount),
                "fb_ads_cost_after_vat_fmt": format_money(fb_ads_cost_after_vat),
                "revenue_raw": f"{revenue} (daily_shop_metrics.gross_revenue)",
                "orders_raw": f"{orders} (daily_shop_metrics.order_count)",
                "avg_profit_raw": f"{avg_profit} (daily_shop_metrics POS-weighted avg_profit)",
                "profit_raw": f"{profit} (daily_shop_metrics.pos_profit_loss)",
                "ads_cost_raw": f"{ads_cost} (daily_shop_metrics.ads_cost)",
                "fb_ads_cost_raw": f"{fb_ads_cost} (fb_ads_daily_metrics.spend)",
                "fb_ad_account_count": int(fb_mapping.get("fb_ad_account_count", 0) or 0),
                "fb_mapping_status": str(fb_mapping.get("fb_mapping_status", "")).strip() or "-",
                "profit_class": money_class(profit),
                "avg_profit_class": money_class(avg_profit),
                "status": status,
                "status_class": status_class(status),
            })
        shops.sort(key=lambda x: x.get("revenue", 0), reverse=True)
        return shops

    for path in discover_files(allowed_shop_keys=allowed_shop_keys):
        try:
            shop_data = parse_shop_file(path, selected_date, name_map)
            shop_meta = meta_map.get(shop_data["shop_key"], {})
            if shop_meta.get("status") != "active":
                continue
            shop_id = shop_meta.get("shop_id", "")
            shop_data["business_type"] = (shop_meta.get("business_type") or "cty").lower()

            # Mặc định 0 để không phá dữ liệu doanh thu nếu phần status lỗi
            shop_data["new_orders"] = 0
            shop_data["confirmed_orders"] = 0
            shop_data["sent_orders"] = 0
            shop_data["received_orders"] = 0
            shop_data["returning_orders"] = 0
            shop_data["returned_orders"] = 0
            shop_data["total_active_returning"] = 0
            shop_data["total_returned_all"] = 0
            shop_data["fb_ads_cost"] = float(db_fb_ads_map.get(shop_data["shop_key"], 0) or 0)
            shop_data["fb_ads_cost_fmt"] = format_money(shop_data["fb_ads_cost"])
            shop_data["fb_ads_vat_amount"] = shop_data["fb_ads_cost"] * 0.113
            shop_data["fb_ads_cost_after_vat"] = shop_data["fb_ads_cost"] * 1.113
            shop_data["fb_ads_vat_amount_fmt"] = format_money(shop_data["fb_ads_vat_amount"])
            shop_data["fb_ads_cost_after_vat_fmt"] = format_money(shop_data["fb_ads_cost_after_vat"])
            shop_data["fb_ads_cost_raw"] = f"{shop_data['fb_ads_cost']} (fb_ads_daily_metrics.spend)"
            fb_mapping = fb_mapping_map.get(shop_data["shop_key"], {})
            shop_data["fb_ad_account_count"] = int(fb_mapping.get("fb_ad_account_count", 0) or 0)
            shop_data["fb_mapping_status"] = str(fb_mapping.get("fb_mapping_status", "")).strip() or "-"

            try:
                shop_meta = meta_map.get(shop_data["shop_key"], {})
                shop_id = shop_meta.get("shop_id", "")

                _sk = shop_data["shop_key"]
                _lc2 = live_shop_counts_map.get(_sk, {})

                if cumulative_status:
                    # Tích lũy (không có ?date=) → dùng live_pos_status.json per-shop counts
                    # Logic giống hệt tab bar: live_date_mode=False → live_counts từ JSON
                    shop_data["new_status_orders"]  = _lc2.get(0, 0)
                    shop_data["new_orders"]         = _lc2.get(0, 0)
                    shop_data["confirmed_orders"]   = _lc2.get(9, 0)
                    shop_data["sent_orders"]        = _lc2.get(2, 0)
                    shop_data["received_orders"]    = _lc2.get(3, 0)
                    shop_data["returning_orders"]   = _lc2.get(4, 0)
                    shop_data["returned_orders"]    = _lc2.get(5, 0)
                    shop_data["cancelled_orders"]   = _lc2.get(6, 0)
                    shop_data["total_active_returning"] = _lc2.get(4, 0)
                    shop_data["total_returned_all"]     = _lc2.get(5, 0)
                elif db_order_status_map is not None:
                    status_data = db_order_status_map.get(_sk, {})
                    shop_data["new_status_orders"]  = int(status_data.get("new_orders", 0) or 0)
                    shop_data["new_orders"]         = int(status_data.get("new_orders", 0) or 0)
                    shop_data["confirmed_orders"]   = int(status_data.get("confirmed_orders", 0) or 0)
                    shop_data["sent_orders"]        = int(status_data.get("sent_orders", 0) or 0)
                    shop_data["received_orders"]    = int(status_data.get("received_orders", 0) or 0)
                    shop_data["returning_orders"]   = int(status_data.get("returning_orders", 0) or 0)
                    shop_data["returned_orders"]    = int(status_data.get("returned_orders", 0) or 0)
                    shop_data["cancelled_orders"]   = int(max(
                        int(status_data.get("cancelled_orders", 0) or 0),
                        int((status_cache_map or {}).get(_sk, {}).get("cancelled_orders", 0) or 0),
                    ))
                elif status_cache_map is not None:
                    status_data = status_cache_map.get(_sk, {})
                    shop_data["new_status_orders"]  = int(status_data.get("new_orders", 0) or 0)
                    shop_data["new_orders"]         = int(status_data.get("new_orders", 0) or 0)
                    shop_data["confirmed_orders"]   = int(status_data.get("confirmed_orders", 0) or 0)
                    shop_data["sent_orders"]        = int(status_data.get("sent_orders", 0) or 0)
                    shop_data["received_orders"]    = int(status_data.get("received_orders", 0) or 0)
                    shop_data["returning_orders"]   = int(status_data.get("returning_orders", 0) or 0)
                    shop_data["returned_orders"]    = int(status_data.get("returned_orders", 0) or 0)
                    shop_data["cancelled_orders"]   = int(status_data.get("cancelled_orders", 0) or 0)
                elif shop_id:
                    status_data = get_order_status_for_shop_by_date(shop_id, selected_date)
                    shop_data["new_status_orders"]  = status_data.get("new_orders", 0)
                    shop_data["new_orders"]         = status_data.get("new_orders", 0)
                    shop_data["confirmed_orders"]   = status_data.get("confirmed_orders", 0)
                    shop_data["sent_orders"]        = status_data.get("sent_orders", 0)
                    shop_data["received_orders"]    = status_data.get("received_orders", 0)
                    shop_data["returning_orders"]   = status_data.get("returning_orders", 0)
                    shop_data["returned_orders"]    = status_data.get("returned_orders", 0)
            except Exception:
                pass

            # Tích lũy đến hiện tại: đang hoàn (status=4) + đã hoàn (status=5) từ Pancake aggs
            # (chỉ ghi đè nếu chưa set ở trên — cumulative mode đã set trong block cumulative_status)
            _sk2 = shop_data.get("shop_key", "")
            if not cumulative_status:
                # Ưu tiên status_cache_map (single-day); nếu thiếu thì dùng db_order_status_map (date-range)
                shop_data["total_active_returning"] = int(
                    (status_cache_map or {}).get(_sk2, {}).get("total_active_returning") or
                    (db_order_status_map or {}).get(_sk2, {}).get("total_active_returning") or 0
                )
                shop_data["total_returned_all"] = int(
                    (status_cache_map or {}).get(_sk2, {}).get("total_returned_all") or
                    (db_order_status_map or {}).get(_sk2, {}).get("total_returned_all") or 0
                )

            # Safe phase cutover: only daily revenue + order_count from DB (verified parity),
            # while keeping all other fields/logic unchanged.
            db_daily = db_daily_map.get(shop_data["shop_key"])
            if db_daily is not None:
                shop_data["revenue"] = float(db_daily.get("revenue", 0) or 0)
                shop_data["orders"] = float(db_daily.get("orders", 0) or 0)
                shop_data["revenue_fmt"] = format_money(shop_data["revenue"])
                shop_data["orders_fmt"] = format_int(shop_data["orders"])
                shop_data["revenue_raw"] = shop_data["revenue"]
                shop_data["orders_raw"] = shop_data["orders"]
                if shop_data["profit"] < 0:
                    shop_data["status"] = "Lỗ"
                elif shop_data["revenue"] <= 0 or shop_data["orders"] <= 0:
                    shop_data["status"] = "Cảnh báo"
                elif shop_data["revenue"] < 1_000_000:
                    shop_data["status"] = "Cần xem"
                elif shop_data["revenue"] >= 10_000_000:
                    shop_data["status"] = "Tốt"
                else:
                    shop_data["status"] = "Ổn định"
                shop_data["status_class"] = status_class(shop_data["status"])

            shop_data.setdefault("carrier_pickup_orders", carrier_pickup_map.get(shop_data.get("shop_key", ""), 0))
            shops.append(shop_data)

        except Exception as exc:
            shops.append({
                "shop_key": extract_shop_key_from_filename(path),
                "shop_name": extract_shop_key_from_filename(path),
                "filename": os.path.basename(path),
                "filepath": path,
                "date_key": selected_date,
                "revenue": 0.0,
                "orders": 0.0,
                "new_orders": 0,
                "confirmed_orders": 0,
                "sent_orders": 0,
                "received_orders": 0,
                "returning_orders": 0,
                "returned_orders": 0,
                "total_active_returning": 0,
                "total_returned_all": 0,
                "avg_profit": 0.0,
                "profit": 0.0,
                "ads_cost": 0.0,
                "fb_ads_cost": 0.0,
                "fb_ads_vat_amount": 0.0,
                "fb_ads_cost_after_vat": 0.0,
                "revenue_fmt": "0",
                "orders_fmt": "0",
                "avg_profit_fmt": "0",
                "profit_fmt": "0",
                "ads_cost_fmt": "0",
                "fb_ads_cost_fmt": "0",
                "fb_ads_vat_amount_fmt": "0",
                "fb_ads_cost_after_vat_fmt": "0",
                "revenue_raw": f"parse error: {exc}",
                "orders_raw": "-",
                "avg_profit_raw": "-",
                "profit_raw": "-",
                "ads_cost_raw": "-",
                "fb_ads_cost_raw": "-",
                "fb_ad_account_count": 0,
                "fb_mapping_status": "-",
                "profit_class": "",
                "avg_profit_class": "",
                "status": "Lỗi file",
                "status_class": "tag-warn",
            })

    shops.sort(key=lambda x: x.get("revenue", 0), reverse=True)
    return shops
def build_alerts(shops: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []
    zero_order = [s for s in shops if s["orders"] <= 0]
    low_revenue = [s for s in shops if 0 < s["revenue"] < 1_000_000]
    broken = [s for s in shops if s["status"] == "Lỗi file"]
    loss_shops = [s for s in shops if s["profit"] < 0]
    ads_shops = [s for s in shops if s["ads_cost"] > 0]

    def _full(lst):
        return ", ".join(s["shop_name"] for s in lst)

    if zero_order:
        alerts.append({"level": "Cảnh báo", "message": f"{len(zero_order)} shop chưa có đơn", "detail": _full(zero_order)})
    if low_revenue:
        alerts.append({"level": "Theo dõi", "message": f"{len(low_revenue)} shop doanh thu thấp", "detail": _full(low_revenue)})
    if loss_shops:
        alerts.append({"level": "Lãi/lỗ", "message": f"Có {len(loss_shops)} shop đang lỗ", "detail": _full(loss_shops)})
    if ads_shops:
        alerts.append({"level": "Ads", "message": f"Có {len(ads_shops)} shop phát sinh chi phí quảng cáo", "detail": _full(ads_shops)})
    if broken:
        alerts.append({"level": "Lỗi dữ liệu", "message": "Có file cần kiểm tra", "detail": _full(broken)})
    return alerts[:5]

def build_shop_alerts(shop: Dict[str, Any]) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []
    if shop["orders"] <= 0:
        alerts.append({"level": "Cảnh báo", "message": "Shop này chưa có đơn hoặc ngày bạn chọn không có dữ liệu."})
    if shop["revenue"] <= 0:
        alerts.append({"level": "Cảnh báo", "message": "Shop này chưa có doanh thu hoặc ngày bạn chọn không có dữ liệu."})
    if shop["profit"] < 0:
        alerts.append({"level": "Lỗ", "message": "Shop này đang âm lợi nhuận ở ngày đang xem."})
    if shop["ads_cost"] > 0:
        alerts.append({"level": "Ads", "message": "Shop này có phát sinh chi phí quảng cáo ở ngày đang xem."})
    if shop["status"] == "Lỗi file":
        alerts.append({"level": "Lỗi", "message": "File JSON này đọc không thành công. Cần kiểm tra format."})
    return alerts

# STOCK / SLOW
def stock_file_path_for_date(shop_id: str, selected_date: Optional[str]) -> Optional[str]:
    if selected_date:
        path = os.path.join(BASE_DIR, "stock_history", selected_date, f"shop_{shop_id}.json")
        return path if os.path.exists(path) else None
    latest_path = os.path.join(BASE_DIR, f"stock_{shop_id}.json")
    return latest_path if os.path.exists(latest_path) else None

def load_stock_file_for_date(shop_id: str, selected_date: Optional[str]) -> Optional[Any]:
    path = stock_file_path_for_date(shop_id, selected_date)
    if not path:
        return None
    try:
        return try_parse_json(path)
    except Exception:
        return None

def build_stock_map(stock_data: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(stock_data, dict):
        return result
    for product in stock_data.get("data", []):
        if not isinstance(product, dict):
            continue
        product_id = product.get("id")
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")
        for variation in product.get("variations", []):
            if not isinstance(variation, dict):
                continue
            variation_id = variation.get("id")
            variation_code = variation.get("custom_id") or product_code or ""
            for wh in variation.get("variations_warehouses", []):
                if not isinstance(wh, dict):
                    continue
                warehouse_id = wh.get("warehouse_id")
                qty = wh.get("available_quantity")
                if qty is None:
                    continue
                key = f"{product_id}|{variation_id}|{warehouse_id}"
                result[key] = {
                    "product_name": product_name,
                    "product_code": variation_code,
                    "variation_id": variation_id,
                    "warehouse_id": warehouse_id,
                    "qty": qty,
                }
    return result

def analyze_slow_items(shop_id: str, selected_date: Optional[str]) -> List[Dict[str, Any]]:
    today_str = selected_date or today_hcm()
    old_str = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=SLOW_DAYS)).strftime("%Y-%m-%d")

    today_data = load_stock_file_for_date(shop_id, today_str)
    old_data = load_stock_file_for_date(shop_id, old_str)
    if not today_data or not old_data:
        return []

    today_map = build_stock_map(today_data)
    old_map = build_stock_map(old_data)

    items = []
    for key, old_item in old_map.items():
        today_item = today_map.get(key)
        if not today_item:
            continue
        old_qty = old_item["qty"]
        today_qty = today_item["qty"]
        sold_7d = old_qty - today_qty
        if old_qty < MIN_OLD_QTY:
            continue
        if sold_7d < 0:
            continue
        if sold_7d <= MAX_SOLD_IN_7D:
            items.append({
                "product_name": today_item["product_name"],
                "product_code": today_item["product_code"],
                "warehouse_id": today_item["warehouse_id"],
                "old_qty": old_qty,
                "today_qty": today_qty,
                "sold_7d": sold_7d,
            })
    items.sort(key=lambda x: (x["sold_7d"], -x["today_qty"], x["product_name"]))
    return items

def parse_stock_file(shop_id: str, path: str, shop_id_to_meta: Dict[str, Dict[str, str]], selected_date: Optional[str]) -> Dict[str, Any]:
    data = try_parse_json(path)
    meta = shop_id_to_meta.get(shop_id, {})
    shop_key = meta.get("shop_key", shop_id)
    shop_name = meta.get("shop_name", shop_id)

    total_rows = 0
    out_count = 0
    low_count = 0

    for product in data.get("data", []):
        if not isinstance(product, dict):
            continue
        for variation in product.get("variations", []):
            if not isinstance(variation, dict):
                continue
            for wh in variation.get("variations_warehouses", []):
                if not isinstance(wh, dict):
                    continue
                qty = wh.get("available_quantity")
                if qty is None:
                    continue
                total_rows += 1
                if qty == 0:
                    out_count += 1
                elif 0 < qty <= LOW_STOCK_THRESHOLD:
                    low_count += 1

    slow_count = len(analyze_slow_items(shop_id, selected_date))

    status = "Ổn định"
    if out_count > 0:
        status = "Hết hàng"
    elif low_count > 0:
        status = "Sắp hết"
    elif slow_count > 0:
        status = "Bán chậm"

    status_css = "tag-good"
    if status == "Hết hàng":
        status_css = "tag-loss"
    elif status in {"Sắp hết", "Bán chậm"}:
        status_css = "tag-warn"

    return {
        "shop_id": shop_id,
        "shop_key": shop_key,
        "shop_name": shop_name,
        "filepath": path,
        "total_rows": total_rows,
        "out_count": out_count,
        "low_count": low_count,
        "slow_count": slow_count,
        "total_rows_fmt": format_int(total_rows),
        "out_count_fmt": format_int(out_count),
        "low_count_fmt": format_int(low_count),
        "slow_count_fmt": format_int(slow_count),
        "status": status,
        "status_class": status_css,
        "data": data.get("data", []),
    }

def build_total_stock_items(stock_shops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    item_map: Dict[str, Dict[str, Any]] = {}

    for shop in stock_shops:
        shop_name = shop.get("shop_name", "")
        for raw_item in shop.get("data", []):
            product_name = raw_item.get("name", "")

            for variation in raw_item.get("variations", []):
                if not isinstance(variation, dict):
                    continue

                product_code = variation.get("custom_id") or raw_item.get("display_id") or ""
                import_price = variation.get("average_imported_price", 0) or 0

                try:
                    import_price = float(import_price)
                except Exception:
                    import_price = 0

                for wh in variation.get("variations_warehouses", []):
                    if not isinstance(wh, dict):
                        continue

                    quantity = wh.get("actual_remain_quantity", 0) or 0
                    warehouse_id = wh.get("warehouse_id", "")

                    try:
                        quantity = float(quantity)
                    except Exception:
                        quantity = 0

                    key = f"{product_name}|{product_code}|{warehouse_id}"

                    if key not in item_map:
                        item_map[key] = {
                            "product_name": product_name,
                            "product_code": product_code,
                            "warehouse_id": warehouse_id,
                            "quantity": 0,
                            "stock_value": 0,
                            "shop_names": set(),
                        }

                    item_map[key]["quantity"] += quantity
                    item_map[key]["stock_value"] += quantity * import_price
                    if shop_name:
                        item_map[key]["shop_names"].add(shop_name)

    items = []
    for item in item_map.values():
        items.append({
            "product_name": item["product_name"],
            "product_code": item["product_code"],
            "warehouse_id": item["warehouse_id"],
            "quantity": item["quantity"],
            "quantity_fmt": format_int(item["quantity"]),
            "stock_value": item["stock_value"],
            "stock_value_fmt": format_int(item["stock_value"]),
            "shop_names": ", ".join(sorted(item["shop_names"]))[:120],
        })

    items.sort(key=lambda x: (-x["quantity"], -x["stock_value"], x["product_name"]))
    return items
def load_all_stock_shops(selected_date: Optional[str]) -> List[Dict[str, Any]]:
    stock_shops = []
    shop_id_to_meta = build_shop_id_to_meta_map()
    for shop_id, meta in shop_id_to_meta.items():
        path = stock_file_path_for_date(shop_id, selected_date)
        if not path:
            continue
        try:
            stock_shops.append(parse_stock_file(shop_id, path, shop_id_to_meta, selected_date))
        except Exception as exc:
            stock_shops.append({
                "shop_id": shop_id,
                "shop_key": meta.get("shop_key", shop_id),
                "shop_name": meta.get("shop_name", shop_id),
                "filepath": path,
                "total_rows": 0,
                "out_count": 0,
                "low_count": 0,
                "slow_count": 0,
                "total_rows_fmt": "0",
                "out_count_fmt": "0",
                "low_count_fmt": "0",
                "slow_count_fmt": "0",
                "status": f"Lỗi file: {exc}",
                "status_class": "tag-warn",
            })
    stock_shops.sort(key=lambda x: (x.get("out_count", 0), x.get("low_count", 0), x.get("slow_count", 0)), reverse=True)
    return stock_shops

def build_stock_dashboard_alerts(stock_shops: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    alerts = []
    out_shops = [s for s in stock_shops if s["out_count"] > 0]
    low_shops = [s for s in stock_shops if s["low_count"] > 0]
    slow_shops = [s for s in stock_shops if s["slow_count"] > 0]

    if out_shops:
        names = ", ".join(s["shop_name"] for s in out_shops[:3])
        alerts.append({"level": "Hết hàng", "message": f"Có {len(out_shops)} shop có SKU hết hàng: {names}."})
    if low_shops:
        names = ", ".join(s["shop_name"] for s in low_shops[:3])
        alerts.append({"level": "Sắp hết", "message": f"Có {len(low_shops)} shop có SKU sắp hết: {names}."})
    if slow_shops:
        names = ", ".join(s["shop_name"] for s in slow_shops[:3])
        alerts.append({"level": "Bán chậm", "message": f"Có {len(slow_shops)} shop có SKU bán chậm: {names}."})
    return alerts[:5]

def build_stock_shop_alerts(shop_id: str, selected_date: Optional[str]) -> List[Dict[str, str]]:
    alerts = []
    data = load_stock_file_for_date(shop_id, selected_date)
    if not data:
        return [{"level": "Lỗi", "message": "Không có dữ liệu tồn kho cho ngày đang xem."}]
    for product in data.get("data", []):
        if not isinstance(product, dict):
            continue
        product_name = product.get("name", "Không tên")
        product_code = product.get("custom_id", "")
        for variation in product.get("variations", []):
            if not isinstance(variation, dict):
                continue
            variation_code = variation.get("custom_id") or product_code or ""
            for wh in variation.get("variations_warehouses", []):
                if not isinstance(wh, dict):
                    continue
                qty = wh.get("available_quantity")
                warehouse_id = wh.get("warehouse_id", "")
                if qty is None:
                    continue
                if qty == 0:
                    alerts.append({"level": "Hết hàng", "message": f"{product_name} | Mã: {variation_code} | Kho: {warehouse_id} | Tồn: 0"})
                elif 0 < qty <= LOW_STOCK_THRESHOLD:
                    alerts.append({"level": "Sắp hết", "message": f"{product_name} | Mã: {variation_code} | Kho: {warehouse_id} | Tồn: {qty}"})
    for item in analyze_slow_items(shop_id, selected_date)[:30]:
        alerts.append({"level": "Bán chậm", "message": f"{item['product_name']} | Mã: {item['product_code']} | Xuất 7 ngày: {item['sold_7d']} | Tồn hiện tại: {item['today_qty']}"})
    return alerts[:30]

def build_slow_dashboard_data(selected_date: Optional[str]) -> List[Dict[str, Any]]:
    shop_id_to_meta = build_shop_id_to_meta_map()
    results = []
    for shop_id, meta in shop_id_to_meta.items():
        items = analyze_slow_items(shop_id, selected_date)
        if not items:
            continue
        stuck_qty = sum(item["today_qty"] for item in items)
        results.append({
            "shop_id": shop_id,
            "shop_key": meta.get("shop_key", shop_id),
            "shop_name": meta.get("shop_name", shop_id),
            "count": len(items),
            "stuck": stuck_qty,
            "count_fmt": format_int(len(items)),
            "stuck_fmt": format_int(stuck_qty),
            "status": "Cảnh báo",
            "status_class": "tag-warn",
            "items": items,
        })
    results.sort(key=lambda x: (-x["count"], -x["stuck"]))
    return results

def build_slow_dashboard_alerts(slow_shops: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    alerts = []
    if slow_shops:
        top_names = ", ".join(s["shop_name"] for s in slow_shops[:3])
        alerts.append({"level": "Bán chậm", "message": f"{len(slow_shops)} shop có sản phẩm bán chậm. Nổi bật: {top_names}."})
    return alerts[:5]

# SENT ITEMS
def get_day_range_params(target_date_str: str):
    from datetime import timezone as _tz
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_utc = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0) - timedelta(hours=7)
    end_utc   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59) - timedelta(hours=7)
    start_ts = int(start_utc.replace(tzinfo=_tz.utc).timestamp())
    end_ts   = int(end_utc.replace(tzinfo=_tz.utc).timestamp())
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return start_ts, end_ts, start_iso, end_iso

def request_sent_orders(
    shop_id: str,
    target_date_str: str,
    page: int = 1,
    page_size: int = 100,
    *,
    update_status: str = "inserted_at",
    access_token: str = "",
    cookie: str = "",
):
    api_key = access_token or _pancake_auth.get_shop_api_key(str(shop_id))
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"
    params = [
        ("api_key", api_key),
        ("page_size", page_size),
        ("status", 2),
        ("page", page),
        ("updateStatus", update_status),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception as e:
        return {"error": str(e)}


def _env_truthy(name: str, default: str = "0") -> bool:
    v = str(os.environ.get(name, default) or default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _carrier_pickup_count_options() -> Tuple[bool, bool]:
    """(lenient khi thiếu mốc parse, tin full cửa sổ ES)."""
    lenient = _env_truthy("WEB_SHIPPED_COUNT_LENIENT_MISSING_PICKUP_TS", "1")
    trust_es = _env_truthy("WEB_SHIPPED_TRUST_CARRIER_PICKUP_ES_WINDOW", "0")
    return lenient, trust_es


def filter_orders_carrier_pickup_day(orders: Optional[List[Dict[str, Any]]], target_date: str) -> List[Dict[str, Any]]:
    """Đơn status=2 thuộc ngày ĐVVC lấy (cùng option env với đếm ĐVVC)."""
    if not orders:
        return []
    lenient, trust_es = _carrier_pickup_count_options()
    out: List[Dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        if order_counts_for_carrier_pickup_day(
            order,
            target_date,
            trust_carrier_window_when_pickup_unknown=lenient,
            trust_carrier_pickup_es_scope=trust_es,
        ):
            out.append(order)
    return out


def count_carrier_pickup_in_orders(orders: Optional[List[Dict[str, Any]]], target_date: str) -> int:
    return len(filter_orders_carrier_pickup_day(orders or [], target_date))


def get_shop_errors() -> List[Dict[str, Any]]:
    """Trả về danh sách shop có lỗi: thiếu API key hoặc inactive.
    Chỉ hiển thị cho admin trên dashboard."""
    from pancake_auth import is_valid_hex_api_key as _is_valid
    errors: List[Dict[str, Any]] = []
    try:
        from db import get_conn as _gc
        with _gc() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT shop_key, shop_name, pos_shop_id, pos_api_key, status "
                    "FROM wh_shops ORDER BY shop_key"
                )
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            key = str(r.get("pos_api_key") or "").strip()
            status = str(r.get("status") or "active")
            if status != "active":
                errors.append({**r, "error_type": "inactive"})
            elif not _is_valid(key):
                errors.append({**r, "error_type": "no_api_key"})
    except Exception:
        pass
    return errors


def get_kho_shipping_stats(allowed_shop_keys: Optional[set]) -> Dict[str, int]:
    """
    Số đơn đang giao (shipped) và đã xác nhận (confirmed) từ wh_outbound_requests.
    Trả về tích lũy (không filter ngày) — phản ánh trạng thái hiện tại.
    """
    try:
        from modules.kho_vat_ly.wh_db import wh_db
        params: List[Any] = []
        shop_cond = ""
        if allowed_shop_keys is not None:
            meta_map = load_shop_meta_map()
            allowed_names = [
                v["shop_name"] for k, v in meta_map.items()
                if k in allowed_shop_keys and v.get("shop_name") and v.get("status") == "active"
            ]
            if allowed_names:
                placeholders = ",".join(["%s"] * len(allowed_names))
                shop_cond = f" WHERE shop_name IN ({placeholders})"
                params.extend(allowed_names)
            elif allowed_shop_keys:
                return {"waiting": 0, "shipped": 0, "confirmed": 0, "outbound_confirmed": 0, "returns_received": 0}
        with wh_db() as conn:
            rows = conn.execute(
                f"SELECT pancake_status, COUNT(*) AS c FROM ("
                f"  SELECT DISTINCT shop_id, order_code, pancake_status"
                f"  FROM wh_outbound_requests{shop_cond}"
                f") sq GROUP BY pancake_status",
                params,
            ).fetchall()
            counts = {r["pancake_status"]: int(r["c"] or 0) for r in rows}

            # Đã xuất kho: đơn shipped/received mà kho đã scan xác nhận hết (status != pending)
            shop_and_cond = (" AND" + shop_cond.replace(" WHERE", "")) if shop_cond else ""
            outbound_confirmed = (conn.execute(
                f"SELECT COUNT(*) AS c FROM ("
                f"  SELECT shop_id, order_code, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pc"
                f"  FROM wh_outbound_requests"
                f"  WHERE pancake_status IN ('shipped','received'){shop_and_cond}"
                f"  GROUP BY shop_id, order_code"
                f") sq WHERE pc = 0",
                params,
            ).fetchone() or {}).get("c", 0)

            # Đã nhận hoàn: đơn hoàn mà kho đã nhận lại hết (không còn item pending)
            ret_shop_cond = shop_cond.replace("shop_name IN", "r.shop_name IN") if shop_cond else ""
            ret_cond_str = (" AND r.shop_name IN (" + ",".join(["%s"]*len(params)) + ")") if params else ""
            returns_received = (conn.execute(
                f"SELECT COUNT(*) AS c FROM ("
                f"  SELECT shop_id, order_code, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pc"
                f"  FROM wh_return_receipts"
                f"  WHERE 1=1{ret_cond_str}"
                f"  GROUP BY shop_id, order_code"
                f") sq WHERE pc = 0",
                params,
            ).fetchone() or {}).get("c", 0)

            return {
                "waiting":             counts.get("waiting", 0),
                "shipped":             counts.get("shipped", 0),
                "confirmed":           counts.get("confirmed", 0),
                "outbound_confirmed":  int(outbound_confirmed or 0),
                "returns_received":    int(returns_received or 0),
            }
    except Exception:
        return {"waiting": 0, "shipped": 0, "confirmed": 0, "outbound_confirmed": 0, "returns_received": 0}


def load_carrier_pickup_orders_map_from_db(
    selected_date: Optional[str],
    meta_map: Dict[str, Any],
) -> Dict[str, int]:
    """Trả về {shop_key: count} đơn ĐVVC đã lấy trong ngày (carrier_picked_up_at) per shop."""
    if not selected_date:
        return {}
    try:
        from modules.kho_vat_ly.wh_db import wh_db
        date_prefix = selected_date + "%"
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT shop_name, COUNT(DISTINCT order_code) AS c"
                " FROM wh_outbound_requests"
                " WHERE carrier_picked_up_at LIKE %s"
                " AND pancake_status IN ('shipped','received')"
                " GROUP BY shop_name",
                [date_prefix],
            ).fetchall()
        name_to_key: Dict[str, str] = {
            str(v.get("shop_name", "")).strip(): k
            for k, v in meta_map.items()
            if v.get("shop_name")
        }
        result: Dict[str, int] = {}
        for row in rows:
            sname = str(row["shop_name"] or "").strip()
            sk = name_to_key.get(sname)
            if sk:
                result[sk] = int(row["c"] or 0)
        return result
    except Exception:
        return {}


def sum_carrier_pickup_orders_dashboard(
    selected_date: str,
    allowed_shop_keys: Optional[set],
) -> Optional[int]:
    """
    Tổng đơn ĐVVC lấy trong ngày (toàn phạm vi dashboard).
    Ưu tiên query từ wh_outbound_requests (DB local, nhanh, luôn bật).
    Fallback sang Pancake API nếu DB lỗi VÀ WEB_DASHBOARD_CARRIER_PICKUP_KPI=1.
    """
    if not selected_date:
        return None

    # ── Fast path: DB local (wh_outbound_requests) ──────────────────────────
    try:
        from modules.kho_vat_ly.wh_db import wh_db
        date_prefix = selected_date + "%"
        params: List[Any] = [date_prefix]
        shop_cond = ""
        if allowed_shop_keys is not None:
            meta_map = load_shop_meta_map()
            allowed_names = [
                v["shop_name"] for k, v in meta_map.items()
                if k in allowed_shop_keys and v.get("shop_name") and v.get("status") == "active"
            ]
            if allowed_names:
                placeholders = ",".join(["%s"] * len(allowed_names))
                shop_cond = f" AND shop_name IN ({placeholders})"
                params.extend(allowed_names)
            elif allowed_shop_keys:
                return 0
        with wh_db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM ("
                f"  SELECT DISTINCT shop_id, order_code FROM wh_outbound_requests"
                f"  WHERE carrier_picked_up_at LIKE %s"
                f"  AND pancake_status IN ('shipped','received'){shop_cond}"
                f") sq",
                params,
            ).fetchone()
            return int((row["c"] if row else None) or 0)
    except Exception:
        pass

    # ── Fallback: Pancake API (chỉ khi bật env var) ─────────────────────────
    if not _env_truthy("WEB_DASHBOARD_CARRIER_PICKUP_KPI", "0"):
        return None
    ttl_seconds = max(
        15,
        int(str(os.getenv("WEB_DASHBOARD_CARRIER_PICKUP_CACHE_TTL_SECONDS", "900")).strip() or "900"),
    )
    cache_key = f"carrier_pickup|{selected_date}|{_home_alert_counts_scope_key(allowed_shop_keys)}"
    from redis_cache import cache_get, cache_set
    cached = cache_get(cache_key)
    if cached is not None:
        return int(cached.get("total", 0) or 0)

    shop_meta = get_visible_shop_meta_map() if has_request_context() else load_shop_meta_map()
    shop_ids: List[str] = []
    for shop_key, meta in shop_meta.items():
        if meta.get("status") != "active":
            continue
        if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
            continue
        sid = str(meta.get("shop_id", "") or "").strip()
        if sid:
            shop_ids.append(sid)
    if not shop_ids:
        return 0

    try:
        max_w = int(str(os.environ.get("WEB_PANCAKE_PARALLEL_WORKERS", "4") or "4").strip())
    except Exception:
        max_w = 4
    max_w = max(1, min(max_w, 16, len(shop_ids)))

    def _one(sid: str) -> int:
        orders, err = fetch_all_sent_orders(
            sid, selected_date, update_status="carrier_picked_up_at"
        )
        if err:
            return 0
        return count_carrier_pickup_in_orders(orders, selected_date)

    total = 0
    with ThreadPoolExecutor(max_workers=max_w) as ex:
        futs = [ex.submit(_one, sid) for sid in shop_ids]
        for fut in as_completed(futs):
            try:
                total += int(fut.result() or 0)
            except Exception:
                continue
    cache_set(cache_key, {"total": int(total)}, ttl=ttl_seconds)
    return total


def fetch_all_sent_orders(
    shop_id: str,
    target_date_str: str,
    *,
    update_status: str = "inserted_at",
    access_token: str = "",
    cookie: str = "",
):
    all_orders = []
    page = 1
    page_size = 100
    while True:
        data = request_sent_orders(
            shop_id,
            target_date_str,
            page=page,
            page_size=page_size,
            update_status=update_status,
        )
        if data.get("error"):
            return None, data["error"]
        orders = data.get("data", [])
        if not isinstance(orders, list):
            return None, "API không trả về danh sách đơn hàng"
        all_orders.extend(orders)
        if len(orders) < page_size:
            break
        page += 1
        if page > 100:
            break
    return all_orders, None

def aggregate_sent_items_from_orders(orders: List[Dict[str, Any]]):
    item_map: Dict[str, Dict[str, Any]] = {}
    total_orders = 0
    total_quantity = 0

    for order in orders:
        if not isinstance(order, dict):
            continue
        if int(order.get("status", -1)) != 2:
            continue

        total_orders += 1
        for item in order.get("items", []):
            if not isinstance(item, dict):
                continue
            quantity = item.get("quantity", 0) or 0
            try:
                quantity = float(quantity)
            except Exception:
                quantity = 0

            variation_info = item.get("variation_info", {}) or {}
            product_name = (
                variation_info.get("name")
                or item.get("note_product")
                or item.get("note_product_internal")
                or "Không rõ tên"
            )
            product_code = (
                variation_info.get("custom_id")
                or variation_info.get("product_id")
                or item.get("keyword_variation")
                or ""
            )
            variation_id = item.get("variation_id") or ""
            key = f"{product_name}|{product_code}|{variation_id}"

            if key not in item_map:
                item_map[key] = {
                    "product_name": product_name,
                    "product_code": product_code,
                    "variation_id": variation_id,
                    "quantity": 0,
                    "order_count": 0,
                }

            item_map[key]["quantity"] += quantity
            item_map[key]["order_count"] += 1
            total_quantity += quantity

    items = list(item_map.values())
    items.sort(key=lambda x: (-x["quantity"], -x["order_count"], x["product_name"]))
    return {
        "total_orders": total_orders,
        "total_quantity": total_quantity,
        "items": items,
    }

def _build_sent_items_from_db(target_date: str, selected_shop_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Fast path: query wh_outbound_requests từ PostgreSQL (< 100ms).
    Trả None nếu chưa có dữ liệu cho ngày này → fall back sang API.
    """
    try:
        from modules.kho_vat_ly.wh_db import wh_db
    except Exception:
        return None

    try:
        date_prefix = target_date + "%"
        shop_params: List[Any] = [selected_shop_key] if selected_shop_key else []
        shop_cond = " AND s.shop_key = ?" if selected_shop_key else ""

        with wh_db() as conn:
            # 1. Kiểm tra có dữ liệu không — dùng order_inserted_at thay vì carrier_picked_up_at
            # vì sáng sớm carrier chưa lấy hàng nên carrier_picked_up_at = 0 → ko nên fallback API
            chk = conn.execute(
                "SELECT COUNT(*) AS cnt FROM wh_outbound_requests o "
                "JOIN wh_shops s ON o.shop_id = s.id "
                "WHERE (o.order_inserted_at LIKE ? OR o.carrier_picked_up_at LIKE ?)"
                + shop_cond,
                [date_prefix, date_prefix] + shop_params,
            ).fetchone()
            if not chk or int(chk["cnt"] or 0) == 0:
                return None  # Kho chưa sync ngày này → gọi API

            # 2. Tổng hợp theo shop
            shop_rows = conn.execute(
                "SELECT s.shop_key, s.pos_shop_id, MAX(o.shop_name) AS shop_name, "
                "COUNT(DISTINCT o.order_id_external) AS carrier_pickup_orders, "
                "SUM(o.qty_ordered) AS total_quantity "
                "FROM wh_outbound_requests o JOIN wh_shops s ON o.shop_id = s.id "
                "WHERE o.carrier_picked_up_at LIKE ? AND o.pancake_status = 'shipped'"
                + shop_cond +
                " GROUP BY s.shop_key, s.pos_shop_id ORDER BY SUM(o.qty_ordered) DESC",
                [date_prefix] + shop_params,
            ).fetchall()

            # 3. Tổng hợp theo shop + sản phẩm
            prod_rows = conn.execute(
                "SELECT s.shop_key, o.product_sku, MAX(o.product_name) AS product_name, "
                "SUM(o.qty_ordered) AS qty, COUNT(DISTINCT o.order_id_external) AS order_count "
                "FROM wh_outbound_requests o JOIN wh_shops s ON o.shop_id = s.id "
                "WHERE o.carrier_picked_up_at LIKE ? AND o.pancake_status = 'shipped'"
                + shop_cond +
                " GROUP BY s.shop_key, o.product_sku ORDER BY s.shop_key, SUM(o.qty_ordered) DESC",
                [date_prefix] + shop_params,
            ).fetchall()

            # 4. Tổng đơn theo order_inserted_at (ngày đặt hàng)
            ins_rows = conn.execute(
                "SELECT s.shop_key, COUNT(DISTINCT o.order_id_external) AS total_orders "
                "FROM wh_outbound_requests o JOIN wh_shops s ON o.shop_id = s.id "
                "WHERE o.order_inserted_at LIKE ?"
                + shop_cond +
                " GROUP BY s.shop_key",
                [date_prefix] + shop_params,
            ).fetchall()
    except Exception:
        return None

    # Nhóm sản phẩm theo shop
    products_by_shop: Dict[str, List[Dict[str, Any]]] = {}
    for pr in prod_rows:
        sk = pr["shop_key"]
        qty = int(pr["qty"] or 0)
        oc = int(pr["order_count"] or 0)
        products_by_shop.setdefault(sk, []).append({
            "product_name": pr["product_name"] or "Không rõ tên",
            "product_code": pr["product_sku"] or "",
            "variation_id": "",
            "quantity": qty,
            "order_count": oc,
            "quantity_fmt": format_int(qty),
            "order_count_fmt": format_int(oc),
        })

    ins_by_shop = {r["shop_key"]: int(r["total_orders"] or 0) for r in ins_rows}
    shop_meta = get_visible_shop_meta_map()

    rows: List[Dict[str, Any]] = []
    total_orders = total_quantity = total_distinct = total_carrier_pickup_orders = 0
    combined_items: Dict[str, Any] = {}

    for sr in shop_rows:
        sk = sr["shop_key"]
        if sk not in shop_meta:
            continue
        meta = shop_meta.get(sk, {})
        shop_name = str(meta.get("shop_name") or sr["shop_name"] or sk)
        shop_id_pos = str(meta.get("shop_id") or sr["pos_shop_id"] or "")
        carrier_n = int(sr["carrier_pickup_orders"] or 0)
        total_qty = int(sr["total_quantity"] or 0)
        ins_n = ins_by_shop.get(sk, carrier_n)
        items = products_by_shop.get(sk, [])
        distinct = len(items)

        rows.append({
            "shop_key": sk,
            "shop_id": shop_id_pos,
            "shop_name": shop_name,
            "total_orders": ins_n,
            "total_quantity": total_qty,
            "distinct_items": distinct,
            "carrier_pickup_orders": carrier_n,
            "total_orders_fmt": format_int(ins_n),
            "total_quantity_fmt": format_int(total_qty),
            "distinct_items_fmt": format_int(distinct),
            "carrier_pickup_orders_fmt": format_int(carrier_n),
            "items": items,
            "error": "",
            "status": "Ổn định" if carrier_n > 0 else "Không có xuất",
            "status_class": "tag-good" if carrier_n > 0 else "tag-warn",
        })
        total_orders += ins_n
        total_quantity += total_qty
        total_distinct += distinct
        total_carrier_pickup_orders += carrier_n

        for item in items:
            key = f"{item['product_name']}|{item['product_code']}"
            if key not in combined_items:
                combined_items[key] = {
                    "product_name": item["product_name"],
                    "product_code": item["product_code"],
                    "quantity": 0,
                    "order_count": 0,
                    "shop_names": set(),
                }
            combined_items[key]["quantity"] += item["quantity"]
            combined_items[key]["order_count"] += item["order_count"]
            combined_items[key]["shop_names"].add(shop_name)

    rows.sort(key=lambda x: (-x["total_quantity"], -x["carrier_pickup_orders"], x["shop_name"]))
    combined_list = sorted(combined_items.values(), key=lambda x: (-x["quantity"], -x["order_count"], x["product_name"]))
    top_items = [
        {
            "shop_name": ", ".join(sorted(ci["shop_names"]))[:120],
            "product_name": ci["product_name"],
            "product_code": ci["product_code"],
            "quantity_fmt": format_int(ci["quantity"]),
            "order_count_fmt": format_int(ci["order_count"]),
        }
        for ci in combined_list[:20]
    ]

    return {
        "rows": rows,
        "top_items": top_items,
        "total_orders": total_orders,
        "total_quantity": total_quantity,
        "total_distinct": total_distinct,
        "total_carrier_pickup_orders": total_carrier_pickup_orders,
        "error": "",
        "target_date": target_date,
        "source": "db",
    }


_sent_items_cache: Dict[str, Any] = {}   # key → {data, expires_at}
_SENT_CACHE_TTL = 300                    # 5 phút


def build_sent_items_data(selected_date: Optional[str], selected_shop_key: Optional[str]):
    target_date = selected_date or today_hcm()

    # ─── Cache path: trả ngay nếu đã có trong bộ nhớ và chưa hết hạn ───
    cache_key = f"{target_date}|{selected_shop_key or ''}"
    cached = _sent_items_cache.get(cache_key)
    if cached and time.time() < cached["expires_at"]:
        return cached["data"]

    # ─── Fast path: SQLite DB (sync mỗi 2 phút bởi kho fast-sync) ───
    db_result = _build_sent_items_from_db(target_date, selected_shop_key)
    if db_result is not None:
        _sent_items_cache[cache_key] = {"data": db_result, "expires_at": time.time() + _SENT_CACHE_TTL}
        return db_result

    # ─── Slow path: gọi API Pancake POS song song (fallback khi DB chưa có data) ───
    shop_meta = get_visible_shop_meta_map()
    rows = []
    combined_items: Dict[str, Any] = {}
    total_orders = total_quantity = total_distinct = total_carrier_pickup_orders = 0

    selected_shops = [
        meta for sk, meta in shop_meta.items()
        if meta.get("status") == "active"
        and (not selected_shop_key or selected_shop_key == sk)
    ]

    def _fetch_one_shop(meta: Dict[str, Any]) -> Dict[str, Any]:
        shop_key = meta.get("shop_key", "")
        shop_id  = meta.get("shop_id", "")
        shop_name = meta.get("shop_name", shop_key)

        orders, error = fetch_all_sent_orders(shop_id, target_date)
        if error:
            return {
                "shop_key": shop_key, "shop_id": shop_id, "shop_name": shop_name,
                "total_orders": 0, "total_quantity": 0, "distinct_items": 0,
                "carrier_pickup_orders": 0,
                "total_orders_fmt": "0", "total_quantity_fmt": "0",
                "distinct_items_fmt": "0", "carrier_pickup_orders_fmt": "—",
                "items": [], "error": error, "status": "Lỗi", "status_class": "tag-warn",
            }

        agg_ins = aggregate_sent_items_from_orders(orders)
        pu_orders, pu_err = fetch_all_sent_orders(
            shop_id, target_date, update_status="carrier_picked_up_at"
        )
        if pu_err:
            orders_dvvc_: List[Dict[str, Any]] = []
            carrier_pickup_orders_fmt_ = "—"
        else:
            orders_dvvc_ = filter_orders_carrier_pickup_day(pu_orders, target_date)
            carrier_pickup_orders_fmt_ = format_int(len(orders_dvvc_))

        agg_dvvc = aggregate_sent_items_from_orders(orders_dvvc_)
        carrier_pickup_n_ = len(orders_dvvc_)
        status_ = "Ổn định" if agg_dvvc["total_orders"] > 0 else "Không có xuất"
        return {
            "shop_key": shop_key, "shop_id": shop_id, "shop_name": shop_name,
            "total_orders": agg_ins["total_orders"],
            "total_quantity": agg_dvvc["total_quantity"],
            "distinct_items": len(agg_dvvc["items"]),
            "carrier_pickup_orders": carrier_pickup_n_,
            "total_orders_fmt": format_int(agg_ins["total_orders"]),
            "total_quantity_fmt": format_int(agg_dvvc["total_quantity"]),
            "distinct_items_fmt": format_int(len(agg_dvvc["items"])),
            "carrier_pickup_orders_fmt": carrier_pickup_orders_fmt_,
            "items": agg_dvvc["items"],
            "error": "", "status": status_,
            "status_class": "tag-good" if agg_dvvc["total_orders"] > 0 else "tag-warn",
        }

    # Gọi song song tối đa 4 shop cùng lúc (giảm RAM)
    with ThreadPoolExecutor(max_workers=4) as pool:
        shop_results = list(pool.map(_fetch_one_shop, selected_shops))

    for row in shop_results:
        rows.append(row)
        total_orders  += row["total_orders"]
        total_quantity += row["total_quantity"]
        total_distinct += row["distinct_items"]
        total_carrier_pickup_orders += row["carrier_pickup_orders"]
        shop_name = row["shop_name"]
        for item in row.get("items", []):
            key = f"{item['product_name']}|{item['product_code']}|{item['variation_id']}"
            if key not in combined_items:
                combined_items[key] = {
                    "product_name": item["product_name"],
                    "product_code": item["product_code"],
                    "quantity": 0, "order_count": 0, "shop_names": set(),
                }
            combined_items[key]["quantity"] += item["quantity"]
            combined_items[key]["order_count"] += item["order_count"]
            combined_items[key]["shop_names"].add(shop_name)

    rows.sort(key=lambda x: (-x["total_quantity"], -x["carrier_pickup_orders"], x["shop_name"]))
    combined_list = sorted(combined_items.values(), key=lambda x: (-x["quantity"], -x["order_count"], x["product_name"]))
    top_items = [
        {
            "shop_name": ", ".join(sorted(ci["shop_names"]))[:120],
            "product_name": ci["product_name"],
            "product_code": ci["product_code"],
            "quantity_fmt": format_int(ci["quantity"]),
            "order_count_fmt": format_int(ci["order_count"]),
        }
        for ci in combined_list[:20]
    ]

    result = {
        "rows": rows, "top_items": top_items,
        "total_orders": total_orders, "total_quantity": total_quantity,
        "total_distinct": total_distinct,
        "total_carrier_pickup_orders": total_carrier_pickup_orders,
        "error": "", "target_date": target_date,
    }
    # Lưu cache để lần sau không gọi API nữa
    _sent_items_cache[cache_key] = {"data": result, "expires_at": time.time() + _SENT_CACHE_TTL}
    return result


def save_sent_items_excel(selected_date: str, selected_shop_key: Optional[str]):
    sent_data = build_sent_items_data(selected_date, selected_shop_key)

    exports_dir = os.path.join(BASE_DIR, "exports")
    os.makedirs(exports_dir, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Sent Items"

    ws.append([
        "Ngay",
        "Shop",
        "Ten san pham",
        "Ma san pham",
        "So luong da gui",
        "So don chua san pham",
    ])

    for shop in sent_data["rows"]:
        shop_name = shop["shop_name"]
        for item in shop["items"]:
            ws.append([
                selected_date,
                shop_name,
                item["product_name"],
                item["product_code"],
                int(round(float(item["quantity"]))),
                int(round(float(item["order_count"]))),
            ])

    if selected_shop_key:
        filename = f"sent_items_{selected_shop_key}_{selected_date}.xlsx"
    else:
        filename = f"sent_items_all_{selected_date}.xlsx"

    filepath = os.path.join(exports_dir, filename)
    wb.save(filepath)

    return filepath, filename



# ---------------------------------------------------------------------------
# Dashboard helper functions (lines 8985-9190 of original web_app.py)
# ---------------------------------------------------------------------------
def get_day_range_params_for_orders(target_date_str: str):
    from datetime import timezone as _tz
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    # VN midnight → UTC: subtract 7 hours
    start_utc = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0) - timedelta(hours=7)
    end_utc   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59) - timedelta(hours=7)
    # Use UTC-aware timestamps so server TZ does not cause off-by-7h shift
    start_ts = int(start_utc.replace(tzinfo=_tz.utc).timestamp())
    end_ts   = int(end_utc.replace(tzinfo=_tz.utc).timestamp())
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return start_ts, end_ts, start_iso, end_iso


def load_live_pos_status() -> Dict[str, Any]:
    """Đọc cache live status từ file JSON."""
    try:
        if os.path.exists(LIVE_STATUS_CACHE_FILE):
            with open(LIVE_STATUS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_live_pos_status(data: Dict[str, Any]):
    """Lưu live status vào cache file JSON."""
    try:
        with open(LIVE_STATUS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fetch_live_pos_status(allowed_shop_keys: Optional[set] = None) -> Dict[str, Any]:
    """Fetch cumulative order status counts từ Pancake POS API tất cả shops (không lọc ngày)."""
    totals: Dict[str, int] = {str(i): 0 for i in range(8)}
    totals["9"] = 0
    shop_results: List[Dict[str, Any]] = []
    # Background thread không có Flask request context → fallback dùng load_shop_meta_map()
    try:
        meta_map = get_visible_shop_meta_map()
    except RuntimeError:
        meta_map = load_shop_meta_map()
    for shop_key, meta in meta_map.items():
        if meta.get("status") != "active":
            continue
        if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
            continue
        shop_id = str(meta.get("shop_id", "") or "").strip()
        if not shop_id:
            continue
        api_key = _pancake_auth.get_shop_api_key(shop_id)
        if not api_key:
            shop_results.append({"shop_key": shop_key, "ok": False, "reason": "no_api_key"})
            continue
        try:
            url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"
            params = [
                ("api_key", api_key),
                ("page_size", 1),
                ("page", 1),
                ("es_only", "true"),
            ]
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://pos.pancake.vn",
                "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
                "User-Agent": "Mozilla/5.0",
            }
            res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
            data = res.json()
            buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
            shop_counts: Dict[str, int] = {}
            for bucket in buckets:
                key = str(bucket.get("key", ""))
                count = int(bucket.get("doc_count", 0) or 0)
                totals[key] = totals.get(key, 0) + count
                shop_counts[key] = count
            shop_results.append({"shop_key": shop_key, "ok": True, "counts": shop_counts})
        except Exception as exc:
            shop_results.append({"shop_key": shop_key, "ok": False, "reason": str(exc)})

    ok_count = sum(1 for r in shop_results if r.get("ok"))
    err_count = sum(1 for r in shop_results if not r.get("ok"))
    return {
        "status_counts": totals,
        "synced_at": now_hcm().strftime("%d/%m/%Y %H:%M"),
        "shop_count": ok_count,
        "error_count": err_count,
        "shop_results": shop_results,
    }


def request_order_status_aggs(shop_id: str, target_date_str: str,
                              access_token: str = "", cookie: str = ""):
    api_key = access_token or _pancake_auth.get_shop_api_key(str(shop_id))
    start_ts, end_ts, start_iso, end_iso = get_day_range_params_for_orders(target_date_str)

    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"

    params = [
        ("api_key", api_key),
        ("page_size", 1),
        ("page", 1),
        ("updateStatus", "inserted_at"),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
        ("es_only", "true"),
    ]

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=30)
        return res.json()
    except Exception:
        return {}


def get_order_status_summary_by_date(target_date_str: str, allowed_shop_keys: Optional[set] = None):
    if not target_date_str:
        return {
            "new": 0,
            "confirmed": 0,
            "sent": 0,
            "received": 0,
            "returning": 0,
            "returned": 0,
        }

    if is_db_order_status_enabled():
        db_summary = get_order_status_summary_by_date_from_db(target_date_str, allowed_shop_keys=allowed_shop_keys)
        if db_summary is not None:
            return db_summary

    if not os.path.exists(CONFIG_FILE):
        return {
            "new": 0,
            "confirmed": 0,
            "sent": 0,
            "received": 0,
            "returning": 0,
            "returned": 0,
        }


    try:
        config = try_parse_json(CONFIG_FILE)
    except Exception:
        return {
            "new": 0,
            "confirmed": 0,
            "sent": 0,
            "received": 0,
            "returning": 0,
            "returned": 0,
        }


    totals = {
        "new": 0,
        "confirmed": 0,
        "sent": 0,
        "received": 0,
        "returning": 0,
        "returned": 0,
    }

    shop_meta = get_visible_shop_meta_map()
    for shop_key, meta in shop_meta.items():
        if allowed_shop_keys is not None and shop_key not in allowed_shop_keys:
            continue
        if meta.get("status") != "active":
            continue

        shop_id = meta.get("shop_id", "")
        if not shop_id:
            continue

        data = request_order_status_aggs(shop_id, target_date_str)
        buckets = data.get("aggs", {}).get("status", {}).get("buckets", [])
        bucket_map = {}

        for bucket in buckets:
            bucket_map[str(bucket.get("key"))] = int(bucket.get("doc_count", 0))

        totals["new"] += bucket_map.get("0", 0)
        totals["confirmed"] += bucket_map.get("9", 0)
        totals["sent"] += bucket_map.get("2", 0)
        totals["received"] += bucket_map.get("3", 0)
        totals["returning"] += bucket_map.get("4", 0)
        totals["returned"] += bucket_map.get("5", 0)

    return totals


# ---------------------------------------------------------------------------
# render_page: wrap render_template_string with PAGE_TEMPLATE + context vars
# ---------------------------------------------------------------------------
def render_page(title: str, body: str, **kwargs):
    from flask import render_template_string, g
    from app_constants import PAGE_TEMPLATE, DASHBOARD_WEB_VERSION

    def has_module(key: str) -> bool:
        allowed = getattr(g, "allowed_modules", None)
        return allowed is None or key in allowed

    return render_template_string(
        PAGE_TEMPLATE,
        title=title,
        body=body,
        dashboard_web_version=DASHBOARD_WEB_VERSION,
        has_module=has_module,
        **kwargs,
    )
