"""
Module Kho Vật Lý — Tích hợp vào Posbot Web App.
Routes: /kho-vat-ly/*
DB: PostgreSQL (cùng pool với Posbot, bảng có prefix wh_)
Auth: Dùng session['logged_in'] của Posbot
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

try:
    from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm

from .wh_db import wh_db as db, init_wh_tables

try:
    from db import get_conn as _main_get_conn  # main Posbot DB pool (bypass _adapt)
    import psycopg2.extras as _p_extras
except Exception:
    _main_get_conn = None
    _p_extras = None


def _load_shop_biz_map() -> dict:
    """Dict lookup 'cty' | 'hkd' — keyed by BOTH shop_key AND shop_name.
    Dùng ở template: shop_biz_map.get(shop.shop_key) hoặc shop_biz_map.get(r.shop_name)."""
    if _main_get_conn is None or _p_extras is None:
        return {}
    try:
        with _main_get_conn() as conn:
            with conn.cursor(cursor_factory=_p_extras.RealDictCursor) as cur:
                cur.execute("SELECT shop_key, shop_name, COALESCE(business_type,'cty') AS bt FROM shops")
                out: dict = {}
                for r in cur.fetchall():
                    bt = (r.get("bt") or "cty").lower()
                    if r.get("shop_key"):
                        out[r["shop_key"]] = bt
                    if r.get("shop_name"):
                        out[r["shop_name"]] = bt
                return out
    except Exception:
        return {}
from app_constants import KHO_VAT_LY_ACCESS_ROLES
from .wh_sync_pos import sync_all_shops, sync_shop_inventory_to_db
from .wh_sync_orders import sync_outbound_for_date_range
from .wh_sync_returns import sync_returns_for_date_range as sync_returns_pos

log = logging.getLogger("kho_vat_ly")

AUTO_SYNC_INTERVAL = int(os.environ.get("AUTO_SYNC_INTERVAL", 20 * 60))          # 20 phút — full sync
PRODUCTS_AUTO_SYNC_INTERVAL = int(os.environ.get("PRODUCTS_SYNC_INTERVAL", 3 * 60 * 60))  # 3 tiếng — sync sản phẩm
FAST_SYNC_INTERVAL = int(os.environ.get("FAST_SYNC_INTERVAL", 10))              # 10 giây — shipped/returns (chờ chuyển hàng; override FAST_SYNC_INTERVAL)
_auto_sync_started = False
_last_auto_sync: dict = {"time": None, "outbound": 0, "returns": 0, "shops": 0}

_returns_sync_progress: dict = {
    "running": False,
    "shop_name": "", "shop_idx": 0, "shop_total": 0,
    "status": None, "page": 0, "fetched": 0,
    "inserted": 0, "date_from": "", "date_to": "",
    "started_at": "", "done_at": "", "errors": [],
}

_outbound_sync_progress: dict = {
    "running": False,
    "shop_name": "", "shop_idx": 0, "shop_total": 0,
    "status_label": "", "fetched": 0,
    "inserted": 0, "updated": 0, "date_from": "", "date_to": "",
    "started_at": "", "done_at": "", "errors": [],
}

_products_sync_progress: dict = {
    "running": False,
    "shop_name": "", "shop_idx": 0, "shop_total": 0,
    "synced": 0, "skipped": 0, "removed": 0,
    "started_at": "", "done_at": "", "errors": [],
}
_PRODUCTS_SYNC_LOCK = threading.Lock()

# ── Redis distributed lock (chống multi-worker race) ──────────────────────────
_REDIS_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

def _redis_lock_acquire(key: str, ttl: int = 1800) -> str | None:
    """SET NX PX — trả về token nếu acquire được, None nếu lock đang bị giữ bởi worker khác."""
    try:
        from redis_cache import _get_redis
        r = _get_redis()
        if r:
            token = str(uuid.uuid4())
            if r.set(key, token, nx=True, ex=ttl):
                return token
            return None
    except Exception:
        pass
    return None   # fallback: báo bận (thà false-positive hơn double-run)

def _redis_lock_release(key: str, token: str | None) -> None:
    if not token:
        return
    try:
        from redis_cache import _get_redis
        r = _get_redis()
        if r:
            r.eval(_REDIS_LOCK_LUA, 1, key, token)
    except Exception:
        pass

# Admin bulk auto-confirm outbound — chạy nền, progress polling
_admin_bulk_confirm_progress: dict = {
    "running": False,
    "total": 0, "done": 0, "auto_adjusted": 0, "errors_count": 0,
    "started_at": "", "done_at": "", "message": "", "errors": [],
}
_ADMIN_BULK_CONFIRM_LOCK = threading.Lock()   # secondary: chặn cùng worker
_BULK_CONFIRM_REDIS_KEY    = "pos:bulk_confirm_lock"
_RETURNS_BULK_CONFIRM_REDIS_KEY = "pos:returns_bulk_confirm_lock"

# Cooldown đồng bộ nhanh POS (chờ ĐVVC + shipped) từ trang Xuất hàng — tránh spam
_OUTBOUND_QUICK_SYNC_COOLDOWN_SEC = 12
_outbound_quick_sync_last: dict = {}

# Admin bulk confirm RETURNS — async, batch nhỏ để tránh deadlock
_returns_bulk_confirm_progress: dict = {
    "running": False,
    "total": 0, "done": 0, "errors_count": 0,
    "started_at": "", "done_at": "", "message": "", "errors": [],
}
_RETURNS_BULK_CONFIRM_LOCK = threading.Lock()   # secondary: chặn cùng worker

bp = Blueprint(
    "kho_vat_ly", __name__,
    template_folder="templates",
    url_prefix="/kho-vat-ly",
)


@bp.app_context_processor
def _inject_perm_helpers():
    """Expose has_perm() to ALL Jinja templates (nav, subnav, buttons)."""
    def has_perm(key: str) -> bool:
        try:
            import perm_utils
            return perm_utils.has_permission(session.get("role", "staff"), key)
        except Exception:
            return False

    # Cảnh báo shop có API key invalid (key != 32 hex chars + status active)
    # → sync POS skip silent → đơn kẹt trạng thái. Banner đỏ trên kho_vat_ly.
    def invalid_apikey_shops():
        role = session.get("role", "staff")
        if role not in _WH_FULL_ROLES:
            return []
        try:
            with db() as conn:
                rows = conn.execute("""
                    SELECT id, shop_name, pos_shop_id, pos_api_key
                    FROM wh_shops
                    WHERE COALESCE(status,'active') = 'active'
                      AND pos_api_key IS NOT NULL AND pos_api_key != ''
                      AND (LENGTH(pos_api_key) != 32
                           OR pos_api_key !~ '^[0-9a-fA-F]+$')
                    ORDER BY shop_name
                """).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    return {"has_perm": has_perm, "invalid_apikey_shops": invalid_apikey_shops}


# ─────────────────────────────────────────
# AUTH GUARD
# ─────────────────────────────────────────

_PUBLIC_ENDPOINTS   = {"kho_vat_ly.api_last_sync"}
_INTERNAL_ENDPOINTS = {"kho_vat_ly.api_full_sync"}
_WH_FULL_ROLES      = {"admin", "superadmin", "manager", "ketoan", "accountant", "kho"}
# Các role được phép thực hiện thao tác ghi trong kho (thêm, xuất, nhận hoàn, kiểm kho)
_WH_WRITE_ROLES     = _WH_FULL_ROLES | {"leader"}


def _deny_by_perm(perm_key: str):
    """Từ chối nếu role hiện tại không có quyền `perm_key` (dùng permissions.json).
    perm_key = 'kvl_xk_noi_bo' | 'kvl_kiem_kho_tao' | 'kvl_san_pham_sua' ..."""
    import perm_utils
    role = session.get("role", "staff")
    if not perm_utils.has_permission(role, perm_key):
        ct = request.content_type or ""
        is_ajax = (request.is_json
                   or ct.startswith("multipart/")
                   or request.headers.get("X-Requested-With") == "XMLHttpRequest")
        if is_ajax:
            return jsonify({"ok": False, "error": "Bạn không có quyền thực hiện thao tác này."}), 403
        flash("Bạn không có quyền truy cập chức năng này. Liên hệ admin để cấp quyền.", "danger")
        return redirect(url_for(".index"))
    return None


def _has_perm(perm_key: str) -> bool:
    """Context helper — có quyền không? (cho template và checks nội bộ)"""
    import perm_utils
    return perm_utils.has_permission(session.get("role", "staff"), perm_key)


def _deny_non_full():
    """Chỉ cho phép role full (admin/manager/kế toán/kho) — chặn leader/staff/IT."""
    role = session.get("role", "staff")
    if role not in _WH_FULL_ROLES:
        ct = request.content_type or ""
        is_ajax = (request.is_json
                   or ct.startswith("multipart/")
                   or request.headers.get("X-Requested-With") == "XMLHttpRequest")
        if is_ajax:
            return jsonify({"ok": False, "error": "Bạn không có quyền thực hiện thao tác này."}), 403
        flash("Bạn không có quyền thực hiện thao tác này.", "danger")
        return redirect(request.referrer or url_for(".index"))
    return None


def _deny_staff():
    """Từ chối nhân viên (staff) thực hiện thao tác ghi. Gọi đầu mỗi route POST nhạy cảm."""
    role = session.get("role", "staff")
    if role not in _WH_WRITE_ROLES:
        ct = request.content_type or ""
        is_ajax = (request.is_json
                   or ct.startswith("multipart/")
                   or request.headers.get("X-Requested-With") == "XMLHttpRequest")
        if is_ajax:
            return jsonify({"ok": False, "error": "Bạn không có quyền thực hiện thao tác này."}), 403
        flash("Bạn không có quyền thực hiện thao tác này.", "danger")
        return redirect(request.referrer or url_for(".index"))
    return None


@bp.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return
    if request.endpoint in _INTERNAL_ENDPOINTS and request.remote_addr in ("127.0.0.1", "::1"):
        return
    if not session.get("logged_in"):
        # AJAX / fetch (multipart, json, XHR) → trả JSON thay vì redirect HTML
        ct = request.content_type or ""
        is_ajax = (request.is_json
                   or ct.startswith("multipart/")
                   or request.headers.get("X-Requested-With") == "XMLHttpRequest")
        if is_ajax:
            return jsonify({"ok": False, "error": "Phiên đăng nhập hết hạn, vui lòng tải lại trang"}), 401
        return redirect(url_for("auth.login", next=request.url))


@bp.before_request
def _require_kho_access_role():
    """Chỉ admin / quản lý / trưởng / kế toán / kho / IT được vào Kho vật lý — staff không."""
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return
    if request.endpoint in _INTERNAL_ENDPOINTS and request.remote_addr in ("127.0.0.1", "::1"):
        return
    if not session.get("logged_in"):
        return
    role = str(session.get("role", "staff")).strip().lower()
    if role in KHO_VAT_LY_ACCESS_ROLES:
        return
    ct = request.content_type or ""
    is_ajax = (request.is_json
               or ct.startswith("multipart/")
               or request.headers.get("X-Requested-With") == "XMLHttpRequest")
    if is_ajax:
        return jsonify({"ok": False, "error": "Bạn không có quyền truy cập Kho vật lý."}), 403
    flash("Bạn không có quyền truy cập Kho vật lý.", "warning")
    return redirect(url_for("dashboard.dashboard"))


# ─────────────────────────────────────────
# ROLE / SHOP PERMISSION HELPERS
# ─────────────────────────────────────────

def _load_wh_users():
    """Load users từ DB (ưu tiên) hoặc users.json (fallback)."""
    try:
        import sys
        _repo = os.path.join(os.path.dirname(__file__), "../..")
        sys.path.insert(0, _repo)
        from user_helpers import load_all_users
        return load_all_users()
    except Exception:
        pass
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "../../users.json")
    try:
        with open(path) as f:
            return _json.load(f)
    except Exception:
        return []


def _get_shop_warehouse_id(conn, shop_name: str):
    """Lấy warehouse_id của shop theo shop_name (shop_key hoặc username).
    Trả về None nếu shop chưa được gán kho.
    """
    row = conn.execute(
        "SELECT warehouse_id FROM wh_shops WHERE shop_key=%s OR shop_name=%s LIMIT 1",
        (shop_name, shop_name)
    ).fetchone()
    if row and row.get("warehouse_id"):
        return int(row["warehouse_id"])
    return None


def _get_shop_warehouse_id_by_id(conn, shop_id: int):
    """Lấy warehouse_id của shop theo shop.id. Fallback về kho chính (1)."""
    row = conn.execute(
        "SELECT warehouse_id FROM wh_shops WHERE id=%s LIMIT 1",
        (shop_id,)
    ).fetchone()
    if row and row.get("warehouse_id"):
        return int(row["warehouse_id"])
    return _DEFAULT_WAREHOUSE_ID  # fallback: Kho Vĩnh Xá


_DEFAULT_WAREHOUSE_ID = 1  # Kho Vĩnh Xá — fallback khi không tìm được kho xuất

import re as _re
_UUID_RE = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.I)

def _prod_display(conn, product_id, fallback_sku=None, fallback_name=None):
    """Trả về tên/mã hiển thị của sản phẩm (từ wh_products), tránh hiện UUID."""
    # Nếu fallback_sku đã là mã thật (không phải UUID) → dùng luôn
    if fallback_sku and not _UUID_RE.match(str(fallback_sku)):
        return fallback_sku
    # Lookup từ wh_products
    if product_id:
        try:
            p = conn.execute(
                "SELECT sku, name FROM wh_products WHERE id=%s", (product_id,)
            ).fetchone()
            if p:
                return p["sku"] or p["name"] or str(product_id)
        except Exception:
            pass
    return fallback_name or str(product_id or "?")


def _derive_return_wh_id(conn, item) -> int:
    """Xác định kho nhận hoàn theo thứ tự ưu tiên:
    1. warehouse_id đã lưu trên bản ghi return
    2. warehouse_id từ đơn xuất hàng (wh_outbound_requests) khớp order_code
    3. warehouse_id của shop đã gán
    4. Fallback: kho mặc định (Kho Vĩnh Xá, id=1)
    """
    # 1. Đã có sẵn trên bản ghi
    wh_id = (item.get("warehouse_id") or None)
    if wh_id:
        return int(wh_id)

    order_code = item.get("order_code") or ""
    # 2. Tra từ đơn xuất hàng tương ứng
    if order_code:
        ob = conn.execute(
            "SELECT warehouse_id, shop_id FROM wh_outbound_requests WHERE order_code=%s LIMIT 1",
            (order_code,)
        ).fetchone()
        if ob:
            if ob.get("warehouse_id"):
                return int(ob["warehouse_id"])
            if ob.get("shop_id"):
                wh_id = _get_shop_warehouse_id_by_id(conn, ob["shop_id"])
                if wh_id:
                    return wh_id

    # 3. Từ shop của bản ghi hoàn
    if item.get("shop_id"):
        wh_id = _get_shop_warehouse_id_by_id(conn, item["shop_id"])
        if wh_id:
            return wh_id
    if item.get("shop_name"):
        wh_id = _get_shop_warehouse_id(conn, item["shop_name"])
        if wh_id:
            return wh_id

    # 4. Fallback kho mặc định
    return _DEFAULT_WAREHOUSE_ID


def _hkd_shop_names_for_team(team_code: str) -> list:
    """HKD shop_names (wh_shops.shop_name) thuộc về team theo wh_shops.team_id."""
    team_code = (team_code or "").strip()
    if not team_code or _main_get_conn is None:
        return []
    try:
        with _main_get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT w.shop_name
                FROM wh_shops w
                JOIN shops s ON s.shop_key = w.shop_key
                WHERE COALESCE(s.business_type,'cty') = 'hkd'
                  AND w.team_id = %s
                  AND COALESCE(w.status,'active') = 'active'
            """, (team_code,))
            return [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def _hkd_shop_names_for_user(user_id) -> list:
    """HKD shop_names được gán cho user qua user_shop_assignments."""
    if user_id is None or _main_get_conn is None:
        return []
    try:
        with _main_get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.shop_name
                FROM shops s
                JOIN user_shop_assignments usa ON usa.shop_id = s.id
                WHERE usa.user_id = %s
                  AND usa.assigned_to IS NULL
                  AND COALESCE(s.business_type,'cty') = 'hkd'
            """, (int(user_id),))
            return [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def _get_allowed_shop_names(user_id, role):
    """Return list of wh_shops.shop_name this user may see, or None (= no restriction).
    - CTY: shop_name == username của owner (namleader, congminhth, …).
    - HKD: shop_name là tên shop riêng (Bảo AN mkt, Danh DN, …); thuộc team qua wh_shops.team_id
      và thuộc cá nhân qua user_shop_assignments (DB main).
    """
    if role in _WH_FULL_ROLES:
        return None   # unrestricted
    users = _load_wh_users()
    uid_map = {str(u.get("id", "")): u for u in users}
    me = uid_map.get(str(user_id), {})
    if not me:
        return []
    my_username = me.get("username", "")
    if role == "leader":
        team_id = str(me.get("team_id", "")).strip()
        if not team_id:
            base = [my_username] if my_username else []
        else:
            base = [
                u.get("username", "") for u in users
                if str(u.get("team_id", "")).strip() == team_id
                and u.get("status", "active") == "active"
                and u.get("username")
            ]
            if not base and my_username:
                base = [my_username]
        # bổ sung HKD shop của cả team + của bản thân leader
        merged = set(base)
        merged.update(_hkd_shop_names_for_team(team_id))
        merged.update(_hkd_shop_names_for_user(user_id))
        return [n for n in merged if n]
    # staff / other: shop CTY của bản thân + HKD được gán cho bản thân
    merged = set([my_username]) if my_username else set()
    merged.update(_hkd_shop_names_for_user(user_id))
    return [n for n in merged if n]


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def fmt_qty(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return "0"


def now_vn():
    return now_hcm().strftime("%Y-%m-%d %H:%M")


def _rt(template, **ctx):
    ctx.setdefault("current_role",     session.get("role",     "staff"))
    ctx.setdefault("current_username", session.get("username", ""))
    return render_template(f"kho_vat_ly/{template}", fmt_qty=fmt_qty, fmt_hcm=fmt_hcm, **ctx)


# ─────────────────────────────────────────
# TRANG CHỦ
# ─────────────────────────────────────────

@bp.route("/")
def index():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        # Lấy danh sách kho vật lý active
        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()

        # Tồn POS theo sản phẩm — lọc theo shop nếu nhân viên bị giới hạn
        if allowed is None:
            pos_map = {}
            for row in conn.execute("""
                SELECT product_id, COALESCE(SUM(qty_pos), 0) as qty_pos
                FROM wh_shop_inventory GROUP BY product_id
            """).fetchall():
                pos_map[row["product_id"]] = row["qty_pos"]
        else:
            pos_map = {}
            for row in conn.execute("""
                SELECT si.product_id, COALESCE(SUM(si.qty_pos), 0) as qty_pos
                FROM wh_shop_inventory si
                JOIN wh_shops sh ON sh.id = si.shop_id
                WHERE sh.shop_name = ANY(%s)
                GROUP BY si.product_id
            """, (allowed,)).fetchall():
                pos_map[row["product_id"]] = row["qty_pos"]

        # Tồn vật lý theo (product_id, warehouse_id) — SUM qua các biến thể
        # (1 sản phẩm có thể có nhiều dòng cùng (product, warehouse) khác variant_key → phải SUM)
        inv_map = {}  # {product_id: {warehouse_id: qty}}
        for row in conn.execute(
            "SELECT product_id, warehouse_id, COALESCE(SUM(qty),0) AS qty "
            "FROM wh_inventory WHERE warehouse_id IS NOT NULL "
            "GROUP BY product_id, warehouse_id"
        ).fetchall():
            inv_map.setdefault(row["product_id"], {})[row["warehouse_id"]] = row["qty"]

        if allowed is None:
            prods = conn.execute(
                "SELECT id, sku, name, unit, category, COALESCE(min_qty, 0) as min_qty FROM wh_products ORDER BY category, name"
            ).fetchall()
        else:
            # Chỉ lấy sản phẩm mà shop của nhân viên thực sự có tồn POS
            prods = conn.execute("""
                SELECT DISTINCT p.id, p.sku, p.name, p.unit, p.category, COALESCE(p.min_qty, 0) as min_qty
                FROM wh_products p
                JOIN wh_shop_inventory si ON si.product_id = p.id
                JOIN wh_shops sh ON sh.id = si.shop_id
                WHERE sh.shop_name = ANY(%s) AND si.qty_pos > 0
                ORDER BY p.category, p.name
            """, (allowed,)).fetchall()

        rows = []
        for p in prods:
            wh_qtys = inv_map.get(p["id"], {})
            total_physical = sum(wh_qtys.values())
            rows.append({
                "id": p["id"], "sku": p["sku"], "name": p["name"],
                "unit": p["unit"], "category": p["category"],
                "qty": total_physical,
                "min_qty": int(p["min_qty"] or 0),
                "qty_pos": pos_map.get(p["id"], 0),
                "wh_qtys": wh_qtys,
            })

        if allowed is None:
            # Đơn chờ ĐVVC lấy (waiting = status 9 — Chờ chuyển hàng, POS status 7 đã tách sang confirmed)
            pending_outbound = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_outbound_requests WHERE pancake_status='waiting') sq"
            ).fetchone() or {}).get("cnt", 0)
            # Tổng xuất kho = đang giao + đã nhận (returned/cancelled đã bị loại tự động)
            outbound_total = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_outbound_requests WHERE pancake_status IN ('shipped','received')) sq"
            ).fetchone() or {}).get("cnt", 0)
            pending_returns = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_return_receipts WHERE status='pending' AND pancake_return_status IN (4,5,9)) sq"
            ).fetchone() or {}).get("cnt", 0)
        else:
            pending_outbound = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_outbound_requests WHERE pancake_status='waiting' AND shop_name = ANY(%s)) sq",
                (allowed,)
            ).fetchone() or {}).get("cnt", 0)
            outbound_total = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_outbound_requests WHERE pancake_status IN ('shipped','received') AND shop_name = ANY(%s)) sq",
                (allowed,)
            ).fetchone() or {}).get("cnt", 0)
            # NOTE: returned/cancelled đã được update pancake_status → tự loại khỏi shipped/received
            pending_returns = (conn.execute(
                "SELECT COUNT(*) as cnt FROM (SELECT DISTINCT shop_id, order_code FROM wh_return_receipts WHERE status='pending' AND pancake_return_status IN (4,5,9) AND shop_name = ANY(%s)) sq",
                (allowed,)
            ).fetchone() or {}).get("cnt", 0)

        # Cảnh báo tồn thấp: dùng min_qty nếu đặt > 0, fallback hardcode 5
        low_stock = [r for r in rows if r["min_qty"] > 0 and (r["qty"] or 0) <= r["min_qty"]]

        today = today_hcm()
        today_in = (conn.execute(
            "SELECT COALESCE(SUM(qty),0) as total FROM wh_stock_movements WHERE type='inbound' AND created_at LIKE %s",
            (today + "%",)
        ).fetchone() or {}).get("total", 0)
        today_out_vl = (conn.execute(
            "SELECT COALESCE(SUM(qty),0) as total FROM wh_stock_movements WHERE type='outbound_confirmed' AND created_at LIKE %s",
            (today + "%",)
        ).fetchone() or {}).get("total", 0)
        today_out_pos = (conn.execute(
            "SELECT COALESCE(SUM(qty),0) as total FROM wh_stock_movements WHERE type='pos_export' AND created_at LIKE %s",
            (today + "%",)
        ).fetchone() or {}).get("total", 0)
        today_out = today_out_vl + today_out_pos
        today_orders_vl = (conn.execute(
            "SELECT COUNT(DISTINCT order_code||'|'||COALESCE(shop_id::text,'')) as cnt FROM wh_outbound_requests WHERE status='confirmed' AND confirmed_at LIKE %s",
            (today + "%",)
        ).fetchone() or {}).get("cnt", 0)

        if allowed is None:
            top_shops_raw = conn.execute("""
                SELECT s.shop_key, s.shop_name,
                       COALESCE(SUM(si.qty_pos),0) as total_pos
                FROM wh_shops s
                LEFT JOIN wh_shop_inventory si ON si.shop_id = s.id
                WHERE s.status='active'
                GROUP BY s.id, s.shop_key, s.shop_name
                ORDER BY total_pos DESC
                LIMIT 12
            """).fetchall()
            shop_count = (conn.execute(
                "SELECT COUNT(*) as c FROM wh_shops WHERE status='active'"
            ).fetchone() or {}).get("c", 0)
        else:
            top_shops_raw = conn.execute("""
                SELECT s.shop_key, s.shop_name,
                       COALESCE(SUM(si.qty_pos),0) as total_pos
                FROM wh_shops s
                LEFT JOIN wh_shop_inventory si ON si.shop_id = s.id
                WHERE s.status='active' AND s.shop_name = ANY(%s)
                GROUP BY s.id, s.shop_key, s.shop_name
                ORDER BY total_pos DESC
                LIMIT 12
            """, (allowed,)).fetchall()
            shop_count = len(allowed)

    total_physical_all = sum((r["qty"] or 0) for r in rows)
    total_sku_count = len(rows)
    with db() as conn:
        if allowed is None:
            _r = conn.execute(
                "SELECT COALESCE(SUM(qty_pos),0) as t FROM wh_shop_inventory"
            ).fetchone()
        else:
            _r = conn.execute(
                """SELECT COALESCE(SUM(si.qty_pos),0) as t
                   FROM wh_shop_inventory si
                   JOIN wh_shops s ON s.id=si.shop_id
                   WHERE s.shop_name=ANY(%s)""", (allowed,)
            ).fetchone()
        total_pos_all = (_r or {}).get("t", 0)

        total_variation_count = (conn.execute(
            "SELECT COUNT(*) as c FROM wh_variation_map"
        ).fetchone() or {}).get("c", 0)

        # POS-side stats: SKU + mẫu mã thực tế trên Pancake (sync gần đây trong 24h)
        # Filter pos_remain_updated_at để loại pvid POS-orphan (POS đã xóa, không sync nữa)
        # Mẫu mã = group (product_id, lower(variant_name)) — multi-shop cùng vname = 1 mẫu mã
        _recent = "(NOW() - INTERVAL '24 hours')::text"
        pos_sku_count = (conn.execute(
            f"SELECT COUNT(DISTINCT product_id) as c FROM wh_variation_map WHERE pos_remain_updated_at > {_recent}"
        ).fetchone() or {}).get("c", 0)
        pos_variant_count = (conn.execute(
            f"SELECT COUNT(*) as c FROM (SELECT DISTINCT product_id, LOWER(TRIM(COALESCE(variant_name,''))) FROM wh_variation_map WHERE pos_remain_updated_at > {_recent}) t"
        ).fetchone() or {}).get("c", 0)

        # P1-1: đếm pos_export thất bại (pushed_at IS NULL, trong 30 ngày gần nhất)
        from datetime import timedelta as _td
        _since_30d = (now_hcm() - _td(days=30)).strftime("%Y-%m-%d")
        pos_export_failed = (conn.execute(
            """SELECT COUNT(*) as c FROM wh_stock_movements
               WHERE type = 'pos_export'
                 AND (pushed_at IS NULL OR pushed_at = '')
                 AND status = 'active'
                 AND created_at >= %s""",
            (_since_30d,)
        ).fetchone() or {}).get("c", 0)

    return _rt("index.html", rows=rows, low_stock=low_stock,
               pending_outbound=pending_outbound,
               outbound_total=outbound_total,
               pending_returns=pending_returns,
               today_in=today_in, today_out=today_out,
               today_out_vl=today_out_vl,
               today_out_pos=today_out_pos,
               today_orders_vl=today_orders_vl,
               top_shops=top_shops_raw, shop_count=shop_count,
               shop_biz_map=_load_shop_biz_map(),
               total_pos_all=total_pos_all,
               total_physical_all=total_physical_all,
               total_sku_count=total_sku_count,
               total_variation_count=total_variation_count,
               pos_sku_count=pos_sku_count,
               pos_variant_count=pos_variant_count,
               warehouses=warehouses,
               pos_export_failed=pos_export_failed)


# ─────────────────────────────────────────
# NHẬP HÀNG
# ─────────────────────────────────────────

@bp.route("/inbound")
def inbound_list():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)  # None = toàn bộ; list = giới hạn
    can_write = role in _WH_WRITE_ROLES  # False với staff

    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    product_id_f = request.args.get("product_id", "").strip()
    sku_q = request.args.get("sku_q", "").strip().upper()  # Tìm phiếu theo SKU
    warehouse = request.args.get("warehouse", "vat_ly")
    push_filter = request.args.get("push_filter", "all")  # all / not_pushed / pushed
    page = max(1, int(request.args.get("page", 1)))
    per_page = 100

    with db() as conn:
        if allowed is None:
            products = conn.execute(
                "SELECT id, sku, name, unit, category FROM wh_products ORDER BY category, name"
            ).fetchall()
        else:
            products = conn.execute("""
                SELECT DISTINCT p.id, p.sku, p.name, p.unit, p.category
                FROM wh_products p
                JOIN wh_shop_inventory si ON si.product_id = p.id
                JOIN wh_shops sh ON sh.id = si.shop_id
                WHERE sh.shop_name = ANY(%s)
                ORDER BY p.category, p.name
            """, (allowed,)).fetchall()
        inventory = {row["product_id"]: row["qty"] for row in
                     conn.execute("SELECT product_id, SUM(qty) as qty FROM wh_inventory GROUP BY product_id").fetchall()}
        # Sản phẩm có biến thể (>1 UUID) → hiện badge trong modal nhập hàng
        products_with_variants = {row["product_id"] for row in
                                   conn.execute("""
                                     SELECT product_id FROM wh_variation_map
                                     GROUP BY product_id HAVING COUNT(*) > 1
                                   """).fetchall()}
        movements = []
        total_rows = 0
        pos_rows = []
        count_not_pushed = 0
        count_pushed = 0

        warehouses_list = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()

        show_cancelled = request.args.get("show_cancelled", "0") == "1"
        if warehouse == "vat_ly":
            base_where = ["m.type = 'inbound'"]
            base_params: list = []
            # Mặc định ẩn phiếu đã huỷ
            if not show_cancelled:
                base_where.append("(m.status IS NULL OR m.status != 'cancelled')")
            # Nhân viên chỉ thấy lịch sử nhập của shop mình
            if allowed is not None:
                base_where.append(
                    "m.shop_id IN (SELECT id FROM wh_shops WHERE shop_name = ANY(%s))"
                )
                base_params.append(allowed)
            if date_from:
                base_where.append("m.created_at >= %s"); base_params.append(date_from)
            if date_to:
                dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
                base_where.append("m.created_at < %s"); base_params.append(dt_end.strftime("%Y-%m-%d"))
            if product_id_f:
                base_where.append("m.product_id = %s"); base_params.append(product_id_f)
            if sku_q:
                # JOIN với wh_products để filter theo SKU text (ILIKE partial)
                base_where.append(
                    "m.product_id IN (SELECT id FROM wh_products WHERE UPPER(sku) LIKE %s)"
                )
                base_params.append(f"%{sku_q}%")
            # Đếm tab
            base_sql = "FROM wh_stock_movements m WHERE " + " AND ".join(base_where)
            count_not_pushed = (conn.execute(
                f"SELECT COUNT(*) as c {base_sql} AND (m.pushed_at IS NULL OR m.pushed_at='')", base_params
            ).fetchone() or {}).get("c", 0)
            count_pushed = (conn.execute(
                f"SELECT COUNT(*) as c {base_sql} AND m.pushed_at IS NOT NULL AND m.pushed_at!=''", base_params
            ).fetchone() or {}).get("c", 0)
            # Đếm phiếu huỷ (luôn tính không kể filter)
            count_cancelled = (conn.execute(
                "SELECT COUNT(*) as c FROM wh_stock_movements m WHERE m.type='inbound' AND m.status='cancelled'",
                []
            ).fetchone() or {}).get("c", 0)
            # Thêm filter push nếu cần
            where = list(base_where)
            params = list(base_params)
            if push_filter == "not_pushed":
                where.append("(m.pushed_at IS NULL OR m.pushed_at='')")
            elif push_filter == "pushed":
                where.append("m.pushed_at IS NOT NULL AND m.pushed_at!=''")
            where_sql = "WHERE " + " AND ".join(where)
            total_rows = (conn.execute(
                f"SELECT COUNT(*) as c FROM wh_stock_movements m {where_sql}", params
            ).fetchone() or {}).get("c", 0)
            movements = conn.execute(f"""
                SELECT m.*, p.sku, p.name as product_name, p.unit,
                       w.name as warehouse_name,
                       vm.variant_name,
                       COALESCE(
                           vm.pos_remain_qty,
                           (SELECT SUM(qty_pos) FROM wh_shop_inventory
                            WHERE product_id = p.id)
                       ) AS qty_pos
                FROM wh_stock_movements m
                JOIN wh_products p ON p.id = m.product_id
                LEFT JOIN wh_warehouses w ON w.id = m.warehouse_id
                LEFT JOIN wh_variation_map vm ON vm.pos_variation_id = m.pos_variation_id
                {where_sql}
                ORDER BY m.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page]).fetchall()
        else:
            where = ["1=1"]
            params = []
            if allowed is not None:
                where.append("sh.shop_name = ANY(%s)"); params.append(allowed)
            if product_id_f:
                where.append("si.product_id = %s"); params.append(product_id_f)
            where_sql = "WHERE " + " AND ".join(where)
            base_from = "FROM wh_shop_inventory si JOIN wh_shops sh ON sh.id = si.shop_id"
            total_rows = (conn.execute(
                f"SELECT COUNT(*) as c {base_from} {where_sql}", params
            ).fetchone() or {}).get("c", 0)
            pos_rows = conn.execute(f"""
                SELECT si.qty_pos, si.synced_at,
                       sh.shop_name, sh.shop_key,
                       p.id as product_id, p.sku, p.name as product_name, p.unit
                {base_from}
                JOIN wh_products p ON p.id = si.product_id
                {where_sql}
                ORDER BY si.synced_at DESC, sh.shop_name
                LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page]).fetchall()

    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    return _rt("inbound.html", shop_biz_map=_load_shop_biz_map(), movements=movements, pos_rows=pos_rows,
               products=products, inventory=inventory,
               warehouses=warehouses_list,
               date_from=date_from, date_to=date_to,
               product_id_f=product_id_f, warehouse=warehouse,
               push_filter=push_filter,
               count_not_pushed=count_not_pushed,
               count_pushed=count_pushed,
               count_cancelled=locals().get("count_cancelled", 0),
               show_cancelled=locals().get("show_cancelled", False),
               page=page, total_pages=total_pages, total_rows=total_rows,
               can_write=can_write,
               products_with_variants=products_with_variants,
               sku_q=sku_q)


def _get_variant_key(conn, pos_variation_id: str) -> str:
    """Tra cứu variant_key (tên canonical) từ UUID. Trả về str hoặc ''."""
    if not pos_variation_id:
        return ""
    row = conn.execute(
        "SELECT variant_name FROM wh_variation_map WHERE pos_variation_id=%s",
        (pos_variation_id.strip(),)
    ).fetchone()
    return (row["variant_name"] if row else "") or ""


def _get_sole_variant(conn, product_id: int):
    """Nếu SP chỉ có đúng 1 biến thể THẬT trong wh_variation_map → trả (pvid, variant_key).
    Dùng để normalize slot tồn: SP đơn / chỉ 1 biến thể phải chia sẻ CÙNG 1 row
    trong wh_inventory, tránh split giữa 'nhập thủ công' (pvid rỗng) và
    'nhập Excel' (pvid có giá trị). Ngược lại trả (None, None).

    Đếm biến thể theo TÊN distinct, KHÔNG đếm dòng: Pancake có thể đổi UUID
    biến thể → 1 biến thể có 2+ dòng cùng variant_name. Đếm dòng làm SP đơn
    bị tưởng đa biến thể → nhập thủ công rơi vào row product-level, quét đơn
    báo thiếu tồn dù còn hàng (INCIDENTS 2026-06-11 PHUONGHCM0011-13).
    """
    rows = conn.execute(
        "SELECT pos_variation_id, variant_name, pos_remain_updated_at FROM wh_variation_map "
        "WHERE product_id=%s",
        (product_id,)
    ).fetchall()
    if not rows:
        return None, None
    named = [r for r in rows if (r["variant_name"] or "").strip()]
    names = {(r["variant_name"] or "").strip() for r in named}
    if len(names) > 1:
        return None, None          # SP đa biến thể thật → không normalize
    if len(rows) > 1 and not names:
        return None, None          # nhiều dòng đều không tên → không đoán được
    # 1 biến thể thật (1 tên, có thể nhiều UUID) → ưu tiên UUID sync gần nhất
    pool = named or rows
    best = max(pool, key=lambda r: (r["pos_remain_updated_at"] or ""))
    return ((best["pos_variation_id"] or "").strip() or None,
            (best["variant_name"] or "").strip() or None)


def _get_variant_inv(conn, product_id: int, warehouse_id, pos_variation_id: str = None):
    """Lấy dòng wh_inventory theo variant_key (tên), không phải UUID.
    Nhiều UUID cùng tên → dùng chung 1 slot tồn kho.
    Trả về (row_or_None, is_variant_level: bool).
    """
    var = (pos_variation_id or "").strip()
    # ── Normalize cho SP có ĐÚNG 1 biến thể (SP đơn) ──
    # Nếu caller không truyền pvid, nhưng SP chỉ có 1 entry trong variation_map,
    # thì ép dùng pvid canonical đó → đảm bảo mọi inbound/outbound/transfer
    # đều đụng CÙNG 1 row tồn, không tách product-level vs variant-level.
    if not var:
        sole_pvid, sole_vkey = _get_sole_variant(conn, product_id)
        if sole_pvid or sole_vkey:
            var = sole_pvid or ""
            if not var and sole_vkey and warehouse_id:
                # variation_map có variant_name nhưng pvid rỗng — vẫn route qua
                # nhánh variant bằng cách query trực tiếp bằng variant_key.
                row = conn.execute(
                    "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key=%s",
                    (product_id, warehouse_id, sole_vkey)
                ).fetchone()
                if row:
                    return row, True
    if var:
        # Lookup tên canonical từ UUID
        vkey = _get_variant_key(conn, var)
        if vkey:
            if warehouse_id:
                row = conn.execute(
                    "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key=%s",
                    (product_id, warehouse_id, vkey)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND variant_key=%s ORDER BY id LIMIT 1",
                    (product_id, vkey)
                ).fetchone()
            if row:
                return row, True
        # UUID chưa đặt tên → thử fallback theo pos_variation_id cũ (legacy)
        if warehouse_id:
            row = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                (product_id, warehouse_id, var)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND pos_variation_id=%s ORDER BY id LIMIT 1",
                (product_id, var)
            ).fetchone()
        if row:
            return row, True
        # ⚠ KHÔNG fallback xuống product-level / variant khác khi đã chỉ định
        # pos_variation_id — nếu không thấy, biến thể này chưa có tồn (qty=0).
        # Fallback cũ gây bug dồn qty giữa các biến thể khi import Excel.
        return None, True
    # KHÔNG thêm cross-product fallback ở đây — chỉ outbound mới cần (xem _get_variant_inv_outbound)
    # Fallback product-level — CHỈ dòng variant_key IS NULL (đối xứng với _upsert_inventory).
    # FIX 2026-06-18 (HIEUHCM0001 chuyển kho đẻ tồn ảo): TRƯỚC đây có fallback
    # "lấy đại 1 dòng bất kỳ" khi không có dòng NULL → trả về 1 biến thể ngẫu nhiên
    # (vd màu cam) trong khi _upsert_inventory LUÔN ghi vào dòng variant_key IS NULL.
    # Đọc 1 dòng (cam) + ghi dòng khác (NULL) → bất đối xứng → chuyển kho dòng
    # "không màu" copy nguyên qty biến thể đó thành tồn ảo. Bỏ fallback: SP đa biến
    # thể + không chỉ định pvid mà chưa có dòng product-level → coi như 0 (return None),
    # KHÔNG vớ dòng biến thể khác. SP đơn đã được _get_sole_variant chuẩn hoá ở trên.
    if warehouse_id:
        row = conn.execute(
            "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key IS NULL",
            (product_id, warehouse_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND variant_key IS NULL ORDER BY id LIMIT 1",
            (product_id,)
        ).fetchone()
    return row, False


def _get_variant_inv_outbound(conn, product_id: int, warehouse_id, pos_variation_id: str = None):
    """Như _get_variant_inv nhưng có thêm cross-product fallback cho outbound.

    Dùng riêng cho xuất kho / auto-confirm. KHÔNG dùng cho nhập kho (inbound).

    Khi SP được TÁCH từ 1 product → nhiều products riêng (vd: "Túi thêu" → "Túi thêu (4 tím)",
    "Túi thêu (5 xanh)"…), variation_map cập nhật về products mới nhưng wh_inventory
    vẫn lưu tồn dưới product_id cũ. Fallback này tìm theo pvid không lọc product_id
    → trả về row của product cũ để deduct đúng chỗ.

    Trigger fallback khi:
    - Không tìm thấy row cho product hiện tại, HOẶC
    - Tìm thấy nhưng qty = 0 (row placeholder rỗng tạo khi tách SP)
    """
    row, is_variant = _get_variant_inv(conn, product_id, warehouse_id, pos_variation_id)
    # Chỉ cần fallback khi pvid được chỉ định
    var = (pos_variation_id or "").strip()
    if not var:
        return row, is_variant
    # Nếu đã có row với qty > 0 → không cần fallback
    if row is not None and row["qty"] > 0:
        return row, is_variant
    # Cross-product fallback: tìm row có qty > 0 theo pvid, không lọc product_id
    if warehouse_id:
        cross = conn.execute(
            "SELECT id, qty FROM wh_inventory WHERE pos_variation_id=%s AND warehouse_id=%s AND qty > 0 ORDER BY qty DESC LIMIT 1",
            (var, warehouse_id)
        ).fetchone()
    else:
        cross = conn.execute(
            "SELECT id, qty FROM wh_inventory WHERE pos_variation_id=%s AND qty > 0 ORDER BY qty DESC LIMIT 1",
            (var,)
        ).fetchone()
    if cross:
        return cross, True   # Row từ product cũ — deduction theo id (xem _upsert_inventory)
    # Pool fallback: biến thể chưa có row tồn riêng → dùng stock chung của product.
    # Xảy ra khi Pancake tách variant thành product riêng rồi ta gộp lại qua variation_map
    # (vd: HIEUHCM0001 có 2 màu, lưu tồn dưới 1 row chung màu cam).
    # Chỉ trigger khi KHÔNG tồn tại row nào (kể cả qty=0) cho pvid/variant_key này.
    if row is None:
        has_own_row = conn.execute(
            "SELECT 1 FROM wh_inventory WHERE product_id=%s AND pos_variation_id=%s LIMIT 1",
            (product_id, var)
        ).fetchone()
        if not has_own_row:
            vkey2 = _get_variant_key(conn, var)
            if vkey2:
                has_own_row = conn.execute(
                    "SELECT 1 FROM wh_inventory WHERE product_id=%s AND variant_key=%s LIMIT 1",
                    (product_id, vkey2)
                ).fetchone()
        if not has_own_row:
            if warehouse_id:
                pool = conn.execute(
                    "SELECT id, qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s AND qty>0 ORDER BY qty DESC LIMIT 1",
                    (product_id, warehouse_id)
                ).fetchone()
            else:
                pool = conn.execute(
                    "SELECT id, qty FROM wh_inventory "
                    "WHERE product_id=%s AND qty>0 ORDER BY qty DESC LIMIT 1",
                    (product_id,)
                ).fetchone()
            if pool:
                return pool, True
    return row, is_variant   # Trả về row gốc (dù qty=0) để caller hiển thị đúng lỗi


def _pick_warehouse_for_outbound(conn, product_id: int, pos_variation_id=None, qty_needed: int = 1):
    """Chọn kho có đủ tồn để xuất. Duyệt theo thứ tự id tăng dần (Kho Vĩnh Xá ưu tiên).
    Returns: (warehouse_id, qty_available)
      - warehouse_id: kho được chọn (có đủ hàng), hoặc _DEFAULT_WAREHOUSE_ID nếu không kho nào đủ
      - qty_available: số lượng thực tế ở kho được chọn (để hiển thị lỗi thiếu tồn)
    """
    warehouses = conn.execute("SELECT id FROM wh_warehouses ORDER BY id").fetchall()
    best_wh, best_qty = _DEFAULT_WAREHOUSE_ID, 0
    for wh in warehouses:
        wh_id = wh["id"]
        # Dùng _get_variant_inv_outbound để có cross-product fallback (SP bị tách)
        inv, _ = _get_variant_inv_outbound(conn, product_id, wh_id, pos_variation_id)
        qty = inv["qty"] if inv else 0
        if qty >= qty_needed:
            return wh_id, qty          # Kho đầu tiên có đủ hàng
        if qty > best_qty:
            best_wh, best_qty = wh_id, qty
    return best_wh, best_qty            # Không kho nào đủ → trả kho có nhiều nhất để hiện lỗi


def _upsert_inventory(conn, product_id: int, warehouse_id, qty_after: int,
                      pos_variation_id: str = None, variant_key: str = None,
                      inventory_row_id: int = None):
    """INSERT hoặc UPDATE dòng wh_inventory theo variant_key (tên canonical).
    Nếu variant_key không truyền vào, tự lookup từ pos_variation_id.
    Trả về qty_before.

    inventory_row_id: nếu truyền vào → cập nhật TRỰC TIẾP theo id (cross-product case:
    tồn lưu dưới product_id cũ sau khi SP bị tách). Bỏ qua product_id/variant_key lookup.
    """
    # ── Fast path: cross-product / direct row update ──────────────────────────────
    if inventory_row_id is not None:
        old = conn.execute(
            "SELECT qty FROM wh_inventory WHERE id=%s", (inventory_row_id,)
        ).fetchone()
        qty_before = old["qty"] if old else 0
        conn.execute(
            "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
            (qty_after, now_vn(), inventory_row_id)
        )
        return qty_before
    var = (pos_variation_id or "").strip() or None
    # Resolve variant_key: ưu tiên tham số trực tiếp, fallback lookup từ UUID
    vkey = (variant_key or "").strip() or None
    if not vkey and var:
        vkey = _get_variant_key(conn, var) or None

    # ── Normalize cho SP có ĐÚNG 1 biến thể (SP đơn) ──
    # Caller không chỉ định pvid/vkey nhưng SP chỉ có 1 entry trong variation_map
    # → ép ghi vào slot canonical, tránh tạo thêm row product-level song song
    # với row variant-level (nguyên nhân tồn bị cộng double).
    if not var and not vkey:
        sole_pvid, sole_vkey = _get_sole_variant(conn, product_id)
        if sole_pvid or sole_vkey:
            var = sole_pvid or var
            vkey = sole_vkey or vkey

    # ── Fallback legacy: UUID có hàng nhưng chưa kịp đặt tên biến thể trong
    # wh_variation_map → lookup row trực tiếp qua pos_variation_id (cùng pattern
    # mà `_get_variant_inv` dùng ở fallback legacy line 696-707).
    # Nếu không có bước này, read sẽ trả về variant row (qty_before đúng) nhưng
    # write sẽ đi xuống nhánh product-level và ghi vào base row → corrupt!
    if not vkey and var:
        if warehouse_id:
            legacy = conn.execute(
                "SELECT id, variant_key FROM wh_inventory "
                "WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                (product_id, warehouse_id, var)
            ).fetchone()
        else:
            legacy = conn.execute(
                "SELECT id, variant_key FROM wh_inventory "
                "WHERE product_id=%s AND pos_variation_id=%s ORDER BY id LIMIT 1",
                (product_id, var)
            ).fetchone()
        if legacy:
            # Nếu row legacy đã có variant_key → dùng nó; nếu chưa, update trực
            # tiếp theo id để không nhảy sang base row.
            if legacy.get("variant_key"):
                vkey = legacy["variant_key"]
            else:
                qty_before = conn.execute(
                    "SELECT qty FROM wh_inventory WHERE id=%s", (legacy["id"],)
                ).fetchone()["qty"]
                conn.execute(
                    "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                    (qty_after, now_vn(), legacy["id"])
                )
                return qty_before

    if vkey:
        # Variant-level: key = (product_id, warehouse_id, variant_key)
        if warehouse_id:
            inv = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key=%s",
                (product_id, warehouse_id, vkey)
            ).fetchone()
        else:
            inv = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND variant_key=%s ORDER BY id LIMIT 1",
                (product_id, vkey)
            ).fetchone()
        qty_before = inv["qty"] if inv else 0
        if inv:
            conn.execute(
                "UPDATE wh_inventory SET qty=%s, updated_at=%s, pos_variation_id=COALESCE(pos_variation_id,%s) WHERE id=%s",
                (qty_after, now_vn(), var, inv["id"])
            )
        else:
            conn.execute(
                "INSERT INTO wh_inventory (product_id, warehouse_id, qty, updated_at, pos_variation_id, variant_key) VALUES (%s,%s,%s,%s,%s,%s)",
                (product_id, warehouse_id, qty_after, now_vn(), var, vkey)
            )
    else:
        # Product-level (variant_key IS NULL)
        if warehouse_id:
            inv = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key IS NULL",
                (product_id, warehouse_id)
            ).fetchone()
        else:
            inv = conn.execute(
                "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND variant_key IS NULL ORDER BY id LIMIT 1",
                (product_id,)
            ).fetchone()
        qty_before = inv["qty"] if inv else 0
        if inv:
            conn.execute(
                "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                (qty_after, now_vn(), inv["id"])
            )
        else:
            conn.execute(
                "INSERT INTO wh_inventory (product_id, warehouse_id, qty, updated_at) VALUES (%s,%s,%s,%s)",
                (product_id, warehouse_id, qty_after, now_vn())
            )
    return qty_before


def _inbound_add(product_id: int, qty: int, note: str, warehouse_id: int = None,
                 pos_variation_id: str = None, batch_id: str = None,
                 batch_source: str = None, created_by: str = None,
                 gia_nhap=None, gia_ban=None) -> dict:
    with db() as conn:
        prod = conn.execute("SELECT sku, name, unit FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            return {"ok": False, "error": "Không tìm thấy sản phẩm."}
        wh_name = ""
        if warehouse_id:
            wh_row = conn.execute("SELECT name FROM wh_warehouses WHERE id=%s", (warehouse_id,)).fetchone()
            wh_name = wh_row["name"] if wh_row else ""
        var = (pos_variation_id or "").strip() or None
        # Lấy tên biến thể (nếu có)
        variant_label = ""
        if var:
            vm_row = conn.execute(
                "SELECT variant_name FROM wh_variation_map WHERE pos_variation_id=%s", (var,)
            ).fetchone()
            variant_label = (vm_row["variant_name"] or "") if vm_row else ""

        # Tìm current inventory (variant hoặc product-level)
        current_inv, _ = _get_variant_inv(conn, product_id, warehouse_id, var)
        qty_before = current_inv["qty"] if current_inv else 0
        qty_after  = qty_before + qty
        qty_before = _upsert_inventory(conn, product_id, warehouse_id, qty_after, var)

        cur_ins = conn.execute("""
            INSERT INTO wh_stock_movements
              (type, product_id, warehouse_id, qty, qty_before, qty_after,
               note, created_at, created_by, pos_variation_id,
               batch_id, batch_source, gia_nhap, gia_ban)
            VALUES ('inbound', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (product_id, warehouse_id, qty, qty_before, qty_after,
              note or "Nhập kho", now_vn(), created_by or "system", var,
              batch_id, batch_source, gia_nhap, gia_ban))
        movement_id = (cur_ins.fetchone() or {}).get("id")
        product_display = prod["name"]
        if variant_label:
            product_display += f" ({variant_label})"
        return {"ok": True, "product_id": product_id, "warehouse_id": warehouse_id, "warehouse_name": wh_name,
                "movement_id": movement_id, "pos_variation_id": var, "variant_name": variant_label,
                "sku": prod["sku"], "product_name": product_display, "unit": prod["unit"],
                "qty_added": qty, "qty_before": qty_before, "qty_after": qty_after}


@bp.route("/inbound/excel-template")
def inbound_excel_template():
    """Tải file Excel mẫu 4 cột: MÃ HÀNG / TÊN SẢN PHẨM / BIẾN THỂ / SỐ LƯỢNG."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from flask import send_file

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "mau_nhap_hang"

    # Header — 4 cột: MÃ HÀNG / TÊN SẢN PHẨM / BIẾN THỂ / SỐ LƯỢNG
    # Giá nhập / giá bán KHÔNG nhập ở kho vật lý — lấy từ POS khi đẩy.
    headers = ["MÃ HÀNG", "TÊN SẢN PHẨM", "BIẾN THỂ", "SỐ LƯỢNG"]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="4CAF50")
    header_font = Font(bold=True, color="FFFFFF", size=12)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Màu riêng cho cột BIẾN THỂ để nổi bật
    var_fill = PatternFill("solid", fgColor="2196F3")

    for col_idx, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = var_fill if col_idx == 3 else header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    # Dữ liệu mẫu — cột BIẾN THỂ để trống nếu SP không có BT.
    sample_rows = [
        ["Nam0003",     "Ví da nam gấp gọn",    "Nâu đậm",  10],
        ["Nam0003",     "Ví da nam gấp gọn",    "Đen",       5],
        ["Anhhy0003",   "Túi thêu",             "Hồng sen",  8],
        ["Cminhhy0001", "Đinh cây móc lốp",     "",         20],
    ]
    for row in sample_rows:
        ws.append(row)
        for col_idx in range(1, 5):
            ws.cell(row=ws.max_row, column=col_idx).border = border

    # Ghi chú hướng dẫn
    note_row = ws.max_row + 2
    ws.cell(row=note_row, column=1,
            value="* BIẾN THỂ: để trống nếu SP không có biến thể. "
                  "Giá nhập / giá bán KHÔNG cần điền ở đây — hệ thống tự lấy từ POS khi đẩy phiếu nhập.")
    ws.cell(row=note_row, column=1).font = Font(italic=True, color="888888", size=9)
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=4)

    # Độ rộng cột
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 14
    ws.row_dimensions[1].height = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="mau_nhap_hang.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/inbound/excel-preview", methods=["POST"])
def inbound_excel_preview():
    """Parse file Excel nhập hàng → trả preview JSON với matched/unmatched rows."""
    denied = _deny_staff()
    if denied: return denied
    import io, difflib, openpyxl

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Chưa chọn file Excel"}), 400
    wh_id = request.form.get("warehouse_id", "").strip()
    wh_id = int(wh_id) if wh_id and wh_id.isdigit() else None

    try:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        ws = wb.active
    except Exception as e:
        return jsonify({"ok": False, "error": f"Không đọc được file: {e}"}), 400

    # Đọc header row để tìm cột SKU / Tên / Số lượng / Ghi chú
    headers = []
    header_row = None
    for row in ws.iter_rows(max_row=5):
        vals = [str(c.value or "").strip().lower() for c in row]
        if any(v for v in vals):
            headers = vals
            header_row = row[0].row
            break
    if not headers:
        return jsonify({"ok": False, "error": "File trống hoặc không đọc được header"}), 400

    def find_col(keywords):
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                return i
        return None

    col_sku  = find_col(["mã hàng", "ma hang", "sku", "mã sp", "ma sp", "ma_sp", "product_id", "id", "mã"])
    col_name = find_col(["tên sản phẩm", "ten san pham", "tên", "ten", "name", "sản phẩm", "san pham"])
    col_var  = find_col(["biến thể", "bien the", "biến", "bien", "bt", "màu", "mau", "loại", "loai", "variant", "variation"])
    col_qty  = find_col(["tồn", "ton", "số lượng", "so luong", "qty", "quantity", "sl", "số lượ", "nhập"])
    col_note = find_col(["ghi chú", "ghi chu", "note", "chú thích"])

    if col_sku is None and col_name is None:
        return jsonify({"ok": False, "error": "Không tìm thấy cột MÃ HÀNG hoặc TÊN SẢN PHẨM"}), 400
    if col_qty is None:
        return jsonify({"ok": False, "error": "Không tìm thấy cột TỒN / Số lượng"}), 400

    import unicodedata

    def _norm(s: str) -> str:
        """Lowercase + bỏ dấu + strip để so sánh fuzzy."""
        s = s.strip().lower()
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return s

    # Tải toàn bộ sản phẩm trong kho
    with db() as conn:
        all_prods = conn.execute(
            "SELECT id, sku, name FROM wh_products"
        ).fetchall()
        # Tải toàn bộ variation_map: product_id → list of {pos_variation_id, variant_name, pos_remain_qty}
        all_vars = conn.execute(
            "SELECT product_id, pos_variation_id, variant_name, pos_remain_qty FROM wh_variation_map"
        ).fetchall()

    sku_map   = {(p["sku"] or "").strip().upper(): p for p in all_prods}
    name_list = [(p["name"] or "").strip().lower() for p in all_prods]

    # Build map: product_id → list variants, ưu tiên uuid có pos_remain_qty > 0
    from collections import defaultdict
    prod_variants: dict = defaultdict(list)
    for v in all_vars:
        prod_variants[v["product_id"]].append(dict(v))

    def _match_variant(product_id: int, raw_var: str):
        """Tìm pos_variation_id khớp nhất với raw_var.
        Trả về (pos_variation_id, matched_name, match_score) hoặc (None, None, 0).
        """
        variants = prod_variants.get(product_id, [])
        if not variants:
            return None, None, 0

        raw_norm = _norm(raw_var) if raw_var else ""

        # Nếu SP chỉ có 1 biến thể → dùng luôn (kể cả tên = SKU)
        if len(variants) == 1:
            v = variants[0]
            return v["pos_variation_id"], v["variant_name"], 100

        best_uuid, best_name, best_score = None, None, 0
        for v in variants:
            vname = v["variant_name"] or ""
            vname_norm = _norm(vname)
            if not raw_norm:
                # Không có tên BT trong Excel → lấy uuid có pos_remain_qty cao nhất
                score = v["pos_remain_qty"] or 0
            elif raw_norm == vname_norm:
                score = 100
            else:
                score = int(difflib.SequenceMatcher(None, raw_norm, vname_norm).ratio() * 99)
            # Ưu tiên uuid có pos_remain_qty > 0 nếu điểm bằng nhau
            if score > best_score or (score == best_score and (v["pos_remain_qty"] or 0) > 0 and best_uuid):
                best_score = score
                best_uuid = v["pos_variation_id"]
                best_name = vname
        return (best_uuid, best_name, best_score) if best_score >= 50 else (None, None, best_score)

    matched = []
    unmatched = []

    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        raw_sku  = str(row[col_sku]).strip() if col_sku is not None and row[col_sku] is not None else ""
        raw_name = str(row[col_name]).strip() if col_name is not None and row[col_name] is not None else ""
        raw_var  = str(row[col_var]).strip() if col_var is not None and row[col_var] is not None else ""
        try:
            raw_qty = int(float(str(row[col_qty]).replace(",", ""))) if row[col_qty] is not None else 0
        except Exception:
            raw_qty = 0
        raw_note = str(row[col_note]).strip() if col_note is not None and row[col_note] is not None else ""

        if raw_qty <= 0:
            unmatched.append({"sku": raw_sku, "name": raw_name, "variant": raw_var,
                               "qty": raw_qty, "reason": "Số lượng = 0"})
            continue

        prod = None
        match_by = ""

        # 1a. Có biến thể: thử SKU-VARIANT ghép (e.g. NAM0003-NAU DAM)
        if raw_sku and raw_var:
            combined = f"{raw_sku.upper()}-{raw_var.upper()}"
            prod = sku_map.get(combined)
            if prod:
                match_by = "SKU+Biến"

        # 1b. Khớp SKU chính xác
        if prod is None and raw_sku:
            prod = sku_map.get(raw_sku.upper())
            if prod:
                match_by = "SKU"

        # 2. Fuzzy tên (kết hợp tên + biến thể nếu có)
        if prod is None:
            search_name = f"{raw_name} ({raw_var})" if raw_var else raw_name
            search_lower = search_name.lower()
            close = difflib.get_close_matches(search_lower, name_list, n=1, cutoff=0.6)
            if not close and raw_var and raw_name:
                close = difflib.get_close_matches(raw_name.lower(), name_list, n=1, cutoff=0.6)
            if close:
                idx = name_list.index(close[0])
                prod = all_prods[idx]
                match_by = f"Tên (~{int(difflib.SequenceMatcher(None, search_lower, close[0]).ratio()*100)}%)"

        if prod:
            # ── Resolve pos_variation_id từ tên biến thể ──
            pos_var_id, matched_var_name, var_score = _match_variant(prod["id"], raw_var)
            var_note = ""
            if raw_var and matched_var_name and _norm(raw_var) != _norm(matched_var_name):
                var_note = f"BT: '{raw_var}' → '{matched_var_name}' ({var_score}%)"
            elif raw_var and not pos_var_id:
                var_note = f"⚠ Không khớp biến thể '{raw_var}'"

            # Danh sách biến thể available của SP để FE hiện dropdown cho sửa tay
            avail = []
            seen_names = set()
            for v in prod_variants.get(prod["id"], []):
                vn = (v.get("variant_name") or "").strip()
                key = vn.lower() or v.get("pos_variation_id")
                if key in seen_names:
                    continue
                seen_names.add(key)
                avail.append({
                    "pos_variation_id": v.get("pos_variation_id"),
                    "variant_name": vn,
                })
            matched.append({
                "product_id": prod["id"],
                "sku": prod["sku"],
                "name": prod["name"],
                "input_sku": raw_sku,
                "input_name": raw_name,
                "input_variant": raw_var,
                "matched_variant": matched_var_name or "",
                "pos_variation_id": pos_var_id or "",
                "var_score": var_score,
                "var_note": var_note,
                "qty": raw_qty,
                "note": raw_note or "Nhập từ Excel",
                "warehouse_id": wh_id,
                "match_by": match_by,
                "available_variants": avail,
            })
        else:
            reason = "Không tìm thấy sản phẩm khớp"
            if raw_var:
                reason += f" — biến thể '{raw_var}' chưa được khai báo trong hệ thống"
            unmatched.append({
                "sku": raw_sku, "name": raw_name, "variant": raw_var,
                "qty": raw_qty, "reason": reason
            })

    return jsonify({
        "ok": True,
        "matched": matched,
        "unmatched": unmatched,
        "col_info": {
            "sku": col_sku, "name": col_name, "qty": col_qty, "note": col_note
        }
    })


@bp.route("/inbound/batch", methods=["POST"])
def inbound_batch():
    denied = _deny_staff()
    if denied: return denied
    payload = request.get_json(silent=True)
    # Hỗ trợ 2 dạng payload: list legacy (cart thủ công) hoặc {source, items}
    if isinstance(payload, dict):
        items = payload.get("items") or []
        source = (payload.get("source") or "").strip() or "manual-cart"
    else:
        items = payload or []
        source = "manual-cart"
    if not items:
        return jsonify({"ok": False, "error": "Danh sách trống"}), 400

    import uuid as _uuid
    batch_id = _uuid.uuid4().hex
    username = session.get("username", "system")

    results = []
    errors = []
    for item in items:
        try:
            pid = int(item.get("product_id") or 0)
            qty = int(item.get("qty") or 0)
            note = (item.get("note") or "Nhập thủ công").strip()
            wh_id = item.get("warehouse_id")
            wh_id = int(wh_id) if wh_id else None
            pos_var_id = (item.get("pos_variation_id") or "").strip() or None
            if pid <= 0 or qty <= 0:
                errors.append(f"product_id={pid} qty={qty}: không hợp lệ"); continue
            # Kho vật lý CHỈ nhập số lượng — giá nhập/giá bán lấy từ POS khi đẩy phiếu.
            r = _inbound_add(pid, qty, note, warehouse_id=wh_id,
                             pos_variation_id=pos_var_id,
                             batch_id=batch_id, batch_source=source,
                             created_by=username)
            if r["ok"]:
                results.append(r)
            else:
                errors.append(r["error"])
        except Exception as e:
            errors.append(str(e))

    return jsonify({"ok": True, "inserted": len(results), "errors": errors,
                    "results": results, "batch_id": batch_id, "source": source})


@bp.route("/inbound/scan", methods=["POST"])
def inbound_scan():
    denied = _deny_staff()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    qty = max(1, int(data.get("qty") or 1))
    confirmed_by = (data.get("confirmed_by") or "").strip()
    base_note = (data.get("note") or "Quét mã vạch nhập kho").strip()
    note = f"{base_note} — NV: {confirmed_by}" if confirmed_by else base_note
    wh_id = data.get("warehouse_id")
    wh_id = int(wh_id) if wh_id else None
    if not code:
        return jsonify({"ok": False, "error": "Mã trống"}), 400
    with db() as conn:
        prod = conn.execute(
            "SELECT id FROM wh_products WHERE UPPER(sku)=%s", (code,)
        ).fetchone()
    if not prod:
        return jsonify({"ok": False, "error": f"Không tìm thấy sản phẩm: {code}"})
    username = session.get("username") or session.get("full_name") or "system"
    result = _inbound_add(prod["id"], qty, note, warehouse_id=wh_id, created_by=username)
    if result.get("ok"):
        result["note"] = note
        result["confirmed_by"] = confirmed_by
    return jsonify(result)


@bp.route("/api/product/<int:product_id>/variants", methods=["GET"])
def api_product_variants(product_id: int):
    """Trả về danh sách biến thể của sản phẩm (cho inbound/opening-stock form).
    Group by variant_key (tên canonical) để gộp các UUID cùng tên.
    """
    wh_id_str = request.args.get("warehouse_id", "").strip()
    wh_id = int(wh_id_str) if wh_id_str.isdigit() else None
    with db() as conn:
        rows = conn.execute("""
            SELECT vm.pos_variation_id, vm.variant_name
            FROM wh_variation_map vm
            WHERE vm.product_id = %s
            ORDER BY vm.variant_name NULLS LAST, vm.pos_variation_id
        """, (product_id,)).fetchall()

        # Group by variant_key (tên) để gộp UUID cùng tên thành 1 dòng
        seen_keys = {}  # variant_key → first uuid
        for r in rows:
            vname = (r["variant_name"] or "").strip()
            vkey = vname or str(r["pos_variation_id"])
            if vkey not in seen_keys:
                seen_keys[vkey] = {"pos_variation_id": r["pos_variation_id"],
                                   "variant_name": vname}

        # Lấy tồn kho theo variant_key
        variants = []
        for idx, (vkey, v) in enumerate(seen_keys.items()):
            display = v["variant_name"] or f"BT {idx+1}"
            # Tìm tồn theo variant_key hoặc pos_variation_id
            if v["variant_name"] and wh_id:
                inv = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) as q FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key=%s",
                    (product_id, wh_id, v["variant_name"])
                ).fetchone()
            elif v["variant_name"]:
                inv = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) as q FROM wh_inventory WHERE product_id=%s AND variant_key=%s",
                    (product_id, v["variant_name"])
                ).fetchone()
            elif wh_id:
                # H6 fix: khi có wh_id nhưng BT chưa có tên — lọc theo kho
                inv = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) as q FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                    (product_id, wh_id, v["pos_variation_id"])
                ).fetchone()
            else:
                inv = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) as q FROM wh_inventory WHERE product_id=%s AND pos_variation_id=%s",
                    (product_id, v["pos_variation_id"])
                ).fetchone()
            qty = inv["q"] if inv else 0
            is_named = bool(v["variant_name"])
            variants.append({
                "pos_variation_id": v["pos_variation_id"],
                "variant_name": display,
                "is_named": is_named,
                "qty": qty,
            })

    return jsonify({"ok": True, "variants": variants, "count": len(variants),
                    "all_named": all(v["is_named"] for v in variants)})


@bp.route("/api/product/<int:product_id>/stock-by-variant", methods=["GET"])
def api_stock_by_variant(product_id: int):
    """T006: Trả về tồn kho theo biến thể cho một sản phẩm."""
    with db() as conn:
        prod = conn.execute("SELECT sku, name, unit FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            return jsonify({"ok": False, "error": "Không tìm thấy"}), 404
        # Variant-level inventory
        var_rows = conn.execute("""
            SELECT wi.pos_variation_id, vm.variant_name,
                   wi.warehouse_id, wh.name as warehouse_name,
                   wi.qty, wi.updated_at
            FROM wh_inventory wi
            JOIN wh_variation_map vm ON vm.pos_variation_id = wi.pos_variation_id
            LEFT JOIN wh_warehouses wh ON wh.id = wi.warehouse_id
            WHERE wi.product_id = %s AND wi.pos_variation_id IS NOT NULL
            ORDER BY vm.variant_name, wh.name
        """, (product_id,)).fetchall()
        # Product-level fallback (no variant)
        prod_rows = conn.execute("""
            SELECT wi.warehouse_id, wh.name as warehouse_name,
                   wi.qty, wi.updated_at
            FROM wh_inventory wi
            LEFT JOIN wh_warehouses wh ON wh.id = wi.warehouse_id
            WHERE wi.product_id = %s AND wi.pos_variation_id IS NULL
            ORDER BY wh.name
        """, (product_id,)).fetchall()
        # Count variants
        variant_count = conn.execute(
            "SELECT COUNT(*) as c FROM wh_variation_map WHERE product_id=%s", (product_id,)
        ).fetchone()
    by_variant = {}
    var_idx = 0
    for r in var_rows:
        var_id = r["pos_variation_id"]
        if var_id not in by_variant:
            var_idx += 1
            by_variant[var_id] = {
                "variant_name": r["variant_name"] or f"BT {var_idx}",
                "pos_variation_id": var_id, "warehouses": [], "total": 0
            }
        by_variant[var_id]["warehouses"].append({
            "warehouse_id": r["warehouse_id"],
            "warehouse_name": r["warehouse_name"] or "—",
            "qty": r["qty"], "updated_at": str(r["updated_at"] or "")
        })
        by_variant[var_id]["total"] += r["qty"]
    product_level = [{"warehouse_id": r["warehouse_id"], "warehouse_name": r["warehouse_name"] or "—",
                       "qty": r["qty"]} for r in prod_rows]
    return jsonify({
        "ok": True, "product_id": product_id,
        "sku": prod["sku"], "name": prod["name"], "unit": prod["unit"],
        "variant_count": (variant_count["c"] if variant_count else 0),
        "by_variant": list(by_variant.values()),
        "product_level": product_level
    })


@bp.route("/bien-the")
def bien_the_list():
    """Trang quản lý biến thể: liệt kê tất cả sản phẩm có biến thể, trạng thái đặt tên, tồn POS."""
    q = (request.args.get("q") or "").strip().lower()
    filter_status = request.args.get("status") or "all"  # all | unnamed | named
    with db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.sku, p.name,
                   COUNT(vm.pos_variation_id) AS n_var,
                   COUNT(CASE WHEN COALESCE(vm.variant_name,'') != '' THEN 1 END) AS n_named,
                   COUNT(CASE WHEN COALESCE(vm.variant_name,'') = '' THEN 1 END) AS n_unnamed,
                   COALESCE(SUM(vm.pos_remain_qty),0) AS total_pos_qty,
                   MAX(vm.pos_remain_updated_at) AS last_sync
            FROM wh_products p
            JOIN wh_variation_map vm ON vm.product_id = p.id
            GROUP BY p.id, p.sku, p.name
            ORDER BY n_unnamed DESC, p.name
        """).fetchall()
    products = []
    for r in rows:
        if q and q not in (r["name"] or "").lower() and q not in (r["sku"] or "").lower():
            continue
        if filter_status == "unnamed" and r["n_unnamed"] == 0:
            continue
        if filter_status == "named" and r["n_unnamed"] > 0:
            continue
        last_sync_short = ""
        if r["last_sync"]:
            try:
                last_sync_short = r["last_sync"][:16]
            except Exception:
                last_sync_short = r["last_sync"]
        products.append({
            "id": r["id"],
            "sku": r["sku"],
            "name": r["name"],
            "n_var": r["n_var"],
            "n_named": r["n_named"],
            "n_unnamed": r["n_unnamed"],
            "total_pos_qty": r["total_pos_qty"],
            "last_sync": last_sync_short,
            "all_named": r["n_unnamed"] == 0,
        })
    total_products = len(products)
    total_unnamed_products = sum(1 for p in products if p["n_unnamed"] > 0)
    return _rt("bien_the_list.html",
               products=products, q=q, filter_status=filter_status,
               total_products=total_products, total_unnamed_products=total_unnamed_products)


@bp.route("/san-pham/<int:product_id>/nhap-bien-the", methods=["GET", "POST"])
def opening_stock_variants(product_id: int):
    """T003: Trang nhập tồn kho đầu kỳ theo biến thể cho sản phẩm có nhiều biến thể."""
    denied = _deny_staff()
    if denied: return denied
    with db() as conn:
        prod = conn.execute("SELECT * FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            flash("Không tìm thấy sản phẩm.", "danger")
            return redirect(url_for(".inbound_list"))
        raw_variants = conn.execute("""
            SELECT vm.pos_variation_id, vm.variant_name,
                   COALESCE(vm.pos_remain_qty, 0) as pos_remain_qty,
                   vm.pos_remain_updated_at,
                   COALESCE(wi.qty, 0) as qty_current
            FROM wh_variation_map vm
            LEFT JOIN wh_inventory wi ON wi.product_id = vm.product_id
                AND (
                    (vm.variant_name IS NOT NULL AND vm.variant_name != '' AND wi.variant_key = vm.variant_name)
                    OR
                    (COALESCE(vm.variant_name,'') = '' AND wi.pos_variation_id = vm.pos_variation_id::text AND wi.variant_key IS NULL)
                )
            WHERE vm.product_id = %s
            ORDER BY vm.variant_name NULLS LAST, vm.pos_variation_id
        """, (product_id,)).fetchall()
        if not raw_variants:
            flash("Sản phẩm này không có biến thể trong hệ thống.", "warning")
            return redirect(url_for(".inbound_list"))
        # Đếm số đơn xuất theo từng UUID (để dễ nhận biết)
        sold_map = {r["pos_variation_id"]: r["cnt"] for r in conn.execute("""
            SELECT pos_variation_id, COUNT(*) as cnt
            FROM wh_outbound_requests
            WHERE pos_variation_id IS NOT NULL AND pos_variation_id != ''
              AND product_id = %s
            GROUP BY pos_variation_id
        """, (product_id,)).fetchall()}
        # Gắn display_name fallback "BT N" nếu variant_name trống
        variants = []
        for idx, r in enumerate(raw_variants):
            variants.append({
                "pos_variation_id": r["pos_variation_id"],
                "variant_name": r["variant_name"] or "",
                "display_name": r["variant_name"] or f"BT {idx+1}",
                "qty_current": r["qty_current"],
                "qty_sold": sold_map.get(r["pos_variation_id"], 0),
                "pos_remain_qty": r["pos_remain_qty"],
                "pos_remain_updated_at": r["pos_remain_updated_at"] or "",
            })
        warehouses = conn.execute("SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY name").fetchall()

        if request.method == "POST":
            wh_id_str = request.form.get("warehouse_id", "").strip()
            wh_id = int(wh_id_str) if wh_id_str.isdigit() else None
            if not wh_id:
                flash("Vui lòng chọn kho.", "danger")
                return redirect(request.url)
            username = session.get("username", "system")
            ts = now_vn()
            saved = 0
            names_saved = 0
            for var in variants:
                var_id = var["pos_variation_id"]
                # Lưu tên biến thể (nếu user điền)
                new_vname = request.form.get(f"vname_{var_id}", "").strip()
                if new_vname:
                    conn.execute("""
                        UPDATE wh_variation_map SET variant_name=%s
                        WHERE pos_variation_id=%s
                    """, (new_vname, var_id))
                    # Cập nhật variant_key trong wh_inventory nếu có row cũ theo UUID này
                    conn.execute("""
                        UPDATE wh_inventory SET variant_key=%s
                        WHERE product_id=%s AND pos_variation_id=%s AND variant_key IS NULL
                    """, (new_vname, product_id, var_id))
                    names_saved += 1
                qty_str = request.form.get(f"qty_{var_id}", "").strip()
                if not qty_str:
                    continue
                try:
                    qty = int(qty_str)
                    if qty < 0:
                        raise ValueError
                except ValueError:
                    continue
                # Dùng tên vừa nhập (hoặc tên cũ) làm variant_key
                vkey = new_vname or var.get("variant_name") or ""
                # Upsert tồn kho theo variant_key (tên), không phải UUID
                # → nhiều UUID cùng tên Màu Đỏ → dùng chung 1 slot
                if vkey:
                    existing = conn.execute(
                        "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND variant_key=%s",
                        (product_id, wh_id, vkey)
                    ).fetchone()
                else:
                    existing = conn.execute(
                        "SELECT id, qty FROM wh_inventory WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                        (product_id, wh_id, var_id)
                    ).fetchone()
                old_qty = existing["qty"] if existing else 0
                if existing:
                    conn.execute(
                        "UPDATE wh_inventory SET qty=%s, updated_at=%s, pos_variation_id=COALESCE(pos_variation_id,%s) WHERE id=%s",
                        (qty, ts, var_id, existing["id"])
                    )
                else:
                    conn.execute("""
                        INSERT INTO wh_inventory (product_id, warehouse_id, qty, updated_at, pos_variation_id, variant_key)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (product_id, wh_id, qty, ts, var_id, vkey or None))
                conn.execute("""
                    INSERT INTO wh_stock_movements
                      (type, product_id, warehouse_id, qty, qty_before, qty_after,
                       note, created_at, pos_variation_id)
                    VALUES ('opening', %s, %s, %s, %s, %s, %s, %s, %s)
                """, (product_id, wh_id, qty, old_qty, qty, f"Nhập tồn đầu kỳ — {username}", ts, var_id))
                saved += 1
            parts = []
            if saved: parts.append(f"tồn kho {saved} biến thể")
            if names_saved: parts.append(f"tên {names_saved} biến thể")
            flash(f"✅ Đã lưu {' và '.join(parts)}." if parts else "Không có thay đổi.", "success" if parts else "info")
            return redirect(url_for(".opening_stock_variants", product_id=product_id))

    return render_template("kho_vat_ly/opening_stock_variants.html",
                           prod=prod, variants=variants, warehouses=warehouses)


@bp.route("/inbound/create", methods=["POST"])
def inbound_create():
    denied = _deny_staff()
    if denied: return denied
    product_id = request.form.get("product_id", "").strip()
    qty_str = request.form.get("qty", "0").strip()
    note = request.form.get("note", "").strip()
    wh_id_str = request.form.get("warehouse_id", "").strip()
    wh_id = int(wh_id_str) if wh_id_str.isdigit() else None
    pos_var_id = request.form.get("pos_variation_id", "").strip() or None
    if not product_id:
        flash("Vui lòng chọn sản phẩm.", "danger")
        return redirect(url_for(".inbound_list"))
    try:
        qty = int(qty_str)
        if qty <= 0: raise ValueError
    except ValueError:
        flash("Số lượng phải là số nguyên dương.", "danger")
        return redirect(url_for(".inbound_list"))
    username = session.get("username") or session.get("full_name") or "system"
    result = _inbound_add(int(product_id), qty, note, warehouse_id=wh_id,
                          pos_variation_id=pos_var_id, created_by=username)
    if result["ok"]:
        wh_info = f" → {result['warehouse_name']}" if result.get("warehouse_name") else ""
        flash(f"✅ Đã nhập {fmt_qty(qty)} × {result['product_name']}{wh_info}.", "success")
    else:
        flash(result["error"], "danger")
    return redirect(url_for(".inbound_list"))


@bp.route("/inbound/<int:movement_id>/edit", methods=["POST"])
def inbound_edit(movement_id: int):
    """Sửa phiếu nhập kho: cập nhật qty và/hoặc ghi chú, tự điều chỉnh tồn kho."""
    denied = _deny_staff()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    new_qty  = data.get("qty")
    new_note = data.get("note", "").strip()

    try:
        new_qty = int(new_qty)
        if new_qty <= 0:
            return jsonify({"ok": False, "error": "Số lượng phải > 0"}), 400
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Số lượng không hợp lệ"}), 400

    with db() as conn:
        mov = conn.execute(
            "SELECT id, type, product_id, warehouse_id, qty, qty_before, status, pos_variation_id FROM wh_stock_movements WHERE id=%s",
            (movement_id,)
        ).fetchone()
        if not mov:
            return jsonify({"ok": False, "error": "Không tìm thấy phiếu nhập"}), 404
        if mov["type"] != "inbound":
            return jsonify({"ok": False, "error": "Chỉ sửa được phiếu nhập kho"}), 400
        if mov["status"] == "cancelled":
            return jsonify({"ok": False, "error": "Phiếu đã huỷ — không sửa được"}), 400

        old_qty   = mov["qty"]
        qty_before = mov["qty_before"]
        delta     = new_qty - old_qty          # chênh lệch cần điều chỉnh tồn
        new_qty_after = qty_before + new_qty

        pid   = mov["product_id"]
        wh_id = mov["warehouse_id"]
        pvid  = mov.get("pos_variation_id")

        # Điều chỉnh tồn kho — lọc đúng biến thể nếu phiếu có pos_variation_id
        inv, _ = _get_variant_inv(conn, pid, wh_id, pvid)
        if inv:
            adjusted = max(0, inv["qty"] + delta)
            conn.execute(
                "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                (adjusted, now_vn(), inv["id"])
            )

        # Cập nhật bản ghi movement
        conn.execute(
            "UPDATE wh_stock_movements SET qty=%s, qty_after=%s, note=%s WHERE id=%s",
            (new_qty, new_qty_after, new_note or mov.get("note", ""), movement_id)
        )

    return jsonify({"ok": True, "msg": f"Đã cập nhật phiếu #{movement_id}: {old_qty} → {new_qty}"})


@bp.route("/inbound/<int:movement_id>/cancel", methods=["POST"])
def inbound_cancel(movement_id: int):
    """Huỷ phiếu nhập kho (soft-delete): đảo ngược tồn kho, đánh dấu status='cancelled'."""
    denied = _deny_staff()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "").strip() or "Huỷ thủ công"
    username = session.get("username", "admin")

    pos_purchase_list = []
    with db() as conn:
        mov = conn.execute(
            "SELECT id, type, product_id, warehouse_id, qty, status, pos_purchase_ids, pos_variation_id FROM wh_stock_movements WHERE id=%s",
            (movement_id,)
        ).fetchone()
        if not mov:
            return jsonify({"ok": False, "error": "Không tìm thấy phiếu nhập"}), 404
        if mov["type"] != "inbound":
            return jsonify({"ok": False, "error": "Chỉ huỷ được phiếu nhập kho"}), 400
        if mov["status"] == "cancelled":
            return jsonify({"ok": False, "error": "Phiếu này đã bị huỷ rồi"}), 400

        # Lấy danh sách POS purchases đã lưu
        try:
            pos_purchase_list = json.loads(mov["pos_purchase_ids"] or "[]") or []
        except Exception:
            pos_purchase_list = []

        # Đảo ngược tồn kho — lọc đúng biến thể
        pid = mov["product_id"]
        qty = mov["qty"]
        wh_id = mov["warehouse_id"]
        pvid = mov.get("pos_variation_id")
        inv, _ = _get_variant_inv(conn, pid, wh_id, pvid)
        if inv:
            new_qty = max(0, inv["qty"] - qty)
            conn.execute(
                "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                (new_qty, now_vn(), inv["id"])
            )

        # Đánh dấu huỷ
        conn.execute(
            "UPDATE wh_stock_movements SET status='cancelled', cancelled_at=%s, cancelled_by=%s, cancel_reason=%s WHERE id=%s",
            (now_vn(), username, reason, movement_id)
        )

    # Huỷ phiếu nhập trên Pancake POS bằng cách set qty=0 trong items
    # (endpoint /cancel không tồn tại, DELETE trả 404 — set qty=0 an toàn nhất)
    import requests as _req
    pos_cancel_results = []
    pos_warn = []

    # Lấy variation_id từ movement's product nếu không có trong pos_purchase_ids
    fallback_var_id = None
    if pos_purchase_list:
        with db() as conn:
            prod_row = conn.execute(
                "SELECT pos_variation_id FROM wh_products WHERE id=(SELECT product_id FROM wh_stock_movements WHERE id=%s)",
                (movement_id,)
            ).fetchone()
            fallback_var_id = str(prod_row["pos_variation_id"]).strip() if prod_row and prod_row.get("pos_variation_id") else None

    for p in pos_purchase_list:
        ps_id     = p.get("pos_shop_id", "")
        api_key   = p.get("api_key", "")
        pur_id    = p.get("purchase_id")
        var_id    = p.get("variation_id") or fallback_var_id
        shop_name = p.get("shop_name", ps_id)
        display_id = p.get("display_id", "")
        if not ps_id or not api_key or not pur_id or not var_id:
            # Không đủ thông tin → yêu cầu huỷ thủ công
            pos_warn.append({"shop": shop_name, "display_id": display_id, "purchase_id": pur_id or ""})
            continue
        try:
            url = f"https://pos.pages.fm/api/v1/shops/{ps_id}/purchases/{pur_id}"
            rc = _req.put(url, params={"api_key": api_key},
                          json={"purchase": {
                              "items": [{"variation_id": var_id, "quantity": 0, "imported_price": 0}]
                          }}, timeout=15)
            if rc.status_code in (200, 201, 204):
                pos_cancel_results.append({"shop": shop_name, "display_id": display_id, "ok": True})
            else:
                # API thất bại → yêu cầu huỷ thủ công
                pos_warn.append({"shop": shop_name, "display_id": display_id, "purchase_id": pur_id,
                                 "err": f"HTTP {rc.status_code}"})
        except Exception as ex:
            pos_warn.append({"shop": shop_name, "display_id": display_id, "purchase_id": pur_id,
                             "err": str(ex)[:100]})

    ok_count = sum(1 for r in pos_cancel_results if r.get("ok"))
    pos_msg = f" | POS: huỷ {ok_count}/{len(pos_purchase_list)} shop" if pos_purchase_list else ""

    return jsonify({
        "ok": True,
        "msg": f"Đã huỷ phiếu #{movement_id}{pos_msg}",
        "pos_cancel_results": pos_cancel_results,
        "pos_warn": pos_warn   # những shop cần huỷ thủ công (nếu API thất bại)
    })


@bp.route("/inbound/<int:movement_id>/delete", methods=["POST"])
def inbound_delete(movement_id: int):
    """Xoá cứng phiếu nhập kho: đảo ngược tồn kho rồi xoá bản ghi."""
    denied = _deny_staff()
    if denied: return denied
    username = session.get("username", "admin")

    with db() as conn:
        mov = conn.execute(
            "SELECT id, type, product_id, warehouse_id, qty, status, pos_variation_id FROM wh_stock_movements WHERE id=%s",
            (movement_id,)
        ).fetchone()
        if not mov:
            return jsonify({"ok": False, "error": "Không tìm thấy phiếu nhập"}), 404
        if mov["type"] != "inbound":
            return jsonify({"ok": False, "error": "Chỉ xoá được phiếu nhập kho"}), 400

        # Đảo ngược tồn kho (chỉ nếu chưa huỷ — huỷ đã trừ rồi) — lọc đúng biến thể
        if mov["status"] != "cancelled":
            pid = mov["product_id"]
            qty = mov["qty"]
            wh_id = mov["warehouse_id"]
            pvid = mov.get("pos_variation_id")
            inv, _ = _get_variant_inv(conn, pid, wh_id, pvid)
            if inv:
                new_qty = max(0, inv["qty"] - qty)
                conn.execute(
                    "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                    (new_qty, now_vn(), inv["id"])
                )

        conn.execute("DELETE FROM wh_stock_movements WHERE id=%s", (movement_id,))
    return jsonify({"ok": True, "msg": f"Đã xoá phiếu #{movement_id}"})


# ══════════════════════════════════════════════════════════════════════
#  Hoàn tác batch nhập kho (Excel / cart thủ công)
# ══════════════════════════════════════════════════════════════════════
@bp.route("/inbound/batches", methods=["GET"])
def inbound_batches_list():
    """Trả về danh sách các batch nhập kho gần đây — dùng để hiển thị nút Hoàn tác.

    Chỉ trả batch có ít nhất 1 movement còn active (status='active').
    """
    denied = _deny_staff()
    if denied: return denied
    limit = max(1, min(50, int(request.args.get("limit") or 20)))
    with db() as conn:
        rows = conn.execute("""
            SELECT batch_id,
                   COALESCE(batch_source,'') AS source,
                   COUNT(*) AS n_rows,
                   SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS n_active,
                   SUM(CASE WHEN status='active' THEN qty ELSE 0 END) AS qty_active,
                   MIN(created_at) AS started_at,
                   MAX(created_at) AS ended_at,
                   MIN(created_by) AS created_by
              FROM wh_stock_movements
             WHERE type='inbound' AND batch_id IS NOT NULL AND batch_id <> ''
             GROUP BY batch_id, COALESCE(batch_source,'')
             HAVING SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) > 0
             ORDER BY MAX(created_at) DESC
             LIMIT %s
        """, (limit,)).fetchall()
    return jsonify({"ok": True, "batches": [dict(r) for r in rows]})


@bp.route("/inbound/excel-undo/<batch_id>", methods=["POST"])
def inbound_excel_undo(batch_id: str):
    """Hoàn tác toàn bộ 1 batch nhập kho (Excel / cart thủ công).

    - Chỉ admin / manager được dùng (theo _WH_WRITE_ROLES hiện tại là _deny_staff).
    - Mỗi row active trong batch: trừ qty khỏi wh_inventory, set status='cancelled'.
    - Idempotent — row đã cancelled thì bỏ qua.
    """
    # Chỉ admin mới hoàn tác cả batch — rủi ro cao hơn huỷ 1 phiếu lẻ
    role = (session.get("role") or "").lower()
    if role != "admin":
        return jsonify({"ok": False, "error": "Chỉ admin mới được hoàn tác cả batch"}), 403
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or "Hoàn tác batch nhập kho"
    username = session.get("username", "admin")

    with db() as conn:
        rows = conn.execute("""
            SELECT id, product_id, warehouse_id, qty, status, pos_variation_id
              FROM wh_stock_movements
             WHERE type='inbound' AND batch_id=%s AND status='active'
             ORDER BY id
        """, (batch_id,)).fetchall()
        if not rows:
            return jsonify({"ok": False, "error": "Batch không tồn tại hoặc đã được hoàn tác"}), 404

        reversed_count = 0
        total_qty = 0
        for mov in rows:
            pid = mov["product_id"]
            qty = mov["qty"]
            wh_id = mov["warehouse_id"]
            pvid = mov.get("pos_variation_id")
            inv, _ = _get_variant_inv(conn, pid, wh_id, pvid)
            if inv:
                new_qty = max(0, inv["qty"] - qty)
                conn.execute(
                    "UPDATE wh_inventory SET qty=%s, updated_at=%s WHERE id=%s",
                    (new_qty, now_vn(), inv["id"])
                )
            conn.execute(
                "UPDATE wh_stock_movements SET status='cancelled', cancelled_at=%s, "
                "cancelled_by=%s, cancel_reason=%s WHERE id=%s",
                (now_vn(), username, reason, mov["id"])
            )
            reversed_count += 1
            total_qty += qty
    return jsonify({"ok": True,
                    "msg": f"Đã hoàn tác batch {batch_id[:8]}… — {reversed_count} phiếu / {total_qty} đơn vị",
                    "reversed": reversed_count, "qty": total_qty})


@bp.route("/inbound/shops-with-api", methods=["GET"])
def inbound_shops_with_api():
    """Trả về danh sách shop có sản phẩm này trên POS.

    Source: bảng local `wh_shop_inventory` (sync POS mỗi 2h, atomic, không cần API call).
    Trước đây poll POS API 86 shop × 8s → kẹt UI. Giờ query DB ~10ms.
    Trade-off: data có thể stale tối đa 2h — chấp nhận được cho UI chọn shop.
    """
    product_id = request.args.get("product_id", "").strip()
    if not product_id or not product_id.isdigit():
        # Không có product_id → trả tất cả shop active có api_key
        with db() as conn:
            shops = conn.execute(
                "SELECT id, shop_name FROM wh_shops "
                "WHERE status='active' AND pos_api_key IS NOT NULL AND pos_api_key != '' "
                "ORDER BY shop_name"
            ).fetchall()
        return jsonify({"shops": [{"id": s["id"], "name": s["shop_name"]} for s in shops], "warning": None})

    pid = int(product_id)
    with db() as conn:
        # Shop có sản phẩm + có api_key (sẵn sàng push)
        ok_shops = conn.execute(
            "SELECT s.id, s.shop_name FROM wh_shop_inventory si "
            "JOIN wh_shops s ON s.id = si.shop_id "
            "WHERE si.product_id = %s "
            "  AND s.status = 'active' "
            "  AND s.pos_api_key IS NOT NULL AND s.pos_api_key != '' "
            "ORDER BY s.shop_name",
            (pid,)
        ).fetchall()
        # Shop có sản phẩm nhưng api_key rỗng/hết hạn → cảnh báo
        bad_key_shops = conn.execute(
            "SELECT s.id, s.shop_name FROM wh_shop_inventory si "
            "JOIN wh_shops s ON s.id = si.shop_id "
            "WHERE si.product_id = %s "
            "  AND s.status = 'active' "
            "  AND (s.pos_api_key IS NULL OR s.pos_api_key = '') "
            "ORDER BY s.shop_name",
            (pid,)
        ).fetchall()

    shops_list = [{"id": s["id"], "name": s["shop_name"]} for s in ok_shops]
    warning = None
    if bad_key_shops and not shops_list:
        warning = f"{len(bad_key_shops)} shop có sản phẩm này nhưng chưa có API key — cần cập nhật key trong Quản lý kho."
        return jsonify({
            "warning": warning,
            "shops": [{"id": s["id"], "name": s["shop_name"], "key_error": True} for s in bad_key_shops],
        })
    if bad_key_shops:
        warning = f"{len(bad_key_shops)} shop khác có sản phẩm nhưng thiếu API key, không hiển thị."
    if not shops_list:
        return jsonify({"warning": "Không tìm thấy shop nào có sản phẩm này. Có thể chưa sync POS — đợi cycle sync kế tiếp.", "shops": []})

    return jsonify({"shops": shops_list, "warning": warning})


@bp.route("/inbound/push-to-pos", methods=["POST"])
def inbound_push_to_pos():
    """Cộng dồn số lượng nhập vào tồn kho POS cho shop được chọn (hoặc tất cả shop thuộc kho nếu không chọn)."""
    denied = _deny_staff()
    if denied: return denied
    import requests as _req
    product_id = request.form.get("product_id", "").strip()
    warehouse_id = request.form.get("warehouse_id", "").strip()
    movement_id = request.form.get("movement_id", "").strip()
    inbound_qty_str = request.form.get("qty", "0").strip()
    target_shop_id = request.form.get("shop_id", "").strip()  # nếu có → chỉ push 1 shop
    pos_note_input = request.form.get("note", "").strip()
    if not product_id or not warehouse_id:
        return jsonify({"ok": False, "error": "Thiếu product_id hoặc warehouse_id"}), 400

    pid = int(product_id)
    wh_id = int(warehouse_id)
    try:
        inbound_qty = int(inbound_qty_str)
    except (ValueError, TypeError):
        inbound_qty = 0

    with db() as conn:
        prod = conn.execute(
            "SELECT sku, name, pos_variation_id, pos_product_id "
            "FROM wh_products WHERE id=%s", (pid,)
        ).fetchone()
        if not prod:
            return jsonify({"ok": False, "error": "Không tìm thấy sản phẩm"}), 404

        if target_shop_id and target_shop_id.isdigit():
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE id=%s AND status='active'", (int(target_shop_id),)
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE warehouse_id=%s AND status='active'", (wh_id,)
            ).fetchall()

    if not shops:
        return jsonify({"ok": False, "error": "Không tìm thấy shop được chọn hoặc kho chưa có shop nào được gán."})

    # Xác định pos_variation_id để đẩy POS — chiến lược đơn giản:
    #   1. UI cung cấp (form_var_id) — ưu tiên cao nhất
    #   2. Phiếu nhập (wh_stock_movements.pos_variation_id) — nếu có movement_id
    #   3. Fallback CLEAN qua wh_variation_map (single source of truth):
    #      - 0 row: lỗi "chưa sync POS"
    #      - 1 row: single-variant → dùng pvid duy nhất
    #      - >1 row: multi-variant → yêu cầu UI gửi pvid (không tự đoán)
    form_var_id = request.form.get("pos_variation_id", "").strip()
    variation_id = form_var_id

    if not variation_id and movement_id and movement_id.isdigit():
        with db() as _c2:
            mov_row = _c2.execute(
                "SELECT pos_variation_id FROM wh_stock_movements WHERE id=%s", (int(movement_id),)
            ).fetchone()
            if mov_row and mov_row.get("pos_variation_id"):
                variation_id = mov_row["pos_variation_id"]

    if not variation_id:
        # Fallback qua vmap — single source of truth
        with db() as _c2b:
            vmap_rows = _c2b.execute(
                "SELECT pos_variation_id FROM wh_variation_map WHERE product_id=%s",
                (pid,)
            ).fetchall()
        if len(vmap_rows) == 0:
            return jsonify({"ok": False, "error": "Sản phẩm chưa sync POS (chưa có biến thể trong wh_variation_map). Chạy sync POS trước."})
        elif len(vmap_rows) == 1:
            variation_id = vmap_rows[0]["pos_variation_id"]  # single-variant case
        else:
            return jsonify({"ok": False, "error": f"Sản phẩm có {len(vmap_rows)} biến thể — vui lòng chọn biến thể trước khi đẩy POS."})

    # pos_product_id: ưu tiên từ wh_products.pos_product_id, fallback qua vmap
    product_id_pos = str(prod.get("pos_product_id") or "").strip()
    if not product_id_pos and variation_id:
        with db() as _c3:
            vm_row = _c3.execute(
                "SELECT p.pos_product_id FROM wh_variation_map vm JOIN wh_products p ON p.id=vm.product_id WHERE vm.pos_variation_id=%s",
                (variation_id,)
            ).fetchone()
            if vm_row:
                product_id_pos = str(vm_row.get("pos_product_id") or "")
    if not product_id_pos:
        return jsonify({"ok": False, "error": "Sản phẩm chưa có pos_product_id. Cần sync POS trước."})

    results = []
    pos_purchase_list = []  # [{pos_shop_id, api_key, purchase_id, display_id}]
    for shop in shops:
        api_key = str(shop.get("pos_api_key") or "").strip()
        pos_shop_id = str(shop.get("pos_shop_id") or "").strip()
        if not api_key or not pos_shop_id:
            results.append({"shop": shop["shop_name"], "ok": False, "msg": "Chưa có API key"})
            continue
        try:
            # GET thông tin product trực tiếp bằng product_id để lấy pos_warehouse_id
            url_prod = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products/{product_id_pos}"
            rp = _req.get(url_prod, params={"api_key": api_key}, timeout=20)
            rp.raise_for_status()
            prod_data = rp.json()
            wh_id_pos = None
            pos_price_in = None   # average_imported_price của variation (giá nhập trên POS)
            pos_price_out = None  # retail_price của variation (giá bán trên POS)
            for src in (prod_data.get("data") or {}, prod_data.get("product") or {}):
                for var in (src.get("variations") or []):
                    if str(var.get("id")) == variation_id:
                        vw_list = var.get("variations_warehouses") or []
                        if vw_list:
                            wh_id_pos = vw_list[0].get("warehouse_id")
                        # Lấy giá hiện tại từ POS để dùng làm imported_price
                        try:
                            v_in = var.get("average_imported_price")
                            if v_in in (None, "", 0): v_in = var.get("price")
                            if v_in is not None:
                                pos_price_in = float(v_in)
                        except Exception:
                            pass
                        try:
                            v_out = var.get("retail_price")
                            if v_out is not None:
                                pos_price_out = float(v_out)
                        except Exception:
                            pass
                        break
                if wh_id_pos or pos_price_in is not None:
                    break
            # Fallback 1: lấy warehouse_id từ bất kỳ variation nào khác của cùng sản phẩm
            if not wh_id_pos:
                for src in (prod_data.get("data") or {}, prod_data.get("product") or {}):
                    for var in (src.get("variations") or []):
                        vw_list = var.get("variations_warehouses") or []
                        if vw_list and vw_list[0].get("warehouse_id"):
                            wh_id_pos = vw_list[0].get("warehouse_id")
                            break
                    if wh_id_pos:
                        break
            # Fallback 2: gọi /warehouses của shop → lấy kho đầu tiên
            if not wh_id_pos:
                try:
                    url_wh = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/warehouses"
                    rw = _req.get(url_wh, params={"api_key": api_key}, timeout=15)
                    if rw.status_code == 200:
                        wh_list = (rw.json() or {}).get("data") or []
                        if wh_list:
                            wh_id_pos = wh_list[0].get("id")
                except Exception:
                    pass
            if not wh_id_pos:
                results.append({"shop": shop["shop_name"], "ok": False,
                                "msg": f"Shop chưa cấu hình kho POS (variation={variation_id[:8]}… không có warehouse, fallback /warehouses cũng rỗng)"})
                continue
            # Tạo phiếu nhập kho trong Pancake POS
            # Dùng endpoint /purchases (theo Pancake OpenAPI docs api-docs.pancake.vn)
            url_purchase = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/purchases"
            # Giá nhập / giá bán LẤY TỪ POS (không nhập ở kho vật lý).
            # imported_price của phiếu nhập POS = giá nhập hiện tại trên POS,
            # để phiếu nhập không bị lưu giá 0.
            purchase_item = {
                "variation_id": variation_id,
                "imported_price": pos_price_in if pos_price_in is not None else 0,
                "quantity": inbound_qty
            }
            purchase_payload = {
                "purchase": {
                    "warehouse_id": wh_id_pos,
                    "note": pos_note_input if pos_note_input else f"Nhập từ kho Tiểu Hiềm — SKU: {prod['sku']}",
                    "status": 1,  # 1 = Mới — cập nhật kho ngay, không crash web UI
                    "items": [purchase_item]
                }
            }
            ri = _req.post(url_purchase, params={"api_key": api_key}, json=purchase_payload, timeout=20)
            resp_json = {}
            try:
                resp_json = ri.json()
            except Exception:
                pass
            purchase_id = (resp_json.get("data") or {}).get("id") if isinstance(resp_json.get("data"), dict) else None
            display_id  = (resp_json.get("data") or {}).get("display_id", "") if isinstance(resp_json.get("data"), dict) else ""
            items_created = len((resp_json.get("data") or {}).get("items") or []) if isinstance(resp_json.get("data"), dict) else 0
            if ri.status_code in (200, 201) and purchase_id:
                if items_created > 0:
                    # Hiển thị giá POS đã dùng (để user biết giá nhập gắn vào purchase là bao nhiêu).
                    parts = []
                    if pos_price_in is not None:
                        parts.append(f"nhập {int(pos_price_in):,}đ".replace(",", "."))
                    if pos_price_out is not None:
                        parts.append(f"bán {int(pos_price_out):,}đ".replace(",", "."))
                    price_sync_note = f" · {' / '.join(parts)} ↓POS" if parts else ""
                    results.append({"shop": shop["shop_name"], "ok": True,
                                    "msg": f"Đã tạo phiếu nhập +{inbound_qty} (POS #{display_id}){price_sync_note}"})
                    pos_purchase_list.append({
                        "pos_shop_id": pos_shop_id,
                        "api_key": api_key,
                        "purchase_id": purchase_id,
                        "display_id": display_id,
                        "shop_name": shop["shop_name"],
                        "variation_id": variation_id   # cần khi huỷ: set qty→0
                    })
                else:
                    # Phiếu tạo nhưng items rỗng — variation_id không khớp
                    results.append({"shop": shop["shop_name"], "ok": False,
                                    "msg": f"Phiếu tạo nhưng items rỗng (variation_id={variation_id[:8]}... không tìm thấy trong kho POS)"})
            else:
                results.append({"shop": shop["shop_name"], "ok": False,
                                "msg": f"HTTP {ri.status_code}: {str(resp_json)[:120]}"})
        except Exception as ex:
            results.append({"shop": shop["shop_name"], "ok": False, "msg": str(ex)})

    ok_count = sum(1 for r in results if r["ok"])
    all_ok = ok_count == len(results) and len(results) > 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if all_ok:
        # Đánh dấu phiếu nhập đã được đẩy POS.
        # Kho vật lý KHÔNG bị trừ — chỉ cộng POS để so sánh chênh lệch.
        # Kho vật lý chỉ giảm khi xuất hàng thực tế (outbound_confirm).
        with db() as conn:
            if movement_id and movement_id.isdigit():
                conn.execute(
                    "UPDATE wh_stock_movements SET pushed_at=%s, pushed_qty=%s, pos_purchase_ids=%s WHERE id=%s",
                    (now_str, inbound_qty, json.dumps(pos_purchase_list, ensure_ascii=False), int(movement_id))
                )
    return jsonify({"ok": True, "inbound_qty": inbound_qty, "results": results,
                    "all_ok": all_ok,
                    "pushed_at": now_str if all_ok else None,
                    "summary": f"Tạo phiếu nhập +{inbound_qty} trên {ok_count}/{len(results)} shop POS"
                                + (" (kho vật lý giữ nguyên)" if all_ok else "")})


# ─────────────────────────────────────────
# XUẤT HÀNG
# ─────────────────────────────────────────

@bp.route("/outbound")
def outbound_list():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)
    status_filter = request.args.get("status", "waiting")
    _today = now_hcm().strftime("%Y-%m-%d")
    date_from = request.args.get("date_from", _today).strip()
    date_to   = request.args.get("date_to",   _today).strip()
    shop_filter = request.args.get("shop", "").strip()
    tracking_search = request.args.get("tracking_search", "").strip()
    staff_filter = request.args.get("staff", "").strip()  # NV đã quét xác nhận xuất kho
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    with db() as conn:
        # ── Tìm kiếm theo mã vận đơn / mã đơn / SKU sản phẩm (cả xuất kho lẫn hoàn) ──
        # Vd nhập "NHATHY0005" → mọi đơn chứa item có SKU này sẽ hiển thị.
        tracking_results = None
        if tracking_search:
            q = f"%{tracking_search}%"
            ob_rows = conn.execute("""
                SELECT order_code, shop_name, MAX(carrier_name) as carrier_name,
                       MAX(tracking_code) as tracking_code,
                       MAX(carrier_picked_up_at) as carrier_picked_up_at,
                       MAX(pancake_status) as pancake_status,
                       COUNT(*) as item_count,
                       SUM(qty_ordered) as total_qty,
                       SUM(CASE WHEN status='pending'   THEN 1 ELSE 0 END) as pending_cnt,
                       SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) as confirmed_cnt,
                       string_agg(DISTINCT product_sku, ', ') FILTER (WHERE product_sku ILIKE %s) AS matched_skus
                FROM wh_outbound_requests
                WHERE tracking_code ILIKE %s
                   OR order_code   ILIKE %s
                   OR product_sku  ILIKE %s
                GROUP BY order_code, shop_name
                ORDER BY MAX(carrier_picked_up_at) DESC NULLS LAST
                LIMIT 50
            """, (q, q, q, q)).fetchall()
            ret_rows = conn.execute("""
                SELECT order_code, MAX(shop_name) as shop_name, MAX(carrier_name) as carrier_name,
                       MAX(tracking_code) as tracking_code,
                       MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) as created_at,
                       COUNT(*) as item_count,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending_cnt,
                       MAX(pancake_return_status) as pancake_return_status,
                       string_agg(DISTINCT product_sku, ', ') FILTER (WHERE product_sku ILIKE %s) AS matched_skus
                FROM wh_return_receipts
                WHERE tracking_code ILIKE %s
                   OR order_code   ILIKE %s
                   OR product_sku  ILIKE %s
                GROUP BY order_code
                ORDER BY MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) DESC NULLS LAST
                LIMIT 50
            """, (q, q, q, q)).fetchall()
            tracking_results = {
                "outbound": [dict(r) for r in ob_rows],
                "returns":  [dict(r) for r in ret_rows],
                "query":    tracking_search,
            }
        # ── Base: shop restriction + date filter theo carrier_picked_up_at ──────────
        shop_parts: list = []
        shop_params: list = []
        if allowed is not None:
            shop_parts.append("shop_name = ANY(%s)"); shop_params.append(allowed)
        if shop_filter:
            shop_parts.append("shop_name = %s"); shop_params.append(shop_filter)
        shop_sql = ("WHERE " + " AND ".join(shop_parts)) if shop_parts else ""

        # Date filter CHỈ dùng carrier_picked_up_at — áp dụng cho shipped/received
        date_parts: list = []
        date_params: list = []
        if date_from:
            date_parts.append("carrier_picked_up_at >= %s"); date_params.append(date_from)
        if date_to:
            dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            date_parts.append("carrier_picked_up_at < %s"); date_params.append(dt_end.strftime("%Y-%m-%d"))

        # where_sql cho shipped/received (áp dụng date filter)
        shipped_parts = shop_parts[:]
        shipped_params = shop_params[:]
        shipped_parts.append("pancake_status IN ('shipped','received')")
        if date_parts:
            shipped_parts.extend(date_parts); shipped_params.extend(date_params)
        shipped_where = "WHERE " + " AND ".join(shipped_parts)

        # where_sql cho waiting (KHÔNG áp dụng date filter) — chỉ đơn CHƯA quét (pending)
        waiting_parts = shop_parts[:] + ["pancake_status='waiting'", "status='pending'"]
        waiting_where = "WHERE " + " AND ".join(waiting_parts)
        waiting_params = shop_params[:]

        # where_sql cho pre_confirmed (kho đã quét, DVVC chưa lấy)
        pre_conf_parts = shop_parts[:] + ["status='pre_confirmed'"]
        pre_conf_where = "WHERE " + " AND ".join(pre_conf_parts)
        pre_conf_params = shop_params[:]

        # ── Filter theo nhân viên đã quét xác nhận xuất kho (pre_confirmed_by) ──
        # Chỉ áp khi status filter ở các tab có ý nghĩa: pre_confirmed / pending / confirmed.
        # Tab waiting (chưa quét) không có nhân viên xác nhận → bỏ qua.
        if staff_filter and status_filter != "waiting":
            staff_clause = "pre_confirmed_by = %s"
            shipped_parts.append(staff_clause); shipped_params.append(staff_filter)
            pre_conf_parts.append(staff_clause); pre_conf_params.append(staff_filter)
            shipped_where = "WHERE " + " AND ".join(shipped_parts)
            pre_conf_where = "WHERE " + " AND ".join(pre_conf_parts)

        # ── Main list query theo status_filter ──────────────────────────────────────
        order_by_col = "MAX(carrier_picked_up_at)"
        if status_filter == "waiting":
            main_where = waiting_where
            main_params = waiting_params[:]
            having_sql = ""
        elif status_filter == "pre_confirmed":
            # Đã chuẩn bị: pre_confirmed + waiting (ĐVVC chưa lấy)
            main_where = pre_conf_where + " AND pancake_status='waiting'"
            main_params = pre_conf_params[:]
            having_sql = ""
            order_by_col = "MAX(pre_confirmed_at)"
        elif status_filter == "pending":
            # Chờ xuất kho: NV đã quét (pre_confirmed) + ĐVVC đã lấy (shipped/received)
            main_where = shipped_where + " AND status='pre_confirmed'"
            main_params = shipped_params[:]
            having_sql = ""
            order_by_col = "MAX(carrier_picked_up_at)"
        elif status_filter == "confirmed":
            # Đã xuất kho: NV đã xác nhận (confirmed)
            main_where = shipped_where + " AND status='confirmed'"
            main_params = shipped_params[:]
            having_sql = ""
        elif status_filter == "lech_pos":
            # Lệch POS: ĐVVC đã lấy trên POS nhưng NV chưa quét kho
            main_where = shipped_where + " AND status='pending'"
            main_params = shipped_params[:]
            having_sql = ""
            order_by_col = "MAX(carrier_picked_up_at)"
        else:
            main_where = shipped_where
            main_params = shipped_params[:]
            having_sql = ""

        base_sql = f"""
            FROM wh_outbound_requests
            {main_where}
            GROUP BY shop_id, order_code
            {having_sql}
        """
        total_orders = (conn.execute(
            f"SELECT COUNT(*) as c FROM (SELECT shop_id, order_code {base_sql}) sq", main_params
        ).fetchone() or {}).get("c", 0)

        # Gợi ý kho có tồn: chỉ tính cho tab waiting + pending (chờ xuất kho)
        # để tránh correlated subquery trên 400K đơn confirmed
        wh_hint_cols = ""
        if status_filter in ("waiting", "pending"):
            wh_hint_cols = """
                   ,BOOL_AND(
                       COALESCE((
                           SELECT SUM(wi.qty) FROM wh_inventory wi
                           WHERE wi.product_id = wh_outbound_requests.product_id
                             AND wi.warehouse_id = 1
                             AND (COALESCE(wh_outbound_requests.pos_variation_id,'') = ''
                                  OR wi.pos_variation_id = wh_outbound_requests.pos_variation_id
                                  OR COALESCE(wi.pos_variation_id,'') = '')
                       ), 0) >= wh_outbound_requests.qty_ordered
                   ) AS can_fulfill_wh1
                   ,BOOL_AND(
                       COALESCE((
                           SELECT SUM(wi.qty) FROM wh_inventory wi
                           WHERE wi.product_id = wh_outbound_requests.product_id
                             AND wi.warehouse_id = 6
                             AND (COALESCE(wh_outbound_requests.pos_variation_id,'') = ''
                                  OR wi.pos_variation_id = wh_outbound_requests.pos_variation_id
                                  OR COALESCE(wi.pos_variation_id,'') = '')
                       ), 0) >= wh_outbound_requests.qty_ordered
                   ) AS can_fulfill_wh6
                   ,BOOL_AND(
                       COALESCE((
                           SELECT SUM(wi.qty) FROM wh_inventory wi
                           WHERE wi.product_id = wh_outbound_requests.product_id
                             AND (COALESCE(wh_outbound_requests.pos_variation_id,'') = ''
                                  OR wi.pos_variation_id = wh_outbound_requests.pos_variation_id
                                  OR COALESCE(wi.pos_variation_id,'') = '')
                       ), 0) >= wh_outbound_requests.qty_ordered
                   ) AS can_fulfill_total"""

        orders = conn.execute(f"""
            SELECT order_code,
                   MAX(order_id_external) as order_id_external,
                   MAX(shop_name) as shop_name,
                   MAX(carrier_name) as carrier_name,
                   MAX(tracking_code) as tracking_code,
                   MAX(carrier_picked_up_at) as carrier_picked_up_at,
                   MAX(pre_confirmed_at) as pre_confirmed_at,
                   MAX(pre_confirmed_by) as pre_confirmed_by,
                   MAX(pancake_status) as pancake_status,
                   MAX(warehouse_id) as warehouse_id,
                   COUNT(*) as item_count,
                   SUM(qty_ordered) as total_qty,
                   SUM(CASE WHEN status='pending'       THEN 1 ELSE 0 END) as pending_cnt,
                   SUM(CASE WHEN status='confirmed'     THEN 1 ELSE 0 END) as confirmed_cnt,
                   SUM(CASE WHEN status='pre_confirmed' THEN 1 ELSE 0 END) as pre_confirmed_cnt,
                   STRING_AGG(
                       DISTINCT COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)'),
                       ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)')
                   ) as products_preview,
                   STRING_AGG(
                       DISTINCT CASE
                           WHEN pos_variation_id IS NOT NULL AND pos_variation_id != ''
                           THEN (SELECT NULLIF(TRIM(variant_name),'') FROM wh_variation_map
                                 WHERE wh_variation_map.pos_variation_id = wh_outbound_requests.pos_variation_id
                                 LIMIT 1)
                           ELSE NULL END,
                       ', '
                   ) as variants_preview
                   {wh_hint_cols}
            {base_sql}
            ORDER BY {order_by_col} DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, main_params + [per_page, (page - 1) * per_page]).fetchall()

        # ── Tab counts ────────────────────────────────────────────────────────────
        # Chờ ĐVVC lấy: ĐỌC CÙNG NGUỒN với Dashboard (live_pos_status.json — Pancake live)
        # để badge khớp 100% với "Chờ chuyển hàng" trên trang chủ. Cách cũ đếm DB
        # wh_outbound_requests bị phồng do polling không update đơn rời status 9.
        # live_pos_status.json dùng shop_key → cần map shop_key→shop_name để filter quyền.
        try:
            from app_ctx import load_live_pos_status
            _live = load_live_pos_status() or {}
            _shop_results = _live.get("shop_results") or []
            # Map shop_key → shop_name (cần để filter theo `allowed` — list shop_name)
            _key_to_name = {}
            try:
                from db import get_conn as _gc
                with _gc() as _c2:
                    with _c2.cursor() as _cur2:
                        _cur2.execute("SELECT shop_key, shop_name FROM shops WHERE status='active'")
                        _key_to_name = {str(r[0]): str(r[1]) for r in _cur2.fetchall()}
            except Exception:
                pass
            count_waiting = 0
            for _sr in _shop_results:
                _sk = (_sr.get("shop_key") or "").strip()
                _sn = _key_to_name.get(_sk, "")
                if allowed is not None and _sn not in allowed:
                    continue
                if shop_filter and _sn != shop_filter:
                    continue
                _counts = _sr.get("counts") or {}
                count_waiting += int(_counts.get("9", 0) or 0)
        except Exception:
            # Fallback về cách cũ nếu live_pos_status không đọc được
            count_waiting = (conn.execute(f"""
                SELECT COUNT(*) as c FROM (
                    SELECT DISTINCT shop_id, order_code
                    FROM wh_outbound_requests {waiting_where}
                ) sq
            """, waiting_params).fetchone() or {}).get("c", 0)

        # Đã quét chuẩn bị, ĐVVC chưa lấy (pre_confirmed + waiting)
        count_pre_confirmed = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_outbound_requests {pre_conf_where}
                AND pancake_status='waiting'
            ) sq
        """, pre_conf_params).fetchone() or {}).get("c", 0)

        # Chờ xuất kho: NV đã quét (pre_confirmed) VÀ ĐVVC đã lấy (shipped/received)
        # → NV cần xác nhận xuất để trừ tồn. KHÔNG hiện đơn pending+shipped (lệch POS).
        count_pending = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_outbound_requests {shipped_where}
                AND status='pre_confirmed'
            ) sq
        """, shipped_params).fetchone() or {}).get("c", 0)

        # Kho VL xác nhận: NV thủ công quét (pre_confirmed_by set) + ĐVVC đã lấy hôm đó
        # Date dimension = carrier_picked_up_at (cùng chiều POS) → Kho luôn <= POS
        # KHÔNG dùng confirmed_at / order_inserted_at / created_at làm date filter
        count_confirmed = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_outbound_requests {shipped_where}
                AND status='confirmed'
                AND pre_confirmed_by IS NOT NULL AND pre_confirmed_by != ''
            ) sq
        """, shipped_params).fetchone() or {}).get("c", 0)

        # Lệch POS: ĐVVC lấy trên POS nhưng kho chưa quét (pending+shipped) → cảnh báo
        # KHÔNG áp staff_filter ở đây — lệch POS = NV chưa quét, không liên quan filter NV
        lech_pos_parts = shop_parts[:] + ["pancake_status IN ('shipped','received')", "status='pending'"]
        if date_parts:
            lech_pos_parts.extend(date_parts)
        lech_pos_where = "WHERE " + " AND ".join(lech_pos_parts)
        lech_pos_params = shop_params[:] + (date_params[:] if date_parts else [])
        count_lech_pos = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_outbound_requests {lech_pos_where}
            ) sq
        """, lech_pos_params).fetchone() or {}).get("c", 0)

        # Tổng POS đã giao ĐVVC = TẤT CẢ đơn shipped/received trong ngày (mọi status kho).
        # Bao gồm: confirmed + pre_confirmed+shipped (chờ xuất kho) + pending+shipped (lệch POS).
        # Đây là số thực tế ĐVVC lấy — dùng cho comparison card.
        count_pos_shipped = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_outbound_requests {shipped_where}
            ) sq
        """, shipped_params).fetchone() or {}).get("c", 0)
        if allowed is None:
            shops = conn.execute(
                "SELECT shop_name FROM wh_shops WHERE status='active' ORDER BY shop_name"
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT shop_name FROM wh_shops WHERE status='active' AND shop_name = ANY(%s) ORDER BY shop_name",
                (allowed,)
            ).fetchall()
        wh_rows = conn.execute("SELECT id, name FROM wh_warehouses WHERE status='active'").fetchall()
        wh_map = {r["id"]: r["name"] for r in wh_rows}

        # Danh sách nhân viên đã từng quét xác nhận xuất kho (cho dropdown filter)
        staff_rows = conn.execute("""
            SELECT DISTINCT pre_confirmed_by AS name
            FROM wh_outbound_requests
            WHERE pre_confirmed_by IS NOT NULL AND pre_confirmed_by <> ''
            ORDER BY pre_confirmed_by
        """).fetchall()
        staff_list = [r["name"] for r in staff_rows]

    can_write = role in _WH_WRITE_ROLES
    total_pages = max(1, (total_orders + per_page - 1) // per_page)
    return _rt("outbound.html", shop_biz_map=_load_shop_biz_map(), orders=orders, status_filter=status_filter,
               date_from=date_from, date_to=date_to, shop_filter=shop_filter,
               tracking_search=tracking_search, tracking_results=tracking_results,
               shops=shops, page=page, total_pages=total_pages, wh_map=wh_map,
               total_orders=total_orders, per_page=per_page,
               count_waiting=count_waiting, count_pre_confirmed=count_pre_confirmed,
               count_pending=count_pending,
               count_confirmed=count_confirmed, count_lech_pos=count_lech_pos,
               count_pos_shipped=count_pos_shipped,
               can_write=can_write,
               staff_list=staff_list, staff_filter=staff_filter)


@bp.route("/outbound/report-by-staff")
def outbound_report_by_staff():
    """Báo cáo in các đơn 1 nhân viên đã quét xác nhận xuất kho.
    Query params: staff (bắt buộc), date_from, date_to, shop, status (mặc định confirmed).
    """
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))
    staff = request.args.get("staff", "").strip()
    if not staff:
        return "<p>Thiếu tham số <code>staff</code>.</p>", 400
    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to", "").strip()
    shop_filter = request.args.get("shop", "").strip()
    status_filter = request.args.get("status", "confirmed").strip()

    where = ["pre_confirmed_by = %s"]
    params: list = [staff]
    if shop_filter:
        where.append("shop_name = %s"); params.append(shop_filter)
    if status_filter == "pre_confirmed":
        where.append("status = 'pre_confirmed'")
        date_col = "pre_confirmed_at"
    else:
        # confirmed / pending / khác → bám theo carrier_picked_up_at
        where.append("pancake_status IN ('shipped','received')")
        date_col = "carrier_picked_up_at"
    if date_from:
        where.append(f"{date_col} >= %s"); params.append(date_from)
    if date_to:
        dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        where.append(f"{date_col} < %s"); params.append(dt_end.strftime("%Y-%m-%d"))
    where_sql = "WHERE " + " AND ".join(where)

    with db() as conn:
        orders = conn.execute(f"""
            SELECT order_code,
                   MAX(shop_name)              AS shop_name,
                   MAX(carrier_name)           AS carrier_name,
                   MAX(tracking_code)          AS tracking_code,
                   MAX(carrier_picked_up_at)   AS carrier_picked_up_at,
                   MAX(pre_confirmed_at)       AS pre_confirmed_at,
                   COUNT(*)                    AS item_count,
                   SUM(qty_ordered)            AS total_qty,
                   STRING_AGG(DISTINCT
                       COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)'),
                       ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)')
                   ) AS products_preview
            FROM wh_outbound_requests
            {where_sql}
            GROUP BY shop_id, order_code
            ORDER BY MAX({date_col}) DESC NULLS LAST
        """, params).fetchall()
        orders = [dict(r) for r in orders]

    total_qty = sum((o.get("total_qty") or 0) for o in orders)
    printed_at = now_hcm().strftime("%d/%m/%Y %H:%M")
    return render_template("kho_vat_ly/bao_cao_xuat_theo_nv.html",
        staff=staff, orders=orders,
        date_from=date_from, date_to=date_to,
        shop_filter=shop_filter, status_filter=status_filter,
        total_orders=len(orders), total_qty=total_qty,
        printed_at=printed_at)


@bp.route("/outbound/order/<order_code>")
def outbound_order_detail(order_code):
    shop_name = request.args.get("shop", "").strip()
    with db() as conn:
        _uuid_re = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        _sm_sub = """
            LEFT JOIN (
                SELECT DISTINCT ON (product_id, ref_order_id)
                       product_id, ref_order_id, created_at AS sm_confirmed_at
                FROM wh_stock_movements
                WHERE type = 'outbound_confirmed'
                ORDER BY product_id, ref_order_id, id ASC
            ) sm ON sm.product_id = o.product_id AND sm.ref_order_id = o.order_code
        """
        # Subquery: tồn kho đúng biến thể.
        # - Nếu có pos_variation_id: dùng variant_key (canonical) từ wh_variation_map
        #   để SUM đúng biến thể (nhiều UUID shop khác nhau cùng variant_key); fallback
        #   UUID legacy nếu chưa có variant_key.
        # - Nếu không có pos_variation_id: SUM toàn bộ (base + biến thể) cho SP đó.
        _inv_sub = """
            LEFT JOIN LATERAL (
                SELECT COALESCE(
                    CASE WHEN NULLIF(o.pos_variation_id,'') IS NOT NULL THEN
                        COALESCE(
                          (SELECT SUM(qty) FROM wh_inventory wi
                             JOIN wh_variation_map vm2 ON vm2.variant_name = wi.variant_key
                            WHERE wi.product_id = o.product_id
                              AND vm2.pos_variation_id = o.pos_variation_id),
                          (SELECT SUM(qty) FROM wh_inventory
                            WHERE product_id = o.product_id
                              AND pos_variation_id = o.pos_variation_id),
                          0
                        )
                    ELSE
                        (SELECT COALESCE(SUM(qty),0) FROM wh_inventory
                          WHERE product_id = o.product_id)
                    END, 0
                ) AS qty
            ) i ON true
        """
        _varmap_sub = """
            LEFT JOIN wh_variation_map vm ON vm.pos_variation_id = o.pos_variation_id
        """
        # Ẩn row legacy duplicate: row có product_sku=UUID + pos_variation_id rỗng
        # và đã có row khác cùng đơn cùng variation_id (twin đã đúng) → không hiện.
        # Tránh người dùng thấy 2 dòng cùng SP, 1 dòng "None" gây hiểu nhầm.
        _hide_legacy = """
            AND NOT (
                o.product_sku ~ %s
                AND (o.pos_variation_id IS NULL OR o.pos_variation_id = '')
                AND EXISTS (
                    SELECT 1 FROM wh_outbound_requests o2
                    WHERE o2.order_id_external = o.order_id_external
                      AND o2.pos_variation_id = o.product_sku
                      AND o2.id != o.id
                )
            )
        """
        if shop_name:
            items = conn.execute(f"""
                SELECT o.*,
                       i.qty as current_qty,
                       COALESCE(
                           p.sku,
                           CASE WHEN o.product_sku ~ %s THEN NULL
                                ELSE NULLIF(o.product_sku, '') END
                       ) as display_sku,
                       COALESCE(NULLIF(o.confirmed_at,''), sm.sm_confirmed_at) as effective_confirmed_at,
                       NULLIF(TRIM(COALESCE(vm.variant_name,'')), '') as variant_name,
                       ws.pos_shop_id
                FROM wh_outbound_requests o
                {_inv_sub}
                LEFT JOIN wh_products  p ON p.id = o.product_id
                {_varmap_sub}
                {_sm_sub}
                LEFT JOIN wh_shops ws ON ws.id = o.shop_id
                WHERE o.order_code=%s AND o.shop_name=%s
                  {_hide_legacy}
                ORDER BY o.id
            """, (_uuid_re, order_code, shop_name, _uuid_re)).fetchall()
        else:
            items = conn.execute(f"""
                SELECT o.*,
                       i.qty as current_qty,
                       COALESCE(
                           p.sku,
                           CASE WHEN o.product_sku ~ %s THEN NULL
                                ELSE NULLIF(o.product_sku, '') END
                       ) as display_sku,
                       COALESCE(NULLIF(o.confirmed_at,''), sm.sm_confirmed_at) as effective_confirmed_at,
                       NULLIF(TRIM(COALESCE(vm.variant_name,'')), '') as variant_name,
                       ws.pos_shop_id
                FROM wh_outbound_requests o
                {_inv_sub}
                LEFT JOIN wh_products  p ON p.id = o.product_id
                {_varmap_sub}
                {_sm_sub}
                LEFT JOIN wh_shops ws ON ws.id = o.shop_id
                WHERE o.order_code=%s
                  {_hide_legacy}
                ORDER BY o.id
            """, (_uuid_re, order_code, _uuid_re)).fetchall()
        if not items:
            flash("Không tìm thấy đơn.", "danger")
            return redirect(url_for(".outbound_list"))
    first = items[0]
    pending_cnt = sum(1 for r in items if r["status"] == "pending")
    confirmed_cnt = sum(1 for r in items if r["status"] == "confirmed")
    total_qty = sum(r["qty_ordered"] for r in items)
    role = session.get("role", "staff")
    can_write = role in _WH_WRITE_ROLES
    return _rt("outbound_order.html", items=items, order_code=order_code,
               first=first, pending_cnt=pending_cnt, confirmed_cnt=confirmed_cnt,
               total_qty=total_qty, can_write=can_write)


@bp.route("/outbound/<int:req_id>/confirm", methods=["POST"])
def outbound_confirm(req_id):
    """Redirect sang confirm-all — 1 mã vận đơn = 1 gói hàng, không xuất 1 phần."""
    denied = _deny_staff()
    if denied: return denied
    note = request.form.get("note", "").strip()
    with db() as conn:
        req = conn.execute("SELECT order_code, shop_name FROM wh_outbound_requests WHERE id=%s", (req_id,)).fetchone()
        if not req:
            flash("Không tìm thấy đơn.", "danger")
            return redirect(url_for(".outbound_list"))
    order_code = req["order_code"]
    shop_name  = req["shop_name"] or ""
    # 1 mã vận đơn = 1 gói hàng → luôn xác nhận TẤT CẢ (307 giữ nguyên POST method)
    return redirect(url_for(".outbound_confirm_all", order_code=order_code,
                            shop=shop_name), code=307)


@bp.route("/outbound/order/<order_code>/confirm-all", methods=["POST"])
def outbound_confirm_all(order_code):
    denied = _deny_staff()
    if denied: return denied
    note = request.form.get("note", "").strip()
    shop_name = (request.form.get("shop_name") or request.args.get("shop") or "").strip()

    with db() as conn:
        if shop_name:
            items = conn.execute(
                "SELECT * FROM wh_outbound_requests WHERE order_code=%s AND shop_name=%s AND status='pending'",
                (order_code, shop_name)
            ).fetchall()
        else:
            items = conn.execute(
                "SELECT * FROM wh_outbound_requests WHERE order_code=%s AND status='pending'",
                (order_code,)
            ).fetchall()
        if not items:
            flash("Không có mặt hàng nào cần xác nhận.", "info")
            return redirect(url_for(".outbound_order_detail", order_code=order_code, shop=shop_name))

        if not shop_name:
            shop_name = items[0]["shop_name"] or ""

        # ── Chặn xác nhận nếu đơn chưa được ĐVVC lấy ──
        ps = items[0]["pancake_status"] or ""
        if ps not in ("shipped", "received"):
            label = "Chờ chuyển hàng" if ps == "waiting" else ps
            flash(f"⛔ Đơn {order_code} đang ở trạng thái '{label}' — ĐVVC chưa lấy hàng, không thể xác nhận xuất kho.", "danger")
            return redirect(url_for(".outbound_order_detail", order_code=order_code, shop=shop_name))

        # ── Kiểm tra tồn kho: auto-pick kho có đủ hàng (kho nào có hàng kho đó xuất) ──
        short_items = []
        # cache (pid, var_id) → (wh_id_picked, qty_running)
        inv_cache = {}
        for item in items:
            pid = item["product_id"]
            if not pid:
                continue
            var_id = (item.get("pos_variation_id") or "").strip() or None
            cache_key = (pid, var_id)
            if cache_key not in inv_cache:
                wh_picked, qty_avail = _pick_warehouse_for_outbound(
                    conn, pid, var_id, item["qty_ordered"]
                )
                inv_cache[cache_key] = [wh_picked, qty_avail]
            wh_id_to_use, qty_before = inv_cache[cache_key]
            if item["qty_ordered"] > qty_before:
                prod_label = _prod_display(conn, pid, item.get('product_sku'), item.get('product_name'))
                vm_row = conn.execute(
                    "SELECT variant_name FROM wh_variation_map WHERE pos_variation_id=%s",
                    (var_id,)
                ).fetchone() if var_id else None
                vname = (vm_row["variant_name"] if vm_row else "") or ""
                if vname:
                    prod_label = f"{prod_label} [{vname}]"
                wh_row = conn.execute("SELECT name FROM wh_warehouses WHERE id=%s", (wh_id_to_use,)).fetchone()
                wh_label = wh_row["name"] if wh_row else f"Kho {wh_id_to_use}"
                short_items.append(
                    f"{prod_label}: cần {fmt_qty(item['qty_ordered'])}, còn {fmt_qty(qty_before)} ({wh_label})"
                )

        if short_items:
            flash(
                f"⛔ Không thể xuất đơn {order_code} — các mặt hàng sau thiếu tồn kho: "
                + "; ".join(short_items),
                "danger"
            )
            return redirect(url_for(".outbound_order_detail", order_code=order_code, shop=shop_name))

        # ── Tiến hành xuất: trừ đúng kho đã pick ──
        done = 0
        confirmed_by = session.get("username") or session.get("full_name") or "?"
        for item in items:
            product_id = item["product_id"]
            qty_actual = item["qty_ordered"]
            var_id = (item.get("pos_variation_id") or "").strip() or None
            cache_key = (product_id, var_id)
            wh_id_to_use, qty_before = inv_cache.get(cache_key, [_DEFAULT_WAREHOUSE_ID, 0])

            if product_id:
                qty_after = qty_before - qty_actual
                inv_cache[cache_key] = [wh_id_to_use, qty_after]   # cập nhật qty chạy
                _upsert_inventory(conn, product_id, wh_id_to_use, qty_after, var_id)
                conn.execute("""
                    INSERT INTO wh_stock_movements
                      (type, product_id, warehouse_id, qty, qty_before, qty_after,
                       ref_order_id, note, created_at, pos_variation_id)
                    VALUES ('outbound_confirmed', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (product_id, wh_id_to_use, qty_actual, qty_before, qty_after, order_code,
                      note or f"Xuất đơn {order_code}", now_vn(), var_id))
            # None badge → xác nhận không trừ tồn

            conn.execute(
                """UPDATE wh_outbound_requests
                   SET status='confirmed', qty_confirmed=%s, note=%s,
                       confirmed_by=%s, confirmed_at=%s, warehouse_id=%s
                   WHERE id=%s""",
                (qty_actual, note, confirmed_by, now_vn(), wh_id_to_use, item["id"])
            )
            done += 1

    flash(f"✅ Đã xác nhận xuất {done} mặt hàng cho đơn {order_code}.", "success")
    return redirect(url_for(".outbound_order_detail", order_code=order_code, shop=shop_name))


def _parallel_find_shop_by_tracking(tracking: str, timeout: int = 8) -> "str | None":
    """Parallel POS lookup theo `extend_code=<tracking>` trên TẤT CẢ shop active.

    Pancake API hỗ trợ filter `extend_code` chính xác — mỗi shop 1 call ~500ms.
    Quét song song 83 shop bằng 16 worker → tổng 2-3s. Tìm thấy 1 shop nào có đơn
    với tracking đó → return shop_name ngay (cancel các future còn lại).

    Dùng làm Lớp 4 fallback khi NV scan tracking nhưng DB cache + sync polling đều miss.
    Trả về `None` nếu không shop nào có (tracking không tồn tại trên POS, hoặc shop
    đang lỗi 500/timeout — silent skip).
    """
    import requests as _rq
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    try:
        with db() as _conn:
            shops = _conn.execute(
                "SELECT shop_name, pos_shop_id, pos_api_key FROM wh_shops "
                "WHERE status='active' AND pos_api_key IS NOT NULL AND pos_api_key <> ''"
            ).fetchall()
        if not shops:
            return None

        def _query(shop):
            try:
                r = _rq.post(
                    f"https://pos.pancake.vn/api/v1/shops/{shop['pos_shop_id']}/orders/get_orders",
                    params=[("api_key", shop["pos_api_key"]),
                            ("extend_code", tracking), ("page_size", 3)],
                    json={}, timeout=timeout
                )
                if r.status_code != 200:
                    return None
                d = r.json()
                if isinstance(d, dict) and (d.get("total_entries") or 0) > 0:
                    return shop["shop_name"]
            except Exception:
                pass
            return None

        with _TPE(max_workers=16) as ex:
            futs = [ex.submit(_query, s) for s in shops]
            try:
                for fut in _ac(futs, timeout=timeout + 5):
                    name = fut.result()
                    if name:
                        # Early cancel — tiết kiệm 80+ API call thừa
                        for f in futs:
                            if not f.done():
                                f.cancel()
                        return name
            except Exception:
                pass
        return None
    except Exception:
        log.warning("_parallel_find_shop_by_tracking lỗi tracking=%s", tracking, exc_info=True)
        return None


def _live_sync_shop_for_scan(shop_name: str) -> bool:
    """Kéo đơn status 9+2 từ Pancake cho shop cụ thể (fallback khi scan không tìm thấy).
    Chạy blocking ~1-3s. Trả về True nếu sync chạy được."""
    try:
        with db() as _conn:
            row = _conn.execute(
                "SELECT pos_shop_id FROM wh_shops WHERE shop_name=%s AND status='active' LIMIT 1",
                (shop_name,)
            ).fetchone()
        if not row:
            return False
        pos_shop_id = str(row["pos_shop_id"] or "").strip()
        if not pos_shop_id:
            return False
        _now_dt = now_hcm()
        date_to   = _now_dt.strftime("%Y-%m-%d")
        date_from = (_now_dt - timedelta(days=2)).strftime("%Y-%m-%d")  # 2 ngày đủ, nhanh hơn 7 ngày
        sync_outbound_for_date_range(date_from, date_to, only_pos_shop_id=pos_shop_id, shipped_only=True)
        return True
    except Exception:
        log.warning("_live_sync_shop_for_scan lỗi shop=%s", shop_name, exc_info=True)
        return False


@bp.route("/outbound/scan-confirm", methods=["POST"])
def outbound_scan_confirm():
    denied = _deny_staff()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    confirmed_by = (data.get("confirmed_by") or session.get("username") or "").strip()
    shop_name_filter = (data.get("shop_name") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Mã trống"}), 400

    with db() as conn:
        # ── Case 1: Đơn đã shipped/received (DVVC đã lấy) → xác nhận xuất kho + trừ tồn ──
        if shop_name_filter:
            items = conn.execute("""
                SELECT * FROM wh_outbound_requests
                WHERE status='pending'
                  AND pancake_status IN ('shipped','received')
                  AND (order_code=%s OR tracking_code=%s)
                  AND shop_name=%s
            """, (code, code, shop_name_filter)).fetchall()
        else:
            items = conn.execute("""
                SELECT * FROM wh_outbound_requests
                WHERE status='pending'
                  AND pancake_status IN ('shipped','received')
                  AND (order_code=%s OR tracking_code=%s)
            """, (code, code)).fetchall()

        if not items:
            # ── Case 2: Đơn đang chờ chuyển hàng → đánh dấu pre_confirmed (CHƯA trừ tồn) ──
            if shop_name_filter:
                waiting_items = conn.execute("""
                    SELECT * FROM wh_outbound_requests
                    WHERE status='pending'
                      AND pancake_status IN ('waiting','confirmed')
                      AND (order_code=%s OR tracking_code=%s)
                      AND shop_name=%s
                """, (code, code, shop_name_filter)).fetchall()
            else:
                waiting_items = conn.execute("""
                    SELECT * FROM wh_outbound_requests
                    WHERE status='pending'
                      AND pancake_status IN ('waiting','confirmed')
                      AND (order_code=%s OR tracking_code=%s)
                """, (code, code)).fetchall()
                # Khi tracking code trùng nhiều shop (SPX tái sử dụng mã),
                # ưu tiên đơn pancake_status='waiting' (status 9 — đang chờ ĐVVC lấy).
                # Bỏ qua các đơn 'confirmed' cũ nếu vẫn còn 'waiting' mới hơn.
                if waiting_items:
                    waiting_only = [w for w in waiting_items if w["pancake_status"] == "waiting"]
                    if waiting_only:
                        waiting_items = waiting_only

            # ── Fallback: live sync nếu đơn chưa kịp về DB ──
            if not waiting_items and shop_name_filter:
                _live_sync_shop_for_scan(shop_name_filter)
                waiting_items = conn.execute("""
                    SELECT * FROM wh_outbound_requests
                    WHERE status='pending'
                      AND pancake_status IN ('waiting','confirmed')
                      AND (order_code=%s OR tracking_code=%s)
                      AND shop_name=%s
                """, (code, code, shop_name_filter)).fetchall()

            # ── Fallback 2: không chọn shop → parallel POS lookup extend_code 83 shop ──
            # (refactor 2026-05-13): Pancake hỗ trợ filter extend_code chính xác.
            # Quét song song 83 shop trong ~2-3s, tìm shop nào có tracking → sync shop đó.
            # Cũ: sequential 10 shop top-4h (~30s, miss đơn shop ít hoạt động). Xem
            # INCIDENTS 2026-05-13 + adspage rationale.
            if not waiting_items and not shop_name_filter:
                _t0_scan = time.time()
                found_shop = _parallel_find_shop_by_tracking(code, timeout=8)
                log.info("[scan] parallel_find_shop_by_tracking code=%s → %s (%.2fs)",
                         code, found_shop, time.time() - _t0_scan)
                if found_shop:
                    _live_sync_shop_for_scan(found_shop)
                    waiting_items = conn.execute("""
                        SELECT * FROM wh_outbound_requests
                        WHERE status='pending'
                          AND pancake_status IN ('waiting','confirmed')
                          AND (order_code=%s OR tracking_code=%s)
                          AND shop_name=%s
                    """, (code, code, found_shop)).fetchall()

            if waiting_items:
                order_code = waiting_items[0]["order_code"]
                shop_name  = waiting_items[0]["shop_name"]

                # ── Kiểm tra tồn kho trước khi đánh dấu chuẩn bị ──
                # Kiểm tra tồn kho theo kho có hàng (kho nào có hàng kho đó xuất)
                stock_errors = []
                pre_wh_map = {}  # item_id → wh_id đã pick
                for wi in waiting_items:
                    product_id = wi["product_id"]
                    qty_need   = wi["qty_ordered"] or 0
                    pos_var_id = (wi.get("pos_variation_id") or "").strip() or None
                    wh_id, qty_have = _pick_warehouse_for_outbound(conn, product_id, pos_var_id, qty_need)
                    pre_wh_map[wi["id"]] = wh_id
                    if qty_need > qty_have:
                        stock_errors.append(
                            f"{_prod_display(conn, product_id, wi.get('product_sku'), wi.get('product_name'))}: cần {qty_need}, tồn {qty_have}"
                        )
                if stock_errors:
                    return jsonify({
                        "ok": False,
                        "error": "Không đủ tồn kho, không thể chuẩn bị: " + "; ".join(stock_errors)
                    })

                ts = now_vn()
                for wi in waiting_items:
                    wh_id = pre_wh_map.get(wi["id"], _DEFAULT_WAREHOUSE_ID)
                    conn.execute("""
                        UPDATE wh_outbound_requests
                        SET status='pre_confirmed', pre_confirmed_at=%s, pre_confirmed_by=%s,
                            warehouse_id=%s
                        WHERE id=%s
                    """, (ts, confirmed_by, wh_id, wi["id"]))
                return jsonify({
                    "ok": True,
                    "pre_confirmed": True,
                    "order_code": order_code,
                    "shop_name": shop_name,
                    "items_confirmed": len(waiting_items),
                    "message": f"✅ Đã đánh dấu chuẩn bị hàng — chờ ĐVVC lấy để tự động xuất kho",
                    "confirmed_by": confirmed_by
                })

            # ── Case 3: Kiểm tra các trạng thái khác ──
            pre_done = (conn.execute("""
                SELECT COUNT(*) as c FROM wh_outbound_requests
                WHERE (order_code=%s OR tracking_code=%s) AND status='pre_confirmed'
            """, (code, code)).fetchone() or {}).get("c", 0)
            if pre_done > 0:
                return jsonify({"ok": False, "already": True,
                                "error": "Đơn này đã được đánh dấu chuẩn bị — đang chờ ĐVVC lấy hàng."})
            done = (conn.execute("""
                SELECT COUNT(*) as c FROM wh_outbound_requests
                WHERE (order_code=%s OR tracking_code=%s) AND status='confirmed'
            """, (code, code)).fetchone() or {}).get("c", 0)
            if done > 0:
                return jsonify({"ok": False, "already": True,
                                "error": f"Đơn này đã xác nhận xuất kho rồi ({done} mục)."})

            # ── Case 5: Đơn đã hủy / hoàn / auto-cleaned (2026-06-10) ──
            # Trước đây các case này rơi xuống "Không tìm thấy đơn" → NV nhầm là
            # mã sai. Giờ check riêng để báo rõ trạng thái.
            cancelled = conn.execute("""
                SELECT pancake_status, status, shop_name, order_code
                  FROM wh_outbound_requests
                 WHERE (order_code=%s OR tracking_code=%s)
                   AND (pancake_status IN ('cancelled','returned') OR status='auto_cleaned')
                 ORDER BY created_at DESC LIMIT 1
            """, (code, code)).fetchone()
            if cancelled:
                ps = (cancelled["pancake_status"] or "").strip()
                sn = (cancelled["shop_name"] or "").strip()
                oc = (cancelled["order_code"] or "").strip()
                shop_part = f" ({sn})" if sn else ""
                if ps == "cancelled":
                    msg = f"⚠ Đơn #{oc}{shop_part} đã HỦY trên POS — không cần xử lý."
                elif ps == "returned":
                    msg = f"⚠ Đơn #{oc}{shop_part} đã HOÀN trên POS — không cần xử lý."
                else:
                    msg = f"⚠ Đơn #{oc}{shop_part} đã được dọn tự động (quá hạn / shop không xác nhận)."
                return jsonify({"ok": False, "cancelled": True, "error": msg})

            return jsonify({"ok": False, "error": "Không tìm thấy đơn. Kiểm tra lại mã."})

        # ── Xử lý Case 1: trừ tồn kho + xác nhận ──
        order_code = items[0]["order_code"]
        shop_name = items[0]["shop_name"]
        errors = []
        confirmed_items = []

        for item in items:
            product_id = item["product_id"]
            qty_actual = item["qty_ordered"]
            pos_var_id = (item.get("pos_variation_id") or "").strip() or None

            # Auto-pick kho có đủ hàng (kho nào có hàng kho đó xuất)
            wh_id, qty_before = _pick_warehouse_for_outbound(conn, product_id, pos_var_id, qty_actual)

            if qty_actual > qty_before:
                errors.append(f"{_prod_display(conn, product_id, item.get('product_sku'), item.get('product_name'))}: tồn {qty_before}, đặt {qty_actual}")
                continue
            qty_after = qty_before - qty_actual
            # Dùng _get_variant_inv_outbound để có cross-product fallback (SP tách)
            inv, is_variant = _get_variant_inv_outbound(conn, product_id, wh_id, pos_var_id)
            # Cập nhật tồn đúng variant (hoặc product-level nếu chưa có variant)
            # inventory_row_id → deduct trực tiếp theo id khi tồn ở product_id khác
            _upsert_inventory(conn, product_id, wh_id, qty_after,
                              pos_variation_id=pos_var_id if is_variant else None,
                              inventory_row_id=inv["id"] if inv else None)
            conn.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after,
                   ref_order_id, note, created_at, pos_variation_id)
                VALUES ('outbound_confirmed', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (product_id, wh_id, qty_actual, qty_before, qty_after, order_code,
                  f"Quét mã — đơn {order_code}", now_vn(), pos_var_id))
            _ts_now = now_vn()
            conn.execute(
                "UPDATE wh_outbound_requests"
                " SET status='confirmed', qty_confirmed=%s, note=%s,"
                "     confirmed_by=%s, confirmed_at=%s, warehouse_id=%s,"
                "     pre_confirmed_by=%s, pre_confirmed_at=%s"  # bắt buộc ghi nhận NV quét
                " WHERE id=%s",
                (qty_actual, "Quét mã vạch", confirmed_by, _ts_now, wh_id,
                 confirmed_by, _ts_now,  # Case 1: direct scan = pre_confirm + confirm cùng lúc
                 item["id"])
            )
            # Lấy tên biến thể để hiển thị trong kết quả scan
            vm_row = conn.execute(
                "SELECT variant_name FROM wh_variation_map WHERE pos_variation_id=%s",
                (pos_var_id,)
            ).fetchone() if pos_var_id else None
            vname = (vm_row["variant_name"] if vm_row else "") or ""
            confirmed_items.append({
                "product_name": item["product_name"],
                "variant_name": vname,
                "product_sku": item["product_sku"],
                "qty": qty_actual
            })

    if not confirmed_items and errors:
        return jsonify({"ok": False, "error": "Lỗi tồn kho: " + "; ".join(errors)})
    order_fully_confirmed = len(errors) == 0 and len(confirmed_items) > 0
    return jsonify({"ok": True, "order_code": order_code, "shop_name": shop_name,
                    "items_confirmed": len(confirmed_items), "products": confirmed_items,
                    "errors": errors, "order_fully_confirmed": order_fully_confirmed,
                    "confirmed_by": confirmed_by})


@bp.route("/outbound/sync", methods=["POST"])
def outbound_sync():
    denied = _deny_staff()
    if denied: return denied
    from .wh_db import OUTBOUND_MANUAL_SYNC_LOCK
    now_vn_tz = datetime.now(timezone.utc) + timedelta(hours=7)
    date_to = now_vn_tz.strftime("%Y-%m-%d")
    try:
        days_back = max(1, min(int(request.form.get("days_back", 3)), 365))
    except (ValueError, TypeError):
        days_back = 3
    date_from = (now_vn_tz - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")

    # Kiểm tra nếu manual sync đang chạy → không khởi thêm thread
    if not OUTBOUND_MANUAL_SYNC_LOCK.acquire(blocking=False):
        flash("⚠️ Sync xuất hàng đang chạy nền. Vui lòng đợi hoàn tất rồi thử lại.", "warning")
        return redirect(url_for(".outbound_list"))
    OUTBOUND_MANUAL_SYNC_LOCK.release()

    _outbound_sync_progress.update({
        "running": True, "shop_name": "", "shop_idx": 0, "shop_total": 0,
        "status_label": "", "fetched": 0, "inserted": 0, "updated": 0,
        "date_from": date_from, "date_to": date_to,
        "started_at": now_hcm().strftime("%H:%M:%S"), "done_at": "", "errors": [],
    })

    def _progress_cb(shop_name="", shop_idx=0, shop_total=0,
                     status_label="", fetched=0, inserted=0, updated=0):
        _outbound_sync_progress.update({
            "shop_name": shop_name, "shop_idx": shop_idx, "shop_total": shop_total,
            "status_label": status_label, "fetched": fetched,
            "inserted": inserted, "updated": updated,
        })

    def _run():
        try:
            log.info("[outbound_sync] Bắt đầu sync xuất hàng %s → %s", date_from, date_to)
            res = sync_outbound_for_date_range(
                date_from, date_to, active_only=False, wait_for_lock=True,
                progress_cb=_progress_cb,
            )
            errors = res.get("errors", [])
            _outbound_sync_progress.update({
                "running": False,
                "inserted": res.get("inserted", 0),
                "updated": res.get("updated", 0),
                "done_at": now_hcm().strftime("%H:%M:%S"),
                "errors": errors,
            })
            if errors:
                log.error("[outbound_sync] Lỗi: %s", errors)
            else:
                log.info("[outbound_sync] Xong: +%d mới, cập nhật %d, bỏ %d (%s → %s)",
                         res.get('inserted', 0), res.get('updated', 0),
                         res.get('skipped', 0), date_from, date_to)
        except Exception as e:
            _outbound_sync_progress.update({"running": False, "done_at": now_hcm().strftime("%H:%M:%S"),
                                            "errors": [str(e)]})
            log.error("[outbound_sync bg] %s", e)

    threading.Thread(target=_run, daemon=True, name="wh-manual-sync").start()
    flash(f"⏳ Đang sync xuất hàng từ POS ({days_back} ngày: {date_from} → {date_to}, "
          f"toàn bộ status). Quá trình mất 2–5 phút. Vui lòng refresh trang sau vài phút.", "info")
    return redirect(url_for(".outbound_list"))


def outbound_sync_quick():
    """Đồng bộ nhanh từ POS: chỉ trạng thái Chờ chuyển hàng (9) + Đã giao ĐVVC (2), 7 ngày.
    Cập nhật mã vận đơn / ĐVVC trước khi NV quét — cùng logic fast-sync nền."""
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "endpoint": "outbound_sync_quick",
            "hint": "Dùng POST (nút Đồng bộ POS) — GET chỉ để kiểm tra route có đăng ký.",
            "aliases": ["/kho-vat-ly/outbound/sync-quick", "/kho-vat-ly/outbound/sync_quick"],
        })
    denied = _deny_staff()
    if denied:
        return denied
    user = session.get("username") or session.get("full_name") or "anon"
    now_ts = time.time()
    last_ts = _outbound_quick_sync_last.get(user, 0)
    if now_ts - last_ts < _OUTBOUND_QUICK_SYNC_COOLDOWN_SEC:
        wait = int(_OUTBOUND_QUICK_SYNC_COOLDOWN_SEC - (now_ts - last_ts)) + 1
        return jsonify({"ok": False, "error": f"Vui lòng đợi {wait} giây rồi thử lại."}), 429

    now_vn = datetime.now(timezone.utc) + timedelta(hours=7)
    date_to = now_vn.strftime("%Y-%m-%d")
    date_from = (now_vn - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        res = sync_outbound_for_date_range(
            date_from, date_to, active_only=False, shipped_only=True
        )
    except Exception as e:
        log.exception("[outbound_sync_quick]")
        return jsonify({"ok": False, "error": str(e)}), 500

    errs = res.get("errors") or []
    lock_busy = (
        res.get("inserted", 0) == 0
        and res.get("updated", 0) == 0
        and errs
        and "đang chạy" in (errs[0] or "")
    )
    if lock_busy:
        return jsonify({
            "ok": False,
            "error": "Đồng bộ đang chạy nền — vui lòng đợi vài giây rồi thử lại.",
            "inserted": 0, "updated": 0,
        }), 503

    _outbound_quick_sync_last[user] = now_ts
    return jsonify({
        "ok": True,
        "inserted": res.get("inserted", 0),
        "updated": res.get("updated", 0),
        "skipped": res.get("skipped", 0),
        "errors": errs[:5],
        "date_from": date_from,
        "date_to": date_to,
    })


# ─────────────────────────────────────────
# ADMIN: BULK AUTO-CONFIRM XUẤT KHO
# ─────────────────────────────────────────
@bp.route("/outbound/admin-bulk-confirm", methods=["POST"])
def outbound_admin_bulk_confirm():
    if session.get("role") != "admin":
        abort(403)

    # Chặn double-click / 2 lần chạy song song (kể cả từ worker khác qua Redis)
    if not _ADMIN_BULK_CONFIRM_LOCK.acquire(blocking=False):
        flash("⏳ Job tự động xuất kho đang chạy — vui lòng chờ job hiện tại xong.", "warning")
        return redirect(url_for(".outbound_list"))
    _bulk_confirm_redis_token = _redis_lock_acquire(_BULK_CONFIRM_REDIS_KEY, ttl=1800)
    if _bulk_confirm_redis_token is None:
        _ADMIN_BULK_CONFIRM_LOCK.release()
        flash("⏳ Job tự động xuất kho đang chạy ở worker khác — vui lòng chờ.", "warning")
        return redirect(url_for(".outbound_list"))

    confirmed_by = session.get("username") or session.get("full_name") or "admin"
    _bc_redis_tok = _bulk_confirm_redis_token   # capture cho closure

    _admin_bulk_confirm_progress.update({
        "running": True, "total": 0, "done": 0, "auto_adjusted": 0, "errors_count": 0,
        "started_at": now_hcm().strftime("%H:%M:%S"), "done_at": "",
        "message": "Đang khởi tạo...", "errors": [],
    })

    def _run():
        try:
            BATCH_SIZE = 200  # commit mỗi 200 dòng để không giữ lock + khỏi timeout
            auto_adjusted = 0
            done = 0

            # Lấy tổng trước (chỉ COUNT, nhanh)
            with db() as conn:
                total = conn.execute("""
                    SELECT COUNT(*) FROM wh_outbound_requests
                    WHERE pancake_status IN ('shipped','received') AND status='pending'
                """).fetchone()[0]

            _admin_bulk_confirm_progress.update({
                "total": total, "message": f"Tìm thấy {total} dòng cần xử lý"
            })

            if total == 0:
                _admin_bulk_confirm_progress.update({
                    "running": False, "done_at": now_hcm().strftime("%H:%M:%S"),
                    "message": "Không có dòng nào cần xác nhận.",
                })
                return

            # Xử lý theo batch — mỗi batch 1 transaction riêng
            offset_id = 0
            while True:
                with db() as conn:
                    pending_items = conn.execute("""
                        SELECT o.* FROM wh_outbound_requests o
                        WHERE o.pancake_status IN ('shipped','received')
                          AND o.status='pending'
                          AND o.id > %s
                        ORDER BY o.id
                        LIMIT %s
                    """, (offset_id, BATCH_SIZE)).fetchall()

                    if not pending_items:
                        break

                    for item in pending_items:
                        offset_id = item["id"]
                        product_id = item["product_id"]

                        if not product_id:
                            conn.execute(
                                "UPDATE wh_outbound_requests SET status='confirmed', qty_confirmed=%s, confirmed_by=%s, confirmed_at=%s WHERE id=%s",
                                (item["qty_ordered"] or 1, confirmed_by, now_vn(), item["id"])
                            )
                            done += 1
                            continue

                        qty_needed = item["qty_ordered"] or 0
                        if qty_needed <= 0:
                            continue

                        pos_var_id = (item.get("pos_variation_id") or "").strip() or None
                        wh_id, qty_before = _pick_warehouse_for_outbound(conn, product_id, pos_var_id, qty_needed)
                        # Cross-product: tìm row thực tế (SP tách sau nhập kho)
                        inv_xp, _ = _get_variant_inv_outbound(conn, product_id, wh_id, pos_var_id)

                        if qty_before < qty_needed:
                            deficit = qty_needed - qty_before
                            qty_topped = qty_before + deficit
                            _upsert_inventory(conn, product_id, wh_id, qty_topped, pos_var_id,
                                              inventory_row_id=inv_xp["id"] if inv_xp else None)
                            conn.execute("""
                                INSERT INTO wh_stock_movements
                                  (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
                                VALUES ('admin_auto_adjust', %s, %s, %s, %s, %s, %s, %s, %s)
                            """, (product_id, wh_id, deficit, qty_before, qty_topped,
                                  item["order_code"], "Admin tự động bù tồn kho", now_vn()))
                            qty_before = qty_topped
                            auto_adjusted += 1
                            # Refresh row sau khi tự động bù
                            inv_xp, _ = _get_variant_inv_outbound(conn, product_id, wh_id, pos_var_id)

                        qty_after = qty_before - qty_needed
                        _upsert_inventory(conn, product_id, wh_id, qty_after, pos_var_id,
                                          inventory_row_id=inv_xp["id"] if inv_xp else None)
                        conn.execute("""
                            INSERT INTO wh_stock_movements
                              (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
                            VALUES ('outbound_confirmed', %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (product_id, wh_id, qty_needed, qty_before, qty_after,
                              item["order_code"], "Admin bulk confirm", now_vn()))
                        conn.execute(
                            "UPDATE wh_outbound_requests SET status='confirmed', qty_confirmed=%s, confirmed_by=%s, confirmed_at=%s, warehouse_id=%s WHERE id=%s",
                            (qty_needed, confirmed_by, now_vn(), wh_id, item["id"])
                        )
                        done += 1
                # commit của batch này đã xảy ra khi `with db()` thoát

                _admin_bulk_confirm_progress.update({
                    "done": done, "auto_adjusted": auto_adjusted,
                    "message": f"Đã xử lý {done}/{total} dòng ({auto_adjusted} sp bù tồn)"
                })

            _admin_bulk_confirm_progress.update({
                "running": False,
                "done": done, "auto_adjusted": auto_adjusted,
                "done_at": now_hcm().strftime("%H:%M:%S"),
                "message": f"✅ Xong: xác nhận {done}/{total} dòng" +
                           (f", bù tồn {auto_adjusted} sp" if auto_adjusted else ""),
            })
            log.info("[admin_bulk_confirm] Xong: %d/%d, bù tồn %d", done, total, auto_adjusted)
        except Exception as e:
            log.exception("[admin_bulk_confirm] Lỗi")
            _admin_bulk_confirm_progress.update({
                "running": False,
                "done_at": now_hcm().strftime("%H:%M:%S"),
                "errors_count": _admin_bulk_confirm_progress.get("errors_count", 0) + 1,
                "errors": (_admin_bulk_confirm_progress.get("errors") or [])[-4:] + [str(e)[:200]],
                "message": f"❌ Lỗi: {e}",
            })
        finally:
            _redis_lock_release(_BULK_CONFIRM_REDIS_KEY, _bc_redis_tok)
            try:
                _ADMIN_BULK_CONFIRM_LOCK.release()
            except RuntimeError:
                pass

    threading.Thread(target=_run, daemon=True, name="wh-admin-bulk-confirm").start()
    flash("⏳ Đang tự động xuất kho toàn bộ — chạy nền, bạn có thể thao tác bình thường. Xem tiến độ ở banner phía trên.", "info")
    return redirect(url_for(".outbound_list"))


@bp.route("/outbound/admin-bulk-confirm/progress")
def outbound_admin_bulk_confirm_progress():
    if session.get("role") != "admin":
        abort(403)
    return jsonify(_admin_bulk_confirm_progress)


# ─────────────────────────────────────────
# IN ĐƠN HÀNG LOẠT
# ─────────────────────────────────────────

@bp.route("/in-don")
def in_don_list():
    """Tổng hợp & In đơn hàng loạt — tất cả shops, không lọc theo shop."""
    role    = session.get("role", "staff")
    user_id = session.get("user_id")
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))

    date_from   = request.args.get("date_from", "").strip()
    date_to     = request.args.get("date_to", "").strip()
    carrier_f   = request.args.get("carrier", "").strip()
    shop_f      = request.args.get("shop", "").strip()
    status_f    = request.args.get("status", "all").strip()   # all | waiting | pre_confirmed | pending
    search_q    = request.args.get("q", "").strip()
    page        = max(1, int(request.args.get("page", 1)))
    per_page    = 100

    # Admin/manager xem tất cả; staff chỉ xem shop mình
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        # ── Build WHERE ──────────────────────────────────────────
        where_parts: list = []
        params: list = []

        if allowed is not None:
            where_parts.append("shop_name = ANY(%s)"); params.append(allowed)

        if date_from:
            where_parts.append("COALESCE(carrier_picked_up_at, order_inserted_at) >= %s")
            params.append(date_from)
        if date_to:
            dt_end = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            where_parts.append("COALESCE(carrier_picked_up_at, order_inserted_at) < %s")
            params.append(dt_end)

        if carrier_f:
            where_parts.append("carrier_name ILIKE %s"); params.append(f"%{carrier_f}%")

        if shop_f:
            where_parts.append("shop_name = %s"); params.append(shop_f)

        if search_q:
            where_parts.append("(order_code ILIKE %s OR tracking_code ILIKE %s)")
            params.extend([f"%{search_q}%", f"%{search_q}%"])

        if status_f == "waiting":
            where_parts.append("pancake_status = 'waiting'")
            where_parts.append("status = 'pending'")
        elif status_f == "pre_confirmed":
            where_parts.append("status = 'pre_confirmed'")
        elif status_f == "pending":
            where_parts.append("pancake_status IN ('shipped','received')")
            where_parts.append("status = 'pending'")
        elif status_f == "confirmed":
            where_parts.append("status = 'confirmed'")
        else:
            # all — loại trừ cancelled
            where_parts.append("pancake_status NOT IN ('cancelled','returned')")

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # ── Count total orders ────────────────────────────────────
        total_orders = (conn.execute(f"""
            SELECT COUNT(DISTINCT order_code) FROM wh_outbound_requests {where_sql}
        """, params).fetchone() or {}).get("count", 0)

        total_pages = max(1, -(-total_orders // per_page))
        offset = (page - 1) * per_page

        # ── Fetch orders (grouped by order_code) ─────────────────
        rows = conn.execute(f"""
            SELECT
                order_code,
                MAX(shop_name) as shop_name,
                MAX(carrier_name) as carrier_name,
                MAX(tracking_code) as tracking_code,
                MAX(pancake_status) as pancake_status,
                MAX(status) as status,
                MAX(COALESCE(carrier_picked_up_at, order_inserted_at)) as order_date,
                COUNT(*) as item_count,
                SUM(qty_ordered) as total_qty,
                STRING_AGG(DISTINCT COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)'), ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)')) as products_preview
            FROM wh_outbound_requests
            {where_sql}
            GROUP BY order_code
            ORDER BY MAX(COALESCE(carrier_picked_up_at, order_inserted_at)) DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, params + [per_page, offset]).fetchall()

        orders = [dict(r) for r in rows]

        # ── Danh sách ĐVVC để filter ──────────────────────────────
        carrier_rows = conn.execute("""
            SELECT DISTINCT carrier_name FROM wh_outbound_requests
            WHERE carrier_name IS NOT NULL AND carrier_name != ''
            ORDER BY carrier_name
        """).fetchall()
        carriers = [r["carrier_name"] for r in carrier_rows]

        # ── Danh sách Shop để filter (giới hạn theo quyền) ────────
        shop_where = ""
        shop_params: list = []
        if allowed is not None:
            shop_where = "WHERE shop_name = ANY(%s)"
            shop_params.append(allowed)
        shop_rows = conn.execute(f"""
            SELECT DISTINCT shop_name FROM wh_outbound_requests
            {shop_where}
            {'AND' if shop_where else 'WHERE'} shop_name IS NOT NULL AND shop_name != ''
            ORDER BY shop_name
        """, shop_params).fetchall()
        shops = [r["shop_name"] for r in shop_rows]

    return render_template("kho_vat_ly/in_don.html",
        orders=orders,
        carriers=carriers,
        shops=shops,
        date_from=date_from, date_to=date_to,
        carrier_f=carrier_f, shop_f=shop_f, status_f=status_f, search_q=search_q,
        page=page, total_pages=total_pages, total_orders=total_orders,
        per_page=per_page,
    )


@bp.route("/in-don/print")
def in_don_print():
    """Trang in đơn — printable HTML (mở tab mới, Ctrl+P)."""
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))

    order_codes_raw = request.args.get("orders", "")
    order_codes = [c.strip() for c in order_codes_raw.split(",") if c.strip()]
    print_mode  = request.args.get("mode", "full")  # full | summary

    if not order_codes:
        return "<p>Không có đơn nào được chọn.</p>", 400

    with db() as conn:
        placeholders = ",".join(["%s"] * len(order_codes))
        items = conn.execute(f"""
            SELECT
                r.order_code, r.shop_name, r.carrier_name, r.tracking_code,
                r.pancake_status, r.status,
                r.product_id, r.product_sku, r.product_name, r.qty_ordered,
                COALESCE(p.sku, '') as real_sku,
                COALESCE(p.name, r.product_name, r.product_sku, '(trống)') as real_name,
                COALESCE(r.carrier_picked_up_at, r.order_inserted_at) as order_date,
                r.note
            FROM wh_outbound_requests r
            LEFT JOIN wh_products p ON p.id = r.product_id
            WHERE r.order_code IN ({placeholders})
            ORDER BY r.order_code, COALESCE(p.name, r.product_name)
        """, order_codes).fetchall()

        items = [dict(i) for i in items]

    if not items:
        return "<p>Không tìm thấy dữ liệu đơn hàng.</p>", 404

    # ── Nhóm theo order_code ─────────────────────────────────────
    from collections import OrderedDict, defaultdict
    orders_map = OrderedDict()
    for it in items:
        oc = it["order_code"]
        if oc not in orders_map:
            orders_map[oc] = {
                "order_code": oc,
                "shop_name":  it["shop_name"],
                "carrier_name": it["carrier_name"] or "—",
                "tracking_code": it["tracking_code"] or "—",
                "order_date": it["order_date"],
                "status": it["status"],
                "pancake_status": it["pancake_status"],
                "items": []
            }
        orders_map[oc]["items"].append({
            "name": it["real_name"],
            "sku":  it["real_sku"] or it["product_sku"] or "",
            "qty":  it["qty_ordered"] or 0,
        })

    # ── Tổng hợp hàng hóa (gộp theo tên sản phẩm) ───────────────
    summary = defaultdict(lambda: {"name": "", "sku": "", "qty": 0})
    for it in items:
        key = it["real_name"]
        summary[key]["name"] = it["real_name"]
        summary[key]["sku"]  = it["real_sku"] or it["product_sku"] or ""
        summary[key]["qty"] += (it["qty_ordered"] or 0)
    summary_list = sorted(summary.values(), key=lambda x: x["name"])

    printed_at = now_vn().strftime("%d/%m/%Y %H:%M")

    return render_template("kho_vat_ly/in_don_print.html",
        orders=list(orders_map.values()),
        summary=summary_list,
        print_mode=print_mode,
        printed_at=printed_at,
        total_orders=len(orders_map),
        total_items=sum(sum(i["qty"] for i in o["items"]) for o in orders_map.values()),
    )


# ─────────────────────────────────────────
# BÀN GIAO ĐƠN CHO ĐVVC
# ─────────────────────────────────────────

@bp.route("/ban-giao")
def ban_giao_list():
    """Bàn giao đơn — quét nhiều đơn, in biên bản bàn giao cho ĐVVC ký."""
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))
    role    = session.get("role", "staff")
    user_id = session.get("user_id")
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        where_parts: list = ["status = 'pre_confirmed'"]
        params: list = []
        if allowed is not None:
            where_parts.append("shop_name = ANY(%s)"); params.append(allowed)
        where_sql = "WHERE " + " AND ".join(where_parts)

        orders = conn.execute(f"""
            SELECT
                order_code,
                MAX(shop_name)       AS shop_name,
                MAX(carrier_name)    AS carrier_name,
                MAX(tracking_code)   AS tracking_code,
                MAX(pre_confirmed_at) AS pre_confirmed_at,
                COUNT(*)             AS item_count,
                SUM(qty_ordered)     AS total_qty,
                STRING_AGG(
                    DISTINCT COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)'),
                    ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)')
                ) AS products_preview
            FROM wh_outbound_requests
            {where_sql}
            GROUP BY order_code
            ORDER BY MAX(pre_confirmed_at) DESC NULLS LAST
        """, params).fetchall()
        orders = [dict(r) for r in orders]

    can_write = role in _WH_WRITE_ROLES
    current_username = session.get("username", "")
    return render_template("kho_vat_ly/ban_giao.html",
        orders=orders,
        can_write=can_write,
        current_username=current_username,
    )


@bp.route("/ban-giao/lookup")
def ban_giao_lookup():
    """API: tìm đơn pre_confirmed theo mã quét (tracking hoặc order code)."""
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Chưa đăng nhập"}), 401
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Thiếu mã"})
    role    = session.get("role", "staff")
    user_id = session.get("user_id")
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        where_parts = [
            "status = 'pre_confirmed'",
            "(tracking_code = %s OR order_code = %s)",
        ]
        params: list = [code, code]
        if allowed is not None:
            where_parts.append("shop_name = ANY(%s)"); params.append(allowed)
        where_sql = "WHERE " + " AND ".join(where_parts)

        rows = conn.execute(f"""
            SELECT order_code,
                   MAX(shop_name)    AS shop_name,
                   MAX(carrier_name) AS carrier_name,
                   MAX(tracking_code) AS tracking_code,
                   COUNT(*)          AS item_count,
                   SUM(qty_ordered)  AS total_qty,
                   STRING_AGG(
                       DISTINCT COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), ''),
                       ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '')
                   ) AS products_preview
            FROM wh_outbound_requests {where_sql}
            GROUP BY order_code LIMIT 5
        """, params).fetchall()

        if not rows:
            # Thử tìm ở trạng thái khác để báo lỗi cụ thể
            other = conn.execute("""
                SELECT status, pancake_status FROM wh_outbound_requests
                WHERE tracking_code = %s OR order_code = %s LIMIT 1
            """, (code, code)).fetchone()
            if other:
                st = other["status"]
                if st == "confirmed":
                    return jsonify({"ok": False, "error": f"Đơn '{code}' đã xuất kho rồi."})
                elif st == "pending":
                    return jsonify({"ok": False, "error": f"Đơn '{code}' đang chờ ĐVVC lấy (chưa quét chuẩn bị)."})
                else:
                    return jsonify({"ok": False, "error": f"Đơn '{code}' trạng thái: {st}."})
            return jsonify({"ok": False, "error": f"Không tìm thấy đơn '{code}' ở trạng thái Đã chuẩn bị."})

        results = [dict(r) for r in rows]
        return jsonify({"ok": True, "orders": results})


@bp.route("/ban-giao/print")
def ban_giao_print():
    """In biên bản bàn giao đơn hàng cho ĐVVC."""
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))

    order_codes_raw = request.args.get("orders", "")
    carrier_name    = request.args.get("carrier", "").strip() or "ĐVVC"
    order_codes = [c.strip() for c in order_codes_raw.split(",") if c.strip()]
    if not order_codes:
        return "<p>Không có đơn nào.</p>", 400

    with db() as conn:
        placeholders = ",".join(["%s"] * len(order_codes))
        rows = conn.execute(f"""
            SELECT
                r.order_code,
                MAX(r.shop_name)    AS shop_name,
                MAX(r.carrier_name) AS carrier_name,
                MAX(r.tracking_code) AS tracking_code,
                COUNT(*)            AS item_count,
                SUM(r.qty_ordered)  AS total_qty,
                STRING_AGG(
                    DISTINCT COALESCE(p.name, NULLIF(r.product_name,''), '(trống)'),
                    ', ' ORDER BY COALESCE(p.name, NULLIF(r.product_name,''), '(trống)')
                ) AS products_preview
            FROM wh_outbound_requests r
            LEFT JOIN wh_products p ON p.id = r.product_id
            WHERE r.order_code IN ({placeholders})
            GROUP BY r.order_code
            ORDER BY MAX(r.tracking_code)
        """, order_codes).fetchall()

        orders = [dict(r) for r in rows]

    if not orders:
        return "<p>Không tìm thấy đơn.</p>", 404

    total_orders = len(orders)
    total_qty    = sum(int(o["total_qty"] or 0) for o in orders)
    printed_at   = now_hcm().strftime("%d/%m/%Y %H:%M")
    staff_name   = session.get("username", "")

    # Lấy carrier từ đơn nếu không truyền
    if carrier_name == "ĐVVC":
        carriers = list({o["carrier_name"] for o in orders if o["carrier_name"]})
        if carriers:
            carrier_name = " / ".join(carriers)

    with db() as conn2:
        tmpl = _get_print_template(conn2, "ban_giao_shipper")
    return render_template("kho_vat_ly/ban_giao_print.html",
        orders=orders,
        carrier_name=carrier_name,
        total_orders=total_orders,
        total_qty=total_qty,
        printed_at=printed_at,
        staff_name=staff_name,
        tmpl=tmpl,
    )


@bp.route("/outbound/phieu-xuat-kho")
def outbound_phieu_xuat_kho():
    """In phiếu xuất kho cho các đơn đã chuẩn bị (pre_confirmed).
    Truyền ?orders=CODE1,CODE2,... hoặc ?all=1 để in tất cả pre_confirmed.
    """
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))

    shop_filter = (request.args.get("shop") or "").strip()

    with db() as conn:
        if request.args.get("all"):
            # In toàn bộ pre_confirmed
            parts = ["status = 'pre_confirmed'"]
            params: list = []
            if shop_filter:
                parts.append("shop_name = %s")
                params.append(shop_filter)
            where = "WHERE " + " AND ".join(parts)
            order_rows = conn.execute(f"""
                SELECT DISTINCT order_code FROM wh_outbound_requests {where}
            """, params).fetchall()
            order_codes = [r["order_code"] for r in order_rows]
        else:
            raw = request.args.get("orders", "")
            order_codes = [c.strip() for c in raw.split(",") if c.strip()]

        if not order_codes:
            return "<p>Không có đơn nào để in.</p>", 400

        placeholders = ",".join(["%s"] * len(order_codes))
        # Lấy chi tiết từng dòng (sản phẩm) trong đơn
        rows = conn.execute(f"""
            SELECT
                r.order_code,
                r.shop_name,
                r.tracking_code,
                r.carrier_name,
                r.product_name,
                r.product_sku,
                r.qty_ordered,
                r.pre_confirmed_at,
                r.pre_confirmed_by,
                r.warehouse_id,
                COALESCE(p.name, r.product_name) AS sp_name,
                COALESCE(p.sku,  r.product_sku)  AS sp_sku,
                COALESCE(p.unit, 'cái')           AS sp_unit,
                w.name AS wh_name
            FROM wh_outbound_requests r
            LEFT JOIN wh_products p ON p.id = r.product_id
            LEFT JOIN wh_warehouses w ON w.id = r.warehouse_id
            WHERE r.order_code IN ({placeholders})
              AND r.status = 'pre_confirmed'
            ORDER BY r.order_code, r.product_name
        """, order_codes).fetchall()

    if not rows:
        return "<p>Không tìm thấy đơn nào ở trạng thái Đã chuẩn bị.</p>", 404

    # Nhóm theo order_code
    from collections import OrderedDict
    orders_dict: dict = OrderedDict()
    for r in rows:
        code = r["order_code"]
        if code not in orders_dict:
            orders_dict[code] = {
                "order_code": code,
                "shop_name": r["shop_name"],
                "tracking_code": r["tracking_code"] or "",
                "carrier_name": r["carrier_name"] or "—",
                "pre_confirmed_at": (r["pre_confirmed_at"] or "")[:16],
                "pre_confirmed_by": r["pre_confirmed_by"] or "",
                "wh_name": r["wh_name"] or "",
                "products": [],
                "total_qty": 0,
            }
        orders_dict[code]["products"].append({
            "sp_name": r["sp_name"] or r["product_name"] or "?",
            "sp_sku": r["sp_sku"] or r["product_sku"] or "",
            "sp_unit": r["sp_unit"] or "cái",
            "qty": int(r["qty_ordered"] or 0),
        })
        orders_dict[code]["total_qty"] += int(r["qty_ordered"] or 0)

    orders = list(orders_dict.values())
    total_orders = len(orders)
    total_qty = sum(o["total_qty"] for o in orders)
    printed_at = now_hcm().strftime("%d/%m/%Y %H:%M")
    staff_name = session.get("username", "")

    with db() as conn2:
        tmpl = _get_print_template(conn2, "ban_giao_shipper")

    return render_template("kho_vat_ly/phieu_xuat_kho.html",
        orders=orders,
        total_orders=total_orders,
        total_qty=total_qty,
        printed_at=printed_at,
        staff_name=staff_name,
        shop_filter=shop_filter,
        tmpl=tmpl,
    )


# ─────────────────────────────────────────
# CANCEL PRE_CONFIRMED
# ─────────────────────────────────────────

@bp.route("/outbound/cancel-pre-confirmed", methods=["POST"])
def outbound_cancel_pre_confirmed():
    """Huỷ đánh dấu chuẩn bị → trả đơn về trạng thái Chờ chuyển hàng."""
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Chưa đăng nhập"}), 401
    role = session.get("role", "staff")
    if role not in _WH_WRITE_ROLES:
        return jsonify({"ok": False, "error": "Không có quyền"}), 403

    data = request.get_json(force=True, silent=True) or {}
    order_code = (data.get("order_code") or "").strip()
    if not order_code:
        return jsonify({"ok": False, "error": "Thiếu order_code"})

    with db() as conn:
        affected = conn.execute("""
            UPDATE wh_outbound_requests
            SET status = 'pending',
                pre_confirmed_at = NULL,
                pre_confirmed_by = NULL
            WHERE order_code = %s AND status = 'pre_confirmed'
        """, (order_code,))
        conn.execute("COMMIT")
        cnt = affected.rowcount if hasattr(affected, "rowcount") else 0

    if cnt == 0:
        # Thử kiểm tra xem đơn có tồn tại không
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hoặc đơn không ở trạng thái Đã chuẩn bị"})

    return jsonify({"ok": True, "cancelled": cnt, "order_code": order_code})


# ─────────────────────────────────────────
# XUẤT KHO NỘI BỘ
# ─────────────────────────────────────────

def _next_transfer_code(conn) -> str:
    """Sinh mã phiếu xuất kho nội bộ: NB-YYYYMMDD-NNN"""
    from datetime import datetime as _dt
    prefix = "NB-" + _dt.now().strftime("%Y%m%d") + "-"
    row = conn.execute("""
        SELECT code FROM wh_internal_transfers
        WHERE code LIKE %s ORDER BY code DESC LIMIT 1
    """, (prefix + "%",)).fetchone()
    if row:
        seq = int(row["code"].split("-")[-1]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


@bp.route("/outbound/xuat-kho-noi-bo")
def internal_transfer_list():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))
    role    = session.get("role", "staff")
    user_id = session.get("user_id")
    denied = _deny_by_perm("kvl_xk_noi_bo")
    if denied: return denied

    with db() as conn:
        warehouses = conn.execute("SELECT id, name FROM wh_warehouses ORDER BY id").fetchall()
        # Tồn theo TỪNG kho (gom cả dòng base lẫn biến thể) → JS render theo kho nguồn user chọn.
        # Trước đây hardcode warehouse_id=1 → chiều Tiên Cầu → Vĩnh Xá luôn báo 0.
        products_raw = conn.execute("""
            SELECT p.id, p.name, p.sku, p.unit
            FROM wh_products p
            ORDER BY p.name
        """).fetchall()
        qty_rows = conn.execute("""
            SELECT product_id, warehouse_id, COALESCE(SUM(qty), 0) AS qty
            FROM wh_inventory
            GROUP BY product_id, warehouse_id
        """).fetchall()
        qty_by_product = {}
        for r in qty_rows:
            qty_by_product.setdefault(r["product_id"], {})[r["warehouse_id"]] = int(r["qty"] or 0)
        products = [
            {**dict(p), "qty_by_wh": qty_by_product.get(p["id"], {})}
            for p in products_raw
        ]
        variations = conn.execute("""
            SELECT vm.product_id, vm.pos_variation_id, vm.variant_name,
                   COALESCE(inv.qty, 0) AS qty, inv.warehouse_id
            FROM wh_variation_map vm
            LEFT JOIN wh_inventory inv ON inv.pos_variation_id = vm.pos_variation_id
            ORDER BY vm.product_id
        """).fetchall()
        transfers = conn.execute("""
            SELECT t.id, t.code, t.status, t.created_at, t.created_by, t.note,
                   wf.name AS from_name, wt.name AS to_name,
                   (SELECT SUM(qty) FROM wh_internal_transfer_items WHERE transfer_id = t.id) AS total_qty,
                   (SELECT COUNT(*) FROM wh_internal_transfer_items WHERE transfer_id = t.id) AS item_count
            FROM wh_internal_transfers t
            LEFT JOIN wh_warehouses wf ON wf.id = t.from_warehouse_id
            LEFT JOIN wh_warehouses wt ON wt.id = t.to_warehouse_id
            ORDER BY t.id DESC LIMIT 100
        """).fetchall()

    can_write = role in _WH_WRITE_ROLES
    return render_template("kho_vat_ly/xuat_kho_noi_bo.html",
        warehouses=[dict(r) for r in warehouses],
        products=[dict(r) for r in products],
        variations=[dict(r) for r in variations],
        transfers=[dict(r) for r in transfers],
        can_write=can_write,
        current_username=session.get("username", ""),
    )


@bp.route("/outbound/xuat-kho-noi-bo/create", methods=["POST"])
def internal_transfer_create():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Chưa đăng nhập"}), 401
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kvl_xk_noi_bo"):
        return jsonify({"ok": False, "error": "Không có quyền"}), 403

    data = request.get_json(force=True, silent=True) or {}
    from_wh  = int(data.get("from_warehouse_id") or 0)
    to_wh    = int(data.get("to_warehouse_id")   or 0)
    note     = (data.get("note") or "").strip()
    items    = data.get("items") or []
    username = session.get("username", "")

    if not from_wh or not to_wh:
        return jsonify({"ok": False, "error": "Chọn kho nguồn và kho đích"})
    if from_wh == to_wh:
        return jsonify({"ok": False, "error": "Kho nguồn và kho đích không được trùng"})
    if not items:
        return jsonify({"ok": False, "error": "Chưa có sản phẩm nào"})

    ts = now_vn()
    errors = []
    with db() as conn:
        # ── EXPLODE: SP đa biến thể mà UI gửi lên KHÔNG chỉ định variant
        # (pvid=None, vkey=None) → tự phân bổ FIFO qua các row variant ở kho nguồn,
        # giữ đúng phân bổ giữa các biến thể khi chuyển sang kho đích.
        # Trước fix này, pre-check chỉ match row variant_key=NULL (qty=0) → báo
        # "còn 0" dù tổng qty hiển thị trên UI có đủ.
        exploded = []
        for it in items:
            pid  = int(it.get("product_id") or 0)
            qty  = int(it.get("qty") or 0)
            vkey = (it.get("variant_key") or "").strip() or None
            pvid = (it.get("pos_variation_id") or "").strip() or None
            if pid and qty > 0 and not vkey and not pvid:
                # Luôn FIFO qua các row tồn thực tế ở kho nguồn — không cần variant match.
                # Kho nội bộ chỉ cần có hàng vật lý, không quan tâm biến thể.
                src_rows = conn.execute(
                    "SELECT pos_variation_id, variant_key, qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s AND qty > 0 "
                    "ORDER BY (variant_key IS NULL), id",
                    (pid, from_wh)
                ).fetchall()
                if src_rows:
                    total_avail = sum((r["qty"] or 0) for r in src_rows)
                    if qty > total_avail:
                        exploded.append(it)
                        continue
                    remaining = qty
                    for r in src_rows:
                        if remaining <= 0: break
                        take = min(r["qty"] or 0, remaining)
                        if take <= 0: continue
                        exploded.append({
                            **it,
                            "pos_variation_id": (r["pos_variation_id"] or "").strip() or None,
                            "variant_key": (r["variant_key"] or "").strip() or None,
                            "qty": take,
                        })
                        remaining -= take
                    continue
            exploded.append(it)
        items = exploded

        # ── PRE-CHECK: kiểm tra đủ tồn trước khi tạo phiếu ──
        # Tránh oversell silent (GREATEST(0, qty-x) trước đây che lỗi thiếu hàng).
        short = []
        normalized = []
        for it in items:
            pid  = int(it.get("product_id") or 0)
            qty  = int(it.get("qty") or 0)
            vkey = (it.get("variant_key") or "").strip() or None
            pvid = (it.get("pos_variation_id") or "").strip() or None
            pname = (it.get("product_name") or "").strip()
            psku  = (it.get("product_sku") or "").strip()
            punit = (it.get("product_unit") or "cái").strip()
            if not pid or qty <= 0:
                continue
            # Kiểm tra tổng tồn tại kho nguồn — không lọc theo biến thể.
            # Chuyển kho nội bộ chỉ cần có hàng vật lý, không cần biết là biến thể nào.
            qty_avail = (conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS t FROM wh_inventory "
                "WHERE product_id=%s AND warehouse_id=%s",
                (pid, from_wh)
            ).fetchone() or {}).get("t", 0)
            if qty > qty_avail:
                label = pname or psku or f"SP#{pid}"
                if vkey:
                    label += f" [{vkey}]"
                short.append(f"{label}: cần {qty}, còn {qty_avail}")
            normalized.append({
                "pid": pid, "qty": qty, "vkey": vkey, "pvid": pvid,
                "pname": pname, "psku": psku, "punit": punit,
                "qty_before_from": qty_avail,
            })

        if short:
            return jsonify({
                "ok": False,
                "error": "Kho nguồn không đủ tồn: " + "; ".join(short)
            })
        if not normalized:
            return jsonify({"ok": False, "error": "Không có mặt hàng hợp lệ"})

        code = _next_transfer_code(conn)
        conn.execute("""
            INSERT INTO wh_internal_transfers
                (code, from_warehouse_id, to_warehouse_id, status, note, created_by, created_at)
            VALUES (%s, %s, %s, 'completed', %s, %s, %s)
        """, (code, from_wh, to_wh, note, username, ts))
        tid = conn.execute("SELECT currval('wh_internal_transfers_id_seq') AS id").fetchone()["id"]

        for n in normalized:
            pid, qty = n["pid"], n["qty"]
            vkey, pvid = n["vkey"], n["pvid"]
            conn.execute("""
                INSERT INTO wh_internal_transfer_items
                    (transfer_id, product_id, pos_variation_id, variant_key,
                     product_name, product_sku, product_unit, qty)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (tid, pid, pvid, vkey, n["pname"], n["psku"], n["punit"], qty))

            # ── Trừ tồn kho nguồn (variant-aware) ──
            # FIX 2026-05-29: đọc qty của ĐÚNG row variant tại kho nguồn.
            # Trước đây dùng n["qty_before_from"] = SUM(qty) toàn product
            # → ghi đè row variant với (SUM − qty) → mỗi mẫu mã bị phình ≈ tổng product.
            # Pattern đối xứng với phần cộng vào kho đích bên dưới (dùng _get_variant_inv).
            inv_from, _ = _get_variant_inv(conn, pid, from_wh, pvid)
            qty_before_from = inv_from["qty"] if inv_from else 0
            qty_after_from  = qty_before_from - qty
            _upsert_inventory(conn, pid, from_wh, qty_after_from,
                              pos_variation_id=pvid, variant_key=vkey)
            conn.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after,
                   ref_order_id, note, created_at, pos_variation_id, created_by)
                VALUES ('internal_transfer_out', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (pid, from_wh, qty, qty_before_from, qty_after_from,
                  code, f"Chuyển nội bộ → kho {to_wh}", ts, pvid, username))

            # ── Cộng tồn kho đích (variant-aware) ──
            inv_to, _iv = _get_variant_inv(conn, pid, to_wh, pvid)
            qty_before_to = inv_to["qty"] if inv_to else 0
            qty_after_to  = qty_before_to + qty
            _upsert_inventory(conn, pid, to_wh, qty_after_to,
                              pos_variation_id=pvid, variant_key=vkey)
            conn.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after,
                   ref_order_id, note, created_at, pos_variation_id, created_by)
                VALUES ('internal_transfer_in', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (pid, to_wh, qty, qty_before_to, qty_after_to,
                  code, f"Chuyển nội bộ ← kho {from_wh}", ts, pvid, username))

        conn.execute("COMMIT")

    return jsonify({"ok": True, "code": code, "transfer_id": tid})


@bp.route("/outbound/xuat-kho-noi-bo/<int:transfer_id>/in")
def internal_transfer_print(transfer_id: int):
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))
    with db() as conn:
        t = conn.execute("""
            SELECT t.*, wf.name AS from_name, wt.name AS to_name
            FROM wh_internal_transfers t
            LEFT JOIN wh_warehouses wf ON wf.id = t.from_warehouse_id
            LEFT JOIN wh_warehouses wt ON wt.id = t.to_warehouse_id
            WHERE t.id = %s
        """, (transfer_id,)).fetchone()
        if not t:
            return "<p>Không tìm thấy phiếu.</p>", 404
        items = conn.execute("""
            SELECT i.*, p.name AS p_name, p.sku AS p_sku, p.unit AS p_unit
            FROM wh_internal_transfer_items i
            LEFT JOIN wh_products p ON p.id = i.product_id
            WHERE i.transfer_id = %s ORDER BY i.id
        """, (transfer_id,)).fetchall()

    printed_at = now_hcm().strftime("%d/%m/%Y %H:%M")
    with db() as conn2:
        tmpl = _get_print_template(conn2, "xuat_kho_noi_bo")
    return render_template("kho_vat_ly/phieu_xuat_kho_noi_bo.html",
        t=dict(t),
        items=[dict(r) for r in items],
        printed_at=printed_at,
        staff_name=session.get("username", ""),
        tmpl=tmpl,
    )


# ─────────────────────────────────────────
# HÀNG HOÀN
# ─────────────────────────────────────────

@bp.route("/returns")
def returns_list():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)
    status_filter = request.args.get("status", "da_hoan")
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    shop_filter = request.args.get("shop", "").strip()
    search_q = request.args.get("q", "").strip()
    staff_filter = request.args.get("staff", "").strip()  # NV đã xác nhận nhận hoàn
    # Filter theo kho hoàn (1=Vĩnh Xá, 6=Tiên Cầu). Fallback từ outbound nếu
    # phiếu pending chưa có warehouse_id (NV chưa confirm).
    kho_filter = request.args.get("kho", "").strip()
    try:
        kho_filter_int = int(kho_filter) if kho_filter else 0
    except ValueError:
        kho_filter_int = 0
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    with db() as conn:
        where_parts = []
        params: list = []
        # Restrict to allowed shops
        if allowed is not None:
            where_parts.append("shop_name = ANY(%s)"); params.append(allowed)
        # Filter theo NV đã xác nhận nhận hoàn (chỉ có ý nghĩa với items đã nhận)
        if staff_filter:
            where_parts.append("confirmed_by = %s"); params.append(staff_filter)
        # Ngày thực tế của hàng hoàn — chỉ dùng returned_at/signaled_at thật từ POS
        # KHÔNG fallback về created_at vì created_at = ngày ghi vào DB (không phải ngày hoàn)
        DATE_COL = "COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''))"
        if date_from:
            where_parts.append(f"{DATE_COL} >= %s"); params.append(date_from)
        if date_to:
            dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            where_parts.append(f"{DATE_COL} < %s"); params.append(dt_end.strftime("%Y-%m-%d"))
        if shop_filter:
            where_parts.append("shop_name = %s"); params.append(shop_filter)
        # Nếu có date filter: loại bỏ đơn không có ngày thực tế
        if date_from or date_to:
            where_parts.append(f"{DATE_COL} IS NOT NULL")
        # Tìm kiếm theo mã vận đơn hoặc mã đơn
        if search_q:
            where_parts.append("(tracking_code ILIKE %s OR order_code ILIKE %s OR order_id_external ILIKE %s)")
            params.extend([f"%{search_q}%", f"%{search_q}%", f"%{search_q}%"])
        RECEIVED_STATUSES = "('received_ok','received_partial','received_damaged','exception','received')"
        # Subquery tính warehouse hiệu lực: ưu tiên warehouse_id của phiếu hoàn,
        # fallback warehouse_id của đơn xuất gốc (qua order_id_external).
        # → Phiếu pending (chưa confirm, chưa có warehouse_id) vẫn lọc/hiển thị
        #   đúng kho theo đơn gốc.
        EFF_WH_SQL = ("COALESCE(MAX(warehouse_id), "
                      "(SELECT o.warehouse_id FROM wh_outbound_requests o "
                      " WHERE o.order_id_external = MAX(wh_return_receipts.order_id_external) "
                      " LIMIT 1))")
        having_parts = []
        if status_filter == "in_transit":
            where_parts.append("pancake_return_status = 4")
            having_parts.append("SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) > 0")
        elif status_filter in ("da_hoan", "pending"):
            where_parts.append("pancake_return_status = 5")
            having_parts.append("SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) > 0")
        elif status_filter == "received":
            having_parts.append("SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) = 0")
        if kho_filter_int:
            having_parts.append(f"{EFF_WH_SQL} = %s")
            params.append(kho_filter_int)
        having_sql = ("HAVING " + " AND ".join(having_parts)) if having_parts else ""

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        base_sql = f"FROM wh_return_receipts {where_sql} GROUP BY shop_id, order_code {having_sql}"

        total_orders = (conn.execute(
            f"SELECT COUNT(*) as c FROM (SELECT shop_id, order_code {base_sql}) sq", params
        ).fetchone() or {}).get("c", 0)

        orders = conn.execute(f"""
            SELECT order_code,
                   shop_id,
                   MAX(shop_name) as shop_name,
                   MAX(carrier_name) as carrier_name,
                   MAX(tracking_code) as tracking_code,
                   MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) as created_at,
                   MAX(warehouse_id) as warehouse_id,
                   {EFF_WH_SQL} as eff_warehouse_id,
                   COUNT(*) as item_count,
                   SUM(qty_expected) as total_expected,
                   SUM(received_good_qty) as total_good,
                   SUM(received_damaged_qty) as total_damaged,
                   SUM(missing_qty) as total_missing,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending_cnt,
                   SUM(CASE WHEN status IN {RECEIVED_STATUSES} THEN 1 ELSE 0 END) as received_cnt,
                   SUM(CASE WHEN status='received_damaged' THEN 1 ELSE 0 END) as damaged_cnt,
                   SUM(CASE WHEN status='exception' THEN 1 ELSE 0 END) as exception_cnt,
                   MAX(pancake_return_status) as pancake_return_status
            {base_sql}
            ORDER BY MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, (page - 1) * per_page]).fetchall()

        # Đếm tab bằng bộ lọc hiện tại (shop + date) nhưng không lọc status
        tab_where = []
        tab_params: list = []
        if date_from:
            tab_where.append(f"{DATE_COL} >= %s"); tab_params.append(date_from)
        if date_to:
            dt_end2 = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            tab_where.append(f"{DATE_COL} < %s"); tab_params.append(dt_end2.strftime("%Y-%m-%d"))
        if shop_filter:
            tab_where.append("shop_name = %s"); tab_params.append(shop_filter)
        if date_from or date_to:
            tab_where.append(f"{DATE_COL} IS NOT NULL")
        tab_where_sql = ("WHERE " + " AND ".join(tab_where)) if tab_where else ""

        # Filter kho cho 3 tab — áp HAVING giống main query, dùng EFF_WH_SQL
        # đã định nghĩa ở trên. Khi kho_filter_int=0 → không áp, giữ logic cũ.
        tab_having_kho = f"HAVING {EFF_WH_SQL} = %s" if kho_filter_int else ""
        tab_kho_params = [kho_filter_int] if kho_filter_int else []

        # Đang hoàn (Pancake status 4) — ĐVVC đang chuyển hàng về
        count_in_transit = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT shop_id, order_code
                FROM wh_return_receipts {tab_where_sql}
                {'AND' if tab_where_sql else 'WHERE'} pancake_return_status = 4
                  AND status = 'pending'
                GROUP BY shop_id, order_code
                {tab_having_kho}
            ) sq
        """, tab_params + tab_kho_params).fetchone() or {}).get("c", 0)
        # Đã nhận hoàn (tất cả items đã xử lý, không còn pending)
        count_received = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT shop_id, order_code,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pc
                FROM wh_return_receipts {tab_where_sql} GROUP BY shop_id, order_code
                {('HAVING SUM(CASE WHEN status=\'pending\' THEN 1 ELSE 0 END) = 0 AND ' + EFF_WH_SQL + ' = %s') if kho_filter_int else ''}
            ) sq {'' if kho_filter_int else 'WHERE pc = 0'}
        """, tab_params + tab_kho_params).fetchone() or {}).get("c", 0)
        # Đã hoàn (Pancake status=5) chờ nhập kho: luôn đếm trực tiếp từ dữ liệu sync.
        # Không dùng công thức trừ để tránh lệch badge so với danh sách đã sync.
        count_da_hoan = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT shop_id, order_code
                FROM wh_return_receipts {tab_where_sql}
                {'AND' if tab_where_sql else 'WHERE'} pancake_return_status = 5
                  AND status = 'pending'
                GROUP BY shop_id, order_code
                {tab_having_kho}
            ) sq
        """, tab_params + tab_kho_params).fetchone() or {}).get("c", 0)
        count_pending = count_da_hoan

        # Đếm đơn không có ngày thực (pending, chưa về kho thật sự)
        no_date_where = []
        no_date_params: list = []
        if shop_filter:
            no_date_where.append("shop_name = %s"); no_date_params.append(shop_filter)
        no_date_where.append(f"COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,'')) IS NULL")
        no_date_where_sql = "WHERE " + " AND ".join(no_date_where)
        count_no_date = (conn.execute(f"""
            SELECT COUNT(*) as c FROM (
                SELECT DISTINCT shop_id, order_code
                FROM wh_return_receipts {no_date_where_sql}
            ) sq
        """, no_date_params).fetchone() or {}).get("c", 0)

        if allowed is None:
            products = conn.execute(
                "SELECT id, sku, name, category FROM wh_products ORDER BY category, name"
            ).fetchall()
        else:
            products = conn.execute("""
                SELECT DISTINCT p.id, p.sku, p.name, p.category
                FROM wh_products p
                JOIN wh_shop_inventory si ON si.product_id = p.id
                JOIN wh_shops sh ON sh.id = si.shop_id
                WHERE sh.shop_name = ANY(%s)
                ORDER BY p.category, p.name
            """, (allowed,)).fetchall()
        if allowed is None:
            shops = conn.execute(
                "SELECT shop_key, shop_name FROM wh_shops WHERE status='active' ORDER BY shop_name"
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT shop_key, shop_name FROM wh_shops WHERE status='active' AND shop_name = ANY(%s) ORDER BY shop_name",
                (allowed,)
            ).fetchall()
        wh_rows = conn.execute("SELECT id, name FROM wh_warehouses WHERE status='active'").fetchall()
        wh_map = {r["id"]: r["name"] for r in wh_rows}

        # Danh sách NV đã xác nhận nhận hoàn (cho dropdown filter)
        staff_rows = conn.execute("""
            SELECT DISTINCT confirmed_by AS name
            FROM wh_return_receipts
            WHERE confirmed_by IS NOT NULL AND confirmed_by <> ''
            ORDER BY confirmed_by
        """).fetchall()
        staff_list = [r["name"] for r in staff_rows]

    can_write = role in _WH_WRITE_ROLES
    total_pages = max(1, (total_orders + per_page - 1) // per_page)
    return _rt("returns.html", shop_biz_map=_load_shop_biz_map(), orders=orders, status_filter=status_filter,
               date_from=date_from, date_to=date_to, shop_filter=shop_filter,
               search_q=search_q, wh_map=wh_map,
               shops=shops, page=page, total_pages=total_pages,
               total_orders=total_orders, per_page=per_page,
               count_pending=count_pending, count_received=count_received,
               count_no_date=count_no_date, products=products,
               count_in_transit=count_in_transit, count_da_hoan=count_da_hoan,
               can_write=can_write,
               staff_list=staff_list, staff_filter=staff_filter,
               kho_filter=kho_filter)


@bp.route("/returns/report-by-staff")
def returns_report_by_staff():
    """Báo cáo in các đơn 1 nhân viên đã xác nhận nhận hoàn.
    Query params: staff (bắt buộc), date_from, date_to, shop.
    """
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))
    staff = request.args.get("staff", "").strip()
    if not staff:
        return "<p>Thiếu tham số <code>staff</code>.</p>", 400
    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to", "").strip()
    shop_filter = request.args.get("shop", "").strip()

    DATE_COL = "COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''))"
    where = ["confirmed_by = %s"]
    params: list = [staff]
    if shop_filter:
        where.append("shop_name = %s"); params.append(shop_filter)
    if date_from:
        where.append(f"{DATE_COL} >= %s"); params.append(date_from)
    if date_to:
        dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        where.append(f"{DATE_COL} < %s"); params.append(dt_end.strftime("%Y-%m-%d"))
    where_sql = "WHERE " + " AND ".join(where)

    with db() as conn:
        orders = conn.execute(f"""
            SELECT order_code,
                   MAX(shop_name)         AS shop_name,
                   MAX(carrier_name)      AS carrier_name,
                   MAX(tracking_code)     AS tracking_code,
                   MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) AS received_at,
                   COUNT(*)               AS item_count,
                   SUM(qty_expected)      AS total_expected,
                   SUM(received_good_qty) AS total_good,
                   SUM(received_damaged_qty) AS total_damaged,
                   SUM(missing_qty)       AS total_missing,
                   STRING_AGG(DISTINCT
                       COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)'),
                       ', ' ORDER BY COALESCE(NULLIF(product_name,''), NULLIF(product_sku,''), '(trống)')
                   ) AS products_preview
            FROM wh_return_receipts
            {where_sql}
            GROUP BY shop_id, order_code
            ORDER BY MIN(COALESCE(NULLIF(signaled_at,''), NULLIF(returned_at,''), created_at)) DESC NULLS LAST
        """, params).fetchall()
        orders = [dict(r) for r in orders]

    total_good = sum((o.get("total_good") or 0) for o in orders)
    total_damaged = sum((o.get("total_damaged") or 0) for o in orders)
    total_missing = sum((o.get("total_missing") or 0) for o in orders)
    printed_at = now_hcm().strftime("%d/%m/%Y %H:%M")
    return render_template("kho_vat_ly/bao_cao_hoan_theo_nv.html",
        staff=staff, orders=orders,
        date_from=date_from, date_to=date_to, shop_filter=shop_filter,
        total_orders=len(orders),
        total_good=total_good, total_damaged=total_damaged, total_missing=total_missing,
        printed_at=printed_at)


@bp.route("/returns/by-product")
def returns_by_product():
    """Báo cáo Top sản phẩm bị hoàn nhiều nhất — group theo product (SKU/Tên).
    Filter: date_from, date_to, shop, kho, q, sort. Mặc định 30 ngày qua.
    Date dimension = ngày thực tế POS báo hoàn (signaled_at / returned_at).
    """
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))

    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    # Default 30 ngày qua
    today = now_hcm().date()
    default_from = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    default_to   = today.strftime("%Y-%m-%d")
    date_from = (request.args.get("date_from") or default_from).strip()
    date_to   = (request.args.get("date_to")   or default_to).strip()
    shop_filter = (request.args.get("shop") or "").strip()
    kho_filter  = (request.args.get("kho")  or "").strip()
    q           = (request.args.get("q")    or "").strip()
    sort        = (request.args.get("sort") or "qty_exp").strip()
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    PAGE_SIZE = 30

    try:
        kho_filter_int = int(kho_filter) if kho_filter else 0
    except ValueError:
        kho_filter_int = 0

    # Shops dropdown (theo quyền)
    with db() as conn:
        if allowed is None:
            shops = conn.execute(
                "SELECT shop_key, shop_name FROM wh_shops WHERE status='active' ORDER BY shop_name"
            ).fetchall()
        elif not allowed:
            shops = []
        else:
            shops = conn.execute(
                "SELECT shop_key, shop_name FROM wh_shops WHERE status='active' AND shop_name = ANY(%s) ORDER BY shop_name",
                (allowed,)
            ).fetchall()
        wh_rows = conn.execute("SELECT id, name FROM wh_warehouses WHERE status='active'").fetchall()
        wh_map = {r["id"]: r["name"] for r in wh_rows}

        # Nếu user không có shop nào → render rỗng
        if allowed is not None and not allowed:
            return _rt("returns_by_product.html",
                       rows=[], shops=shops, wh_map=wh_map,
                       total=0, page=1, total_pages=1, page_size=PAGE_SIZE,
                       date_from=date_from, date_to=date_to,
                       shop_filter=shop_filter, kho_filter=kho_filter,
                       q=q, sort=sort,
                       sum_orders=0, sum_qty_exp=0, sum_qty_good=0,
                       sum_qty_damaged=0, sum_qty_missing=0, sum_value=0)

        DATE_COL = "COALESCE(NULLIF(r.signaled_at,''), NULLIF(r.returned_at,''))"
        where_parts: list = []
        params: list = []
        if allowed is not None:
            where_parts.append("r.shop_name = ANY(%s)"); params.append(allowed)
        if shop_filter:
            where_parts.append("r.shop_name = %s"); params.append(shop_filter)
        if date_from:
            where_parts.append(f"{DATE_COL} >= %s"); params.append(date_from)
        if date_to:
            dt_end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            where_parts.append(f"{DATE_COL} < %s"); params.append(dt_end.strftime("%Y-%m-%d"))
        if date_from or date_to:
            where_parts.append(f"{DATE_COL} IS NOT NULL")
        if kho_filter_int:
            # Kho hiệu lực: warehouse_id phiếu hoàn, fallback từ outbound (đơn gốc)
            where_parts.append(
                "COALESCE(r.warehouse_id, "
                "(SELECT o.warehouse_id FROM wh_outbound_requests o "
                " WHERE o.order_id_external = r.order_id_external LIMIT 1)) = %s"
            )
            params.append(kho_filter_int)
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Sort whitelist (chống SQL injection qua param sort)
        sort_map = {
            "qty_exp":   "qty_exp DESC NULLS LAST",
            "orders":    "n_orders DESC, qty_exp DESC NULLS LAST",
            "value":     "value_returned DESC NULLS LAST",
            "good":      "qty_good DESC NULLS LAST",
            "damaged":   "qty_damaged DESC NULLS LAST",
            "missing":   "qty_missing DESC NULLS LAST",
        }
        order_by = sort_map.get(sort, sort_map["qty_exp"])

        # Group key: ưu tiên product_id; nếu null thì group theo SKU
        # COUNT DISTINCT (shop_id, order_code) — vì order_code không unique giữa shop
        sql = f"""
        WITH agg AS (
            SELECT
                r.product_id,
                COALESCE(NULLIF(r.product_sku,''), '') AS r_sku,
                MAX(NULLIF(r.product_name,'')) AS r_name,
                COUNT(DISTINCT (r.shop_id, r.order_code)) AS n_orders,
                SUM(r.qty_expected)         AS qty_exp,
                SUM(r.received_good_qty)    AS qty_good,
                SUM(r.received_damaged_qty) AS qty_damaged,
                SUM(r.missing_qty)          AS qty_missing
            FROM wh_return_receipts r
            {where_sql}
            GROUP BY r.product_id, COALESCE(NULLIF(r.product_sku,''), '')
        )
        SELECT
            agg.product_id,
            COALESCE(p.sku,  NULLIF(agg.r_sku,''),  '(không SKU)') AS sku,
            COALESCE(p.name, NULLIF(agg.r_name,''), '(không tên)') AS name,
            COALESCE(p.gia_ban, 0) AS gia_ban,
            agg.n_orders, agg.qty_exp, agg.qty_good, agg.qty_damaged, agg.qty_missing,
            COALESCE(p.gia_ban, 0) * agg.qty_exp AS value_returned
        FROM agg
        LEFT JOIN wh_products p ON p.id = agg.product_id
        ORDER BY {order_by}
        """
        rows = conn.execute(sql, params).fetchall()
        rows = [dict(r) for r in rows]

    # Filter search Python-side (sau aggregation — light)
    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in (r["sku"] or "").lower() or ql in (r["name"] or "").lower()]

    # Totals (đếm trên toàn bộ rows sau filter, trước phân trang)
    sum_orders      = sum(r.get("n_orders")     or 0 for r in rows)
    sum_qty_exp     = sum(r.get("qty_exp")      or 0 for r in rows)
    sum_qty_good    = sum(r.get("qty_good")     or 0 for r in rows)
    sum_qty_damaged = sum(r.get("qty_damaged")  or 0 for r in rows)
    sum_qty_missing = sum(r.get("qty_missing")  or 0 for r in rows)
    sum_value       = sum(r.get("value_returned") or 0 for r in rows)

    # Phân trang 30/trang
    total = len(rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * PAGE_SIZE
    rows = rows[start:start + PAGE_SIZE]

    return _rt("returns_by_product.html",
               rows=rows, shops=shops, wh_map=wh_map,
               total=total, page=page, total_pages=total_pages, page_size=PAGE_SIZE,
               date_from=date_from, date_to=date_to,
               shop_filter=shop_filter, kho_filter=kho_filter,
               q=q, sort=sort,
               sum_orders=sum_orders, sum_qty_exp=sum_qty_exp,
               sum_qty_good=sum_qty_good, sum_qty_damaged=sum_qty_damaged,
               sum_qty_missing=sum_qty_missing, sum_value=sum_value)


@bp.route("/returns/order/<int:shop_id>/<order_code>")
def returns_order_detail(shop_id, order_code):
    """Chi tiết 1 đơn hoàn — PHẢI filter cả shop_id vì order_code không unique
    giữa các shop (mỗi shop tự đánh số đơn từ 1). Nếu chỉ lọc order_code sẽ trộn
    đơn của nhiều shop lên cùng trang (bug đã fix 2026-06-05)."""
    with db() as conn:
        items = conn.execute("""
            SELECT r.*,
                   COALESCE(
                       NULLIF(p.sku, ''),
                       CASE WHEN r.product_sku ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN '' ELSE NULLIF(r.product_sku, '') END
                   ) as display_sku
            FROM wh_return_receipts r
            LEFT JOIN wh_products p ON p.id = r.product_id
            WHERE r.order_code=%s AND r.shop_id=%s ORDER BY r.id
        """, (order_code, shop_id)).fetchall()
        if not items:
            flash(f"Không tìm thấy đơn hoàn {order_code} cho shop_id={shop_id}", "danger")
            return redirect(url_for(".returns_list"))
    role = session.get("role", "staff")
    can_write = role in _WH_WRITE_ROLES
    return _rt("returns_order.html", order_code=order_code, items=items, can_write=can_write)


# Legacy URL (chỉ order_code) → redirect về URL có shop_id của phiếu mới nhất.
# Giữ cho các link cũ NV đã bookmark / lưu trong notification không bị 404.
@bp.route("/returns/order/<order_code>")
def returns_order_detail_legacy(order_code):
    with db() as conn:
        row = conn.execute("""
            SELECT shop_id FROM wh_return_receipts
            WHERE order_code=%s ORDER BY id DESC LIMIT 1
        """, (order_code,)).fetchone()
    if not row or not row.get("shop_id"):
        flash(f"Không tìm thấy đơn hoàn {order_code}", "danger")
        return redirect(url_for(".returns_list"))
    return redirect(url_for(".returns_order_detail",
                            shop_id=row["shop_id"], order_code=order_code))


@bp.route("/returns/order/<int:shop_id>/<order_code>/confirm-all", methods=["POST"])
def returns_order_confirm_all(shop_id, order_code):
    """Confirm toàn bộ hàng tốt của 1 đơn — PHẢI filter cả shop_id.
    Bug cũ chỉ filter order_code → có thể xác nhận nhầm đơn shop khác (cùng order_code)
    → cộng tồn ảo. Fix 2026-06-05."""
    denied = _deny_staff()
    if denied: return denied
    note = request.form.get("note", "Xác nhận toàn bộ").strip()
    with db() as conn:
        items = conn.execute(
            "SELECT * FROM wh_return_receipts WHERE order_code=%s AND shop_id=%s AND status='pending'",
            (order_code, shop_id)
        ).fetchall()
        if not items:
            flash("Không có mặt hàng nào cần xác nhận.", "info")
            return redirect(url_for(".returns_order_detail", shop_id=shop_id, order_code=order_code))
        # Cảnh báo nếu đơn đang là "đang hoàn" (status 4) — hàng chưa về đến shop
        in_transit_cnt = sum(1 for it in items if (it["pancake_return_status"] or 0) == 4)
        if in_transit_cnt > 0 and not request.form.get("force"):
            flash(f"⚠️ Đơn {order_code} đang ở trạng thái 'Đang hoàn' (ĐVVC chưa giao lại). "
                  "Hàng chưa về đến kho — hãy xác nhận sau khi nhận hàng thực tế.", "warning")
            return redirect(url_for(".returns_order_detail", shop_id=shop_id, order_code=order_code))
        done = 0
        for item in items:
            good_qty = item["qty_expected"]
            old_posted = item["good_ledger_posted_qty"] or 0
            delta = good_qty - old_posted
            product_id = item["product_id"]
            wh_id = _derive_return_wh_id(conn, item)
            pos_var_id = (item.get("pos_variation_id") or "").strip() or None
            if delta > 0 and product_id:
                inv, _is_var = _get_variant_inv(conn, product_id, wh_id, pos_var_id)
                qty_before = inv["qty"] if inv else 0
                qty_after = qty_before + delta
                _upsert_inventory(conn, product_id, wh_id, qty_after, pos_var_id)
                conn.execute("""
                    INSERT INTO wh_stock_movements
                      (type, product_id, warehouse_id, qty, qty_before, qty_after,
                       ref_order_id, note, created_at, pos_variation_id)
                    VALUES ('return_received', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (product_id, wh_id, delta, qty_before, qty_after,
                      order_code, note or f"Hoàn đơn {order_code}", now_vn(), pos_var_id))
            _confirmed_by = session.get("username") or session.get("full_name") or "?"
            conn.execute("""
                UPDATE wh_return_receipts
                SET status='received_ok',
                    received_good_qty=%s, received_damaged_qty=0, missing_qty=0,
                    good_ledger_posted_qty=%s, qty_received=%s, received_at=%s, note=%s,
                    confirmed_by=%s, warehouse_id=COALESCE(warehouse_id,%s)
                WHERE id=%s
            """, (good_qty, good_qty, good_qty, now_vn(), note, _confirmed_by, wh_id, item["id"]))
            done += 1
    flash(f"✅ Đã nhận {done} mặt hàng hoàn (toàn bộ hàng tốt) — đơn {order_code}.", "success")
    return redirect(url_for(".returns_order_detail", shop_id=shop_id, order_code=order_code))


@bp.route("/returns/scan-confirm", methods=["POST"])
def returns_scan_confirm():
    denied = _deny_staff()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    confirmed_by = (data.get("confirmed_by") or session.get("username") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Mã trống"}), 400

    with db() as conn:
        items = conn.execute("""
            SELECT * FROM wh_return_receipts
            WHERE status='pending' AND (order_code=%s OR tracking_code=%s)
        """, (code, code)).fetchall()
        if not items:
            done = (conn.execute("""
                SELECT COUNT(*) as c FROM wh_return_receipts
                WHERE (order_code=%s OR tracking_code=%s)
                  AND status IN ('received_ok','received_partial','received_damaged','exception')
            """, (code, code)).fetchone() or {}).get("c", 0)
            if done > 0:
                return jsonify({"ok": False, "already": True,
                                "error": f"Đơn hoàn này đã nhận rồi ({done} mục)."})

            # ── Case mới: tracking có ở outbound, POS chưa flag là hoàn (2026-06-10) ──
            # Trước đây các case này rơi xuống "Không tìm thấy đơn hoàn" → NV nhầm là
            # mã sai. Giờ check riêng:
            #   - cancelled  → AUTO-RECEIVE (xử lý y như đơn hoàn bình thường, cộng tồn nếu đã xuất)
            #   - shipped/received → message rõ ràng (POS chưa flag hoàn)
            in_outbound = conn.execute("""
                SELECT order_code, shop_name, pancake_status
                  FROM wh_outbound_requests
                 WHERE (order_code=%s OR tracking_code=%s)
                   AND pancake_status IN ('shipped','received','cancelled')
                 ORDER BY created_at DESC LIMIT 1
            """, (code, code)).fetchone()
            if in_outbound:
                sn = (in_outbound["shop_name"] or "").strip()
                oc = (in_outbound["order_code"] or "").strip()
                ps = (in_outbound["pancake_status"] or "").strip()
                shop_part = f" ({sn})" if sn else ""

                if ps == "cancelled":
                    # ─── AUTO-RECEIVE cho đơn HỦY ───
                    # Khi shipper đã lấy → POS hủy → hàng vẫn về kho. NV scan = nhận hoàn.
                    # Xử lý y hệt đơn hoàn bình thường: cộng tồn (nếu đã xuất confirmed) +
                    # tạo wh_return_receipts row 'received_ok' + log stock movement.
                    out_items = conn.execute("""
                        SELECT id, order_code, order_id_external, shop_id, shop_name,
                               product_id, product_sku, product_name,
                               qty_ordered, warehouse_id, pos_variation_id,
                               status, carrier_name
                          FROM wh_outbound_requests
                         WHERE (order_code=%s OR tracking_code=%s)
                           AND pancake_status='cancelled'
                         ORDER BY id ASC
                    """, (code, code)).fetchall()

                    received_count = 0
                    skipped_already = 0
                    for ob in out_items:
                        pid = ob["product_id"]
                        # Idempotent: đã có receipt cho product này từ auto_cancel → bỏ
                        exists = conn.execute("""
                            SELECT id FROM wh_return_receipts
                             WHERE order_code=%s AND product_id=%s
                               AND source='auto_cancel' AND status='received_ok'
                             LIMIT 1
                        """, (ob["order_code"], pid)).fetchone()
                        if exists:
                            skipped_already += 1
                            continue

                        qty = ob["qty_ordered"] or 0
                        wh_id = ob["warehouse_id"] or _DEFAULT_WAREHOUSE_ID
                        pos_var = (ob["pos_variation_id"] or "").strip() or None

                        # Cộng tồn chỉ khi đã xuất kho (status='confirmed')
                        if ob["status"] == "confirmed" and qty > 0 and wh_id and pid:
                            inv, _ = _get_variant_inv(conn, pid, wh_id, pos_var)
                            qty_before = inv["qty"] if inv else 0
                            qty_after = qty_before + qty
                            _upsert_inventory(conn, pid, wh_id, qty_after, pos_var)
                            conn.execute("""
                                INSERT INTO wh_stock_movements
                                  (type, product_id, warehouse_id, qty,
                                   qty_before, qty_after, ref_order_id,
                                   note, created_at, pos_variation_id)
                                VALUES ('return_received',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """, (pid, wh_id, qty, qty_before, qty_after,
                                  ob["order_code"],
                                  f"Hoàn từ đơn HỦY {ob['order_code']}",
                                  now_vn(), pos_var))

                        # Tạo return_receipts row received_ok
                        good_qty = qty if ob["status"] == "confirmed" else 0
                        conn.execute("""
                            INSERT INTO wh_return_receipts
                              (order_code, order_id_external, shop_id, shop_name,
                               product_id, product_sku, product_name,
                               qty_expected, qty_received, tracking_code, carrier_name,
                               status, received_good_qty, received_damaged_qty,
                               missing_qty, good_ledger_posted_qty,
                               received_at, confirmed_by, warehouse_id,
                               pos_variation_id, source, note, created_at)
                            VALUES (%s,%s,%s,%s, %s,%s,%s,
                                    %s,%s,%s,%s,
                                    'received_ok', %s, 0,
                                    0, %s,
                                    %s, %s, %s,
                                    %s, 'auto_cancel', %s, %s)
                        """, (ob["order_code"], ob["order_id_external"], ob["shop_id"],
                              ob["shop_name"], pid, ob["product_sku"], ob["product_name"],
                              qty, qty, code, ob["carrier_name"],
                              good_qty, good_qty,
                              now_vn(), confirmed_by, wh_id, pos_var,
                              f"Auto-receive đơn HỦY {ob['order_code']} (tracking {code})",
                              now_vn()))
                        received_count += 1

                    if received_count == 0 and skipped_already > 0:
                        return jsonify({"ok": False, "already": True,
                            "error": f"Đơn HỦY #{oc}{shop_part} đã nhận hoàn rồi ({skipped_already} mục)."})

                    return jsonify({"ok": True, "auto_cancelled_received": True,
                        "order_code": oc, "shop_name": sn, "items": received_count,
                        "message": f"✅ Đã nhận hoàn {received_count} mục từ đơn HỦY #{oc}{shop_part}",
                        "confirmed_by": confirmed_by})

                # shipped/received: chỉ báo message (giữ nguyên, không auto-receive)
                msg = f"⚠ Đơn #{oc}{shop_part} — POS chưa cập nhật trạng thái HOÀN"
                return jsonify({"ok": False, "pos_not_flagged": True, "error": msg})

            return jsonify({"ok": False, "error": "Không tìm thấy đơn hoàn."})

        order_code = items[0]["order_code"]
        shop_name = items[0]["shop_name"]
        confirmed = []

        for item in items:
            good_qty = item["qty_expected"]
            old_posted = item["good_ledger_posted_qty"] or 0
            delta = good_qty - old_posted
            product_id = item["product_id"]
            wh_id = _derive_return_wh_id(conn, item)
            pos_var_id = (item.get("pos_variation_id") or "").strip() or None

            if delta > 0 and product_id:
                inv, _is_var = _get_variant_inv(conn, product_id, wh_id, pos_var_id)
                qty_before = inv["qty"] if inv else 0
                qty_after = qty_before + delta
                _upsert_inventory(conn, product_id, wh_id, qty_after, pos_var_id)
                conn.execute("""
                    INSERT INTO wh_stock_movements
                      (type, product_id, warehouse_id, qty, qty_before, qty_after,
                       ref_order_id, note, created_at, pos_variation_id)
                    VALUES ('return_received', %s, %s, %s, %s, %s, %s, 'Quét mã vạch', %s, %s)
                """, (product_id, wh_id, delta, qty_before, qty_after, order_code, now_vn(), pos_var_id))
            conn.execute("""
                UPDATE wh_return_receipts
                SET status='received_ok',
                    received_good_qty=%s, received_damaged_qty=0, missing_qty=0,
                    good_ledger_posted_qty=%s, qty_received=%s, received_at=%s,
                    note='Quét mã vạch', confirmed_by=%s,
                    warehouse_id=COALESCE(warehouse_id,%s)
                WHERE id=%s
            """, (good_qty, good_qty, good_qty, now_vn(), confirmed_by, wh_id, item["id"]))
            # Lookup tên biến thể để trả về client
            vm_row = conn.execute("SELECT variant_name FROM wh_variation_map WHERE pos_variation_id=%s",
                                  (pos_var_id,)).fetchone() if pos_var_id else None
            vname = (vm_row["variant_name"] if vm_row else "") or ""
            confirmed.append({"product_name": item["product_name"],
                              "product_sku": item["product_sku"], "qty": good_qty,
                              "variant_name": vname})

    return jsonify({"ok": True, "order_code": order_code, "shop_name": shop_name,
                    "items_confirmed": len(confirmed), "products": confirmed,
                    "confirmed_by": confirmed_by})


@bp.route("/returns/sync", methods=["POST"])
def returns_sync():
    denied = _deny_staff()
    if denied: return denied
    from .wh_db import RETURNS_SYNC_LOCK
    days_back = max(1, min(int(request.form.get("days_back", 30)), 365))
    date_to = today_hcm()
    date_from = (now_hcm() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Kiểm tra nếu returns sync đang chạy → không khởi thêm thread
    if not RETURNS_SYNC_LOCK.acquire(blocking=False):
        flash("⚠️ Sync hàng hoàn đang chạy nền. Vui lòng đợi hoàn tất (1–5 phút) rồi thử lại.", "warning")
        return redirect(url_for(".returns_list"))
    RETURNS_SYNC_LOCK.release()

    _returns_sync_progress.update({
        "running": True, "shop_name": "", "shop_idx": 0, "shop_total": 0,
        "status": None, "page": 0, "fetched": 0, "inserted": 0,
        "date_from": date_from, "date_to": date_to,
        "started_at": now_hcm().strftime("%H:%M:%S"), "done_at": "", "errors": [],
    })

    def _progress_cb(shop_name="", shop_idx=0, shop_total=0, page=0,
                     fetched=0, status=None, inserted=0):
        _returns_sync_progress.update({
            "shop_name": shop_name, "shop_idx": shop_idx, "shop_total": shop_total,
            "page": page, "fetched": fetched, "status": status, "inserted": inserted,
        })

    def _run():
        try:
            log.info("[returns_sync] Bắt đầu sync hoàn %s → %s", date_from, date_to)
            result = sync_returns_pos(date_from, date_to, progress_cb=_progress_cb)
            _returns_sync_progress.update({
                "running": False,
                "inserted": result.get("inserted", 0),
                "done_at": now_hcm().strftime("%H:%M:%S"),
                "errors": result.get("errors", []),
            })
            log.info("[returns_sync] Xong: inserted=%d skipped=%d errors=%s",
                     result.get("inserted", 0), result.get("skipped", 0), result.get("errors", []))
        except Exception as e:
            _returns_sync_progress.update({"running": False, "done_at": now_hcm().strftime("%H:%M:%S"),
                                           "errors": [str(e)]})
            log.error("[returns_sync bg] %s", e)

    threading.Thread(target=_run, daemon=True, name="wh-returns-sync").start()
    flash(f"⏳ Đang sync hàng hoàn từ POS ({date_from} → {date_to}). "
          f"Quá trình mất 1–5 phút tuỳ số lượng đơn. Vui lòng refresh trang sau vài phút.", "info")
    return redirect(url_for(".returns_list"))


# ─────────────────────────────────────────
# ADMIN: BULK AUTO-CONFIRM NHẬN HOÀN
# ─────────────────────────────────────────
@bp.route("/returns/admin-bulk-confirm", methods=["POST"])
def returns_admin_bulk_confirm():
    """Admin bulk confirm hàng hoàn — chạy nền theo batch để tránh deadlock + timeout.
    Frontend poll /returns/admin-bulk-confirm/progress mỗi 1.5s.
    """
    if session.get("role") != "admin":
        abort(403)
    confirmed_by = session.get("username") or session.get("full_name") or "admin"

    if not _RETURNS_BULK_CONFIRM_LOCK.acquire(blocking=False):
        flash("Đang có 1 lượt admin bulk hoàn khác đang chạy. Đợi tý rồi thử lại.", "warning")
        return redirect(url_for(".returns_list"))
    _returns_redis_token = _redis_lock_acquire(_RETURNS_BULK_CONFIRM_REDIS_KEY, ttl=1800)
    if _returns_redis_token is None:
        _RETURNS_BULK_CONFIRM_LOCK.release()
        flash("Đang có 1 lượt admin bulk hoàn đang chạy ở worker khác. Đợi tý rồi thử lại.", "warning")
        return redirect(url_for(".returns_list"))
    _ret_redis_tok = _returns_redis_token

    _returns_bulk_confirm_progress.update({
        "running": True, "total": 0, "done": 0, "errors_count": 0,
        "started_at": now_vn(), "done_at": "", "message": "Đang đếm phiếu cần xác nhận...",
        "errors": [],
    })

    BATCH_SIZE = 100  # nhỏ hơn outbound (200) vì returns có thêm INSERT stock_movement

    def _worker():
        try:
            # Đếm tổng trước (transaction nhỏ, release ngay)
            with db() as conn:
                row = conn.execute("""
                    SELECT COUNT(*) AS c FROM wh_return_receipts
                    WHERE status='pending' AND pancake_return_status=5
                """).fetchone()
                total = int(row["c"] or 0)
            _returns_bulk_confirm_progress.update({
                "total": total,
                "message": f"Sẽ xử lý {total} phiếu hoàn theo batch {BATCH_SIZE}",
            })
            if total == 0:
                _returns_bulk_confirm_progress.update({
                    "running": False, "done_at": now_vn(),
                    "message": "Không có phiếu hoàn nào ở trạng thái 'đã hoàn' chờ xác nhận.",
                })
                return

            done = 0
            errors = []
            offset_id = 0  # cursor theo id để batch không bị skip khi UPDATE thay đổi WHERE
            while True:
                # Mỗi batch là 1 transaction riêng → giảm lock time, tránh deadlock
                with db() as conn:
                    items = conn.execute("""
                        SELECT * FROM wh_return_receipts
                        WHERE status='pending' AND pancake_return_status=5
                          AND id > %s
                        ORDER BY id
                        LIMIT %s
                    """, (offset_id, BATCH_SIZE)).fetchall()
                    if not items:
                        break

                    for item in items:
                        try:
                            product_id = item["product_id"]
                            good_qty = item["qty_expected"] or 0
                            if good_qty <= 0:
                                good_qty = item.get("qty_received") or 0

                            wh_id = item.get("warehouse_id") or None
                            if not wh_id and item.get("shop_id"):
                                wh_id = _get_shop_warehouse_id_by_id(conn, item["shop_id"])

                            if product_id and good_qty > 0:
                                var_id = (item.get("pos_variation_id") or "").strip() or None
                                inv_row, _is_var = _get_variant_inv(conn, product_id, wh_id, var_id)
                                qty_before = inv_row["qty"] if inv_row else 0
                                qty_after = qty_before + good_qty
                                _upsert_inventory(conn, product_id, wh_id, qty_after, pos_variation_id=var_id)
                                conn.execute("""
                                    INSERT INTO wh_stock_movements
                                      (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at, pos_variation_id)
                                    VALUES ('return_received', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """, (product_id, wh_id, good_qty, qty_before, qty_after,
                                      item["order_code"], "Admin bulk confirm hoàn", now_vn(), var_id))

                            conn.execute("""
                                UPDATE wh_return_receipts
                                SET status='received_ok',
                                    received_good_qty=%s, received_damaged_qty=0, missing_qty=0,
                                    good_ledger_posted_qty=%s, qty_received=%s,
                                    received_at=%s, confirmed_by=%s,
                                    warehouse_id=COALESCE(warehouse_id,%s)
                                WHERE id=%s
                            """, (good_qty, good_qty, good_qty, now_vn(), confirmed_by, wh_id, item["id"]))
                            done += 1
                            offset_id = max(offset_id, item["id"])
                        except Exception as e_item:
                            errors.append(f"id={item.get('id')}: {e_item}")
                            offset_id = max(offset_id, item["id"] or 0)

                _returns_bulk_confirm_progress.update({
                    "done": done,
                    "errors_count": len(errors),
                    "message": f"Đã xử lý {done}/{total} phiếu",
                    "errors": errors[-20:],
                })

            _returns_bulk_confirm_progress.update({
                "running": False,
                "done_at": now_vn(),
                "message": f"✅ Hoàn tất: xác nhận {done}/{total} phiếu hoàn"
                           + (f" — ⚠️ {len(errors)} lỗi" if errors else ""),
            })
        except Exception as exc:
            _returns_bulk_confirm_progress.update({
                "running": False,
                "done_at": now_vn(),
                "message": f"❌ Lỗi: {exc}",
            })
        finally:
            _redis_lock_release(_RETURNS_BULK_CONFIRM_REDIS_KEY, _ret_redis_tok)
            try:
                _RETURNS_BULK_CONFIRM_LOCK.release()
            except RuntimeError:
                pass

    threading.Thread(target=_worker, daemon=True, name="returns-admin-bulk").start()
    flash("⏳ Đang xác nhận hàng hoàn theo batch trong nền — không cần đợi, refresh sau 1-2 phút.", "info")
    return redirect(url_for(".returns_list"))


@bp.route("/returns/admin-bulk-confirm/progress")
def returns_admin_bulk_confirm_progress():
    return jsonify(dict(_returns_bulk_confirm_progress))


@bp.route("/api/returns-sync-progress")
def api_returns_sync_progress():
    """Trả về trạng thái sync hàng hoàn đang chạy."""
    p = dict(_returns_sync_progress)
    # Tạo status text dễ đọc
    if p.get("running"):
        st = p.get("status")
        st_label = {4: "đang hoàn", 5: "đã hoàn"}.get(st, "...")
        p["status_text"] = (
            f"Shop {p.get('shop_idx', 0)}/{p.get('shop_total', 0)}: "
            f"<strong>{p.get('shop_name', '')}</strong> — "
            f"Trạng thái {st_label} — Trang {p.get('page', 0)} "
            f"({p.get('fetched', 0)} đơn đã tải)"
        )
    elif p.get("done_at"):
        errs = p.get("errors") or []
        p["status_text"] = (
            f"✅ Xong lúc {p.get('done_at')} — "
            f"+{p.get('inserted', 0)} phiếu mới"
            + (f" — ⚠️ {len(errs)} lỗi" if errs else "")
        )
    else:
        p["status_text"] = ""
    return jsonify(p)


@bp.route("/api/outbound-sync-progress")
def api_outbound_sync_progress():
    """Trả về trạng thái sync xuất hàng đang chạy."""
    p = dict(_outbound_sync_progress)
    if p.get("running"):
        p["status_text"] = (
            f"Shop {p.get('shop_idx', 0)}/{p.get('shop_total', 0)}: "
            f"<strong>{p.get('shop_name', '')}</strong> — "
            f"{p.get('status_label', '...')} — "
            f"{p.get('fetched', 0)} đơn đã tải "
            f"(+{p.get('inserted', 0)} mới, cập nhật {p.get('updated', 0)})"
        )
    elif p.get("done_at"):
        errs = p.get("errors") or []
        p["status_text"] = (
            f"✅ Xong lúc {p.get('done_at')} — "
            f"+{p.get('inserted', 0)} đơn mới, cập nhật {p.get('updated', 0)}"
            + (f" — ⚠️ {len(errs)} lỗi" if errs else "")
        )
    else:
        p["status_text"] = ""
    return jsonify(p)


@bp.route("/api/full-sync", methods=["POST"])
def api_full_sync():
    """Sync TOÀN BỘ lịch sử: xuất hàng (all statuses) + hoàn hàng (từ 2020-01-01)."""
    now_vn = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d")

    def _run():
        try:
            log.info("[full-sync] Bắt đầu sync TOÀN BỘ xuất hàng...")
            res_ob = sync_outbound_for_date_range(now_vn, now_vn)
            log.info("[full-sync] Xuất hàng xong: inserted=%d updated=%d errors=%s",
                     res_ob.get("inserted", 0), res_ob.get("updated", 0), res_ob.get("errors", []))
            log.info("[full-sync] Bắt đầu sync TOÀN BỘ hoàn hàng từ 2020-01-01...")
            res_ret = sync_returns_pos("2020-01-01", now_vn)
            log.info("[full-sync] Hoàn hàng xong: inserted=%d skipped=%d errors=%s",
                     res_ret.get("inserted", 0), res_ret.get("skipped", 0), res_ret.get("errors", []))
        except Exception as e:
            log.error("[full-sync] Lỗi: %s", e)

    threading.Thread(target=_run, daemon=True, name="wh-full-sync").start()
    return jsonify({"ok": True, "message": "Full sync đang chạy nền. Kiểm tra logs để theo dõi tiến trình."})


@bp.route("/returns/create", methods=["POST"])
def returns_create():
    denied = _deny_staff()
    if denied: return denied
    order_code = request.form.get("order_code", "").strip()
    shop_name = request.form.get("shop_name", "").strip()
    product_id = request.form.get("product_id", "").strip()
    return_reason = request.form.get("return_reason", "").strip()
    qty_str = request.form.get("qty_expected", "1").strip()
    carrier_name = request.form.get("carrier_name", "").strip()
    if not product_id:
        flash("Vui lòng chọn sản phẩm.", "danger")
        return redirect(url_for(".returns_list"))
    try:
        qty = int(qty_str)
        if qty <= 0: raise ValueError
    except ValueError:
        flash("Số lượng phải là số nguyên dương.", "danger")
        return redirect(url_for(".returns_list"))
    with db() as conn:
        prod = conn.execute("SELECT sku, name FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            flash("Không tìm thấy sản phẩm.", "danger")
            return redirect(url_for(".returns_list"))
        conn.execute("""
            INSERT INTO wh_return_receipts
              (order_code, shop_name, product_id, product_sku, product_name,
               return_reason, carrier_name, qty_expected, source, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'manual','pending',%s)
        """, (order_code or f"HOAN-{now_vn()}", shop_name, product_id,
              prod["sku"], prod["name"], return_reason, carrier_name, qty, now_vn()))
    flash(f"✅ Đã tạo phiếu hoàn: {prod['name']} x{qty}.", "success")
    return redirect(url_for(".returns_list"))


def _derive_return_status(exp: int, g: int, d: int, m: int) -> str:
    if g + d + m != exp:
        return "pending"
    if m > 0: return "exception"
    if d > 0: return "received_damaged"
    if g == exp: return "received_ok"
    return "received_partial"


@bp.route("/returns/<int:receipt_id>/confirm", methods=["POST"])
def returns_confirm(receipt_id):
    denied = _deny_staff()
    if denied: return denied
    note = request.form.get("note", "").strip()
    try:
        good_qty = int(request.form.get("good_qty", "0") or 0)
        damaged_qty = int(request.form.get("damaged_qty", "0") or 0)
        missing_qty = int(request.form.get("missing_qty", "0") or 0)
        if good_qty < 0 or damaged_qty < 0 or missing_qty < 0:
            raise ValueError("Số lượng không được âm")
    except ValueError as e:
        flash(f"Số lượng không hợp lệ: {e}", "danger")
        return redirect(url_for(".returns_list"))

    with db() as conn:
        receipt = conn.execute(
            "SELECT * FROM wh_return_receipts WHERE id=%s", (receipt_id,)
        ).fetchone()
        if not receipt:
            flash("Không tìm thấy phiếu hoàn.", "danger")
            return redirect(url_for(".returns_list"))

        # Xác định kho nhận hoàn: đơn xuất → shop → kho mặc định
        wh_id_to_use = _derive_return_wh_id(conn, receipt)

        exp = receipt["qty_expected"]
        old_posted = receipt["good_ledger_posted_qty"] or 0
        order_code = receipt["order_code"]
        product_id = receipt["product_id"]
        # shop_id bắt buộc cho url_for sau khi fix route detail (2026-06-05) — luôn lấy từ receipt
        receipt_shop_id = receipt["shop_id"]
        if good_qty + damaged_qty + missing_qty != exp:
            flash(f"Tổng tốt + hỏng + thiếu phải bằng số lượng dự kiến ({exp}).", "danger")
            return redirect(url_for(".returns_order_detail", shop_id=receipt_shop_id, order_code=order_code))
        if good_qty < old_posted:
            flash(f"Không thể giảm SL tốt đã ghi vào kho ({old_posted}).", "danger")
            return redirect(url_for(".returns_order_detail", shop_id=receipt_shop_id, order_code=order_code))
        delta = good_qty - old_posted
        pos_var_id = (receipt.get("pos_variation_id") or "").strip() or None
        if delta > 0 and product_id:
            inv, _is_var = _get_variant_inv(conn, product_id, wh_id_to_use, pos_var_id)
            qty_before = inv["qty"] if inv else 0
            qty_after = qty_before + delta
            _upsert_inventory(conn, product_id, wh_id_to_use, qty_after, pos_var_id)
            conn.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after,
                   ref_order_id, note, created_at, pos_variation_id)
                VALUES ('return_received', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (product_id, wh_id_to_use, delta, qty_before, qty_after,
                  order_code, note or f"Hoàn đơn {order_code}", now_vn(), pos_var_id))
        new_status = _derive_return_status(exp, good_qty, damaged_qty, missing_qty)
        _confirmed_by = session.get("username") or session.get("full_name") or "?"
        conn.execute("""
            UPDATE wh_return_receipts
            SET status=%s, received_good_qty=%s, received_damaged_qty=%s, missing_qty=%s,
                good_ledger_posted_qty=%s, qty_received=%s, received_at=%s, note=%s,
                confirmed_by=%s, warehouse_id=COALESCE(warehouse_id,%s)
            WHERE id=%s
        """, (new_status, good_qty, damaged_qty, missing_qty,
              good_qty, good_qty, now_vn(), note, _confirmed_by, wh_id_to_use, receipt_id))

    status_label = {"received_ok": "hàng tốt", "received_damaged": "có hàng hỏng",
                    "received_partial": "nhận một phần", "exception": "thiếu hàng",
                    "pending": "chưa đủ"}.get(new_status, new_status)
    flash(f"✅ Đã xác nhận ({status_label}) — đơn {order_code}.", "success")
    return redirect(url_for(".returns_order_detail", shop_id=receipt_shop_id, order_code=order_code))


# ─────────────────────────────────────────
# LỊCH SỬ BIẾN ĐỘNG
# ─────────────────────────────────────────

@bp.route("/history")
def history():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)  # None = toàn bộ; list = giới hạn theo shop

    page = int(request.args.get("page", 1))
    per_page = 50
    offset = (page - 1) * per_page
    type_filter = request.args.get("type", "")
    search = request.args.get("q", "").strip()

    with db() as conn:
        where_parts = []
        params = []
        # Giới hạn theo shop được phép (nhân viên chỉ thấy shop của mình)
        # wh_stock_movements dùng shop_id FK → wh_shops, không có cột shop_name trực tiếp
        if allowed is not None:
            where_parts.append(
                "m.shop_id IN (SELECT id FROM wh_shops WHERE shop_name = ANY(%s))"
            ); params.append(allowed)
        if type_filter:
            where_parts.append("m.type=%s"); params.append(type_filter)
        if search:
            where_parts.append("(p.sku LIKE %s OR p.name LIKE %s OR m.ref_order_id LIKE %s)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        total = (conn.execute(
            f"SELECT COUNT(*) as c FROM wh_stock_movements m "
            f"JOIN wh_products p ON p.id=m.product_id {where_sql}", params
        ).fetchone() or {}).get("c", 0)
        rows = conn.execute(f"""
            SELECT m.*, p.sku, p.name as product_name, p.unit
            FROM wh_stock_movements m
            JOIN wh_products p ON p.id = m.product_id
            {where_sql}
            ORDER BY m.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset]).fetchall()

    type_labels = {
        "inbound": "Nhập hàng", "outbound_confirmed": "Xuất hàng",
        "pos_export": "Xuất → POS",
        "return_received": "Hàng hoàn", "adjust_up": "Điều chỉnh tăng",
        "adjust_down": "Điều chỉnh giảm", "opening": "Tồn đầu kỳ",
    }
    total_pages = max(1, (total + per_page - 1) // per_page)
    return _rt("history.html", rows=rows, page=page, total_pages=total_pages,
               total=total, type_filter=type_filter, search=search, type_labels=type_labels)


# ─────────────────────────────────────────
# KHO THEO SHOP
# ─────────────────────────────────────────

@bp.route("/shops")
def shops_list():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    view = request.args.get("view", "by_shop")
    with db() as conn:
        if allowed is None:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active' ORDER BY shop_key"
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active' AND shop_name = ANY(%s) ORDER BY shop_key",
                (allowed,)
            ).fetchall()
        shop_totals = {}
        shop_items_map = {}
        for sh in shops:
            wh_id = sh["warehouse_id"] if sh["warehouse_id"] else None
            # H1 fix: SUM tất cả biến thể trong cùng (product_id, warehouse_id)
            # để tránh JOIN nhân dòng + COALESCE chỉ lấy 1 row khi SP có nhiều BT.
            rows = conn.execute("""
                SELECT si.qty_pos, p.id as product_id, p.sku, p.name as product_name, p.category,
                       COALESCE((SELECT SUM(qty) FROM wh_inventory
                                 WHERE product_id=p.id AND warehouse_id=%s), 0) as qty_physical
                FROM wh_shop_inventory si
                JOIN wh_products p ON p.id = si.product_id
                WHERE si.shop_id=%s
                ORDER BY p.category, p.name
            """, (wh_id, sh["id"])).fetchall()
            shop_totals[sh["id"]] = sum(r["qty_pos"] for r in rows)
            shop_items_map[sh["id"]] = rows

        sync_row = conn.execute("SELECT MAX(synced_at) as last FROM wh_shop_inventory").fetchone()
        synced_at = (sync_row["last"] or "")[:16] if sync_row else None

        warehouses = conn.execute(
            "SELECT id, name, code FROM wh_warehouses WHERE is_active=TRUE ORDER BY name"
        ).fetchall()

        product_rows = []
        if view == "by_product":
            prods = conn.execute("""
                SELECT p.id, p.sku, p.name as product_name, p.category
                FROM wh_products p
                ORDER BY p.category, p.name
            """).fetchall()

            # H2 fix: SUM GROUP BY để cộng tổng biến thể cùng (pid, wh_id).
            # Trước đây setdefault[...][wh_id] = qty ghi đè → SP có biến thể mất tồn.
            inv_rows = conn.execute("""
                SELECT product_id, warehouse_id, COALESCE(SUM(qty),0) AS qty
                FROM wh_inventory
                WHERE warehouse_id IS NOT NULL
                GROUP BY product_id, warehouse_id
            """).fetchall()
            inv_map = {}
            for r in inv_rows:
                inv_map.setdefault(r["product_id"], {})[r["warehouse_id"]] = r["qty"]

            # Lấy tồn POS theo shop — {(shop_id, product_id): qty_pos}
            pos_rows = conn.execute(
                "SELECT shop_id, product_id, qty_pos FROM wh_shop_inventory WHERE qty_pos > 0"
            ).fetchall()
            pos_map = {(r["shop_id"], r["product_id"]): r["qty_pos"] for r in pos_rows}

            for pr in prods:
                wh_qtys = inv_map.get(pr["id"], {})
                total_physical = sum(wh_qtys.values())
                shop_qtys = {}
                for sh in shops:
                    q = pos_map.get((sh["id"], pr["id"]), 0)
                    if q > 0:
                        shop_qtys[sh["id"]] = q
                product_rows.append({
                    "product_id": pr["id"], "sku": pr["sku"], "product_name": pr["product_name"],
                    "category": pr["category"],
                    "total_physical": total_physical,
                    "wh_qtys": wh_qtys,
                    "shop_qtys": shop_qtys,
                })

    if allowed is not None and view == "by_product":
        product_rows = [pr for pr in product_rows if pr["shop_qtys"]]
    can_write = role in _WH_WRITE_ROLES
    # Map shop.id → column index trong bảng "Theo sản phẩm" (cần để nhóm filter chips)
    shop_col_idx = {sh["id"]: i for i, sh in enumerate(shops)}
    return _rt("shops.html", shops=shops, shop_totals=shop_totals,
               shop_items=shop_items_map, synced_at=synced_at,
               view=view, product_rows=product_rows, warehouses=warehouses,
               shop_col_idx=shop_col_idx,
               shop_biz_map=_load_shop_biz_map(),
               can_write=can_write)


@bp.route("/shops/<shop_key>")
def shop_detail(shop_key):
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        shop = conn.execute(
            "SELECT * FROM wh_shops WHERE shop_key=%s", (shop_key,)
        ).fetchone()
        if not shop:
            flash("Không tìm thấy shop.", "danger")
            return redirect(url_for(".shops_list"))
        if allowed is not None and shop["shop_name"] not in allowed:
            abort(403)
        items_raw = conn.execute("""
            SELECT si.product_id, si.qty_pos, p.sku, p.name as product_name, p.category
            FROM wh_shop_inventory si
            JOIN wh_products p ON p.id = si.product_id
            WHERE si.shop_id=%s AND si.qty_pos > 0
            ORDER BY si.qty_pos ASC
        """, (shop["id"],)).fetchall()

        # Lấy tất cả kho đang active
        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()

        # Lấy tồn kho vật lý PER-WAREHOUSE cho từng sản phẩm
        product_ids = [r["product_id"] for r in items_raw]
        inv_by_product = {}  # {product_id: {warehouse_id: qty}}
        if product_ids:
            # H3 fix: SUM GROUP BY để cộng tổng biến thể cùng (pid, wh_id).
            inv_rows = conn.execute(
                "SELECT product_id, warehouse_id, COALESCE(SUM(qty),0) AS qty "
                "FROM wh_inventory WHERE product_id = ANY(%s) "
                "GROUP BY product_id, warehouse_id",
                (product_ids,)
            ).fetchall()
            for row in inv_rows:
                pid = row["product_id"]
                wid = row["warehouse_id"]
                if pid not in inv_by_product:
                    inv_by_product[pid] = {}
                inv_by_product[pid][wid] = row["qty"]

        # Số lượng xuất 7 ngày qua theo product cho shop này (đơn đã/đang giao)
        # → dùng để xác định "bán chậm" (< 50 đơn / 7 ngày)
        sold_7d_by_product = {}
        if product_ids:
            sold_rows = conn.execute("""
                SELECT product_id, COALESCE(SUM(qty_ordered),0) AS qty_sold
                FROM outbound_requests
                WHERE shop_id=%s
                  AND product_id = ANY(%s)
                  AND pancake_status IN ('shipped','received')
                  AND created_at >= (NOW() - INTERVAL '7 days')::text
                GROUP BY product_id
            """, (shop["id"], product_ids)).fetchall()
            sold_7d_by_product = {r["product_id"]: int(r["qty_sold"] or 0) for r in sold_rows}

        # Gộp thành danh sách với qty_per_wh + tổng qty_physical
        items = []
        for r in items_raw:
            pid = r["product_id"]
            wh_qtys = inv_by_product.get(pid, {})
            qty_physical = sum(wh_qtys.values())
            items.append({
                "product_id": pid,
                "qty_pos": r["qty_pos"],
                "sku": r["sku"],
                "product_name": r["product_name"],
                "category": r["category"],
                "qty_physical": qty_physical,
                "wh_qtys": wh_qtys,   # {warehouse_id: qty} — cho template
                "qty_sold_7d": sold_7d_by_product.get(pid, 0),
            })

        sync_row = conn.execute(
            "SELECT MAX(synced_at) as last FROM wh_shop_inventory WHERE shop_id=%s",
            (shop["id"],)
        ).fetchone()
        synced_at = (sync_row["last"] or "")[:16] if sync_row else None

    total_qty      = sum(r["qty_pos"]      for r in items)
    total_physical = sum(r["qty_physical"] for r in items)
    low_count      = sum(1 for r in items if r["qty_pos"] <= 10)
    shop_biz = (_load_shop_biz_map().get(shop["shop_key"]) or "cty")
    return _rt("shop_detail.html", shop=shop, items=items,
               warehouses=warehouses,
               total_qty=total_qty, total_physical=total_physical,
               low_count=low_count, synced_at=synced_at,
               shop_biz=shop_biz)


@bp.route("/shops/sync", methods=["POST"])
def shops_sync_all():
    """Redirect sang async sync để tránh timeout."""
    return redirect(url_for(".products_sync_all_redirect"), code=307)


@bp.route("/products/sync-all", methods=["POST"])
def products_sync_all_redirect():
    """Sync toàn bộ sản phẩm từ Pancake POS → wh_products + wh_shop_inventory (background)."""
    denied = _deny_staff()
    if denied: return denied

    if not _PRODUCTS_SYNC_LOCK.acquire(blocking=False):
        flash("⚠️ Sync sản phẩm đang chạy. Vui lòng đợi hoàn tất rồi thử lại.", "warning")
        return redirect(url_for(".shops_list"))
    _PRODUCTS_SYNC_LOCK.release()

    # Bootstrap shops.json → wh_shops trước
    try:
        import subprocess, sys as _sys, os as _os
        from pathlib import Path as _Path
        scripts_dir = _Path(__file__).resolve().parents[2] / "scripts"
        subprocess.run(
            [_sys.executable, str(scripts_dir / "bootstrap_wh_shops_from_shops_json.py")],
            env=_os.environ.copy(), timeout=30, check=False,
        )
    except Exception:
        pass

    with db() as conn:
        shops = conn.execute("SELECT * FROM wh_shops WHERE status='active' ORDER BY shop_name").fetchall()

    shop_total = len(shops)
    _products_sync_progress.update({
        "running": True, "shop_name": "", "shop_idx": 0, "shop_total": shop_total,
        "synced": 0, "skipped": 0, "removed": 0, "new_products": 0, "new_skus": [],
        "started_at": now_hcm().strftime("%H:%M:%S"), "done_at": "", "errors": [],
    })

    def _run():
        if not _PRODUCTS_SYNC_LOCK.acquire(blocking=False):
            return
        try:
            total_synced = 0
            total_skipped = 0
            total_removed = 0
            total_new_products = 0
            new_skus_found = []
            all_errors = []
            all_product_ids = set()   # gom product_id mọi shop → Part B reconcile cuối vòng
            try:
                from pancake_auth import is_valid_hex_api_key as _is_valid
            except Exception:
                _is_valid = lambda k: bool(k and len(str(k).strip()) == 32)

            for idx, shop in enumerate(shops, start=1):
                sname = shop["shop_name"]
                _products_sync_progress.update({
                    "shop_name": sname, "shop_idx": idx,
                    "synced": total_synced, "skipped": total_skipped, "removed": total_removed,
                    "new_products": total_new_products,
                })
                api_key = str(shop.get("pos_api_key") or "").strip()
                if not _is_valid(api_key):
                    all_errors.append(f"{sname}: chưa có API key")
                    continue
                try:
                    with db() as conn2:
                        result = sync_shop_inventory_to_db(
                            conn2,
                            shop_db_id=shop["id"],
                            pos_shop_id=shop["pos_shop_id"],
                            api_key=api_key,
                        )
                    total_synced += result.get("synced", 0)
                    total_skipped += result.get("skipped", 0)
                    total_removed += int(result.get("removed") or 0)
                    all_product_ids |= (result.get("seen_product_ids") or set())
                    np = result.get("new_products", 0)
                    total_new_products += np
                    if np > 0 and result.get("new_sku_list"):
                        new_skus_found.extend(result["new_sku_list"])
                    if result.get("errors"):
                        all_errors.append(f"{sname}: {result['errors'][0]}")
                except Exception as e:
                    all_errors.append(f"{sname}: {e}")

            # Part B — gộp tồn vật lý cho SP có biến thể bị tắt (chuyển shop). CHỈ khi mọi
            # shop sync sạch (mirror an toàn ghost cleanup) để không gộp nhầm lúc thiếu dữ liệu.
            locked_merged = 0
            if not all_errors and all_product_ids:
                try:
                    from .wh_sync_pos import reconcile_locked_inventory
                    with db() as conn3:
                        rec = reconcile_locked_inventory(conn3, all_product_ids)
                    locked_merged = rec.get("merged", 0)
                    if rec.get("warnings"):
                        log.warning("[products_sync] %d SP đa biến thể có tồn lạc chờ duyệt: %s",
                                    len(rec["warnings"]), rec["warnings"][:10])
                    if locked_merged:
                        log.info("[products_sync] Part B gộp tồn %d (shop tắt SP)", locked_merged)
                except Exception as _be:
                    log.error("[products_sync] Part B reconcile lỗi: %s", _be)

            _products_sync_progress.update({
                "running": False, "done_at": now_hcm().strftime("%H:%M:%S"),
                "synced": total_synced, "skipped": total_skipped, "removed": total_removed,
                "new_products": total_new_products,
                "new_skus": new_skus_found[:20],
                "errors": all_errors,
            })
            log.info("[products_sync] Xong: %d sp, %d bỏ qua, %d dòng tồn xóa, %d lỗi",
                     total_synced, total_skipped, total_removed, len(all_errors))
        except Exception as e:
            _products_sync_progress.update({
                "running": False, "done_at": now_hcm().strftime("%H:%M:%S"),
                "errors": [str(e)],
            })
            log.error("[products_sync bg] %s", e)
        finally:
            _PRODUCTS_SYNC_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="wh-products-sync").start()
    flash("🔄 Đang sync sản phẩm từ POS (chạy nền). Trang sẽ tự cập nhật khi xong.", "info")
    return redirect(url_for(".shops_list"))


_variation_scan_progress: dict = {"running": False, "done": False, "new": 0, "updated": 0, "backfilled": 0, "errors": [], "started_at": "", "done_at": ""}
_VARIATION_SCAN_LOCK = threading.Lock()


@bp.route("/api/scan-variation-uuids", methods=["POST"])
def api_scan_variation_uuids():
    """Quét nhanh toàn bộ biến thể UUID từ Pancake → cập nhật wh_variation_map + backfill đơn cũ.
    Nhẹ hơn nhiều so với full product sync vì chỉ đọc danh sách sản phẩm + cập nhật mapping.
    """
    denied = _deny_staff()
    if denied:
        return jsonify({"ok": False, "error": "Không có quyền."}), 403

    if not _VARIATION_SCAN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Đang quét, vui lòng chờ..."}), 429

    _VARIATION_SCAN_LOCK.release()

    _variation_scan_progress.update({
        "running": True, "done": False, "new": 0, "updated": 0, "backfilled": 0,
        "errors": [], "started_at": now_hcm().strftime("%H:%M:%S"), "done_at": "",
    })

    def _run():
        if not _VARIATION_SCAN_LOCK.acquire(blocking=False):
            return
        import requests as _req
        try:
            with db() as conn:
                shops = conn.execute(
                    "SELECT pos_shop_id, pos_api_key, shop_name FROM wh_shops WHERE status='active' AND pos_api_key != '' ORDER BY shop_name"
                ).fetchall()

            seen_pos_shop_ids = set()
            new_total = 0
            upd_total = 0
            errs = []
            now = now_hcm().strftime("%Y-%m-%d %H:%M:%S")

            for shop in shops:
                pos_shop_id = str(shop["pos_shop_id"] or "").strip()
                api_key = str(shop["pos_api_key"] or "").strip()
                if not pos_shop_id or not api_key or pos_shop_id in seen_pos_shop_ids:
                    continue
                seen_pos_shop_ids.add(pos_shop_id)
                try:
                    page = 1
                    while True:
                        r = _req.get(
                            f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products",
                            params={"api_key": api_key, "page": page, "limit": 200},
                            timeout=20
                        )
                        r.raise_for_status()
                        data = r.json()
                        products = data.get("data") or []
                        if not products:
                            break
                        with db() as conn:
                            for prod in products:
                                pos_product_id = str(prod.get("id") or "")
                                prod_name = str(prod.get("name") or "").strip()
                                for var in (prod.get("variations") or []):
                                    var_uuid = str(var.get("id") or "").strip()
                                    if not var_uuid:
                                        continue
                                    var_name = str(var.get("display_id") or var.get("name") or "").strip()
                                    # Thử tìm product_id trong DB theo SKU hoặc pos_variation_id
                                    from .wh_sync_pos import _get_sku
                                    sku = _get_sku(var, prod)
                                    prod_db = conn.execute(
                                        "SELECT p.id FROM wh_products p LEFT JOIN wh_variation_map vm ON vm.product_id=p.id WHERE vm.pos_variation_id=%s OR UPPER(p.sku)=UPPER(%s) LIMIT 1",
                                        (var_uuid, sku)
                                    ).fetchone()
                                    if not prod_db:
                                        # Thử tìm qua wh_variation_map cũ
                                        vm = conn.execute(
                                            "SELECT product_id FROM wh_variation_map WHERE pos_variation_id=%s",
                                            (var_uuid,)
                                        ).fetchone()
                                        if vm:
                                            prod_db_id = vm["product_id"]
                                        else:
                                            continue  # Chưa có product nào khớp, bỏ qua
                                    else:
                                        prod_db_id = prod_db["id"]

                                    existing = conn.execute(
                                        "SELECT pos_variation_id FROM wh_variation_map WHERE pos_variation_id=%s",
                                        (var_uuid,)
                                    ).fetchone()
                                    conn.execute("""
                                        INSERT INTO wh_variation_map (pos_variation_id, product_id, variant_name, pos_remain_updated_at)
                                        VALUES (%s, %s, %s, %s)
                                        ON CONFLICT (pos_variation_id) DO UPDATE SET
                                            product_id = EXCLUDED.product_id,
                                            variant_name = CASE WHEN EXCLUDED.variant_name != '' THEN EXCLUDED.variant_name ELSE wh_variation_map.variant_name END,
                                            pos_remain_updated_at = EXCLUDED.pos_remain_updated_at
                                    """, (var_uuid, prod_db_id, var_name, now))
                                    if existing:
                                        upd_total += 1
                                    else:
                                        new_total += 1
                        if len(products) < 200:
                            break
                        page += 1
                except Exception as ex:
                    errs.append(f"{shop['shop_name']}: {ex}")

            _variation_scan_progress.update({"new": new_total, "updated": upd_total, "errors": errs})

            # Backfill: tìm outbound_requests có pos_variation_id nhưng product_id trống → link lại
            backfilled = 0
            try:
                with db() as conn:
                    orphans = conn.execute("""
                        SELECT id, pos_variation_id FROM wh_outbound_requests
                        WHERE (product_id IS NULL OR product_id = 0)
                          AND pos_variation_id != '' AND pos_variation_id IS NOT NULL
                        LIMIT 2000
                    """).fetchall()
                    for row in orphans:
                        vm = conn.execute(
                            "SELECT product_id FROM wh_variation_map WHERE pos_variation_id=%s",
                            (row["pos_variation_id"],)
                        ).fetchone()
                        if vm:
                            conn.execute(
                                "UPDATE wh_outbound_requests SET product_id=%s WHERE id=%s",
                                (vm["product_id"], row["id"])
                            )
                            backfilled += 1
            except Exception as ex:
                errs.append(f"Backfill lỗi: {ex}")

            _variation_scan_progress.update({
                "running": False, "done": True,
                "backfilled": backfilled,
                "done_at": now_hcm().strftime("%H:%M:%S"),
            })
            log.info("[scan-variation-uuids] new=%d upd=%d backfill=%d errs=%d",
                     new_total, upd_total, backfilled, len(errs))
        except Exception as ex:
            _variation_scan_progress.update({"running": False, "done": True, "errors": [str(ex)], "done_at": now_hcm().strftime("%H:%M:%S")})
            log.exception("[scan-variation-uuids]")
        finally:
            _VARIATION_SCAN_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="wh-variation-scan").start()
    return jsonify({"ok": True, "message": "Đang quét biến thể UUID ở nền..."})


@bp.route("/api/scan-variation-uuids/status")
def api_scan_variation_uuids_status():
    """Trả về trạng thái quét UUID biến thể + số UUID chưa map."""
    p = dict(_variation_scan_progress)
    with db() as conn:
        total_map = (conn.execute("SELECT COUNT(*) as c FROM wh_variation_map").fetchone() or {}).get("c", 0)
        unknown = (conn.execute("""
            SELECT COUNT(*) as c FROM wh_outbound_requests
            WHERE (product_id IS NULL OR product_id = 0)
              AND pos_variation_id != '' AND pos_variation_id IS NOT NULL
        """).fetchone() or {}).get("c", 0)
    p["total_map"] = total_map
    p["unknown_orders"] = unknown
    return jsonify(p)


@bp.route("/api/debug-variant/<int:product_id>")
def api_debug_variant(product_id: int):
    """Debug: xem variant_name trong DB + tên trực tiếp từ Pancake."""
    denied = _deny_staff()
    if denied:
        return jsonify({"error": "no access"}), 403
    import requests as _req
    result = {"product_id": product_id, "db_variants": [], "pancake_variants": [], "errors": []}
    with db() as conn:
        prod = conn.execute("SELECT * FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            return jsonify({"error": "product not found"}), 404
        result["product"] = {"name": prod["name"], "sku": prod["sku"], "pos_product_id": prod["pos_product_id"]}
        vm_rows = conn.execute("""
            SELECT pos_variation_id, variant_name, pos_remain_qty, pos_remain_updated_at
            FROM wh_variation_map WHERE product_id=%s ORDER BY variant_name NULLS LAST
        """, (product_id,)).fetchall()
        result["db_variants"] = [dict(r) for r in vm_rows]

        shops = conn.execute(
            "SELECT DISTINCT pos_shop_id, pos_api_key FROM wh_shops WHERE status='active' AND pos_api_key!='' LIMIT 3"
        ).fetchall()

    pos_product_id = prod["pos_product_id"] or ""
    for shop in shops:
        ps_id = str(shop["pos_shop_id"]).strip()
        akey = str(shop["pos_api_key"]).strip()
        if not pos_product_id or not ps_id:
            continue
        try:
            r = _req.get(
                f"https://pos.pages.fm/api/v1/shops/{ps_id}/products/{pos_product_id}",
                params={"api_key": akey}, timeout=10
            )
            if r.status_code == 200:
                data = r.json().get("product") or r.json()
                vars_raw = data.get("variations") or []
                result["pancake_variants"] = [
                    {"id": v.get("id"), "name": v.get("name"), "remain": v.get("remain_quantity")}
                    for v in vars_raw
                ]
                result["pancake_shop"] = ps_id
                break
            else:
                result["errors"].append(f"shop {ps_id}: HTTP {r.status_code}")
        except Exception as ex:
            result["errors"].append(f"shop {ps_id}: {ex}")

    return jsonify(result)


@bp.route("/api/products-sync-progress")
def api_products_sync_progress():
    """Trả về trạng thái sync sản phẩm đang chạy."""
    p = dict(_products_sync_progress)
    if p.get("running"):
        p["status_text"] = (
            f"Shop {p.get('shop_idx', 0)}/{p.get('shop_total', 0)}: "
            f"<strong>{p.get('shop_name', '')}</strong> — "
            f"{p.get('synced', 0)} sp đã cập nhật"
        )
    elif p.get("done_at"):
        errs = p.get("errors") or []
        new_p = p.get("new_products", 0)
        rem = int(p.get("removed") or 0)
        rm_txt = f", <strong class='text-danger'>{rem} dòng tồn cũ xóa (không còn trên POS)</strong>" if rem else ""
        new_badge = f" — <strong class='text-success'>🆕 {new_p} SKU mới!</strong>" if new_p else ""
        p["status_text"] = (
            f"✅ Xong lúc {p.get('done_at')} — "
            f"{p.get('synced', 0)} sp cập nhật, {p.get('skipped', 0)} bỏ qua"
            + rm_txt
            + new_badge
            + (f" — ⚠️ {len(errs)} lỗi" if errs else "")
        )
    else:
        p["status_text"] = ""
    return jsonify(p)


@bp.route("/shops/sync/<shop_key>", methods=["POST"])
def shops_sync_one(shop_key):
    with db() as conn:
        shop = conn.execute("SELECT * FROM wh_shops WHERE shop_key=%s", (shop_key,)).fetchone()
        if not shop:
            flash("Không tìm thấy shop.", "danger")
            return redirect(url_for(".shops_list"))
        from pancake_auth import is_valid_hex_api_key as _is_valid
        api_key = str(shop.get("pos_api_key") or "").strip()
        if not _is_valid(api_key):
            flash(f"⚠️ Shop '{shop['shop_name']}' chưa có API key hợp lệ — vào Cài đặt → Shop & Web để nhập.", "warning")
            return redirect(url_for(".shops_list"))
        result = sync_shop_inventory_to_db(
            conn, shop_db_id=shop["id"],
            pos_shop_id=shop["pos_shop_id"], api_key=api_key
        )
    synced  = result.get("synced", 0)
    errors  = result.get("errors", [])
    skipped = result.get("skipped", 0)
    removed = int(result.get("removed") or 0)
    if errors:
        flash(f"⚠️ Sync {shop['shop_name']} thất bại: {errors[0]}", "danger")
    elif synced == 0 and skipped == 0:
        # wh_sync_pos: parse ra 0 variation nhưng API vẫn trả sản phẩm — đã xóa tồn shop tương ứng
        if removed:
            flash(
                f"✅ Sync {shop['shop_name']}: POS không còn variation — đã xóa {removed} dòng tồn cũ.",
                "success",
            )
        else:
            flash(
                f"ℹ️ Sync {shop['shop_name']}: không có variation nào từ POS; tồn theo shop đã trống.",
                "info",
            )
    elif synced == 0:
        extra = f", đã xóa {removed} dòng tồn cũ (không còn trên POS)" if removed else ""
        flash(f"✅ Sync {shop['shop_name']}: không cập nhật dòng nào ({skipped} bỏ qua){extra}.", "info")
    else:
        extra = f" · Đã xóa {removed} dòng tồn cũ (không còn trên POS)" if removed else ""
        flash(f"✅ Sync {shop['shop_name']}: {synced} sản phẩm cập nhật{f', {skipped} bỏ qua' if skipped else ''}{extra}.", "success")
    return redirect(url_for(".shop_detail", shop_key=shop_key))


@bp.route("/shops/<shop_key>/assign-warehouse", methods=["POST"])
def shop_assign_warehouse(shop_key):
    """Admin: gán kho vật lý cho một shop."""
    role = session.get("role", "staff")
    if role not in ("admin", "superadmin"):
        abort(403)
    wh_id = request.form.get("warehouse_id", "").strip() or None
    wh_id_val = int(wh_id) if wh_id and wh_id.isdigit() else None
    with db() as conn:
        conn.execute(
            "UPDATE wh_shops SET warehouse_id=%s WHERE shop_key=%s",
            (wh_id_val, shop_key)
        )
    flash("✅ Đã cập nhật kho vật lý cho shop.", "success")
    return redirect(url_for(".shops_list"))


# ─────────────────────────────────────────
# Export Excel
# ─────────────────────────────────────────

def _make_excel_response(wb, filename):
    """Trả về file Excel dưới dạng HTTP response."""
    import io
    from flask import make_response
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def _style_header(ws, row=1):
    """Tô màu và làm đậm hàng header."""
    from openpyxl.styles import Font, PatternFill, Alignment
    fill = PatternFill("solid", fgColor="1A56DB")
    font = Font(bold=True, color="FFFFFF")
    align = Alignment(horizontal="center", vertical="center")
    for cell in ws[row]:
        cell.fill = fill
        cell.font = font
        cell.alignment = align


@bp.route("/shops/export/shop/<shop_key>")
def export_shop_excel(shop_key):
    """Xuất Excel tồn kho của một shop."""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from datetime import date

    with db() as conn:
        shop = conn.execute(
            "SELECT * FROM wh_shops WHERE shop_key=%s", (shop_key,)
        ).fetchone()
        if not shop:
            flash("Không tìm thấy shop.", "danger")
            return redirect(url_for(".shops_list"))
        rows = conn.execute("""
            SELECT p.sku, p.name as product_name, p.category, si.qty_pos
            FROM wh_shop_inventory si
            JOIN wh_products p ON p.id = si.product_id
            WHERE si.shop_id=%s AND si.qty_pos > 0
            ORDER BY p.category, p.name
        """, (shop["id"],)).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = shop["shop_key"]
    ws.append(["STT", "SKU", "Tên sản phẩm", "Danh mục", "Tồn kho POS"])
    _style_header(ws)
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 20
    ws.column_dimensions["E"].width = 14

    red  = Font(color="C0392B", bold=True)
    orng = Font(color="E67E22", bold=True)
    grn  = Font(color="27AE60", bold=True)

    for i, r in enumerate(rows, 1):
        ws.append([i, r["sku"], r["product_name"], r["category"] or "", r["qty_pos"]])
        q_cell = ws.cell(row=i+1, column=5)
        q_cell.alignment = Alignment(horizontal="center")
        if r["qty_pos"] <= 3:
            q_cell.font = red
        elif r["qty_pos"] <= 10:
            q_cell.font = orng
        else:
            q_cell.font = grn

    ws.append([])
    ws.append(["", "", "", "Tổng cộng", sum(r["qty_pos"] for r in rows)])
    total_cell = ws.cell(row=ws.max_row, column=5)
    total_cell.font = Font(bold=True)

    # Metadata sheet
    info = wb.create_sheet("Thông tin")
    info.append(["Shop", f"{shop['shop_key']} — {shop['shop_name']}"])
    info.append(["Ngày xuất", str(date.today())])
    info.append(["Số sản phẩm", len(rows)])

    fname = f"tonkho_{shop_key}_{date.today()}.xlsx"
    return _make_excel_response(wb, fname)


@bp.route("/shops/export/all")
def export_all_shops_excel():
    """Xuất Excel tất cả shop — mỗi shop một sheet + sheet tổng hợp."""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from datetime import date

    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    with db() as conn:
        if allowed is None:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active' ORDER BY shop_key"
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active' AND shop_name=ANY(%s) ORDER BY shop_key",
                (allowed,)
            ).fetchall()

        # Lấy tất cả sản phẩm để làm sheet tổng hợp
        all_products = conn.execute(
            "SELECT id, sku, name, category FROM wh_products ORDER BY category, name"
        ).fetchall()

        shop_data = {}
        for sh in shops:
            rows = conn.execute("""
                SELECT p.sku, p.name as product_name, p.category, si.qty_pos
                FROM wh_shop_inventory si
                JOIN wh_products p ON p.id = si.product_id
                WHERE si.shop_id=%s AND si.qty_pos > 0
                ORDER BY p.category, p.name
            """, (sh["id"],)).fetchall()
            shop_data[sh["id"]] = rows

        # Tổng hợp: product_id → shop_id → qty
        all_inv = conn.execute(
            "SELECT product_id, shop_id, qty_pos FROM wh_shop_inventory WHERE qty_pos > 0"
        ).fetchall()

    wb = openpyxl.Workbook()

    # ── Sheet tổng hợp tất cả shop ──
    ws_all = wb.active
    ws_all.title = "Tổng hợp"
    header = ["STT", "SKU", "Tên sản phẩm", "Danh mục"] + [sh["shop_key"] for sh in shops] + ["Tổng"]
    ws_all.append(header)
    _style_header(ws_all)
    ws_all.column_dimensions["A"].width = 6
    ws_all.column_dimensions["B"].width = 14
    ws_all.column_dimensions["C"].width = 38
    ws_all.column_dimensions["D"].width = 18
    for col_i in range(5, 5 + len(shops) + 1):
        ws_all.column_dimensions[openpyxl.utils.get_column_letter(col_i)].width = 10

    inv_map = {}
    for inv in all_inv:
        inv_map.setdefault(inv["product_id"], {})[inv["shop_id"]] = inv["qty_pos"]

    red  = Font(color="C0392B", bold=True)
    orng = Font(color="E67E22", bold=True)
    grn  = Font(color="27AE60", bold=True)

    for i, prod in enumerate(all_products, 1):
        qtys = [inv_map.get(prod["id"], {}).get(sh["id"], 0) for sh in shops]
        total = sum(qtys)
        if total == 0:
            continue
        ws_all.append([i, prod["sku"], prod["name"], prod["category"] or ""] + qtys + [total])
        row_num = ws_all.max_row
        # Màu cho ô tổng
        tc = ws_all.cell(row=row_num, column=5 + len(shops))
        tc.font = Font(bold=True)
        # Màu cho từng ô shop
        for col_offset, q in enumerate(qtys):
            cell = ws_all.cell(row=row_num, column=5 + col_offset)
            cell.alignment = Alignment(horizontal="center")
            if q == 0:
                cell.value = ""
            elif q <= 3:
                cell.font = red
            elif q <= 10:
                cell.font = orng
            else:
                cell.font = grn

    # ── Sheet riêng cho từng shop ──
    for sh in shops:
        rows = shop_data.get(sh["id"], [])
        ws = wb.create_sheet(sh["shop_key"])
        ws.append(["STT", "SKU", "Tên sản phẩm", "Danh mục", "Tồn kho POS"])
        _style_header(ws)
        ws.column_dimensions["A"].width = 6
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 40
        ws.column_dimensions["D"].width = 20
        ws.column_dimensions["E"].width = 14
        for j, r in enumerate(rows, 1):
            ws.append([j, r["sku"], r["product_name"], r["category"] or "", r["qty_pos"]])
            c = ws.cell(row=j+1, column=5)
            c.alignment = Alignment(horizontal="center")
            if r["qty_pos"] <= 3:
                c.font = red
            elif r["qty_pos"] <= 10:
                c.font = orng
            else:
                c.font = grn
        ws.append([])
        ws.append(["", "", "", "Tổng", sum(r["qty_pos"] for r in rows)])
        ws.cell(row=ws.max_row, column=5).font = Font(bold=True)

    fname = f"tonkho_tatca_{date.today()}.xlsx"
    return _make_excel_response(wb, fname)


# ─────────────────────────────────────────
# API sản phẩm (autocomplete)
# ─────────────────────────────────────────

@bp.route("/api/products")
def api_products():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, sku, name, unit, category FROM wh_products ORDER BY category, name"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────
# BÁO CÁO XUẤT HÀNG
# ─────────────────────────────────────────

@bp.route("/bao-cao")
def bao_cao():
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)

    today = today_hcm()
    date_str = request.args.get("date", today)

    with db() as conn:
        shop_filter_sql = "AND s.shop_name = ANY(%s)" if allowed is not None else ""
        shop_params = [allowed] if allowed is not None else []
        rows = conn.execute(f"""
            SELECT
                s.shop_name, s.id as shop_id,
                COUNT(DISTINCT CASE WHEN o.status='confirmed' THEN o.order_code END) as don_xn,
                COALESCE(SUM(CASE WHEN o.status='confirmed' THEN o.qty_confirmed ELSE 0 END), 0) as sp_xn,
                COUNT(DISTINCT CASE WHEN o.status='pending' THEN o.order_code END) as don_cho,
                COALESCE(SUM(CASE WHEN o.status='pending' THEN o.qty_ordered ELSE 0 END), 0) as sp_cho
            FROM wh_shops s
            LEFT JOIN wh_outbound_requests o
                ON o.shop_name = s.shop_name
               AND o.carrier_picked_up_at LIKE %s
            WHERE s.status = 'active' {shop_filter_sql}
            GROUP BY s.id, s.shop_name
            ORDER BY don_xn DESC, don_cho DESC, s.shop_name
        """, [date_str + "%"] + shop_params).fetchall()

        totals = {
            "don_xn": sum(r["don_xn"] for r in rows),
            "sp_xn": sum(r["sp_xn"] for r in rows),
            "don_cho": sum(r["don_cho"] for r in rows),
            "sp_cho": sum(r["sp_cho"] for r in rows),
            "shops_co_don": sum(1 for r in rows if (r["don_xn"] or 0) + (r["don_cho"] or 0) > 0),
        }

    return _rt("bao_cao.html", rows=rows, date_str=date_str, today=today, totals=totals,
               shop_biz_map=_load_shop_biz_map())


@bp.route("/bao-cao/shop/<shop_name>")
def bao_cao_shop(shop_name):
    user_id = session.get("user_id")
    role    = session.get("role", "staff")
    allowed = _get_allowed_shop_names(user_id, role)
    if allowed is not None and shop_name not in allowed:
        abort(403)

    today = today_hcm()
    date_str = request.args.get("date", today)

    with db() as conn:
        shop = conn.execute(
            "SELECT * FROM wh_shops WHERE shop_name=%s", (shop_name,)
        ).fetchone()
        if not shop:
            flash(f"Không tìm thấy shop {shop_name}", "danger")
            return redirect(url_for(".bao_cao"))

        by_product = conn.execute("""
            SELECT product_name, product_sku,
                   SUM(CASE WHEN status='confirmed' THEN qty_confirmed ELSE 0 END) as sp_xn,
                   SUM(CASE WHEN status='pending' THEN qty_ordered ELSE 0 END) as sp_cho,
                   COUNT(DISTINCT CASE WHEN status='confirmed' THEN order_code END) as don_xn,
                   COUNT(DISTINCT CASE WHEN status='pending' THEN order_code END) as don_cho
            FROM wh_outbound_requests
            WHERE shop_name = %s AND carrier_picked_up_at LIKE %s
            GROUP BY product_sku, product_name
            ORDER BY sp_xn DESC, sp_cho DESC
        """, (shop_name, date_str + "%")).fetchall()

        orders = conn.execute("""
            SELECT order_code, status,
                   MIN(carrier_picked_up_at) as picked_at,
                   COUNT(*) as so_sp,
                   SUM(CASE WHEN status='confirmed' THEN qty_confirmed ELSE qty_ordered END) as tong_qty,
                   carrier_name
            FROM wh_outbound_requests
            WHERE shop_name = %s AND carrier_picked_up_at LIKE %s
            GROUP BY order_code, status, carrier_name
            ORDER BY picked_at
        """, (shop_name, date_str + "%")).fetchall()

    _bizmap = _load_shop_biz_map()
    shop_biz = (_bizmap.get(shop_name) or "cty")
    return _rt("bao_cao_shop.html", shop=shop, shop_name=shop_name,
               date_str=date_str, today=today, by_product=by_product, orders=orders,
               shop_biz=shop_biz)


@bp.route("/bao-cao/ton-kho")
def bao_cao_ton_kho():
    """Báo cáo tồn kho vật lý theo kỳ (ngày/tháng)."""
    today = today_hcm()
    date_from = request.args.get("date_from", today[:7] + "-01")  # đầu tháng hiện tại
    date_to   = request.args.get("date_to",   today)
    wh_filter = request.args.get("warehouse_id", "")

    with db() as conn:
        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()

        # Tồn kho hiện tại (cuối kỳ) — SUM tất cả biến thể theo (pid, wh_id)
        # để báo cáo không trùng dòng khi SP có nhiều biến thể.
        inv_rows = conn.execute("""
            SELECT p.id AS product_id, w.id AS warehouse_id,
                   COALESCE(SUM(i.qty), 0) AS qty,
                   p.sku, p.name, p.unit, p.category,
                   COALESCE(p.min_qty, 0) as min_qty,
                   w.name as wh_name
            FROM wh_inventory i
            JOIN wh_products p ON p.id = i.product_id
            JOIN wh_warehouses w ON w.id = i.warehouse_id
            WHERE p.sku != '' AND p.name != ''
            GROUP BY p.id, w.id, p.sku, p.name, p.unit, p.category, p.min_qty, w.name
            ORDER BY p.category, p.name
        """).fetchall()

        # Nhập trong kỳ theo product + warehouse
        # Bao gồm: nhập hàng, hàng hoàn về kho, điều chỉnh tăng, điều chỉnh thủ công dương
        nhap_rows = conn.execute("""
            SELECT product_id, warehouse_id,
                   COALESCE(SUM(
                       CASE
                           WHEN type IN ('inbound', 'return_received', 'adjustment_up') THEN qty
                           WHEN type = 'adjustment' AND qty > 0 THEN qty
                           ELSE 0
                       END
                   ), 0) AS nhap
            FROM wh_stock_movements
            WHERE type IN ('inbound', 'return_received', 'adjustment_up', 'adjustment')
              AND status != 'cancelled'
              AND created_at >= %s AND created_at <= %s
            GROUP BY product_id, warehouse_id
        """, (date_from + " 00:00:00", date_to + " 23:59:59")).fetchall()
        nhap_map = {(r["product_id"], r["warehouse_id"]): int(r["nhap"] or 0) for r in nhap_rows}

        # Xuất trong kỳ theo product + warehouse
        # Bao gồm: xuất hàng, xuất nội bộ/POS, xuất hoàn đi, điều chỉnh giảm, điều chỉnh thủ công âm
        xuat_rows = conn.execute("""
            SELECT product_id, warehouse_id,
                   COALESCE(SUM(
                       CASE
                           WHEN type IN ('outbound_confirmed', 'pos_export', 'return_outbound', 'adjustment_down') THEN qty
                           WHEN type = 'adjustment' AND qty < 0 THEN ABS(qty)
                           ELSE 0
                       END
                   ), 0) AS xuat
            FROM wh_stock_movements
            WHERE type IN ('outbound_confirmed', 'pos_export', 'return_outbound', 'adjustment_down', 'adjustment')
              AND status != 'cancelled'
              AND created_at >= %s AND created_at <= %s
            GROUP BY product_id, warehouse_id
        """, (date_from + " 00:00:00", date_to + " 23:59:59")).fetchall()
        xuat_map = {(r["product_id"], r["warehouse_id"]): int(r["xuat"] or 0) for r in xuat_rows}

    # Xây dựng bảng báo cáo
    rows = []
    for r in inv_rows:
        if wh_filter and str(r["warehouse_id"]) != wh_filter:
            continue
        pid  = r["product_id"]
        whid = r["warehouse_id"]
        ton_cuoi = int(r["qty"] or 0)
        nhap_ky  = nhap_map.get((pid, whid), 0)
        xuat_ky  = xuat_map.get((pid, whid), 0)
        ton_dau  = ton_cuoi - nhap_ky + xuat_ky  # tồn đầu = cuối - nhập + xuất
        min_qty  = int(r["min_qty"] or 0)
        rows.append({
            "sku":       r["sku"],
            "name":      r["name"],
            "unit":      r["unit"] or "cái",
            "category":  r["category"] or "",
            "wh_name":   r["wh_name"],
            "ton_dau":   max(ton_dau, 0),
            "nhap_ky":   nhap_ky,
            "xuat_ky":   xuat_ky,
            "ton_cuoi":  ton_cuoi,
            "min_qty":   min_qty,
            "alert":     min_qty > 0 and ton_cuoi <= min_qty,
        })

    # Tổng
    totals = {
        "ton_dau":  sum(r["ton_dau"]  for r in rows),
        "nhap_ky":  sum(r["nhap_ky"]  for r in rows),
        "xuat_ky":  sum(r["xuat_ky"]  for r in rows),
        "ton_cuoi": sum(r["ton_cuoi"] for r in rows),
        "alert_count": sum(1 for r in rows if r["alert"]),
    }

    return _rt("bao_cao_ton_kho.html",
               rows=rows, totals=totals,
               warehouses=warehouses,
               date_from=date_from, date_to=date_to,
               wh_filter=wh_filter, today=today)


# ─────────────────────────────────────────
# AUTO-SYNC BACKGROUND THREAD
# ─────────────────────────────────────────

def _do_sync_once(active_only: bool = True):
    """Sync kho: xuất hàng + hoàn hàng + tồn kho.
    active_only=True (mặc định cho auto-sync): xuất (1,7,9,2,3) + hoàn cửa sổ 2 ngày.
    active_only=False (full): xuất đầy đủ status + hoàn từ 2020-01-01.
    """
    try:
        now_vn_tz = datetime.now(timezone.utc) + timedelta(hours=7)
        date_to = now_vn_tz.strftime("%Y-%m-%d")
        mode = "nhanh" if active_only else "đầy đủ"

        # 1. Xuất hàng từ POS
        # Auto-sync active_only: cửa sổ 30 ngày — đủ bắt đơn inserted cũ
        # mới đổi status (received/shipped), kết hợp early-termination trong
        # fetch_orders_for_shop giúp peak RAM giảm mạnh (thay vì fetch 50k đơn).
        # Full sync (active_only=False): không giới hạn date_from.
        if active_only:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            date_from_ob = (dt_to - timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            date_from_ob = ""  # fetch toàn bộ — early-term skip
        res_ob = sync_outbound_for_date_range(date_from_ob, date_to, active_only=active_only)
        print(f"[kho auto-sync/{mode}] Xuất hàng: +{res_ob.get('inserted',0)} mới, "
              f"cập nhật {res_ob.get('updated',0)}, bỏ {res_ob.get('skipped',0)}, "
              f"lỗi: {res_ob.get('errors',[])}", flush=True)

        # 2. Hoàn hàng: chạy mỗi 15 phút với cửa sổ 30 ngày.
        # Lý do 30 ngày: có đơn tạo cách đây ~1 tháng mới hoàn về → nếu cửa sổ
        # ngắn sẽ miss. Full sync vẫn lấy toàn lịch sử.
        if active_only:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            date_from_ret = (dt_to - timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            date_from_ret = "2020-01-01"
        res_ret = sync_returns_pos(date_from_ret, date_to)
        print(f"[kho auto-sync/{mode}] Hoàn hàng: +{res_ret.get('inserted',0)} mới, "
              f"bỏ {res_ret.get('skipped',0)}, lỗi: {res_ret.get('errors',[])}", flush=True)
        _last_auto_sync["returns"] = res_ret.get("inserted", 0)

        # 3. Kho theo shop (tồn kho POS của từng shop)
        with db() as conn:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active'"
            ).fetchall()
            res_shops = sync_all_shops(conn, shops)
        print(f"[kho auto-sync/{mode}] Tồn kho: {len(res_shops.get('shops',[]))} shop, "
              f"{res_shops.get('total_synced',0)} bản ghi cập nhật", flush=True)

        _last_auto_sync["time"] = now_vn_tz.strftime("%H:%M %d/%m/%Y")
        _last_auto_sync["outbound"] = res_ob.get("inserted", 0)
        # "shops" dùng để hiển thị số shop đã chạy sync, không phải số bản ghi sản phẩm cập nhật.
        _last_auto_sync["shops"] = len(res_shops.get("shops", []) or [])

    except Exception as e:
        print(f"[kho auto-sync] Lỗi: {e}", flush=True)
        log.error("[auto-sync] Lỗi: %s", e)


def _auto_sync_loop():
    """Background thread: chờ 180 giây rồi sync, lặp mỗi AUTO_SYNC_INTERVAL.
    Delay cao để app ổn định & không burst RAM chồng với fast-shipped/fast-returns."""
    import gc
    time.sleep(180)
    while True:
        now_label = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%H:%M:%S")
        print(f"[kho auto-sync] Bắt đầu ({now_label})...", flush=True)
        _do_sync_once()
        gc.collect()
        print(f"[kho auto-sync] Xong. Nghỉ {AUTO_SYNC_INTERVAL // 60} phút.", flush=True)
        time.sleep(AUTO_SYNC_INTERVAL)


def _products_auto_sync_loop():
    """Background thread: sync sản phẩm từ POS mỗi 2 tiếng.
    Cập nhật wh_products (SKU, tên, giá) + wh_shop_inventory (tồn POS).
    """
    time.sleep(240)  # Chờ 4 phút — app ổn định, tránh burst cùng auto-sync
    while True:
        try:
            now_label = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%H:%M:%S")
            print(f"[kho products-sync] Bắt đầu sync sản phẩm ({now_label})...", flush=True)
            with db() as conn:
                shops = conn.execute(
                    "SELECT * FROM wh_shops WHERE status='active' ORDER BY shop_name"
                ).fetchall()
            total_synced = 0
            total_removed = 0
            total_errors = []
            for shop in shops:
                try:
                    api_key = str(shop.get("pos_api_key") or "").strip()
                    if not api_key:
                        continue
                    with db() as conn2:
                        result = sync_shop_inventory_to_db(
                            conn2,
                            shop_db_id=shop["id"],
                            pos_shop_id=shop["pos_shop_id"],
                            api_key=api_key,
                        )
                    total_synced += result.get("synced", 0)
                    total_removed += int(result.get("removed") or 0)
                    if result.get("errors"):
                        total_errors.append(f"{shop['shop_name']}: {result['errors'][0]}")
                except Exception as e_shop:
                    total_errors.append(f"{shop['shop_name']}: {e_shop}")
            print(
                f"[kho products-sync] Xong: {total_synced} sp, {total_removed} dòng tồn xóa, {len(total_errors)} lỗi. "
                f"Nghỉ {PRODUCTS_AUTO_SYNC_INTERVAL // 3600}h.",
                flush=True
            )
        except Exception as e:
            print(f"[kho products-sync] Lỗi: {e}", flush=True)
            log.error("[products-auto-sync] %s", e)
        time.sleep(PRODUCTS_AUTO_SYNC_INTERVAL)


def _auto_confirm_shipped_pre_confirmed():
    """Auto-confirm đơn pre_confirmed mà ĐVVC đã lấy (pancake_status shipped/received).
    So sánh: NV đã quét (pre_confirmed) + POS xác nhận đã lấy (shipped) = xuất kho luôn.
    Không cần NV bấm thêm bước. Gọi từ fast-sync sau mỗi vòng sync.
    """
    try:
        with db() as conn:
            items = conn.execute("""
                SELECT * FROM wh_outbound_requests
                WHERE status = 'pre_confirmed'
                  AND pancake_status IN ('shipped', 'received')
                ORDER BY id
            """).fetchall()
            if not items:
                return 0, 0

            ts = now_vn()
            confirmed_count = 0
            skip_count = 0
            for item in items:
                try:
                    product_id = item["product_id"]
                    qty_actual = item["qty_ordered"] or 1
                    pos_var_id = (item.get("pos_variation_id") or "").strip() or None
                    wh_id      = item.get("warehouse_id") or _DEFAULT_WAREHOUSE_ID
                    order_code = item["order_code"]

                    inv, is_variant = _get_variant_inv_outbound(conn, product_id, wh_id, pos_var_id)
                    qty_before = inv["qty"] if inv else 0

                    if qty_actual > qty_before:
                        skip_count += 1
                        print(f"[auto-confirm] skip {order_code} item#{item['id']} — tồn {qty_before} < cần {qty_actual}", flush=True)
                        continue

                    qty_after = qty_before - qty_actual
                    _upsert_inventory(conn, product_id, wh_id, qty_after,
                                      pos_variation_id=pos_var_id if is_variant else None,
                                      inventory_row_id=inv["id"] if inv else None)
                    conn.execute("""
                        INSERT INTO wh_stock_movements
                          (type, product_id, warehouse_id, qty, qty_before, qty_after,
                           ref_order_id, note, created_at, pos_variation_id)
                        VALUES ('outbound_confirmed', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (product_id, wh_id, qty_actual, qty_before, qty_after,
                          order_code, "Auto xuất kho — ĐVVC lấy", ts, pos_var_id))
                    conn.execute("""
                        UPDATE wh_outbound_requests
                           SET status='confirmed', qty_confirmed=%s,
                               confirmed_by='auto_sync', confirmed_at=%s
                         WHERE id=%s
                    """, (qty_actual, ts, item["id"]))
                    confirmed_count += 1
                except Exception as _ei:
                    skip_count += 1
                    print(f"[auto-confirm] lỗi item#{item.get('id')} {item.get('order_code')}: {_ei}", flush=True)
        return confirmed_count, skip_count
    except Exception as _e:
        print(f"[auto-confirm] lỗi DB: {_e}", flush=True)
        return 0, 0


def _fast_shipped_sync_loop():
    """Background thread: fetch status=2 (shipped) từ Pancake mỗi FAST_SYNC_INTERVAL giây.
    Sau mỗi vòng sync: auto-confirm đơn pre_confirmed+shipped (không cần NV bấm thêm).
    Fetch 7 ngày gần nhất. Rất nhẹ: ~34 API call (1 call/shop).
    Skip nếu không có đơn pre_confirmed VÀ không có đơn waiting/pending.
    """
    time.sleep(30)   # chờ app khởi động xong
    while True:
        now_label = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%H:%M:%S")
        try:
            # Kiểm tra nhanh: có đơn pre_confirmed hoặc waiting/pending không?
            with db() as _chk:
                _has_work = _chk.execute("""
                    SELECT COUNT(*) as c FROM wh_outbound_requests
                    WHERE status IN ('pending','pre_confirmed')
                      AND pancake_status IN ('waiting','confirmed')
                    LIMIT 1
                """).fetchone()
                _has_work = (_has_work["c"] if _has_work else 0) > 0
            if not _has_work:
                time.sleep(FAST_SYNC_INTERVAL)
                continue

            now_vn = datetime.now(timezone.utc) + timedelta(hours=7)
            date_to   = now_vn.strftime("%Y-%m-%d")
            date_from = (now_vn - timedelta(days=7)).strftime("%Y-%m-%d")
            from .wh_sync_orders import sync_outbound_for_date_range as _sync_ob
            result = _sync_ob(date_from, date_to, active_only=False, shipped_only=True)
            ins  = result.get("inserted", 0)
            upd  = result.get("updated", 0)
            errs = result.get("errors", [])
            _lock_busy = (
                ins == 0 and upd == 0
                and errs
                and "đang chạy" in (errs[0] if errs else "")
            )
            if _lock_busy:
                print(f"[kho fast-sync] {now_label} bị skip (fast-lock bận)", flush=True)
            else:
                err_note = f", lỗi shop: {len(errs)}" if errs else ""
                print(
                    f"[kho fast-sync] {now_label} shipped 7 ngày: +{ins} mới, {upd} cập nhật{err_note}",
                    flush=True,
                )
                # Auto-confirm đơn pre_confirmed vừa được ĐVVC lấy
                ac_ok, ac_skip = _auto_confirm_shipped_pre_confirmed()
                if ac_ok or ac_skip:
                    print(f"[auto-confirm] {now_label} xác nhận {ac_ok} đơn, bỏ qua {ac_skip}", flush=True)
        except Exception as _e:
            print(f"[kho fast-sync] Lỗi: {_e}", flush=True)
        import gc; gc.collect()
        time.sleep(FAST_SYNC_INTERVAL)


def _fast_returns_sync_loop():
    """Background thread: mỗi 2 phút fetch đơn hoàn status 4+5 (Đã hoàn — chờ nhận) 7 ngày gần nhất.
    Dùng FAST_RETURNS_SYNC_LOCK riêng → chạy song song với full returns sync."""
    time.sleep(90)   # chờ app + fast-shipped khởi động & xong vòng đầu trước
    while True:
        now_label = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%H:%M:%S")
        try:
            now_vn = datetime.now(timezone.utc) + timedelta(hours=7)
            date_to   = now_vn.strftime("%Y-%m-%d")
            # Cửa sổ 30 ngày — bắt đơn tạo ~1 tháng trước mới hoàn về tới.
            date_from = (now_vn - timedelta(days=30)).strftime("%Y-%m-%d")
            from .wh_sync_returns import sync_returns_for_date_range as _sync_ret
            result = _sync_ret(date_from, date_to, fast_only=True)
            ins  = result.get("inserted", 0)
            skp  = result.get("skipped", 0)
            errs = result.get("errors", [])
            _lock_busy = (
                ins == 0 and skp == 0
                and errs
                and "đang chạy" in (errs[0] if errs else "")
            )
            if _lock_busy:
                print(f"[kho fast-returns] {now_label} bị skip (fast-lock bận)", flush=True)
            else:
                err_note = f", lỗi shop: {len(errs)}" if errs else ""
                print(
                    f"[kho fast-returns] {now_label} hoàn 7 ngày: +{ins} mới{err_note}",
                    flush=True,
                )
        except Exception as _e:
            print(f"[kho fast-returns] Lỗi: {_e}", flush=True)
        import gc; gc.collect()
        time.sleep(FAST_SYNC_INTERVAL)


def _startup_variation_backfill():
    """Chạy 1 lần khi khởi động: quét Pancake → điền tên biến thể còn thiếu trong wh_variation_map."""
    import time as _time
    _time.sleep(300)  # Đợi 5 phút — chạy sau auto-sync vòng đầu
    import requests as _req
    log.info("[startup-variant-backfill] Bắt đầu điền tên biến thể từ Pancake...")
    try:
        with db() as conn:
            shops = conn.execute(
                "SELECT pos_shop_id, pos_api_key, shop_name FROM wh_shops WHERE status='active' AND pos_api_key != '' ORDER BY shop_name"
            ).fetchall()

        seen = set()
        new_names = 0
        now = now_hcm().strftime("%Y-%m-%d %H:%M:%S")

        for shop in shops:
            pos_shop_id = str(shop["pos_shop_id"] or "").strip()
            api_key = str(shop["pos_api_key"] or "").strip()
            if not pos_shop_id or not api_key or pos_shop_id in seen:
                continue
            seen.add(pos_shop_id)
            try:
                page = 1
                while True:
                    r = _req.get(
                        f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products",
                        params={"api_key": api_key, "page": page, "limit": 200},
                        timeout=20
                    )
                    r.raise_for_status()
                    products = r.json().get("data") or []
                    if not products:
                        break
                    with db() as conn:
                        for prod in products:
                            prod_name = str(prod.get("name") or "").strip()
                            for var in (prod.get("variations") or []):
                                var_uuid = str(var.get("id") or "").strip()
                                var_name = str(var.get("display_id") or var.get("name") or "").strip()
                                if not var_uuid or not var_name:
                                    continue
                                # Chỉ update nếu variant_name đang rỗng
                                existing = conn.execute(
                                    "SELECT pos_variation_id, variant_name, product_id FROM wh_variation_map WHERE pos_variation_id=%s",
                                    (var_uuid,)
                                ).fetchone()
                                if existing:
                                    if not existing["variant_name"]:
                                        conn.execute(
                                            "UPDATE wh_variation_map SET variant_name=%s WHERE pos_variation_id=%s",
                                            (var_name, var_uuid)
                                        )
                                        new_names += 1
                                else:
                                    # Thử tìm product_id từ wh_products
                                    from .wh_sync_pos import _get_sku
                                    sku = _get_sku(var, prod)
                                    prod_db = conn.execute(
                                        "SELECT p.id FROM wh_products p LEFT JOIN wh_variation_map vm ON vm.product_id=p.id WHERE vm.pos_variation_id=%s OR UPPER(p.sku)=UPPER(%s) LIMIT 1",
                                        (var_uuid, sku)
                                    ).fetchone()
                                    if prod_db:
                                        conn.execute("""
                                            INSERT INTO wh_variation_map (pos_variation_id, product_id, variant_name, pos_remain_updated_at)
                                            VALUES (%s, %s, %s, %s)
                                            ON CONFLICT (pos_variation_id) DO UPDATE SET
                                                variant_name = CASE WHEN EXCLUDED.variant_name != '' THEN EXCLUDED.variant_name ELSE wh_variation_map.variant_name END
                                        """, (var_uuid, prod_db["id"], var_name, now))
                                        new_names += 1
                    if len(products) < 200:
                        break
                    page += 1
            except Exception as ex:
                log.warning("[startup-variant-backfill] Shop %s: %s", shop["shop_name"], ex)

        log.info("[startup-variant-backfill] Hoàn tất — điền được %d tên biến thể.", new_names)
    except Exception as ex:
        log.exception("[startup-variant-backfill] Lỗi: %s", ex)


def _start_auto_sync():
    """Khởi động background sync threads trong web worker.

    MẶC ĐỊNH TẮT từ 2026-04-29 — sync đã chuyển sang APScheduler trong
    pos-scheduler.service để isolate khỏi web worker (DB pool, RAM, CPU).
    Lý do: 5 daemon thread + 12 web thread × 87 shop sync = bottleneck DB
    pool (10 connection) → web đơ khi 40+ user vào.

    APScheduler tương đương:
      • wh-auto-sync (20 phút)         → sync_kho_outbound (30 phút)
      • wh-fast-sync (5 phút)          → fast_shipped_outbound (5 phút) — MỚI
      • wh-fast-returns (5 phút)       → fast_returns (5 phút) — MỚI
      • wh-products-sync (3h)          → (TODO: thêm vào scheduler)
      • wh-variant-backfill (1 lần)    → (TODO: chạy thủ công khi cần)

    Set env WEB_KHO_BG_SYNC=1 để bật lại (rollback / debug local).
    """
    global _auto_sync_started
    if _auto_sync_started:
        return
    _auto_sync_started = True

    if os.getenv("WEB_KHO_BG_SYNC", "0") != "1":
        log.info(
            "[kho-bg-sync] DISABLED trong web worker — sync chạy qua "
            "pos-scheduler.service. Set WEB_KHO_BG_SYNC=1 để bật lại."
        )
        return

    t = threading.Thread(target=_auto_sync_loop, daemon=True, name="wh-auto-sync")
    t.start()
    print(f"[kho auto-sync] Thread khởi động — sync mỗi {AUTO_SYNC_INTERVAL // 60} phút.", flush=True)
    tf = threading.Thread(target=_fast_shipped_sync_loop, daemon=True, name="wh-fast-sync")
    tf.start()
    _fs_iv = f"{FAST_SYNC_INTERVAL}s" if FAST_SYNC_INTERVAL < 60 else f"{FAST_SYNC_INTERVAL // 60} phút"
    print(f"[kho fast-sync] Thread khởi động — shipped-only sync mỗi {_fs_iv}.", flush=True)
    tr = threading.Thread(target=_fast_returns_sync_loop, daemon=True, name="wh-fast-returns")
    tr.start()
    print(f"[kho fast-returns] Thread khởi động — returns sync mỗi {_fs_iv}.", flush=True)
    tp = threading.Thread(target=_products_auto_sync_loop, daemon=True, name="wh-products-sync")
    tp.start()
    print(f"[kho products-sync] Thread khởi động — sync sản phẩm mỗi {PRODUCTS_AUTO_SYNC_INTERVAL // 3600}h.", flush=True)
    tv = threading.Thread(target=_startup_variation_backfill, daemon=True, name="wh-variant-backfill")
    tv.start()
    print("[kho variant-backfill] Thread khởi động — điền tên biến thể sau 90 giây.", flush=True)


@bp.route("/api/last-sync")
def api_last_sync():
    return jsonify(_last_auto_sync)


# ─────────────────────────────────────────
# KIỂM KHO (STOCKTAKE)
# ─────────────────────────────────────────

@bp.route("/kiem-kho")
def kiem_kho_list():
    denied = _deny_staff()
    if denied: return denied
    with db() as conn:
        stocktakes = conn.execute("""
            SELECT st.*,
                   COUNT(si.id) as item_count,
                   SUM(ABS(si.diff)) as total_diff
            FROM wh_stocktakes st
            LEFT JOIN wh_stocktake_items si ON si.stocktake_id = st.id
            GROUP BY st.id
            ORDER BY st.created_at DESC
        """).fetchall()
    return _rt("kiem_kho_list.html", stocktakes=stocktakes)


@bp.route("/kiem-kho/moi", methods=["GET"])
def kiem_kho_moi():
    denied = _deny_by_perm("kvl_kiem_kho_tao")
    if denied: return denied
    # Optional ?st_id=X → append SP vào phiếu draft đã có (KHÔNG tạo phiếu mới).
    try:
        edit_st_id = int(request.args.get("st_id", 0))
    except (ValueError, TypeError):
        edit_st_id = 0

    with db() as conn:
        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY name"
        ).fetchall()

        edit_st = None
        existing_items_map = {}  # (product_id, pvid) → qty_counted
        if edit_st_id:
            edit_st = conn.execute(
                "SELECT * FROM wh_stocktakes WHERE id=%s", (edit_st_id,)
            ).fetchone()
            if not edit_st or edit_st["status"] != "draft":
                flash("Chỉ thêm SP cho phiếu nháp. Phiếu này không phải draft.", "warning")
                return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=edit_st_id))
            # Lock warehouse theo phiếu — không cho đổi
            selected_wh_id = edit_st["warehouse_id"]
            # Load items đã có trong phiếu
            existing = conn.execute(
                "SELECT product_id, COALESCE(pos_variation_id,'') AS pvid, qty_counted "
                "FROM wh_stocktake_items WHERE stocktake_id=%s",
                (edit_st_id,)
            ).fetchall()
            for r in existing:
                existing_items_map[(r["product_id"], r["pvid"])] = r["qty_counted"]
        else:
            try:
                selected_wh_id = int(request.args.get("warehouse_id", 0))
            except (ValueError, TypeError):
                selected_wh_id = 0
            if not selected_wh_id and warehouses:
                selected_wh_id = warehouses[0]["id"]

        # 1 row = (product × variant × warehouse).
        # - SP đã sync POS có biến thể → mỗi mẫu mã 1 row (từ wh_inventory).
        # - SP chưa sync hoặc không có biến thể → 1 row duy nhất, variant_key=NULL.
        # - POS qty = SUM(pos_remain_qty) qua tất cả shop có cùng (product_id, variant_name) — đúng nguyên lý §16.3.
        products = conn.execute("""
            SELECT p.id, p.sku, p.name, p.unit, p.category,
                   i.variant_key,
                   i.pos_variation_id,
                   COALESCE(i.qty, 0) as qty_system,
                   COALESCE(vmsum.qty_pos, 0) as qty_pos
            FROM wh_products p
            LEFT JOIN wh_inventory i
                ON i.product_id = p.id AND i.warehouse_id = %s
            LEFT JOIN (
                SELECT product_id, variant_name, SUM(pos_remain_qty) AS qty_pos
                FROM wh_variation_map
                GROUP BY product_id, variant_name
            ) vmsum
                ON vmsum.product_id = p.id AND vmsum.variant_name = i.variant_key
            ORDER BY p.category, p.name, COALESCE(i.variant_key, '')
        """, (selected_wh_id,)).fetchall()
    return _rt("kiem_kho_moi.html", products=products,
               warehouses=warehouses, selected_wh_id=selected_wh_id,
               edit_st=edit_st, edit_st_id=edit_st_id,
               existing_items_map=existing_items_map)


@bp.route("/kiem-kho/moi", methods=["POST"])
def kiem_kho_tao():
    """Tạo phiếu kiểm kho từ list SP đã TÍCH checkbox.

    Quy tắc an toàn (rút từ sự cố NV Nụ 2026-05-15):
      - CHỈ tạo item cho row CÓ checkbox tick (form key `check_<pid>__<pvid>`).
      - Ô trống / không tick → SKIP — KHÔNG mặc định = 0.
      - Đã tick + qty trống → mặc định qty_counted = qty_system (NV thấy đúng số hệ thống, không lệch).
      - Đã tick + qty không phải số → SKIP (an toàn).
    """
    denied = _deny_by_perm("kvl_kiem_kho_tao")
    if denied: return denied
    note = request.form.get("note", "").strip()
    created_by = request.form.get("created_by", "").strip() or "admin"
    try:
        warehouse_id = int(request.form.get("warehouse_id", 0) or 0)
    except (ValueError, TypeError):
        warehouse_id = 0

    # Lấy danh sách (pid, pvid) đã tick từ checkbox `check_<pid>__<pvid>`
    checked_keys = set()
    for key in request.form.keys():
        if not key.startswith("check_"):
            continue
        raw = key.replace("check_", "", 1)
        parts = raw.split("__", 1)
        pid_str = parts[0]
        pvid = parts[1] if len(parts) > 1 else ""
        if pid_str.isdigit():
            checked_keys.add((int(pid_str), pvid))

    # Optional: append SP vào phiếu draft đã có (form field `edit_st_id`)
    try:
        edit_st_id = int(request.form.get("edit_st_id", 0))
    except (ValueError, TypeError):
        edit_st_id = 0

    with db() as conn:
        if edit_st_id:
            # Append mode — load phiếu hiện tại
            existing_st = conn.execute(
                "SELECT id, stocktake_code, warehouse_id, status FROM wh_stocktakes WHERE id=%s FOR UPDATE",
                (edit_st_id,)
            ).fetchone()
            if not existing_st:
                flash("Phiếu không tồn tại.", "danger")
                return redirect(url_for("kho_vat_ly.kiem_kho_list"))
            if existing_st["status"] != "draft":
                flash(f"Phiếu đã {existing_st['status']}, không thêm SP được.", "warning")
                return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=edit_st_id))
            st_id = edit_st_id
            code = existing_st["stocktake_code"]
            warehouse_id = existing_st["warehouse_id"]
        else:
            if not warehouse_id:
                wh_row = conn.execute(
                    "SELECT id FROM wh_warehouses WHERE status='active' ORDER BY id LIMIT 1"
                ).fetchone()
                warehouse_id = wh_row["id"] if wh_row else None
            code = "KK" + now_hcm().strftime("%Y%m%d%H%M%S")
            st_id = conn.execute("""
                INSERT INTO wh_stocktakes (stocktake_code, warehouse_id, status, note, created_by)
                VALUES (%s, %s, 'draft', %s, %s)
                RETURNING id
            """, (code, warehouse_id, note or None, created_by)).fetchone()["id"]

        added = 0
        for (product_id, pvid) in checked_keys:
            qty_key = f"qty_{product_id}__{pvid}"
            qty_str = (request.form.get(qty_key) or "").strip()
            # Lookup qty_system + thông tin SP cho variant này
            if pvid:
                row = conn.execute("""
                    SELECT p.sku, p.name, p.unit, p.category,
                           v.variant_name,
                           COALESCE(
                             (SELECT i.qty FROM wh_inventory i
                                WHERE i.product_id=%s AND i.warehouse_id=%s
                                  AND i.pos_variation_id=%s LIMIT 1), 0) AS qty_system,
                           COALESCE(v.pos_remain_qty, 0) AS qty_pos
                    FROM wh_products p
                    LEFT JOIN wh_variation_map v ON v.pos_variation_id=%s AND v.product_id=p.id
                    WHERE p.id=%s
                """, (product_id, warehouse_id, pvid, pvid, product_id)).fetchone()
                variant_key = (row["variant_name"] if row else None) or None
            else:
                row = conn.execute("""
                    SELECT p.sku, p.name, p.unit, p.category,
                           NULL::text AS variant_name,
                           COALESCE(
                             (SELECT i.qty FROM wh_inventory i
                                WHERE i.product_id=%s AND i.warehouse_id=%s
                                  AND (i.pos_variation_id IS NULL OR i.pos_variation_id='')
                                LIMIT 1), 0) AS qty_system,
                           0 AS qty_pos
                    FROM wh_products p WHERE p.id=%s
                """, (product_id, warehouse_id, product_id)).fetchone()
                variant_key = None
            if not row:
                continue

            # An toàn: nếu qty_str rỗng → mặc định = qty_system (NV tick = thấy đúng, không lệch).
            # Nếu không parse được → SKIP (không default 0).
            if qty_str == "":
                qty_counted = int(row["qty_system"] or 0)
            else:
                try:
                    qty_counted = max(0, int(qty_str))
                except (ValueError, TypeError):
                    continue
            diff = qty_counted - int(row["qty_system"] or 0)
            conn.execute("""
                INSERT INTO wh_stocktake_items
                    (stocktake_id, product_id, warehouse_id, sku, product_name, unit, category,
                     variant_key, pos_variation_id,
                     qty_system, qty_pos, qty_counted, diff)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stocktake_id, product_id, COALESCE(pos_variation_id, '')) DO UPDATE SET
                    qty_counted = EXCLUDED.qty_counted,
                    diff = EXCLUDED.diff
            """, (st_id, product_id, warehouse_id, row["sku"], row["name"], row["unit"],
                  row["category"], variant_key, pvid or None,
                  int(row["qty_system"] or 0), int(row["qty_pos"] or 0),
                  qty_counted, diff))
            added += 1

    if added == 0:
        if edit_st_id:
            flash("Không có SP nào được tick để thêm. Quay lại phiếu.", "info")
        else:
            flash(f"Phiếu {code} đã tạo nhưng KHÔNG có SP nào được tick. Vào trang chi tiết để xem hoặc xoá phiếu.", "warning")
    else:
        if edit_st_id:
            flash(f"Đã thêm/cập nhật {added} SP vào phiếu {code}.", "success")
        else:
            flash(f"Đã tạo phiếu {code} với {added} SP đã kiểm. Kiểm tra chênh lệch trước khi xác nhận.", "success")
    return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))


@bp.route("/kiem-kho/<int:st_id>")
def kiem_kho_detail(st_id):
    with db() as conn:
        st = conn.execute(
            "SELECT * FROM wh_stocktakes WHERE id = %s", (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu kiểm kho.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))

        items = conn.execute("""
            SELECT * FROM wh_stocktake_items
            WHERE stocktake_id = %s
            ORDER BY category, product_name
        """, (st_id,)).fetchall()

        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()
        wh_map = {w["id"]: w["name"] for w in warehouses}

        # Lookup tồn của các kho khác cho mỗi item (để NV có context)
        # Map: {(product_id, pos_variation_id_or_'') : {wh_id: qty}}
        kho_breakdown: dict = {}
        if items:
            product_ids = list({it["product_id"] for it in items})
            inv_rows = conn.execute("""
                SELECT product_id, warehouse_id,
                       COALESCE(pos_variation_id,'') AS pvid_key,
                       SUM(qty) AS qty
                FROM wh_inventory
                WHERE product_id = ANY(%s)
                GROUP BY product_id, warehouse_id, COALESCE(pos_variation_id,'')
            """, (product_ids,)).fetchall()
            for r in inv_rows:
                key = (r["product_id"], r["pvid_key"])
                kho_breakdown.setdefault(key, {})[r["warehouse_id"]] = int(r["qty"] or 0)

        # Đính kèm vào items
        items = [dict(it) for it in items]
        for it in items:
            key = (it["product_id"], it.get("pos_variation_id") or "")
            it["kho_qty_map"] = kho_breakdown.get(key, {})

        # Build "danh sách SP để kiểm" — tất cả SP có inv > 0 trong kho đang kiểm
        # Mỗi SP đa biến thể → N row (1/variant). Đính kèm qty_counted đã thêm.
        all_sp = []
        if st["status"] == "draft":
            items_dict = {(it["product_id"], it.get("pos_variation_id") or ""): it
                          for it in items}
            sp_rows = conn.execute("""
                WITH inv AS (
                    SELECT product_id, COALESCE(pos_variation_id,'') AS pvid_key,
                           SUM(qty) AS qty
                    FROM wh_inventory
                    WHERE warehouse_id = %s
                    GROUP BY product_id, COALESCE(pos_variation_id,'')
                )
                SELECT inv.product_id, inv.pvid_key, inv.qty AS qty_system,
                       p.sku, p.name, p.unit, p.category,
                       v.variant_name,
                       COALESCE(v.pos_remain_qty, 0) AS qty_pos
                FROM inv
                JOIN wh_products p ON p.id = inv.product_id
                LEFT JOIN wh_variation_map v ON v.pos_variation_id = NULLIF(inv.pvid_key,'')
                WHERE inv.qty > 0
                  AND COALESCE(p.hidden, false) = false
                ORDER BY p.category NULLS LAST, p.name, v.variant_name NULLS FIRST
            """, (st["warehouse_id"],)).fetchall()
            for r in sp_rows:
                pvid = r["pvid_key"] or None
                key = (r["product_id"], pvid or "")
                existing = items_dict.get(key)
                all_sp.append({
                    "product_id": r["product_id"],
                    "pos_variation_id": pvid,
                    "sku": r["sku"],
                    "name": r["name"],
                    "unit": r["unit"] or "",
                    "category": r["category"] or "",
                    "variant_key": r["variant_name"] or "",
                    "qty_system": int(r["qty_system"] or 0),
                    "qty_pos": int(r["qty_pos"] or 0),
                    "is_checked": bool(existing),
                    "qty_counted": int(existing["qty_counted"]) if existing else None,
                    "item_id": existing["id"] if existing else None,
                })

    total_items = len(items)
    items_diff = [r for r in items if r["diff"] != 0]
    total_diff_up = sum(r["diff"] for r in items if r["diff"] > 0)
    total_diff_down = sum(r["diff"] for r in items if r["diff"] < 0)

    return _rt("kiem_kho_detail.html",
               st=st, items=items, items_diff=items_diff,
               total_items=total_items, wh_map=wh_map,
               warehouses=warehouses,
               all_sp=all_sp if st["status"] == "draft" else [],
               total_diff_up=total_diff_up,
               total_diff_down=abs(total_diff_down))


@bp.route("/kiem-kho/<int:st_id>/ap-dung", methods=["POST"])
def kiem_kho_ap_dung(st_id):
    denied = _deny_staff()
    if denied: return denied
    confirmed_by = request.form.get("confirmed_by", "admin").strip()
    only_diff = request.form.get("only_diff", "1") == "1"

    with db() as conn:
        # P0 #3 — LOCK phiếu (FOR UPDATE) trước khi check status để chặn race
        # condition: 2 admin cùng bấm "Áp dụng" → đều thấy draft → cả 2 apply
        # → double trừ tồn. Lock này giữ đến hết transaction (with db).
        st = conn.execute(
            "SELECT * FROM wh_stocktakes WHERE id = %s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu kiểm kho.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))
        if st["status"] == "confirmed":
            flash("Phiếu này đã được xác nhận rồi.", "warning")
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))
        if st["status"] == "cancelled":
            flash("Phiếu này đã huỷ, không thể áp dụng.", "warning")
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))

        query = """
            SELECT * FROM wh_stocktake_items WHERE stocktake_id = %s
        """
        if only_diff:
            query += " AND diff <> 0"
        items = conn.execute(query, (st_id,)).fetchall()

        wh_id = st["warehouse_id"] if st["warehouse_id"] else None

        adjusted = 0
        for item in items:
            qty_after = item["qty_counted"]
            item_wh_id = item["warehouse_id"] if item["warehouse_id"] else wh_id

            # P0 #4 — Lookup qty CURRENT của inv row (KHÔNG dùng qty_system
            # snapshot từ lúc tạo phiếu — có thể stale nếu giữa lúc tạo và apply
            # có xuất/nhập khác). _upsert_inventory đã handle set qty=qty_after
            # đúng, chỉ ảnh hưởng movement.qty_before để audit trail chính xác.
            pvid = item.get("pos_variation_id")
            if pvid:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                    (item["product_id"], item_wh_id, pvid)
                ).fetchone()
            else:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s "
                    "AND (pos_variation_id IS NULL OR pos_variation_id='')",
                    (item["product_id"], item_wh_id)
                ).fetchone()
            qty_before_real = cur_row["qty"] if cur_row else 0
            delta = qty_after - qty_before_real

            # Theo §18 (vmap là truth): truyền pvid + variant_key thẳng vào
            # _upsert_inventory. Hàm này tự lookup đúng row variant-level — không
            # corrupt SP base row.
            _upsert_inventory(
                conn, item["product_id"], item_wh_id, qty_after,
                pos_variation_id=pvid,
                variant_key=item.get("variant_key"),
            )

            # Skip nếu không có thay đổi thực sự (qty_counted = qty hiện tại)
            if delta == 0:
                continue
            move_type = "adjustment_up" if delta > 0 else "adjustment_down"
            conn.execute("""
                INSERT INTO wh_stock_movements
                    (type, product_id, warehouse_id, qty, qty_before, qty_after, pos_variation_id, note, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (move_type, item["product_id"], item_wh_id, abs(delta),
                  qty_before_real, qty_after, pvid,
                  f"Kiểm kho {st['stocktake_code']}", confirmed_by))
            adjusted += 1

        conn.execute("""
            UPDATE wh_stocktakes
            SET status = 'confirmed', confirmed_by = %s, confirmed_at = NOW(), updated_at = NOW()
            WHERE id = %s
        """, (confirmed_by, st_id))

    flash(f"Đã xác nhận và điều chỉnh {adjusted} sản phẩm.", "success")
    return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))


@bp.route("/kiem-kho/<int:st_id>/huy", methods=["POST"])
def kiem_kho_huy(st_id):
    denied = _deny_staff()
    if denied: return denied
    with db() as conn:
        conn.execute("""
            UPDATE wh_stocktakes SET status = 'cancelled', updated_at = NOW()
            WHERE id = %s AND status = 'draft'
        """, (st_id,))
    flash("Đã huỷ phiếu kiểm kho.", "info")
    return redirect(url_for("kho_vat_ly.kiem_kho_list"))


@bp.route("/kiem-kho/api/search-sp")
def kiem_kho_search_sp():
    """API autocomplete SKU/tên SP cho widget Thêm SKU. Trả JSON.

    Query: ?q=<text>&warehouse_id=<id>&limit=20

    Trả về: [{product_id, sku, name, variant_key, pos_variation_id, qty_system, qty_pos}]
    Mỗi SKU đa biến thể → trả về N rows (1 row/variant).
    """
    from flask import jsonify
    q = (request.args.get("q") or "").strip()
    try:
        warehouse_id = int(request.args.get("warehouse_id", 0) or 0)
    except (ValueError, TypeError):
        warehouse_id = 0
    try:
        limit = min(50, max(5, int(request.args.get("limit", 20))))
    except (ValueError, TypeError):
        limit = 20
    if not q:
        return jsonify({"items": []})

    pattern = f"%{q.upper()}%"
    with db() as conn:
        rows = conn.execute("""
            WITH matched AS (
                SELECT p.id, p.sku, p.name, p.unit
                FROM wh_products p
                WHERE UPPER(p.sku) LIKE %s OR UPPER(p.name) LIKE %s
                ORDER BY (UPPER(p.sku) = %s) DESC, p.name
                LIMIT %s
            )
            SELECT m.id AS product_id, m.sku, m.name, m.unit,
                   v.pos_variation_id, v.variant_name,
                   COALESCE(
                       (SELECT i.qty FROM wh_inventory i
                          WHERE i.product_id = m.id
                            AND i.warehouse_id = %s
                            AND (
                              (v.pos_variation_id IS NULL AND (i.pos_variation_id IS NULL OR i.pos_variation_id=''))
                              OR i.pos_variation_id = v.pos_variation_id
                            )
                          LIMIT 1),
                       0
                   ) AS qty_system,
                   COALESCE(v.pos_remain_qty, 0) AS qty_pos
            FROM matched m
            LEFT JOIN wh_variation_map v ON v.product_id = m.id
            ORDER BY m.name, v.variant_name NULLS FIRST
        """, (pattern, pattern, q.upper(), limit, warehouse_id)).fetchall()

    items = []
    for r in rows:
        items.append({
            "product_id": r["product_id"],
            "sku": r["sku"],
            "name": r["name"],
            "unit": r["unit"] or "",
            "pos_variation_id": r["pos_variation_id"],
            "variant_key": r["variant_name"] or "",
            "qty_system": int(r["qty_system"] or 0),
            "qty_pos": int(r["qty_pos"] or 0),
        })
    return jsonify({"items": items})


@bp.route("/kiem-kho/<int:st_id>/them-item", methods=["POST"])
def kiem_kho_them_item(st_id):
    """Thêm 1 SKU vào phiếu kiểm kho draft.

    Form: product_id, qty_counted, pos_variation_id (optional)
    Nếu phiếu đã có item cho cùng (product_id, pos_variation_id) → UPDATE qty_counted (tích lũy).
    """
    from flask import jsonify
    denied = _deny_staff()
    if denied: return jsonify({"ok": False, "error": "denied"}), 403

    pid_str = request.form.get("product_id", "").strip()
    qty_str = request.form.get("qty_counted", "").strip()
    pvid = request.form.get("pos_variation_id", "").strip() or None
    mode = request.form.get("mode", "set").strip()  # 'set' hoặc 'add' (cộng dồn)
    if not pid_str.isdigit() or qty_str == "":
        return jsonify({"ok": False, "error": "Thiếu product_id hoặc qty_counted"}), 400
    try:
        qty_counted_input = max(0, int(qty_str))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "qty_counted không phải số"}), 400
    product_id = int(pid_str)

    with db() as conn:
        st = conn.execute(
            "SELECT id, status, warehouse_id FROM wh_stocktakes WHERE id=%s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            return jsonify({"ok": False, "error": "Không tìm thấy phiếu"}), 404
        if st["status"] != "draft":
            return jsonify({"ok": False, "error": f"Phiếu đã {st['status']}, không thêm được"}), 400
        wh_id = st["warehouse_id"]
        # Lookup SP info + qty_system + qty_pos
        if pvid:
            row = conn.execute("""
                SELECT p.sku, p.name, p.unit, p.category,
                       v.variant_name,
                       COALESCE(
                         (SELECT i.qty FROM wh_inventory i
                            WHERE i.product_id=%s AND i.warehouse_id=%s AND i.pos_variation_id=%s
                            LIMIT 1), 0) AS qty_system,
                       COALESCE(v.pos_remain_qty, 0) AS qty_pos
                FROM wh_products p
                LEFT JOIN wh_variation_map v ON v.pos_variation_id=%s AND v.product_id=p.id
                WHERE p.id=%s
            """, (product_id, wh_id, pvid, pvid, product_id)).fetchone()
            variant_key = (row["variant_name"] if row else None) or None
        else:
            row = conn.execute("""
                SELECT p.sku, p.name, p.unit, p.category,
                       NULL::text AS variant_name,
                       COALESCE(
                         (SELECT i.qty FROM wh_inventory i
                            WHERE i.product_id=%s AND i.warehouse_id=%s
                              AND (i.pos_variation_id IS NULL OR i.pos_variation_id='')
                            LIMIT 1), 0) AS qty_system,
                       0 AS qty_pos
                FROM wh_products p
                WHERE p.id=%s
            """, (product_id, wh_id, product_id)).fetchone()
            variant_key = None
        if not row:
            return jsonify({"ok": False, "error": "Không tìm thấy sản phẩm"}), 404

        # Check item đã tồn tại trong phiếu chưa
        existing = conn.execute("""
            SELECT id, qty_counted FROM wh_stocktake_items
            WHERE stocktake_id=%s AND product_id=%s
              AND COALESCE(pos_variation_id,'') = COALESCE(%s,'')
        """, (st_id, product_id, pvid)).fetchone()

        if existing:
            if mode == "add":
                new_qty = (existing["qty_counted"] or 0) + qty_counted_input
            else:
                new_qty = qty_counted_input
            diff = new_qty - row["qty_system"]
            conn.execute("""
                UPDATE wh_stocktake_items
                SET qty_counted=%s, diff=%s
                WHERE id=%s
            """, (new_qty, diff, existing["id"]))
            item_id = existing["id"]
            action = "updated"
        else:
            diff = qty_counted_input - row["qty_system"]
            new_id = conn.execute("""
                INSERT INTO wh_stocktake_items
                    (stocktake_id, product_id, warehouse_id, sku, product_name, unit, category,
                     variant_key, pos_variation_id,
                     qty_system, qty_pos, qty_counted, diff)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (st_id, product_id, wh_id, row["sku"], row["name"], row["unit"],
                  row["category"], variant_key, pvid,
                  row["qty_system"], row["qty_pos"],
                  qty_counted_input, diff)).fetchone()
            item_id = new_id["id"]
            new_qty = qty_counted_input
            action = "inserted"

    return jsonify({
        "ok": True,
        "action": action,
        "item_id": item_id,
        "sku": row["sku"],
        "name": row["name"],
        "variant_key": variant_key or "",
        "qty_system": int(row["qty_system"]),
        "qty_counted": int(new_qty),
        "diff": int(diff),
    })


@bp.route("/kiem-kho/<int:st_id>/xoa-item", methods=["POST"])
def kiem_kho_xoa_item(st_id):
    """Xoá 1 item khỏi phiếu draft."""
    from flask import jsonify
    denied = _deny_staff()
    if denied: return jsonify({"ok": False, "error": "denied"}), 403
    item_id_str = request.form.get("item_id", "").strip()
    if not item_id_str.isdigit():
        return jsonify({"ok": False, "error": "Thiếu item_id"}), 400
    with db() as conn:
        st = conn.execute(
            "SELECT status FROM wh_stocktakes WHERE id=%s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            return jsonify({"ok": False, "error": "Phiếu không tồn tại"}), 404
        if st["status"] != "draft":
            return jsonify({"ok": False, "error": f"Phiếu đã {st['status']}, không xoá item được"}), 400
        conn.execute(
            "DELETE FROM wh_stocktake_items WHERE id=%s AND stocktake_id=%s",
            (int(item_id_str), st_id),
        )
    return jsonify({"ok": True})


@bp.route("/kiem-kho/<int:st_id>/sua-item", methods=["POST"])
def kiem_kho_sua_item(st_id):
    """Sửa qty_counted của 1 item trong phiếu draft. Recompute diff.
    Form: item_id, qty_counted (int).
    Chỉ cho phép khi phiếu ở trạng thái draft.
    """
    denied = _deny_staff()
    if denied: return denied
    item_id_str = request.form.get("item_id", "").strip()
    qty_str = request.form.get("qty_counted", "").strip()
    if not item_id_str.isdigit() or qty_str == "":
        flash("Thiếu item_id hoặc qty_counted.", "danger")
        return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))
    try:
        qty_counted = max(0, int(qty_str))
    except (ValueError, TypeError):
        flash("qty_counted không phải số.", "danger")
        return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))

    with db() as conn:
        st = conn.execute(
            "SELECT status FROM wh_stocktakes WHERE id=%s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))
        if st["status"] != "draft":
            flash(f"Phiếu đã {st['status']}, không sửa được. Rollback trước nếu muốn sửa.", "warning")
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))
        # Update item + recompute diff
        conn.execute("""
            UPDATE wh_stocktake_items
            SET qty_counted = %s,
                diff = %s - qty_system
            WHERE id = %s AND stocktake_id = %s
        """, (qty_counted, qty_counted, int(item_id_str), st_id))
    flash("Đã cập nhật số lượng kiểm.", "success")
    return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))


@bp.route("/kiem-kho/<int:st_id>/rollback", methods=["POST"])
def kiem_kho_rollback(st_id):
    """Rollback phiếu đã confirmed: đảo các movement (adjustment_up/down) → restore inv.
    Chỉ cho phép trên phiếu status='confirmed'. Lock phiếu trước khi xử lý."""
    denied = _deny_staff()
    if denied: return denied
    rolled_by = request.form.get("rolled_by", "admin").strip() or "admin"
    reason = request.form.get("reason", "").strip() or "rollback"

    with db() as conn:
        st = conn.execute(
            "SELECT * FROM wh_stocktakes WHERE id=%s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))
        if st["status"] != "confirmed":
            flash(f"Chỉ rollback được phiếu confirmed (hiện: {st['status']}).", "warning")
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))

        note_tag = f"Kiểm kho {st['stocktake_code']}"
        movements = conn.execute("""
            SELECT id, product_id, warehouse_id, qty_before, qty_after, pos_variation_id
            FROM wh_stock_movements
            WHERE note = %s AND type IN ('adjustment_up','adjustment_down')
            ORDER BY id
        """, (note_tag,)).fetchall()

        restored = 0
        for mv in movements:
            pvid = mv.get("pos_variation_id")
            # Lookup inv hiện tại
            if pvid:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                    (mv["product_id"], mv["warehouse_id"], pvid)
                ).fetchone()
            else:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s "
                    "AND (pos_variation_id IS NULL OR pos_variation_id='')",
                    (mv["product_id"], mv["warehouse_id"])
                ).fetchone()
            qty_now = cur_row["qty"] if cur_row else 0
            qty_target = mv["qty_before"]  # restore về trước khi apply
            # Set inv = qty_target
            _upsert_inventory(
                conn, mv["product_id"], mv["warehouse_id"], qty_target,
                pos_variation_id=pvid,
            )
            # Ghi reverse movement
            delta = qty_target - qty_now
            if delta != 0:
                conn.execute("""
                    INSERT INTO wh_stock_movements
                        (type, product_id, warehouse_id, qty, qty_before, qty_after,
                         pos_variation_id, note, created_by)
                    VALUES ('rollback_stocktake', %s, %s, %s, %s, %s, %s, %s, %s)
                """, (mv["product_id"], mv["warehouse_id"], abs(delta),
                      qty_now, qty_target, pvid,
                      f"Rollback {note_tag} — {reason}", rolled_by))
            restored += 1

        conn.execute("""
            UPDATE wh_stocktakes
            SET status='cancelled',
                note = COALESCE(note,'') || ' [ROLLBACK by ' || %s || ' — ' || %s || ']',
                updated_at = NOW()
            WHERE id = %s
        """, (rolled_by, reason, st_id))
    flash(f"Đã rollback {restored} movements. Tồn kho khôi phục về trước khi apply.", "success")
    return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))


@bp.route("/kiem-kho/<int:st_id>/mo-lai", methods=["POST"])
def kiem_kho_mo_lai(st_id):
    """Mở lại phiếu đã confirmed: rollback movements + reset status='draft'.
    Cho phép NV sửa lại items rồi apply lại.
    Chỉ áp dụng phiếu status='confirmed'.
    """
    denied = _deny_staff()
    if denied: return denied
    rolled_by = request.form.get("rolled_by") or session.get("username", "") or "admin"

    with db() as conn:
        st = conn.execute(
            "SELECT * FROM wh_stocktakes WHERE id=%s FOR UPDATE", (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))
        if st["status"] != "confirmed":
            flash(f"Chỉ mở lại được phiếu đã xác nhận (hiện: {st['status']}).", "warning")
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))

        note_tag = f"Kiểm kho {st['stocktake_code']}"
        movements = conn.execute("""
            SELECT id, product_id, warehouse_id, qty_before, qty_after, pos_variation_id
            FROM wh_stock_movements
            WHERE note = %s AND type IN ('adjustment_up','adjustment_down')
            ORDER BY id
        """, (note_tag,)).fetchall()
        restored = 0
        for mv in movements:
            pvid = mv.get("pos_variation_id")
            if pvid:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s AND pos_variation_id=%s",
                    (mv["product_id"], mv["warehouse_id"], pvid)
                ).fetchone()
            else:
                cur_row = conn.execute(
                    "SELECT COALESCE(qty,0) AS qty FROM wh_inventory "
                    "WHERE product_id=%s AND warehouse_id=%s "
                    "AND (pos_variation_id IS NULL OR pos_variation_id='')",
                    (mv["product_id"], mv["warehouse_id"])
                ).fetchone()
            qty_now = cur_row["qty"] if cur_row else 0
            qty_target = mv["qty_before"]
            _upsert_inventory(
                conn, mv["product_id"], mv["warehouse_id"], qty_target,
                pos_variation_id=pvid,
            )
            delta = qty_target - qty_now
            if delta != 0:
                conn.execute("""
                    INSERT INTO wh_stock_movements
                        (type, product_id, warehouse_id, qty, qty_before, qty_after,
                         pos_variation_id, note, created_by)
                    VALUES ('reopen_stocktake', %s, %s, %s, %s, %s, %s, %s, %s)
                """, (mv["product_id"], mv["warehouse_id"], abs(delta),
                      qty_now, qty_target, pvid,
                      f"Mở lại {note_tag} để sửa", rolled_by))
            restored += 1

        # Cập nhật phiếu: status='draft', clear confirmed fields
        conn.execute("""
            UPDATE wh_stocktakes
            SET status='draft',
                confirmed_by=NULL,
                confirmed_at=NULL,
                note = COALESCE(note,'') || ' [REOPENED by ' || %s || ']',
                updated_at = NOW()
            WHERE id=%s
        """, (rolled_by, st_id))

    flash(f"Đã mở lại phiếu {st['stocktake_code']}. Tồn khôi phục cho {restored} SP. Sửa xong → bấm 'Xác nhận điều chỉnh' lại.", "success")
    return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))


@bp.route("/kiem-kho/<int:st_id>/xoa", methods=["POST"])
def kiem_kho_xoa(st_id):
    """Xoá hẳn phiếu kiểm kho khỏi DB. Chỉ xoá được nếu status IN ('draft','cancelled').
    KHÔNG xoá phiếu confirmed (phải rollback trước → status sẽ thành cancelled)."""
    denied = _deny_staff()
    if denied: return denied
    with db() as conn:
        st = conn.execute(
            "SELECT status, stocktake_code FROM wh_stocktakes WHERE id=%s FOR UPDATE",
            (st_id,)
        ).fetchone()
        if not st:
            flash("Không tìm thấy phiếu.", "danger")
            return redirect(url_for("kho_vat_ly.kiem_kho_list"))
        if st["status"] not in ("draft", "cancelled"):
            flash(
                f"Phiếu {st['stocktake_code']} đang ở trạng thái {st['status']}. "
                "Phải rollback (chuyển về cancelled) trước khi xoá.",
                "warning"
            )
            return redirect(url_for("kho_vat_ly.kiem_kho_detail", st_id=st_id))
        conn.execute("DELETE FROM wh_stocktake_items WHERE stocktake_id=%s", (st_id,))
        conn.execute("DELETE FROM wh_stocktakes WHERE id=%s", (st_id,))
    flash(f"Đã xoá phiếu {st['stocktake_code']}.", "info")
    return redirect(url_for("kho_vat_ly.kiem_kho_list"))




# ─────────────────────────────────────────
# QUẢN LÝ SẢN PHẨM
# ─────────────────────────────────────────

@bp.route("/san-pham")
def san_pham_list():
    """Danh sách sản phẩm — leader/staff chỉ thấy SP có trong shop của team/bản thân.
    Full roles (kho/admin/kế toán) thấy toàn bộ + tồn vật lý."""
    q = (request.args.get("q") or "").strip().lower()
    filter_cat = (request.args.get("cat") or "").strip()
    show_hidden = (request.args.get("show_hidden") or "").strip() in ("1", "true", "on")

    role = session.get("role", "staff")
    user_id = session.get("user_id")
    allowed_shops = _get_allowed_shop_names(user_id, role)
    # allowed_shops: None = full role (no filter); list = filter by these shop_names

    hidden_clause = "" if show_hidden else " AND COALESCE(p.hidden, false) = false"
    # Ẩn SP mà MỌI biến thể đều bị TẮT trên Pancake (is_locked) = shop không còn bán.
    # Chỉ áp khi SP CÓ biến thể; SP nhập tay (không có vmap) không bị ẩn. show_hidden bỏ qua.
    locked_clause = "" if show_hidden else (
        " AND NOT (EXISTS (SELECT 1 FROM wh_variation_map vlk WHERE vlk.product_id=p.id)"
        " AND NOT EXISTS (SELECT 1 FROM wh_variation_map vlk2 WHERE vlk2.product_id=p.id"
        " AND COALESCE(vlk2.is_locked,false)=false))"
    )

    with db() as conn:
        if allowed_shops is None:
            # Full role: xem toàn bộ SP + tồn vật lý tổng + tồn POS tổng
            # qty_pos: ưu tiên SUM wh_variation_map.pos_remain_qty (tách đúng từng
            # biến thể, không bị nhân đôi khi SP được map tới nhiều shop). Fallback
            # wh_shop_inventory chỉ khi SP không có variation map.
            # (wh_shop_inventory duplicate qty_pos giữa các shop cùng pool → SUM sai.)
            rows = conn.execute(f"""
                SELECT p.id, p.sku, p.name, p.unit, p.category,
                       COALESCE(p.gia_nhap, 0) AS gia_nhap,
                       COALESCE(p.gia_ban, 0)  AS gia_ban,
                       COALESCE(p.min_qty, 0)  AS min_qty,
                       COALESCE(p.hidden, false) AS hidden,
                       COALESCE((SELECT SUM(i.qty) FROM wh_inventory i
                                  WHERE i.product_id=p.id), 0) AS qty_vl,
                       COALESCE((SELECT SUM(i.qty) FROM wh_inventory i
                                  WHERE i.product_id=p.id AND i.warehouse_id=1), 0) AS qty_vx,
                       COALESCE((SELECT SUM(i.qty) FROM wh_inventory i
                                  WHERE i.product_id=p.id AND i.warehouse_id=6), 0) AS qty_tc,
                       COALESCE(
                         (SELECT SUM(vm2.pos_remain_qty) FROM wh_variation_map vm2
                           WHERE vm2.product_id=p.id),
                         (SELECT SUM(si.qty_pos) FROM wh_shop_inventory si
                           WHERE si.product_id=p.id),
                         0
                       ) AS qty_pos,
                       COUNT(vm.pos_variation_id) AS n_var,
                       COUNT(CASE WHEN COALESCE(vm.variant_name,'')='' THEN 1 END) AS n_unnamed
                FROM wh_products p
                LEFT JOIN wh_variation_map vm ON vm.product_id=p.id
                WHERE TRIM(p.sku)!='' AND TRIM(p.name)!=''{hidden_clause}{locked_clause}
                GROUP BY p.id, p.sku, p.name, p.unit, p.category,
                         p.gia_nhap, p.gia_ban, p.min_qty, p.hidden
                ORDER BY p.category NULLS LAST, p.name
            """).fetchall()
        elif not allowed_shops:
            rows = []
        else:
            # Leader/staff: chỉ SP có trong shop được phép; qty_vl = 0 (không quản lý kho VL)
            rows = conn.execute(f"""
                SELECT p.id, p.sku, p.name, p.unit, p.category,
                       COALESCE(p.gia_nhap, 0) AS gia_nhap,
                       COALESCE(p.gia_ban, 0)  AS gia_ban,
                       COALESCE(p.min_qty, 0)  AS min_qty,
                       COALESCE(p.hidden, false) AS hidden,
                       0 AS qty_vl,
                       0 AS qty_vx,
                       0 AS qty_tc,
                       COALESCE(SUM(si.qty_pos), 0) AS qty_pos,
                       0 AS n_var,
                       0 AS n_unnamed
                FROM wh_products p
                INNER JOIN wh_shop_inventory si ON si.product_id = p.id
                INNER JOIN wh_shops s ON s.id = si.shop_id
                WHERE TRIM(p.sku)!='' AND TRIM(p.name)!=''
                  AND s.shop_name = ANY(%s){hidden_clause}
                GROUP BY p.id, p.sku, p.name, p.unit, p.category,
                         p.gia_nhap, p.gia_ban, p.min_qty, p.hidden
                HAVING SUM(si.qty_pos) > 0
                ORDER BY p.category NULLS LAST, p.name
            """, (allowed_shops,)).fetchall()
        categories = sorted({r["category"] or "" for r in rows if (r["category"] or "").strip()})

        hidden_count = 0
        if allowed_shops is None:
            hidden_count = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_products WHERE COALESCE(hidden, false) = true "
                "AND TRIM(sku)!='' AND TRIM(name)!=''"
            ).fetchone()["c"]

    products = []
    for r in rows:
        if q and q not in (r["name"] or "").lower() and q not in (r["sku"] or "").lower():
            continue
        if filter_cat and (r["category"] or "") != filter_cat:
            continue
        products.append(dict(r))

    # Phân trang server-side: 30 SP/trang. Giữ nguyên semantic `total` = tổng SP sau filter.
    PAGE_SIZE = 30
    total = len(products)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = int(request.args.get("page", 1) or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    start = (page - 1) * PAGE_SIZE
    products = products[start:start + PAGE_SIZE]

    can_edit = _has_perm("kvl_san_pham_sua")
    return _rt("san_pham_list.html",
               products=products, q=q, filter_cat=filter_cat,
               categories=categories, total=total,
               page=page, total_pages=total_pages, page_size=PAGE_SIZE,
               can_edit=can_edit, scope_limited=(allowed_shops is not None),
               show_hidden=show_hidden, hidden_count=hidden_count)


@bp.route("/san-pham/<int:product_id>/an", methods=["POST"])
def san_pham_toggle_hidden(product_id: int):
    """Toggle ẩn/bỏ ẩn SP. KHÔNG xóa khỏi DB — chỉ set cờ wh_products.hidden.
    Đơn hàng + biến thể + lịch sử giữ nguyên (FK references không bị động).
    """
    denied = _deny_by_perm("kvl_san_pham_sua")
    if denied: return denied
    action = (request.form.get("action") or "hide").strip()
    new_hidden = (action == "hide")
    with db() as conn:
        prod = conn.execute("SELECT id, sku, name FROM wh_products WHERE id=%s",
                            (product_id,)).fetchone()
        if not prod:
            flash("Không tìm thấy sản phẩm.", "danger")
            return redirect(url_for(".san_pham_list"))
        conn.execute("UPDATE wh_products SET hidden=%s WHERE id=%s",
                     (new_hidden, product_id))
    if new_hidden:
        flash(f"Đã ẩn SP «{prod['name']}» ({prod['sku']}). Đơn cũ giữ nguyên.", "success")
        return redirect(url_for(".san_pham_list"))
    flash(f"Đã bỏ ẩn SP «{prod['name']}» ({prod['sku']}).", "success")
    return redirect(url_for(".san_pham_list", show_hidden=1))


@bp.route("/san-pham/<int:product_id>/sua", methods=["GET", "POST"])
def san_pham_sua(product_id: int):
    """Trang sửa sản phẩm: thông tin cơ bản + quản lý biến thể."""
    denied = _deny_by_perm("kvl_san_pham_sua")
    if denied: return denied
    with db() as conn:
        prod = conn.execute("SELECT * FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            flash("Không tìm thấy sản phẩm.", "danger")
            return redirect(url_for(".san_pham_list"))

        if request.method == "POST":
            action = request.form.get("action", "info")

            if action == "info":
                # Sửa thông tin cơ bản
                name     = request.form.get("name", "").strip()
                sku      = request.form.get("sku", "").strip()
                unit     = request.form.get("unit", "").strip()
                category = request.form.get("category", "").strip()
                min_qty_s = request.form.get("min_qty", "0").strip()
                gia_nhap_s = request.form.get("gia_nhap", "0").strip().replace(",","")
                gia_ban_s  = request.form.get("gia_ban", "0").strip().replace(",","")
                try: min_qty  = int(float(min_qty_s or 0))
                except: min_qty = 0
                try: gia_nhap = float(gia_nhap_s or 0)
                except: gia_nhap = 0.0
                try: gia_ban  = float(gia_ban_s or 0)
                except: gia_ban = 0.0
                if not name:
                    flash("Tên sản phẩm không được để trống.", "danger")
                elif not sku:
                    flash("SKU không được để trống.", "danger")
                else:
                    # Kiểm tra SKU trùng
                    dup = conn.execute(
                        "SELECT id FROM wh_products WHERE sku=%s AND id!=%s", (sku, product_id)
                    ).fetchone()
                    if dup:
                        flash(f"SKU '{sku}' đã tồn tại ở sản phẩm khác (ID {dup['id']}).", "danger")
                    else:
                        conn.execute("""
                            UPDATE wh_products SET name=%s, sku=%s, unit=%s, category=%s,
                                   min_qty=%s, gia_nhap=%s, gia_ban=%s
                            WHERE id=%s
                        """, (name, sku, unit, category, min_qty, gia_nhap, gia_ban, product_id))
                        flash("Đã lưu thông tin sản phẩm.", "success")
                return redirect(url_for(".san_pham_sua", product_id=product_id))

            elif action == "variants":
                # Đặt tên biến thể (không cập nhật qty ở đây)
                raw_vars = conn.execute(
                    "SELECT pos_variation_id FROM wh_variation_map WHERE product_id=%s",
                    (product_id,)
                ).fetchall()
                for v in raw_vars:
                    vid = v["pos_variation_id"]
                    vname = (request.form.get(f"vname_{vid}") or "").strip()
                    conn.execute(
                        "UPDATE wh_variation_map SET variant_name=%s WHERE pos_variation_id=%s",
                        (vname, vid)
                    )
                flash("Đã lưu tên biến thể.", "success")
                return redirect(url_for(".san_pham_sua", product_id=product_id))

            elif action == "variant_qty":
                # Chỉ full roles được sửa số lượng tồn kho (admin/manager/kế toán/kho)
                denied = _deny_non_full()
                if denied: return denied
                pvid = (request.form.get("pos_variation_id") or "").strip()
                try:
                    wh_id = int(request.form.get("warehouse_id") or 0)
                except: wh_id = 0
                try:
                    new_qty = int(float((request.form.get("new_qty") or "0").replace(",","")))
                except: new_qty = -1
                if not pvid or wh_id <= 0 or new_qty < 0:
                    flash("Dữ liệu không hợp lệ (thiếu biến thể, kho, hoặc số lượng âm).", "danger")
                    return redirect(url_for(".san_pham_sua", product_id=product_id))
                inv, _ = _get_variant_inv(conn, product_id, wh_id, pvid)
                qty_before = inv["qty"] if inv else 0
                delta = new_qty - qty_before
                if delta == 0:
                    flash("Số lượng không đổi.", "info")
                    return redirect(url_for(".san_pham_sua", product_id=product_id))
                _upsert_inventory(conn, product_id, wh_id, new_qty, pos_variation_id=pvid)
                username = session.get("username", "admin")
                conn.execute("""
                    INSERT INTO wh_stock_movements
                      (type, product_id, warehouse_id, qty, qty_before, qty_after, note, created_at, pos_variation_id)
                    VALUES ('adjustment', %s, %s, %s, %s, %s, %s, %s, %s)
                """, (product_id, wh_id, delta, qty_before, new_qty,
                      f"Sửa tồn tại trang SP (bởi {username})", now_vn(), pvid))
                flash(f"Đã cập nhật tồn: {qty_before} → {new_qty} (Δ {delta:+d}).", "success")
                return redirect(url_for(".san_pham_sua", product_id=product_id))

        # GET: load data
        prod = conn.execute("SELECT * FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        variants_raw = conn.execute("""
            SELECT vm.pos_variation_id, vm.variant_name,
                   COALESCE(vm.pos_remain_qty, 0) AS pos_remain_qty,
                   vm.pos_remain_updated_at,
                   COALESCE(
                     CASE
                       WHEN (SELECT COUNT(*) FROM wh_variation_map vm2 WHERE vm2.product_id = vm.product_id) <= 1
                       THEN (SELECT SUM(wi.qty) FROM wh_inventory wi WHERE wi.product_id = vm.product_id)
                       ELSE (SELECT SUM(wi.qty) FROM wh_inventory wi
                              WHERE wi.product_id=vm.product_id AND wi.variant_key=vm.variant_name
                              AND wi.variant_key IS NOT NULL AND wi.variant_key!='')
                     END, 0) AS qty_vl
            FROM wh_variation_map vm
            WHERE vm.product_id=%s
            ORDER BY vm.variant_name NULLS LAST, vm.pos_variation_id
        """, (product_id,)).fetchall()
        warehouses = conn.execute(
            "SELECT id, name FROM wh_warehouses WHERE status='active' ORDER BY id"
        ).fetchall()
        # Tồn theo (variant_key, warehouse_id) để hiện ô nhập chỉnh sửa
        inv_rows = conn.execute("""
            SELECT warehouse_id, variant_key, pos_variation_id, SUM(qty) AS q
            FROM wh_inventory WHERE product_id=%s
            GROUP BY warehouse_id, variant_key, pos_variation_id
        """, (product_id,)).fetchall()
        inv_by_var_wh = {}  # (vkey_lower_or_uuid, wh_id) -> qty
        for r in inv_rows:
            vk = (r["variant_key"] or "").strip()
            key = ("name:" + vk.lower()) if vk else ("uuid:" + str(r["pos_variation_id"] or ""))
            inv_by_var_wh[(key, r["warehouse_id"])] = int(r["q"] or 0)
        # Gom các UUID có cùng variant_name thành 1 biến thể (các shop trả UUID khác nhau
        # cho cùng biến thể → không phải là BT khác nhau, chỉ là bản ghi song song theo shop).
        # Với BT chưa đặt tên → mỗi UUID vẫn là 1 dòng riêng.
        from collections import OrderedDict
        groups = OrderedDict()  # name_low_or_uuid → aggregated dict
        for idx, r in enumerate(variants_raw):
            vn = (r["variant_name"] or "").strip()
            gkey = ("name:" + vn.lower()) if vn else ("uuid:" + str(r["pos_variation_id"] or ""))
            if gkey not in groups:
                groups[gkey] = {
                    "primary_pos_variation_id": r["pos_variation_id"],  # UUID đại diện để edit qty
                    "pos_variation_ids": [],
                    "shop_count": 0,
                    "variant_name": vn,
                    "display_name": vn or f"BT {idx+1}",
                    "pos_remain_qty": 0,  # tổng tồn POS của mọi shop
                    "pos_remain_updated_at": "",
                    "qty_vl": r["qty_vl"] or 0,  # đã group theo variant_key sẵn
                    "has_name": bool(vn),
                }
            g = groups[gkey]
            g["pos_variation_ids"].append(r["pos_variation_id"])
            g["shop_count"] += 1
            g["pos_remain_qty"] += int(r["pos_remain_qty"] or 0)
            u = (r["pos_remain_updated_at"] or "")[:16]
            if u and u > (g["pos_remain_updated_at"] or ""):
                g["pos_remain_updated_at"] = u
        variants = []
        for gkey, g in groups.items():
            inv_key = gkey  # cùng format với inv_by_var_wh
            qty_per_wh = [
                {"warehouse_id": w["id"], "warehouse_name": w["name"],
                 "qty": inv_by_var_wh.get((inv_key, w["id"]), 0)}
                for w in warehouses
            ]
            variants.append({
                "pos_variation_id": g["primary_pos_variation_id"],
                "pos_variation_ids": g["pos_variation_ids"],
                "shop_count": g["shop_count"],
                "variant_name": g["variant_name"],
                "display_name": g["display_name"],
                "pos_remain_qty": g["pos_remain_qty"],
                "pos_remain_updated_at": g["pos_remain_updated_at"],
                "qty_vl": g["qty_vl"],
                "qty_per_wh": qty_per_wh,
            })
        is_single_variant = len(variants) <= 1
        qty_vl_total = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS t FROM wh_inventory WHERE product_id=%s", (product_id,)
        ).fetchone()["t"]
        # Tồn POS tổng: nếu SP có biến thể thì SUM từ wh_variation_map.pos_remain_qty
        # (tách theo biến thể); nếu không có variant map thì fallback wh_shop_inventory
        # (tổng theo product-level). wh_shop_inventory không track biến thể nên với
        # SP nhiều biến thể nó chỉ có 1 row qty_pos sai → không dùng được.
        qty_pos_total = conn.execute(
            "SELECT COALESCE(SUM(pos_remain_qty),0) AS t FROM wh_variation_map WHERE product_id=%s",
            (product_id,)
        ).fetchone()["t"]
        if not qty_pos_total:
            qty_pos_total = conn.execute(
                "SELECT COALESCE(SUM(qty_pos),0) AS t FROM wh_shop_inventory WHERE product_id=%s",
                (product_id,)
            ).fetchone()["t"]
        can_edit_qty = session.get("role") in _WH_FULL_ROLES

    return _rt("san_pham_sua.html",
               prod=prod, variants=variants, warehouses=warehouses,
               is_single_variant=is_single_variant,
               qty_vl_total=qty_vl_total, qty_pos_total=qty_pos_total,
               can_edit_qty=can_edit_qty)


@bp.route("/san-pham/<int:product_id>/xem")
def san_pham_xem(product_id: int):
    """Trang xem chi tiết sản phẩm (chỉ đọc)."""
    with db() as conn:
        prod = conn.execute("SELECT * FROM wh_products WHERE id=%s", (product_id,)).fetchone()
        if not prod:
            flash("Không tìm thấy sản phẩm.", "danger")
            return redirect(url_for(".san_pham_list"))
        # Chỉ hiện biến thể ĐANG BÁN (is_locked=false). Biến thể shop đã tắt trên Pancake
        # → ẩn khỏi danh sách + badge. COUNT cũng chỉ tính biến thể active → SP còn 1 biến
        # thể active được coi là SP đơn (qty_vl = tổng tồn vật lý).
        variants_raw = conn.execute("""
            SELECT vm.pos_variation_id, vm.variant_name,
                   COALESCE(vm.pos_remain_qty, 0) AS pos_remain_qty,
                   vm.pos_remain_updated_at,
                   COALESCE(
                     CASE
                       WHEN (SELECT COUNT(*) FROM wh_variation_map vm2 WHERE vm2.product_id = vm.product_id
                              AND COALESCE(vm2.is_locked,false)=false) <= 1
                       THEN (SELECT SUM(wi.qty) FROM wh_inventory wi WHERE wi.product_id = vm.product_id)
                       ELSE (SELECT SUM(wi.qty) FROM wh_inventory wi
                              WHERE wi.product_id=vm.product_id AND wi.variant_key=vm.variant_name
                              AND wi.variant_key IS NOT NULL AND wi.variant_key!='')
                     END, 0) AS qty_vl
            FROM wh_variation_map vm
            WHERE vm.product_id=%s AND COALESCE(vm.is_locked,false)=false
            ORDER BY vm.variant_name NULLS LAST, vm.pos_variation_id
        """, (product_id,)).fetchall()
        # Map pvid → shop_name (lấy shop xuất hiện nhiều nhất với pvid đó qua wh_outbound_requests).
        # Mục đích: SP cùng SKU bán ở nhiều shop → mỗi pvid thuộc 1 shop riêng → hiện rõ cho admin.
        pvid_shop_map = {}
        try:
            shop_rows = conn.execute("""
                SELECT o.pos_variation_id, s.shop_name, COUNT(*) AS c
                FROM wh_outbound_requests o
                JOIN wh_shops s ON s.id = o.shop_id
                WHERE o.product_id=%s AND o.pos_variation_id IS NOT NULL
                  AND o.pos_variation_id <> '' AND o.shop_id IS NOT NULL
                GROUP BY o.pos_variation_id, s.shop_name
                ORDER BY o.pos_variation_id, c DESC
            """, (product_id,)).fetchall()
            for r in shop_rows:
                pvid = r["pos_variation_id"]
                if pvid not in pvid_shop_map:
                    pvid_shop_map[pvid] = r["shop_name"]
        except Exception:
            pass
        # Fallback: tìm qua wh_stock_movements.shop_id (KHÔNG join warehouse_id —
        # 1 kho vật lý phục vụ nhiều shop, JOIN qua warehouse_id sẽ trả tên shop ngẫu nhiên).
        try:
            for r in variants_raw:
                pvid = r["pos_variation_id"]
                if pvid in pvid_shop_map:
                    continue
                row = conn.execute("""
                    SELECT s.shop_name FROM wh_stock_movements m
                    JOIN wh_shops s ON s.id = m.shop_id
                    WHERE m.product_id=%s AND m.pos_variation_id=%s
                      AND m.shop_id IS NOT NULL
                    GROUP BY s.shop_name ORDER BY COUNT(*) DESC LIMIT 1
                """, (product_id, pvid)).fetchone()
                if row and row.get("shop_name"):
                    pvid_shop_map[pvid] = row["shop_name"]
        except Exception:
            pass

        # Fallback 2: tìm qua wh_shop_inventory — dùng khi pvid mới chưa có đơn hàng nào
        try:
            unmatched = [r["pos_variation_id"] for r in variants_raw
                         if r["pos_variation_id"] not in pvid_shop_map]
            if unmatched:
                si_rows = conn.execute("""
                    SELECT s.shop_name FROM wh_shop_inventory si
                    JOIN wh_shops s ON s.id = si.shop_id
                    WHERE si.product_id = %s
                    GROUP BY s.shop_name ORDER BY SUM(si.qty_pos) DESC LIMIT 1
                """, (product_id,)).fetchone()
                if si_rows:
                    for pvid in unmatched:
                        pvid_shop_map[pvid] = si_rows["shop_name"]
        except Exception:
            pass

        # Map shop_name → pos_shop_id để tạo link POS
        shop_pos_id_map = {}
        try:
            for row in conn.execute("SELECT shop_name, pos_shop_id FROM wh_shops WHERE pos_shop_id IS NOT NULL").fetchall():
                shop_pos_id_map[row["shop_name"]] = row["pos_shop_id"]
        except Exception:
            pass

        # Group pvid theo variant_name (case-insensitive) — TRONG SCOPE 1 SKU.
        # Query trên đã filter WHERE product_id (= 1 SKU) → grouping (product_id, vname)
        # ≡ (SKU, vname). Multi-shop cùng SKU+vname → nhiều pvid, gom thành 1 mẫu mã,
        # qty = SUM. KHÔNG gom theo CHỈ vname (khác SKU vẫn riêng).
        from collections import OrderedDict as _OD
        groups = _OD()
        for r in variants_raw:
            vname = (r["variant_name"] or "").strip()
            key = vname.lower() or f"__pvid_{r['pos_variation_id']}"
            if key not in groups:
                groups[key] = {
                    "pos_variation_id": r["pos_variation_id"],  # đại diện
                    "variant_name": vname,
                    "pos_remain_qty": 0,
                    "pos_remain_updated_at": r["pos_remain_updated_at"],
                    "qty_vl": r["qty_vl"],  # đã là tổng cho group (cùng variant_key)
                    "shops": [],
                }
            g = groups[key]
            g["pos_remain_qty"] += (r["pos_remain_qty"] or 0)
            sname = pvid_shop_map.get(r["pos_variation_id"], "")
            if sname and not any(s["shop_name"] == sname for s in g["shops"]):
                g["shops"].append({"shop_name": sname, "pos_shop_id": shop_pos_id_map.get(sname, "")})
        variants = []
        for idx, g in enumerate(groups.values()):
            first_shop = g["shops"][0] if g["shops"] else {"shop_name": "", "pos_shop_id": ""}
            variants.append({
                "pos_variation_id": g["pos_variation_id"],
                "variant_name": g["variant_name"],
                "display_name": g["variant_name"] or f"BT {idx+1}",
                "pos_remain_qty": g["pos_remain_qty"],
                "pos_remain_updated_at": (g["pos_remain_updated_at"] or "")[:16],
                "qty_vl": g["qty_vl"],
                "shop_name": first_shop["shop_name"],
                "pos_shop_id": first_shop["pos_shop_id"],
                "shops": g["shops"],  # all shop sở hữu mẫu mã này
            })
        is_single_variant = len(variants) <= 1
        qty_vl_total = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS t FROM wh_inventory WHERE product_id=%s", (product_id,)
        ).fetchone()["t"]
        # Tồn POS tổng: ưu tiên wh_variation_map.pos_remain_qty (tách theo biến thể).
        # wh_shop_inventory không tách biến thể — với SP nhiều BT sẽ ra số sai.
        qty_pos_total = conn.execute(
            "SELECT COALESCE(SUM(pos_remain_qty),0) AS t FROM wh_variation_map WHERE product_id=%s",
            (product_id,)
        ).fetchone()["t"]
        if not qty_pos_total:
            qty_pos_total = conn.execute(
                "SELECT COALESCE(SUM(qty_pos),0) AS t FROM wh_shop_inventory WHERE product_id=%s",
                (product_id,)
            ).fetchone()["t"]
        # Tồn theo kho vật lý
        wh_stock = conn.execute("""
            SELECT w.name AS wh_name, COALESCE(SUM(i.qty),0) AS qty
            FROM wh_warehouses w
            LEFT JOIN wh_inventory i ON i.warehouse_id=w.id AND i.product_id=%s
            WHERE w.status='active'
            GROUP BY w.id, w.name ORDER BY w.name
        """, (product_id,)).fetchall()
    return _rt("san_pham_xem.html",
               prod=prod, variants=variants,
               is_single_variant=is_single_variant,
               qty_vl_total=qty_vl_total, qty_pos_total=qty_pos_total,
               wh_stock=wh_stock)


# ─────────────────────────────────────────
# TÀI CHÍNH KHO
# ─────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Kiểm tra lệch POS ↔ DB
# ─────────────────────────────────────────────────────────────────────────────
_DRIFT_SCAN_STATE: dict = {"running": False, "result": None, "started_at": None, "error": None}
_DRIFT_SCAN_LOCK = threading.Lock()


def _drift_scan_worker():
    """Background scan: gọi POS API tất cả shop active, so với vmap.pos_remain_qty."""
    import requests as _rq
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    try:
        with db() as conn:
            shops = [dict(r) for r in conn.execute(
                "SELECT id, shop_name, pos_shop_id, pos_api_key FROM wh_shops "
                "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' "
                "AND COALESCE(status,'active') = 'active'"
            ).fetchall()]
            vmap_qty = {}
            vmap_meta = {}
            for r in conn.execute(
                "SELECT vm.pos_variation_id, vm.product_id, vm.variant_name, vm.pos_remain_qty, "
                "       p.sku, p.name "
                "FROM wh_variation_map vm "
                "LEFT JOIN wh_products p ON p.id = vm.product_id"
            ).fetchall():
                pvid = r["pos_variation_id"]
                vmap_qty[pvid] = int(r["pos_remain_qty"] or 0)
                vmap_meta[pvid] = {
                    "sku": r["sku"] or "",
                    "name": r["name"] or "",
                    "variant": r["variant_name"] or "",
                    "product_id": r["product_id"],
                }

        def _scan(shop):
            sid = shop["pos_shop_id"]; key = shop["pos_api_key"]
            items, ok = [], True
            try:
                for page in range(1, 50):
                    r = _rq.get(f"https://pos.pages.fm/api/v1/shops/{sid}/products",
                                params={"api_key": key, "page_size": 100, "page": page},
                                timeout=30)
                    if r.status_code != 200:
                        ok = False; break
                    d = r.json() or {}
                    for prod in (d.get("data") or []):
                        for v in (prod.get("variations") or []):
                            whs = v.get("variations_warehouses") or []
                            qty = sum(int(w.get("actual_remain_quantity") or 0) for w in whs)
                            items.append({"pvid": v.get("id") or "", "qty": qty})
                    if page >= int(d.get("total_pages") or 1):
                        break
            except Exception:
                ok = False
            return shop, items, ok

        api_qty: dict = {}
        shop_for: dict = {}
        failed: list = []
        with _TPE(max_workers=8) as ex:
            futs = {ex.submit(_scan, s): s for s in shops}
            for f in _ac(futs):
                shop, items, ok = f.result()
                if not ok or not items:
                    failed.append(shop["shop_name"])
                for it in items:
                    pv = it["pvid"]
                    if not pv: continue
                    api_qty[pv] = api_qty.get(pv, 0) + it["qty"]
                    shop_for.setdefault(pv, []).append({
                        "shop_id": shop["id"],
                        "shop_name": shop["shop_name"],
                        "pos_shop_id": shop["pos_shop_id"],
                    })

        # Tính drift
        drifts = []
        for pvid, qty_api in api_qty.items():
            qty_db = vmap_qty.get(pvid, 0)
            delta = qty_api - qty_db
            if delta == 0: continue
            meta = vmap_meta.get(pvid, {})
            drifts.append({
                "pvid": pvid, "qty_api": qty_api, "qty_db": qty_db, "delta": delta,
                "sku": meta.get("sku") or "(chưa có trong DB)",
                "name": meta.get("name") or "",
                "variant": meta.get("variant") or "",
                "shops": shop_for.get(pvid, []),
                "type": "missing_in_db" if pvid not in vmap_qty else "qty_mismatch",
            })
        # Orphan: vmap có, POS không còn
        for pvid, qty_db in vmap_qty.items():
            if pvid not in api_qty and qty_db != 0:
                meta = vmap_meta.get(pvid, {})
                drifts.append({
                    "pvid": pvid, "qty_api": 0, "qty_db": qty_db, "delta": -qty_db,
                    "sku": meta.get("sku") or "",
                    "name": meta.get("name") or "",
                    "variant": meta.get("variant") or "",
                    "shops": [{"shop_name": "(không còn trên POS)"}],
                    "type": "orphan",
                })

        drifts.sort(key=lambda d: abs(d["delta"]), reverse=True)

        _DRIFT_SCAN_STATE["result"] = {
            "drifts": drifts,
            "total_shops": len(shops),
            "failed_shops": failed,
            "total_pvid_pos": len(api_qty),
            "total_pvid_db": len(vmap_qty),
            "finished_at": now_hcm().strftime("%d/%m/%Y %H:%M:%S"),
        }
    except Exception as e:
        _DRIFT_SCAN_STATE["error"] = str(e)
    finally:
        _DRIFT_SCAN_STATE["running"] = False


@bp.route("/kiem-tra-lech")
def kiem_tra_lech():
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kho_vat_ly"):
        flash("Bạn không có quyền xem trang này.", "danger")
        return redirect(url_for(".index"))

    if request.args.get("scan") == "1":
        with _DRIFT_SCAN_LOCK:
            if not _DRIFT_SCAN_STATE["running"]:
                _DRIFT_SCAN_STATE["running"] = True
                _DRIFT_SCAN_STATE["result"] = None
                _DRIFT_SCAN_STATE["error"] = None
                _DRIFT_SCAN_STATE["started_at"] = now_hcm().strftime("%d/%m/%Y %H:%M:%S")
                threading.Thread(target=_drift_scan_worker, daemon=True).start()
        return redirect(url_for("kho_vat_ly.kiem_tra_lech"))

    return _rt("kiem_tra_lech.html",
               state=_DRIFT_SCAN_STATE,
               result=_DRIFT_SCAN_STATE.get("result"))


@bp.route("/kiem-tra-lech/progress")
def kiem_tra_lech_progress():
    s = _DRIFT_SCAN_STATE
    return jsonify({
        "running": bool(s.get("running")),
        "started_at": s.get("started_at"),
        "has_result": s.get("result") is not None,
        "error": s.get("error"),
    })


@bp.route("/kiem-tra-lech/resync/<int:shop_id>", methods=["POST"])
def kiem_tra_lech_resync(shop_id):
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kho_vat_ly"):
        return jsonify({"ok": False, "msg": "Không có quyền"}), 403
    try:
        from .wh_sync_pos import sync_shop_inventory_to_db
        with db() as conn:
            sh = conn.execute(
                "SELECT id, shop_name, pos_shop_id, pos_api_key FROM wh_shops WHERE id=%s",
                (shop_id,)
            ).fetchone()
            if not sh:
                return jsonify({"ok": False, "msg": "Shop không tồn tại"}), 404
            res = sync_shop_inventory_to_db(conn, sh["id"], sh["pos_shop_id"], sh["pos_api_key"])
        return jsonify({"ok": True, "msg": f"Đã resync {sh['shop_name']}: {res.get('synced',0)} SP",
                        "synced": res.get("synced", 0)})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Lỗi: {e}"}), 500


@bp.route("/tai-chinh")
def tai_chinh():
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kho_vat_ly"):
        flash("Bạn không có quyền xem trang Tài chính Kho.", "danger")
        return redirect(url_for(".index"))

    # ── Filter params ──
    # Team/Shop → lọc cả tồn VL/POS + đã xuất (theo kho của shop)
    # NV/Ngày → CHỈ lọc "Đã xuất" (tồn không gắn với NV / không phải data lịch sử)
    f_team   = (request.args.get("team_code") or "").strip()
    f_shop   = (request.args.get("shop_id") or "").strip()
    f_staff  = (request.args.get("staff") or "").strip()
    f_from   = (request.args.get("date_from") or "").strip()
    f_to     = (request.args.get("date_to") or "").strip()
    # ── Filter mới: NV phụ trách (chọn 1 user) + danh sách shop của NV (multi checkbox) ──
    # Nếu user_id chỉ định mà không tích shop_ids nào → mặc định tất cả shop của team NV.
    # Nếu user tích cụ thể shop_ids → chỉ tính các shop đó (vẫn phải thuộc team NV).
    f_user_id = (request.args.get("user_id") or "").strip()
    f_shop_ids_raw = request.args.getlist("shop_ids")
    f_shop_ids: list = []
    for _v in f_shop_ids_raw:
        try:
            _vi = int((_v or "").strip())
            if _vi > 0:
                f_shop_ids.append(_vi)
        except Exception:
            pass
    has_filter = bool(f_team or f_shop or f_staff or f_from or f_to or f_user_id or f_shop_ids)

    out_where = ["1=1"]
    out_params: list = []
    if f_team:
        out_where.append("o.shop_id IN (SELECT id FROM wh_shops WHERE team_id = %s)")
        out_params.append(f_team)
    if f_shop:
        try:
            out_where.append("o.shop_id = %s")
            out_params.append(int(f_shop))
        except Exception:
            pass
    if f_staff:
        out_where.append("o.pre_confirmed_by = %s")
        out_params.append(f_staff)
    if f_from:
        out_where.append("o.carrier_picked_up_at >= %s")
        out_params.append(f_from)
    if f_to:
        try:
            from datetime import datetime as _dt, timedelta as _td
            _next = (_dt.strptime(f_to, "%Y-%m-%d") + _td(days=1)).strftime("%Y-%m-%d")
            out_where.append("o.carrier_picked_up_at < %s")
            out_params.append(_next)
        except Exception:
            pass

    out_sql = "WHERE " + " AND ".join(out_where)

    with db() as conn:
        # ── Resolve shop_ids + warehouse_ids để filter tồn theo team/shop ──
        scoped_shop_ids: list = []
        scoped_wh_ids:   list = []
        if f_shop:
            try:
                scoped_shop_ids = [int(f_shop)]
            except Exception:
                pass
        elif f_team:
            _r = conn.execute(
                "SELECT id, warehouse_id FROM wh_shops WHERE team_id = %s",
                (f_team,)
            ).fetchall()
            scoped_shop_ids = [r["id"] for r in _r]
            scoped_wh_ids = sorted({r["warehouse_id"] for r in _r if r["warehouse_id"]})

        if f_shop and scoped_shop_ids:
            _w = conn.execute(
                "SELECT DISTINCT warehouse_id FROM wh_shops WHERE id = %s AND warehouse_id IS NOT NULL",
                (scoped_shop_ids[0],)
            ).fetchall()
            scoped_wh_ids = [r["warehouse_id"] for r in _w]

        # ── OVERRIDE scope khi có filter NV (user_id) hoặc shop_ids[] ──
        # Ưu tiên cao hơn team/shop (vì NV được chọn cụ thể).
        # - Nếu có shop_ids[] → dùng đúng các shop chị tích
        # - Nếu chỉ có user_id (chưa tích shop nào) → tất cả shop thuộc team NV
        _scoped_via_user = False
        if f_shop_ids:
            scoped_shop_ids = list(f_shop_ids)
            _scoped_via_user = True
        elif f_user_id:
            # Lấy shop NV phụ trách CỤ THỂ từ user_shop_assignments (nguồn truth).
            # LƯU Ý: conn ở đây là wh_db (alias `db`) — _adapt() rewrite `shops` → `wh_shops`.
            # → Phải bypass _adapt bằng cách dùng raw psycopg2 cursor (conn._pg).
            try:
                with conn._pg.cursor(cursor_factory=_p_extras.RealDictCursor) as _cur:
                    _cur.execute("""
                        SELECT wh.id, wh.warehouse_id
                        FROM user_shop_assignments usa
                        JOIN shops s ON s.id = usa.shop_id
                        JOIN wh_shops wh ON wh.pos_shop_id = s.pancake_shop_id
                        WHERE usa.user_id = %s AND usa.assigned_to IS NULL
                    """, (int(f_user_id),))
                    _rows = _cur.fetchall()
                if _rows:
                    scoped_shop_ids = [r["id"] for r in _rows]
                    scoped_wh_ids = sorted({r["warehouse_id"] for r in _rows if r["warehouse_id"]})
                    _scoped_via_user = True
            except Exception:
                pass

        # Khi scope qua user_id/shop_ids → cũng cần resolve scoped_wh_ids cho tồn VL
        if _scoped_via_user and scoped_shop_ids and not scoped_wh_ids:
            _w2 = conn.execute(
                "SELECT DISTINCT warehouse_id FROM wh_shops "
                "WHERE id = ANY(%s) AND warehouse_id IS NOT NULL",
                (scoped_shop_ids,)
            ).fetchall()
            scoped_wh_ids = [r["warehouse_id"] for r in _w2]

        # Khi scope qua user_id/shop_ids → outbound cũng cần filter (mặc định f_shop xử lý chỉ 1 shop)
        if _scoped_via_user and scoped_shop_ids and not f_shop:
            out_where.append("o.shop_id = ANY(%s)")
            out_params.append(scoped_shop_ids)
            out_sql = "WHERE " + " AND ".join(out_where)

        # Filter clause cho tồn VL (theo warehouse) + tồn POS (theo shop)
        inv_extra_where = ""
        inv_extra_params: list = []
        pos_extra_where = ""
        pos_extra_params: list = []
        if scoped_wh_ids:
            inv_extra_where = " AND i.warehouse_id = ANY(%s)"
            inv_extra_params = [scoped_wh_ids]
        if scoped_shop_ids:
            pos_extra_where = " AND si.shop_id = ANY(%s)"
            pos_extra_params = [scoped_shop_ids]

        # Khi lọc team/shop: CHỈ tính SP mà team/shop đó BÁN
        # (= có row trong wh_shop_inventory cho scoped_shop_ids)
        # Vì kho Vĩnh Xá chứa hàng cho NHIỀU team — nếu lấy tổng kho thì sai
        prod_extra_where = ""
        prod_extra_params: list = []
        if scoped_shop_ids:
            prod_extra_where = """
                AND EXISTS (
                    SELECT 1 FROM wh_shop_inventory si2
                    WHERE si2.product_id = p.id AND si2.shop_id = ANY(%s)
                )
            """
            prod_extra_params = [scoped_shop_ids]

        products = conn.execute(f"""
            SELECT p.id, p.sku, p.name, p.unit, p.category,
                   COALESCE(p.gia_nhap, 0) AS gia_nhap,
                   COALESCE(p.gia_ban,  0) AS gia_ban,
                   COALESCE(p.min_qty,  0) AS min_qty,
                   COALESCE((
                       SELECT SUM(i.qty) FROM wh_inventory i
                       WHERE i.product_id = p.id AND i.warehouse_id IS NOT NULL
                       {inv_extra_where}
                   ), 0) AS qty_vl,
                   COALESCE((
                       SELECT SUM(si.qty_pos) FROM wh_shop_inventory si
                       WHERE si.product_id = p.id
                       {pos_extra_where}
                   ), 0) AS qty_pos
            FROM wh_products p
            WHERE TRIM(p.sku) != '' AND TRIM(p.name) != ''
              {prod_extra_where}
            ORDER BY p.category, p.name
        """, inv_extra_params + pos_extra_params + prod_extra_params).fetchall()

        # ── "Đã xuất" — group theo product_id (FK, ổn định) thay vì product_sku text.
        # Lý do: nếu NV đổi SKU trên Pancake hoặc DB tách product (case Kim/Thanh Trúc),
        # SKU text có thể lệch giữa đơn cũ và đơn mới, nhưng product_id giữ nguyên.
        # FALLBACK: 5,877 đơn legacy thiếu product_id (1.1% tổng) → vẫn group by product_sku
        # để không mất số. Khi lookup phía dưới, cộng cả 2 nguồn.
        outbound_by_id_rows = conn.execute(f"""
            SELECT product_id,
                   COALESCE(SUM(qty_confirmed), 0) AS total_out
            FROM wh_outbound_requests o
            {out_sql} AND o.product_id IS NOT NULL
            GROUP BY product_id
        """, out_params).fetchall()
        outbound_by_sku_rows = conn.execute(f"""
            SELECT product_sku,
                   COALESCE(SUM(qty_confirmed), 0) AS total_out
            FROM wh_outbound_requests o
            {out_sql} AND o.product_id IS NULL AND product_sku != ''
            GROUP BY product_sku
        """, out_params).fetchall()

        # Teams + NV list cho dropdown filter
        teams_list = conn.execute("""
            SELECT t.team_code, t.team_name, COUNT(s.id) AS shop_count
            FROM teams t
            LEFT JOIN wh_shops s ON s.team_id = t.team_code
            GROUP BY t.team_code, t.team_name
            HAVING COUNT(s.id) > 0
            ORDER BY t.team_name
        """).fetchall()

        # NV phụ trách shop — chỉ user active có shop ĐƯỢC GÁN trong user_shop_assignments
        # (Query KHÔNG JOIN `shops` nên có thể dùng conn.execute() bình thường — _adapt
        # rewrite `shops` → `wh_shops` không ảnh hưởng query này.)
        users_list = conn.execute("""
            SELECT u.id, u.username,
                   COALESCE(NULLIF(TRIM(u.full_name),''), u.username) AS display_name,
                   t.team_code, COALESCE(t.team_name,'') AS team_name,
                   COUNT(usa.shop_id) AS so_shop
            FROM users u
            LEFT JOIN teams t ON t.id = u.team_id
            JOIN user_shop_assignments usa ON usa.user_id = u.id AND usa.assigned_to IS NULL
            WHERE u.status='active'
            GROUP BY u.id, u.username, u.full_name, t.team_code, t.team_name
            HAVING COUNT(usa.shop_id) > 0
            ORDER BY display_name
        """).fetchall()

        # Khi đang chọn 1 user_id → load shop NV phụ trách (server-side render)
        # JOIN `shops` → phải bypass _adapt qua raw psycopg2.
        user_shops_list: list = []
        if f_user_id:
            try:
                with conn._pg.cursor(cursor_factory=_p_extras.RealDictCursor) as _cur:
                    _cur.execute("""
                        SELECT wh.id, wh.shop_name, wh.team_id
                        FROM user_shop_assignments usa
                        JOIN shops s ON s.id = usa.shop_id
                        JOIN wh_shops wh ON wh.pos_shop_id = s.pancake_shop_id
                        WHERE usa.user_id = %s
                        ORDER BY wh.shop_name
                    """, (int(f_user_id),))
                    user_shops_list = [dict(r) for r in _cur.fetchall()]
            except Exception:
                pass
        staff_list = conn.execute("""
            SELECT pre_confirmed_by, COUNT(*) AS c
            FROM wh_outbound_requests
            WHERE pre_confirmed_by IS NOT NULL AND pre_confirmed_by != ''
            GROUP BY pre_confirmed_by
            ORDER BY c DESC
        """).fetchall()
        shops_list = conn.execute("""
            SELECT s.id, s.shop_name, s.team_id, t.team_name
            FROM wh_shops s
            LEFT JOIN teams t ON t.team_code = s.team_id
            ORDER BY t.team_name NULLS LAST, s.shop_name
        """).fetchall()

        warehouses = conn.execute("""
            SELECT id, name, code FROM wh_warehouses
            WHERE is_active = TRUE ORDER BY id
        """).fetchall()

        _inv_wh_clause = ""
        _inv_wh_params: list = []
        if scoped_wh_ids:
            _inv_wh_clause = " AND warehouse_id = ANY(%s)"
            _inv_wh_params = [scoped_wh_ids]
        # Khi có team/shop scope: chỉ lấy inv của các SP mà scope đó bán
        _inv_prod_clause = ""
        _inv_prod_params: list = []
        if scoped_shop_ids:
            _inv_prod_clause = """ AND product_id IN (
                SELECT DISTINCT product_id FROM wh_shop_inventory WHERE shop_id = ANY(%s)
            )"""
            _inv_prod_params = [scoped_shop_ids]
        inv_rows = conn.execute(f"""
            SELECT product_id, warehouse_id, COALESCE(SUM(qty), 0) AS qty
            FROM wh_inventory
            WHERE warehouse_id IS NOT NULL {_inv_wh_clause} {_inv_prod_clause}
            GROUP BY product_id, warehouse_id
        """, _inv_wh_params + _inv_prod_params).fetchall()

        var_count_rows = conn.execute("""
            SELECT product_id, COUNT(*) AS var_count
            FROM wh_variation_map
            GROUP BY product_id
        """).fetchall()

    outbound_by_id  = {r["product_id"]:  int(r["total_out"] or 0) for r in outbound_by_id_rows}
    outbound_by_sku = {r["product_sku"]: int(r["total_out"] or 0) for r in outbound_by_sku_rows}
    var_count_map = {r["product_id"]: int(r["var_count"] or 1) for r in var_count_rows}
    total_variation_count = sum(var_count_map.values())

    inv_by_product = {}
    for ir in inv_rows:
        pid = ir["product_id"]
        wid = ir["warehouse_id"]
        inv_by_product.setdefault(pid, {})[wid] = int(ir["qty"] or 0)

    wh_list = [{"id": w["id"], "name": w["name"], "code": w["code"]} for w in warehouses]

    rows = []
    total_ton_tien = 0
    total_xuat_tien = 0
    total_qty_vl = 0
    total_qty_xuat = 0
    total_qty_pos = 0
    total_pos_tien = 0
    wh_total_qty  = {w["id"]: 0 for w in wh_list}
    wh_total_tien = {w["id"]: 0 for w in wh_list}

    for p in products:
        gia_nhap  = float(p["gia_nhap"] or 0)
        gia_ban   = float(p["gia_ban"]  or 0)
        qty_vl    = int(p["qty_vl"]  or 0)
        qty_pos   = int(p["qty_pos"] or 0)
        # Cộng cả 2: đơn modern (FK product_id) + đơn legacy (chỉ có sku text)
        qty_xuat  = outbound_by_id.get(p["id"], 0) + outbound_by_sku.get(p["sku"], 0)
        ton_tien  = qty_vl  * gia_nhap
        pos_tien  = qty_pos * gia_nhap
        xuat_tien = qty_xuat * gia_nhap

        qty_per_wh = {}
        for w in wh_list:
            q = inv_by_product.get(p["id"], {}).get(w["id"], 0)
            qty_per_wh[w["id"]] = q
            wh_total_qty[w["id"]]  += q
            wh_total_tien[w["id"]] += q * gia_nhap

        total_ton_tien  += ton_tien
        total_xuat_tien += xuat_tien
        total_qty_vl    += qty_vl
        total_qty_xuat  += qty_xuat
        total_qty_pos   += qty_pos
        total_pos_tien  += pos_tien

        rows.append({
            "id":          p["id"],
            "sku":         p["sku"],
            "name":        p["name"],
            "unit":        p["unit"] or "cái",
            "category":    p["category"] or "",
            "gia_nhap":    gia_nhap,
            "gia_ban":     gia_ban,
            "min_qty":     int(p["min_qty"] or 0),
            "qty_vl":      qty_vl,
            "qty_pos":     qty_pos,
            "qty_xuat":    qty_xuat,
            "ton_tien":    ton_tien,
            "pos_tien":    pos_tien,
            "xuat_tien":   xuat_tien,
            "qty_per_wh":  qty_per_wh,
            "var_count":   var_count_map.get(p["id"], 1),
        })

    wh_totals = [
        {"id": w["id"], "name": w["name"], "code": w["code"],
         "qty": wh_total_qty[w["id"]], "tien": wh_total_tien[w["id"]]}
        for w in wh_list
    ]

    def vnd(n):
        if n == 0:
            return "—"
        try:
            return "{:,.0f}đ".format(n).replace(",", ".")
        except Exception:
            return str(n)

    return _rt("tai_chinh.html",
               rows=rows,
               wh_list=wh_list,
               wh_totals=wh_totals,
               wh_total_qty=wh_total_qty,
               wh_total_tien=wh_total_tien,
               total_ton_tien=total_ton_tien,
               total_xuat_tien=total_xuat_tien,
               total_qty_vl=total_qty_vl,
               total_qty_xuat=total_qty_xuat,
               total_qty_pos=total_qty_pos,
               total_pos_tien=total_pos_tien,
               total_variation_count=total_variation_count,
               vnd=vnd,
               # Filter state
               teams_list=teams_list, shops_list=shops_list, staff_list=staff_list,
               users_list=users_list, user_shops_list=user_shops_list,
               f_team=f_team, f_shop=f_shop, f_staff=f_staff,
               f_user_id=f_user_id, f_shop_ids=f_shop_ids,
               f_from=f_from, f_to=f_to,
               has_filter=has_filter)


@bp.route("/api/shops-of-user/<int:user_id>", methods=["GET"])
def api_shops_of_user(user_id: int):
    """Trả JSON danh sách shop NV phụ trách CỤ THỂ — dùng cho filter Tài chính Kho.

    Nguồn truth: bảng user_shop_assignments(user_id, shop_id → shops.id).
    Liên kết wh_shops qua pos_shop_id = shops.pancake_shop_id (100% match).
    """
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kho_vat_ly"):
        return jsonify({"error": "Không có quyền"}), 403
    try:
        with db() as conn:
            # JOIN `shops` → bypass _adapt() qua raw psycopg2 cursor
            with conn._pg.cursor(cursor_factory=_p_extras.RealDictCursor) as _cur:
                _cur.execute("""
                    SELECT wh.id, wh.shop_name, wh.pos_shop_id,
                           COALESCE(wh.status,'active') AS status
                    FROM user_shop_assignments usa
                    JOIN shops s ON s.id = usa.shop_id
                    JOIN wh_shops wh ON wh.pos_shop_id = s.pancake_shop_id
                    WHERE usa.user_id = %s
                    ORDER BY wh.shop_name
                """, (user_id,))
                rows = _cur.fetchall()
        return jsonify({
            "user_id": user_id,
            "shops": [{"id": r["id"], "shop_name": r["shop_name"],
                       "pos_shop_id": r.get("pos_shop_id") or "",
                       "status": r.get("status") or "active"} for r in rows],
        })
    except Exception as ex:
        return jsonify({"error": str(ex), "shops": []}), 500


@bp.route("/tai-chinh/sync-gia", methods=["POST"])
def tai_chinh_sync_gia():
    """Kéo giá nhập / giá bán từ POS về cho tất cả shop có api_key."""
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kvl_san_pham_sua"):
        flash("Không có quyền sửa giá.", "danger")
        return redirect(url_for(".tai_chinh"))

    from .wh_sync_pos import load_stock_from_api, parse_shop_stock
    updated = 0
    shops_done = 0
    with db() as conn:
        shops = conn.execute(
            "SELECT id, pos_shop_id, pos_api_key FROM wh_shops WHERE status='active' AND pos_api_key != ''"
        ).fetchall()
        seen_skus = set()
        for shop in shops:
            api_key = str(shop["pos_api_key"] or "").strip()
            if not api_key:
                continue
            raw = load_stock_from_api(str(shop["pos_shop_id"]), api_key)
            if isinstance(raw, dict) and "error" in raw:
                continue
            items = parse_shop_stock(raw, str(shop["pos_shop_id"]))
            shops_done += 1
            for item in items:
                sku = item["sku"]
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)
                gia_nhap = item.get("gia_nhap", 0) or 0
                gia_ban  = item.get("gia_ban",  0) or 0
                if gia_nhap > 0 or gia_ban > 0:
                    conn.execute(
                        "UPDATE wh_products SET gia_nhap=%s, gia_ban=%s WHERE sku=%s",
                        (gia_nhap, gia_ban, sku)
                    )
                    updated += 1

    flash(f"Đã đồng bộ giá: {updated} SKU từ {shops_done} shop.", "success")
    return redirect(url_for(".tai_chinh"))


@bp.route("/tai-chinh/update-gia", methods=["POST"])
def tai_chinh_update_gia():
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kvl_san_pham_sua"):
        return {"ok": False, "msg": "Không có quyền sửa giá"}, 403

    product_id = request.form.get("product_id", "").strip()
    gia_nhap   = request.form.get("gia_nhap", "0").strip().replace(",", "").replace(".", "")
    gia_ban    = request.form.get("gia_ban",  "0").strip().replace(",", "").replace(".", "")
    min_qty    = request.form.get("min_qty",  "0").strip()

    try:
        product_id = int(product_id)
        gia_nhap   = float(gia_nhap) if gia_nhap else 0
        gia_ban    = float(gia_ban)  if gia_ban  else 0
        min_qty    = int(min_qty)    if min_qty   else 0
    except ValueError:
        flash("Dữ liệu không hợp lệ.", "danger")
        return redirect(url_for(".tai_chinh"))

    with db() as conn:
        conn.execute(
            "UPDATE wh_products SET gia_nhap=%s, gia_ban=%s, min_qty=%s WHERE id=%s",
            (gia_nhap, gia_ban, min_qty, product_id)
        )
    flash("Đã lưu giá và ngưỡng cảnh báo thành công.", "success")
    return redirect(url_for(".tai_chinh"))


# ─────────────────────────────────────────
# QUẢN LÝ KHO VẬT LÝ (MULTI-WAREHOUSE)
# ─────────────────────────────────────────

@bp.route("/kho-quan-ly")
def kho_quan_ly():
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kho_vat_ly"):
        flash("Không có quyền.", "danger")
        return redirect(url_for(".index"))

    with db() as conn:
        warehouses = conn.execute("""
            SELECT w.id, w.code, w.name, w.address, w.status,
                   COALESCE(SUM(i.qty), 0) AS total_qty,
                   COUNT(DISTINCT i.product_id) AS sku_count
            FROM wh_warehouses w
            LEFT JOIN wh_inventory i ON i.warehouse_id = w.id
            GROUP BY w.id, w.code, w.name, w.address, w.status
            ORDER BY w.id
        """).fetchall()

    return _rt("kho_quan_ly.html", warehouses=warehouses)


@bp.route("/kho-quan-ly/them", methods=["GET", "POST"])
def kho_them():
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        flash("Không có quyền.", "danger")
        return redirect(url_for(".kho_quan_ly"))

    if request.method == "POST":
        code  = request.form.get("code", "").strip().upper().replace(" ", "_")
        name  = request.form.get("name", "").strip()
        addr  = request.form.get("address", "").strip()
        if not code or not name:
            flash("Vui lòng điền đầy đủ mã kho và tên kho.", "danger")
            return _rt("kho_them.html")
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO warehouses (code, name, address, status, created_at) VALUES (?,?,?,'active',?)",
                    (code, name, addr, now_hcm().strftime("%Y-%m-%d %H:%M:%S"))
                )
            flash(f"Đã thêm kho '{name}' thành công.", "success")
            return redirect(url_for(".kho_quan_ly"))
        except Exception as e:
            flash(f"Lỗi: {e}", "danger")
    return _rt("kho_them.html")


@bp.route("/kho-quan-ly/<int:wh_id>/sua", methods=["GET", "POST"])
def kho_sua(wh_id):
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        flash("Không có quyền.", "danger")
        return redirect(url_for(".kho_quan_ly"))

    with db() as conn:
        wh = conn.execute("SELECT * FROM warehouses WHERE id=?", (wh_id,)).fetchone()
    if not wh:
        flash("Không tìm thấy kho.", "danger")
        return redirect(url_for(".kho_quan_ly"))

    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        addr   = request.form.get("address", "").strip()
        status = request.form.get("status", "active").strip()
        with db() as conn:
            conn.execute(
                "UPDATE warehouses SET name=?, address=?, status=? WHERE id=?",
                (name, addr, status, wh_id)
            )
        flash("Đã cập nhật kho.", "success")
        return redirect(url_for(".kho_quan_ly"))

    return _rt("kho_sua.html", wh=wh)


# ─────────────────────────────────────────
# PHÂN BỔ KHO VẬT LÝ → SHOP POS
# ─────────────────────────────────────────

@bp.route("/phan-bo")
def phan_bo():
    role = session.get("role", "staff")
    if role not in ("admin", "manager", "kho"):
        flash("Không có quyền.", "danger")
        return redirect(url_for(".index"))

    wh_id = request.args.get("warehouse_id", "", type=int) or None

    with db() as conn:
        warehouses = conn.execute(
            "SELECT id, code, name FROM warehouses WHERE status='active' ORDER BY id"
        ).fetchall()
        shops = conn.execute(
            "SELECT id, shop_key, shop_name FROM shops WHERE status='active' ORDER BY shop_name"
        ).fetchall()

        if not wh_id and warehouses:
            wh_id = warehouses[0]["id"]

        products = conn.execute("""
            SELECT p.id, p.sku, p.name, p.unit,
                   COALESCE(i.qty, 0) AS qty_vl
            FROM wh_products p
            LEFT JOIN wh_inventory i ON i.product_id = p.id AND i.warehouse_id = %s
            WHERE TRIM(p.sku) != '' AND TRIM(p.name) != '' AND COALESCE(i.qty,0) > 0
            ORDER BY p.category, p.name
        """, (wh_id,)).fetchall() if wh_id else []

        history = conn.execute("""
            SELECT pb.id, pb.qty, pb.note, pb.status, pb.created_at, pb.synced_at,
                   p.sku, p.name AS product_name, p.unit,
                   s.shop_key, s.shop_name,
                   w.name AS warehouse_name
            FROM wh_phanbo pb
            JOIN wh_products p ON p.id = pb.product_id
            JOIN wh_shops s ON s.id = pb.shop_id
            JOIN wh_warehouses w ON w.id = pb.warehouse_id
            ORDER BY pb.created_at DESC
            LIMIT 50
        """).fetchall()

    return _rt("phan_bo.html",
               warehouses=warehouses, shops=shops,
               products=products, history=history,
               selected_wh_id=wh_id)


@bp.route("/phan-bo/submit", methods=["POST"])
def phan_bo_submit():
    role = session.get("role", "staff")
    if role not in ("admin", "manager", "kho"):
        flash("Không có quyền.", "danger")
        return redirect(url_for(".phan_bo"))

    warehouse_id = request.form.get("warehouse_id", type=int)
    shop_id      = request.form.get("shop_id", type=int)
    product_id   = request.form.get("product_id", type=int)
    qty          = request.form.get("qty", type=int)
    note         = request.form.get("note", "").strip()
    push_pos     = request.form.get("push_pos") == "1"

    if not all([warehouse_id, shop_id, product_id, qty]) or qty <= 0:
        flash("Vui lòng điền đầy đủ thông tin và số lượng > 0.", "danger")
        return redirect(url_for(".phan_bo"))

    now_str = now_hcm().strftime("%Y-%m-%d %H:%M:%S")
    user = session.get("username", "system")

    with db() as conn:
        # Kiểm tra tồn kho vật lý đủ không
        inv = conn.execute(
            "SELECT qty FROM inventory WHERE product_id=? AND warehouse_id=?",
            (product_id, warehouse_id)
        ).fetchone()
        qty_vl = int((inv or {}).get("qty") or 0)
        if qty > qty_vl:
            flash(f"Không đủ tồn kho vật lý. Hiện có {qty_vl} cái.", "danger")
            return redirect(url_for(".phan_bo", warehouse_id=warehouse_id))

        prod = conn.execute("SELECT sku, name, unit FROM products WHERE id=?", (product_id,)).fetchone()
        shop = conn.execute("SELECT pos_shop_id, pos_api_key, shop_name FROM shops WHERE id=?", (shop_id,)).fetchone()

        # Ghi phân bổ
        conn.execute("""
            INSERT INTO phanbo (warehouse_id, shop_id, product_id, product_sku, qty, note, status, created_by, created_at)
            VALUES (?,?,?,?,?,?,'pending',?,?)
        """, (warehouse_id, shop_id, product_id, prod["sku"] if prod else "", qty, note, user, now_str))

        # Trừ tồn kho vật lý
        conn.execute(
            "UPDATE inventory SET qty = qty - ?, updated_at=? WHERE product_id=? AND warehouse_id=?",
            (qty, now_str, product_id, warehouse_id)
        )
        conn.execute("""
            INSERT INTO stock_movements (type, product_id, warehouse_id, shop_id, qty, qty_before, qty_after, note, created_by, created_at)
            VALUES ('phanbo', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (product_id, warehouse_id, shop_id, qty, qty_vl, qty_vl - qty, f"Phân bổ → {shop['shop_name'] if shop else ''}: {note}", user, now_str))

    # Đẩy lên POS nếu chọn
    pos_result = ""
    if push_pos and prod and shop:
        pos_result = _push_phanbo_to_pos(shop, prod, qty)

    if pos_result:
        flash(f"Đã phân bổ {qty} {prod['unit'] if prod else 'cái'}. {pos_result}", "success" if "thành công" in pos_result else "warning")
    else:
        flash(f"Đã phân bổ {qty} {prod['unit'] if prod else 'cái'} từ kho vật lý. {'(Chưa đẩy lên POS)' if not push_pos else ''}", "success")

    return redirect(url_for(".phan_bo", warehouse_id=warehouse_id))


def _push_phanbo_to_pos(shop: dict, prod: dict, qty: int) -> str:
    """Đẩy số lượng phân bổ lên POS bằng cách cộng thêm qty vào tồn shop."""
    import requests as _req
    api_key    = str(shop.get("pos_api_key") or "").strip()
    pos_shop_id = str(shop.get("pos_shop_id") or "").strip()
    variation_id = None

    # Lấy pos_variation_id từ DB
    try:
        with db() as conn:
            p = conn.execute("SELECT pos_variation_id, pos_product_id FROM products WHERE sku=?",
                             (prod["sku"],)).fetchone()
            if p:
                variation_id = p.get("pos_variation_id")
                product_id_pos = p.get("pos_product_id")
    except Exception:
        return "Lỗi đọc DB."

    if not api_key or not pos_shop_id or not variation_id:
        return "Chưa có API key hoặc không tìm thấy mã variation POS."

    try:
        # Lấy qty hiện tại trên POS để cộng thêm
        url_get = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products"
        r = _req.get(url_get, params={"api_key": api_key, "page": 1, "limit": 200}, timeout=20)
        r.raise_for_status()
        data = r.json()
        current_qty = 0
        wh_id_pos = None
        for prd in (data.get("data") or []):
            for var in (prd.get("variations") or []):
                if str(var.get("id")) == str(variation_id):
                    vw = (var.get("variations_warehouses") or [{}])[0]
                    current_qty = int(vw.get("actual_remain_quantity") or 0)
                    wh_id_pos = vw.get("warehouse_id")
                    break

        if not wh_id_pos:
            return "Không tìm thấy warehouse_id trên POS."

        new_qty = current_qty + qty
        url_put = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products/{product_id_pos}"
        payload = {"product": {"variations": [{"id": variation_id,
                    "variations_warehouses": [{"warehouse_id": wh_id_pos, "remain_quantity": new_qty}]}]}}
        rp = _req.put(url_put, params={"api_key": api_key}, json=payload, timeout=20)
        rp.raise_for_status()
        return f"Đẩy lên POS thành công (+{qty}, tổng {new_qty})."
    except Exception as ex:
        return f"Lỗi đẩy POS: {ex}"


# ─────────────────────────────────────────
# CẬP NHẬT GIÁ 2 CHIỀU → POS
# ─────────────────────────────────────────

@bp.route("/tai-chinh/push-gia/<int:product_id>", methods=["POST"])
def tai_chinh_push_gia(product_id):
    role = session.get("role", "staff")
    import perm_utils
    if not perm_utils.has_permission(role, "kvl_san_pham_sua"):
        return {"ok": False, "msg": "Không có quyền"}, 403

    with db() as conn:
        prod = conn.execute(
            "SELECT sku, gia_ban, pos_variation_id, pos_product_id FROM wh_products WHERE id=%s",
            (product_id,)
        ).fetchone()
        if not prod:
            return {"ok": False, "msg": "Không tìm thấy sản phẩm"}, 404

        shops = conn.execute(
            "SELECT pos_shop_id, pos_api_key FROM wh_shops WHERE status='active' AND pos_api_key != ''"
        ).fetchall()

    import requests as _req
    gia_ban = int(prod.get("gia_ban") or 0)
    variation_id = str(prod.get("pos_variation_id") or "").strip()
    product_id_pos = str(prod.get("pos_product_id") or "").strip()

    if not variation_id or not product_id_pos or gia_ban <= 0:
        return {"ok": False, "msg": "Thiếu thông tin POS hoặc giá bán = 0"}, 400

    ok_count = 0
    errs = []
    seen_keys = set()
    for shop in shops:
        api_key = str(shop.get("pos_api_key") or "").strip()
        pos_shop_id = str(shop.get("pos_shop_id") or "").strip()
        if not api_key or not pos_shop_id or pos_shop_id in seen_keys:
            continue
        seen_keys.add(pos_shop_id)
        try:
            url = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products/{product_id_pos}"
            payload = {"product": {"variations": [{"id": variation_id, "retail_price": gia_ban}]}}
            r = _req.put(url, params={"api_key": api_key}, json=payload, timeout=15)
            r.raise_for_status()
            ok_count += 1
        except Exception as ex:
            errs.append(str(ex))

    if ok_count > 0:
        return {"ok": True, "msg": f"Đã cập nhật giá cho {ok_count} shop POS."}
    return {"ok": False, "msg": f"Lỗi: {'; '.join(errs[:2])}"}


# ─────────────────────────────────────────
# CÀI ĐẶT MẪU IN
# ─────────────────────────────────────────

PRINT_TYPES = {
    "xuat_kho_noi_bo":  "Xuất kho nội bộ",
    "ban_giao_shipper": "Bàn giao Shipper",
    "hang_loat":        "In hàng loạt",
}
PAPER_SIZES = ["A4", "A5", "70x100mm", "80x120mm", "100x150mm", "Tùy chỉnh"]
FONT_SIZES  = [("small","Nhỏ"), ("medium","Vừa"), ("large","Lớn")]


def _get_print_template(conn, ttype):
    """Load mẫu in mặc định cho loại phiếu; trả dict defaults nếu chưa có."""
    try:
        _ensure_print_template_table(conn)
        row = conn.execute(
            "SELECT * FROM wh_print_templates WHERE type=%s AND is_default=TRUE LIMIT 1",
            (ttype,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM wh_print_templates WHERE type=%s LIMIT 1",
                (ttype,)
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return {
        "paper_size": "A5", "copies": 1, "show_logo": True,
        "show_barcode": False, "show_qr": False, "font_size": "medium",
        "custom_header": "", "custom_footer": "", "name": "Mặc định",
    }


def _ensure_print_template_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wh_print_templates (
            id           SERIAL PRIMARY KEY,
            type         VARCHAR(50)  NOT NULL,
            name         VARCHAR(100) NOT NULL,
            paper_size   VARCHAR(20)  DEFAULT 'A5',
            copies       INTEGER      DEFAULT 1,
            show_logo    BOOLEAN      DEFAULT TRUE,
            show_barcode BOOLEAN      DEFAULT FALSE,
            show_qr      BOOLEAN      DEFAULT FALSE,
            font_size    VARCHAR(10)  DEFAULT 'medium',
            custom_header TEXT,
            custom_footer TEXT,
            is_default   BOOLEAN      DEFAULT FALSE,
            auto_print   BOOLEAN      DEFAULT FALSE,
            created_at   TEXT         DEFAULT ''
        )
    """)
    # ── Migration: bổ sung các cột thiếu với DB schema cũ ──
    _migrations = [
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS type VARCHAR(50) DEFAULT ''",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS paper_size VARCHAR(20) DEFAULT 'A5'",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS copies INTEGER DEFAULT 1",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS show_logo BOOLEAN DEFAULT TRUE",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS show_barcode BOOLEAN DEFAULT FALSE",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS show_qr BOOLEAN DEFAULT FALSE",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS font_size VARCHAR(10) DEFAULT 'medium'",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS custom_header TEXT",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS custom_footer TEXT",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE",
        "ALTER TABLE wh_print_templates ADD COLUMN IF NOT EXISTS auto_print BOOLEAN DEFAULT FALSE",
        "UPDATE wh_print_templates SET type = template_type WHERE (type IS NULL OR type = '') AND template_type IS NOT NULL AND template_type <> ''",
    ]
    for sql in _migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass


@bp.route("/cai-dat-mau-in")
def print_template_list():
    role = session.get("role", "staff")
    can_write = role in ("admin", "superadmin", "manager")
    with db() as conn:
        _ensure_print_template_table(conn)
        rows = conn.execute(
            "SELECT * FROM wh_print_templates ORDER BY type, id"
        ).fetchall()
    templates = {}
    for r in rows:
        t = dict(r)
        templates.setdefault(t["type"], []).append(t)
    # tabs theo thứ tự cố định
    tabs = [(k, v, templates.get(k, [])) for k, v in PRINT_TYPES.items()]
    active_tab = request.args.get("tab", "xuat_kho_noi_bo")
    return render_template(
        "kho_vat_ly/mau_in.html",
        tabs=tabs, active_tab=active_tab,
        paper_sizes=PAPER_SIZES, font_sizes=FONT_SIZES,
        can_write=can_write,
        print_types=PRINT_TYPES,
    )


@bp.route("/cai-dat-mau-in/tao", methods=["POST"])
def print_template_create():
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        flash("Không có quyền tạo mẫu in", "danger")
        return redirect(url_for("kho_vat_ly.print_template_list"))
    ttype     = request.form.get("type", "xuat_kho_noi_bo")
    name      = request.form.get("name", "").strip()
    paper     = request.form.get("paper_size", "A5")
    copies    = int(request.form.get("copies", 1) or 1)
    show_logo = request.form.get("show_logo") == "1"
    show_bc   = request.form.get("show_barcode") == "1"
    show_qr   = request.form.get("show_qr") == "1"
    font_size = request.form.get("font_size", "medium")
    c_header  = request.form.get("custom_header", "").strip()
    c_footer  = request.form.get("custom_footer", "").strip()
    if not name:
        flash("Tên mẫu in không được để trống", "danger")
        return redirect(url_for("kho_vat_ly.print_template_list", tab=ttype))
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M")
    with db() as conn:
        _ensure_print_template_table(conn)
        conn.execute("""
            INSERT INTO wh_print_templates
            (type, name, paper_size, copies, show_logo, show_barcode, show_qr,
             font_size, custom_header, custom_footer, is_default, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s)
        """, (ttype, name, paper, copies, show_logo, show_bc, show_qr,
              font_size, c_header, c_footer, now))
    flash(f"Đã tạo mẫu in «{name}»", "success")
    return redirect(url_for("kho_vat_ly.print_template_list", tab=ttype))


@bp.route("/cai-dat-mau-in/<int:tid>/sua", methods=["GET", "POST"])
def print_template_edit(tid):
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        flash("Không có quyền chỉnh sửa mẫu in", "danger")
        return redirect(url_for("kho_vat_ly.print_template_list"))
    with db() as conn:
        _ensure_print_template_table(conn)
        tmpl = conn.execute(
            "SELECT * FROM wh_print_templates WHERE id=%s", (tid,)
        ).fetchone()
        if not tmpl:
            flash("Không tìm thấy mẫu in", "danger")
            return redirect(url_for("kho_vat_ly.print_template_list"))
        if request.method == "POST":
            name      = request.form.get("name", "").strip()
            paper     = request.form.get("paper_size", "A5")
            copies    = int(request.form.get("copies", 1) or 1)
            show_logo = request.form.get("show_logo") == "1"
            show_bc   = request.form.get("show_barcode") == "1"
            show_qr   = request.form.get("show_qr") == "1"
            font_size = request.form.get("font_size", "medium")
            c_header  = request.form.get("custom_header", "").strip()
            c_footer  = request.form.get("custom_footer", "").strip()
            if not name:
                flash("Tên mẫu in không được để trống", "danger")
            else:
                conn.execute("""
                    UPDATE wh_print_templates SET
                        name=%s, paper_size=%s, copies=%s, show_logo=%s,
                        show_barcode=%s, show_qr=%s, font_size=%s,
                        custom_header=%s, custom_footer=%s
                    WHERE id=%s
                """, (name, paper, copies, show_logo, show_bc, show_qr,
                      font_size, c_header, c_footer, tid))
                flash(f"Đã lưu mẫu in «{name}»", "success")
                return redirect(url_for("kho_vat_ly.print_template_list",
                                        tab=dict(tmpl)["type"]))
        tmpl = dict(conn.execute(
            "SELECT * FROM wh_print_templates WHERE id=%s", (tid,)
        ).fetchone())
    return render_template(
        "kho_vat_ly/mau_in_edit.html",
        tmpl=tmpl, paper_sizes=PAPER_SIZES, font_sizes=FONT_SIZES,
        print_types=PRINT_TYPES,
    )


@bp.route("/cai-dat-mau-in/<int:tid>/xoa", methods=["POST"])
def print_template_delete(tid):
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        return {"ok": False, "msg": "Không có quyền"}, 403
    with db() as conn:
        _ensure_print_template_table(conn)
        tmpl = conn.execute(
            "SELECT type, name FROM wh_print_templates WHERE id=%s", (tid,)
        ).fetchone()
        if not tmpl:
            return {"ok": False, "msg": "Không tìm thấy"}, 404
        ttype = tmpl["type"]
        conn.execute("DELETE FROM wh_print_templates WHERE id=%s", (tid,))
    flash(f"Đã xóa mẫu in «{tmpl['name']}»", "success")
    return redirect(url_for("kho_vat_ly.print_template_list", tab=ttype))


@bp.route("/cai-dat-mau-in/<int:tid>/dat-mac-dinh", methods=["POST"])
def print_template_set_default(tid):
    role = session.get("role", "staff")
    if role not in ("admin", "manager"):
        return {"ok": False, "msg": "Không có quyền"}, 403
    with db() as conn:
        _ensure_print_template_table(conn)
        tmpl = conn.execute(
            "SELECT type FROM wh_print_templates WHERE id=%s", (tid,)
        ).fetchone()
        if not tmpl:
            return {"ok": False, "msg": "Không tìm thấy"}, 404
        conn.execute(
            "UPDATE wh_print_templates SET is_default=FALSE WHERE type=%s",
            (tmpl["type"],)
        )
        conn.execute(
            "UPDATE wh_print_templates SET is_default=TRUE WHERE id=%s", (tid,)
        )
    return {"ok": True}


# ─────────────────────────────────────────
# ĐĂNG KÝ MODULE
# ─────────────────────────────────────────

def register_kho_vat_ly_module(app):
    """Đăng ký Blueprint kho_vat_ly vào Flask app, khởi tạo bảng DB, chạy auto-sync."""
    init_wh_tables()
    # Tự động đồng bộ hex API keys từ shops.json → wh_shops DB khi khởi động
    try:
        from pancake_auth import sync_keys_from_json_to_db
        n = sync_keys_from_json_to_db()
        if n:
            import logging as _logging
            _logging.getLogger(__name__).info("pancake_auth: synced %d shop API keys từ shops.json → DB", n)
    except Exception:
        pass
    # Đăng ký tường minh (trước register_blueprint) — vài môi trường @bp.route bị lệch → POST 404
    bp.add_url_rule(
        "/outbound/sync-quick", "outbound_sync_quick", outbound_sync_quick, methods=["GET", "POST"]
    )
    bp.add_url_rule(
        "/outbound/sync_quick", "outbound_sync_quick_us", outbound_sync_quick, methods=["GET", "POST"]
    )
    app.register_blueprint(bp)

    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_auto_sync()
