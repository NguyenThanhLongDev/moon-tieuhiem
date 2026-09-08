from __future__ import annotations
import csv
import decimal
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from functools import wraps
try:
    from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

logger = logging.getLogger(__name__)

chi_phi_qc_bp = Blueprint(
    "chi_phi_qc", __name__,
    template_folder="templates",
    url_prefix="/chi-phi-qc",
)

# ── Auth guard ───────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get("username")
        if not session.get("user_id") and not username:
            return redirect(f"/login?next=/chi-phi-qc/")
        # Backfill user_id into session if it's missing (legacy sessions)
        if not session.get("user_id") and username:
            for u in _load_users_json():
                if str(u.get("username", "")).strip() == username.strip():
                    session["user_id"] = u.get("id")
                    session["full_name"] = u.get("full_name", "") or username
                    break
        return f(*args, **kwargs)
    return decorated

# ── Role helpers ─────────────────────────────────────────────────────
_SENIOR_ROLES    = {"admin", "superadmin", "manager", "ketoan", "accountant", "leader", "it"}
_FULL_VIEW_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}
# Role KHÔNG chạy quảng cáo → không hiện trong tab "Phân theo nhân viên"
# (kho/sale/sale_leader/it/packing: không phụ trách TK QC nên ẩn cho gọn).
_NON_ADS_ROLES   = {"kho", "sale", "sale_leader", "it", "packing"}

def _is_senior(role):
    return role in _SENIOR_ROLES

def _is_full_view(role):
    """Admin/kế toán see everything."""
    return role in _FULL_VIEW_ROLES

# ── User/team helpers from users.json ────────────────────────────────
import os as _os

def _load_users_json() -> list:
    """Load all users từ DB (ưu tiên) hoặc users.json (fallback)."""
    try:
        import sys
        _base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        sys.path.insert(0, _base)
        from user_helpers import load_all_users
        return load_all_users()
    except Exception:
        pass
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    path = _os.path.join(base, "users.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("users", [])
    except Exception:
        return []

def _normalize_shops(value) -> list:
    """Convert assigned_shops to a list of shop_keys, excluding '*'."""
    if isinstance(value, list):
        return [s.strip() for s in value if s and str(s).strip() not in ("", "*")]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip() and s.strip() != "*"]
    return []

def _get_user_by_id(user_id) -> dict:
    for u in _load_users_json():
        if str(u.get("id", "")) == str(user_id):
            return u
    return {}

def _get_team_members(leader_team_id: str) -> list:
    """Return all active staff+leader in the given team_id (from users.json).
    Bao gồm cả NV đã nghỉ — caller tự lọc theo ngày nếu cần (xem _is_member_visible)."""
    if not leader_team_id:
        return []
    return [
        u for u in _load_users_json()
        if u.get("status", "active") == "active"
        and str(u.get("team_id", "")).strip() == leader_team_id
    ]

def _get_allowed_shop_keys_for_user(user_id, role) -> list | None:
    """
    Returns the set of shop_keys this user can see, or None (= all shops).
    admin/ketoan/manager/superadmin → None (all)
    leader → all shops of their team members (union)
    staff  → only their assigned_shops
    """
    if _is_full_view(role):
        return None  # no filter

    user = _get_user_by_id(user_id)
    if not user:
        return []

    if role == "leader":
        team_id = str(user.get("team_id", "")).strip()
        members = _get_team_members(team_id) if team_id else [user]
        shops: set = set()
        for m in members:
            shops.update(_normalize_shops(m.get("assigned_shops", [])))
        # also include leader's own assigned_shops
        shops.update(_normalize_shops(user.get("assigned_shops", [])))
        return list(shops) if shops else []

    # staff / other
    shops = _normalize_shops(user.get("assigned_shops", []))
    return shops

def _shop_keys_to_account_ids(shop_keys: list, date_from: str = "", date_to: str = "") -> list:
    """Map shop_keys → fb_ad_account_id via fb_ad_account_mappings.

    Date-aware: có date_from/date_to → lấy mapping theo CỬA SỔ HIỆU LỰC giao với kỳ
    (bảng versioned migration 036; row đóng có status='inactive' nên KHÔNG filter
    status ở nhánh này). Vd TK AnhTh20.1 map shop Nam Leader từ 1/6: lọc tháng 5
    KHÔNG trả TK này cho shop Nam Leader → spend tháng 5 ở lại với chủ cũ (anhth).

    Không có ngày → giữ hành vi cũ: mapping active hiện tại.
    """
    if not shop_keys:
        return []
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if date_from and date_to:
                    cur.execute("""
                        SELECT DISTINCT m.fb_ad_account_id
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE s.shop_key = ANY(%s)
                          AND m.assigned_from <= %s::date
                          AND (m.assigned_to IS NULL OR m.assigned_to >= %s::date)
                    """, (shop_keys, date_to, date_from))
                else:
                    cur.execute("""
                        SELECT DISTINCT m.fb_ad_account_id
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE s.shop_key = ANY(%s) AND m.status = 'active'
                    """, (shop_keys,))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_shop_keys_to_account_ids error: %s", exc)
        return []


def _user_direct_account_ids(user_id, date_from: str = "", date_to: str = "") -> list:
    """Return ad_account_ids directly assigned to this user trong khoảng [date_from, date_to].

    Date-aware: đọc từ `user_ad_account_assignments` (uaa) và filter theo overlap với date range.
    → Báo cáo lịch sử (lọc ngày trước) hiển thị đúng chủ TẠI THỜI ĐIỂM ĐÓ, không phải chủ hiện tại.

    Nếu date_from/date_to rỗng → fallback: trả về TK active (assigned_to IS NULL) hiện tại.
    Dùng cho ngữ cảnh không có date filter (vd: cache/permissions).
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if date_from and date_to:
                    # Overlap rule: TK có uaa giao với khoảng filter
                    # (uaa.assigned_from <= date_to) AND (uaa.assigned_to IS NULL OR uaa.assigned_to >= date_from)
                    cur.execute("""
                        SELECT DISTINCT ad_account_id
                          FROM user_ad_account_assignments
                         WHERE user_id = %s
                           AND assigned_from <= %s::date
                           AND (assigned_to IS NULL OR assigned_to >= %s::date)
                    """, (int(user_id), date_to, date_from))
                else:
                    # Fallback: chỉ TK active hiện tại
                    cur.execute("""
                        SELECT DISTINCT ad_account_id
                          FROM user_ad_account_assignments
                         WHERE user_id = %s AND assigned_to IS NULL
                    """, (int(user_id),))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_user_direct_account_ids error: %s", exc)
        return []

def _build_team_groups(leader_user_id, leader_team_id: str) -> list:
    """
    For leader/admin view: return list of {user, shop_keys, account_ids}
    so the template can group pages by employee.
    """
    members = _get_team_members(leader_team_id) if leader_team_id else []
    groups = []
    for m in members:
        shops = _normalize_shops(m.get("assigned_shops", []))
        acc_ids = _shop_keys_to_account_ids(shops)
        groups.append({
            "user_id":   m.get("id"),
            "username":  m.get("username", ""),
            "full_name": m.get("full_name", "") or m.get("username", ""),
            "role":      m.get("role", "staff"),
            "team_id":   m.get("team_id", ""),
            "shop_keys": shops,
            "account_ids": acc_ids,
        })
    return groups

# ── DB helpers ───────────────────────────────────────────────────────
def _active_pages_for_user(user_id, role, allowed_account_ids=None):
    """Return distinct active pages. Each page appears once with a JSON-agg of ad accounts.
    allowed_account_ids=None → no restriction (admin/ketoan).
    allowed_account_ids=[...] → filter pages to those mapped to these ad account IDs (leader/staff).
    """
    _PAGE_AGG = """
        SELECT
            p.page_id,
            p.page_name,
            p.picture_url,
            p.page_category,
            COALESCE(
                json_agg(
                    json_build_object(
                        'ad_account_id',   m.ad_account_id,
                        'ad_account_name', m.ad_account_name,
                        'shop_key',        m.shop_key
                    ) ORDER BY m.ad_account_name
                ) FILTER (WHERE m.ad_account_id IS NOT NULL),
                '[]'::json
            ) AS ad_accounts
        FROM fb_pages p
        {join_clause}
        WHERE p.is_active = TRUE {extra_where}
        GROUP BY p.page_id, p.page_name, p.picture_url, p.page_category
        ORDER BY p.page_name
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_full_view(role):
                    # Admin/ketoan/manager: all pages, no filter
                    sql = _PAGE_AGG.format(
                        join_clause="LEFT JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                        extra_where="",
                    )
                    cur.execute(sql)
                elif role == "leader":
                    # Leader: pages whose ad accounts belong to their team
                    if not allowed_account_ids:
                        return []
                    sql = _PAGE_AGG.format(
                        join_clause="JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                        extra_where="AND m.ad_account_id = ANY(%s)",
                    )
                    cur.execute(sql, (allowed_account_ids,))
                else:
                    # Staff: use account-based filter if they have mapped accounts
                    # (same logic as leader), otherwise return empty (fb_page_assignments
                    # uses bigint user_id which is incompatible with string user IDs)
                    if allowed_account_ids:
                        sql = _PAGE_AGG.format(
                            join_clause="JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                            extra_where="AND m.ad_account_id = ANY(%s)",
                        )
                        cur.execute(sql, (allowed_account_ids,))
                    else:
                        return []
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_active_pages_for_user error: %s", exc)
        return []


def _daily_spend_by_page(date_from: str, date_to: str, user_id, role):
    """Return spend aggregated by page_id for a given date range (inclusive).
    Returns dict: {page_id: {win_chua, win_co, test_chua, test_co, tong, count}}
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    if role == "leader":
                        # Use JSON-based team members (DB users table may be empty)
                        team_ids = _get_leader_team_member_ids(user_id)
                        placeholders = ",".join(["%s"] * len(team_ids))
                        user_filter = f"AND d.user_id IN ({placeholders})"
                        params = [date_from, date_to] + team_ids
                    else:
                        user_filter = ""
                        params = [date_from, date_to]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [date_from, date_to, user_id]

                cur.execute(f"""
                    SELECT
                        d.page_id,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.tien_chua_thue ELSE 0 END), 0) as win_chua,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.thanh_tien     ELSE 0 END), 0) as win_co,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.tien_chua_thue ELSE 0 END), 0) as test_chua,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.thanh_tien     ELSE 0 END), 0) as test_co,
                        COALESCE(SUM(d.thanh_tien), 0) as tong,
                        COUNT(d.id) as count
                    FROM fb_page_spend_declarations d
                    WHERE d.spend_date BETWEEN %s AND %s {user_filter}
                    GROUP BY d.page_id
                """, params)
                result = {}
                for row in cur.fetchall():
                    pid = row[0]
                    result[pid] = {
                        "win_chua": float(row[1]),
                        "win_co":   float(row[2]),
                        "test_chua":float(row[3]),
                        "test_co":  float(row[4]),
                        "tong":     float(row[5]),
                        "count":    int(row[6]),
                    }
                return result
    except Exception as exc:
        logger.error("_daily_spend_by_page error: %s", exc)
        return {}


def _declarations_for_page_date(page_id, spend_date, user_id, role):
    """Return all declarations for a specific page + date (for the detail modal)."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    user_filter = ""
                    params = [page_id, spend_date]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [page_id, spend_date, user_id]

                cur.execute(f"""
                    SELECT d.id, d.spend_date, d.ad_account_id, d.ad_account_name,
                           d.phan_loai, d.tien_chua_thue, d.thue_rate, d.thanh_tien,
                           d.note, d.created_at, d.user_id, d.page_name_manual
                    FROM fb_page_spend_declarations d
                    WHERE d.page_id = %s AND d.spend_date = %s {user_filter}
                    ORDER BY d.created_at DESC
                """, params)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                # Enrich user info from users.json (users DB table may be empty)
                uid_to_user = {str(u.get("id", "")): u for u in _load_users_json()}
                for row in rows:
                    uid = str(row.get("user_id") or "")
                    u = uid_to_user.get(uid, {})
                    row["full_name"] = u.get("full_name") or u.get("username") or uid
                    row["username"]  = u.get("username") or uid
                return rows
    except Exception as exc:
        logger.error("_declarations_for_page_date error: %s", exc)
        return []


def _page_ad_accounts(page_id: str):
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ad_account_id, ad_account_name, shop_key
                    FROM fb_page_ad_account_map
                    WHERE page_id = %s
                    ORDER BY ad_account_name
                """, (page_id,))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_page_ad_accounts error: %s", exc)
        return []


def _kpi_spend_by_page(date_from: str, date_to: str) -> dict:
    """Get actual Facebook spend from fb_ads_daily_metrics per page over a date range.

    Links via:
      1. fb_page_ad_account_map.ad_account_id  → fb_ads_daily_metrics.fb_ad_account_id  (direct)
      2. fb_page_ad_account_map.shop_key → shops.shop_key → shops.id → fb_ads_daily_metrics.shop_id
    Returns {page_id: {cp_ads_fb, cp_ads_fb_vat, impressions, clicks, fb_account_names}}
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # `fb_ads_daily_metrics` lưu trùng spend cho mỗi shop nếu ad account
                # map vào nhiều shop (cùng số liệu nhân N). Direct-match: dedup bằng MAX
                # THEO TỪNG NGÀY rồi SUM qua các ngày (range), tránh nhân trùng per-shop.
                # shop_key fallback giữ SUM vì trong 1 shop các ad_account_id khác nhau
                # là row khác nhau (không trùng).
                cur.execute("""
                    SELECT
                        m.page_id,
                        COALESCE(
                            -- direct ad_account_id match: MAX/ngày rồi SUM cả range
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.spend) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            -- fallback via shop_key
                            (SELECT SUM(dm.spend)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS cp_ads_fb,
                        COALESCE(
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.impressions) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            (SELECT SUM(dm.impressions)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS impressions,
                        COALESCE(
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.clicks) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            (SELECT SUM(dm.clicks)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS clicks,
                        COALESCE(
                            (SELECT string_agg(DISTINCT dm.account_name, ', ')
                             FROM fb_ads_daily_metrics dm
                             WHERE dm.fb_ad_account_id = m.ad_account_id
                               AND dm.metric_date BETWEEN %s AND %s),
                            (SELECT string_agg(DISTINCT dm.account_name, ', ')
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            ''
                        ) AS fb_account_names
                    FROM fb_page_ad_account_map m
                """, (date_from, date_to) * 8)
                result = {}
                for row in cur.fetchall():
                    fb = float(row[1] or 0)
                    result[row[0]] = {
                        "cp_ads_fb":     fb,
                        "cp_ads_fb_vat": round(fb * 1.113, 0),
                        "impressions":   int(row[2] or 0),
                        "clicks":        int(row[3] or 0),
                        "fb_account_names": row[4] or "",
                    }
                return result
    except Exception as exc:
        logger.error("_kpi_spend_by_page error: %s", exc)
        return {}


def _my_ad_accounts(user_id):
    """Return ad accounts mapped to this user from user_ad_account_map."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, ad_account_id, ad_account_name
                    FROM user_ad_account_map
                    WHERE user_id = %s
                    ORDER BY ad_account_name
                """, (user_id,))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_my_ad_accounts error: %s", exc)
        return []


def _load_account_meta(date_from: str = "", date_to: str = "") -> dict:
    """Return dict keyed by ad_account_id with name + assigned employee info.

    Merges data from:
      1. fb_ads_daily_metrics.account_name  (real names from FB API)
      2. user_ad_account_assignments (time-aware: NV phụ trách TK tại ngày đang xem)
         Fallback: user_ad_account_map nếu không có date range
    Result: {account_id: {"name": str, "owner_id": int|None, "owner_name": str|None, "owner_username": str|None}}
    """
    meta: dict = {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Account names from metrics table (populated by sync_facebook_ads_to_db)
                cur.execute("""
                    SELECT DISTINCT fb_ad_account_id, account_name
                    FROM fb_ads_daily_metrics
                    WHERE account_name IS NOT NULL AND account_name <> ''
                """)
                for aid, aname in cur.fetchall():
                    meta.setdefault(aid, {})["name"] = aname

                # 2. Account names from fb_ad_account_info cache (populated by sync_fb_ads_by_page)
                try:
                    cur.execute("""
                        SELECT ad_account_id, account_name
                        FROM fb_ad_account_info
                        WHERE account_name IS NOT NULL AND account_name <> ''
                    """)
                    for aid, aname in cur.fetchall():
                        meta.setdefault(aid, {}).setdefault("name", aname)
                except Exception:
                    pass  # table may not exist yet

                # 3. Employee assignment — time-aware nếu có date range,
                #    fallback user_ad_account_map (snapshot hiện tại) khi không có ngày.
                uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
                if date_from and date_to:
                    # Time-aware: lấy NV phụ trách TK trong khoảng [date_from, date_to]
                    # Nếu giữa kỳ đổi NV → lấy NV có window CUỐI (ORDER BY assigned_from DESC)
                    # để ưu tiên NV mới nhất trong khoảng (DISTINCT ON lấy dòng đầu tiên theo thứ tự).
                    cur.execute("""
                        SELECT DISTINCT ON (ad_account_id)
                               ad_account_id, ad_account_name, user_id
                          FROM user_ad_account_assignments
                         WHERE assigned_from <= %s::date
                           AND (assigned_to IS NULL OR assigned_to >= %s::date)
                         ORDER BY ad_account_id, assigned_from DESC
                    """, (date_to, date_from))
                else:
                    # Fallback: snapshot hiện tại từ user_ad_account_map
                    cur.execute("""
                        SELECT ad_account_id, ad_account_name, user_id
                          FROM user_ad_account_map
                         ORDER BY ad_account_id
                    """)
                for aid, aname, uid in cur.fetchall():
                    entry = meta.setdefault(aid, {})
                    if "name" not in entry and aname:
                        entry["name"] = aname
                    u = uid_to_user.get(str(uid) if uid else "", {})
                    entry["owner_id"]       = str(uid) if uid else None
                    entry["owner_name"]     = u.get("full_name") or u.get("username") or str(uid or "")
                    entry["owner_username"] = u.get("username") or str(uid or "")

                # 3. Fallback: nếu ad account chưa có owner → lấy từ fb_ad_account_mappings → shops
                #    (shop_name = username NV, vì mỗi shop gắn 1 NV)
                try:
                    cur.execute("""
                        SELECT m.fb_ad_account_id, s.shop_name, s.shop_key
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE m.status = 'active'
                    """)
                    for aid, shop_name, shop_key in cur.fetchall():
                        entry = meta.setdefault(aid, {})
                        if not entry.get("owner_name"):
                            display = shop_name or shop_key or ""
                            entry["owner_id"]       = None
                            entry["owner_name"]     = display
                            entry["owner_username"] = display
                except Exception as _fe:
                    logger.debug("fb_ad_account_mappings fallback skip: %s", _fe)
    except Exception as exc:
        logger.error("_load_account_meta error: %s", exc)
    return meta


def _serialize(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj


# ── Time-aware: tách TK theo từng NV phụ trách trong khoảng lọc ──
def _account_owner_segments(date_from: str, date_to: str) -> dict:
    """Trả về {ad_account_id: [list segments]} cho các TK có assignment overlap
    với khoảng [date_from, date_to]. Segment được clamp về khoảng lọc.

    Mỗi segment: {owner_id, owner_name, owner_username, name_snapshot,
                  seg_from (date), seg_to (date)}
    Dùng để: khi TK đổi tay GIỮA kỳ → trả về nhiều segments → UI tách thành nhiều dòng.
    """
    segments: dict = {}
    if not date_from or not date_to:
        return segments
    try:
        from db import get_conn
        df = date.fromisoformat(date_from)
        dt = date.fromisoformat(date_to)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ad_account_id, ad_account_name, user_id, assigned_from, assigned_to
                      FROM user_ad_account_assignments
                     WHERE assigned_from <= %s::date
                       AND (assigned_to IS NULL OR assigned_to >= %s::date)
                     ORDER BY ad_account_id, assigned_from
                """, (date_to, date_from))
                uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
                for aid, aname, uid, a_from, a_to in cur.fetchall():
                    user_info = uid_to_user.get(str(uid) if uid else "", {})
                    seg_from = max(a_from, df) if a_from else df
                    seg_to   = min(a_to, dt) if a_to else dt
                    if seg_from > seg_to:
                        continue
                    segments.setdefault(aid, []).append({
                        "owner_id":       str(uid) if uid else None,
                        "owner_name":     user_info.get("full_name") or user_info.get("username") or str(uid or ""),
                        "owner_username": user_info.get("username") or str(uid or ""),
                        "name_snapshot":  aname or "",
                        "seg_from":       seg_from,
                        "seg_to":         seg_to,
                    })
    except Exception as exc:
        logger.error("_account_owner_segments error: %s", exc)
    return segments


def _page_account_spend_in_range(page_id: str, account_id: str,
                                 seg_from, seg_to) -> dict:
    """Lấy spend/impressions/clicks cho 1 cặp (page, account) trong sub-range.
    Dùng khi tách dòng theo segment NV phụ trách.
    """
    out = {"spend": 0.0, "impressions": 0, "clicks": 0}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(spend),0), COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0)
                      FROM fb_ads_page_daily_spend
                     WHERE page_id = %s AND fb_ad_account_id = %s
                       AND metric_date BETWEEN %s AND %s
                """, (page_id, account_id, seg_from, seg_to))
                row = cur.fetchone()
                if row:
                    out = {"spend": float(row[0] or 0),
                           "impressions": int(row[1] or 0),
                           "clicks": int(row[2] or 0)}
    except Exception as exc:
        logger.error("_page_account_spend_in_range error: %s", exc)
    return out


def _split_page_entry_by_owner(entry: dict, owner_segments: dict,
                               date_from: str, date_to: str) -> list:
    """Nếu TK của page entry có nhiều NV phụ trách trong khoảng → tách thành N entries.
    Mỗi entry: dùng owner riêng + spend cộng dồn trong segment đó.
    Trả về list 1 phần tử (giữ nguyên) hoặc N phần tử (đã tách).
    """
    accs = entry.get("ad_accounts") or []
    if not accs:
        return [entry]
    aid = (accs[0].get("ad_account_id") or "")
    segs = owner_segments.get(aid) or []
    if len(segs) <= 1:
        return [entry]
    # Tách: mỗi segment 1 entry
    result = []
    seg_total_spend = 0.0   # cộng dồn spend đã gán segment → để tính phần GAP còn lại
    seg_total_impr  = 0
    seg_total_click = 0
    for seg in segs:
        sub_spend = _page_account_spend_in_range(
            entry["page_id"], aid, seg["seg_from"], seg["seg_to"]
        )
        seg_total_spend += float(sub_spend["spend"] or 0)
        seg_total_impr  += int(sub_spend["impressions"] or 0)
        seg_total_click += int(sub_spend["clicks"] or 0)
        new_acc = {
            **accs[0],
            "ad_account_name": seg.get("name_snapshot") or accs[0].get("ad_account_name") or aid,
            "owner_id":       seg["owner_id"],
            "owner_name":     seg["owner_name"],
            "owner_username": seg["owner_username"],
            "segment_from":   seg["seg_from"].isoformat() if hasattr(seg["seg_from"],"isoformat") else str(seg["seg_from"]),
            "segment_to":     seg["seg_to"].isoformat() if hasattr(seg["seg_to"],"isoformat") else str(seg["seg_to"]),
        }
        new_entry = {**entry}
        new_entry["ad_accounts"] = [new_acc] + accs[1:]  # giữ TK khác nếu có
        new_entry["kpi"] = {
            **(entry.get("kpi") or {}),
            "cp_ads_fb":     sub_spend["spend"],
            "cp_ads_fb_vat": round(sub_spend["spend"] * 1.113, 0),
            "impressions":   sub_spend["impressions"],
            "clicks":        sub_spend["clicks"],
        }
        new_entry["owner_segment"] = {
            "from": new_acc["segment_from"],
            "to":   new_acc["segment_to"],
            "owner_name": seg["owner_name"],
        }
        result.append(new_entry)

    # ── GAP: ngày TK chạy ads nhưng CHƯA gán NV nào (nằm NGOÀI mọi segment) ──
    # Trước đây phần này bị BỎ RƠI khỏi tổng → index thấp hơn báo cáo (~100tr ở kỳ 1-15).
    # Gom vào 1 dòng "⚠ Chưa gán NV" — đối xứng bucket "_unbound" bên báo cáo →
    # tổng index = tổng page-level = báo cáo. (gap = spend cả kỳ − Σ segment)
    full = _page_account_spend_in_range(entry["page_id"], aid, date_from, date_to)
    gap_spend = float(full["spend"] or 0) - seg_total_spend
    if gap_spend > 1.0:
        gap_acc = {
            **accs[0],
            "ad_account_name": accs[0].get("ad_account_name") or aid,
            "owner_id":       None,
            "owner_name":     "⚠ Chưa gán NV",
            "owner_username": "",
            "segment_from":   date_from,
            "segment_to":     date_to,
        }
        gap_entry = {**entry}
        gap_entry["ad_accounts"] = [gap_acc] + accs[1:]
        gap_entry["kpi"] = {
            **(entry.get("kpi") or {}),
            "cp_ads_fb":     gap_spend,
            "cp_ads_fb_vat": round(gap_spend * 1.113, 0),
            "impressions":   max(0, int(full["impressions"] or 0) - seg_total_impr),
            "clicks":        max(0, int(full["clicks"] or 0) - seg_total_click),
        }
        gap_entry["owner_segment"] = {
            "from": date_from, "to": date_to, "owner_name": "⚠ Chưa gán NV",
        }
        result.append(gap_entry)
    return result


# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════

def _auto_pages_for_date(date_from: str, date_to: str, allowed_account_ids: list | None = None) -> list:
    """Load pages discovered from fb_ads_page_daily_spend for a date range (inclusive).
    allowed_account_ids=None → no filter (admin/ketoan).
    allowed_account_ids=[]   → no accounts allowed (empty result).
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if allowed_account_ids is None:
                    cur.execute("""
                        SELECT
                            s.page_id,
                            COALESCE(
                                NULLIF(fpn.name, ''),
                                NULLIF(p.page_name, ''),
                                NULLIF(MAX(s.page_name), ''),
                                'Page ' || RIGHT(s.page_id, 8)
                            ) AS page_name,
                            p.picture_url,
                            p.page_category,
                            s.fb_ad_account_id,
                            SUM(s.spend)       AS spend,
                            SUM(s.impressions) AS impressions,
                            SUM(s.clicks)      AS clicks
                        FROM fb_ads_page_daily_spend s
                        LEFT JOIN fb_pages p ON p.page_id = s.page_id
                        LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                        WHERE s.metric_date BETWEEN %s AND %s
                        GROUP BY s.page_id, fpn.name, p.page_name, p.picture_url, p.page_category, s.fb_ad_account_id
                        ORDER BY SUM(s.spend) DESC
                    """, (date_from, date_to))
                elif not allowed_account_ids:
                    return []
                else:
                    cur.execute("""
                        SELECT
                            s.page_id,
                            COALESCE(
                                NULLIF(fpn.name, ''),
                                NULLIF(p.page_name, ''),
                                NULLIF(MAX(s.page_name), ''),
                                'Page ' || RIGHT(s.page_id, 8)
                            ) AS page_name,
                            p.picture_url,
                            p.page_category,
                            s.fb_ad_account_id,
                            SUM(s.spend)       AS spend,
                            SUM(s.impressions) AS impressions,
                            SUM(s.clicks)      AS clicks
                        FROM fb_ads_page_daily_spend s
                        LEFT JOIN fb_pages p ON p.page_id = s.page_id
                        LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                        WHERE s.metric_date BETWEEN %s AND %s
                          AND s.fb_ad_account_id = ANY(%s)
                        GROUP BY s.page_id, fpn.name, p.page_name, p.picture_url, p.page_category, s.fb_ad_account_id
                        ORDER BY SUM(s.spend) DESC
                    """, (date_from, date_to, allowed_account_ids))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_auto_pages_for_date error: %s", exc)
        return []


def _has_page_spend_table() -> bool:
    """Check if fb_ads_page_daily_spend table exists."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'fb_ads_page_daily_spend'
                """)
                return cur.fetchone() is not None
    except Exception:
        return False


def _ensure_auto_phan_loai_table():
    """Create fb_page_auto_phan_loai table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_page_auto_phan_loai (
                        page_id    VARCHAR(100) NOT NULL,
                        date       DATE         NOT NULL,
                        phan_loai  VARCHAR(20)  NOT NULL DEFAULT 'ma_win',
                        updated_by VARCHAR(60),
                        updated_at TIMESTAMP    DEFAULT NOW(),
                        PRIMARY KEY (page_id, date)
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_page_auto_phan_loai: %s", e)


def _ensure_sync_log_table():
    """Create fb_sync_log table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_sync_log (
                        id         SERIAL PRIMARY KEY,
                        sync_date  DATE         NOT NULL,
                        synced_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
                        synced_by  VARCHAR(60),
                        status     VARCHAR(20)  NOT NULL DEFAULT 'ok',
                        message    TEXT
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_sync_log: %s", e)


def _log_sync(sync_date: str, synced_by: str, status: str = "ok", message: str = ""):
    """Record a sync event in fb_sync_log."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_sync_log (sync_date, synced_by, status, message)
                    VALUES (%s, %s, %s, %s)
                """, (sync_date, synced_by, status, message[:500] if message else ""))
            conn.commit()
    except Exception as e:
        logger.warning("Could not log sync: %s", e)


def _get_last_sync(sync_date: str) -> dict | None:
    """Return the most recent sync record for a given date, or None."""
    try:
        _ensure_sync_log_table()
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT synced_at, synced_by, status
                    FROM fb_sync_log
                    WHERE sync_date = %s AND status = 'ok'
                    ORDER BY synced_at DESC
                    LIMIT 1
                """, (sync_date,))
                row = cur.fetchone()
                if row:
                    return {"synced_at": row[0], "synced_by": row[1], "status": row[2]}
    except Exception as e:
        logger.warning("Could not get last sync: %s", e)
    return None


def _load_auto_phan_loai_map(date_from: str, date_to: str) -> dict:
    """Return {page_id: 'ma_win'|'ma_test'} for a date range. Default 'ma_win' if not set.
    Nếu 1 page có phân loại khác nhau giữa các ngày → lấy ngày mới nhất trong khoảng."""
    try:
        from db import get_conn
        _ensure_auto_phan_loai_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (page_id) page_id, phan_loai
                    FROM fb_page_auto_phan_loai
                    WHERE date BETWEEN %s AND %s
                    ORDER BY page_id, date DESC
                """, (date_from, date_to))
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not load auto phan_loai: %s", e)
        return {}


def _save_auto_phan_loai(page_id: str, selected_date: str, phan_loai: str, user_id: str):
    """Upsert Win/Test choice for an auto-detected page on a given date."""
    from db import get_conn
    _ensure_auto_phan_loai_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_page_auto_phan_loai (page_id, date, phan_loai, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (page_id, date) DO UPDATE
                    SET phan_loai  = EXCLUDED.phan_loai,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
            """, (page_id, selected_date, phan_loai, user_id))
        conn.commit()


# ── VAT rate per page (6.1% hoặc 11.3%) — NV tự chọn ──────────────────
_VAT_RATES = (0.061, 0.113)   # 6.1% và 11.3%
_DEFAULT_VAT_RATE = 0.0       # cty KHÔNG dùng VAT → mặc định 0%


