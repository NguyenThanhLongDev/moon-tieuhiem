from __future__ import annotations

import os
import sys
import threading

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass
import time

# Đảm bảo timezone là UTC+7 (Việt Nam) nếu chưa được set
if not os.environ.get("TZ"):
    os.environ["TZ"] = "Asia/Ho_Chi_Minh"
try:
    time.tzset()
except AttributeError:
    pass

from flask import Flask, request, session, g, redirect, abort, url_for, jsonify

from order_pickup_utils import order_counts_for_carrier_pickup_day
from modules.ads_mapping import register_ads_mapping_module
from scheduler import start_scheduler
from run_migrations import auto_migrate, auto_bootstrap_if_empty
from modules.salary import register_salary_module
from modules.kho_vat_ly import register_kho_vat_ly_module
import perm_utils
from modules.fb_pages import register_fb_pages_module
from modules.chi_phi_qc import register_chi_phi_qc_module
from modules.cham_cong import register_cham_cong_module

from app_constants import DASHBOARD_WEB_VERSION, KHO_VAT_LY_ACCESS_ROLES, PAGE_TEMPLATE, BASE_DIR
from app_ctx import (
    login_required,
    _find_home_url,
    _enforce_admin_only_sections,
    # thread state
    SYNC_IN_PROGRESS_DATES, SYNC_IN_PROGRESS_LOCK,
    AUTO_SYNC_MISSING_DATE_FAILED, _AUTO_SYNC_BG_LAST_RUN, _AUTO_SYNC_BG_COOLDOWN,
    HOME_ALERT_COUNTS_CACHE, HOME_ALERT_COUNTS_CACHE_LOCK,
    HOME_CARRIER_PICKUP_CACHE, HOME_CARRIER_PICKUP_CACHE_LOCK,
    # dashboard helpers
    fetch_live_pos_status, save_live_pos_status,
    get_allowed_shop_keys_for_current_user,
    can_view_ads_global,
)

# ---------------------------------------------------------------------------
# Flask app init
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("POS_DASHBOARD_SECRET")
    or "doi-secret-key-ngay-2025"
)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7


# ---------------------------------------------------------------------------
# Context processors
# ---------------------------------------------------------------------------
@app.context_processor
def _inject_dashboard_footer():
    return {"dashboard_web_version": DASHBOARD_WEB_VERSION}


@app.context_processor
def _perm_context():
    def has_module(key: str) -> bool:
        allowed = getattr(g, "allowed_modules", None)
        return allowed is None or key in allowed
    return dict(has_module=has_module)


# ---------------------------------------------------------------------------
# Before request hooks
# ---------------------------------------------------------------------------
@app.before_request
def _check_module_permission():
    path = request.path
    if not session.get("logged_in"):
        return
    role = str(session.get("role", "staff")).strip()
    perms = perm_utils.load_perms()
    allowed = perm_utils.get_allowed_modules(role, perms)
    g.allowed_modules = allowed  # None = unrestricted
    if allowed is None:
        return
    module_key = perm_utils.path_to_module(path, perms.get("path_map", {}))
    if module_key and module_key not in allowed:
        home_url = _find_home_url(allowed)
        if request.method == "GET" and path != home_url:
            return redirect(home_url)
        abort(403)


@app.before_request
def _enforce_admin_sections():
    return _enforce_admin_only_sections()