def _ensure_vat_rate_table():
    """Create fb_page_vat_rate table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_page_vat_rate (
                        page_id    VARCHAR(100)  NOT NULL,
                        date       DATE          NOT NULL,
                        vat_rate   NUMERIC(5,3)  NOT NULL DEFAULT 0.0,
                        updated_by VARCHAR(60),
                        updated_at TIMESTAMP     DEFAULT NOW(),
                        PRIMARY KEY (page_id, date)
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_page_vat_rate: %s", e)


def _load_vat_rate_map(date_from: str, date_to: str) -> dict:
    """Return {page_id: vat_rate(float)} cho khoảng ngày. Lấy ngày mới nhất nếu khác nhau.
    Page không có trong map → mặc định 11.3% (xử lý ở chỗ dùng)."""
    try:
        from db import get_conn
        _ensure_vat_rate_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (page_id) page_id, vat_rate
                    FROM fb_page_vat_rate
                    WHERE date BETWEEN %s AND %s
                    ORDER BY page_id, date DESC
                """, (date_from, date_to))
                return {row[0]: float(row[1]) for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not load vat_rate: %s", e)
        return {}


def _save_vat_rate(page_id: str, selected_date: str, vat_rate: float, user_id: str):
    """Upsert VAT rate (6.1%/11.3%) cho 1 page trên 1 ngày."""
    from db import get_conn
    _ensure_vat_rate_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_page_vat_rate (page_id, date, vat_rate, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (page_id, date) DO UPDATE
                    SET vat_rate   = EXCLUDED.vat_rate,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
            """, (page_id, selected_date, vat_rate, user_id))
        conn.commit()


def _get_leader_team_member_ids(leader_user_id) -> list:
    """Return list of user_id strings for all members of a leader's team (from users.json)."""
    leader = _get_user_by_id(leader_user_id)
    if not leader:
        return [str(leader_user_id)]
    team_id = str(leader.get("team_id", "")).strip()
    members = _get_team_members(team_id) if team_id else []
    ids = [str(m.get("id")) for m in members if m.get("id")]
    if str(leader_user_id) not in ids:
        ids.append(str(leader_user_id))
    return ids or [str(leader_user_id)]


def _manual_declared_pages_for_date(date_from: str, date_to: str, user_id, role) -> list:
    """Fetch ALL manually declared pages for a date range visible to this user.
    Tiền (tien_chua_thue/thanh_tien) được SUM qua các ngày trong khoảng cho mỗi page;
    metadata (decl_id, phan_loai, note...) lấy theo bản ghi mới nhất.
    Always includes declared_user_id so team_groups can map pages to members.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    if role == "leader":
                        team_ids = _get_leader_team_member_ids(user_id)
                        placeholders = ",".join(["%s"] * len(team_ids))
                        user_filter = f"AND d.user_id IN ({placeholders})"
                        params = [date_from, date_to] + team_ids
                    else:
                        # admin/ketoan: see ALL manual pages
                        user_filter = ""
                        params = [date_from, date_to]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [date_from, date_to, user_id]

                cur.execute(f"""
                    WITH decl AS (
                        SELECT
                            d.id,
                            d.page_id,
                            COALESCE(d.page_name_manual, d.page_id) AS page_name,
                            d.ad_account_id,
                            d.ad_account_name,
                            d.user_id AS declared_user_id,
                            d.phan_loai,
                            d.tien_chua_thue,
                            d.thanh_tien,
                            d.note,
                            d.created_at
                        FROM fb_page_spend_declarations d
                        WHERE d.spend_date BETWEEN %s AND %s
                          AND d.page_id LIKE 'MANUAL_%%'
                          {user_filter}
                    )
                    SELECT DISTINCT ON (page_id)
                        id AS decl_id,
                        page_id,
                        page_name,
                        ad_account_id,
                        ad_account_name,
                        declared_user_id,
                        phan_loai,
                        SUM(tien_chua_thue) OVER (PARTITION BY page_id) AS tien_chua_thue,
                        SUM(thanh_tien)     OVER (PARTITION BY page_id) AS thanh_tien,
                        note
                    FROM decl
                    ORDER BY page_id, created_at DESC
                """, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_manual_declared_pages_for_date error: %s", exc)
        return []


def _build_page_entry(ap, meta, spend_map):
    """Build a unified page dict from an auto-page record."""
    pid = ap["page_id"]
    aid = ap.get("fb_ad_account_id") or ""
    spend = spend_map.get(pid, {
        "win_chua": 0, "win_co": 0,
        "test_chua": 0, "test_co": 0,
        "tong": 0, "count": 0,
    })
    acc_entry = {
        "ad_account_id":   aid,
        "ad_account_name": meta.get("name") or aid,
        "owner_id":        meta.get("owner_id"),
        "owner_name":      meta.get("owner_name"),
        "owner_username":  meta.get("owner_username"),
    } if aid else None
    return {
        "page_id":       pid,
        "page_name":     ap["page_name"],
        "picture_url":   ap.get("picture_url"),
        "page_category": ap.get("page_category"),
        "ad_accounts":   [acc_entry] if acc_entry else [],
        "source":        "auto",
        "spend":         spend,
        "kpi": {
            "cp_ads_fb":     float(ap.get("spend") or 0),
            "cp_ads_fb_vat": round(float(ap.get("spend") or 0) * 1.113, 0),
            "impressions":   int(ap.get("impressions") or 0),
            "clicks":        int(ap.get("clicks") or 0),
            "fb_account_names": meta.get("name") or aid,
        },
    }


@chi_phi_qc_bp.route("/")
@login_required
def index():
    user_id   = session.get("user_id")
    role      = session.get("role", "staff")
    today     = date.today().isoformat()
    # ── Date range (Từ ngày → Đến ngày). Tương thích link cũ ?date= ──
    legacy    = (request.args.get("date") or "").strip()
    date_from = (request.args.get("date_from") or legacy or today).strip() or today
    date_to   = (request.args.get("date_to")   or legacy or today).strip() or today
    # Đảo nếu user chọn ngược (từ > đến)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    # `selected` = ngày Đến: dùng cho các thao tác theo-ngày (khai báo / Win-Test / sync)
    selected  = date_to
    view_tab  = request.args.get("tab", "all")   # "all" or "team"

    has_auto_table = _has_page_spend_table()
    account_meta   = _load_account_meta(date_from, date_to)
    spend_map      = _daily_spend_by_page(date_from, date_to, user_id, role)

    # ── Determine which ad accounts this user is allowed to see ──
    allowed_shop_keys  = _get_allowed_shop_keys_for_user(user_id, role)  # None = all
    if allowed_shop_keys is None:
        allowed_account_ids = None  # admin/ketoan: xem tất cả
    else:
        # Lấy account từ shop được gán (date-aware: mapping theo cửa sổ giao với kỳ lọc)
        shop_based_ids = _shop_keys_to_account_ids(allowed_shop_keys, date_from, date_to)
        # Lấy account được gán trực tiếp cho user này (date-aware theo khoảng lọc)
        direct_ids = set(_user_direct_account_ids(user_id, date_from, date_to))
        # LEADER: gộp thêm TK gán trực tiếp của TẤT CẢ thành viên trong team —
        # nếu không, NV có TK gán-trực-tiếp (không qua shop) sẽ bị thiếu khỏi
        # tab "Phân theo nhân viên" khi leader xem (bug: leader chỉ thấy 1 phần team).
        if role == "leader":
            _cu = _get_user_by_id(user_id)
            _tid = str(_cu.get("team_id", "")).strip()
            for _mb in (_get_team_members(_tid) if _tid else []):
                if _mb.get("id"):
                    direct_ids |= set(_user_direct_account_ids(_mb.get("id"), date_from, date_to))
        # Gộp cả hai nguồn
        allowed_account_ids = list(set(shop_based_ids) | direct_ids)

    # ── Auto-discovered pages (filtered by role) ──
    auto_pages = _auto_pages_for_date(date_from, date_to, allowed_account_ids) if has_auto_table else []

    # ── Manual pages (legacy, for pages not in auto) ──
    # Pass allowed_account_ids so leader sees only team pages (staff uses fb_page_assignments)
    manual_pages = _active_pages_for_user(user_id, role, allowed_account_ids=allowed_account_ids)
    kpi_map_old  = _kpi_spend_by_page(date_from, date_to)

    # ── Manually declared pages (user-typed, no auto sync) ──
    manual_declared = _manual_declared_pages_for_date(date_from, date_to, user_id, role)

    # ── Load Win/Test choices for auto-detected pages ──
    auto_phan_loai_map = _load_auto_phan_loai_map(date_from, date_to)

    # ── Mức VAT mỗi page (6.1%/11.3%, NV tự chọn; mặc định 11.3%) ──
    vat_rate_map = _load_vat_rate_map(date_from, date_to)

    # ── Build unified page list ──
    all_page_ids: set = set()
    pages = []
    for ap in auto_pages:
        meta = account_meta.get(ap.get("fb_ad_account_id") or "", {})
        entry = _build_page_entry(ap, meta, spend_map)
        # Inject Win/Test choice (default: ma_win)
        entry["auto_phan_loai"] = auto_phan_loai_map.get(ap["page_id"], "ma_win")
        pages.append(entry)
        all_page_ids.add(ap["page_id"])

    for mp in manual_pages:
        pid = mp["page_id"]
        if pid in all_page_ids:
            continue
        spend = spend_map.get(pid, {
            "win_chua": 0, "win_co": 0,
            "test_chua": 0, "test_co": 0,
            "tong": 0, "count": 0,
        })
        kpi = kpi_map_old.get(pid, {
            "cp_ads_fb": 0, "cp_ads_fb_vat": 0,
            "impressions": 0, "clicks": 0,
            "fb_account_names": "",
        })
        enriched_accs = []
        for acc in mp.get("ad_accounts", []):
            aid  = acc.get("ad_account_id", "")
            meta = account_meta.get(aid, {})
            enriched_accs.append({
                **acc,
                "ad_account_name": meta.get("name") or acc.get("ad_account_name") or aid,
                "owner_id":        meta.get("owner_id"),
                "owner_name":      meta.get("owner_name"),
                "owner_username":  meta.get("owner_username"),
            })
        pages.append({
            "page_id":       pid,
            "page_name":     mp.get("page_name", pid),
            "picture_url":   mp.get("picture_url"),
            "page_category": mp.get("page_category"),
            "ad_accounts":   enriched_accs,
            "source":        "manual",
            "spend":         spend,
            "kpi":           kpi,
        })

    # ── Manually declared pages (user typed page name, no auto sync) ──
    for md in manual_declared:
        pid = md["page_id"]
        if pid in all_page_ids:
            continue
        spend = spend_map.get(pid, {
            "win_chua": 0, "win_co": 0,
            "test_chua": 0, "test_co": 0,
            "tong": 0, "count": 0,
        })
        aid  = md.get("ad_account_id") or ""
        meta = account_meta.get(aid, {})
        pages.append({
            "page_id":           pid,
            "page_name":         md.get("page_name") or pid,
            "picture_url":       None,
            "page_category":     None,
            "is_manual_decl":    True,
            "declared_user_id":  str(md.get("declared_user_id") or ""),
            "decl_id":           md.get("decl_id"),
            "phan_loai":         md.get("phan_loai") or "ma_win",
            "tien_chua_thue":    float(md.get("tien_chua_thue") or 0),
            "thanh_tien":        float(md.get("thanh_tien") or 0),
            "decl_note":         md.get("note") or "",
            "ad_accounts":       [{
                "ad_account_id":   aid,
                "ad_account_name": meta.get("name") or md.get("ad_account_name") or aid,
                "owner_id":        meta.get("owner_id"),
                "owner_name":      meta.get("owner_name"),
                "owner_username":  meta.get("owner_username"),
            }] if aid else [],
            "source":            "manual_decl",
            "spend":             spend,
            "kpi": {
                "cp_ads_fb":     0,
                "cp_ads_fb_vat": 0,
                "impressions":   0,
                "clicks":        0,
                "fb_account_names": meta.get("name") or md.get("ad_account_name") or "",
            },
        })
        all_page_ids.add(pid)

    # ── Tách dòng theo NV phụ trách: TK đổi tay GIỮA kỳ → 1 dòng tách thành N dòng ──
    # VD: TK X giai đoạn 13/5-31/5 là Danh, 1/6- là Hân → khi lọc 31/5→1/6
    # sẽ tách thành 2 dòng (Danh cho 31/5, Hân cho 1/6) với chi phí cộng dồn riêng.
    try:
        _owner_segs = _account_owner_segments(date_from, date_to)
        _multi_aids = {aid for aid, segs in _owner_segs.items() if len(segs) > 1}
        if _multi_aids:
            _new_pages = []
            for _entry in pages:
                _eaids = [(a.get("ad_account_id") or "") for a in (_entry.get("ad_accounts") or [])]
                if any(aid in _multi_aids for aid in _eaids):
                    _new_pages.extend(_split_page_entry_by_owner(_entry, _owner_segs, date_from, date_to))
                else:
                    _new_pages.append(_entry)
            pages = _new_pages
    except Exception as _split_err:
        logger.error("Split-by-owner error: %s", _split_err)

    # ── Áp mức VAT mỗi page (6.1%/11.3%) → tính lại cp_ads_fb_vat ──
    # Page chưa chọn → mặc định 11.3%. Lưu rate (%) lên page để template hiện nút + tổng.
    # Map {ad_account_id: default_vat_rate} — VAT mặc định mỗi TK. Page sẽ tự áp rate
    # của TK đầu (primary). TK không có entry → mặc định 11.3% (_DEFAULT_VAT_RATE).
    _tk_default_vat: dict = {}
    try:
        from db import get_conn as _gc_v
        with _gc_v() as _c, _c.cursor() as _cur:
            _cur.execute("SELECT fb_ad_account_id, default_vat_rate FROM fb_ad_account_vat_options")
            _tk_default_vat = {str(r[0]): float(r[1] or _DEFAULT_VAT_RATE) for r in _cur.fetchall() if r[0]}
    except Exception as _e:
        logger.warning("Load vat_options error: %s", _e)
    # ── Số POS theo page (đối chiếu CP QC POS vs CP Ads FB) ──
    pos_page_map = _query_pos_page_metrics(date_from, date_to)
    # Page TỪNG có trên POS (bất kỳ ngày) → để phân biệt "chưa có số kỳ này" vs "không phải page POS"
    _all_pids = [p.get("page_id") for p in pages if p.get("page_id")]
    pos_known_map = _query_pos_known_pages(_all_pids) if _all_pids else {}
    # ── Đơn Meta theo page (cả kỳ) — số đơn cho page TEST (giống modal Chi tiết theo ngày) ──
    # Ưu tiên registrations (điền form ladipage) > purchases (lượt mua). Ladipage đa số đếm
    # bằng complete_registration, không bắn purchase.
    _meta_orders: dict = {}
    if _all_pids:
        try:
            from db import get_conn as _gc_m
            with _gc_m() as _cm, _cm.cursor() as _curm:
                _curm.execute("""
                    SELECT page_id,
                           GREATEST(COALESCE(SUM(registrations),0), COALESCE(SUM(purchases),0))
                      FROM mb_fb_entity_daily
                     WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                     GROUP BY page_id
                """, (_all_pids, date_from, date_to))
                for _pid, _mo in _curm.fetchall():
                    _meta_orders[str(_pid)] = int(_mo or 0)
        except Exception as _e_mo:
            logger.warning("Load meta orders error: %s", _e_mo)
    for p in pages:
        # Ưu tiên VAT:  #1 admin override per page (vat_rate_map)
        #              #2 TK default rate (theo TK chính của page, từ fb_ad_account_vat_options)
        #              #3 _DEFAULT_VAT_RATE (11.3%) cho TK chưa khai báo
        _page_aids = [(a.get("ad_account_id") or "") for a in (p.get("ad_accounts") or [])]
        _has_declared_tk = any(aid in _tk_default_vat for aid in _page_aids)
        _tk_rate = None
        for _aid in _page_aids:
            if _aid in _tk_default_vat:
                _tk_rate = _tk_default_vat[_aid]
                break
        if _tk_rate is None:
            _tk_rate = _DEFAULT_VAT_RATE
        rate = vat_rate_map.get(p.get("page_id"), _tk_rate)
        p["vat_rate"] = rate
        p["vat_pct"]  = round(rate * 100, 1)
        _fb = float(p.get("kpi", {}).get("cp_ads_fb", 0) or 0)
        if _fb > 0:
            p["kpi"]["cp_ads_fb_vat"] = round(_fb * (1.0 + rate), 0)
        # allow_6_1 = TK có trong vat_options table (admin mới được click radio).
        # TK chưa khai báo → ẩn radio, ẩn ô "Tổng X TK", chỉ hiện số 11.3 tính sẵn.
        p["allow_6_1"] = _has_declared_tk
        # POS metrics (None nếu page chưa có số trong kỳ đang xem)
        p["pos"] = pos_page_map.get(str(p.get("page_id")))
        # Tên page POS đã biết (dù kỳ này chưa có số) → để không ghi nhầm "Chưa có trên POS"
        p["pos_known_name"] = pos_known_map.get(str(p.get("page_id")))
        # Phân loại Win/Test (chốt 2026-08-04 — boss Phong):
        #   • NV đánh tay → theo mark đó
        #   • auto: WIN chỉ khi page CÓ trên POS **VÀ có phát sinh doanh thu**;
        #     page chưa lên POS, HOẶC có trên POS mà doanh thu = 0 → TEST
        _pl_explicit = auto_phan_loai_map.get(p.get("page_id"))
        _pos_rev = float((p["pos"] or {}).get("revenue") or 0) if p.get("pos") else 0.0
        if _pl_explicit == "ma_test":
            p["auto_phan_loai"] = "ma_test"
        elif _pl_explicit == "ma_win":
            p["auto_phan_loai"] = "ma_win"
        elif p["pos_known_name"] and _pos_rev > 0:
            p["auto_phan_loai"] = "ma_win"
        else:
            p["auto_phan_loai"] = "ma_test"
        # Page LÀ page POS (đã từng có số) nhưng kỳ này chưa phát sinh → coi như số 0,
        # hiển thị đầy đủ y như page POS có số (0₫ + lệch + DT/LN/chốt/hoàn = 0).
        if p["pos"] is None and p["pos_known_name"]:
            p["pos"] = {
                "ads": 0.0, "revenue": 0.0, "profit": 0.0, "sales": 0.0,
                "chot": 0, "hoan": 0, "name": p["pos_known_name"], "cp_per_order": 0,
            }
        # Số đơn + nguồn (giống modal): WIN→đơn chốt POS; TEST→đơn Meta (form ladipage)
        if p["auto_phan_loai"] == "ma_win" and p.get("pos"):
            p["so_don"] = int((p["pos"] or {}).get("chot") or 0)
            p["don_src"] = "POS"
        else:
            p["so_don"] = int(_meta_orders.get(str(p.get("page_id")), 0))
            p["don_src"] = "Meta"

    # ── Đánh dấu DÒNG ĐẦU mỗi page + tổng +VAT theo page (GLOBAL list, cho tab "Tất cả page") ──
    # Page chạy nhiều TK → nhiều entry cùng page_id. Dòng có spend cao nhất = primary →
    # hiện đầy đủ POS/lệch + ô "Tổng X TK"; các dòng sau hiện "↑ cùng page".
    from collections import defaultdict as _dd_g
    _g_sum_vat = _dd_g(float); _g_sum_fb = _dd_g(float); _g_row_count = _dd_g(int)
    _g_top: dict = {}
    for _i, _pg in enumerate(pages):
        _pid = _pg.get("page_id")
        _sp = float((_pg.get("kpi") or {}).get("cp_ads_fb", 0) or 0)
        _vt = float((_pg.get("kpi") or {}).get("cp_ads_fb_vat", 0) or 0)
        _g_sum_vat[_pid] += _vt; _g_sum_fb[_pid] += _sp; _g_row_count[_pid] += 1
        if _pid not in _g_top or _sp > _g_top[_pid][1]:
            _g_top[_pid] = (_i, _sp)
    for _i, _pg in enumerate(pages):
        _pid = _pg.get("page_id")
        _pg["_is_primary_row"] = (_g_top[_pid][0] == _i)
        _pg["_page_total_vat"] = round(_g_sum_vat[_pid], 0)
        _pg["_page_total_fb"]  = round(_g_sum_fb[_pid], 0)
        _pg["_page_row_count"] = _g_row_count[_pid]
    # Sort global pages: gom dòng cùng page liền nhau (theo tổng page desc, trong page theo TK spend desc)
    pages.sort(key=lambda p: (
        -float(_g_sum_vat.get(p.get("page_id"), 0) or p["kpi"]["cp_ads_fb"]),
        p.get("page_id") or "",
        -float(p["kpi"]["cp_ads_fb"] or 0),
    ))

    # ── Team groups (for admin/ketoan/leader: group pages by employee) ──
    team_groups = []
    if _is_full_view(role) or role == "leader":
        current_user_data = _get_user_by_id(user_id)
        if role == "leader":
            team_id = str(current_user_data.get("team_id", "")).strip()
            raw_members = _get_team_members(team_id) if team_id else [current_user_data]
        else:
            # admin/ketoan: show all teams grouped
            raw_members = [u for u in _load_users_json()
                           if u.get("status", "active") == "active"
                           and u.get("role") not in ("admin", "superadmin", "ketoan", "accountant")]
        # NV đã nghỉ: vẫn hiện nếu ngày xem (date_from) TRƯỚC ngày nghỉ →
        # lịch sử trước lúc nghỉ vẫn thấy. Nghỉ rồi xem ngày sau ngày nghỉ → ẩn.
        def _is_member_visible(m):
            if str(m.get("status", "active")).strip().lower() != "active":
                return False
            r = (m.get("resigned_at") or "").strip()
            if not r:
                return True
            # date_from ≤ resigned_at-1 (ngày cuối còn active) → vẫn hiện
            return date_from < r
        raw_members = [m for m in raw_members if _is_member_visible(m)]

        # Build lookups: account_id → pages AND declared_user_id → manual pages
        account_id_to_pages: dict = {}
        user_id_to_manual_pages: dict = {}
        for pg in pages:
            for acc in pg.get("ad_accounts", []):
                aid = acc.get("ad_account_id") or ""
                if aid:
                    account_id_to_pages.setdefault(aid, []).append(pg)
            # Track manual pages by the user who declared them
            if pg.get("is_manual_decl") and pg.get("declared_user_id"):
                uid = pg["declared_user_id"]
                user_id_to_manual_pages.setdefault(uid, []).append(pg)

        # ── Page → NV qua PAGE-BINDING shop (chia TK chung cho nhiều NV) ──
        # Nếu 1 page được bind vào shop của NV nào → page đó thuộc NV đó (ưu tiên
        # hơn việc gom theo TK). Cho phép 1 TK chạy nhiều NV: mỗi page về đúng người.
        # Shop→NV phải theo NGƯỜI GIỮ SHOP TRONG KỲ LỌC (usa versioned, migration 058)
        # — không phải chủ hiện tại. Vd shop Ken→Minh Đạt hiệu lực 1/6: lọc tháng 5
        # page/chi phí vẫn thuộc Ken. Cùng bài với fix dashboard 7426275d.
        _period_staff_shops: dict = {}
        if date_from and date_to:
            try:
                import app_ctx as _app_ctx
                _, _period_staff_shops = _app_ctx.load_shop_user_team_map_for_range(date_from, date_to)
            except Exception as _e:
                logger.warning("load_shop_user_team_map_for_range error: %s", _e)
                _period_staff_shops = {}

        def _member_shops_in_period(_mb) -> list:
            if _period_staff_shops:
                return _normalize_shops(sorted(_period_staff_shops.get(str(_mb.get("username") or ""), set())))
            return _normalize_shops(_mb.get("assigned_shops", []))

        shopkey_to_member: dict = {}
        for _mb in raw_members:
            for _sk in _member_shops_in_period(_mb):
                shopkey_to_member.setdefault(_sk, str(_mb.get("id") or ""))
        bound_page_member: dict = {}   # page_id -> member_id (theo binding mới nhất trong khoảng)
        try:
            from db import get_conn as _gc_pb
            _all_pids = [pg["page_id"] for pg in pages if pg.get("page_id")]
            if _all_pids and shopkey_to_member:
                with _gc_pb() as _cn:
                    with _cn.cursor() as _cur:
                        _cur.execute(
                            """
                            SELECT DISTINCT ON (b.page_id) b.page_id, s.shop_key
                              FROM fb_page_shop_binding b
                              JOIN shops s ON s.id = b.pos_shop_id
                             WHERE b.page_id = ANY(%s)
                               AND b.assigned_from <= %s::date
                               AND (b.assigned_to IS NULL OR b.assigned_to >= %s::date)
                             ORDER BY b.page_id, b.assigned_from DESC
                            """,
                            (_all_pids, date_to, date_from),
                        )
                        for _pid, _sk in _cur.fetchall():
                            _mid = shopkey_to_member.get(_sk)
                            if _mid:
                                bound_page_member[str(_pid)] = _mid
        except Exception as _e:
            logger.warning("team page-binding map error: %s", _e)

        # ── Tiền theo (NV, TK, page) CẮT THEO CỬA SỔ SỞ HỮU TK (uaa versioned) ──
        # TK đổi chủ giữa kỳ lọc: tiền NGÀY NÀO thuộc người giữ TK ngày đó — không
        # phải "TK overlap kỳ lọc thì ôm trọn". Vd AnhMinh9.6: yennhith 5-8/6,
        # anhminhth từ 9/6 → lọc 1-12/6 spend ngày 6/6 thuộc Yến Nhi (bug 12/6).
        _uaa_win_spend: dict = {}   # (user_id, ad_account_id, page_id) -> spend trong cửa sổ
        if date_from and date_to:
            try:
                from db import get_conn as _gc_ws
                with _gc_ws() as _cn_ws:
                    with _cn_ws.cursor() as _cur_ws:
                        _cur_ws.execute("""
                            SELECT a.user_id::text, s.fb_ad_account_id, s.page_id, SUM(s.spend)
                              FROM fb_ads_page_daily_spend s
                              JOIN user_ad_account_assignments a
                                ON a.ad_account_id = s.fb_ad_account_id
                               AND s.metric_date >= a.assigned_from
                               AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                             WHERE s.metric_date BETWEEN %s AND %s
                             GROUP BY 1, 2, 3
                        """, (date_from, date_to))
                        for _uid_w, _aid_w, _pid_w, _sp_w in _cur_ws.fetchall():
                            _uaa_win_spend[(str(_uid_w), str(_aid_w), str(_pid_w))] = float(_sp_w or 0)
            except Exception as _e:
                logger.warning("uaa window spend map error: %s", _e)

        # ── POS theo (NV, page) CHỈ TÍNH NGÀY NV GIỮ TK chạy page (đối xứng _uaa_win_spend) ──
        # FB đã cắt theo cửa sổ TK; POS cũng phải vậy, nếu không page chạy test 1 ngày của
        # NV này ôm trọn POS cả kỳ của chủ TK khác (bug 19/6: phuong test page 654 ngày 16
        # mà hiện POS 963k của duytungth ngày 4-9). Ngày NV không có POS → không có key → POS rỗng.
        _uaa_win_pos: dict = {}   # (user_id, page_id) -> pos dict
        if date_from and date_to:
            try:
                from db import get_conn as _gc_wp
                with _gc_wp() as _cn_wp:
                    with _cn_wp.cursor() as _cur_wp:
                        _cur_wp.execute("""
                            SELECT od.user_id, od.page_id,
                                   COALESCE(SUM(pp.ads_amount),0), COALESCE(SUM(pp.revenue),0),
                                   COALESCE(SUM(pp.profit),0), COALESCE(SUM(pp.success_order_count),0),
                                   COALESCE(SUM(pp.returned_order_count),0), MAX(pp.page_name),
                                   MAX(pp.pos_shop_id) AS pos_shop_id
                              FROM (
                                  SELECT DISTINCT a.user_id::text AS user_id, s.page_id, s.metric_date
                                    FROM fb_ads_page_daily_spend s
                                    JOIN user_ad_account_assignments a
                                      ON a.ad_account_id = s.fb_ad_account_id
                                     AND s.metric_date >= a.assigned_from
                                     AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                                   WHERE s.metric_date BETWEEN %s AND %s
                              ) od
                              JOIN pos_page_daily_metrics pp
                                ON pp.page_id = od.page_id AND pp.metric_date = od.metric_date
                             GROUP BY 1, 2
                        """, (date_from, date_to))
                        for _u, _p, _ads, _rev, _pf, _sc, _rc, _nm, _psid in _cur_wp.fetchall():
                            _ads = float(_ads or 0); _sc = int(_sc or 0)
                            _uaa_win_pos[(str(_u), str(_p))] = {
                                "ads": _ads, "revenue": float(_rev or 0), "profit": float(_pf or 0),
                                "chot": _sc, "hoan": int(_rc or 0), "name": (_nm or "").strip(),
                                "pos_shop_id": (str(_psid) if _psid else None),  # → link Pancake
                                "cp_per_order": round(_ads / _sc, 0) if _sc > 0 else 0,
                            }
            except Exception as _e:
                logger.warning("uaa window POS map error: %s", _e)

        # ── [FIX nhiều NV chung shop] TK gán ĐÍCH DANH cho NV qua uaa → KHÔNG hiện ở NV khác ──
        # Trước: nhiều NV chung shop1 → acc_ids_via_shop = MỌI TK của shop → ai cũng thấy hết TK.
        # Giờ: TK đã gán trực tiếp (user_ad_account_assignments) cho 1 NV cụ thể thì NV khác
        # (dù chung shop) bỏ TK đó. TK chung chưa gán ai vẫn hiện cho mọi NV của shop.
        _acc_direct_owner: dict = {}   # account_id -> set(member_id) đã gán trực tiếp
        for _mb in raw_members:
            if not _mb.get("id"):
                continue
            for _a in (_user_direct_account_ids(_mb.get("id"), date_from, date_to) or []):
                _acc_direct_owner.setdefault(str(_a), set()).add(str(_mb.get("id") or ""))

        for member in raw_members:
            shops     = _member_shops_in_period(member)
            acc_ids_via_shop = _shop_keys_to_account_ids(shops, date_from, date_to) if shops else []
            member_id = str(member.get("id") or "")
            # Bỏ TK đã gán đích danh cho NV KHÁC (giữ TK chung chưa gán ai + TK của chính mình)
            acc_ids_via_shop = [
                _a for _a in acc_ids_via_shop
                if (str(_a) not in _acc_direct_owner) or (member_id in _acc_direct_owner[str(_a)])
            ]
            # Direct assignment: TK QC ↔ NV qua user_ad_account_assignments (date-aware)
            # (TK đã được gán cho NV này, kể cả khi TK chưa có shop mapping).
            # FIX: date-aware → page lịch sử về đúng chủ tại thời điểm đó (không phải chủ hiện tại).
            acc_ids_direct = _user_direct_account_ids(member.get("id"), date_from, date_to) if member.get("id") else []
            # Hợp 2 nguồn → đầy đủ TK của NV
            acc_ids = list(dict.fromkeys(list(acc_ids_via_shop) + list(acc_ids_direct)))
            member_pages = []
            # Dedup theo (page_id, ad_account_id) → page chạy 2 TK = 2 dòng (mỗi dòng 1 TK).
            seen_keys: set = set()

            def _pg_aid(_pg):
                _acs = _pg.get("ad_accounts") or []
                return (_acs[0].get("ad_account_id") if _acs else "") or ""

            # Pages from ad account mapping — BỎ QUA page đã bind cho NV KHÁC
            # (page-binding ưu tiên: TK chung chia page theo shop của từng NV).
            # Dùng shallow copy để team-local flags không corrupt global flags.
            # TK gán TRỰC TIẾP (uaa, không qua shop): cắt tiền theo cửa sổ NV giữ TK
            # — page chỉ có tiền NGOÀI cửa sổ → bỏ hẳn khỏi NV này (về đúng chủ cũ).
            _direct_only = set(acc_ids_direct) - set(acc_ids_via_shop)
            for aid in acc_ids:
                for pg in account_id_to_pages.get(aid, []):
                    pid = pg["page_id"]
                    key = (pid, _pg_aid(pg) or aid)
                    if key in seen_keys:
                        continue
                    _bm = bound_page_member.get(pid)
                    if _bm is not None and _bm != member_id:
                        continue  # page này thuộc NV khác (theo binding) → không gom vào đây
                    _copy = dict(pg)
                    if aid in _direct_only and _uaa_win_spend:
                        _win = _uaa_win_spend.get((member_id, aid, pid), 0.0)
                        _k = dict(_copy.get("kpi") or {})
                        _full = float(_k.get("cp_ads_fb") or 0)
                        if _full > 0:
                            if _win <= 0:
                                continue   # toàn bộ tiền page này nằm ngoài kỳ NV giữ TK
                            if _win < _full:
                                _ratio = _win / _full
                                _k["cp_ads_fb"]     = round(_win, 0)
                                _k["cp_ads_fb_vat"] = round(float(_k.get("cp_ads_fb_vat") or 0) * _ratio, 0)
                                _copy["kpi"] = _k
                    # POS scope theo ngày NV GIỮ TK (uaa) chạy page — áp cho MỌI TK NV giữ
                    # qua uaa (kể cả TK đồng thời map qua shop, nên KHÔNG nằm trong _direct_only).
                    # None = ngày NV không có POS → cột POS trống, không ôm POS cả kỳ của chủ
                    # TK khác (bug 19/6: phuong test page 654 ngày 16 → POS phải trống, không
                    # phải 963k của duytungth ngày 4-9). Page thuần-shop (aid không thuộc uaa
                    # của NV) giữ POS global như cũ.
                    if aid in acc_ids_direct:
                        _copy["pos"] = _uaa_win_pos.get((member_id, pid))
                    member_pages.append(_copy)
                    seen_keys.add(key)

            # Pages BIND vào shop của NV này (kể cả khi TK không thuộc acc_ids của họ)
            for pg in pages:
                pid = pg["page_id"]
                key = (pid, _pg_aid(pg))
                if key in seen_keys:
                    continue
                if bound_page_member.get(pid) == member_id:
                    member_pages.append(dict(pg))
                    seen_keys.add(key)

            # Manual pages declared for/by this member (giữ dedup chỉ theo page_id
            # cho phiếu khai báo tay, vì không gắn TK).
            _seen_manual_pids = {p["page_id"] for p in member_pages}
            for pg in user_id_to_manual_pages.get(member_id, []):
                if pg["page_id"] not in _seen_manual_pids:
                    member_pages.append(dict(pg))
                    _seen_manual_pids.add(pg["page_id"])

            # ── Đánh dấu DÒNG ĐẦU mỗi page + tổng +VAT/FB theo page ──
            from collections import defaultdict as _dd
            _page_sum_vat = _dd(float); _page_sum_fb = _dd(float); _page_row_count = _dd(int)
            _page_top: dict = {}
            for _i, _pg in enumerate(member_pages):
                _pid = _pg.get("page_id")
                _sp = float((_pg.get("kpi") or {}).get("cp_ads_fb", 0) or 0)
                _vt = float((_pg.get("kpi") or {}).get("cp_ads_fb_vat", 0) or 0)
                _page_sum_vat[_pid] += _vt
                _page_sum_fb[_pid]  += _sp
                _page_row_count[_pid] += 1
                if _pid not in _page_top or _sp > _page_top[_pid][1]:
                    _page_top[_pid] = (_i, _sp)
            for _i, _pg in enumerate(member_pages):
                _pid = _pg.get("page_id")
                _pg["_is_primary_row"] = (_page_top[_pid][0] == _i)
                _pg["_page_total_vat"] = round(_page_sum_vat[_pid], 0)
                _pg["_page_total_fb"]  = round(_page_sum_fb[_pid], 0)
                _pg["_page_row_count"] = _page_row_count[_pid]

            # CHỈ giữ page ĐANG CHẠY: có chi phí FB (cp_ads_fb>0) hoặc có khai báo
            # (spend.tong>0). Bỏ page legacy map vào TK nhưng 0đ (gây rối báo cáo).
            member_pages = [
                p for p in member_pages
                if float((p.get("kpi") or {}).get("cp_ads_fb", 0) or 0) > 0
                or float((p.get("spend") or {}).get("tong", 0) or 0) > 0
            ]

            # Ẩn NV KHÔNG chạy quảng cáo: không có TK QC nào gán VÀ không có khai
            # báo tay. (Loại kho/sale/it + staff không phụ trách ads như vuit/huynhth.)
            # NV ads có TK nhưng 0đ hôm nay → acc_ids vẫn có → VẪN hiện ("chưa có chi phí").
            _role = str(member.get("role", "")).strip().lower()
            if (not acc_ids and not member_pages) or _role in _NON_ADS_ROLES:
                continue

            team_groups.append({
                "user_id":   member.get("id"),
                "username":  member.get("username", ""),
                "full_name": member.get("full_name", "") or member.get("username", ""),
                "role":      member.get("role", "staff"),
                "team_id":   member.get("team_id", ""),
                "shop_keys": shops,
                "acc_ids":   acc_ids,
                "pages":     sorted(member_pages, key=lambda p: (
                    -float(_page_sum_vat.get(p.get("page_id"), 0) or p["kpi"]["cp_ads_fb"]),
                    p.get("page_id") or "",
                    -float(p["kpi"]["cp_ads_fb"] or 0),
                )),
                "totals": {
                    "cp_ads_fb":     sum(p["kpi"]["cp_ads_fb"]     for p in member_pages),
                    "cp_ads_fb_vat": sum(p["kpi"]["cp_ads_fb_vat"] for p in member_pages),
                    # effective total: use FB VAT when auto-detected, else manual declaration
                    "tong_khai":     sum(
                        p["kpi"]["cp_ads_fb_vat"] if p["kpi"]["cp_ads_fb_vat"] > 0
                        else p["spend"]["tong"]
                        for p in member_pages
                    ),
                },
            })
        # Sort by cp_ads_fb desc
        team_groups.sort(key=lambda g: g["totals"]["cp_ads_fb"], reverse=True)

    # Effective win/test: auto pages counted by their Win/Test classification
    eff_win = eff_test = 0.0
    for p in pages:
        vat = p["kpi"]["cp_ads_fb_vat"]
        if vat > 0:
            # Auto page — count based on Win/Test choice
            if p.get("auto_phan_loai", "ma_win") == "ma_test":
                eff_test += vat
            else:
                eff_win  += vat
        else:
            # Manual declaration — use declared amounts
            eff_win  += p["spend"]["win_co"]
            eff_test += p["spend"]["test_co"]

    totals = {
        "win_chua":      sum(p["spend"]["win_chua"]      for p in pages),
        "win_co":        sum(p["spend"]["win_co"]        for p in pages),
        "test_chua":     sum(p["spend"]["test_chua"]     for p in pages),
        "test_co":       sum(p["spend"]["test_co"]       for p in pages),
        # effective win/test includes auto page amounts
        "effective_win":  eff_win,
        "effective_test": eff_test,
        # effective total: FB VAT when auto-detected, else manual declaration
        "tong":          sum(
            p["kpi"]["cp_ads_fb_vat"] if p["kpi"]["cp_ads_fb_vat"] > 0
            else p["spend"]["tong"]
            for p in pages
        ),
        "count":         sum(p["spend"]["count"]         for p in pages),
        "cp_ads_fb":     sum(p["kpi"]["cp_ads_fb"]       for p in pages),
        "cp_ads_fb_vat": sum(p["kpi"]["cp_ads_fb_vat"]  for p in pages),
        # Tổng CP QC POS (chỉ page có trên POS) — để đối chiếu ở tfoot
        "pos_ads":       sum((p.get("pos") or {}).get("ads", 0) for p in pages),
        "pos_chot":      sum((p.get("pos") or {}).get("chot", 0) for p in pages),
        "pos_count":     sum(1 for p in pages if p.get("pos")),
        # Tổng Doanh thu / Lợi nhuận / Hoàn từ POS — cho dòng tổng bảng
        "pos_sales":     sum((p.get("pos") or {}).get("sales", 0) for p in pages),
        "pos_revenue":   sum((p.get("pos") or {}).get("revenue", 0) for p in pages),
        "pos_profit":    sum((p.get("pos") or {}).get("profit", 0) for p in pages),
        "pos_hoan":      sum((p.get("pos") or {}).get("hoan", 0) for p in pages),
    }

    # Last sync time for the selected date
    last_sync = _get_last_sync(selected)
    last_sync_at = ""
    if last_sync and last_sync.get("synced_at"):
        last_sync_at = fmt_hcm(last_sync["synced_at"], "%H:%M %d/%m/%Y")

    # Tổng NV hiển thị (giờ hiện cả NV chưa có chi phí trong khoảng ngày)
    team_active_count = len(team_groups)
    team_pages_total  = sum(len(g.get("pages", [])) for g in team_groups)

    # ── Load page→shop binding (2 tier: explicit + auto-detected) + shops của user ──
    page_bindings: dict = {}
    user_shops: list = []
    try:
        from db import get_conn as _gc
        from repositories.admin_repo import list_page_bindings_active
        with _gc() as _conn:
            with _conn.cursor() as _cur:
                # Tier 1: explicit page bindings
                page_bindings = list_page_bindings_active(_cur)
                # Tier 2: auto-detect — pages của TK đơn shop (fb_ad_account_mappings)
                # Lấy danh sách TK chỉ map 1 shop active
                _cur.execute(
                    """
                    SELECT m.fb_ad_account_id, MIN(m.shop_id), MIN(s.shop_name), MIN(s.shop_key)
                      FROM fb_ad_account_mappings m
                      JOIN shops s ON s.id = m.shop_id
                     WHERE m.assigned_to IS NULL
                     GROUP BY m.fb_ad_account_id
                    HAVING COUNT(*) = 1
                    """
                )
                _single_shop_tk = {
                    str(r[0]): {"pos_shop_id": int(r[1]), "shop_name": r[2] or "", "shop_key": r[3] or ""}
                    for r in _cur.fetchall()
                }
                # Map page_id → TK đơn shop (qua fb_ads_page_daily_spend) — trong khoảng ngày đang xem
                _cur.execute(
                    """
                    SELECT DISTINCT s.page_id, s.fb_ad_account_id
                      FROM fb_ads_page_daily_spend s
                     WHERE s.metric_date BETWEEN %s AND %s
                       AND s.fb_ad_account_id = ANY(%s)
                    """,
                    (date_from, date_to, list(_single_shop_tk.keys()) or [""]),
                )
                for r in _cur.fetchall():
                    pid = str(r[0])
                    aid = str(r[1])
                    if pid in page_bindings:
                        continue  # Tier 1 (explicit) đã có → giữ
                    tk_shop = _single_shop_tk.get(aid)
                    if tk_shop:
                        page_bindings[pid] = {
                            **tk_shop,
                            "shop_source": "auto",
                        }
                # Mark tier 1 explicit ones với shop_source='manual' để UI phân biệt
                for pid, b in page_bindings.items():
                    if "shop_source" not in b:
                        b["shop_source"] = "manual"

                # Shops của user hiện tại
                _priv = (role or "").lower() in {"admin", "superadmin", "manager", "accountant", "ketoan", "it", "leader", "sale_leader"}
                if _priv:
                    _cur.execute("SELECT id, shop_name, shop_key FROM shops WHERE status='active' ORDER BY shop_name")
                else:
                    _cur.execute(
                        "SELECT s.id, s.shop_name, s.shop_key FROM user_shop_assignments usa "
                        "JOIN shops s ON s.id=usa.shop_id WHERE usa.user_id=%s AND usa.assigned_to IS NULL AND s.status='active' "
                        "ORDER BY s.shop_name",
                        (int(user_id) if user_id else 0,),
                    )
                user_shops = [
                    {"id": int(r[0]), "shop_name": r[1] or "", "shop_key": r[2] or ""}
                    for r in _cur.fetchall()
                ]
    except Exception as _exc:
        logger.warning("Load page_bindings/user_shops error: %s", _exc)

    return render_template("chi_phi_qc/index.html",
                           pages=pages,
                           totals=totals,
                           team_groups=team_groups,
                           team_active_count=team_active_count,
                           team_pages_total=team_pages_total,
                           view_tab=view_tab,
                           selected_date=selected,
                           date_from=date_from,
                           date_to=date_to,
                           is_range=(date_from != date_to),
                           today=today,
                           current_user_id=user_id,
                           current_role=role,
                           has_auto_table=has_auto_table,
                           auto_page_count=len(auto_pages),
                           last_sync_at=last_sync_at,
                           page_bindings=page_bindings,
                           user_shops=user_shops)


def _lai_lo_report(date_from: str, date_to: str, team_f: str = "", nv_f: str = "",
                   uoc_tinh: bool = True) -> dict:
    """Báo cáo Lãi/Lỗ 4 chiều (page/TK/NV/team) cho khoảng ngày.
    Lãi/Lỗ = Lợi nhuận POS − CP QC (đã VAT) − CP khác (75.000đ/đơn: vận chuyển + chi phí chung).
    Chiều TK: phân bổ doanh thu/lợi nhuận/đơn của PAGE cho từng TK theo TỶ LỆ CHI TIÊU."""
    from collections import defaultdict
    _DEF_VAT = 0.113
    _CP_KHAC = 75000   # đồng/đơn chốt — vận chuyển + chi phí chung (sếp 05/08)
    _TL_PHAT = 0.57    # tỷ lệ phát thành công ước tính (sếp 25/08: sau hoàn = trước hoàn × 0,57)
    def _norm(a):
        return str(a or "").replace("act_", "")
    out = {"by_page": [], "by_tk": [], "by_nv": [], "by_team": []}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT page_id, fb_ad_account_id, SUM(spend) FROM (
                    SELECT page_id, fb_ad_account_id, metric_date, MAX(spend) AS spend
                      FROM fb_ads_page_daily_spend
                     WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_id,'')<>''
                       AND NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e
                                        WHERE e.ad_account_id = REPLACE(fb_ad_account_id,'act_',''))
                     GROUP BY page_id, fb_ad_account_id, metric_date
                ) t GROUP BY page_id, fb_ad_account_id
            """, (date_from, date_to))
            spend_pt = {(str(p), _norm(a)): float(s or 0) for p, a, s in cur.fetchall()}
            cur.execute("""
                SELECT page_id, SUM(revenue), SUM(profit), SUM(success_order_count),
                       SUM(capital), SUM(returned_order_count)
                  FROM pos_page_daily_metrics
                 WHERE metric_date BETWEEN %s AND %s GROUP BY page_id
            """, (date_from, date_to))
            pos_map = {str(p): {"rev": float(r or 0), "profit": float(pf or 0), "chot": int(c or 0),
                                "gia_von": float(gv or 0), "hoan": int(h or 0)}
                       for p, r, pf, c, gv, h in cur.fetchall()}
            cur.execute("SELECT fb_ad_account_id, default_vat_rate FROM fb_ad_account_vat_options")
            vat_map = {_norm(r[0]): float(r[1] or _DEF_VAT) for r in cur.fetchall() if r[0]}
            cur.execute("""
                SELECT m.ad_account_id, u.id,
                       COALESCE(NULLIF(u.full_name,''), u.username), COALESCE(t.team_name,'')
                  FROM user_ad_account_assignments m
                  JOIN users u ON u.id = m.user_id
                  LEFT JOIN teams t ON t.id = u.team_id
                 WHERE m.assigned_to IS NULL
            """)
            tk_owner = {_norm(a): {"nv_id": str(uid), "nv": nm, "team": tm or ""}
                        for a, uid, nm, tm in cur.fetchall()}
            cur.execute("SELECT ad_account_id, account_name FROM fb_ad_account_info")
            acc_name = {_norm(r[0]): (r[1] or "") for r in cur.fetchall()}
            cur.execute("SELECT page_id, name FROM fb_page_names")
            page_name = {str(r[0]): (r[1] or "") for r in cur.fetchall()}
            # Fallback tên page: POS (pos_page_daily_metrics.page_name) → bảng spend.
            # Không có 2 nguồn này thì báo cáo/Zalo hiện trơ page_id (sếp 11/08).
            cur.execute("""
                SELECT page_id, MAX(page_name) FROM pos_page_daily_metrics
                 WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_name,'') <> ''
                 GROUP BY page_id
            """, (date_from, date_to))
            for pid, nm in cur.fetchall():
                if nm and not page_name.get(str(pid)):
                    page_name[str(pid)] = nm
            cur.execute("""
                SELECT page_id, MAX(page_name) FROM fb_ads_page_daily_spend
                 WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_name,'') <> ''
                 GROUP BY page_id
            """, (date_from, date_to))
            for pid, nm in cur.fetchall():
                if nm and not page_name.get(str(pid)):
                    page_name[str(pid)] = nm

        page_spend = defaultdict(float)
        for (pid, aid), sp in spend_pt.items():
            page_spend[pid] += sp

        by_page, by_tk, by_nv, by_team = {}, {}, {}, {}
        seen_pages = set()
        _blank = lambda k, extra=None: {**{"key": k, "rev": 0.0, "profit": 0.0,
                                            "cpqc": 0.0, "cp_test": 0.0,
                                            "don": 0.0, "cp_khac": 0.0, "gia_von": 0.0, "hoan": 0.0},
                                        **(extra or {})}
        for (pid, aid), spend in spend_pt.items():
            pos = pos_map.get(pid) or {"rev": 0.0, "profit": 0.0, "chot": 0, "gia_von": 0.0, "hoan": 0}
            ptot = page_spend[pid] or 0.0
            share = (spend / ptot) if ptot > 0 else 0.0
            a_rev = pos["rev"] * share
            a_pf = pos["profit"] * share
            a_gv = pos.get("gia_von", 0.0) * share
            a_hoan = pos.get("hoan", 0) * share
            a_don = pos["chot"] * share
            a_khac = a_don * _CP_KHAC
            cpqc = spend * (1.0 + vat_map.get(aid, _DEF_VAT))
            # Page có doanh thu POS = WIN → CP QC; page KHÔNG có doanh thu = TEST → Chi phí test
            _is_test = (pos["rev"] <= 0)
            seen_pages.add(pid)
            own = tk_owner.get(aid) or {"nv_id": "", "nv": "(chưa gán NV)", "team": ""}
            if team_f and (own.get("team") or "") != team_f:
                continue
            if nv_f and str(own.get("nv_id") or "") != str(nv_f):
                continue
            for d, k, extra in (
                (by_page, pid, {"name": page_name.get(pid) or pid}),
                (by_tk, aid, {"name": acc_name.get(aid) or ("act_" + aid),
                              "nv": own["nv"], "team": own["team"]}),
                (by_nv, own["nv_id"] or ("?" + aid), {"name": own["nv"], "team": own["team"]}),
                (by_team, own["team"] or "(chưa gán team)",
                 {"name": own["team"] or "(chưa gán team)"}),
            ):
                r = d.setdefault(k, _blank(k, extra))
                r["rev"] += a_rev; r["profit"] += a_pf
                if _is_test:
                    r["cp_test"] += cpqc
                else:
                    r["cpqc"] += cpqc
                r["don"] += a_don; r["cp_khac"] += a_khac; r["gia_von"] += a_gv; r["hoan"] += a_hoan

        # Page bán ORGANIC (KHÔNG chạy ads trong kỳ) — có doanh thu POS nhưng 0
        # tiền ads. Sếp Phong 28/08: "hầu hết CÓ gán NV rồi bạn" — trước đây cứ
        # organic là gom thẳng vào "(không chạy ads)", bỏ qua việc page đó có
        # chủ hay không. Giờ tra NV phụ trách page qua _page_owner (nhìn lùi 14
        # ngày trước, đúng logic tab Page/Zalo Lan đang dùng) — chỉ page THẬT SỰ
        # chưa ai từng chạy ads mới rơi vào "(không chạy ads)".
        if team_f or nv_f:
            pos_map = {}   # đang lọc theo team/NV → bỏ khối organic (không thuộc ai)
        _org_owner = {}
        if pos_map:
            from modules.chi_phi_qc.lan_ads_report import _page_owner
            _org_owner = _page_owner(date_from, date_to)
        org_rev = org_pf = org_don = org_khac = org_gv = org_hoan = 0.0
        for pid, pos in pos_map.items():
            if pid not in seen_pages and (pos["rev"] or pos["profit"]):
                _khac = pos["chot"] * _CP_KHAC
                by_page[pid] = _blank(pid, {"name": page_name.get(pid) or pid,
                                            "rev": pos["rev"], "profit": pos["profit"],
                                            "don": float(pos["chot"]), "cp_khac": _khac,
                                            "gia_von": pos.get("gia_von", 0.0),
                                            "hoan": float(pos.get("hoan", 0))})
                _own = _org_owner.get(pid) or {}
                if _own.get("nv_id"):
                    # key GIỐNG HỆT nhánh ads-loop phía trên (own["nv_id"] or "?"+aid)
                    # để gộp đúng vào dòng NV đã có, không tạo dòng trùng theo tên.
                    for d, k, extra in (
                        (by_tk, "__org_" + _own["nv_id"], {"name": "(page bán không chạy ads)",
                                                        "nv": _own["nv"], "team": _own.get("team","")}),
                        (by_nv, _own["nv_id"], {"name": _own["nv"], "team": _own.get("team","")}),
                        (by_team, _own.get("team") or "(chưa gán team)",
                         {"name": _own.get("team") or "(chưa gán team)"}),
                    ):
                        r = d.setdefault(k, _blank(k, extra))
                        r["rev"] += pos["rev"]; r["profit"] += pos["profit"]
                        r["don"] += pos["chot"]; r["cp_khac"] += _khac
                        r["gia_von"] += pos.get("gia_von", 0.0); r["hoan"] += pos.get("hoan", 0)
                else:
                    org_rev += pos["rev"]; org_pf += pos["profit"]
                    org_don += pos["chot"]; org_khac += _khac; org_gv += pos.get("gia_von", 0.0)
                    org_hoan += pos.get("hoan", 0)
        if org_rev or org_pf:
            for d, key, extra in (
                (by_tk, "__org", {"name": "(không chạy ads)", "nv": "—", "team": "—"}),
                (by_nv, "__org", {"name": "(không chạy ads)", "team": "—"}),
                (by_team, "(không chạy ads)", {"name": "(không chạy ads)"}),
            ):
                r = d.setdefault(key, _blank(key, extra))
                r["rev"] += org_rev; r["profit"] += org_pf
                r["don"] += org_don; r["cp_khac"] += org_khac; r["gia_von"] += org_gv; r["hoan"] += org_hoan

        def _fin(d):
            rows = list(d.values())
            for r in rows:
                # Sếp Phong 27/08 chốt LÀM 2 BẢNG:
                # 1) uoc_tinh=True — "Lãi/Lỗ dự phòng": hoàn về POS trễ cả tháng
                #    nên nhân 57% (tỷ lệ phát thành công dự tính) cho MỌI chỉ tiêu
                #    theo đơn — đơn, doanh thu, giá vốn, lợi nhuận.
                # 2) uoc_tinh=False — "Lãi/Lỗ theo POS": thông số POS nguyên bản,
                #    sau ~1 tháng hoàn về đủ thì bảng này mới chuẩn 100%.
                # Tiền ads (CP QC, CP test) 2 bảng đều giữ nguyên vì đã đốt thật.
                r["don_truoc"] = r["don"] + r.get("hoan", 0)
                r["rev_truoc"] = r["rev"]
                if uoc_tinh:
                    r["don_sau"] = r["don_truoc"] * _TL_PHAT
                    r["rev"] = r["rev"] * _TL_PHAT
                    r["gia_von"] = r.get("gia_von", 0.0) * _TL_PHAT
                    r["profit"] = r["profit"] * _TL_PHAT
                else:
                    r["don_sau"] = float(r["don"])
                # CP chung 75k tính trên đơn TRƯỚC hoàn — đơn hoàn vẫn tốn ship +
                # chi phí (sếp Phong 28/08: "75k áp cho đơn trước hoàn; quy về đơn
                # sau hoàn thì đơn giá = 75k/0,57"). 2 cách tính ra CÙNG một tổng.
                r["cp_khac"] = r["don_truoc"] * _CP_KHAC
                r["lai_lo"] = r["profit"] - r["cpqc"] - r["cp_test"] - r["cp_khac"]
            rows.sort(key=lambda x: -x["lai_lo"])   # mặc định CAO → THẤP (sếp 07/08)
            return rows
        out = {"by_page": _fin(by_page), "by_tk": _fin(by_tk),
               "by_nv": _fin(by_nv), "by_team": _fin(by_team)}
    except Exception as exc:
        logger.error("_lai_lo_report error: %s", exc)
    return out


@chi_phi_qc_bp.route("/lai-lo")
@chi_phi_qc_bp.route("/lai-lo-pos")
@login_required
def lai_lo():
    # 2 bảng (sếp Phong 27/08): /lai-lo = DỰ PHÒNG (dự hoàn 57%),
    # /lai-lo-pos = THEO POS nguyên bản (chuẩn khi hoàn đã về đủ ~1 tháng).
    uoc = not request.path.endswith("-pos")
    role = session.get("role", "staff")
    today = date.today().isoformat()
    legacy = (request.args.get("date") or "").strip()
    date_from = (request.args.get("date_from") or legacy or today).strip() or today
    date_to = (request.args.get("date_to") or legacy or today).strip() or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    tab = request.args.get("tab", "page")   # page|tk|nv|team
    team_f = (request.args.get("team") or "").strip()
    nv_f = (request.args.get("nv") or "").strip()
    data = _lai_lo_report(date_from, date_to, team_f=team_f, nv_f=nv_f, uoc_tinh=uoc)
    # Tab "Theo Page": gắn thêm NV phụ trách (theo TK QC chi nhiều nhất cho page)
    # — sếp muốn nhìn page là biết của ai, khỏi phải mở tab khác đối chiếu.
    if tab == "page":
        try:
            from modules.chi_phi_qc.lan_ads_report import _page_owner
            _own = _page_owner(date_from, date_to)
            for _r in (data.get("by_page") or []):
                _o = _own.get(str(_r.get("key"))) or {}
                _r["nv"] = _o.get("nv") or ""
                _r["team"] = _o.get("team") or ""
        except Exception as _e:
            logger.warning("lai_lo page owner: %s", _e)
    # Danh sách Team + NV đang giữ TK QC (cho dropdown lọc)
    team_list, nv_list = [], []
    try:
        from db import get_conn as _gc
        with _gc() as _conn, _conn.cursor() as _cur:
            _cur.execute("""
                SELECT DISTINCT COALESCE(t.team_name,''), u.id,
                       COALESCE(NULLIF(u.full_name,''), u.username)
                  FROM user_ad_account_assignments m
                  JOIN users u ON u.id = m.user_id
                  LEFT JOIN teams t ON t.id = u.team_id
                 WHERE m.assigned_to IS NULL
            """)
            _seen_t, _seen_u = set(), set()
            for _tm, _uid, _nm in _cur.fetchall():
                if _tm and _tm not in _seen_t:
                    _seen_t.add(_tm); team_list.append(_tm)
                if _uid and _uid not in _seen_u:
                    _seen_u.add(_uid); nv_list.append({"id": str(_uid), "name": _nm, "team": _tm or ""})
            team_list.sort(); nv_list.sort(key=lambda x: (x["team"], x["name"]))
    except Exception as _e:
        logger.warning("lai_lo filter lists error: %s", _e)

    def _tot(rows):
        return {"rev": sum(r["rev"] for r in rows), "profit": sum(r["profit"] for r in rows),
                "cpqc": sum(r["cpqc"] for r in rows), "cp_test": sum(r["cp_test"] for r in rows),
                "cp_khac": sum(r["cp_khac"] for r in rows),
                "gia_von": sum(r.get("gia_von", 0) for r in rows),
                "hoan": sum(r.get("hoan", 0) for r in rows),
                "don": sum(r["don"] for r in rows),
                "don_truoc": sum(r.get("don_truoc", r["don"]) for r in rows),
                "rev_truoc": sum(r.get("rev_truoc", r["rev"]) for r in rows),
                "don_sau": sum(r.get("don_sau", r["don"]) for r in rows),
                "lai_lo": sum(r["lai_lo"] for r in rows)}
    totals = {k: _tot(v) for k, v in data.items()}
    return render_template("chi_phi_qc/lai_lo.html",
                           data=data, totals=totals, tab=tab, uoc=uoc,
                           date_from=date_from, date_to=date_to,
                           is_range=(date_from != date_to), current_role=role,
                           team_f=team_f, nv_f=nv_f, team_list=team_list, nv_list=nv_list)


def _load_budget_by_team(snap_date: str) -> dict:
    """Đọc snapshot ngân sách TK QC ngày `snap_date`, gom theo team.
    Trả {teams: [{team_name, accounts:[...], total_remaining, urgent_count}], grand, synced_at, dates[]}."""
    from db import get_conn
    teams: dict = {}
    grand = {"remaining": 0.0, "spend_cap": 0.0, "amount_spent": 0.0, "count": 0, "urgent": 0}
    synced_at = None
    URGENT = 1_000_000  # < 1 triệu = sắp hết
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT team_name, short_name, account_name, user_name, ad_account_id,
                   spend_cap, amount_spent, remaining, account_status, synced_at
              FROM fb_ad_account_budget_snapshot
             WHERE snapshot_date = %s
             ORDER BY remaining ASC NULLS LAST
            """,
            (snap_date,),
        )
        for tn, sn, an, nv, aid, cap, spent, rem, st, sat in cur.fetchall():
            synced_at = synced_at or sat
            tn = tn or "(chưa gán team)"
            g = teams.setdefault(tn, {"team_name": tn, "accounts": [], "total_remaining": 0.0, "urgent_count": 0})
            remf = float(rem) if rem is not None else None
            is_urgent = remf is not None and remf < URGENT
            g["accounts"].append({
                "short_name": sn, "account_name": an, "user_name": nv, "ad_account_id": aid,
                "spend_cap": float(cap or 0), "amount_spent": float(spent or 0),
                "remaining": remf, "account_status": st, "urgent": is_urgent,
                "no_cap": (rem is None),
            })
            if remf is not None:
                g["total_remaining"] += remf
                grand["remaining"] += remf
            grand["spend_cap"] += float(cap or 0)
            grand["amount_spent"] += float(spent or 0)
            grand["count"] += 1
            if is_urgent:
                g["urgent_count"] += 1
                grand["urgent"] += 1
        # các ngày có snapshot (cho dropdown chọn ngày)
        cur.execute("SELECT DISTINCT snapshot_date FROM fb_ad_account_budget_snapshot ORDER BY snapshot_date DESC LIMIT 30")
        dates = [r[0].isoformat() for r in cur.fetchall()]
    # team còn ít tổng → lên đầu
    team_list = sorted(teams.values(), key=lambda x: x["total_remaining"])
    return {"teams": team_list, "grand": grand, "synced_at": synced_at, "dates": dates}


@chi_phi_qc_bp.route("/ngan-sach-tk")
@login_required
def ngan_sach_tk():
    """Ngân sách (giới hạn chi tiêu) còn lại của các TK QC đuôi TH — gom theo team.
    Snapshot quét 1 lần/ngày lúc 19:30. Chỉ IT/admin/quản lý xem."""
    role = session.get("role", "staff")
    if not (_is_full_view(role) or role in ("it", "admin", "superadmin")):
        abort(403)
    snap_date = (request.args.get("date") or date.today().isoformat()).strip()
    data = _load_budget_by_team(snap_date)
    return render_template("chi_phi_qc/ngan_sach_tk.html",
                           snap_date=snap_date,
                           teams=data["teams"],
                           grand=data["grand"],
                           synced_at=data["synced_at"],
                           dates=data["dates"])


@chi_phi_qc_bp.route("/ngan-sach-tk/refresh", methods=["POST"])
@login_required
def ngan_sach_tk_refresh():
    """Quét lại snapshot ngân sách ngay (nút Quét lại). Chỉ IT/admin."""
    role = session.get("role", "staff")
    if not (_is_full_view(role) or role in ("it", "admin", "superadmin")):
        return jsonify({"ok": False, "error": "Không có quyền"}), 403
    import subprocess, sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    try:
        subprocess.Popen(
            [_sys.executable, str(_root / "scripts" / "sync_fb_budget.py")],
            cwd=str(_root),
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@chi_phi_qc_bp.route("/huong-dan")
@login_required
def huong_dan_ke_toan():
    """Trang hướng dẫn dành cho kế toán: cách đọc báo cáo + xử lý sai số."""
    return render_template("chi_phi_qc/huong_dan.html")


@chi_phi_qc_bp.route("/bao-cao-kt")
@login_required
def bao_cao_kt():
    """Báo cáo ads kế toán (Bc ads KT) — Reconciliation 100%, drill-down NV → TK → page,
    action items prompt. Triết lý "không sót 1 đồng": tổng các bucket = Truth FB API.

    KHÔNG thay thế /bao-cao cũ — chạy song song để dễ rollback.
    """
    from datetime import date as _date
    today = _date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()
    date_from = (request.args.get("date_from") or default_from).strip()
    date_to   = (request.args.get("date_to") or default_to).strip()

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Truth FB API (page-level + acct fallback combined)
                cur.execute("""
                    WITH page_lvl AS (
                        SELECT fb_ad_account_id, metric_date, SUM(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    acct_lvl AS (
                        SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
                          FROM fb_ads_daily_metrics
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    combined AS (
                        SELECT fb_ad_account_id, metric_date, spend FROM page_lvl
                        UNION ALL
                        SELECT a.fb_ad_account_id, a.metric_date, a.spend FROM acct_lvl a
                        LEFT JOIN page_lvl p
                          ON p.fb_ad_account_id = a.fb_ad_account_id AND p.metric_date = a.metric_date
                        WHERE p.fb_ad_account_id IS NULL
                    )
                    SELECT
                        SUM(spend)::numeric AS truth_total,
                        COUNT(DISTINCT fb_ad_account_id) AS tk_count
                    FROM combined
                """, (date_from, date_to, date_from, date_to))
                row = cur.fetchone()
                truth_total = float(row[0] or 0)
                tk_count = int(row[1] or 0)

                # 2. Đã quy về NV (qua uaa time-aware)
                cur.execute("""
                    WITH page_lvl AS (
                        SELECT fb_ad_account_id, metric_date, SUM(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    acct_lvl AS (
                        SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
                          FROM fb_ads_daily_metrics
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    combined AS (
                        SELECT fb_ad_account_id, metric_date, spend FROM page_lvl
                        UNION ALL
                        SELECT a.fb_ad_account_id, a.metric_date, a.spend FROM acct_lvl a
                        LEFT JOIN page_lvl p ON p.fb_ad_account_id=a.fb_ad_account_id AND p.metric_date=a.metric_date
                        WHERE p.fb_ad_account_id IS NULL
                    )
                    SELECT
                        SUM(CASE WHEN a.user_id IS NOT NULL THEN c.spend ELSE 0 END) AS spend_with_nv,
                        SUM(CASE WHEN a.user_id IS NULL     THEN c.spend ELSE 0 END) AS spend_no_nv,
                        COUNT(DISTINCT a.user_id) FILTER (WHERE a.user_id IS NOT NULL) AS nv_count
                    FROM combined c
                    LEFT JOIN user_ad_account_assignments a
                           ON a.ad_account_id = c.fb_ad_account_id
                          AND c.metric_date >= a.assigned_from
                          AND c.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                """, (date_from, date_to, date_from, date_to))
                row = cur.fetchone()
                spend_with_nv = float(row[0] or 0)
                spend_no_nv   = float(row[1] or 0)
                nv_count = int(row[2] or 0)

                # 3. Đã quy về Shop (qua page binding OR TK single-shop mapping)
                cur.execute("""
                    WITH page_lvl AS (
                        SELECT fb_ad_account_id, page_id, metric_date, SUM(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, page_id, metric_date
                    )
                    SELECT
                        SUM(CASE WHEN COALESCE(b.pos_shop_id, t.shop_id) IS NOT NULL THEN p.spend ELSE 0 END) AS with_shop,
                        SUM(CASE WHEN COALESCE(b.pos_shop_id, t.shop_id) IS NULL     THEN p.spend ELSE 0 END) AS no_shop,
                        COUNT(DISTINCT COALESCE(b.pos_shop_id, t.shop_id)) FILTER (WHERE COALESCE(b.pos_shop_id, t.shop_id) IS NOT NULL) AS shop_count
                    FROM page_lvl p
                    -- Page binding theo NGÀY: LATERAL lấy 1 binding mới nhất (chống nhân đôi
                    -- khi page có binding chồng ngày), đồng bộ _FB_ALLOCATED_CTE.
                    LEFT JOIN LATERAL (
                        SELECT bb.pos_shop_id
                          FROM fb_page_shop_binding bb
                         WHERE bb.page_id = p.page_id
                           AND p.metric_date >= bb.assigned_from
                           AND p.metric_date <= COALESCE(bb.assigned_to, DATE '9999-12-31')
                         ORDER BY bb.assigned_from DESC, bb.id DESC
                         LIMIT 1
                    ) b ON TRUE
                    -- Shop của TK theo NGÀY (date-aware, đồng bộ _FB_ALLOCATED_CTE)
                    LEFT JOIN LATERAL (
                        SELECT MIN(m.shop_id) AS shop_id
                          FROM fb_ad_account_mappings m
                         WHERE m.fb_ad_account_id = p.fb_ad_account_id
                           AND p.metric_date >= m.assigned_from
                           AND p.metric_date <= COALESCE(m.assigned_to, DATE '9999-12-31')
                        HAVING COUNT(DISTINCT m.shop_id) = 1
                    ) t ON TRUE
                """, (date_from, date_to))
                row = cur.fetchone()
                spend_with_shop = float(row[0] or 0)
                spend_no_shop   = float(row[1] or 0)
                shop_count = int(row[2] or 0)

                # 4. Drill-down NV → TK breakdown (cho click expand)
                cur.execute("""
                    WITH page_lvl AS (
                        SELECT fb_ad_account_id, metric_date, SUM(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    acct_lvl AS (
                        SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
                          FROM fb_ads_daily_metrics
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    combined AS (
                        SELECT fb_ad_account_id, metric_date, spend FROM page_lvl
                        UNION ALL
                        SELECT a.fb_ad_account_id, a.metric_date, a.spend FROM acct_lvl a
                        LEFT JOIN page_lvl p ON p.fb_ad_account_id=a.fb_ad_account_id AND p.metric_date=a.metric_date
                        WHERE p.fb_ad_account_id IS NULL
                    )
                    SELECT
                        a.user_id,
                        MAX(u.username)   AS username,
                        MAX(u.full_name)  AS full_name,
                        c.fb_ad_account_id,
                        COALESCE(MAX(ai.account_name), c.fb_ad_account_id) AS account_name,
                        SUM(c.spend) AS spend,
                        BOOL_OR(EXISTS (
                            SELECT 1 FROM fb_ad_account_mappings m
                             WHERE m.fb_ad_account_id = c.fb_ad_account_id AND m.assigned_to IS NULL
                        )) AS has_shop_mapping
                    FROM combined c
                    LEFT JOIN user_ad_account_assignments a
                           ON a.ad_account_id = c.fb_ad_account_id
                          AND c.metric_date >= a.assigned_from
                          AND c.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                    LEFT JOIN users u ON u.id = a.user_id
                    LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = c.fb_ad_account_id
                    GROUP BY a.user_id, c.fb_ad_account_id
                    ORDER BY SUM(c.spend) DESC
                """, (date_from, date_to, date_from, date_to))
                # Group TK rows per NV
                _nv_dict: dict = {}
                for r in cur.fetchall():
                    uid = r[0]
                    key = str(uid) if uid is not None else "_no_nv"
                    if key not in _nv_dict:
                        _nv_dict[key] = {
                            "user_id":   uid,
                            "username":  r[1] or "",
                            "full_name": r[2] or "",
                            "spend":     0.0,
                            "tk_count":  0,
                            "tks":       [],
                        }
                    sp = float(r[5] or 0)
                    _nv_dict[key]["spend"] += sp
                    _nv_dict[key]["tk_count"] += 1
                    _nv_dict[key]["tks"].append({
                        "ad_account_id": str(r[3] or ""),
                        "account_name":  r[4] or str(r[3]),
                        "spend":         sp,
                        "has_shop":      bool(r[6]),
                    })
                # Sort NV by spend desc
                nv_rows = sorted(_nv_dict.values(), key=lambda x: x["spend"], reverse=True)
                # Convert legacy fields for compat (template uses spend, tk_count)
                for nv in nv_rows:
                    nv["tks"].sort(key=lambda t: t["spend"], reverse=True)

                # 5. Action items
                cur.execute("""
                    WITH spent AS (
                        SELECT DISTINCT fb_ad_account_id FROM fb_ads_page_daily_spend
                        WHERE metric_date BETWEEN %s AND %s
                    )
                    SELECT
                        SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM user_ad_account_assignments a
                                                   WHERE a.ad_account_id=s.fb_ad_account_id
                                                     AND a.assigned_to IS NULL) THEN 1 ELSE 0 END) AS no_nv,
                        SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM fb_ad_account_mappings m
                                                   WHERE m.fb_ad_account_id=s.fb_ad_account_id
                                                     AND m.assigned_to IS NULL) THEN 1 ELSE 0 END) AS no_shop
                    FROM spent s
                """, (date_from, date_to))
                row = cur.fetchone()
                action_no_nv   = int(row[0] or 0)
                action_no_shop = int(row[1] or 0)

        # Tính %
        def pct(x): return (x / truth_total * 100) if truth_total > 0 else 0.0
        nv_pct       = pct(spend_with_nv)
        no_nv_pct    = pct(spend_no_nv)
        shop_pct     = pct(spend_with_shop)
        no_shop_pct  = pct(spend_no_shop)
        # Cross-check
        check_nv   = (spend_with_nv + spend_no_nv)
        check_shop = (spend_with_shop + spend_no_shop)
        sync_ok = abs(check_nv - truth_total) < 1 and abs(check_shop - truth_total) < 1
    except Exception as exc:
        logger.error("bao_cao_kt error: %s", exc, exc_info=True)
        truth_total = spend_with_nv = spend_no_nv = spend_with_shop = spend_no_shop = 0
        nv_pct = no_nv_pct = shop_pct = no_shop_pct = 0
        tk_count = nv_count = shop_count = 0
        nv_rows = []
        action_no_nv = action_no_shop = 0
        sync_ok = False

    # Invariant check — báo IT nếu lệch dù chỉ 1đ
    invariant = _verify_fb_bucket_invariant(date_from, date_to)

    return render_template(
        "chi_phi_qc/bao_cao_kt.html",
        date_from=date_from, date_to=date_to,
        truth_total=truth_total,
        spend_with_nv=spend_with_nv, spend_no_nv=spend_no_nv,
        spend_with_shop=spend_with_shop, spend_no_shop=spend_no_shop,
        nv_pct=nv_pct, no_nv_pct=no_nv_pct,
        shop_pct=shop_pct, no_shop_pct=no_shop_pct,
        tk_count=tk_count, nv_count=nv_count, shop_count=shop_count,
        nv_rows=nv_rows,
        action_no_nv=action_no_nv, action_no_shop=action_no_shop,
        sync_ok=sync_ok,
        invariant=invariant,
    )


@chi_phi_qc_bp.route("/set-auto-phan-loai", methods=["POST"])
@login_required
def set_auto_phan_loai():
    """Save Win/Test classification for an auto-detected page on a given date."""
    data      = request.get_json(silent=True) or {}
    page_id   = (data.get("page_id") or "").strip()
    sel_date  = (data.get("date") or "").strip()
    phan_loai = (data.get("phan_loai") or "ma_win").strip()
    user_id   = session.get("user_id", "")

    if not page_id or not sel_date:
        return jsonify({"ok": False, "error": "Thiếu page_id hoặc date"}), 400
    if phan_loai not in ("ma_win", "ma_test"):
        phan_loai = "ma_win"

    try:
        _save_auto_phan_loai(page_id, sel_date, phan_loai, user_id)
        return jsonify({"ok": True, "phan_loai": phan_loai})
    except Exception as e:
        logger.error("set_auto_phan_loai error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@chi_phi_qc_bp.route("/set-vat-rate", methods=["POST"])
@login_required
def set_vat_rate():
    """Lưu mức VAT (6.1% / 11.3%) NV chọn cho 1 page trên 1 ngày.
    Chỉ admin/superadmin/IT/ketoan/manager mới được phép chỉnh."""
    role = (session.get("role") or "").lower()
    if role not in ("admin", "superadmin", "it", "manager", "ketoan", "accountant"):
        return jsonify({"ok": False, "error": "Bạn không có quyền chỉnh VAT (chỉ admin/kế toán/IT)"}), 403
    data    = request.get_json(silent=True) or {}
    page_id = (data.get("page_id") or "").strip()
    sel_date = (data.get("date") or "").strip()
    try:
        vat_rate = float(data.get("vat_rate"))
    except (TypeError, ValueError):
        vat_rate = _DEFAULT_VAT_RATE
    if not page_id or not sel_date:
        return jsonify({"ok": False, "error": "Thiếu page_id hoặc date"}), 400
    # Chỉ chấp nhận 6.1% hoặc 11.3%
    vat_rate = min(_VAT_RATES, key=lambda r: abs(r - vat_rate))
    try:
        _save_vat_rate(page_id, sel_date, vat_rate, session.get("user_id", ""))
        return jsonify({"ok": True, "vat_rate": vat_rate})
    except Exception as e:
        logger.error("set_vat_rate error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@chi_phi_qc_bp.route("/api/sync-page-spend", methods=["POST"])
@login_required
def api_sync_page_spend():
    """Trigger sync_fb_ads_by_page script (admin/manager only). Supports single date or date range."""
    role = session.get("role", "staff")
    if role not in ("admin", "superadmin", "manager"):
        return jsonify({"ok": False, "error": "Chỉ admin/manager được sync"}), 403

    payload = request.get_json(silent=True) or {}
    date_from = payload.get("date_from", "").strip()
    date_to   = payload.get("date_to", "").strip()
    single    = payload.get("date", "").strip()

    import subprocess, sys, threading, os as _os, logging as _logging
    from pathlib import Path
    base = Path(__file__).resolve().parents[2]
    script_page = base / "scripts" / "sync_fb_ads_by_page.py"   # chi phí FB theo page
    script_pos = base / "run_auto_refresh_all_data.sh"          # đơn/doanh thu/lợi nhuận POS

    # ── Danh sách ngày cần kéo (giới hạn 31 ngày) ──
    if date_from and date_to:
        d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
        d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    else:
        d0 = d1 = (datetime.strptime(single, "%Y-%m-%d").date() if single else date.today())
    if d0 > d1:
        d0, d1 = d1, d0
    if (d1 - d0).days > 30:
        d0 = d1 - timedelta(days=30)
    days = []
    _cd = d0
    while _cd <= d1:
        days.append(_cd.isoformat())
        _cd += timedelta(days=1)

    synced_by = session.get("username", "system")

    # ── (1) Chi phí FB theo page — chạy NGAY (đồng bộ, cập nhật cột CP ADS FB) ──
    if len(days) > 1:
        cmd = [sys.executable, str(script_page), "--date-from", days[0], "--date-to", days[-1]]
        timeout_s = 600
    else:
        cmd = [sys.executable, str(script_page), "--date", days[0]]
        timeout_s = 180
    ok = True
    lines = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        ok = result.returncode == 0
        lines = (result.stdout + result.stderr).strip().splitlines()
        summary = next((l for l in reversed(lines) if "DONE" in l or "pages written" in l or "ERROR" in l), "")
        msg = summary or ("Sync chi phí FB hoàn tất" if ok else "Sync chi phí FB thất bại")
    except subprocess.TimeoutExpired:
        ok = False
        msg = f"Sync chi phí FB timeout (>{timeout_s}s)"
    except Exception as exc:
        ok = False
        msg = str(exc)

    # ── (2) POS (đơn/doanh thu/lợi nhuận + chi phí account) — chạy NỀN, không chặn UI ──
    def _pos_worker(day_list, oldest):
        env = _os.environ.copy()
        env["SKIP_SYNC_PRODUCTS"] = "1"   # không kéo sản phẩm
        env["SYNC_DAYS"] = str((date.today() - oldest).days + 2)
        for ds in day_list:
            try:
                subprocess.run(["bash", str(script_pos), ds], cwd=str(base),
                               check=False, timeout=1800, env=env)
            except Exception:
                _logging.getLogger("chi_phi_qc").exception("[sync] POS refresh lỗi %s", ds)
    threading.Thread(target=_pos_worker, args=(days, d0), daemon=True).start()

    for ds in days:
        _log_sync(ds, synced_by, "ok" if ok else "error", msg)
    synced_at_str = now_hcm().strftime("%H:%M %d/%m/%Y")
    return jsonify({
        "ok": ok,
        "message": msg + " · Đang kéo POS (đơn/doanh thu/lợi nhuận) trong nền…",
        "log": lines[-20:],
        "synced_at": synced_at_str,
    })


@chi_phi_qc_bp.route("/api/last-sync-time")
@login_required
def api_last_sync_time():
    """Return the last sync time for a given date as a formatted string."""
    d = request.args.get("date", date.today().isoformat()).strip()
    info = _get_last_sync(d)
    if info and info.get("synced_at"):
        formatted = fmt_hcm(info["synced_at"], "%H:%M %d/%m/%Y")
        return jsonify({"ok": True, "synced_at": formatted, "synced_by": info.get("synced_by", "")})
    return jsonify({"ok": False, "synced_at": None})


@chi_phi_qc_bp.route("/api/page-ad-accounts")
@login_required
def api_page_ad_accounts():
    page_id = request.args.get("page_id", "").strip()
    if not page_id:
        return jsonify([])
    return jsonify(_page_ad_accounts(page_id))


@chi_phi_qc_bp.route("/api/my-ad-accounts")
@login_required
def api_my_ad_accounts():
    """Return ad accounts pre-mapped to the current user."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify([])
    return jsonify(_my_ad_accounts(user_id))


@chi_phi_qc_bp.route("/api/declarations")
@login_required
def api_declarations():
    page_id    = request.args.get("page_id", "").strip()
    spend_date = request.args.get("spend_date", "").strip()
    user_id    = session.get("user_id")
    role       = session.get("role", "staff")

    if not page_id or not spend_date:
        return jsonify([])

    rows = _declarations_for_page_date(page_id, spend_date, user_id, role)
    result = [{k: _serialize(v) for k, v in d.items()} for d in rows]
    return jsonify(result)


@chi_phi_qc_bp.route("/declare", methods=["POST"])
@login_required
def declare():
    page_id            = request.form.get("page_id", "").strip()
    spend_date         = request.form.get("spend_date", "").strip()
    ad_account_id      = request.form.get("ad_account_id", "").strip()
    ad_account_name    = request.form.get("ad_account_name", "").strip()
    phan_loai          = request.form.get("phan_loai", "ma_win").strip()
    tien_raw           = request.form.get("tien_chua_thue", "0").strip().replace(",", "")
    note               = request.form.get("note", "").strip()
    auto_assign        = request.form.get("auto_assign_to_page") == "1"
    user_id            = session.get("user_id")
    redirect_date      = spend_date or date.today().isoformat()

    if not page_id or not user_id:
        flash("Thiếu thông tin khai báo.", "warning")
        return redirect(url_for("chi_phi_qc.index"))

    if phan_loai not in ("ma_win", "ma_test"):
        phan_loai = "ma_win"

    try:
        tien_chua_thue = float(tien_raw) if tien_raw else 0
    except ValueError:
        tien_chua_thue = 0

    if tien_chua_thue <= 0:
        flash("Vui lòng nhập tiền chưa VAT hợp lệ (> 0).", "warning")
        return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_date}")

    thue_rate  = 0.0
    thanh_tien = round(tien_chua_thue * (1 + thue_rate), 2)
    sdate      = spend_date if spend_date else date.today().isoformat()

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Save the declaration
                cur.execute("""
                    INSERT INTO fb_page_spend_declarations
                        (page_id, user_id, spend_date, amount, note,
                         ad_account_id, ad_account_name, phan_loai,
                         tien_chua_thue, thue_rate, thanh_tien)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (page_id, user_id, sdate, thanh_tien, note,
                      ad_account_id or None, ad_account_name or None,
                      phan_loai, tien_chua_thue, thue_rate, thanh_tien))

                # Auto-assign ad account to page if requested
                if auto_assign and ad_account_id:
                    cur.execute("""
                        INSERT INTO fb_page_ad_account_map
                            (page_id, ad_account_id, ad_account_name, shop_key)
                        VALUES (%s, %s, %s, '')
                        ON CONFLICT (page_id) DO UPDATE SET
                            ad_account_id   = EXCLUDED.ad_account_id,
                            ad_account_name = EXCLUDED.ad_account_name,
                            updated_at      = NOW()
                    """, (page_id, ad_account_id, ad_account_name or ad_account_id))

        flash("Đã lưu khai báo chi tiêu." + (" TK QC đã gán vào page." if auto_assign and ad_account_id else ""), "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_date}")


@chi_phi_qc_bp.route("/declare-manual", methods=["POST"])
@login_required
def declare_manual():
    """Handle manual page declaration (user types page name directly)."""
    page_name_manual = request.form.get("page_name_manual", "").strip()
    spend_date       = request.form.get("spend_date", "").strip()
    phan_loai        = request.form.get("phan_loai", "ma_win").strip()
    tien_raw         = request.form.get("tien_chua_thue", "0").strip().replace(",", "")
    note             = request.form.get("note", "").strip()
    for_user_id_raw  = request.form.get("for_user_id", "").strip()
    current_user_id  = session.get("user_id")
    role             = session.get("role", "staff")
    redirect_date    = spend_date or date.today().isoformat()

    # Admin/ketoan/leader can declare on behalf of another user
    if for_user_id_raw and _is_senior(role):
        user_id = for_user_id_raw
    else:
        user_id = current_user_id

    if not page_name_manual:
        flash("Vui lòng nhập tên page.", "warning")
        return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_date}")

    if not user_id:
        flash("Phiên đăng nhập hết hạn, vui lòng đăng nhập lại.", "warning")
        return redirect(f"/login?next=/chi-phi-qc/?date={redirect_date}")

    if phan_loai not in ("ma_win", "ma_test"):
        phan_loai = "ma_win"

    try:
        tien_chua_thue = float(tien_raw) if tien_raw else 0
    except ValueError:
        tien_chua_thue = 0

    if tien_chua_thue <= 0:
        flash("Vui lòng nhập tiền chưa VAT hợp lệ (> 0).", "warning")
        return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_date}")

    thue_rate  = 0.0
    thanh_tien = round(tien_chua_thue * (1 + thue_rate), 2)
    sdate      = spend_date if spend_date else date.today().isoformat()

    # Generate a unique MANUAL page_id (per user + page_name + date to allow grouping)
    import hashlib as _hashlib
    slug = _hashlib.md5(f"{user_id}|{page_name_manual}|{sdate}".encode()).hexdigest()[:12].upper()
    page_id = f"MANUAL_{slug}"

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_page_spend_declarations
                        (page_id, user_id, spend_date, amount, note,
                         ad_account_id, ad_account_name, phan_loai,
                         tien_chua_thue, thue_rate, thanh_tien, page_name_manual)
                    VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s)
                """, (page_id, user_id, sdate, thanh_tien, note or None,
                      phan_loai, tien_chua_thue, thue_rate, thanh_tien, page_name_manual))

        flash(f"Đã lưu khai báo thủ công cho \"{page_name_manual}\".", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")

    return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_date}")


@chi_phi_qc_bp.route("/delete", methods=["POST"])
@login_required
def delete():
    decl_id     = request.form.get("decl_id", "").strip()
    redirect_to = request.form.get("redirect_date", date.today().isoformat()).strip()
    user_id     = session.get("user_id")
    role        = session.get("role", "staff")
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
    return redirect(url_for("chi_phi_qc.index") + f"?date={redirect_to}")


@chi_phi_qc_bp.route("/edit-declaration", methods=["POST"])
@login_required
def edit_declaration():
    """Edit an existing declaration (amount, phan_loai, note, page_name_manual)."""
    decl_id          = request.form.get("decl_id", "").strip()
    tien_raw         = request.form.get("tien_chua_thue", "0").strip().replace(",", "")
    phan_loai        = request.form.get("phan_loai", "ma_win").strip()
    note             = request.form.get("note", "").strip()
    page_name_manual = request.form.get("page_name_manual", "").strip()
    redirect_date    = request.form.get("redirect_date", date.today().isoformat()).strip()
    user_id          = session.get("user_id")
    role             = session.get("role", "staff")

    if not decl_id:
        return jsonify({"ok": False, "error": "Thiếu ID khai báo"}), 400

    if phan_loai not in ("ma_win", "ma_test"):
        phan_loai = "ma_win"

    try:
        tien_chua_thue = float(tien_raw) if tien_raw else 0
    except ValueError:
        tien_chua_thue = 0

    if tien_chua_thue <= 0:
        return jsonify({"ok": False, "error": "Số tiền không hợp lệ"}), 400

    thue_rate  = 0.0
    thanh_tien = round(tien_chua_thue * (1 + thue_rate), 2)

    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Permission: admin/manager can edit any; others only own
                if role in ("admin", "superadmin", "manager", "ketoan", "accountant"):
                    cur.execute("""
                        UPDATE fb_page_spend_declarations SET
                            tien_chua_thue   = %s,
                            thue_rate        = %s,
                            thanh_tien       = %s,
                            amount           = %s,
                            phan_loai        = %s,
                            note             = %s,
                            page_name_manual = CASE WHEN page_name_manual IS NOT NULL THEN %s ELSE page_name_manual END,
                            updated_at       = NOW()
                        WHERE id = %s
                    """, (tien_chua_thue, thue_rate, thanh_tien, thanh_tien,
                          phan_loai, note or None,
                          page_name_manual or None,
                          decl_id))
                else:
                    cur.execute("""
                        UPDATE fb_page_spend_declarations SET
                            tien_chua_thue   = %s,
                            thue_rate        = %s,
                            thanh_tien       = %s,
                            amount           = %s,
                            phan_loai        = %s,
                            note             = %s,
                            page_name_manual = CASE WHEN page_name_manual IS NOT NULL THEN %s ELSE page_name_manual END,
                            updated_at       = NOW()
                        WHERE id = %s AND user_id = %s
                    """, (tien_chua_thue, thue_rate, thanh_tien, thanh_tien,
                          phan_loai, note or None,
                          page_name_manual or None,
                          decl_id, user_id))
                if cur.rowcount == 0:
                    return jsonify({"ok": False, "error": "Không tìm thấy khai báo hoặc bạn không có quyền"}), 403
        return jsonify({"ok": True, "thanh_tien": thanh_tien})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Snapshot table ───────────────────────────────────────────────────
def _ensure_snapshots_table():
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chi_phi_qc_period_snapshots (
                        id               BIGSERIAL PRIMARY KEY,
                        period_label     VARCHAR(40) NOT NULL,
                        date_from        DATE NOT NULL,
                        date_to          DATE NOT NULL,
                        note             TEXT,
                        locked_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        locked_by_user_id BIGINT,
                        locked_by_name   VARCHAR(120),
                        total_rows       INT     DEFAULT 0,
                        total_amount     NUMERIC(18,2) DEFAULT 0,
                        data_json        JSONB   NOT NULL DEFAULT '{}'::jsonb
                    )
                """)
    except Exception as exc:
        logger.error("_ensure_snapshots_table error: %s", exc)


# ── Report helpers ────────────────────────────────────────────────────
def _build_user_filter(cur, user_id, role, params_prefix):
    """Return (user_filter_sql, extra_params) for declaration queries."""
    if role in ("admin", "superadmin", "manager", "ketoan", "accountant"):
        return "", []
    if role == "leader":
        cur.execute("""
            SELECT u.id FROM users u
            JOIN teams t ON t.id = u.team_id
            WHERE t.leader_user_id = %s AND u.status = 'active'
        """, (user_id,))
        team_ids = [r[0] for r in cur.fetchall()]
        if user_id not in team_ids:
            team_ids.append(user_id)
        ph = ",".join(["%s"] * len(team_ids))
        return f"AND d.user_id IN ({ph})", team_ids
    return "AND d.user_id = %s", [user_id]


def _query_employee_summary(conn, date_from, date_to, user_id, role):
    uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
    with conn.cursor() as cur:
        uf, ep = _build_user_filter(cur, user_id, role, [])
        cur.execute(f"""
            SELECT
                d.user_id,
                COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.tien_chua_thue ELSE 0 END),0) AS win_chua_vat,
                COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.thanh_tien     ELSE 0 END),0) AS win_co_vat,
                COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.tien_chua_thue ELSE 0 END),0) AS test_chua_vat,
                COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.thanh_tien     ELSE 0 END),0) AS test_co_vat,
                COALESCE(SUM(d.thanh_tien),0) AS tong
            FROM fb_page_spend_declarations d
            WHERE d.spend_date BETWEEN %s AND %s {uf}
            GROUP BY d.user_id
            ORDER BY d.user_id
        """, [date_from, date_to] + ep)
        rows = []
        for r in cur.fetchall():
            uid, win_chua, win_co, test_chua, test_co, tong = r
            u = uid_to_user.get(str(uid) if uid else "", {})
            rows.append({
                "user_id":      str(uid) if uid else "",
                "full_name":    u.get("full_name") or u.get("username") or str(uid or ""),
                "username":     u.get("username") or str(uid or ""),
                "win_chua_vat": float(win_chua),
                "win_co_vat":   float(win_co),
                "test_chua_vat":float(test_chua),
                "test_co_vat":  float(test_co),
                "tong":         float(tong),
            })
        return sorted(rows, key=lambda x: x["full_name"])


# ════════════════════════════════════════════════════════════════════════════
# SINGLE SOURCE OF TRUTH cho FB ads bucket allocation (2026-05-15)
# ════════════════════════════════════════════════════════════════════════════
# Mọi report (shop/NV/Bc ads KT/cron invariant) PHẢI build trên CTE `allocated`
# này. Không định nghĩa lại bucket logic ở nơi khác → không thể drift.
#
# `allocated` columns:
#   fb_ad_account_id, page_id (NULL nếu là acct-level fallback), metric_date,
#   spend, shop_id (NULL = unbound), user_id (NULL = chưa gán NV qua uaa)
#
# Bất biến (verify bằng _verify_fb_bucket_invariant): SUM(allocated.spend)
# = SUM(combined source) = truth FB API.
#
# 4 params bắt buộc: (date_from, date_to, date_from, date_to)
# ════════════════════════════════════════════════════════════════════════════
_FB_ALLOCATED_CTE = """
    WITH page_lvl AS (
        SELECT fb_ad_account_id, page_id, metric_date, SUM(spend) AS spend
          FROM fb_ads_page_daily_spend
         WHERE metric_date BETWEEN %s AND %s
         GROUP BY fb_ad_account_id, page_id, metric_date
    ),
    acct_lvl AS (
        SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
          FROM fb_ads_daily_metrics
         WHERE metric_date BETWEEN %s AND %s
         GROUP BY fb_ad_account_id, metric_date
    ),
    combined AS (
        SELECT fb_ad_account_id, page_id, metric_date, spend FROM page_lvl
        UNION ALL
        SELECT a.fb_ad_account_id, NULL::TEXT AS page_id, a.metric_date, a.spend
          FROM acct_lvl a
          LEFT JOIN (SELECT DISTINCT fb_ad_account_id, metric_date FROM page_lvl) p
                 ON p.fb_ad_account_id = a.fb_ad_account_id
                AND p.metric_date = a.metric_date
         WHERE p.fb_ad_account_id IS NULL
    ),
    allocated AS (
        SELECT
            s.fb_ad_account_id,
            s.page_id,
            s.metric_date,
            s.spend,
            -- Ưu tiên: binding tay > SHOP CÓ CHI PHÍ ADS POS (đơn về đâu, FB về đó —
            -- yêu cầu KT 20/6) > mapping tài khoản. Page chạy ads ở TK shop A nhưng đơn
            -- về shop B (có POS) → FB dồn về B cùng POS để đối chiếu 1 chỗ.
            COALESCE(b.pos_shop_id, posh.shop_id, t.shop_id) AS shop_id,
            a.user_id
          FROM combined s
          -- Page binding theo NGÀY: LATERAL lấy ĐÚNG 1 binding (mới nhất) per (page, ngày).
          -- Trước đây LEFT JOIN thường → nếu page có 2 binding chồng ngày (data lỗi) thì
          -- spend bị NHÂN ĐÔI → tổng bucket > truth FB (banner lệch). LIMIT 1 chặn nhân đôi.
          LEFT JOIN LATERAL (
              SELECT bb.pos_shop_id
                FROM fb_page_shop_binding bb
               WHERE bb.page_id = s.page_id
                 AND s.metric_date >= bb.assigned_from
                 AND s.metric_date <= COALESCE(bb.assigned_to, DATE '9999-12-31')
               ORDER BY bb.assigned_from DESC, bb.id DESC
               LIMIT 1
          ) b ON TRUE
          -- SHOP CÓ CHI PHÍ ADS POS cho page+ngày: LATERAL lấy ĐÚNG 1 shop có POS ads lớn
          -- nhất (LIMIT 1 → không nhân spend). FB của page dồn về shop nhận đơn (có CP ads POS).
          LEFT JOIN LATERAL (
              SELECT pp.shop_id
                FROM pos_page_daily_metrics pp
               WHERE pp.page_id = s.page_id
                 AND pp.metric_date = s.metric_date
                 AND pp.shop_id IS NOT NULL
                 AND pp.ads_amount > 0
               ORDER BY pp.ads_amount DESC, pp.shop_id
               LIMIT 1
          ) posh ON TRUE
          -- Shop của TK theo NGÀY (date-aware): đọc fb_ad_account_mappings phủ metric_date,
          -- CHỈ gán khi đúng 1 shop ngày đó (COUNT DISTINCT = 1) → không nhân spend khi TK
          -- chạy nhiều shop cùng ngày (trường hợp đó cần bind page riêng). Thay tk_single cũ
          -- (chỉ lấy mapping đang mở, KHÔNG căn ngày → bỏ sót mapping lịch sử đã đóng).
          LEFT JOIN LATERAL (
              SELECT MIN(m.shop_id) AS shop_id
                FROM fb_ad_account_mappings m
               WHERE m.fb_ad_account_id = s.fb_ad_account_id
                 AND s.metric_date >= m.assigned_from
                 AND s.metric_date <= COALESCE(m.assigned_to, DATE '9999-12-31')
              HAVING COUNT(DISTINCT m.shop_id) = 1
          ) t ON TRUE
          -- LATERAL pick uaa mới nhất per (TK, ngày) để KHÔNG nhân spend khi
          -- 1 TK lỡ có nhiều uaa overlap (data quality issue ở admin gán NV).
          -- Đảm bảo invariant: 1 row combined → đúng 1 row allocated.
          LEFT JOIN LATERAL (
              SELECT a.user_id
                FROM user_ad_account_assignments a
               WHERE a.ad_account_id = s.fb_ad_account_id
                 AND s.metric_date >= a.assigned_from
                 AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
               ORDER BY a.assigned_from DESC, a.id DESC
               LIMIT 1
          ) a ON TRUE
    )
"""


def _verify_fb_bucket_invariant(date_from: str, date_to: str) -> dict:
    """Kiểm tra bất biến: SUM(buckets) = SUM(combined source) = truth FB API.

    Trả về dict {
      'truth':       float,  # tổng combined source
      'buckets':     float,  # SUM(allocated.spend) — nếu khớp truth thì OK
      'bound':       float,  # spend đã gán shop
      'unbound':     float,  # spend chưa gán shop
      'diff':        float,  # buckets - truth (phải = 0)
      'ok':          bool,   # |diff| ≤ 1đ
    }
    Mỗi báo cáo gọi hàm này → nếu ok=False thì show banner đỏ.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_FB_ALLOCATED_CTE + """
                    SELECT
                        COALESCE(SUM(spend), 0) AS total,
                        COALESCE(SUM(CASE WHEN shop_id IS NOT NULL THEN spend ELSE 0 END), 0) AS bound,
                        COALESCE(SUM(CASE WHEN shop_id IS     NULL THEN spend ELSE 0 END), 0) AS unbound
                    FROM allocated
                """, (date_from, date_to, date_from, date_to))
                total, bound, unbound = cur.fetchone()
                total   = float(total or 0)
                bound   = float(bound or 0)
                unbound = float(unbound or 0)
                # Truth = lại sum combined source riêng để cross-check CTE
                cur.execute("""
                    WITH page_lvl AS (
                        SELECT fb_ad_account_id, metric_date, SUM(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    ),
                    acct_lvl AS (
                        SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
                          FROM fb_ads_daily_metrics
                         WHERE metric_date BETWEEN %s AND %s
                         GROUP BY fb_ad_account_id, metric_date
                    )
                    SELECT COALESCE(SUM(spend), 0) FROM (
                        SELECT spend FROM page_lvl
                        UNION ALL
                        SELECT a.spend FROM acct_lvl a
                          LEFT JOIN page_lvl p
                                 ON p.fb_ad_account_id = a.fb_ad_account_id
                                AND p.metric_date = a.metric_date
                         WHERE p.fb_ad_account_id IS NULL
                    ) x
                """, (date_from, date_to, date_from, date_to))
                truth = float(cur.fetchone()[0] or 0)
                diff = total - truth
                return {
                    "truth":   truth,
                    "buckets": total,
                    "bound":   bound,
                    "unbound": unbound,
                    "diff":    diff,
                    "ok":      abs(diff) <= 1.0,
                }
    except Exception as e:
        logger.error("_verify_fb_bucket_invariant error: %s", e)
        return {"truth": 0.0, "buckets": 0.0, "bound": 0.0, "unbound": 0.0,
                "diff": 0.0, "ok": False, "error": str(e)}