# ---------------------------------------------------------------------------
# Register Blueprints
# ---------------------------------------------------------------------------
from blueprints.auth_bp import auth_bp
from blueprints.dashboard_bp import dashboard_bp
from blueprints.ads_bp import ads_bp
from blueprints.settings_bp import settings_bp
from blueprints.shop_bp import shop_bp
from blueprints.misc_bp import misc_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(ads_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(shop_bp)
app.register_blueprint(misc_bp)


# ---------------------------------------------------------------------------
# Register external modules (keep login_required from app_ctx)
# ---------------------------------------------------------------------------
register_salary_module(app, login_required=login_required, page_template=PAGE_TEMPLATE)
register_ads_mapping_module(
    app,
    login_required=login_required,
    page_template=PAGE_TEMPLATE,
    get_allowed_shop_keys=get_allowed_shop_keys_for_current_user,
    can_view_ads_global=can_view_ads_global,
)

auto_migrate()
auto_bootstrap_if_empty()

register_kho_vat_ly_module(app)


@app.route("/api/kho/outbound-sync-quick", methods=["GET", "POST"])
def api_kho_outbound_sync_quick():
    ct = request.content_type or ""
    is_ajax = (
        request.is_json
        or ct.startswith("multipart/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )
    if not session.get("logged_in"):
        if is_ajax:
            return jsonify({"ok": False, "error": "Phiên đăng nhập hết hạn, vui lòng tải lại trang"}), 401
        return redirect(url_for("auth.login", next=request.url))
    role = str(session.get("role", "staff")).strip().lower()
    if role not in KHO_VAT_LY_ACCESS_ROLES:
        if is_ajax:
            return jsonify({"ok": False, "error": "Bạn không có quyền truy cập Kho vật lý."}), 403
        return redirect(url_for("dashboard.dashboard"))
    from modules.kho_vat_ly import outbound_sync_quick as _fn
    return _fn()


register_fb_pages_module(app)
register_chi_phi_qc_module(app)
register_cham_cong_module(app)

_scheduler = start_scheduler()

# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------
def _order_status_cache_sync_loop():
    """Background thread: sync order status cho hôm nay + hôm qua mỗi 12 phút."""
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZoneInfo

    _VN_TZ = _ZoneInfo("Asia/Ho_Chi_Minh")
    _INTERVAL = int(os.environ.get("ORDER_STATUS_SYNC_INTERVAL_SECONDS", str(12 * 60)))
    _INITIAL_DELAY = int(os.environ.get("ORDER_STATUS_SYNC_INITIAL_DELAY_SECONDS", "10"))
    _script = os.path.join(BASE_DIR, "scripts", "sync_order_status_cache.py")

    _time.sleep(_INITIAL_DELAY)

    while True:
        try:
            today = _dt.now(_VN_TZ).strftime("%Y-%m-%d")
            yesterday = (_dt.now(_VN_TZ) - _td(days=1)).strftime("%Y-%m-%d")
            import subprocess as _sp
            _sp.run(
                [sys.executable, _script, "--date", today, "--days", "2"],
                cwd=BASE_DIR,
                timeout=300,
                env=os.environ.copy(),
            )
        except Exception as _e:
            pass
        _time.sleep(_INTERVAL)


if str(os.environ.get("ORDER_STATUS_SYNC_DISABLED", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
    _ost = threading.Thread(
        target=_order_status_cache_sync_loop,
        daemon=True,
        name="order-status-cache-sync",
    )
    _ost.start()


# ---------------------------------------------------------------------------
# Background loop: tự động sync live POS status từ Pancake mỗi 15 phút
# ---------------------------------------------------------------------------
def _live_status_sync_loop():
    """Background thread: fetch live POS order counts từ Pancake API mỗi 15 phút."""
    import time as _time

    _INTERVAL = int(os.environ.get("LIVE_STATUS_SYNC_INTERVAL_SECONDS", str(15 * 60)))
    _INITIAL_DELAY = int(os.environ.get("LIVE_STATUS_SYNC_INITIAL_DELAY_SECONDS", "30"))

    _time.sleep(_INITIAL_DELAY)

    while True:
        try:
            status_data = fetch_live_pos_status()
            save_live_pos_status(status_data)
        except Exception as _e:
            import logging as _log
            _log.getLogger("live_pos_sync").error("[live-pos-sync] Lỗi: %s", _e)
        _time.sleep(_INTERVAL)


if str(os.environ.get("LIVE_STATUS_SYNC_DISABLED", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
    _lst = threading.Thread(
        target=_live_status_sync_loop,
        daemon=True,
        name="live-pos-status-sync",
    )
    _lst.start()


# ---------------------------------------------------------------------------
# Background loop: tự động sync dữ liệu analytics POS hàng ngày mỗi 60 phút
# sync_pos.py → data_shop*.json → bootstrap_daily_shop_metrics_from_json.py → DB
# ---------------------------------------------------------------------------
def _pos_daily_sync_loop():
    """Background thread: sync POS analytics data vào DB mỗi 60 phút."""
    import time as _time
    import subprocess as _sp
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZoneInfo

    _VN_TZ = _ZoneInfo("Asia/Ho_Chi_Minh")
    _INTERVAL = int(os.environ.get("POS_DAILY_SYNC_INTERVAL_SECONDS", str(60 * 60)))
    _INITIAL_DELAY = int(os.environ.get("POS_DAILY_SYNC_INITIAL_DELAY_SECONDS", "60"))
    _sync_script = os.path.join(BASE_DIR, "sync_pos.py")
    _bootstrap_script = os.path.join(BASE_DIR, "scripts", "bootstrap_daily_shop_metrics_from_json.py")
    _orders_script = os.path.join(BASE_DIR, "scripts", "sync_orders_order_items_to_db.py")

    _time.sleep(_INITIAL_DELAY)

    while True:
        try:
            today = _dt.now(_VN_TZ).strftime("%Y-%m-%d")
            yesterday = (_dt.now(_VN_TZ) - _td(days=1)).strftime("%Y-%m-%d")
            # Bước 1: Kéo data từ Pancake API → JSON files
            for _date in [yesterday, today]:
                _sp.run(
                    [sys.executable, _sync_script, "--date", _date],
                    cwd=BASE_DIR,
                    timeout=300,
                    env=os.environ.copy(),
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
            # Bước 2: Import JSON vào DB (daily_shop_metrics)
            _sp.run(
                [sys.executable, _bootstrap_script, "--require-date", today],
                cwd=BASE_DIR,
                timeout=300,
                env=os.environ.copy(),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            # Bước 3: Sync orders → orders table (hôm qua + hôm nay)
            for _date in [yesterday, today]:
                _sp.run(
                    [sys.executable, _orders_script, "--date", _date],
                    cwd=BASE_DIR,
                    timeout=600,
                    env=os.environ.copy(),
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
        except Exception as _e:
            pass
        _time.sleep(_INTERVAL)


if str(os.environ.get("POS_DAILY_SYNC_DISABLED", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
    _pds = threading.Thread(
        target=_pos_daily_sync_loop,
        daemon=True,
        name="pos-daily-sync",
    )
    _pds.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    debug_enabled = str(os.getenv("WEB_APP_DEBUG", "0")).strip().lower() in {"1", "true", "yes", "on"}
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