def _query_fb_ads_by_shop_id(date_from: str, date_to: str) -> dict:
    """Return {shop_id: fb_spend, '_unbound': fb_spend} — spend FB Ads theo shop.
    Dùng `_FB_ALLOCATED_CTE` làm nguồn truth → đồng bộ với các báo cáo khác.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_FB_ALLOCATED_CTE + """
                    SELECT shop_id, SUM(spend) AS spend
                      FROM allocated
                     GROUP BY shop_id
                """, (date_from, date_to, date_from, date_to))
                result: dict = {}
                for r in cur.fetchall():
                    sid = r[0]
                    spend = float(r[1] or 0)
                    if sid is None:
                        result["_unbound"] = result.get("_unbound", 0.0) + spend
                    else:
                        result[int(sid)] = spend
                return result
    except Exception as e:
        logger.warning("_query_fb_ads_by_shop_id error: %s", e)
        return {}


def _query_page_kpi_by_shop_id(date_from: str, date_to: str) -> dict:
    """Return {shop_id: {...}, '_unbound': {...}} — KPI Win/Test theo shop với 3-tier
    (page binding → TK đơn shop → unbound). Không double, không mất tiền."""
    _ensure_auto_phan_loai_table()
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Build trên _FB_ALLOCATED_CTE (single source of truth) + JOIN
                # fb_page_auto_phan_loai để gán Win/Test theo page-date.
                # Acct-fallback rows có page_id NULL → không match → COALESCE 'ma_win'.
                #
                # VAT rate per-page (KHỚP với team tab /chi-phi-qc/), 3-tier:
                #   #1 fb_page_vat_rate (admin/NV override per page — chọn 6.1%/11.3%)
                #   #2 fb_ad_account_vat_options.default_vat_rate (mặc định mỗi TK)
                #   #3 0.113 (fallback cuối cho TK chưa khai)
                # → Không hardcode 1.113 ở chỗ aggregate nữa (bug cũ).
                # WIN = page CÓ trên POS (pk.page_id NOT NULL) VÀ không bị bấm Test.
                # Còn lại (chưa lên POS, hoặc bấm Test, hoặc acct-fallback page NULL) = TEST.
                cur.execute(_FB_ALLOCATED_CTE + """
                    SELECT
                        s.shop_id,
                        ROUND(SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test'
                                       THEN s.spend ELSE 0 END)::numeric, 0) AS win_chua,
                        ROUND(SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test'
                                       THEN s.spend * (1.0 + COALESCE(pr.vat_rate, tr.default_vat_rate, 0.0))
                                       ELSE 0 END)::numeric, 0) AS win_co,
                        ROUND(SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test')
                                       THEN s.spend ELSE 0 END)::numeric, 0) AS test_chua,
                        ROUND(SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test')
                                       THEN s.spend * (1.0 + COALESCE(pr.vat_rate, tr.default_vat_rate, 0.0))
                                       ELSE 0 END)::numeric, 0) AS test_co,
                        ROUND(SUM(s.spend * (1.0 + COALESCE(pr.vat_rate, tr.default_vat_rate, 0.0)))::numeric, 0) AS tong_co
                    FROM allocated s
                    LEFT JOIN fb_page_auto_phan_loai pl
                           ON pl.page_id = s.page_id AND pl.date = s.metric_date
                    LEFT JOIN (SELECT DISTINCT page_id FROM pos_page_daily_metrics) pk
                           ON pk.page_id = s.page_id
                    LEFT JOIN (
                        SELECT DISTINCT ON (page_id) page_id, vat_rate
                          FROM fb_page_vat_rate
                         WHERE date BETWEEN %s AND %s
                         ORDER BY page_id, date DESC
                    ) pr ON pr.page_id = s.page_id
                    LEFT JOIN fb_ad_account_vat_options tr
                           ON tr.fb_ad_account_id = s.fb_ad_account_id
                    GROUP BY s.shop_id
                """, (date_from, date_to, date_from, date_to, date_from, date_to))
                result: dict = {}
                for row in cur.fetchall():
                    sid = row[0]
                    bucket = {
                        "win_chua": float(row[1] or 0),
                        "win_co":   float(row[2] or 0),
                        "test_chua":float(row[3] or 0),
                        "test_co":  float(row[4] or 0),
                        "tong_co":  float(row[5] or 0),
                    }
                    if sid is None:
                        result["_unbound"] = bucket
                    else:
                        result[int(sid)] = bucket
                return result
    except Exception as e:
        logger.warning("_query_page_kpi_by_shop_id error: %s", e)
        return {}


def _query_pos_ads_by_shop_id(date_from: str, date_to: str) -> dict:
    """Return {shop_id: pos_ads_cost} cho khoảng ngày — KHỚP với modal "Chi tiết theo ngày".

    POS gán theo FB-shop (page nào FB về shop nào thì POS theo đó). Kết hợp posh override
    trong _FB_ALLOCATED_CTE (FB đã dồn về shop có POS) → POS cũng về đúng shop đó, bảng
    khớp modal. KHÔNG gán POS theo Pancake shop trực tiếp (sẽ gom cả đơn từ ads shop khác
    → phình; bug 20/6 Thảo Linh HCM 3 báo 18.5tr trong khi modal chỉ 1.04tr).
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_FB_ALLOCATED_CTE + """
                    , page_shop AS (
                        SELECT DISTINCT shop_id, page_id, metric_date
                          FROM allocated
                         WHERE shop_id IS NOT NULL AND page_id IS NOT NULL
                    )
                    SELECT ps.shop_id, COALESCE(SUM(pp.ads_amount), 0)
                      FROM page_shop ps
                      JOIN pos_page_daily_metrics pp
                             ON pp.page_id = ps.page_id
                            AND pp.metric_date = ps.metric_date
                     GROUP BY ps.shop_id
                """, (date_from, date_to, date_from, date_to))
                return {int(r[0]): float(r[1]) for r in cur.fetchall() if r[0] is not None}
    except Exception as e:
        logger.warning("_query_pos_ads_by_shop_id error: %s", e)
        return {}


def _query_pos_page_metrics(date_from: str, date_to: str) -> dict:
    """Return {page_id: {ads, revenue, profit, chot, hoan}} từ POS (Pancake) theo page.

    Số liệu lấy từ pos_page_daily_metrics (sync_pos_page_metrics.py kéo qua endpoint
    analytics/sale split_by Order.source). Dùng để ĐỐI CHIẾU CP QC POS vs CP Ads FB.
    Page chưa có trên POS → không có key → template để trống cột.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.page_id,
                           COALESCE(SUM(m.ads_amount), 0),
                           COALESCE(SUM(m.revenue), 0),
                           COALESCE(SUM(m.profit), 0),
                           COALESCE(SUM(m.success_order_count), 0),
                           COALESCE(SUM(m.returned_order_count), 0),
                           COALESCE(SUM(m.sales), 0),
                           MAX(m.page_name),
                           (SELECT m2.pos_shop_id FROM pos_page_daily_metrics m2
                             WHERE m2.page_id = m.page_id AND m2.metric_date BETWEEN %s AND %s
                               AND m2.pos_shop_id IS NOT NULL
                             GROUP BY m2.pos_shop_id ORDER BY SUM(m2.revenue) DESC NULLS LAST LIMIT 1) AS top_shop
                      FROM pos_page_daily_metrics m
                     WHERE m.metric_date BETWEEN %s AND %s
                     GROUP BY m.page_id
                """, (date_from, date_to, date_from, date_to))
                out = {}
                for r in cur.fetchall():
                    if not r[0]:
                        continue
                    ads, chot = float(r[1]), int(r[4])
                    out[str(r[0])] = {
                        "ads":     ads,
                        "revenue": float(r[2]),
                        "profit":  float(r[3]),
                        "chot":    chot,
                        "hoan":    int(r[5]),
                        "sales":   float(r[6]),  # tiền hàng (trước chiết khấu)
                        "name":    (r[7] or "").strip(),
                        "pos_shop_id": (str(r[8]) if r[8] else None),  # shop POS chính → link Pancake
                        # Chi phí QC / đơn chốt (như cột "Chi phí QC / Đơn hàng" của Pancake)
                        "cp_per_order": round(ads / chot, 0) if chot > 0 else 0,
                    }
                return out
    except Exception as e:
        logger.warning("_query_pos_page_metrics error: %s", e)
        return {}


def _query_pos_known_pages(page_ids=None) -> dict:
    """{page_id: page_name} cho MỌI page TỪNG xuất hiện trên POS (bất kỳ ngày nào).

    Dùng để phân biệt: page là page POS (đã từng có số) nhưng kỳ đang xem chưa có
    số → KHÔNG ghi 'Chưa có trên POS' (gây hiểu nhầm), mà hiện là page POS chưa có
    số trong kỳ. Page chưa bao giờ lên POS mới ghi 'Chưa có trên POS'.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if page_ids:
                    cur.execute("""
                        SELECT DISTINCT ON (page_id) page_id, page_name
                          FROM pos_page_daily_metrics
                         WHERE page_id = ANY(%s)
                         ORDER BY page_id, metric_date DESC
                    """, (list(page_ids),))
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (page_id) page_id, page_name
                          FROM pos_page_daily_metrics
                         ORDER BY page_id, metric_date DESC
                    """)
                return {str(r[0]): (r[1] or "").strip() for r in cur.fetchall() if r[0]}
    except Exception as e:
        logger.warning("_query_pos_known_pages error: %s", e)
        return {}


def _query_page_breakdown(conn, date_from, date_to, user_id, role):
    """Per-page: FB actual spend + declared (chưa VAT / đã VAT).
    For auto-detected pages (fb_spend > 0), Win/Test split is taken from
    fb_page_auto_phan_loai (default = ma_win). Manual declarations are used
    for pages without FB auto data.
    """
    _ensure_auto_phan_loai_table()
    with conn.cursor() as cur:
        uf, ep = _build_user_filter(cur, user_id, role, [])
        cur.execute(f"""
            WITH fb AS (
                SELECT s.page_id,
                       COALESCE(
                           NULLIF(MAX(p.page_name), ''),
                           NULLIF(MAX(s.page_name), ''),
                           'Page ' || RIGHT(MAX(s.page_id), 8)
                       ) AS page_name,
                       SUM(s.spend)       AS fb_spend,
                       SUM(s.impressions) AS fb_impr,
                       SUM(s.clicks)      AS fb_clicks,
                       -- Win/Test: WIN = page CÓ trên POS (pk NOT NULL) & không bấm Test; còn lại = Test
                       ROUND(SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test'
                                      THEN s.spend         ELSE 0 END)::numeric, 0) AS auto_win_chua,
                       ROUND(SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test'
                                      THEN s.spend * 1.113 ELSE 0 END)::numeric, 0) AS auto_win_co,
                       ROUND(SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test')
                                      THEN s.spend         ELSE 0 END)::numeric, 0) AS auto_test_chua,
                       ROUND(SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') != 'ma_test')
                                      THEN s.spend * 1.113 ELSE 0 END)::numeric, 0) AS auto_test_co
                FROM fb_ads_page_daily_spend s
                LEFT JOIN fb_pages p ON p.page_id = s.page_id
                LEFT JOIN fb_page_auto_phan_loai pl
                       ON pl.page_id = s.page_id AND pl.date = s.metric_date
                LEFT JOIN (SELECT DISTINCT page_id FROM pos_page_daily_metrics) pk
                       ON pk.page_id = s.page_id
                WHERE s.metric_date BETWEEN %s AND %s
                GROUP BY s.page_id
            ),
            decl AS (
                SELECT d.page_id,
                       COALESCE(NULLIF(MAX(p.page_name),''), 'Page '||RIGHT(MAX(d.page_id),8)) AS page_name_d,
                       SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.tien_chua_thue ELSE 0 END) AS win_chua,
                       SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.thanh_tien     ELSE 0 END) AS win_co,
                       SUM(CASE WHEN d.phan_loai='ma_test' THEN d.tien_chua_thue ELSE 0 END) AS test_chua,
                       SUM(CASE WHEN d.phan_loai='ma_test' THEN d.thanh_tien     ELSE 0 END) AS test_co,
                       SUM(d.tien_chua_thue) AS khai_chua,
                       SUM(d.thanh_tien)     AS khai_co,
                       COUNT(d.id)           AS so_khai
                FROM fb_page_spend_declarations d
                LEFT JOIN fb_pages p ON p.page_id = d.page_id
                WHERE d.spend_date BETWEEN %s AND %s {uf}
                GROUP BY d.page_id
            )
            SELECT
                COALESCE(fb.page_id, decl.page_id)          AS page_id,
                COALESCE(fb.page_name, decl.page_name_d,'') AS page_name,
                COALESCE(fb.fb_spend,  0)   AS fb_spend,
                COALESCE(fb.fb_impr,   0)   AS fb_impr,
                COALESCE(fb.fb_clicks, 0)   AS fb_clicks,
                -- Effective Win: auto pages use fb_page_auto_phan_loai; manual pages use declarations
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN COALESCE(fb.auto_win_chua, 0)
                     ELSE COALESCE(decl.win_chua, 0) END    AS win_chua,
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN COALESCE(fb.auto_win_co, 0)
                     ELSE COALESCE(decl.win_co, 0) END      AS win_co,
                -- Effective Test
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN COALESCE(fb.auto_test_chua, 0)
                     ELSE COALESCE(decl.test_chua, 0) END   AS test_chua,
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN COALESCE(fb.auto_test_co, 0)
                     ELSE COALESCE(decl.test_co, 0) END     AS test_co,
                -- Effective total: auto pages → fb*1.113; manual → declared
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN ROUND((fb.fb_spend * 1.113)::numeric, 0)
                     ELSE COALESCE(decl.khai_chua, 0) END   AS khai_chua,
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN ROUND((fb.fb_spend * 1.113)::numeric, 0)
                     ELSE COALESCE(decl.khai_co, 0) END     AS khai_co,
                CASE WHEN COALESCE(fb.fb_spend,0) > 0
                     THEN 0
                     ELSE COALESCE(decl.so_khai, 0) END     AS so_khai
            FROM fb FULL OUTER JOIN decl ON decl.page_id = fb.page_id
            ORDER BY fb_spend DESC NULLS LAST
        """, [date_from, date_to, date_from, date_to] + ep)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _query_declarations_detail(conn, date_from, date_to, user_id, role):
    """All individual declaration rows for the period."""
    uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
    with conn.cursor() as cur:
        uf, ep = _build_user_filter(cur, user_id, role, [])
        cur.execute(f"""
            SELECT
                d.id, d.spend_date, d.page_id, d.user_id,
                COALESCE(d.page_name_manual, p.page_name, d.page_id) AS page_name,
                d.ad_account_name, d.phan_loai,
                d.tien_chua_thue, d.thue_rate, d.thanh_tien,
                d.note, d.created_at
            FROM fb_page_spend_declarations d
            LEFT JOIN fb_pages p ON p.page_id = d.page_id
            WHERE d.spend_date BETWEEN %s AND %s {uf}
            ORDER BY d.spend_date DESC, d.created_at DESC
        """, [date_from, date_to] + ep)
        cols = [c[0] for c in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            uid = str(row.get("user_id") or "")
            u   = uid_to_user.get(uid, {})
            row["full_name"] = u.get("full_name") or u.get("username") or uid
            row["username"]  = u.get("username") or uid
            rows.append(row)
        return rows


def _query_ad_account_spend(conn, date_from: str, date_to: str) -> list:
    """Chi phí theo từng TK QC (fb_ad_account_id) trong khoảng ngày.

    Nguồn dữ liệu UNION (đã dedup, không double-count):
      - page-level (`fb_ads_page_daily_spend`): chính, có breakdown per page.
      - account-level fallback (`fb_ads_daily_metrics`): khi page-level chưa sync xong
        (vd FB rate limit) — dedup MAX per (account, date) vì daily_metrics lưu
        trùng spend cho mỗi shop khi 1 account map nhiều shop.
    Chỉ lấy account-level cho (account, date) mà page-level KHÔNG có để tránh trùng.
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH page_lvl AS (
                SELECT
                    fb_ad_account_id, metric_date,
                    SUM(spend)       AS spend,
                    SUM(impressions) AS impressions,
                    SUM(clicks)      AS clicks
                FROM fb_ads_page_daily_spend
                WHERE metric_date BETWEEN %s AND %s
                GROUP BY fb_ad_account_id, metric_date
            ),
            acct_lvl AS (
                SELECT
                    dm.fb_ad_account_id, dm.metric_date,
                    MAX(dm.spend)       AS spend,
                    MAX(dm.impressions) AS impressions,
                    MAX(dm.clicks)      AS clicks
                FROM fb_ads_daily_metrics dm
                WHERE dm.metric_date BETWEEN %s AND %s
                GROUP BY dm.fb_ad_account_id, dm.metric_date
            ),
            combined AS (
                SELECT fb_ad_account_id, metric_date, spend, impressions, clicks FROM page_lvl
                UNION ALL
                SELECT a.fb_ad_account_id, a.metric_date, a.spend, a.impressions, a.clicks
                FROM acct_lvl a
                LEFT JOIN page_lvl p
                  ON p.fb_ad_account_id = a.fb_ad_account_id
                 AND p.metric_date = a.metric_date
                WHERE p.fb_ad_account_id IS NULL
            )
            ,
            -- Dedup mapping: 1 ad account có thể map N shop → string_agg để 1 row/account
            -- ⚠ FIX 2026-05-13: filter assigned_to IS NULL → chỉ mapping ACTIVE.
            -- Trước fix lấy cả mapping đã đóng (versioned history) → 77 rows = 53 TK
            -- thay vì 10 TK active → cộng dồn với "Chưa gán shop".
            mapping_agg AS (
                SELECT
                    m.fb_ad_account_id,
                    MIN(m.account_name)                         AS account_name,
                    string_agg(DISTINCT sh.shop_name, ', ' ORDER BY sh.shop_name) AS shop_name,
                    string_agg(DISTINCT sh.shop_key,  ', ' ORDER BY sh.shop_key)  AS shop_key
                FROM fb_ad_account_mappings m
                LEFT JOIN shops sh ON sh.id = m.shop_id
                WHERE m.assigned_to IS NULL
                GROUP BY m.fb_ad_account_id
            ),
            -- Time-aware: lấy NV đã phụ trách TK trong khoảng [date_from, date_to]
            -- (giao với window [assigned_from, COALESCE(assigned_to, +∞)]).
            -- Nếu giữa kỳ đổi NV → string_agg gộp cả 2 tên.
            user_agg AS (
                SELECT
                    a.ad_account_id,
                    string_agg(DISTINCT u.full_name, ', ' ORDER BY u.full_name) AS employee_name
                FROM user_ad_account_assignments a
                LEFT JOIN users u ON u.id = a.user_id
                WHERE a.assigned_from <= %s::date
                  AND COALESCE(a.assigned_to, DATE '9999-12-31') >= %s::date
                GROUP BY a.ad_account_id
            )
            -- ⚠ FIX 2026-05-13: filter CHỈ TK có mapping (Đã gán shop).
            -- Trước đây không filter → trộn cả TK chưa map → đếm 2 lần với
            -- _query_pa_account_spend (bucket "Chưa gán shop"). Tổng = 2× truth.
            SELECT
                c.fb_ad_account_id,
                COALESCE(ai.account_name, ma.account_name, c.fb_ad_account_id) AS account_name,
                ma.shop_name,
                ma.shop_key,
                ua.employee_name,
                SUM(c.spend)       AS total_spend,
                SUM(c.impressions) AS impressions,
                SUM(c.clicks)      AS clicks,
                MIN(c.metric_date) AS date_min,
                MAX(c.metric_date) AS date_max
            FROM combined c
            INNER JOIN mapping_agg ma       ON ma.fb_ad_account_id = c.fb_ad_account_id
            LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id    = c.fb_ad_account_id
            LEFT JOIN user_agg ua           ON ua.ad_account_id    = c.fb_ad_account_id
            GROUP BY c.fb_ad_account_id, ai.account_name, ma.account_name,
                     ma.shop_name, ma.shop_key, ua.employee_name
            ORDER BY total_spend DESC
        """, (date_from, date_to, date_from, date_to, date_to, date_from))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["total_spend"]  = float(r["total_spend"]  or 0)
            r["impressions"]  = int(r["impressions"]  or 0)
            r["clicks"]       = int(r["clicks"]       or 0)
        return rows


def _query_pa_account_spend(conn, date_from: str, date_to: str) -> list:
    """Chi phí TK CHƯA gán shop — cùng nguồn truth với `_query_ad_account_spend`.

    ⚠ FIX 2026-05-13: trước đây dùng `pa_account_insights` (sync khác từ pages.fm BM
    endpoint) → trùng với fb_ads_page_daily_spend → cộng dồn 2× (bug 154M vs truth 72M).
    Giờ dùng CHUNG nguồn page-level + acct fallback, chỉ filter TK chưa có mapping.
    Tổng "Đã gán" + "Chưa gán" = đúng total FB API trả.
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH page_lvl AS (
                SELECT fb_ad_account_id, metric_date, SUM(spend) AS spend
                  FROM fb_ads_page_daily_spend
                 WHERE metric_date BETWEEN %s AND %s
                 GROUP BY fb_ad_account_id, metric_date
            ),
            acct_lvl AS (
                SELECT fb_ad_account_id, metric_date, MAX(spend) AS spend
                  FROM fb_ads_daily_metrics
                 WHERE metric_date BETWEEN %s AND %s
                 GROUP BY fb_ad_account_id, metric_date
            ),
            combined AS (
                SELECT fb_ad_account_id, metric_date, spend FROM page_lvl
                UNION ALL
                SELECT a.fb_ad_account_id, a.metric_date, a.spend FROM acct_lvl a
                LEFT JOIN page_lvl p
                  ON p.fb_ad_account_id = a.fb_ad_account_id AND p.metric_date = a.metric_date
                WHERE p.fb_ad_account_id IS NULL
            )
            SELECT
                c.fb_ad_account_id AS account_id,
                COALESCE(NULLIF(ai.account_name,''), c.fb_ad_account_id) AS account_name,
                MAX(b.bm_name) AS bm_name,
                SUM(c.spend) AS total_spend,
                MIN(c.metric_date) AS date_min,
                MAX(c.metric_date) AS date_max
            FROM combined c
            LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = c.fb_ad_account_id
            LEFT JOIN pa_ad_accounts a       ON a.account_id    = 'act_' || c.fb_ad_account_id
            LEFT JOIN pa_business_managers b ON b.bm_id         = a.bm_id
            -- Filter: TK chưa có mapping active
            WHERE NOT EXISTS (
                SELECT 1 FROM fb_ad_account_mappings m
                 WHERE m.fb_ad_account_id = c.fb_ad_account_id
                   AND m.assigned_to IS NULL
            )
              AND c.spend > 0
            GROUP BY c.fb_ad_account_id, ai.account_name
            ORDER BY total_spend DESC
        """, (date_from, date_to, date_from, date_to))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["total_spend"] = float(r["total_spend"] or 0)
        return rows


def _query_unbound_fb_by_nv(date_from: str, date_to: str) -> list:
    """Spend của TK chưa map shop, chia theo NV (qua uaa active).

    Trả về list rows: [{user_id, username, full_name, spend}, ...]
    Row cuối có user_id=NULL = TK không có uaa active (admin chưa gán NV).
    Mỗi row spend = SUM combined (page-level + acct fallback) của TK NV này chưa map shop.

    FIX 2026-05-14: phục vụ tách bucket "Chưa gán shop" thành sub-rows theo NV để
    kế toán xem báo cáo thấy đủ NV chạy ads kể cả khi shop chưa map.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Build trên _FB_ALLOCATED_CTE → tổng unbound khớp tuyệt đối với
                # _query_fb_ads_by_shop_id._unbound (cùng nguồn).
                cur.execute(_FB_ALLOCATED_CTE + """
                    SELECT
                        s.user_id,
                        MAX(u.username)  AS username,
                        MAX(u.full_name) AS full_name,
                        SUM(s.spend)     AS spend,
                        -- Win/Test: WIN = page CÓ trên POS (pk NOT NULL) & không bấm Test; còn lại = Test.
                        -- Page chưa lên POS / acct-fallback (page NULL) → Test.
                        SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') <> 'ma_test')
                                 THEN s.spend ELSE 0 END) AS spend_test,
                        SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') <> 'ma_test'
                                 THEN s.spend ELSE 0 END) AS spend_win,
                        -- Da VAT theo TUNG page (3-tier: fb_page_vat_rate -> TK default -> 0.113),
                        -- KHOP _query_page_kpi_by_shop_id. Bo nhan 1.113 phang o step 5 (bug cu:
                        -- page VAT 6.1 bi tinh thanh 11.3 -> dong Chua co shop tong > modal chi tiet).
                        SUM(CASE WHEN NOT (pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') <> 'ma_test')
                                 THEN s.spend * (1.0 + COALESCE(pr.vat_rate, tr.default_vat_rate, 0.0)) ELSE 0 END) AS spend_test_co,
                        SUM(CASE WHEN pk.page_id IS NOT NULL AND COALESCE(pl.phan_loai,'ma_win') <> 'ma_test'
                                 THEN s.spend * (1.0 + COALESCE(pr.vat_rate, tr.default_vat_rate, 0.0)) ELSE 0 END) AS spend_win_co,
                        -- Tên TK QC NV đang chạy (dù chưa map shop) — dùng subquery để
                        -- KHÔNG nhân spend. Ưu tiên tên trong user_ad_account_map.
                        -- FILTER spend>0: KHÔNG liệt kê TK có row spend=0 ngày đó (FB sync tạo
                        -- dòng 0đ cho ngày trước khi TK được cấp → gây nhiễu, vd VinhTH26.5
                        -- cấp 26/5 vẫn hiện ở 09/5 dù 0đ). Tiền không đổi, chỉ sạch tên.
                        string_agg(DISTINCT COALESCE(
                            (SELECT m.ad_account_name FROM user_ad_account_map m
                              WHERE m.ad_account_id = s.fb_ad_account_id AND m.ad_account_name <> '' LIMIT 1),
                            (SELECT dm.account_name FROM fb_ads_daily_metrics dm
                              WHERE dm.fb_ad_account_id = s.fb_ad_account_id AND dm.account_name <> '' LIMIT 1),
                            s.fb_ad_account_id
                        ), ', ') FILTER (WHERE s.spend > 0) AS tk_names
                    FROM allocated s
                    LEFT JOIN users u ON u.id = s.user_id
                    LEFT JOIN fb_page_auto_phan_loai pl
                           ON pl.page_id = s.page_id AND pl.date = s.metric_date
                    LEFT JOIN (SELECT DISTINCT page_id FROM pos_page_daily_metrics) pk
                           ON pk.page_id = s.page_id
                    -- VAT per-page 3-tier (khớp _query_page_kpi_by_shop_id)
                    LEFT JOIN (
                        SELECT DISTINCT ON (page_id) page_id, vat_rate
                          FROM fb_page_vat_rate
                         WHERE date BETWEEN %s AND %s
                         ORDER BY page_id, date DESC
                    ) pr ON pr.page_id = s.page_id
                    LEFT JOIN fb_ad_account_vat_options tr
                           ON tr.fb_ad_account_id = s.fb_ad_account_id
                    -- Gom 2 trường hợp: (a) page chưa bind shop & TK không tk_single
                    -- (shop_id IS NULL), (b) shop_id trỏ vào shop đã inactive
                    -- hoặc đã xóa khỏi shops table (binding cũ còn sót lại).
                    -- Cả 2 đều không có row hiển thị trong rows_shop active →
                    -- spend bị mất nếu không gom về unbound. FIX 2026-06-08.
                    WHERE s.shop_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM shops sh
                            WHERE sh.id = s.shop_id AND sh.status = 'active'
                       )
                    GROUP BY s.user_id
                    ORDER BY SUM(s.spend) DESC NULLS LAST
                """, (date_from, date_to, date_from, date_to, date_from, date_to))
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                for r in rows:
                    r["spend"] = float(r["spend"] or 0)
                    r["spend_test"] = float(r.get("spend_test") or 0)
                    r["spend_win"]  = float(r.get("spend_win") or 0)
                    r["spend_win_co"]  = float(r.get("spend_win_co") or 0)
                    r["spend_test_co"] = float(r.get("spend_test_co") or 0)
                return rows
    except Exception as exc:
        logger.error("_query_unbound_fb_by_nv error: %s", exc)
        return []


def _query_unbound_pos_by_nv(date_from: str, date_to: str) -> dict:
    """POS ad cost của các page CHƯA phân bổ được shop active, chia theo NV (qua uaa).

    ĐỐI XỨNG với _query_unbound_fb_by_nv: bucket FB "Chưa gán shop" cộng spend FB,
    bucket này cộng POS (pos_page_daily_metrics.ads_amount) cho ĐÚNG các page đó.
    Trước đây dòng unbound ghi cứng cp_ads_pos=0.0 → card "CP Ads POS" bỏ sót POS của
    page mà TK QC chạy nhiều shop & chưa bind page → chênh "Khai báo vs POS" phồng giả.

    Phân hoạch theo (page, ngày): page CHỈ vào unbound nếu KHÔNG có allocated row nào
    trỏ vào shop active (bool_or(has_active)=FALSE) → KHÔNG đếm trùng với POS đã phân bổ
    trong _query_pos_ads_by_shop_id (đã verify overlap = 0, bound+unbound = POS toàn bộ).
    Mỗi page gán cho NV của TK chi nhiều nhất trên page đó (array_agg ORDER BY spend DESC).

    Return {user_id (int|None): pos_ads}. user_id=None = TK không có uaa active.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_FB_ALLOCATED_CTE + """
                    , alloc_pg AS (
                        SELECT page_id, metric_date, user_id, spend,
                               (shop_id IS NOT NULL AND EXISTS (
                                    SELECT 1 FROM shops sh
                                     WHERE sh.id = allocated.shop_id AND sh.status = 'active'
                               )) AS has_active
                          FROM allocated
                         WHERE page_id IS NOT NULL
                    ),
                    unbound_pages AS (
                        SELECT page_id, metric_date,
                               (array_agg(user_id ORDER BY spend DESC NULLS LAST))[1] AS user_id
                          FROM alloc_pg
                         GROUP BY page_id, metric_date
                        HAVING bool_or(has_active) = FALSE
                    )
                    SELECT up.user_id, COALESCE(SUM(pp.ads_amount), 0) AS pos_ads
                      FROM unbound_pages up
                      JOIN pos_page_daily_metrics pp
                             ON pp.page_id = up.page_id
                            AND pp.metric_date = up.metric_date
                     GROUP BY up.user_id
                """, (date_from, date_to, date_from, date_to))
                return {r[0]: float(r[1] or 0) for r in cur.fetchall()}
    except Exception as exc:
        logger.error("_query_unbound_pos_by_nv error: %s", exc)
        return {}


def _build_rows_shop(date_from: str, date_to: str, user_id, role: str,
                     team_filter: str = "", user_filter: str = "") -> list:
    """Build rows_shop SHOP-CENTRIC: mỗi row = 1 shop. NV phụ trách = property hiển thị.
    Canonical key = shops.id. POS + FB Ads + Win/Test KPI gắn theo shop_id.
    Declarations (NV tự khai) attribute về shop primary của NV (single-shop hoặc first shop).
    """
    from db import get_conn

    # 1) Lấy danh sách shop active (canonical: shop_id)
    shops_list = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, shop_key, shop_name, COALESCE(pancake_shop_id,''),
                           COALESCE(team_id::text, '')
                    FROM shops
                    WHERE status='active'
                    ORDER BY shop_name
                """)
                for r in cur.fetchall():
                    shops_list.append({
                        "shop_id":         int(r[0]),
                        "shop_key":        str(r[1] or ""),
                        "shop_name":       str(r[2] or ""),
                        "pancake_shop_id": str(r[3] or ""),
                        "team_id":         str(r[4] or ""),
                    })
    except Exception as exc:
        logger.error("_build_rows_shop shops query error: %s", exc)
        return []

    # 2) Maps shop-level (key = shop_id)
    pos_map   = _query_pos_ads_by_shop_id(date_from, date_to)
    fb_map    = _query_fb_ads_by_shop_id(date_from, date_to)
    kpi_map   = _query_page_kpi_by_shop_id(date_from, date_to)

    # 3) Declarations (NV tự khai) — user-level → attribute về shop primary
    rows_employee = []
    try:
        with get_conn() as conn:
            rows_employee = _query_employee_summary(conn, date_from, date_to, user_id, role)
    except Exception as exc:
        logger.error("_build_rows_shop employee_summary error: %s", exc)

    # Map shop_key → shop_id để chuyển user.assigned_shops sang shop_id
    sk_to_sid = {s["shop_key"]: s["shop_id"] for s in shops_list if s["shop_key"]}
    # Map username → user info
    users_list = _load_users_json()
    username_to_user = {u.get("username", ""): u for u in users_list}

    # NV phụ trách shop nào? Reverse map shop_id → user (first match)
    shop_to_user = {}    # shop_id → user dict
    user_to_primary_shop = {}  # username → shop_id (primary)
    for u in users_list:
        if str(u.get("status", "active")).strip() != "active":
            continue
        role_u = str(u.get("role", "")).strip()
        if role_u in ("admin", "superadmin", "ketoan", "accountant", "manager", "it"):
            continue  # vai trò full-view không gán shop cụ thể
        sk_list = _normalize_shops(u.get("assigned_shops", []))
        sid_list = [sk_to_sid[k] for k in sk_list if k in sk_to_sid]
        if not sid_list:
            continue
        # NV phụ trách: gắn vào TẤT CẢ shop của NV (chỉ ghi nếu shop chưa có NV)
        for sid in sid_list:
            if sid not in shop_to_user:
                shop_to_user[sid] = u
        # Primary shop của NV để attribute declarations (single → đó, multi → first)
        user_to_primary_shop[u.get("username", "")] = sid_list[0]

    # Attribute declarations về primary shop
    decl_by_sid = {}  # shop_id → {test_chua_vat, test_co_vat,...}
    for emp in rows_employee:
        uname = emp.get("username", "")
        sid = user_to_primary_shop.get(uname)
        if not sid:
            continue
        agg = decl_by_sid.setdefault(sid, {"test_chua_vat":0.0, "test_co_vat":0.0,
                                           "win_chua_vat_decl":0.0, "win_co_vat_decl":0.0})
        agg["test_chua_vat"] += float(emp.get("test_chua_vat") or 0)
        agg["test_co_vat"]   += float(emp.get("test_co_vat")   or 0)

    # 4) Build rows — 1 row per shop, attach NV nếu có
    rows_shop = []
    for s in shops_list:
        sid = s["shop_id"]
        nv = shop_to_user.get(sid, {})
        kpi = kpi_map.get(sid, {})
        decl = decl_by_sid.get(sid, {})

        cp_pos        = float(pos_map.get(sid, 0.0))
        cp_fb         = float(fb_map.get(sid, 0.0))
        win_chua      = float(kpi.get("win_chua",  0.0))
        win_co        = float(kpi.get("win_co",    0.0))
        auto_t_chua   = float(kpi.get("test_chua", 0.0))
        auto_t_co     = float(kpi.get("test_co",   0.0))
        test_tc_chua  = float(decl.get("test_chua_vat") or 0)
        test_tc_co    = float(decl.get("test_co_vat")   or 0)
        tong          = win_co + auto_t_co + test_tc_co

        # Bỏ shop hoàn toàn không có data trong kỳ để không phình UI
        if cp_pos == 0 and cp_fb == 0 and win_chua == 0 and auto_t_chua == 0 and test_tc_chua == 0 and tong == 0:
            continue

        rows_shop.append({
            # Canonical
            "shop_id":         sid,
            "shop_key":        s["shop_key"],
            "shop_name":       s["shop_name"],
            "pos_shop_id":     s["pancake_shop_id"],
            "team_id":         s["team_id"],
            # NV phụ trách (có thể rỗng nếu shop chưa gán NV)
            "username":        nv.get("username", "") or "",
            "full_name":       nv.get("full_name") or nv.get("username") or "",
            "nv_team_id":      nv.get("team_id") or "",
            # Tài chính
            "cp_ads_pos":      cp_pos,
            "cp_ads_fb_shop":  cp_fb,
            "win_chua_vat":    win_chua,
            "win_co_vat":      win_co,
            "auto_test_chua":  auto_t_chua,
            "auto_test_co":    auto_t_co,
            "test_chua_vat":   test_tc_chua,
            "test_co_vat":     test_tc_co,
            "tong":            tong,
        })

    # 5) Bucket "⚠ Chưa gán shop" — tách per NV qua uaa (FIX 2026-05-14)
    #    Cũ: 1 row tổng → ẩn 28 NV. Mới: 1 row/NV để kế toán biết ai chạy bao nhiêu
    #    dù shop chưa gán. + 1 row cuối "Không NV" cho TK không có uaa.
    unbound_by_nv = _query_unbound_fb_by_nv(date_from, date_to)
    # POS của page chưa gán shop, gom theo cùng NV (đối xứng FB) — KHÔNG để rớt 0.
    unbound_pos_by_nv = _query_unbound_pos_by_nv(date_from, date_to)
    unbound_total_check = sum(r["spend"] for r in unbound_by_nv)
    if unbound_by_nv:
        for r in unbound_by_nv:
            sp = float(r["spend"] or 0)
            pos_unb = float(unbound_pos_by_nv.get(r.get("user_id"), 0.0))
            # Giữ dòng nếu có FB spend HOẶC có POS chưa gán (tránh nuốt POS khi spend≈0)
            if sp <= 0 and pos_unb <= 0:
                continue
            # Map username → user info từ users.json để lấy team_id và full_name fallback
            _uname = r.get("username") or ""
            _u = username_to_user.get(_uname, {})
            _team_id_uaa = str(_u.get("team_id", "")) if _u else ""
            full_name = r.get("full_name") or _uname or "(không NV)"
            _tk_names = (r.get("tk_names") or "").strip()
            # Hiện tên TK QC NV đang chạy ngay trên dòng (NV chưa có shop POS).
            _shop_label = "⚠ Chưa có shop"
            if _tk_names:
                _shop_label += f" · TK: {_tk_names}"
            # Tách Win/Test theo fb_page_auto_phan_loai (đã split từ query unbound)
            sp_win  = float(r.get("spend_win") or 0)
            sp_test = float(r.get("spend_test") or 0)
            # VAT đã tính per-page trong _query_unbound_fb_by_nv (KHÔNG nhân 1.113 phẳng nữa —
            # page 6.1% phải tính 6.1%, không thì dòng "Chưa có shop" lệch với modal chi tiết)
            win_co_unb  = float(r.get("spend_win_co") or 0)
            test_co_unb = float(r.get("spend_test_co") or 0)
            rows_shop.append({
                "shop_id":         0,
                "shop_key":        "_unbound",
                "shop_name":       _shop_label,
                "pos_shop_id":     "",
                "team_id":         "",
                "user_id":         r.get("user_id"),
                "username":        _uname,
                "full_name":       full_name,
                "nv_team_id":      _team_id_uaa,
                "cp_ads_pos":      pos_unb,
                "cp_ads_fb_shop":  sp,
                "win_chua_vat":    sp_win,
                "win_co_vat":      win_co_unb,
                "auto_test_chua":  sp_test,
                "auto_test_co":    test_co_unb,
                "test_chua_vat":   0.0,
                "test_co_vat":     0.0,
                "tong":            win_co_unb + test_co_unb,
                "is_unbound":      True,
            })

    # 6) Filters
    if team_filter:
        # filter theo shops.team_id (chính), fallback NV.team_id nếu shop chưa có team
        rows_shop = [r for r in rows_shop
                     if r["team_id"] == team_filter or r["nv_team_id"] == team_filter]
    if user_filter:
        rows_shop = [r for r in rows_shop if r["username"] == user_filter]

    return rows_shop


# ── Routes ────────────────────────────────────────────────────────────
@chi_phi_qc_bp.route("/bao-cao")
@login_required
def bao_cao():
    from db import get_conn
    _ensure_snapshots_table()
    today        = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()

    user_id   = session.get("user_id")
    role      = session.get("role", "staff")

    # Only ketoan / admin / manager / superadmin may access this report
    if not _is_full_view(role):
        abort(403)

    date_from    = request.args.get("date_from",    default_from).strip()
    date_to      = request.args.get("date_to",      default_to).strip()
    active_tab   = request.args.get("tab",          "nhanvien")
    team_filter  = request.args.get("team_filter",  "").strip()
    user_filter  = request.args.get("user_filter",  "").strip()

    rows_employee   = []
    rows_page       = []
    rows_detail     = []
    rows_ad_account = []
    rows_pa_account = []
    snapshots       = []
    try:
        with get_conn() as conn:
            rows_employee   = _query_employee_summary(conn, date_from, date_to, user_id, role)
            rows_page       = _query_page_breakdown(conn, date_from, date_to, user_id, role)
            rows_detail     = _query_declarations_detail(conn, date_from, date_to, user_id, role)
            rows_ad_account = _query_ad_account_spend(conn, date_from, date_to)
            rows_pa_account = _query_pa_account_spend(conn, date_from, date_to)
            if _is_senior(role):
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, period_label, date_from, date_to, note,
                               locked_at, locked_by_name, total_rows, total_amount
                        FROM chi_phi_qc_period_snapshots
                        ORDER BY locked_at DESC LIMIT 50
                    """)
                    cols = [c[0] for c in cur.description]
                    snapshots = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("bao_cao error: %s", exc)
        flash(f"Lỗi truy vấn: {exc}", "danger")

    # Build merged rows_shop using shared helper (includes team/user filters)
    rows_shop = _build_rows_shop(date_from, date_to, user_id, role,
                                 team_filter=team_filter, user_filter=user_filter)

    # Enrich rows_employee for the employee tab (legacy enrichment, kept for other tabs)
    pos_map     = {r["username"]: r["cp_ads_pos"]     for r in rows_shop}
    fb_shop_map = {r["username"]: r["cp_ads_fb_shop"] for r in rows_shop}
    for r in rows_employee:
        r["cp_ads_pos"]     = pos_map.get(r["username"], 0.0)
        r["cp_ads_fb_shop"] = fb_shop_map.get(r["username"], 0.0)

    # Build team list and user list for filter dropdowns
    all_users_json  = _load_users_json()
    team_map = {}   # team_id → display name (derived from leader username)
    user_dropdown = []
    for u_obj in sorted(all_users_json, key=lambda x: x.get("username", "")):
        tid = u_obj.get("team_id") or ""
        if tid and tid not in team_map:
            # Derive team display name from team_id (e.g. "team-nam" → "Team Nam")
            team_map[tid] = tid.replace("team-", "Team ").title()
        user_dropdown.append({
            "username":  u_obj.get("username", ""),
            "full_name": u_obj.get("full_name") or u_obj.get("username", ""),
            "team_id":   tid,
        })
    team_list = [{"id": tid, "name": name} for tid, name in sorted(team_map.items())]

    # KPI card totals — computed from rows_shop so they match the table footer exactly
    # pg_fb_total uses rows_shop (team-filtered) so the FB ADS card respects team filter
    pg_fb_total      = sum(float(r.get("cp_ads_fb_shop", 0) or 0) for r in rows_shop)
    pg_win_chua      = sum(float(r.get("win_chua_vat",   0) or 0) for r in rows_shop)
    pg_win_co        = sum(float(r.get("win_co_vat",     0) or 0) for r in rows_shop)
    pg_auto_test_chua= sum(float(r.get("auto_test_chua", 0) or 0) for r in rows_shop)
    pg_auto_test_co  = sum(float(r.get("auto_test_co",   0) or 0) for r in rows_shop)
    pg_test_chua     = sum(float(r.get("test_chua_vat",  0) or 0) for r in rows_shop)
    pg_test_co       = pg_auto_test_co + sum(float(r.get("test_co_vat", 0) or 0) for r in rows_shop)
    pg_khai_co       = sum(float(r.get("tong",           0) or 0) for r in rows_shop)
    # chưa VAT total = sum of all chưa-VAT columns for VAT card
    pg_khai_chua     = pg_win_chua + pg_auto_test_chua + pg_test_chua

    # Tổng POS = cộng thẳng qua TỪNG shop (không dồn qua username — 1 NV nhiều shop
    # sẽ bị gộp mất shop). Khớp với cách tính pg_khai_co + tổng cột ở footer.
    pos_total = sum(float(r.get("cp_ads_pos", 0) or 0) for r in rows_shop)
    # Chênh lệch FB auto vs POS
    chenh_lech_fb_pos = pg_fb_total - pos_total

    # Invariant — banner đỏ nếu drift để kế toán KHÔNG bao giờ chốt nhầm
    invariant = _verify_fb_bucket_invariant(date_from, date_to)

    return render_template("chi_phi_qc/bao_cao.html",
                           invariant=invariant,
                           rows_employee=rows_employee,
                           rows_shop=rows_shop,
                           rows_page=rows_page,
                           rows_detail=rows_detail,
                           rows_ad_account=rows_ad_account,
                           rows_pa_account=rows_pa_account,
                           snapshots=snapshots,
                           date_from=date_from,
                           date_to=date_to,
                           active_tab=active_tab,
                           current_role=role,
                           pg_win_chua=pg_win_chua,
                           pg_win_co=pg_win_co,
                           pg_test_chua=pg_test_chua,
                           pg_test_co=pg_test_co,
                           pg_khai_co=pg_khai_co,
                           pg_khai_chua=pg_khai_chua,
                           pg_fb_total=pg_fb_total,
                           pos_total=pos_total,
                           chenh_lech_fb_pos=chenh_lech_fb_pos,
                           team_list=team_list,
                           user_dropdown=user_dropdown,
                           team_filter=team_filter,
                           user_filter=user_filter)


@chi_phi_qc_bp.route("/bao-cao/shop-daily")
@login_required
def bao_cao_shop_daily():
    """JSON chi tiết TỪNG NGÀY của 1 shop (cho modal khi bấm shop ở báo cáo).

    Trả về: ngày · CP Ads FB (chưa VAT + đã VAT) · CP Ads POS · Doanh thu ·
    Lợi nhuận POS · số đơn. FB lấy từ _FB_ALLOCATED_CTE (khớp số báo cáo);
    doanh thu/lợi nhuận/CP ads POS từ daily_shop_metrics.
    """
    import datetime as _dt
    try:
        shop_id = int(request.args.get("shop_id") or 0)
    except (TypeError, ValueError):
        shop_id = 0
    try:
        user_id_param = int(request.args.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id_param = 0
    date_from = (request.args.get("from") or "").strip()
    date_to   = (request.args.get("to") or "").strip()
    if (not shop_id and not user_id_param) or not date_from or not date_to:
        return jsonify({"error": "thiếu shop_id/user_id/from/to"}), 400
    try:
        _dt.datetime.strptime(date_from, "%Y-%m-%d")
        _dt.datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "ngày sai định dạng"}), 400

    VAT = float(ADS_COST_MULTIPLIER) if 'ADS_COST_MULTIPLIER' in globals() else 1.113
    by_date: dict = {}        # date -> {fb_raw, pos_ads, revenue, profit, orders}
    page_by_date: dict = {}   # date -> {page_id: fb_raw}
    page_names: dict = {}     # page_id -> name
    phan_loai: dict = {}      # (date, page_id) -> 'ma_win'|'ma_test' (THEO NGÀY)
    page_vat_rate: dict = {}  # page_id -> vat_rate tick tay per page (tầng 1)
    acct_vat_rate: dict = {}  # page_id -> default_vat_rate khai theo TK (tầng 2); thiếu cả 2 → 11.3%
    pos_pd: dict = {}         # (date, page_id) -> {ads,revenue,profit,chot,hoan,name} — POS theo page
    pos_pd_shops: dict = {}   # (date, page_id) -> [{shop,ads,revenue,profit,chot,hoan}] — tách theo shop
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1) FB ads theo (NGÀY, PAGE) — nếu shop_id: filter theo shop; nếu user_id (unbound):
                #    filter spend UNBOUND của NV đó (shop_id NULL + user_id = X). Mục đích: cho phép
                #    bấm "chi tiết ngày" ở dòng "Chưa có shop · TK: ..." dù NV shop inactive.
                if shop_id:
                    cur.execute(_FB_ALLOCATED_CTE + """
                        SELECT metric_date, page_id, SUM(spend) AS fb_spend
                        FROM allocated
                        WHERE shop_id = %s
                        GROUP BY metric_date, page_id
                    """, (date_from, date_to, date_from, date_to, shop_id))
                else:
                    cur.execute(_FB_ALLOCATED_CTE + """
                        SELECT metric_date, page_id, SUM(spend) AS fb_spend
                        FROM allocated
                        WHERE shop_id IS NULL AND user_id = %s
                        GROUP BY metric_date, page_id
                    """, (date_from, date_to, date_from, date_to, user_id_param))
                pids: set = set()
                for d, pid, sp in cur.fetchall():
                    dk = str(d); spv = float(sp or 0)
                    by_date.setdefault(dk, {})
                    by_date[dk]["fb_raw"] = by_date[dk].get("fb_raw", 0.0) + spv
                    if pid:
                        pid = str(pid)
                        pm = page_by_date.setdefault(dk, {})
                        pm[pid] = pm.get(pid, 0.0) + spv
                        pids.add(pid)
                # 2) Tên page + Win/Test (mới nhất trong khoảng) — giống tab team
                if pids:
                    cur.execute("SELECT page_id, page_name FROM fb_pages WHERE page_id = ANY(%s)", (list(pids),))
                    for pid, nm in cur.fetchall():
                        if nm:
                            page_names[str(pid)] = nm
                    # Fallback tên page (đồng nhất với Page Test): fb_ads_page_daily_spend
                    # (đã suy từ campaign) rồi pa_pages — để page ngoài BM cũng có tên.
                    _miss = [pid for pid in pids if str(pid) not in page_names]
                    if _miss:
                        cur.execute("""SELECT page_id, MAX(COALESCE(NULLIF(page_name,''),''))
                                         FROM fb_ads_page_daily_spend WHERE page_id = ANY(%s)
                                        GROUP BY page_id""", (_miss,))
                        for pid, nm in cur.fetchall():
                            if nm:
                                page_names[str(pid)] = nm
                        _miss2 = [pid for pid in _miss if str(pid) not in page_names]
                        if _miss2:
                            cur.execute("SELECT page_id, page_name FROM pa_pages "
                                        "WHERE page_id = ANY(%s) AND COALESCE(page_name,'')<>''", (_miss2,))
                            for pid, nm in cur.fetchall():
                                page_names[str(pid)] = nm
                    try:
                        # Win/Test THEO TỪNG NGÀY (không gộp) → modal hiện đúng mark mỗi ngày
                        cur.execute("""SELECT date, page_id, phan_loai
                                         FROM fb_page_auto_phan_loai
                                        WHERE date BETWEEN %s AND %s AND page_id = ANY(%s)""",
                                    (date_from, date_to, list(pids)))
                        for _d, pid, pl in cur.fetchall():
                            phan_loai[(str(_d), str(pid))] = pl
                    except Exception:
                        pass
                    # Mức VAT mỗi page (6.1%/11.3%) NV chọn ở tab team — dùng chung nguồn
                    try:
                        cur.execute("""SELECT DISTINCT ON (page_id) page_id, vat_rate
                                         FROM fb_page_vat_rate
                                        WHERE date BETWEEN %s AND %s AND page_id = ANY(%s)
                                        ORDER BY page_id, date DESC""",
                                    (date_from, date_to, list(pids)))
                        for pid, vr in cur.fetchall():
                            page_vat_rate[str(pid)] = float(vr)
                    except Exception:
                        pass
                    # Tầng 2: mức VAT KHAI THEO TK (fb_ad_account_vat_options) — đồng bộ
                    # quy tắc 3 tầng với bảng tổng báo cáo (xem comment _FB_ALLOCATED_CTE).
                    # Page chạy nhiều TK trong kỳ → lấy mức của TK spend lớn nhất.
                    try:
                        cur.execute("""
                            SELECT page_id, default_vat_rate FROM (
                                SELECT s.page_id, v.default_vat_rate, SUM(s.spend) AS sp,
                                       ROW_NUMBER() OVER (PARTITION BY s.page_id ORDER BY SUM(s.spend) DESC) AS rn
                                  FROM fb_ads_page_daily_spend s
                                  JOIN fb_ad_account_vat_options v ON v.fb_ad_account_id = s.fb_ad_account_id
                                 WHERE s.page_id = ANY(%s) AND s.metric_date BETWEEN %s AND %s
                                 GROUP BY s.page_id, v.default_vat_rate
                            ) x WHERE rn = 1
                        """, (list(pids), date_from, date_to))
                        for pid, vr in cur.fetchall():
                            acct_vat_rate[str(pid)] = float(vr)
                    except Exception:
                        pass
                # 3) Doanh thu / lợi nhuận / CP ads POS / đơn từ daily_shop_metrics
                #    Chỉ áp dụng khi có shop_id (mode user_id = unbound NV, không có shop → bỏ qua).
                if shop_id:
                    cur.execute("""
                        SELECT metric_date, COALESCE(net_revenue,0), COALESCE(pos_profit_loss,0),
                               COALESCE(ads_cost,0), COALESCE(order_count,0)
                        FROM daily_shop_metrics
                        WHERE shop_id = %s AND metric_date BETWEEN %s AND %s
                    """, (shop_id, date_from, date_to))
                    for d, rev, profit, posads, oc in cur.fetchall():
                        e = by_date.setdefault(str(d), {})
                        e["revenue"] = float(rev or 0)
                        e["profit"]  = float(profit or 0)
                        e["pos_ads"] = float(posads or 0)
                        e["orders"]  = int(oc or 0)
                # 4) POS theo TỪNG PAGE — TỔNG POS của page (mọi pos_shop). FB đã dồn về shop
                # có POS (posh override) nên POS theo page khớp với shop đang xem; KHÔNG lọc
                # theo shop_id ở đây để khớp _query_pos_ads_by_shop_id (bảng) — tránh lệch.
                if pids:
                    cur.execute("""
                        SELECT page_id, metric_date,
                               COALESCE(SUM(ads_amount),0), COALESCE(SUM(revenue),0),
                               COALESCE(SUM(profit),0), COALESCE(SUM(success_order_count),0),
                               COALESCE(SUM(returned_order_count),0), MAX(page_name),
                               COALESCE(SUM(sales),0)
                          FROM pos_page_daily_metrics
                         WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                         GROUP BY page_id, metric_date
                    """, (list(pids), date_from, date_to))
                    for pid, d, ads, rev, pf, sc, rc, nm, sal in cur.fetchall():
                        pos_pd[(str(d), str(pid))] = {
                            "ads": float(ads), "revenue": float(rev), "profit": float(pf),
                            "chot": int(sc), "hoan": int(rc), "name": (nm or "").strip(),
                            "sales": float(sal),
                        }
                    # Breakdown theo shop Pancake (page bán nhiều shop → dòng phụ từng shop)
                    cur.execute("""
                        SELECT m.page_id, m.metric_date,
                               COALESCE(NULLIF(s.shop_name,''), m.pos_shop_id) AS shop_name,
                               COALESCE(m.ads_amount,0), COALESCE(m.revenue,0), COALESCE(m.profit,0),
                               COALESCE(m.success_order_count,0), COALESCE(m.returned_order_count,0)
                          FROM pos_page_daily_metrics m
                          LEFT JOIN shops s ON s.id = m.shop_id
                         WHERE m.page_id = ANY(%s) AND m.metric_date BETWEEN %s AND %s
                         ORDER BY m.ads_amount DESC
                    """, (list(pids), date_from, date_to))
                    for pid, d, sname, ads, rev, pf, sc, rc in cur.fetchall():
                        pos_pd_shops.setdefault((str(d), str(pid)), []).append({
                            "shop": (sname or "").strip(), "ads": float(ads), "revenue": float(rev),
                            "profit": float(pf), "chot": int(sc), "hoan": int(rc),
                        })
    except Exception as exc:
        logger.error("bao_cao_shop_daily error: %s", exc)
        return jsonify({"error": "lỗi truy vấn"}), 500

    # Build list theo từng ngày (page gộp trong ngày). FB hiển thị CHƯA VAT (Meta gốc).
    # Page TỪNG lên POS (bất kỳ ngày nào) — phân loại Win/Test khớp bảng ngoài.
    pos_known = _query_pos_known_pages(list(pids))
    d0 = _dt.datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = _dt.datetime.strptime(date_to, "%Y-%m-%d").date()
    days = []
    # cp_kt = chi phí theo kế toán: page WIN tính ĐÃ VAT (×1.113), page TEST tính GỐC (chưa VAT)
    win_vat = test_vat = test_raw = 0.0
    win113 = win61 = test113 = test61 = 0.0   # tách theo mức VAT
    cur_d = d0
    while cur_d <= d1:
        k = cur_d.isoformat()
        e = by_date.get(k, {})
        fb_raw = float(e.get("fb_raw", 0) or 0)
        pg_map = page_by_date.get(k, {})
        pages = []
        day_cp_kt = 0.0
        # tổng POS theo page trong ngày (đối chiếu CP QC POS)
        day_pos_ads = day_pos_rev = day_pos_profit = day_pos_sales = 0.0
        day_pos_chot = day_pos_hoan = 0
        for pid, praw in sorted(pg_map.items(), key=lambda x: -x[1]):
            # Win/Test theo NGÀY (chốt 2026-08-04 — boss Phong):
            #   • NV đánh tay → theo mark
            #   • auto: WIN chỉ khi page CÓ trên POS **VÀ có doanh thu ngày đó**;
            #     chưa lên POS, hoặc lên POS mà doanh thu = 0 → TEST
            _pl_explicit = phan_loai.get((k, pid))   # mark NV cho (page, ngày)
            _rev = float((pos_pd.get((k, pid)) or {}).get("revenue") or 0)
            if _pl_explicit == "ma_test":
                pl = "ma_test"
            elif _pl_explicit == "ma_win":
                pl = "ma_win"
            elif pos_known.get(pid) and _rev > 0:
                pl = "ma_win"
            else:
                pl = "ma_test"
            # VAT 3 tầng: tick per-page → mức khai theo TK → mặc định 11.3%
            rate = page_vat_rate.get(pid, acct_vat_rate.get(pid, _DEFAULT_VAT_RATE))
            pvat = praw * (1.0 + rate)
            cp_kt = pvat   # đã VAT theo mức của page (6.1% hoặc 11.3%)
            day_cp_kt += cp_kt
            _is61 = abs(rate - 0.061) < 1e-6
            if pl == "ma_test":
                test_vat += pvat
                test_raw += praw
                if _is61: test61 += pvat
                else:     test113 += pvat
            else:
                win_vat += pvat
                if _is61: win61 += pvat
                else:     win113 += pvat
            # POS theo page (ngày này)
            _po = pos_pd.get((k, pid))
            if _po:
                day_pos_ads    += _po["ads"]
                day_pos_rev    += _po["revenue"]
                day_pos_profit += _po["profit"]
                day_pos_chot   += _po["chot"]
                day_pos_hoan   += _po["hoan"]
                day_pos_sales  += _po.get("sales", 0)
            pages.append({
                "page_id":     pid,
                "name":        page_names.get(pid) or (pos_pd.get((k, pid), {}) or {}).get("name") or ("Page " + pid[-8:]),
                "phan_loai":   pl,
                "vat_pct":     round(rate * 100, 1),
                "fb_raw":      round(praw, 0),
                "fb_vat":      round(pvat, 0),
                "cp_kt":       round(cp_kt, 0),
                # POS theo page (None nếu page chưa có trên POS)
                "pos_name":    (_po["name"] if _po else None),
                "pos_ads":     (round(_po["ads"], 0) if _po else None),
                "pos_sales":   (round(_po.get("sales", 0), 0) if _po else None),
                "pos_revenue": (round(_po["revenue"], 0) if _po else None),
                "pos_profit":  (round(_po["profit"], 0) if _po else None),
                "pos_chot":    (_po["chot"] if _po else None),
                "pos_hoan":    (_po["hoan"] if _po else None),
                "pos_cp_per_order": (round(_po["ads"] / _po["chot"], 0) if (_po and _po["chot"] > 0) else None),
                "diff_vat":    (round(pvat - _po["ads"], 0) if _po else None),  # đã VAT − POS
                # Tách POS theo shop (>1 shop → JS hiện dòng phụ từng shop để kế toán đối chiếu)
                "pos_shops": ([
                    {"shop": _sh["shop"], "pos_ads": round(_sh["ads"], 0),
                     "pos_revenue": round(_sh["revenue"], 0), "pos_profit": round(_sh["profit"], 0),
                     "pos_chot": _sh["chot"], "pos_hoan": _sh["hoan"],
                     "pos_cp_per_order": (round(_sh["ads"] / _sh["chot"], 0) if _sh["chot"] > 0 else None)}
                    for _sh in pos_pd_shops.get((k, pid), [])
                ] if _po else []),
            })
        days.append({
            "date":     k,
            "fb_raw":   round(fb_raw, 0),
            # Có breakdown page → tổng ngày = Σ pvat theo mức từng page (khớp các dòng con);
            # không có page (fallback acct-level) → nhân VAT mặc định như cũ.
            "fb_vat":   round(day_cp_kt if pg_map else fb_raw * VAT, 0),
            "cp_kt":    round(day_cp_kt, 0),
            "pos_ads":  round(float(e.get("pos_ads", 0) or 0), 0),
            "revenue":  round(float(e.get("revenue", 0) or 0), 0),
            "profit":   round(float(e.get("profit", 0) or 0), 0),
            "orders":   int(e.get("orders", 0) or 0),
            # tổng POS theo page (per-page) cho ngày
            "pos_page_ads":    round(day_pos_ads, 0),
            "pos_page_sales":  round(day_pos_sales, 0),
            "pos_page_rev":    round(day_pos_rev, 0),
            "pos_page_profit": round(day_pos_profit, 0),
            "pos_page_chot":   day_pos_chot,
            "pos_page_hoan":   day_pos_hoan,
            "pages":    pages,
        })
        cur_d += _dt.timedelta(days=1)
    totals = {
        "fb_raw":  sum(x["fb_raw"]  for x in days),
        "fb_vat":  sum(x["fb_vat"]  for x in days),
        "cp_kt":   sum(x["cp_kt"]   for x in days),
        "pos_ads": sum(x["pos_ads"] for x in days),
        "revenue": sum(x["revenue"] for x in days),
        "profit":  sum(x["profit"]  for x in days),
        "orders":  sum(x["orders"]  for x in days),
        "pos_page_ads":    sum(x["pos_page_ads"]    for x in days),
        "pos_page_sales":  sum(x["pos_page_sales"]  for x in days),
        "pos_page_rev":    sum(x["pos_page_rev"]    for x in days),
        "pos_page_profit": sum(x["pos_page_profit"] for x in days),
        "pos_page_chot":   sum(x["pos_page_chot"]   for x in days),
        "pos_page_hoan":   sum(x["pos_page_hoan"]   for x in days),
    }
    return jsonify({
        "days": days, "totals": totals,
        "win_vat": round(win_vat, 0), "test_vat": round(test_vat, 0),
        "test_raw": round(test_raw, 0),
        "win113": round(win113, 0), "win61": round(win61, 0),
        "test113": round(test113, 0), "test61": round(test61, 0),
        "from": date_from, "to": date_to,
    })


@chi_phi_qc_bp.route("/page-daily-detail", methods=["GET", "POST"])
@login_required
def page_daily_detail():
    """JSON chi tiết TỪNG NGÀY theo PAGE — cho modal đối chiếu POS vs FB (tab Tất cả page).

    Nhận: from, to, page_ids (phẩy ngăn cách — chính các page đang hiện trên bảng;
    truyền 1 page = chế độ chi tiết riêng page đó).
    Mỗi ngày → list page chạy ngày đó kèm: CP Ads FB (chưa VAT), CP đã VAT (theo mức
    VAT của page), CP QC POS, CP/đơn POS, doanh thu, lợi nhuận, đơn chốt, đơn hoàn,
    và mức lệch (đã VAT − POS). Có tổng mỗi ngày + tổng chung.
    """
    import datetime as _dt
    # POST (JSON body) cho danh sách page_ids LỚN (tránh URL quá dài → 414);
    # GET (query) cho trường hợp 1 page.
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        date_from = str(body.get("from") or "").strip()
        date_to   = str(body.get("to") or "").strip()
        _ids = body.get("page_ids") or []
        if isinstance(_ids, str):
            _ids = _ids.split(",")
        page_ids = [str(p).strip() for p in _ids if str(p).strip()]
        user_id_filter = body.get("user_id")
    else:
        date_from = (request.args.get("from") or "").strip()
        date_to   = (request.args.get("to") or "").strip()
        raw_ids   = (request.args.get("page_ids") or "").strip()
        page_ids  = [p.strip() for p in raw_ids.split(",") if p.strip()]
        user_id_filter = request.args.get("user_id")
    if not date_from or not date_to or not page_ids:
        return jsonify({"error": "thiếu from/to/page_ids"}), 400
    try:
        _dt.datetime.strptime(date_from, "%Y-%m-%d")
        _dt.datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "ngày sai định dạng"}), 400

    fb_pd: dict = {}      # (date, page_id) -> fb_raw
    meta_pd: dict = {}    # (date, page_id) -> lượt mua Meta (số đơn cho page TEST)
    pos_pd: dict = {}     # (date, page_id) -> {ads, revenue, profit, chot, hoan}
    pos_pd_shops: dict = {}  # (date, page_id) -> [{shop, ads, revenue, profit, chot, hoan}] (tách theo shop)
    page_names: dict = {} # page_id -> name
    page_pics: dict = {}  # page_id -> avatar url
    phan_loai: dict = {}  # (date, page_id) -> ma_win/ma_test (THEO NGÀY)
    vat_rate: dict = {}   # page_id -> rate (per-page override, ưu tiên cao nhất)
    tk_default_rate: dict = {}  # page_id -> rate (TK default từ vat_options, tier 2)
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # FB raw theo (page, ngày): MAX mỗi (page,TK,ngày) rồi SUM theo page-ngày
                cur.execute("""
                    SELECT page_id, metric_date, SUM(spend) FROM (
                        SELECT page_id, metric_date, fb_ad_account_id, MAX(spend) AS spend
                          FROM fb_ads_page_daily_spend
                         WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                         GROUP BY page_id, metric_date, fb_ad_account_id
                    ) t GROUP BY page_id, metric_date
                """, (page_ids, date_from, date_to))
                for pid, d, sp in cur.fetchall():
                    fb_pd[(str(d), str(pid))] = float(sp or 0)
                # POS theo (page, ngày)
                cur.execute("""
                    SELECT page_id, metric_date,
                           COALESCE(SUM(ads_amount),0), COALESCE(SUM(revenue),0),
                           COALESCE(SUM(profit),0), COALESCE(SUM(success_order_count),0),
                           COALESCE(SUM(returned_order_count),0), MAX(page_name),
                           COALESCE(SUM(sales),0)
                      FROM pos_page_daily_metrics
                     WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                     GROUP BY page_id, metric_date
                """, (page_ids, date_from, date_to))
                for pid, d, ads, rev, pf, sc, rc, nm, sal in cur.fetchall():
                    pos_pd[(str(d), str(pid))] = {
                        "ads": float(ads), "revenue": float(rev), "profit": float(pf),
                        "chot": int(sc), "hoan": int(rc), "sales": float(sal),
                    }
                    if nm:
                        page_names[str(pid)] = nm
                # ĐƠN Meta theo (page, ngày) — số đơn cho page TEST (chốt 2026-08-04):
                # ưu tiên registrations (điền form ladipage) > purchases (lượt mua).
                # Ladipage đa số đếm bằng complete_registration, không bắn purchase.
                cur.execute("""
                    SELECT page_id, metric_date,
                           GREATEST(COALESCE(SUM(registrations),0), COALESCE(SUM(purchases),0))
                      FROM mb_fb_entity_daily
                     WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                     GROUP BY page_id, metric_date
                """, (page_ids, date_from, date_to))
                for pid, d, pu in cur.fetchall():
                    meta_pd[(str(d), str(pid))] = int(pu or 0)
                # POS TÁCH THEO SHOP (1 page bán nhiều shop → kế toán đối chiếu từng shop).
                cur.execute("""
                    SELECT m.page_id, m.metric_date,
                           COALESCE(NULLIF(s.shop_name,''), m.pos_shop_id) AS shop_name,
                           COALESCE(m.ads_amount,0), COALESCE(m.revenue,0), COALESCE(m.profit,0),
                           COALESCE(m.success_order_count,0), COALESCE(m.returned_order_count,0)
                      FROM pos_page_daily_metrics m
                      LEFT JOIN shops s ON s.id = m.shop_id
                     WHERE m.page_id = ANY(%s) AND m.metric_date BETWEEN %s AND %s
                     ORDER BY m.ads_amount DESC
                """, (page_ids, date_from, date_to))
                for pid, d, sname, ads, rev, pf, sc, rc in cur.fetchall():
                    pos_pd_shops.setdefault((str(d), str(pid)), []).append({
                        "shop": (sname or "").strip(), "ads": float(ads), "revenue": float(rev),
                        "profit": float(pf), "chot": int(sc), "hoan": int(rc),
                    })
                # Tên page (ưu tiên fb_pages)
                cur.execute("SELECT page_id, page_name FROM fb_pages WHERE page_id = ANY(%s)", (page_ids,))
                for pid, nm in cur.fetchall():
                    if nm:
                        page_names[str(pid)] = nm
                # Tên+ẢNH THẬT (fb_page_names — ưu tiên cao nhất, từ resolver Graph/scrape)
                cur.execute("SELECT page_id, name, COALESCE(picture_url,'') FROM fb_page_names WHERE page_id = ANY(%s)", (page_ids,))
                for pid, nm, pic in cur.fetchall():
                    if nm:
                        page_names[str(pid)] = nm
                    if pic:
                        page_pics[str(pid)] = pic
                # Fallback cuối: tên trong bảng spend (suy từ campaign) cho page còn thiếu
                cur.execute("""SELECT page_id, MAX(COALESCE(NULLIF(page_name,''),''))
                                 FROM fb_ads_page_daily_spend WHERE page_id = ANY(%s)
                                GROUP BY page_id""", (page_ids,))
                for pid, nm in cur.fetchall():
                    if nm and str(pid) not in page_names:
                        page_names[str(pid)] = nm
                # ảnh mặc định (graph public) cho page chưa có pic
                for pid in page_ids:
                    page_pics.setdefault(str(pid), f"https://graph.facebook.com/{pid}/picture?type=square")
                # Win/Test + mức VAT (mới nhất trong khoảng)
                try:
                    # Win/Test THEO TỪNG NGÀY (không gộp) → modal hiện đúng mark mỗi ngày
                    cur.execute("""SELECT date, page_id, phan_loai
                                     FROM fb_page_auto_phan_loai
                                    WHERE date BETWEEN %s AND %s AND page_id = ANY(%s)""",
                                (date_from, date_to, page_ids))
                    for _d, pid, pl in cur.fetchall():
                        phan_loai[(str(_d), str(pid))] = pl
                except Exception:
                    pass
                try:
                    cur.execute("""SELECT DISTINCT ON (page_id) page_id, vat_rate
                                     FROM fb_page_vat_rate
                                    WHERE date BETWEEN %s AND %s AND page_id = ANY(%s)
                                    ORDER BY page_id, date DESC""", (date_from, date_to, page_ids))
                    for pid, vr in cur.fetchall():
                        vat_rate[str(pid)] = float(vr)
                except Exception:
                    pass
                # TK default rate (tier 2): mỗi page lấy TK có spend lớn nhất trong khoảng
                # → lookup default_vat_rate trong fb_ad_account_vat_options.
                # Fix bug: trước đây fallback thẳng 11.3% nếu thiếu per-page override → bỏ qua TK 6.1%.
                try:
                    cur.execute("""
                        SELECT DISTINCT ON (page_id) page_id, fb_ad_account_id
                          FROM fb_ads_page_daily_spend
                         WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                         GROUP BY page_id, fb_ad_account_id
                         ORDER BY page_id, SUM(spend) DESC
                    """, (page_ids, date_from, date_to))
                    page_to_tk = {str(pid): str(aid) for pid, aid in cur.fetchall()}
                    if page_to_tk:
                        all_tks = list({aid for aid in page_to_tk.values() if aid})
                        if all_tks:
                            cur.execute("""SELECT fb_ad_account_id, default_vat_rate
                                             FROM fb_ad_account_vat_options
                                            WHERE fb_ad_account_id = ANY(%s)""", (all_tks,))
                            tk_rate = {str(r[0]): float(r[1]) for r in cur.fetchall()
                                       if r[1] is not None}
                            for pid, aid in page_to_tk.items():
                                if aid in tk_rate:
                                    tk_default_rate[pid] = tk_rate[aid]
                except Exception as _exc:
                    logger.warning("tk_default_rate lookup error: %s", _exc)
    except Exception as exc:
        logger.error("page_daily_detail error: %s", exc)
        return jsonify({"error": "lỗi truy vấn"}), 500

    # ── Filter time-aware: nếu có user_id, loại bỏ (ngày, page) mà page
    # thuộc shop của NV khác vào ngày đó (chỉ áp cho page có explicit binding) ──
    if user_id_filter:
        try:
            uid = int(user_id_filter)
            from db import get_conn as _gc2
            with _gc2() as _conn2, _conn2.cursor() as _cur2:
                # shop IDs của NV này
                _cur2.execute("SELECT shop_id FROM user_shop_assignments WHERE user_id=%s AND assigned_to IS NULL", (uid,))
                _user_shop_ids = {r[0] for r in _cur2.fetchall()}
                # binding time-window cho tất cả page_ids trong khoảng ngày
                _cur2.execute("""
                    SELECT page_id::text, pos_shop_id,
                           assigned_from,
                           COALESCE(assigned_to, DATE '9999-12-31') AS assigned_to
                      FROM fb_page_shop_binding
                     WHERE page_id = ANY(%s)
                       AND assigned_from <= %s::date
                       AND (assigned_to IS NULL OR assigned_to >= %s::date)
                """, (page_ids, date_to, date_from))
                _bindings: dict = {}  # page_id -> [(from, to, shop_id)]
                for _pid, _sid, _bf, _bt in _cur2.fetchall():
                    _bindings.setdefault(str(_pid), []).append((_bf, _bt, _sid))
                # ── Cửa sổ sở hữu TK (uaa): page chạy bằng TK user TỪNG giữ →
                # chỉ giữ những (ngày, page) nằm TRONG cửa sổ user giữ TK.
                # TK đổi chủ giữa kỳ: ngày của chủ cũ không hiện bên chủ mới (bug 12/6).
                _cur2.execute("""
                    SELECT DISTINCT s.page_id::text
                      FROM fb_ads_page_daily_spend s
                      JOIN user_ad_account_assignments a ON a.ad_account_id = s.fb_ad_account_id
                     WHERE a.user_id = %s AND s.page_id = ANY(%s)
                       AND s.metric_date BETWEEN %s AND %s
                """, (uid, page_ids, date_from, date_to))
                _tk_pages = {r[0] for r in _cur2.fetchall()}
                _cur2.execute("""
                    SELECT DISTINCT s.metric_date::text, s.page_id::text
                      FROM fb_ads_page_daily_spend s
                      JOIN user_ad_account_assignments a ON a.ad_account_id = s.fb_ad_account_id
                       AND s.metric_date >= a.assigned_from
                       AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                     WHERE a.user_id = %s AND s.page_id = ANY(%s)
                       AND s.metric_date BETWEEN %s AND %s
                """, (uid, page_ids, date_from, date_to))
                _own_days = {(r[0], r[1]) for r in _cur2.fetchall()}

            def _page_ok(pid: str, d: _dt.date) -> bool:
                """True nếu page này thuộc NV (hoặc không có explicit binding)."""
                if pid not in _bindings:
                    return True  # tier-2 auto → không filter
                for _bf, _bt, _sid in _bindings[pid]:
                    if _bf <= d <= _bt:
                        return _sid in _user_shop_ids
                return True  # không có binding phủ ngày này → không filter

            fb_pd  = {(d, pid): v for (d, pid), v in fb_pd.items()
                      if _page_ok(pid, _dt.date.fromisoformat(d))
                      and (pid not in _tk_pages or (d, pid) in _own_days)}
            # POS cũng phải lọc theo cửa sổ sở hữu TK như FB — nếu không, ngày TK
            # thuộc NV khác (chủ cũ) bị lọc khỏi FB nhưng POS vẫn lọt → page hiện
            # với FB=0 mà POS có số (bug 19/6: Kim Khí Duy Tùng ngày 4,5 của duytungth
            # lọt vào modal phuong). Dùng ĐÚNG điều kiện như fb_pd.
            pos_pd = {(d, pid): v for (d, pid), v in pos_pd.items()
                      if _page_ok(pid, _dt.date.fromisoformat(d))
                      and (pid not in _tk_pages or (d, pid) in _own_days)}
        except Exception as _fe:
            logger.warning("page_daily_detail user_id filter error: %s", _fe)

    # Breakdown theo shop chỉ giữ những (ngày, page) còn lại sau lọc NV (khớp pos_pd).
    pos_pd_shops = {k: v for k, v in pos_pd_shops.items() if k in pos_pd}

    # Page TỪNG lên POS (bất kỳ ngày nào) — để phân loại Win/Test khớp bảng ngoài.
    pos_known = _query_pos_known_pages(page_ids)

    d0 = _dt.datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = _dt.datetime.strptime(date_to, "%Y-%m-%d").date()
    days = []
    cur_d = d0
    while cur_d <= d1:
        k = cur_d.isoformat()
        # các page có FB hoặc POS trong ngày này
        pids_today = {pid for (dd, pid) in fb_pd if dd == k} | {pid for (dd, pid) in pos_pd if dd == k}
        rows = []
        for pid in pids_today:
            fb_raw = fb_pd.get((k, pid), 0.0)
            # Priority: per-page override > TK default > _DEFAULT_VAT_RATE (11.3%)
            rate = vat_rate.get(pid)
            if rate is None:
                rate = tk_default_rate.get(pid, _DEFAULT_VAT_RATE)
            fb_vat = fb_raw * (1.0 + rate)
            po = pos_pd.get((k, pid))
            pos_ads = po["ads"] if po else None
            chot = po["chot"] if po else 0
            # Win/Test theo NGÀY (chốt 2026-08-04 — boss Phong):
            #   • NV đánh tay → theo mark
            #   • auto: WIN chỉ khi page CÓ trên POS **VÀ có doanh thu ngày đó**;
            #     chưa lên POS, hoặc lên POS mà doanh thu = 0 → TEST
            _pl = phan_loai.get((k, pid))   # mark NV cho (page, ngày)
            _rev = float((po or {}).get("revenue") or 0)
            if _pl == "ma_test":
                pl = "ma_test"
            elif _pl == "ma_win":
                pl = "ma_win"
            elif pos_known.get(pid) and _rev > 0:
                pl = "ma_win"
            else:
                pl = "ma_test"
            # THỐNG KÊ số đơn / tiền ads / ads-đơn (chốt 2026-08-04 boss):
            #   WIN → số đơn = đơn CHỐT POS ; TEST → số đơn = LƯỢT MUA Meta.
            #   tiền ads = CP Ads FB (chưa VAT = fb_raw); ads/đơn = fb_raw / số đơn.
            _meta_mua = meta_pd.get((k, pid), 0)
            if pl == "ma_win":
                _don = chot; _don_src = "POS"
            else:
                _don = _meta_mua; _don_src = "Meta"
            _ads_per_don = round(fb_raw / _don, 0) if _don > 0 else None
            rows.append({
                "page_id":   pid,
                "name":      page_names.get(pid) or ("Page " + pid[-8:]),
                "pic":       page_pics.get(pid) or (f"https://graph.facebook.com/{pid}/picture?type=square"),
                "phan_loai": pl,
                "vat_pct":   round(rate * 100, 1),
                "fb_raw":    round(fb_raw, 0),
                "fb_vat":    round(fb_vat, 0),
                # THỐNG KÊ đơn (win=POS / test=Meta)
                "so_don":    _don,
                "don_src":   _don_src,
                "meta_mua":  _meta_mua,
                "ads_per_don": _ads_per_don,
                "pos_ads":   round(pos_ads, 0) if pos_ads is not None else None,
                "cp_per_order": (round(pos_ads / chot, 0) if (pos_ads and chot > 0) else None),
                "sales":     round(po.get("sales", 0), 0) if po else None,
                "revenue":   round(po["revenue"], 0) if po else None,
                "profit":    round(po["profit"], 0) if po else None,
                "chot":      chot if po else None,
                "hoan":      po["hoan"] if po else None,
                "diff":      (round(fb_vat - pos_ads, 0) if pos_ads is not None else None),
                # Tách POS theo shop (>1 shop → JS hiện dòng phụ từng shop để kế toán đối chiếu)
                "pos_shops": [
                    {"shop": _sh["shop"],
                     "pos_ads": round(_sh["ads"], 0),
                     "revenue": round(_sh["revenue"], 0),
                     "profit":  round(_sh["profit"], 0),
                     "chot":    _sh["chot"], "hoan": _sh["hoan"],
                     "cp_per_order": (round(_sh["ads"] / _sh["chot"], 0) if _sh["chot"] > 0 else None)}
                    for _sh in pos_pd_shops.get((k, pid), [])
                ] if po else [],
            })
        if not rows:
            cur_d += _dt.timedelta(days=1)
            continue
        rows.sort(key=lambda x: -(x["fb_raw"] or 0))
        day_t = {
            "fb_raw":  sum(r["fb_raw"] for r in rows),
            "fb_vat":  sum(r["fb_vat"] for r in rows),
            "pos_ads": sum((r["pos_ads"] or 0) for r in rows),
            "sales":   sum((r["sales"] or 0) for r in rows),
            "revenue": sum((r["revenue"] or 0) for r in rows),
            "profit":  sum((r["profit"] or 0) for r in rows),
            "chot":    sum((r["chot"] or 0) for r in rows),
            "hoan":    sum((r["hoan"] or 0) for r in rows),
        }
        days.append({"date": k, "pages": rows, "totals": day_t})
        cur_d += _dt.timedelta(days=1)

    grand = {
        "fb_raw":  sum(d["totals"]["fb_raw"]  for d in days),
        "fb_vat":  sum(d["totals"]["fb_vat"]  for d in days),
        "pos_ads": sum(d["totals"]["pos_ads"] for d in days),
        "sales":   sum(d["totals"]["sales"]   for d in days),
        "revenue": sum(d["totals"]["revenue"] for d in days),
        "profit":  sum(d["totals"]["profit"]  for d in days),
        "chot":    sum(d["totals"]["chot"]    for d in days),
        "hoan":    sum(d["totals"]["hoan"]    for d in days),
    }
    return jsonify({"days": days, "totals": grand, "from": date_from, "to": date_to})


def sync_user_team_to_vat_options(user_id, team_code) -> int:
    """Đẩy team của 1 user XUỐNG mọi TK QC user đó đang phụ trách (vat-options).

    Gọi khi đổi team của user bên Cài đặt → cột Team của các TK NV đó đang giữ
    (qua user_ad_account_assignments active) tự nhảy theo team mới.
    `team_code` rỗng (bỏ team) → giữ nguyên, KHÔNG xoá team đang có.
    Trả số TK được đổi. Không raise — lỗi DB chỉ log + trả 0.
    """
    if not user_id or not team_code:
        return 0
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return 0
    team_code = str(team_code).strip()
    if not team_code:
        return 0
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE fb_ad_account_vat_options vo
                   SET team_code = %s, updated_at = NOW()
                  FROM user_ad_account_assignments a
                 WHERE a.ad_account_id = vo.fb_ad_account_id
                   AND a.user_id = %s
                   AND a.assigned_to IS NULL
                   AND vo.team_code IS DISTINCT FROM %s
            """, (team_code, uid, team_code))
            n = cur.rowcount
            conn.commit()
        return n or 0
    except Exception as exc:
        logger.warning("sync_user_team_to_vat_options error: %s", exc)
        return 0


@chi_phi_qc_bp.route("/vat-options", methods=["GET", "POST"])
@login_required
def vat_options():
    """Quản lý TK được phép chọn 6.1% (mặc định mọi TK = 11.3%).
    Chỉ admin/IT/manager/kế toán truy cập được."""
    role = (session.get("role") or "").lower()
    if role not in ("admin", "superadmin", "it", "manager", "ketoan", "accountant"):
        abort(403)
    user_id = session.get("user_id")
    from db import get_conn
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        def _clean_aid(x):
            x = (x or "").strip()
            return x[4:] if x.startswith("act_") else x

        if action == "add":
            # Cho phép chọn NHIỀU TK → áp chung 1 mức VAT/team/ghi chú
            aids = [_clean_aid(a) for a in request.form.getlist("fb_ad_account_id")]
            aids = [a for a in aids if a]
            if not aids:
                flash("Chưa chọn tài khoản quảng cáo.", "danger")
                return redirect(url_for("chi_phi_qc.vat_options"))
            note = (request.form.get("note") or "").strip() or None
            # VAT % tự do (mỗi cty 1 mức riêng) — vd 6.1 / 8 / 10
            raw_rate = (request.form.get("default_vat_rate") or "").strip().replace("%", "").replace(",", ".")
            try:
                rate = float(raw_rate)
            except (TypeError, ValueError):
                rate = 11.3
            if rate > 1:
                rate = rate / 100.0
            rate = max(0.0, min(rate, 1.0))
            team_code = (request.form.get("team_code") or "").strip() or None
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    for aid in aids:
                        # Tự tra tên TK (fb_ad_account_mappings → pa_ad_accounts)
                        nm = ""
                        for q, p in (
                            ("SELECT account_name FROM fb_ad_account_mappings WHERE fb_ad_account_id=%s AND account_name<>'' LIMIT 1", (aid,)),
                            ("SELECT account_name FROM pa_ad_accounts WHERE regexp_replace(account_id,'^act_','')=%s AND COALESCE(account_name,'')<>'' LIMIT 1", (aid,)),
                        ):
                            try:
                                cur.execute(q, p)
                                r = cur.fetchone()
                                if r and r[0]:
                                    nm = r[0]; break
                            except Exception:
                                pass
                        cur.execute("""
                            INSERT INTO fb_ad_account_vat_options (fb_ad_account_id, account_name, default_vat_rate, allow_6_1, team_code, updated_by, note)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (fb_ad_account_id) DO UPDATE SET
                                account_name = COALESCE(NULLIF(EXCLUDED.account_name,''), fb_ad_account_vat_options.account_name),
                                default_vat_rate = EXCLUDED.default_vat_rate,
                                allow_6_1 = EXCLUDED.allow_6_1,
                                team_code = COALESCE(EXCLUDED.team_code, fb_ad_account_vat_options.team_code),
                                updated_at = NOW(),
                                updated_by = EXCLUDED.updated_by,
                                note = COALESCE(EXCLUDED.note, fb_ad_account_vat_options.note)
                        """, (aid, nm or None, rate, abs(rate - 0.113) > 1e-6, team_code, user_id, note))
                    conn.commit()
                _msg = (f"Đã set VAT <b>{rate*100:.1f}%</b> cho <b>{len(aids)}</b> tài khoản."
                        if len(aids) > 1 else f"Đã set VAT <b>{rate*100:.1f}%</b> cho tài khoản.")
                flash(_msg, "success")
            except Exception as exc:
                flash(f"Lỗi: {exc}", "danger")
            return redirect(url_for("chi_phi_qc.vat_options"))

        elif action == "delete":
            aid_raw = _clean_aid(request.form.get("fb_ad_account_id"))
            if not aid_raw:
                flash("Thiếu Ad Account ID", "danger")
                return redirect(url_for("chi_phi_qc.vat_options"))
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM fb_ad_account_vat_options WHERE fb_ad_account_id=%s", (aid_raw,))
                    conn.commit()
                flash(f"Đã gỡ TK <b>{aid_raw}</b> (TK sẽ về VAT mặc định 11.3%)", "warning")
            except Exception as exc:
                flash(f"Lỗi: {exc}", "danger")
            return redirect(url_for("chi_phi_qc.vat_options"))

    # GET — list + filter team + tìm kiếm
    f_team = (request.args.get("team") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    items = []
    teams_list = []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            # Lấy list teams active để filter
            try:
                cur.execute("SELECT team_code, team_name FROM teams WHERE status='active' ORDER BY team_name")
                teams_list = [{"code": r[0], "name": r[1]} for r in cur.fetchall()]
            except Exception:
                teams_list = []
            sql = """
                SELECT v.fb_ad_account_id, v.account_name, v.default_vat_rate, v.allow_6_1,
                       v.team_code, t.team_name,
                       v.created_at, v.updated_at, v.note,
                       u.full_name AS updated_by_name,
                       -- NV phụ trách (qua user_ad_account_assignments active, dedup nếu nhiều)
                       (SELECT string_agg(DISTINCT COALESCE(NULLIF(uu.full_name,''), uu.username), ', ' ORDER BY COALESCE(NULLIF(uu.full_name,''), uu.username))
                          FROM user_ad_account_assignments a
                          JOIN users uu ON uu.id = a.user_id
                         WHERE a.ad_account_id = v.fb_ad_account_id
                           AND a.assigned_to IS NULL) AS assigned_nv
                  FROM fb_ad_account_vat_options v
                  LEFT JOIN users u ON u.id = v.updated_by
                  LEFT JOIN teams t ON t.team_code = v.team_code
            """
            args = []
            conds = []
            if f_team:
                if f_team == "_none":
                    conds.append("v.team_code IS NULL")
                else:
                    conds.append("v.team_code = %s")
                    args.append(f_team)
            if f_q:
                like = f"%{f_q}%"
                conds.append(
                    "(v.fb_ad_account_id ILIKE %s OR v.account_name ILIKE %s OR v.note ILIKE %s "
                    "OR EXISTS (SELECT 1 FROM user_ad_account_assignments a2 "
                    "JOIN users uu2 ON uu2.id = a2.user_id "
                    "WHERE a2.ad_account_id = v.fb_ad_account_id AND a2.assigned_to IS NULL "
                    "AND (uu2.full_name ILIKE %s OR uu2.username ILIKE %s)))"
                )
                args.extend([like, like, like, like, like])
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY v.team_code NULLS LAST, v.default_vat_rate DESC, v.account_name NULLS LAST"
            cur.execute(sql, args)
            cols = [c[0] for c in cur.description]
            items = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        flash(f"Lỗi đọc danh sách: {exc}", "danger")

    # Danh sách TK quảng cáo đã kéo từ Facebook (để gợi ý chọn sẵn, khỏi gõ ID tay)
    ad_accounts = []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT regexp_replace(account_id, '^act_', '') AS aid,
                       COALESCE(NULLIF(account_name,''), account_id) AS name
                FROM pa_ad_accounts
                WHERE account_id IS NOT NULL AND account_id <> ''
                ORDER BY name
            """)
            ad_accounts = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    except Exception:
        ad_accounts = []

    return render_template("chi_phi_qc/vat_options.html",
                           items=items, current_role=role,
                           teams=teams_list, f_team=f_team, f_q=f_q,
                           ad_accounts=ad_accounts)


@chi_phi_qc_bp.route("/bao-cao/xuat-csv")
@login_required
def bao_cao_xuat_csv():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from db import get_conn
    user_id   = session.get("user_id")
    role      = session.get("role", "staff")

    if not _is_full_view(role):
        abort(403)

    today     = date.today()
    date_from    = request.args.get("date_from",   today.replace(day=1).isoformat()).strip()
    date_to      = request.args.get("date_to",     today.isoformat()).strip()
    tab          = request.args.get("tab",          "nhanvien")
    team_filter  = request.args.get("team_filter",  "").strip()
    user_filter  = request.args.get("user_filter",  "").strip()

    wb = openpyxl.Workbook()
    ws = wb.active

    # Style helpers
    hdr_fill  = PatternFill("solid", fgColor="1F4E79")
    hdr_font  = Font(bold=True, color="FFFFFF", size=10)
    foot_fill = PatternFill("solid", fgColor="D9E1F2")
    foot_font = Font(bold=True, size=10)
    num_fmt   = '#,##0'
    center    = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def write_header(ws, cols):
        ws.append(cols)
        for cell in ws[1]:
            cell.font      = hdr_font
            cell.fill      = hdr_fill
            cell.alignment = center

    def style_number_row(ws, row_idx, num_cols):
        for col in num_cols:
            cell = ws.cell(row=row_idx, column=col)
            cell.number_format = num_fmt
            cell.alignment = center

    def autofit(ws):
        for col_cells in ws.columns:
            length = max((len(str(c.value or "")) for c in col_cells), default=8)
            ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length + 4, 40)

    try:
        if tab == "nhanvien":
            ws.title = "Nhân viên"
            rows = _build_rows_shop(date_from, date_to, user_id, role,
                                    team_filter=team_filter, user_filter=user_filter)
            cols = ["Nhân viên", "Username", "Team",
                    "Win khai\nchưa VAT", "Win khai\nđã VAT",
                    "Test tự động\nchưa VAT", "Test tự động\nđã VAT",
                    "Test thủ công\nchưa VAT", "Test thủ công\nđã VAT",
                    "Tổng đã VAT", "CP Ads POS", "Tổng khai − POS"]
            write_header(ws, cols)
            num_cols = list(range(4, len(cols) + 1))
            for r in rows:
                tong  = float(r.get("tong",         0) or 0)
                pos   = float(r.get("cp_ads_pos",   0) or 0)
                chenh = tong - pos
                ws.append([
                    r["full_name"], r["username"],
                    r.get("team_id","").replace("team-","Team ").title(),
                    r.get("win_chua_vat",   0), r.get("win_co_vat",   0),
                    r.get("auto_test_chua", 0), r.get("auto_test_co", 0),
                    r.get("test_chua_vat",  0), r.get("test_co_vat",  0),
                    tong, pos, chenh,
                ])
                style_number_row(ws, ws.max_row, num_cols)
            # Footer totals
            if rows:
                ws.append(["TỔNG", "", ""] + [
                    sum(float(r.get(k, 0) or 0) for r in rows)
                    for k in ["win_chua_vat","win_co_vat","auto_test_chua","auto_test_co",
                              "test_chua_vat","test_co_vat","tong","cp_ads_pos"]
                ] + [sum(float(r.get("tong",0) or 0) - float(r.get("cp_ads_pos",0) or 0) for r in rows)])
                for cell in ws[ws.max_row]:
                    cell.fill = foot_fill; cell.font = foot_font
                    cell.number_format = num_fmt; cell.alignment = center

        elif tab == "page":
            ws.title = "Theo page"
            with get_conn() as conn:
                rows = _query_page_breakdown(conn, date_from, date_to, user_id, role)
            cols = ["Page ID", "Tên Page", "FB Spend (VND)", "Impressions", "Clicks",
                    "Win khai\nchưa VAT", "Win khai\nđã VAT",
                    "Test khai\nchưa VAT", "Test khai\nđã VAT",
                    "Tổng khai\nchưa VAT", "Tổng khai\nđã VAT", "Số khai báo"]
            write_header(ws, cols)
            num_cols = list(range(3, len(cols) + 1))
            for r in rows:
                ws.append([r["page_id"], r["page_name"],
                           r["fb_spend"], r["fb_impr"], r["fb_clicks"],
                           r["win_chua"], r["win_co"],
                           r["test_chua"], r["test_co"],
                           r["khai_chua"], r["khai_co"], r["so_khai"]])
                style_number_row(ws, ws.max_row, num_cols)

        else:
            ws.title = "Chi tiết"
            with get_conn() as conn:
                rows = _query_declarations_detail(conn, date_from, date_to, user_id, role)
            cols = ["Ngày", "Page ID", "Tên Page", "Nhân viên", "Username",
                    "Tài khoản Ads", "Phân loại", "Chưa VAT", "Thuế suất (%)",
                    "Đã VAT", "Ghi chú", "Thời gian khai báo"]
            write_header(ws, cols)
            num_cols = [8, 10]
            for r in rows:
                ws.append([r["spend_date"], r["page_id"], r["page_name"],
                           r["full_name"], r["username"], r["ad_account_name"] or "",
                           r["phan_loai"],
                           float(r["tien_chua_thue"] or 0),
                           float(r["thue_rate"] or 0),
                           float(r["thanh_tien"] or 0),
                           r["note"] or "",
                           r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""])
                style_number_row(ws, ws.max_row, num_cols)

        autofit(ws)

    except Exception as exc:
        logger.error("xuat_excel error: %s", exc)
        return f"Lỗi xuất Excel: {exc}", 500

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"chi_phi_qc_{tab}_{date_from}_{date_to}.xlsx"
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@chi_phi_qc_bp.route("/bao-cao/chot-ky", methods=["POST"])
@login_required
def chot_ky():
    role = session.get("role", "staff")
    if role not in ("admin", "superadmin", "manager", "ketoan", "accountant"):
        return jsonify({"ok": False, "error": "Không có quyền chốt kỳ"}), 403

    from db import get_conn
    _ensure_snapshots_table()

    date_from    = request.json.get("date_from", "").strip()
    date_to      = request.json.get("date_to",   "").strip()
    note         = request.json.get("note",       "").strip()
    period_label = request.json.get("period_label", f"{date_from} → {date_to}").strip()
    user_id      = session.get("user_id")
    username     = session.get("full_name") or session.get("username", "")

    if not date_from or not date_to:
        return jsonify({"ok": False, "error": "Thiếu date_from / date_to"}), 400

    try:
        with get_conn() as conn:
            rows_employee = _query_employee_summary(conn, date_from, date_to, None, "admin")
            rows_page     = _query_page_breakdown(conn, date_from, date_to, None, "admin")
            rows_detail   = _query_declarations_detail(conn, date_from, date_to, None, "admin")

            snapshot = {
                "date_from": date_from,
                "date_to":   date_to,
                "employee_summary": rows_employee,
                "page_breakdown":   rows_page,
                "declarations":     [
                    {k: (v.isoformat() if hasattr(v, "isoformat") else
                         float(v) if isinstance(v, decimal.Decimal) else v)
                     for k, v in r.items()}
                    for r in rows_detail
                ],
            }

            total_amount = sum(
                float(r.get("thanh_tien") or 0) for r in rows_detail
            )

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chi_phi_qc_period_snapshots
                        (period_label, date_from, date_to, note,
                         locked_by_user_id, locked_by_name,
                         total_rows, total_amount, data_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (period_label, date_from, date_to, note or None,
                      user_id, username, len(rows_detail), total_amount,
                      json.dumps(snapshot, default=str)))
                snap_id = cur.fetchone()[0]

        return jsonify({
            "ok": True,
            "snapshot_id": snap_id,
            "total_rows": len(rows_detail),
            "total_amount": total_amount,
        })
    except Exception as exc:
        logger.error("chot_ky error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@chi_phi_qc_bp.route("/bao-cao/xem-snapshot/<int:snap_id>")
@login_required
def xem_snapshot(snap_id):
    role = session.get("role", "staff")
    if not _is_senior(role):
        flash("Không có quyền xem snapshot.", "danger")
        return redirect(url_for("chi_phi_qc.bao_cao"))

    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, period_label, date_from, date_to, note,
                           locked_at, locked_by_name, total_rows, total_amount, data_json
                    FROM chi_phi_qc_period_snapshots WHERE id = %s
                """, (snap_id,))
                row = cur.fetchone()
        if not row:
            flash("Không tìm thấy snapshot.", "warning")
            return redirect(url_for("chi_phi_qc.bao_cao"))
        cols = ["id","period_label","date_from","date_to","note",
                "locked_at","locked_by_name","total_rows","total_amount","data_json"]
        snap = dict(zip(cols, row))
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
        return redirect(url_for("chi_phi_qc.bao_cao"))

    return render_template("chi_phi_qc/xem_snapshot.html", snap=snap, current_role=role)


# ═══════════ PAGE TEST — Bảng tổng hợp sản phẩm test (thay Excel, 2026-06-12) ═══════════
# Mỗi dòng = 1 ngày × 1 page × 1 sản phẩm. NV nhập tay: sản phẩm/giá bán/đơn/SL (+DT tuỳ chỉnh).
# Tự động: spend Meta theo page/ngày (fb_ads_page_daily_spend), VAT theo quy định sẵn,
# TK→NV→team theo đúng ngày (versioned). Trạng thái Test/Win đọc & ghi CHUNG
# fb_page_auto_phan_loai với trang Chi phí QC (nút Win|Test y hệt, cùng endpoint).


def _pt_spend_map(d_from: str, d_to: str) -> dict:
    """{(page_id, 'YYYY-MM-DD') → {spend, accounts:[{id, name}]}} — TK sort theo spend DESC."""
    out = {}
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT s.page_id, s.metric_date::date, s.fb_ad_account_id, SUM(s.spend)
                FROM fb_ads_page_daily_spend s
                WHERE s.metric_date::date BETWEEN %s::date AND %s::date
                GROUP BY 1, 2, 3
            """, (d_from, d_to))
            rows = cur.fetchall()
            aids = sorted({str(r[2]) for r in rows if r[2]})
            names = {}
            if aids:
                cur.execute("""
                    SELECT DISTINCT ON (ad_account_id) ad_account_id, ad_account_name
                    FROM user_ad_account_assignments
                    WHERE ad_account_id = ANY(%s)
                    ORDER BY ad_account_id, assigned_from DESC
                """, (aids,))
                names = {str(r[0]): (r[1] or "") for r in cur.fetchall()}
        for pid, mdate, aid, spend in rows:
            key = (str(pid), mdate.strftime("%Y-%m-%d"))
            rec = out.setdefault(key, {"spend": 0.0, "accounts": []})
            rec["spend"] += float(spend or 0)
            rec["accounts"].append({"id": str(aid or ""),
                                    "name": names.get(str(aid), "") or str(aid or ""),
                                    "spend": float(spend or 0)})
        for rec in out.values():
            rec["accounts"].sort(key=lambda a: -a["spend"])
    except Exception as e:
        logger.warning("page_test: load spend map error: %s", e)
    return out


def _pt_load_eval_map() -> dict:
    """{(page_id, 'YYYY-MM-DD') → 'dat'|'khong_dat'} — đánh giá theo TỪNG NGÀY.
    Bấm Đạt ở ngày nào chỉ đánh đúng (page, ngày) đó; ngày khác chưa đánh → không hiện."""
    out = {}
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("SELECT page_id, entry_date, danh_gia FROM page_test_eval WHERE danh_gia IS NOT NULL")
            out = {(str(r[0]), r[1].strftime("%Y-%m-%d")): r[2] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("page_test: load eval map error: %s", e)
    return out


def _pt_page_team_code(page_id: str) -> str | None:
    """team_code của NV đang phụ trách page (qua TK QC chạy gần nhất). Để check quyền leader."""
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT t.team_code
                  FROM fb_ads_page_daily_spend s
                  JOIN user_ad_account_assignments ua
                    ON ua.ad_account_id = s.fb_ad_account_id AND ua.assigned_to IS NULL
                  JOIN users u ON u.id = ua.user_id
                  JOIN teams t ON t.id = u.team_id
                 WHERE s.page_id = %s
                 ORDER BY s.metric_date DESC
                 LIMIT 1
            """, (str(page_id),))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning("page_test: resolve page team error: %s", e)
        return None


def _pt_owner_segments(account_ids: list, d_from: str, d_to: str) -> dict:
    """{account_id → [{from, to, username, full_name, team_code, team_name}]} (versioned)."""
    out = {}
    if not account_ids:
        return out
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT a.ad_account_id, a.assigned_from, a.assigned_to,
                       u.username, COALESCE(NULLIF(TRIM(u.full_name), ''), u.username),
                       COALESCE(t.team_code, ''), COALESCE(t.team_name, '')
                FROM user_ad_account_assignments a
                JOIN users u ON u.id = a.user_id
                LEFT JOIN teams t ON t.id = u.team_id
                WHERE a.ad_account_id = ANY(%s)
                  AND a.assigned_from <= %s::date
                  AND (a.assigned_to IS NULL OR a.assigned_to >= %s::date)
            """, (list(account_ids), d_to, d_from))
            for aid, af, at_, un, fn, tc, tn in cur.fetchall():
                out.setdefault(str(aid), []).append({
                    "from": af, "to": at_, "username": un, "full_name": fn,
                    "team_code": tc, "team_name": tn,
                })
    except Exception as e:
        logger.warning("page_test: load owner segments error: %s", e)
    return out


def _pt_resolve_owner(segments: list, d) -> dict | None:
    for seg in segments or []:
        if seg["from"] <= d and (seg["to"] is None or d <= seg["to"]):
            return seg
    return None


def _pt_page_options(limit_days: int = 60) -> list:
    """Danh sách page (id, name) có spend trong N ngày gần nhất — cho dropdown thêm dòng."""
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT page_id, MAX(COALESCE(NULLIF(page_name, ''), page_id)) AS pname
                FROM fb_ads_page_daily_spend
                WHERE metric_date::date >= CURRENT_DATE - %s
                GROUP BY page_id ORDER BY 2
            """, (limit_days,))
            return [{"id": str(r[0]), "name": r[1]} for r in cur.fetchall()]
    except Exception as e:
        logger.warning("page_test: load page options error: %s", e)
        return []


import re as _pt_re
import unicodedata as _pt_ud

_PT_DATE_SEG = _pt_re.compile(r"^\d{1,2}\s*/\s*\d{1,2}(\s*/\s*\d{2,4})?$")


def _pt_no_accent(s: str) -> str:
    s = _pt_ud.normalize("NFD", s or "")
    return "".join(c for c in s if _pt_ud.category(c) != "Mn").lower().strip()


def _pt_extract_product(campaign: str, page_name: str) -> str:
    """Bóc tên sản phẩm từ tên chiến dịch: bỏ đoạn ngày (8/6, 09/06/2026...) và
    đoạn trùng tên page (so khớp không dấu, chấp nhận viết tắt 1 phần).
    NV đặt tên bằng '-', '+', '_' hoặc '–' đều xử lý. Không bóc được → trả nguyên tên."""
    parts = [p.strip() for p in _pt_re.split(r"\s*[-+_–]\s*", campaign or "") if p.strip()]
    pn_words = set(_pt_no_accent(page_name).split())
    keep = []
    for p in parts:
        if _PT_DATE_SEG.match(p):
            continue
        pw = set(_pt_no_accent(p).split())
        if pw and pn_words and len(pw & pn_words) / len(pw) >= 0.6:
            continue  # đoạn trùng tên page
        keep.append(p)
    return " - ".join(keep).strip() or (campaign or "").strip()


def _pt_campaigns_map(d_from: str, d_to: str) -> dict:
    """{page_id → {'YYYY-MM-DD': [campaign_name, ...]}} — sort theo spend DESC.
    Quét RỘNG ±14 ngày quanh khoảng lọc: page đánh test nhưng chưa tiêu tiền
    đúng ngày đó vẫn gợi ý được tên SP từ chiến dịch ngày gần nhất."""
    out = {}
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT page_id, metric_date::date, campaign_name, SUM(spend) AS sp
                FROM mb_fb_entity_daily
                WHERE metric_date::date BETWEEN %s::date - 14 AND %s::date + 14
                  AND COALESCE(page_id, '') <> '' AND COALESCE(campaign_name, '') <> ''
                GROUP BY 1, 2, 3 ORDER BY 1, 2, sp DESC
            """, (d_from, d_to))
            for pid, d, camp, _sp in cur.fetchall():
                out.setdefault(str(pid), {}).setdefault(d.strftime("%Y-%m-%d"), []).append(camp)
    except Exception as e:
        logger.warning("page_test: load campaigns error: %s", e)
    return out


def _pt_fallback_maps(pids: list, d_from: str, d_to: str) -> tuple:
    """Fallback cho page không có spend trong ngày:
    - tk_fb: {page_id → {id, name}} TK gần nhất từng chạy page (≤ d_to)
    - bind_owner: {page_id → [segments]} NV/team qua page→shop binding × gán shop (versioned)
    """
    tk_fb, bind_owner = {}, {}
    if not pids:
        return tk_fb, bind_owner
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (s.page_id) s.page_id, s.fb_ad_account_id
                FROM fb_ads_page_daily_spend s
                WHERE s.page_id = ANY(%s) AND s.metric_date::date <= %s::date
                ORDER BY s.page_id, s.metric_date DESC
            """, (list(pids), d_to))
            tk_rows = cur.fetchall()
            aids = sorted({str(r[1]) for r in tk_rows if r[1]})
            names = {}
            if aids:
                cur.execute("""
                    SELECT DISTINCT ON (ad_account_id) ad_account_id, ad_account_name
                    FROM user_ad_account_assignments WHERE ad_account_id = ANY(%s)
                    ORDER BY ad_account_id, assigned_from DESC
                """, (aids,))
                names = {str(r[0]): (r[1] or "") for r in cur.fetchall()}
            for pid, aid in tk_rows:
                if aid:
                    tk_fb[str(pid)] = {"id": str(aid),
                                       "name": names.get(str(aid), "") or str(aid)}
            cur.execute("""
                SELECT b.page_id,
                       GREATEST(b.assigned_from, usa.assigned_from),
                       LEAST(COALESCE(b.assigned_to, '9999-12-31'::date),
                             COALESCE(usa.assigned_to, '9999-12-31'::date)),
                       u.username, COALESCE(NULLIF(TRIM(u.full_name), ''), u.username),
                       COALESCE(t.team_code, ''), COALESCE(t.team_name, '')
                FROM fb_page_shop_binding b
                JOIN user_shop_assignments usa ON usa.shop_id = b.pos_shop_id
                JOIN users u ON u.id = usa.user_id
                LEFT JOIN teams t ON t.id = u.team_id
                WHERE b.page_id = ANY(%s)
                  AND b.assigned_from <= %s::date
                  AND (b.assigned_to IS NULL OR b.assigned_to >= %s::date)
                  AND usa.assigned_from <= %s::date
                  AND (usa.assigned_to IS NULL OR usa.assigned_to >= %s::date)
            """, (list(pids), d_to, d_from, d_to, d_from))
            for pid, sfrom, sto, un, fn, tc, tn in cur.fetchall():
                bind_owner.setdefault(str(pid), []).append({
                    "from": sfrom, "to": sto, "username": un, "full_name": fn,
                    "team_code": tc, "team_name": tn,
                })
    except Exception as e:
        logger.warning("page_test: fallback maps error: %s", e)
    return tk_fb, bind_owner


def _pt_current_user_team() -> str:
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(t.team_code, '') FROM users u
                LEFT JOIN teams t ON t.id = u.team_id WHERE u.username = %s
            """, (session.get("username", ""),))
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


@chi_phi_qc_bp.route("/page-test")
@login_required
def page_test():
    role = (session.get("role") or "").lower()
    username = session.get("username", "")
    today = now_hcm().strftime("%Y-%m-%d")

    date_from = (request.args.get("date_from") or today).strip()
    date_to = (request.args.get("date_to") or today).strip()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    f_team = (request.args.get("team") or "").strip()
    f_staff = (request.args.get("staff") or "").strip()
    f_q = (request.args.get("q") or "").strip().lower()
    f_tab = (request.args.get("tab") or "").strip()  # '' | 'test' | 'win'

    # ── Entries trong khoảng ngày ──
    entries = []
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, entry_date, page_id, page_name, product_name, gia_ban,
                       don_hang, so_luong, doanh_thu_custom, note, created_by
                FROM page_test_entries
                WHERE entry_date BETWEEN %s::date AND %s::date
                ORDER BY entry_date DESC, page_name, product_name
            """, (date_from, date_to))
            cols = ["id", "entry_date", "page_id", "page_name", "product_name", "gia_ban",
                    "don_hang", "so_luong", "doanh_thu_custom", "note", "created_by"]
            entries = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error("page_test: load entries error: %s", e)

    # ── Dữ liệu tự động: spend, VAT, phân loại, NV/team ──
    spend_map = _pt_spend_map(date_from, date_to)
    vat_rate_map = _load_vat_rate_map(date_from, date_to)          # override per page
    phan_loai_map = _load_auto_phan_loai_map(date_from, date_to)   # ma_test / ma_win (mới nhất trong range)

    # Phân loại CHI TIẾT theo từng (page, ngày) — để tự sinh dòng cho page bị đánh Test
    pl_by_pid_date = {}
    page_name_map = {}
    try:
        from db import get_conn as _gc_pl
        with _gc_pl() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT page_id, date, phan_loai FROM fb_page_auto_phan_loai
                WHERE date BETWEEN %s::date AND %s::date
            """, (date_from, date_to))
            for pid, d, pl in cur.fetchall():
                pl_by_pid_date[(str(pid), d.strftime("%Y-%m-%d"))] = pl
            _pl_pids = sorted({k[0] for k in pl_by_pid_date} | {k[0] for k in spend_map})
            if _pl_pids:
                cur.execute("""
                    SELECT page_id, MAX(COALESCE(NULLIF(page_name,''), page_id))
                    FROM fb_ads_page_daily_spend WHERE page_id = ANY(%s) GROUP BY page_id
                """, (_pl_pids,))
                page_name_map = {str(r[0]): r[1] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("page_test: load phan_loai per-date error: %s", e)
    tk_default_vat = {}
    try:
        from db import get_conn as _gc_v
        with _gc_v() as _c, _c.cursor() as _cur:
            _cur.execute("SELECT fb_ad_account_id, default_vat_rate FROM fb_ad_account_vat_options")
            tk_default_vat = {str(r[0]): float(r[1] or _DEFAULT_VAT_RATE) for r in _cur.fetchall() if r[0]}
    except Exception:
        pass
    # Fallback cho page chưa có spend trong ngày (CP=0 nhưng vẫn phải biết TK/NV/team)
    _all_pids = sorted({e["page_id"] for e in entries}
                       | {k[0] for k in pl_by_pid_date})
    tk_fb, bind_owner = _pt_fallback_maps(_all_pids, date_from, date_to)
    campaigns_map = _pt_campaigns_map(date_from, date_to)  # gợi ý tên SP từ tên chiến dịch
    # POS: page ĐÃ TỪNG lên POS (có đơn thật) → dùng để tự phân loại Win/Test.
    _spend_pids = sorted({k[0] for k in spend_map} | {k[0] for k in pl_by_pid_date})
    pos_known = set(_query_pos_known_pages(_spend_pids).keys()) if _spend_pids else set()
    eval_map = _pt_load_eval_map()   # {(page_id, ngày) → 'dat'|'khong_dat'}

    _all_aids = {a["id"] for rec in spend_map.values() for a in rec["accounts"] if a["id"]}
    _all_aids |= {t["id"] for t in tk_fb.values()}
    owner_segs = _pt_owner_segments(sorted(_all_aids), date_from, date_to)

    def _pt_build_row(e, auto=False):
        d_str = e["entry_date"].strftime("%Y-%m-%d")
        rec = spend_map.get((e["page_id"], d_str), {"spend": 0.0, "accounts": []})
        main_acc = rec["accounts"][0] if rec["accounts"] else tk_fb.get(e["page_id"])
        owner = _pt_resolve_owner(owner_segs.get(main_acc["id"]) if main_acc else None,
                                  e["entry_date"])
        if owner is None:
            # Fallback 2: page → shop binding → NV gán shop (đều versioned theo ngày)
            owner = _pt_resolve_owner(bind_owner.get(e["page_id"]), e["entry_date"])
        # VAT: #1 override per page → #2 default theo TK chính → #3 11.3%
        rate = vat_rate_map.get(e["page_id"])
        if rate is None and main_acc:
            rate = tk_default_vat.get(main_acc["id"])
        if rate is None:
            rate = _DEFAULT_VAT_RATE
        spend = rec["spend"]
        gia_ban = float(e["gia_ban"] or 0)
        so_luong = int(e["so_luong"] or 0)
        doanh_thu = (float(e["doanh_thu_custom"])
                     if e["doanh_thu_custom"] is not None else so_luong * gia_ban)
        # Gợi ý tên SP cho dòng auto: bóc từ tên chiến dịch của page hôm đó.
        # Chưa có chiến dịch đúng ngày (page test chưa tiêu tiền) → lấy ngày
        # gần nhất trong ±14 ngày (ưu tiên ngày trước). Nhiều chiến dịch →
        # liệt kê cách nhau dấu phẩy (dedup, giữ thứ tự spend).
        product_suggest = ""
        if auto:
            _by_date = campaigns_map.get(e["page_id"], {})
            camps = _by_date.get(d_str)
            if not camps and _by_date:
                def _dist(x):
                    dx = (datetime.strptime(x, "%Y-%m-%d").date() - e["entry_date"]).days
                    return (abs(dx), 0 if dx < 0 else 1)
                _best = min(_by_date, key=_dist)
                if abs((datetime.strptime(_best, "%Y-%m-%d").date()
                        - e["entry_date"]).days) <= 14:
                    camps = _by_date[_best]
            _seen = {}
            for camp in camps or []:
                sp = _pt_extract_product(camp, e["page_name"] or "")
                if sp:
                    _seen.setdefault(sp, True)
            product_suggest = ", ".join(_seen)
        return {
            **e, "date_str": d_str, "auto": auto,
            "product_suggest": product_suggest,
            "tk_names": (", ".join(a["name"] for a in rec["accounts"])
                         or (main_acc["name"] if main_acc else "")
                         or "—"),
            "main_acc_id": main_acc["id"] if main_acc else "",
            "owner_username": owner["username"] if owner else "",
            "owner_name": owner["full_name"] if owner else "—",
            "team_code": owner["team_code"] if owner else "",
            "team_name": owner["team_name"] if owner else "—",
            "spend_meta": spend,
            "vat_pct": round(rate * 100, 1),
            "spend_vat": round(spend * (1.0 + rate), 0),
            "doanh_thu": doanh_thu,
            "doanh_thu_custom_flag": e["doanh_thu_custom"] is not None,
            # Win/Test HIỆU LỰC theo POS (chốt 2026-06-13):
            #  • chưa lên POS → luôn Test (kể cả NV bấm Win)
            #  • đã lên POS   → Win, trừ khi NV bấm Test (case 2 — chủ mới test lại)
            "phan_loai": ("ma_win"
                          if (e["page_id"] in pos_known
                              and pl_by_pid_date.get((e["page_id"], d_str)) != "ma_test")
                          else "ma_test"),
            "danh_gia": eval_map.get((e["page_id"], d_str)),
        }

    rows = [_pt_build_row(e) for e in entries]

    # ── Dòng TỰ ĐỘNG (theo POS — chốt 2026-06-13) ──
    #  • Page CHƯA lên POS → luôn lấy về (Test), kể cả NV bấm Win.
    #  • Page ĐÃ lên POS   → chỉ lấy khi NV bấm Test (ma_test) — case 2: chủ mới test lại.
    _manual_keys = {(e["page_id"], e["entry_date"].strftime("%Y-%m-%d")) for e in entries}
    _auto_keys = set(spend_map.keys()) | set(pl_by_pid_date.keys())
    for (pid, d_str) in sorted(_auto_keys, key=lambda k: k[1], reverse=True):
        if (pid, d_str) in _manual_keys:
            continue
        if (spend_map.get((pid, d_str), {}).get("spend", 0.0) or 0) <= 0:
            continue   # phải có chi phí ads thật > 0 mới là dòng test (bỏ dòng 0đ)
        if pid in pos_known and pl_by_pid_date.get((pid, d_str)) != "ma_test":
            continue   # đã lên POS, không đánh Test → Win → bỏ qua
        rows.append(_pt_build_row({
            "id": None,
            "entry_date": datetime.strptime(d_str, "%Y-%m-%d").date(),
            "page_id": pid,
            "page_name": page_name_map.get(pid, pid),
            "product_name": "", "gia_ban": 0, "don_hang": 0, "so_luong": 0,
            "doanh_thu_custom": None, "note": "", "created_by": "",
        }, auto=True))
    rows.sort(key=lambda r: (r["date_str"], r["page_name"] or "", r["product_name"] or ""),
              reverse=True)

    # ── Phân quyền: full → tất cả; leader → team mình; NV → của mình ──
    my_team = _pt_current_user_team()
    if not _is_full_view(role):
        if role == "leader" and my_team:
            rows = [r for r in rows if r["team_code"] == my_team or r["created_by"] == username]
        else:
            rows = [r for r in rows
                    if r["owner_username"] == username or r["created_by"] == username]

    # ── Bộ lọc ──
    # Dropdown NV: TOÀN BỘ NV đang giữ TK QC (giống Marketing Brain), scope theo quyền —
    # full view thấy hết, leader thấy team mình, NV thấy mình
    staff_options = []
    try:
        from db import get_conn as _gc_s
        with _gc_s() as conn, conn.cursor() as cur:
            _sql = """
                SELECT DISTINCT u.username,
                       COALESCE(NULLIF(TRIM(u.full_name), ''), u.username) AS name,
                       COALESCE(t.team_code, '')
                FROM users u
                JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                LEFT JOIN teams t ON t.id = u.team_id
                WHERE u.status = 'active'
            """
            _params = []
            if not _is_full_view(role):
                if role == "leader" and my_team:
                    _sql += " AND COALESCE(t.team_code, '') = %s"
                    _params.append(my_team)
                else:
                    _sql += " AND u.username = %s"
                    _params.append(username)
            _sql += " ORDER BY 2"
            cur.execute(_sql, _params or None)
            staff_options = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("page_test: load staff options error: %s", e)
        staff_options = sorted(
            {(r["owner_username"], r["owner_name"], r["team_code"]) for r in rows if r["owner_username"]},
            key=lambda x: x[1])
    if f_team:
        rows = [r for r in rows if r["team_code"] == f_team]
    if f_staff:
        rows = [r for r in rows if r["owner_username"] == f_staff]
    if f_q:
        rows = [r for r in rows if f_q in (r["page_name"] or "").lower()
                or f_q in (r["product_name"] or "").lower()
                or f_q in (r["tk_names"] or "").lower()
                or f_q in (r["owner_name"] or "").lower()
                or f_q in (r["owner_username"] or "").lower()]
    # ── GỘP THEO PAGE: cộng tổng cả kỳ + giữ chi tiết từng ngày (cho popup Chi tiết) ──
    from collections import OrderedDict as _OD
    _pg = _OD()
    for r in rows:
        pid = r["page_id"]
        g = _pg.get(pid)
        if g is None:
            g = {
                "page_id": pid, "page_name": r["page_name"] or pid,
                "tk_names": r["tk_names"],
                "owner_name": r["owner_name"], "owner_username": r["owner_username"],
                "team_code": r["team_code"], "team_name": r["team_name"],
                # danh_gia cấp page suy SAU vòng lặp: 'dat' nếu CÓ ngày nào đánh Đạt.
                "spend_meta": 0.0, "spend_vat": 0.0,
                "don_hang": 0, "so_luong": 0, "doanh_thu": 0.0,
                "_prods": [], "_win_prods": [],
                "_has_dat": False, "_has_khongdat": False, "days": [],
            }
            _pg[pid] = g
        g["spend_meta"] += r["spend_meta"]
        g["spend_vat"]  += r["spend_vat"]
        g["don_hang"]   += int(r["don_hang"] or 0)
        g["so_luong"]   += int(r["so_luong"] or 0)
        g["doanh_thu"]  += r["doanh_thu"]
        for _piece in (r.get("product_name") or r.get("product_suggest") or "").split(","):
            _piece = _piece.strip()
            if _piece and _piece not in g["_prods"]:
                g["_prods"].append(_piece)
        # SP Win = SP của ĐÚNG ngày đánh Đạt (chi phí/đơn/DT vẫn cộng đủ mọi ngày ở trên).
        _rdg = r.get("danh_gia")
        if _rdg == "dat":
            g["_has_dat"] = True
            for _wp in (r.get("product_name") or r.get("product_suggest") or "").split(","):
                _wp = _wp.strip()
                if _wp and _wp not in g["_win_prods"]:
                    g["_win_prods"].append(_wp)
        elif _rdg == "khong_dat":
            g["_has_khongdat"] = True
        g["days"].append({
            "date": r["date_str"],
            "danh_gia": r.get("danh_gia"),
            "spend_meta": round(r["spend_meta"], 0), "spend_vat": round(r["spend_vat"], 0),
            "don_hang": int(r["don_hang"] or 0), "so_luong": int(r["so_luong"] or 0),
            "doanh_thu": round(r["doanh_thu"], 0),
            "product": (r.get("product_name") or r.get("product_suggest") or ""),
            "entry_id": r.get("id"),
            "page": str(pid) + "||" + (r["page_name"] or ""),
            "page_label": r["page_name"] or pid,
            "gia_ban": int(r.get("gia_ban") or 0),
            "dt_custom": (int(r["doanh_thu_custom"]) if r.get("doanh_thu_custom_flag") else ""),
        })
    pages = list(_pg.values())
    for g in pages:
        g["product_label"] = ", ".join(g["_prods"]) or "—"
        # danh_gia cấp page: 'dat' nếu CÓ ngày đánh Đạt; else 'khong_dat' nếu có ngày đánh
        # Không đạt (và không ngày nào Đạt); else chưa đánh (None).
        g["danh_gia"] = ("dat" if g["_has_dat"]
                         else ("khong_dat" if g["_has_khongdat"] else None))
        # SP Win = chỉ SP của (các) ngày bấm Đạt; chưa có thì để trống.
        g["win_product"] = ", ".join(g["_win_prods"])
        g["days"].sort(key=lambda d: d["date"])
        g["has_entry"] = any(d.get("entry_id") for d in g["days"])  # NV đã nhập SP thật
        g.pop("_prods", None)
        g.pop("_win_prods", None)

    # Đếm cho tab — Tất cả = số dòng từng ngày (rows); còn lại = số page gộp
    tab_counts = {
        "all": len(rows),
        "sp": sum(1 for g in pages if g["danh_gia"] == "dat"),
        "test": sum(1 for g in pages if not g["danh_gia"]),
        "dat": sum(1 for g in pages if g["danh_gia"] == "dat"),
        "khongdat": sum(1 for g in pages if g["danh_gia"] == "khong_dat"),
    }
    # Lọc tab
    if f_tab == "sp":              # SP Win = sản phẩm leader đánh ĐẠT
        pages = [g for g in pages if g["danh_gia"] == "dat"]
    elif f_tab == "test":         # Đang test = chưa đánh giá
        pages = [g for g in pages if not g["danh_gia"]]
    elif f_tab == "dat":
        pages = [g for g in pages if g["danh_gia"] == "dat"]
    elif f_tab == "khongdat":
        pages = [g for g in pages if g["danh_gia"] == "khong_dat"]

    # Tổng: tab Tất cả tính theo rows (từng ngày); tab khác theo pages (gộp)
    _src = rows if not f_tab else pages
    totals = {
        "spend_meta": sum(x["spend_meta"] for x in _src),
        "spend_vat": sum(x["spend_vat"] for x in _src),
        "doanh_thu": sum(x["doanh_thu"] for x in _src),
        "don_hang": sum(int(x["don_hang"] or 0) for x in _src),
        "so_luong": sum(int(x["so_luong"] or 0) for x in _src),
    }

    teams_list = []
    try:
        from db import get_conn as _gc_t
        with _gc_t() as conn, conn.cursor() as cur:
            cur.execute("SELECT team_code, COALESCE(team_name,team_code) FROM teams ORDER BY team_code")
            teams_list = [{"code": r[0], "name": r[1]} for r in cur.fetchall() if r[0]]
    except Exception:
        pass

    return render_template(
        "chi_phi_qc/page_test.html",
        rows=rows, pages=pages, totals=totals, tab_counts=tab_counts,
        date_from=date_from, date_to=date_to, today=today,
        f_team=f_team, f_staff=f_staff, f_q=request.args.get("q", ""), f_tab=f_tab,
        staff_options=staff_options,
        teams_list=teams_list, page_options=_pt_page_options(),
        current_role=role, current_username=username,
        is_senior=_is_senior(role),
        # Quyền ĐÁNH GIÁ Đạt/Không đạt: full view đánh hết; leader chỉ team mình; NV chỉ xem.
        pt_eval_all=_is_full_view(role),
        pt_eval_team=(my_team if role == "leader" else ""),
    )


def _pt_can_edit(entry_created_by: str) -> bool:
    role = (session.get("role") or "").lower()
    return _is_senior(role) or (entry_created_by == session.get("username", ""))


def _pt_redirect_back():
    args = {k: v for k, v in request.form.items()
            if k in ("date_from", "date_to", "team", "staff", "q", "tab") and v}
    return redirect(url_for("chi_phi_qc.page_test", **args))


@chi_phi_qc_bp.route("/page-test/add", methods=["POST"])
@login_required
def page_test_add():
    f = request.form
    entry_date = (f.get("entry_date") or "").strip()
    page_raw = (f.get("page") or "").strip()           # "page_id||page_name"
    product = (f.get("product_name") or "").strip()
    if not entry_date or not page_raw or not product:
        flash("Thiếu ngày / page / tên sản phẩm.", "warning")
        return _pt_redirect_back()
    page_id, _, page_name = page_raw.partition("||")

    def _num(name, integer=False):
        raw = (f.get(name) or "").replace(".", "").replace(",", "").strip()
        try:
            return int(raw) if integer else float(raw)
        except (TypeError, ValueError):
            return 0
    dt_custom_raw = (f.get("doanh_thu_custom") or "").replace(".", "").replace(",", "").strip()
    dt_custom = float(dt_custom_raw) if dt_custom_raw else None

    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO page_test_entries
                    (entry_date, page_id, page_name, product_name, gia_ban,
                     don_hang, so_luong, doanh_thu_custom, note, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entry_date, page_id, product_name) DO UPDATE SET
                    gia_ban = EXCLUDED.gia_ban, don_hang = EXCLUDED.don_hang,
                    so_luong = EXCLUDED.so_luong,
                    doanh_thu_custom = EXCLUDED.doanh_thu_custom,
                    note = EXCLUDED.note, updated_at = NOW()
            """, (entry_date, page_id.strip(), page_name.strip(), product,
                  _num("gia_ban"), _num("don_hang", True), _num("so_luong", True),
                  dt_custom, (f.get("note") or "").strip(),
                  session.get("username", "")))
            # Page vừa đưa vào bảng test mà chưa được đánh dấu ngày đó → tự đánh 'ma_test'
            # (ghi chung fb_page_auto_phan_loai — trang CPQC thấy y hệt)
            cur.execute("SELECT 1 FROM fb_page_auto_phan_loai WHERE page_id=%s AND date=%s",
                        (page_id.strip(), entry_date))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO fb_page_auto_phan_loai (page_id, date, phan_loai, updated_by, updated_at)
                    VALUES (%s, %s, 'ma_test', %s, NOW())
                    ON CONFLICT (page_id, date) DO NOTHING
                """, (page_id.strip(), entry_date, str(session.get("user_id", ""))))
            conn.commit()
        flash("Đã lưu dòng test.", "success")
    except Exception as e:
        logger.error("page_test_add error: %s", e)
        flash(f"Lỗi lưu: {e}", "danger")
    return _pt_redirect_back()


@chi_phi_qc_bp.route("/page-test/<int:entry_id>/update", methods=["POST"])
@login_required
def page_test_update(entry_id):
    f = request.form

    def _num(name, integer=False):
        raw = (f.get(name) or "").replace(".", "").replace(",", "").strip()
        try:
            return int(raw) if integer else float(raw)
        except (TypeError, ValueError):
            return 0
    dt_custom_raw = (f.get("doanh_thu_custom") or "").replace(".", "").replace(",", "").strip()
    dt_custom = float(dt_custom_raw) if dt_custom_raw else None
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("SELECT created_by FROM page_test_entries WHERE id=%s", (entry_id,))
            row = cur.fetchone()
            if not row:
                flash("Không tìm thấy dòng.", "warning")
                return _pt_redirect_back()
            if not _pt_can_edit(row[0]):
                flash("Bạn chỉ sửa được dòng do mình tạo.", "warning")
                return _pt_redirect_back()
            cur.execute("""
                UPDATE page_test_entries SET
                    product_name = %s, gia_ban = %s, don_hang = %s, so_luong = %s,
                    doanh_thu_custom = %s, note = %s, updated_at = NOW()
                WHERE id = %s
            """, ((f.get("product_name") or "").strip(), _num("gia_ban"),
                  _num("don_hang", True), _num("so_luong", True),
                  dt_custom, (f.get("note") or "").strip(), entry_id))
            conn.commit()
        flash("Đã cập nhật.", "success")
    except Exception as e:
        logger.error("page_test_update error: %s", e)
        flash(f"Lỗi cập nhật: {e}", "danger")
    return _pt_redirect_back()


@chi_phi_qc_bp.route("/page-test/<int:entry_id>/delete", methods=["POST"])
@login_required
def page_test_delete(entry_id):
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("SELECT created_by, page_id, entry_date FROM page_test_entries WHERE id=%s", (entry_id,))
            row = cur.fetchone()
            if not row:
                flash("Không tìm thấy dòng.", "warning")
                return _pt_redirect_back()
            if not _pt_can_edit(row[0]):
                flash("Bạn chỉ xóa được dòng do mình tạo.", "warning")
                return _pt_redirect_back()
            _pid = row[1]; _edate = row[2]
            cur.execute("DELETE FROM page_test_entries WHERE id=%s", (entry_id,))
            # Reset đánh giá Đạt/Không đạt của ĐÚNG (page, ngày) đó về "chưa đánh".
            if _pid and _edate:
                cur.execute("DELETE FROM page_test_eval WHERE page_id=%s AND entry_date=%s",
                            (str(_pid), _edate))
            conn.commit()
        flash("Đã reset dòng — giá/đơn/SL về 0, đánh giá về chưa đánh.", "success")
    except Exception as e:
        logger.error("page_test_delete error: %s", e)
        flash(f"Lỗi xóa: {e}", "danger")
    return _pt_redirect_back()


@chi_phi_qc_bp.route("/page-test/eval", methods=["POST"])
@login_required
def page_test_eval_save():
    """Lưu đánh giá Đạt/Không đạt theo page. Chỉ là nhãn (không đổi logic Win/Test).

    Quyền: full view (admin/IT/kế toán/...) đánh mọi page; leader chỉ đánh team mình;
    nhân viên thường chỉ xem, không đánh.
    """
    role = (session.get("role") or "").lower()
    data = request.get_json(silent=True) or {}
    page_id = str(data.get("page_id") or "").strip()
    danh_gia = (data.get("danh_gia") or "").strip()
    # Ngày của ĐÚNG dòng bấm đánh giá — đánh theo từng (page, ngày).
    entry_date = (str(data.get("date") or "").strip())
    if not page_id or not entry_date or danh_gia not in ("dat", "khong_dat", ""):
        return jsonify({"ok": False, "error": "Tham số không hợp lệ"}), 400
    if not _is_full_view(role):
        if role != "leader":
            return jsonify({"ok": False, "error": "Bạn chỉ được xem, không có quyền đánh giá"}), 403
        my_team = _pt_current_user_team()
        page_team = _pt_page_team_code(page_id)
        if not my_team or page_team != my_team:
            return jsonify({"ok": False, "error": "Leader chỉ đánh giá page của team mình"}), 403
    val = danh_gia or None   # "" → bỏ đánh giá
    try:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            if val is None:
                # Bỏ đánh giá (bấm lại) → xoá hẳn dòng (page, ngày) đó.
                cur.execute("DELETE FROM page_test_eval WHERE page_id=%s AND entry_date=%s::date",
                            (page_id, entry_date))
            else:
                cur.execute("""
                    INSERT INTO page_test_eval (page_id, entry_date, danh_gia, updated_by, updated_at)
                    VALUES (%s, %s::date, %s, %s, NOW())
                    ON CONFLICT (page_id, entry_date) DO UPDATE
                       SET danh_gia = EXCLUDED.danh_gia,
                           updated_by = EXCLUDED.updated_by, updated_at = NOW()
                """, (page_id, entry_date, val, session.get("username", "")))
            conn.commit()
        return jsonify({"ok": True, "danh_gia": val})
    except Exception as e:
        logger.error("page_test_eval_save error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def register_chi_phi_qc_module(app):
    app.register_blueprint(chi_phi_qc_bp)
    logger.info("chi_phi_qc module registered at /chi-phi-qc/")


# ============================================================
#  TRANG QUẢN LÝ NGƯỜI NHẬN BÁO CÁO LAN  (/chi-phi-qc/lan-bao-cao)
#  Sếp xem 1 trang là biết: nhóm nào nhận, sếp nào nhận, NV nào đã nối Zalo
# ============================================================
_LAN_ADMIN_ROLES = {"admin", "manager", "it_staff"}


def _lan_bridge_get(path: str, timeout: int = 8):
    """GET tới bridge Zalo. Trả (ok, data|error)."""
    import os as _os, requests as _rq
    secret = (_os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        return False, "chưa cấu hình ZALO_BRIDGE_SECRET"
    base = _os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5071/send").replace("/send", "")
    try:
        r = _rq.get(base + path, headers={"X-Bridge-Secret": secret}, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, r.json()
    except Exception as exc:
        return False, str(exc)


def _lan_cfg_set(key: str) -> set:
    from app_ctx import load_config
    return {t.strip() for t in str((load_config() or {}).get(key) or "").split(",") if t.strip()}


def _lan_cfg_save(key: str, values) -> None:
    from app_ctx import save_config_key
    save_config_key(key, ",".join(sorted({str(v).strip() for v in values if str(v).strip()})))


@chi_phi_qc_bp.route("/lan-bao-cao")
@login_required
def lan_bao_cao():
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    from db import get_conn as _gc

    group_tids = _lan_cfg_set("lan_ads_report_threads")
    sep_uids = _lan_cfg_set("lan_ads_report_users")

    # ── Bridge còn sống / đã đăng nhập chưa
    ok_g, gdata = _lan_bridge_get("/list-groups")
    bridge_ok = bool(ok_g)
    bridge_err = "" if ok_g else str(gdata)
    groups = []
    if ok_g:
        for g in (gdata or {}).get("groups", []):
            groups.append({"thread_id": str(g.get("thread_id")), "name": g.get("name") or "(không tên)",
                           "member_count": int(g.get("member_count") or 0),
                           "on": str(g.get("thread_id")) in group_tids})
    seen = {g["thread_id"] for g in groups}
    for t in group_tids:
        if t not in seen:
            groups.append({"thread_id": t, "name": "(nhóm đã bật — Lan chưa quét thấy)",
                           "member_count": 0, "on": True})
    groups.sort(key=lambda x: (not x["on"], x["name"]))

    nvs, pending, mapped_by_uid = [], [], {}
    with _gc() as conn, conn.cursor() as cur:
        # NV đang giữ TK QC = người cần nhận báo cáo riêng
        cur.execute("""
            SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS ten, u.username,
                   COALESCE(u.zalo_uid,'') AS zuid, COALESCE(t.team_name, t.team_code, '') AS team,
                   COUNT(DISTINCT m.ad_account_id) AS so_tk
              FROM users u
              JOIN user_ad_account_assignments m ON m.user_id = u.id AND m.assigned_to IS NULL
              LEFT JOIN teams t ON t.id = u.team_id
             WHERE COALESCE(u.status,'active') = 'active'
             GROUP BY u.id, ten, u.username, zuid, team
             ORDER BY team, ten
        """)
        for r in cur.fetchall():
            nvs.append({"id": int(r[0]), "ten": r[1], "username": r[2], "zalo_uid": r[3],
                        "team": r[4], "so_tk": int(r[5] or 0)})
        cur.execute("""SELECT id, COALESCE(NULLIF(full_name,''), username), COALESCE(zalo_uid,'')
                         FROM users WHERE COALESCE(zalo_uid,'') <> ''""")
        for r in cur.fetchall():
            mapped_by_uid[r[2]] = {"id": int(r[0]), "ten": r[1]}
        # Mọi user còn hoạt động — để nối Zalo cả người KHÔNG giữ TK QC (sale, quản lý…)
        cur.execute("""
            SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username), u.username,
                   COALESCE(t.team_name, t.team_code, '')
              FROM users u LEFT JOIN teams t ON t.id = u.team_id
             WHERE COALESCE(u.status,'active')='active'
             ORDER BY COALESCE(t.team_name, t.team_code, 'zzz'), 2
        """)
        all_users = [{"id": int(r[0]), "ten": r[1], "username": r[2], "team": r[3]}
                     for r in cur.fetchall()]
        # Ai đã nhắn Lan mà chưa gán vào tài khoản nào
        try:
            cur.execute("""
                SELECT sender_uid, sender_name, message_count, last_seen
                  FROM zalo_pending_senders
                 WHERE sender_uid NOT IN (SELECT zalo_uid FROM users WHERE zalo_uid IS NOT NULL)
                 ORDER BY last_seen DESC LIMIT 100
            """)
            pending = [{"uid": r[0], "ten": r[1] or "(chưa rõ tên)",
                        "count": int(r[2] or 0), "last_seen": r[3]} for r in cur.fetchall()]
        except Exception:
            pending = []

    # ── Danh bạ Zalo của Lan (bạn bè) → cho phép CHỌN THEO TÊN, khỏi phải đi tìm mã uid
    danhba, danhba_err = [], ""
    ok_p, pdata = _lan_bridge_get("/people", timeout=15)
    if ok_p:
        for p in (pdata or {}).get("people", []):
            if p.get("uid"):
                danhba.append({"uid": str(p["uid"]), "ten": (p.get("name") or "").strip() or "(chưa rõ tên)"})
        danhba.sort(key=lambda x: x["ten"].lower())
    else:
        danhba_err = str(pdata)
    danhba_ten = {d["uid"]: d["ten"] for d in danhba}

    # Nhóm nào nhận báo cáo của TEAM nào + danh sách team để chọn
    from modules.chi_phi_qc.lan_ads_report import _team_report_map
    team_map = _team_report_map()
    with _gc() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT COALESCE(t.team_name, t.team_code)
                         FROM teams t WHERE COALESCE(t.team_name, t.team_code) <> ''
                        ORDER BY 1""")
        team_list = [r[0] for r in cur.fetchall()]
    for g in groups:
        g["team"] = team_map.get(g["thread_id"], "")

    nv_by_uid = {n["zalo_uid"]: n for n in nvs if n["zalo_uid"]}
    # Gắn trạng thái cho từng người trong danh bạ: đã nối ai chưa, có nhận báo cáo tổng không
    for d in danhba:
        info = mapped_by_uid.get(d["uid"])
        d["noi_voi"] = (info or {}).get("ten") or ""
        d["nhan_tong"] = d["uid"] in sep_uids
    seps = []
    for u in sorted(sep_uids):
        info = mapped_by_uid.get(u)
        seps.append({
            "uid": u,
            "ten": danhba_ten.get(u) or (info or {}).get("ten") or "(Zalo chưa gán tên)",
            # cảnh báo: uid này cũng là 1 NV → NV sẽ nhận CẢ báo cáo tổng toàn công ty
            "la_nv": (nv_by_uid.get(u) or {}).get("ten") or "",
        })
    for p in pending:
        p["ten"] = p["ten"] or danhba_ten.get(p["uid"], "")

    da_noi = sum(1 for x in nvs if x["zalo_uid"])
    return render_template(
        "chi_phi_qc/lan_bao_cao.html",
        bridge_ok=bridge_ok, bridge_err=bridge_err,
        groups=groups, seps=seps, nvs=nvs, pending=pending,
        danhba=danhba, danhba_err=danhba_err, sep_uids=sep_uids, all_users=all_users,
        team_list=team_list,
        da_noi=da_noi, tong_nv=len(nvs),
        role=(session.get("role") or ""),
    )


@chi_phi_qc_bp.route("/lan-bao-cao/toggle-group", methods=["POST"])
@login_required
def lan_toggle_group():
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    tid = (request.form.get("thread_id") or "").strip()
    on = request.form.get("enable") == "1"
    cur = _lan_cfg_set("lan_ads_report_threads")
    cur.add(tid) if on else cur.discard(tid)
    _lan_cfg_save("lan_ads_report_threads", cur)
    flash("Đã BẬT báo cáo cho nhóm này ✅" if on else "Đã tắt báo cáo nhóm này", "success" if on else "info")
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/toggle-sep", methods=["POST"])
@login_required
def lan_toggle_sep():
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    uid = (request.form.get("zalo_uid") or "").strip()
    on = request.form.get("enable") == "1"
    cur = _lan_cfg_set("lan_ads_report_users")
    cur.add(uid) if on else cur.discard(uid)
    _lan_cfg_save("lan_ads_report_users", cur)
    flash("Đã thêm người nhận báo cáo TỔNG ✅" if on else "Đã bỏ khỏi danh sách nhận báo cáo TỔNG",
          "success" if on else "info")
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/map-nv", methods=["POST"])
@login_required
def lan_map_nv():
    """Gán 1 Zalo (đã nhắn Lan) vào đúng tài khoản nhân viên."""
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    zuid = (request.form.get("zalo_uid") or "").strip()
    uid = (request.form.get("user_id") or "").strip()
    from db import get_conn as _gc
    with _gc() as conn, conn.cursor() as cur:
        if uid and uid != "0" and zuid:
            cur.execute("UPDATE users SET zalo_uid=NULL WHERE zalo_uid=%s AND id<>%s", (zuid, int(uid)))
            cur.execute("UPDATE users SET zalo_uid=%s WHERE id=%s", (zuid, int(uid)))
            try:
                cur.execute("DELETE FROM zalo_pending_senders WHERE sender_uid=%s", (zuid,))
            except Exception:
                pass
            flash("Đã nối Zalo với nhân viên ✅ — tối nay bạn đó sẽ nhận báo cáo riêng", "success")
        elif uid == "0" and zuid:
            try:
                cur.execute("DELETE FROM zalo_pending_senders WHERE sender_uid=%s", (zuid,))
            except Exception:
                pass
            flash("Đã bỏ qua Zalo này", "info")
        conn.commit()
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/unmap-nv", methods=["POST"])
@login_required
def lan_unmap_nv():
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    uid = int(request.form.get("user_id") or 0)
    if uid:
        from db import get_conn as _gc
        with _gc() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET zalo_uid=NULL WHERE id=%s", (uid,))
            conn.commit()
        flash("Đã ngắt Zalo của nhân viên này", "info")
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/gui-thu", methods=["POST"])
@login_required
def lan_gui_thu():
    """Gửi thử ngay để xem báo cáo trông thế nào (không cần đợi 20h)."""
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    from modules.chi_phi_qc import lan_ads_report as _lan
    kind = (request.form.get("kind") or "").strip()
    target = (request.form.get("target") or "").strip()
    day = (date.today() - timedelta(days=1)).isoformat()
    try:
        if kind == "nv":
            uid = int(request.form.get("user_id") or 0)
            name = (request.form.get("name") or "").strip()
            txt = _lan.build_personal_report(uid, name, day)
            ok = _lan.send_long(target, txt, "user")
        else:  # nhóm hoặc sếp → báo cáo TỔNG
            parts = _lan.build_parts(day, day)
            ok = _lan.send_parts(target, parts, "group" if kind == "group" else "user")
        flash("Đã gửi thử ✅ — mở Zalo xem nhé" if ok else
              "Gửi KHÔNG thành công ✖ — Lan chưa đăng nhập Zalo hoặc chưa kết bạn với người này", 
              "success" if ok else "danger")
    except Exception as exc:
        flash(f"Lỗi gửi thử: {exc}", "danger")
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/map-team", methods=["POST"])
@login_required
def lan_map_team():
    """Gán 1 nhóm Zalo cho 1 TEAM (báo cáo riêng của team đó). team rỗng = gỡ."""
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    tid = (request.form.get("thread_id") or "").strip()
    team = (request.form.get("team") or "").strip()
    if not tid:
        flash("Thiếu nhóm", "warning")
        return redirect(url_for("chi_phi_qc.lan_bao_cao"))
    from modules.chi_phi_qc.lan_ads_report import _team_report_map
    from app_ctx import save_config_key
    m = _team_report_map()
    if team:
        m[tid] = team
        flash(f"Nhóm này giờ nhận báo cáo của {team} ✅", "success")
    else:
        m.pop(tid, None)
        flash("Đã gỡ báo cáo team khỏi nhóm này", "info")
    save_config_key("lan_team_report_threads",
                    ",".join(f"{k}={v}" for k, v in sorted(m.items())))
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))


@chi_phi_qc_bp.route("/lan-bao-cao/gui-thu-team", methods=["POST"])
@login_required
def lan_gui_thu_team():
    if (session.get("role") or "").lower() not in _LAN_ADMIN_ROLES:
        abort(403)
    from modules.chi_phi_qc import lan_ads_report as _lan
    tid = (request.form.get("thread_id") or "").strip()
    team = (request.form.get("team") or "").strip()
    day = (date.today() - timedelta(days=1)).isoformat()
    try:
        ok = _lan.send_long(tid, _lan.build_team_report(team, day), "group")
        flash("Đã gửi thử báo cáo team ✅" if ok else "Gửi KHÔNG thành công ✖",
              "success" if ok else "danger")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("chi_phi_qc.lan_bao_cao"))
