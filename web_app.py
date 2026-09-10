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

from flask import Flask, request, session, g, redirect, abort, url_for, Response, send_from_directory, jsonify, render_template_string

from order_pickup_utils import order_counts_for_carrier_pickup_day
from modules.ads_mapping import register_ads_mapping_module
from scheduler import start_scheduler
from run_migrations import auto_migrate, auto_bootstrap_if_empty
from modules.salary import register_salary_module
import perm_utils
from modules.fb_pages import register_fb_pages_module
from modules.chi_phi_qc import register_chi_phi_qc_module
from modules.cham_cong import register_cham_cong_module
from modules.page_account import register_page_account_module
from modules.budget_chat import register_budget_chat_module
from modules.expense_chat import register_expense_chat_module
# moon KHÔNG dùng module lương (sếp Phong 13/08) — bỏ đăng ký, 2 trang này
# vốn cũng lỗi 500 vì enum user_role của moon không có role "kho".
# from modules.salary_2b import register_salary_2b_module
# from modules.salary_b1 import register_salary_b1_module
from modules.hr import register_hr_module

from app_constants import DASHBOARD_WEB_VERSION, PAGE_TEMPLATE, BASE_DIR
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
    is_admin_user,
    load_config,
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
# PWA — manifest, service worker, icons
# ---------------------------------------------------------------------------
_FAVICON_TAGS = (
    '<link rel="icon" href="/favicon.ico?v=2" sizes="any">'
    '<link rel="icon" type="image/png" href="/static/pwa/icon-192.png?v=2">'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=2">'
)


@app.after_request
def _them_favicon(resp):
    """Chèn thẻ khai báo icon vào MỌI trang HTML.

    Hơn 30 template có <head> riêng, sửa tay từng file vừa lâu vừa dễ sót; mà
    thiếu thẻ này thì trình duyệt giữ nguyên icon quả địa cầu đã lưu trong cache.
    """
    try:
        if (resp.status_code == 200
                and "text/html" in (resp.content_type or "")
                and not resp.direct_passthrough):
            body = resp.get_data(as_text=True)
            if "</head>" in body and 'rel="icon"' not in body:
                resp.set_data(body.replace("</head>", _FAVICON_TAGS + "</head>", 1))
    except Exception:
        pass
    return resp


@app.route("/favicon.ico")
def favicon():
    """Icon hiện trên tab trình duyệt (logo TH cam). Trình duyệt tự gọi đường dẫn
    này nên không phải thêm thẻ <link> vào từng trang."""
    return send_from_directory(
        os.path.join(app.root_path, "static"), "favicon.ico",
        mimetype="image/x-icon", max_age=86400)


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    """iPhone/iPad lưu màn hình chính thì dùng icon này."""
    return send_from_directory(
        os.path.join(app.root_path, "static", "pwa"), "icon-192.png",
        mimetype="image/png", max_age=86400)


@app.route("/manifest.json")
def pwa_manifest():
    return Response("""
{
  "name": "Moon — Quản lý",
  "short_name": "Moon",
  "description": "Hệ thống quản lý chi phí quảng cáo, đơn hàng, chấm công",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#1a1a1a",
  "theme_color": "#F59E0B",
  "orientation": "any",
  "icons": [
    { "src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}
""", mimetype="application/manifest+json")


@app.route("/sw.js")
def pwa_sw():
    return Response("""
const CACHE = 'moon-v1';
const OFFLINE_URLS = ['/'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(OFFLINE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

/* ── Web Push Notifications ─────────────────────────────────────────── */
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch(e) { data = { title: 'Tiểu Hiềm', body: event.data ? event.data.text() : '' }; }

  const title = data.title || 'Tiểu Hiềm';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/pwa/icon-192.png',
    badge: data.badge || '/static/pwa/icon-192.png',
    tag: data.tag || 'tieuhiem-notif',
    data: { url: data.url || '/', extra: data.extra || {} },
    requireInteraction: false,
    vibrate: [200, 100, 200],
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  let raw = (event.notification.data && event.notification.data.url) || '/';
  // Resolve về absolute URL để tránh 404 do Safari/iOS hiểu sai scope
  let targetUrl;
  try { targetUrl = new URL(raw, self.registration.scope).href; }
  catch (e) { targetUrl = self.registration.scope; }

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      // Ưu tiên focus window đã mở có cùng origin
      for (const c of list) {
        if ('focus' in c) {
          c.navigate(targetUrl).catch(()=>{});
          return c.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(targetUrl);
    })
  );
});
""", mimetype="application/javascript",
        headers={"Service-Worker-Allowed": "/"})


# ---------------------------------------------------------------------------
# Web Push API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/push/vapid-key", methods=["GET"])
def api_push_vapid_key():
    """Trả public key base64url để browser subscribe."""
    try:
        from push_notifications import get_vapid_public_key_b64url
        return jsonify({"ok": True, "publicKey": get_vapid_public_key_b64url()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "not logged in"}), 401
    user_id = str(session.get("user_id") or session.get("username") or "")
    if not user_id:
        return jsonify({"ok": False, "error": "no user_id"}), 400
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription") or {}
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "invalid subscription"}), 400
    try:
        from push_notifications import save_subscription
        sub_id = save_subscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=request.headers.get("User-Agent", "")[:500],
        )
        return jsonify({"ok": True, "id": sub_id})
    except Exception as e:
        app.logger.exception("push subscribe failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if not endpoint:
        return jsonify({"ok": False, "error": "endpoint required"}), 400
    try:
        from push_notifications import remove_subscription
        removed = remove_subscription(endpoint)
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    """Gửi push thử cho chính user đang login — để test."""
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "not logged in"}), 401
    user_id = str(session.get("user_id") or session.get("username") or "")
    try:
        from push_notifications import send_push_to_user
        r = send_push_to_user(
            user_id=user_id,
            title="Test thông báo ✅",
            body="Nếu bạn thấy dòng này nghĩa là push hoạt động.",
            url="/cham-cong/",
            tag="test-push",
        )
        return jsonify({"ok": True, **r})
    except Exception as e:
        app.logger.exception("push test failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/static/pwa/<path:filename>")
def pwa_static(filename):
    return send_from_directory(
        os.path.join(BASE_DIR, "static", "pwa"), filename
    )


# ---------------------------------------------------------------------------
# Context processors
# ---------------------------------------------------------------------------
@app.context_processor
def _inject_dashboard_footer():
    return {"dashboard_web_version": DASHBOARD_WEB_VERSION}


@app.context_processor
def _billing_banner_ctx():
    """Inject trạng thái thuê bao để navbar hiện banner cảnh báo sắp/đang hết hạn."""
    from flask import session as _s
    if not _s.get("logged_in"):
        return {}
    try:
        return {"billing_st": billing_status()}
    except Exception:
        return {}


@app.context_processor
def _perm_context():
    def has_module(key: str) -> bool:
        allowed = getattr(g, "allowed_modules", None)
        return allowed is None or key in allowed
    return dict(has_module=has_module)


@app.context_processor
def _marketing_brain_nav():
    from flask import session, g
    _MB_FULL_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it", "leader"}

    def can_access_marketing_brain() -> bool:
        r = str(session.get("role", "staff")).strip().lower()
        if r in _MB_FULL_ROLES:
            return True
        # NV marketing: hiện menu nếu đang được gán TK QC (cache per-request)
        cached = getattr(g, "_mb_nav_ok", None)
        if cached is not None:
            return cached
        ok = False
        uid = session.get("user_id")
        if uid:
            try:
                from db import get_conn as _gc
                with _gc() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM user_ad_account_assignments "
                            "WHERE user_id=%s AND assigned_to IS NULL LIMIT 1",
                            (int(uid),),
                        )
                        ok = cur.fetchone() is not None
            except Exception:
                ok = False
        g._mb_nav_ok = ok
        return ok
    return dict(can_access_marketing_brain=can_access_marketing_brain)


@app.context_processor
def _shop_biz_context():
    """Inject shop_biz() helper và shop_biz_badge() cho mọi template.
    Sử dụng: {{ shop_biz(shop_key_or_name) }} → 'cty'|'hkd'
             {{ shop_biz_badge(shop_key_or_name) | safe }} → HTML span badge
    """
    def _get_map():
        m = getattr(g, "_shop_biz_map", None)
        if m is not None:
            return m
        try:
            from db import get_conn as _gc
            import psycopg2.extras as _pe
            with _gc() as conn:
                with conn.cursor(cursor_factory=_pe.RealDictCursor) as cur:
                    cur.execute("SELECT shop_key, shop_name, COALESCE(business_type,'cty') AS bt FROM shops")
                    out = {}
                    for r in cur.fetchall():
                        bt = (r.get("bt") or "cty").lower()
                        if r.get("shop_key"):
                            out[r["shop_key"]] = bt
                        if r.get("shop_name"):
                            out[r["shop_name"]] = bt
                    g._shop_biz_map = out
                    return out
        except Exception:
            g._shop_biz_map = {}
            return {}

    def shop_biz(key):
        if not key:
            return "cty"
        return _get_map().get(key, "cty")

    def shop_biz_badge(key):
        bt = shop_biz(key)
        if bt == "hkd":
            return '<span class="tag-biz tag-biz-hkd" style="display:inline-block;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.3px;margin-left:6px;vertical-align:middle;line-height:1.4;background:#fef3c7;color:#b45309;">HKD</span>'
        return '<span class="tag-biz tag-biz-cty" style="display:inline-block;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.3px;margin-left:6px;vertical-align:middle;line-height:1.4;background:#dbeafe;color:#1d4ed8;">CTY</span>'

    return dict(shop_biz=shop_biz, shop_biz_badge=shop_biz_badge)


# ---------------------------------------------------------------------------
# Before request hooks
# ---------------------------------------------------------------------------

# Trang hiển thị cho user thường khi hệ thống đang bảo trì.
MAINTENANCE_PAGE = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đang bảo trì — Tiểu Hiềm</title>
<link rel="stylesheet" href="/static/cc-theme.css">
<style>
  body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:#1a1a1a;color:#f3f4f6;
       min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .box{max-width:520px;width:100%;background:#fff;color:#1f2937;border-radius:16px;
       box-shadow:0 8px 32px rgba(0,0,0,.4);padding:36px 32px;text-align:center}
  .icon{font-size:64px;margin-bottom:12px}
  h1{margin:0 0 8px;font-size:22px;color:#b45309}
  p{margin:8px 0;font-size:14px;line-height:1.6;color:#374151}
  .msg{background:#fef3c7;border:1px solid #fde68a;border-radius:10px;padding:14px;margin:16px 0;
       color:#78350f;font-size:14px;white-space:pre-wrap;text-align:left}
  .meta{font-size:12px;color:#6b7280;margin-top:14px}
  .actions{margin-top:20px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
  a.btn{padding:9px 18px;border-radius:10px;border:1.5px solid #d1d5db;background:#fff;
        color:#374151;text-decoration:none;font-size:13px;font-weight:600}
  a.btn:hover{background:#f9fafb}
  a.btn-primary{background:#F59E0B;border-color:#F59E0B;color:#fff}
  a.btn-primary:hover{background:#d97706;border-color:#d97706}
</style></head><body>
<div class="box">
  <div class="icon">🛠️</div>
  <h1>Hệ thống đang bảo trì</h1>
  <p>Vui lòng quay lại sau ít phút.</p>
  {% if message %}<div class="msg">{{ message }}</div>{% endif %}
  {% if started_at %}<div class="meta">Bắt đầu: {{ started_at }}</div>{% endif %}
  <div class="meta">Nếu gấp, vui lòng liên hệ quản trị viên.</div>
  <div class="actions">
    {% if logged_in %}
      <a class="btn" href="/logout">Đăng xuất</a>
    {% endif %}
    <a class="btn btn-primary" href="/login">Đăng nhập admin</a>
    <a class="btn" href="/">Thử lại</a>
  </div>
</div>
</body></html>"""

# Whitelist path khi bảo trì BẬT — vẫn truy cập được dù không phải admin.
_MAINTENANCE_WHITELIST_EXACT = {"/favicon.ico", "/robots.txt", "/manifest.json", "/sw.js", "/logout"}
_MAINTENANCE_WHITELIST_PREFIX = ("/static/", "/api/webhooks/", "/login")


@app.before_request
def _check_maintenance_mode():
    """Khi maintenance_mode=1: chỉ admin/superadmin/manager/it dùng được, còn lại thấy trang bảo trì."""
    path = request.path or ""
    # Whitelist tĩnh, webhook, login/logout, manifest PWA
    if path in _MAINTENANCE_WHITELIST_EXACT:
        return
    for pref in _MAINTENANCE_WHITELIST_PREFIX:
        if path.startswith(pref):
            return
    try:
        cfg = load_config() or {}
    except Exception:
        return  # nếu DB lỗi, không chặn — fail open
    if str(cfg.get("maintenance_mode", "0")) != "1":
        return
    # Đang bật bảo trì — admin được pass, mọi người khác (kể cả chưa login) đều thấy trang bảo trì
    if session.get("logged_in") and is_admin_user():
        return
    msg = str(cfg.get("maintenance_message", "") or "").strip()
    started = str(cfg.get("maintenance_started_at", "") or "").strip()
    html = render_template_string(
        MAINTENANCE_PAGE,
        message=msg,
        started_at=started,
        logged_in=bool(session.get("logged_in")),
    )
    return Response(html, status=503, mimetype="text/html; charset=utf-8")


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
from blueprints.teams_admin_bp import teams_admin_bp
from blueprints.billing_bp import billing_bp, register_billing_module, billing_status

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(ads_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(shop_bp)
app.register_blueprint(misc_bp)
app.register_blueprint(teams_admin_bp)
register_billing_module(app)


# ── Khóa mềm khi thuê bao phần mềm hết hạn quá ân hạn (SePay billing) ──
_BILLING_WHITELIST_EXACT = {"/favicon.ico", "/robots.txt", "/manifest.json", "/sw.js",
                            "/logout", "/gia-han", "/billing"}
_BILLING_WHITELIST_PREFIX = ("/static/", "/api/billing/", "/api/webhooks/", "/login", "/billing/")


@app.before_request
def _check_billing_lock():
    path = request.path or ""
    if path in _BILLING_WHITELIST_EXACT or any(path.startswith(p) for p in _BILLING_WHITELIST_PREFIX):
        return
    if not session.get("logged_in"):
        return
    # Chủ phần mềm (username 'admin') luôn vào được để quản lý/cấu hình
    if str(session.get("username", "")).strip().lower() == "admin":
        return
    try:
        st = billing_status()
    except Exception:
        return  # DB lỗi → fail open
    if st.get("state") == "khoa":
        return redirect("/gia-han")

# Bắt sale gian lận (LÕI độc lập nguồn — Pancake Chat / Webhook đổ data vào)
from modules.fraud_detect import register_fraud_detect  # noqa: E402
register_fraud_detect(app)

# Marketing Brain V0 (GĐ1: Ad ↔ Đơn ↔ Doanh thu) — module riêng, bảng mb_*
from modules.marketing_brain import marketing_brain_bp as _marketing_brain_bp  # noqa: E402
app.register_blueprint(_marketing_brain_bp)

# Leader Brain LB-1 — giám sát team kinh doanh (scoreboard, nhịp đập, giá đơn campaign)
from modules.leader_brain import leader_brain_bp as _leader_brain_bp  # noqa: E402
app.register_blueprint(_leader_brain_bp)

# LadiPage → POS: webhook nhận đơn ladipage, lưu DB, đẩy POS + trang kiểm tra
from modules.ladipage import register_ladipage_module  # noqa: E402
register_ladipage_module(app, login_required=login_required)


# Tải file phân tích/export (Excel...) — CHỈ admin/quản lý/kế toán, không public
@app.route("/exports/<path:filename>")
@login_required
def download_export_file(filename):
    from flask import abort, send_from_directory, session as _s
    role = str(_s.get("role") or "").strip().lower()
    if role not in {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}:
        abort(403)
    _dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    # send_from_directory tự chặn path traversal
    return send_from_directory(_dir, filename, as_attachment=True)


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
# [CPQC] Bỏ auto_bootstrap (sync POS/kho không cần cho bản chi phí quảng cáo)
# auto_bootstrap_if_empty()

# [CPQC] Bỏ đăng ký module Kho vật lý — không thuộc phạm vi chi phí quảng cáo
# register_kho_vat_ly_module(app)


# ─────────────────────────────────────────────────────────────────
# WEBHOOK PANCAKE — nhận push real-time khi đơn đổi trạng thái / có
# tracking code mới. KHÔNG thay thế polling — chỉ bổ sung để NV quét
# được mã vận đơn ngay khi shop bấm "Đẩy sang ĐVVC" (status 9), thay
# vì phải đợi 10s polling tiếp theo.
#
# Cấu hình bên Pancake: Cấu hình → Nâng cao → Kết nối bên thứ 3 →
#   Webhook URL: https://<domain>/api/webhooks/pancake/<pos_shop_id>
#   Dữ liệu:    Đơn hàng (orders)  ← bắt buộc; các loại khác sẽ ignore
#   Headers:    X-Webhook-Secret = <giá trị env PANCAKE_WEBHOOK_SECRET>
#               (nếu env không set → bỏ qua check; KHUYẾN NGHỊ luôn set)
# ─────────────────────────────────────────────────────────────────
_PANCAKE_STATUS_INT_TO_LABEL = {
    # KHÔNG map status 0 (Mới) — webhook bỏ qua đơn vừa tạo, đợi shop chuyển sang
    # status 9 (Chờ chuyển hàng) mới sync về DB. Đảm bảo tab "Chờ chuyển hàng"
    # chỉ chứa đơn shop đã sẵn sàng đẩy cho ĐVVC, không lẫn đơn mới chưa xác nhận.
    # (Fix 2026-05-12: đơn 2930 ngoccanth nhảy tab sai vì cũ map 0→waiting)
    1: "confirmed",   # Đã xác nhận
    4: "confirmed",   # Đang đóng hàng
    7: "confirmed",   # Đã xác nhận thường
    9: "waiting",     # Chờ chuyển hàng — ĐVVC chưa lấy
    2: "shipped",     # Đã giao ĐVVC
    3: "received",    # Đã nhận
    5: "returned",    # Đã hoàn
    6: "cancelled",   # Đã hủy
}


def _webhook_insert_items(cur, order, pos_order_id, shop_id, shop_name, warehouse_id,
                           pancake_status, tracking_code, carrier_name, picked_up_at):
    """Insert các item của đơn mới từ webhook payload → wh_outbound_requests.
    Gọi khi updated=0 và đơn chưa tồn tại trong DB (status 0/9 = waiting).
    """
    import logging as _wh_log
    _log = _wh_log.getLogger("pancake_webhook")
    try:
        from modules.kho_vat_ly.wh_sync_orders import _extract_items, _safe_parse_dt
    except Exception:
        return 0
    from tz_utils import now_hcm

    items = _extract_items(order)
    if not items:
        return 0

    # Load product lookup maps (giống _sync_outbound_locked)
    cur.execute("SELECT id, sku, name, pos_variation_id FROM wh_products")
    products = cur.fetchall()
    cur.execute("SELECT pos_variation_id, product_id FROM wh_variation_map")
    var_map_rows = cur.fetchall()

    prod_by_id = {p[0]: {"id": p[0], "sku": p[1], "name": p[2]} for p in products}
    # var_id → product: ưu tiên wh_variation_map, fallback wh_products.pos_variation_id
    var_id_to_prod: dict = {}
    for vm in var_map_rows:
        p = prod_by_id.get(vm[1])
        if p:
            var_id_to_prod[vm[0]] = p
    for p in products:
        if p[3] and p[3] not in var_id_to_prod:
            var_id_to_prod[p[3]] = prod_by_id[p[0]]
    sku_to_prod: dict = {}
    name_to_prod: dict = {}
    name_seen: set = set()
    for p in products:
        sk = (p[1] or "").upper()
        if sk:
            sku_to_prod[sk] = prod_by_id[p[0]]
        nk = (p[2] or "").strip().lower()
        if nk in name_seen:
            name_to_prod.pop(nk, None)
        elif nk:
            name_to_prod[nk] = prod_by_id[p[0]]
            name_seen.add(nk)

    display_id = str(order.get("display_id") or pos_order_id)
    try:
        ins_dt = _safe_parse_dt(order.get("inserted_at") or order.get("created_at") or "")
        order_inserted_at = ins_dt.strftime("%Y-%m-%d %H:%M") if ins_dt else ""
    except Exception:
        order_inserted_at = ""
    now_str = now_hcm().strftime("%Y-%m-%d %H:%M")

    inserted = 0
    for item in items:
        var_id = item["variation_id"]
        prod = var_id_to_prod.get(var_id)
        if not prod and item["sku"]:
            prod = sku_to_prod.get(item["sku"].upper())
        if not prod:
            prod = name_to_prod.get(item["product_name"].strip().lower())

        product_id   = prod["id"]  if prod else None
        product_sku  = (prod["sku"]  if prod else item["sku"])  or ""
        product_name = (prod["name"] if prod else item["product_name"]) or item["product_name"]

        try:
            cur.execute("""
                INSERT INTO wh_outbound_requests
                (order_code, order_id_external, shop_id, shop_name, product_id,
                 product_sku, product_name, qty_ordered, carrier_name, tracking_code,
                 carrier_picked_up_at, order_inserted_at, status, pancake_status,
                 pos_variation_id, warehouse_id, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (display_id, pos_order_id, shop_id, shop_name, product_id,
                  product_sku, product_name, item["qty"],
                  carrier_name, tracking_code, picked_up_at or "",
                  order_inserted_at, pancake_status,
                  var_id or "", warehouse_id, now_str))
            inserted += cur.rowcount or 0
        except Exception as ie:
            _log.warning("webhook INSERT item lỗi order=%s product=%s: %s",
                         pos_order_id, product_name, ie)

    return inserted


@app.route("/api/webhooks/pancake/<pos_shop_id>", methods=["POST"])
def pancake_webhook(pos_shop_id):
    import logging as _wh_log
    log = _wh_log.getLogger("pancake_webhook")

    # ── 1. Auth ────────────────────────────────────────────────
    secret = (os.environ.get("PANCAKE_WEBHOOK_SECRET") or "").strip()
    if secret:
        got = (request.headers.get("X-Webhook-Secret") or "").strip()
        if got != secret:
            log.warning("bad-secret shop=%s ip=%s", pos_shop_id, request.remote_addr)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    # ── 2. Parse payload (Pancake gửi thẳng object hoặc {"data": {"record": {...}}}) ──
    try:
        payload = request.get_json(silent=True, force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400

    if not isinstance(payload, dict):
        return jsonify({"ok": True, "skipped": "non-dict payload"}), 200

    # Hỗ trợ cả 2 shape: top-level order, hoặc wrapped trong data.record
    order = payload
    data_node = payload.get("data")
    if isinstance(data_node, dict):
        rec = data_node.get("record")
        if isinstance(rec, dict):
            order = rec
        elif any(k in data_node for k in ("id", "system_id", "status")):
            order = data_node

    # ── 3. Detect type — chỉ xử lý orders ──────────────────────
    is_order = isinstance(order, dict) and (
        "partner" in order or "system_id" in order
        or "bill_full_name" in order or "extend_code" in order
    )
    if not is_order:
        log.info("skip-event shop=%s keys=%s", pos_shop_id, list(payload.keys())[:8])
        return jsonify({"ok": True, "skipped": "not-order-event"}), 200

    pos_order_id = str(order.get("id") or "").strip()
    if not pos_order_id:
        return jsonify({"ok": False, "error": "missing order id"}), 400

    p_status_int = order.get("status")
    try:
        p_status_int = int(p_status_int)
    except (TypeError, ValueError):
        p_status_int = -1
    new_pancake_status = _PANCAKE_STATUS_INT_TO_LABEL.get(p_status_int)
    if new_pancake_status is None:
        return jsonify({"ok": True,
                        "skipped": f"status {p_status_int} không cần xử lý"}), 200

    # ── 4. Trích tracking_code + carrier (dùng helper sẵn có) ──
    try:
        from modules.kho_vat_ly.wh_sync_orders import (
            detect_carrier_name, get_tracking_code, _extract_pickup_time,
        )
        carrier_name  = detect_carrier_name(order) or ""
        tracking_code = get_tracking_code(order) or ""
        picked_up_at  = (_extract_pickup_time(order) if p_status_int in (2, 3) else "") or ""
    except Exception:
        partner        = order.get("partner") or {}
        carrier_name   = partner.get("partner_name") or partner.get("delivery_name") or ""
        tracking_code  = partner.get("extend_code") or ""
        picked_up_at   = ""
        for h in (order.get("histories") or []):
            if str(h.get("status", "")) == "2":
                picked_up_at = h.get("updated_at") or h.get("inserted_at") or ""
                break

    # ── 4.5 Track webhook received (cho trang webhook-setup) ──
    try:
        from db import get_conn as _gc_track
        with _gc_track() as _tconn:
            with _tconn.cursor() as _tcur:
                _tcur.execute(
                    "UPDATE wh_shops SET last_webhook_at = NOW() WHERE pos_shop_id = %s",
                    (pos_shop_id,)
                )
            _tconn.commit()
    except Exception:
        pass  # tracking là phụ, không cản webhook chính

    # ── 5. UPDATE existing + INSERT nếu đơn chưa có trong DB ──
    # ASYNC (sau sự cố 20/05): để không tắc gunicorn worker khi Pancake bão webhook,
    # ack 200 ngay và xử lý DB trong background thread. Pancake retry nếu thread fail.
    # ── 5. ASYNC: ack 200 ngay, DB work chạy background ──
    def _do_db_work():
        try:
            from db import get_conn as _gc
            with _gc() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, shop_name, warehouse_id
                        FROM wh_shops
                        WHERE pos_shop_id = %s AND status = 'active'
                    """, (pos_shop_id,))
                    shop_rows = cur.fetchall()
                    if not shop_rows:
                        return
                    wh_shop_ids = [r[0] for r in shop_rows]

                    # 5a. Update đầy đủ: status + tracking + carrier (đơn chưa xác nhận)
                    cur.execute("""
                        UPDATE wh_outbound_requests
                           SET pancake_status        = %s,
                               carrier_name          = CASE WHEN COALESCE(%s,'')<>''
                                                            THEN %s ELSE carrier_name END,
                               tracking_code         = CASE WHEN COALESCE(%s,'')<>''
                                                            THEN %s ELSE tracking_code END,
                               carrier_picked_up_at  = CASE WHEN COALESCE(%s,'')<>''
                                                            THEN %s ELSE carrier_picked_up_at END
                         WHERE order_id_external = %s
                           AND status IN ('pending', 'pre_confirmed')
                           AND shop_id = ANY(%s)
                    """, (new_pancake_status,
                          carrier_name, carrier_name,
                          tracking_code, tracking_code,
                          picked_up_at, picked_up_at,
                          pos_order_id, wh_shop_ids))
                    updated = cur.rowcount or 0

                    # 5b. Update chỉ tracking/carrier cho đơn đã confirmed chưa có tracking
                    if tracking_code and updated == 0:
                        cur.execute("""
                            UPDATE wh_outbound_requests
                               SET carrier_name  = CASE WHEN COALESCE(%s,'')<>''
                                                        THEN %s ELSE carrier_name END,
                                   tracking_code = %s
                             WHERE order_id_external = %s
                               AND status = 'confirmed'
                               AND (tracking_code IS NULL OR tracking_code = '')
                               AND shop_id = ANY(%s)
                        """, (carrier_name, carrier_name, tracking_code,
                              pos_order_id, wh_shop_ids))
                        updated += cur.rowcount or 0

                    # 5c. INSERT đơn mới (CHỈ status 9, single-shop)
                    inserted = 0
                    if updated == 0 and p_status_int == 9 and len(wh_shop_ids) == 1:
                        cur.execute(
                            "SELECT 1 FROM wh_outbound_requests"
                            " WHERE order_id_external = %s AND shop_id = %s LIMIT 1",
                            (pos_order_id, wh_shop_ids[0])
                        )
                        if not cur.fetchone():
                            inserted = _webhook_insert_items(
                                cur, order, pos_order_id,
                                shop_rows[0][0], shop_rows[0][1], shop_rows[0][2],
                                new_pancake_status, tracking_code, carrier_name, picked_up_at,
                            )
                conn.commit()
                log.info("[async] shop=%s order=%s %s→%s upd=%d ins=%d",
                         pos_shop_id, pos_order_id, p_status_int, new_pancake_status,
                         updated, inserted)

            # 5d. Đơn NV đã quét chuẩn bị + ĐVVC đã lấy/đã nhận → tự xác nhận
            # xuất kho (trừ tồn + status='confirmed'). Vá kẽ hở 2026-06-11:
            # webhook update pancake_status TRƯỚC polling sync → transition
            # waiting→shipped bị "tiêu thụ", auto-confirm của sync không bao
            # giờ chạy → ~263 đơn/ngày kẹt "Chờ xuất kho" không trừ tồn.
            if new_pancake_status in ("shipped", "received"):
                try:
                    from modules.kho_vat_ly.wh_db import wh_db as _whdb
                    from modules.kho_vat_ly.wh_sync_orders import _auto_confirm_pre_confirmed
                    with _whdb() as wconn:
                        _rows = wconn.execute(
                            "SELECT * FROM wh_outbound_requests"
                            " WHERE order_id_external = %s AND status = 'pre_confirmed'"
                            "   AND shop_id = ANY(%s)",
                            (pos_order_id, wh_shop_ids),
                        ).fetchall()
                        for _r in _rows:
                            _auto_confirm_pre_confirmed(
                                wconn, _r, carrier_name, tracking_code,
                                picked_up_at or (_r["carrier_picked_up_at"] or ""),
                                str(pos_order_id),
                                pancake_status_label=new_pancake_status,
                            )
                except Exception:
                    log.exception("[async] auto-confirm pre_confirmed fail shop=%s order=%s",
                                  pos_shop_id, pos_order_id)
        except Exception:
            log.exception("async db-update fail shop=%s order=%s",
                          pos_shop_id, pos_order_id)

    import threading
    threading.Thread(target=_do_db_work, daemon=True,
                     name=f"wh-{pos_shop_id}-{pos_order_id}").start()
    return jsonify({"ok": True, "queued": True, "order_id": pos_order_id}), 200


register_fb_pages_module(app)
register_chi_phi_qc_module(app)
register_cham_cong_module(app)
register_hr_module(app)
register_page_account_module(app)
register_budget_chat_module(app)
register_expense_chat_module(app)
# register_salary_2b_module(app)   # moon không dùng lương
# register_salary_b1_module(app)   # moon không dùng lương

# Scheduler chạy trong process riêng (pos-scheduler.service) khi WEB_SCHEDULER_DISABLED=1
# → cho phép tăng gunicorn workers mà không bị duplicate jobs/Telegram spam.
# Mặc định: vẫn chạy trong web process (backward-compat) trừ khi env var được set.
if not os.environ.get("WEB_SCHEDULER_DISABLED", "").strip() in ("1", "true", "yes"):
    _scheduler = start_scheduler()
else:
    _scheduler = None
    import logging as _log; _log.getLogger("scheduler").info(
        "Scheduler disabled in web process (WEB_SCHEDULER_DISABLED=1) — "
        "running via pos-scheduler.service"
    )

# ---------------------------------------------------------------------------
# Marketing Brain — nhận attribution ads từ shipcod.vn (spec MARKETING_BRAIN_SHIPCOD_SPEC.md)
# Bối cảnh: bỏ Pancake Chat → đơn POS mất ad_id; shipcod hứng referral FB rồi bắn sang đây.
# ---------------------------------------------------------------------------
@app.route("/api/mb/attribution", methods=["POST"])
def mb_attribution_ingest():
    import logging as _mblog
    log = _mblog.getLogger("mb_attribution")

    # 1. Auth — bắt buộc có secret (khác webhook Pancake: không cho qua khi chưa cấu hình)
    expected = (os.environ.get("MB_ATTRIBUTION_SECRET") or "").strip()
    if not expected:
        try:
            expected = str((load_config() or {}).get("mb_attribution_secret") or "").strip()
        except Exception:
            expected = ""
    if not expected:
        return jsonify({"ok": False, "error": "endpoint chưa cấu hình secret"}), 503
    got = (request.headers.get("X-MB-Secret") or "").strip()
    if got != expected:
        log.warning("bad-secret ip=%s", request.remote_addr)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # 2. Parse — nhận 1 object hoặc array (tối đa 500/lần)
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    items = body if isinstance(body, list) else [body]
    if len(items) > 500:
        return jsonify({"ok": False, "error": "tối đa 500 item/lần"}), 400

    # 3. Ghi inbox + áp dụng ngay cho đơn đã tồn tại
    try:
        from db import get_conn as _gc
        from modules.marketing_brain.attribution_ingest import ingest_items, apply_pending_inbox
        with _gc() as conn:
            with conn.cursor() as cur:
                received, ads_no_id = ingest_items(cur, items)
                applied = apply_pending_inbox(cur)
            conn.commit()
    except Exception as exc:
        log.exception("ingest error")
        return jsonify({"ok": False, "error": str(exc)}), 500
    resp = {"ok": True, "received": received, "applied": applied}
    if ads_no_id:
        # Cảnh báo shipcod: có gói khai ADS nhưng thiếu ad_id → kiểm khâu bắt referral FB
        resp["warning"] = f"{ads_no_id} gói ad_source=ADS nhưng ad_id rỗng — kiểm bắt referral (Meta đổi field?)"
        log.warning("ADS thiếu ad_id: %d/%d gói", ads_no_id, received)
    return jsonify(resp), 200


# ---------------------------------------------------------------------------
# Zalo Bridge — nhận tin từ zalo_bridge/bridge.js (zca-js listener)
# Map zalo_thread_id → team_code qua app_config["zalo_thread_<tid>"] = team_code
# Map zalo sender → user_id qua users.full_name (case-insensitive)
# ---------------------------------------------------------------------------

# Cooldown per (sender_uid, thread_id) để tránh Lan reply spam khi NV gõ liên tiếp.
# Map: key "uid:tid" → timestamp last reply. In-memory, mỗi worker giữ riêng.
_REPLY_COOLDOWN: dict = {}
try:
    _REPLY_COOLDOWN_SEC = float(os.environ.get("ZALO_LAN_REPLY_COOLDOWN_SEC", "3.0"))
except Exception:
    _REPLY_COOLDOWN_SEC = 3.0


def _lan_cooldown_check(sender_uid: str, thread_id: str) -> bool:
    """Trả True nếu trong cooldown window — caller nên SKIP reply (vẫn save data)."""
    try:
        import time as _t
        if not sender_uid or not thread_id:
            return False
        key = f"{sender_uid}:{thread_id}"
        now = _t.time()
        last = _REPLY_COOLDOWN.get(key, 0.0)
        if now - last < _REPLY_COOLDOWN_SEC:
            return True
        return False
    except Exception:
        return False


def _lan_cooldown_mark(sender_uid: str, thread_id: str) -> None:
    """Đánh dấu vừa reply — gọi sau mỗi nhánh reply thành công."""
    try:
        import time as _t
        if not sender_uid or not thread_id:
            return
        _REPLY_COOLDOWN[f"{sender_uid}:{thread_id}"] = _t.time()
        # Sweep entries cũ (> 60s) để map không tích tụ
        if len(_REPLY_COOLDOWN) > 500:
            now = _t.time()
            stale = [k for k, v in _REPLY_COOLDOWN.items() if now - v > 60.0]
            for k in stale:
                _REPLY_COOLDOWN.pop(k, None)
    except Exception:
        pass


@app.route("/api/zalo-bridge/inbound", methods=["POST"])
def zalo_bridge_inbound():
    import logging as _log
    log = _log.getLogger("zalo_bridge")

    # 1. Auth
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if secret:
        got = (request.headers.get("X-Bridge-Secret") or "").strip()
        if got != secret:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True, force=True) or {}
    tid = str(payload.get("zalo_thread_id") or "").strip()
    sender_name = (payload.get("zalo_sender_name") or "").strip()
    sender_uid = (payload.get("zalo_sender_id") or "").strip()
    msg_id_zalo = (payload.get("zalo_msg_id") or "").strip()
    kind = (payload.get("kind") or "text").strip()
    body = (payload.get("body") or "").strip()
    image_url = (payload.get("image_url") or "").strip()
    caption = (payload.get("caption") or "").strip()
    thread_type = (payload.get("thread_type") or "group").strip()
    is_private = (thread_type == "user")

    # Catchup từ getGroupChatHistory không trả dName → lookup user DB qua zalo_uid
    if not sender_name and sender_uid:
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT full_name, username FROM users WHERE zalo_uid=%s LIMIT 1", (sender_uid,))
                    r = cur.fetchone()
                    if r:
                        sender_name = (r[0] or r[1] or "").strip()
        except Exception:
            pass

    # Dedup: nếu zalo_msg_id đã có trong DB → skip (catchup scan / replay)
    if msg_id_zalo:
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT 1 FROM budget_chat_messages WHERE zalo_msg_id=%s
                        UNION ALL
                        SELECT 1 FROM company_expense_items WHERE zalo_msg_id=%s
                        LIMIT 1
                    """, (msg_id_zalo, msg_id_zalo))
                    if cur.fetchone():
                        return jsonify({"ok": True, "skipped": "duplicate", "zalo_msg_id": msg_id_zalo})
        except Exception as exc:
            log.warning("dedup check skip: %s", exc)

    # Ảnh: CHỈ đọc ở nhóm chi phí (zalo_expense_thread_* / zalo_expense_inbox)
    # + chat 1-1. Nhóm team NS (zalo_thread_*) hoặc nhóm khác (vd Leader Mar) →
    # BỎ QUA im lặng. Việc phân biệt biên lai vs dashboard/báo cáo do
    # handle_expense_image lo (chỉ ảnh là biên lai mới ghi nhận).
    if kind == "image":
        if not tid or not image_url:
            return jsonify({"ok": False, "error": "missing thread/image_url"}), 400

        # ƯU TIÊN 0: Caption có lệnh broadcast "gửi <nhóm>: ..." trong 1-1 với Lan
        # → forward CẢ ẢNH + text sang các group đã chọn.
        if is_private and caption.strip():
            try:
                from modules.expense_chat.lan_broadcast import handle_broadcast_if_any
                bc_reply = handle_broadcast_if_any(sender_uid, sender_name, caption,
                                                    image_url=image_url)
                if bc_reply:
                    try:
                        from modules.expense_chat import _push_to_zalo
                        _push_to_zalo(tid, bc_reply, sender_uid, sender_name,
                                      thread_type="user")
                    except Exception:
                        log.exception("push broadcast reply fail")
                    return jsonify({"ok": True, "broadcast": True, "via": "image_caption"})
            except Exception as exc:
                log.warning("lan_broadcast (image caption) fail: %s", exc)

        from app_ctx import load_config as _load_cfg_img
        _cfg_img = _load_cfg_img() or {}
        _is_expense_ctx = (
            is_private
            or bool((_cfg_img.get(f"zalo_expense_thread_{tid}") or "").strip())
            or str(_cfg_img.get("zalo_expense_inbox") or "").strip() == tid
        )
        if not _is_expense_ctx:
            return jsonify({"ok": True, "skipped": "image_non_expense_group"})

        # Ưu tiên 1: caption có pattern NS (tk X chạy/nạp Y ngày Z)?
        if caption.strip() and not is_private:
            try:
                from modules.budget_chat.ai_parser import parse_budget_message
                from modules.budget_chat.lan_personality import is_budget_intent
                parsed_cap = parse_budget_message(caption)
                if len(parsed_cap.get("items") or []) > 0 or is_budget_intent(caption):
                    # Caption rõ là NS → coi như text NS, dùng caption làm body
                    # KHÔNG OCR ảnh, sẽ rơi xuống flow text NS phía dưới
                    body = caption
                    kind = "text"
            except Exception as exc:
                log.info("caption budget parse skip: %s", exc)

        # Ưu tiên 2: caption có chi phí rõ (cho ảnh kèm note)?
        if kind == "image" and caption.strip():
            try:
                from modules.expense_chat import _parse_expense_text, handle_expense_text
                ep = _parse_expense_text(caption)
                if int(ep.get("amount_vnd") or 0) > 0:
                    # Caption rõ chi phí → ghi theo caption, không OCR ảnh
                    return handle_expense_text(tid, sender_uid, sender_name, caption,
                                               zalo_msg_id=msg_id_zalo, is_private=is_private)
            except Exception as exc:
                log.info("caption expense parse skip: %s", exc)

        # Fallback: OCR ảnh (phân biệt biên lai vs dashboard ở trong hàm)
        if kind == "image":
            from modules.expense_chat import handle_expense_image
            return handle_expense_image(tid, sender_uid, sender_name, image_url, caption,
                                        zalo_msg_id=msg_id_zalo, is_private=is_private)

    if not tid or not body:
        return jsonify({"ok": False, "error": "missing thread/body"}), 400

    # Tin 1-1 → bỏ qua flow budget, chỉ thử expense
    if is_private:
        try:
            from modules.expense_chat import (
                _parse_expense_text, handle_expense_text, _push_to_zalo,
                handle_expense_follow_up, handle_dup_pending_reply,
                _is_relevant_to_lan, _load_dup_pending,
                handle_stop_signal_if_any, _has_money_signal,
            )
            # Stop signal: NV bảo "dừng/thôi" gần đây Lan vừa hỏi → ack + dừng
            if handle_stop_signal_if_any(tid, sender_uid, sender_name, body, is_private=True):
                return jsonify({"ok": True, "stopped": True})

            # Broadcast: sếp gõ "gửi <nhóm>: <nội dung>" → Lan forward sang group.
            # Check trước menu vì cú pháp đặc trưng, không nhầm lẫn.
            try:
                from modules.expense_chat.lan_broadcast import handle_broadcast_if_any
                bc_reply = handle_broadcast_if_any(sender_uid, sender_name, body)
                if bc_reply:
                    _lan_cooldown_mark(sender_uid, tid)
                    _push_to_zalo(tid, bc_reply, sender_uid, sender_name,
                                  thread_type="user")
                    return jsonify({"ok": True, "broadcast": True})
            except Exception as exc:
                log.warning("lan_broadcast fail: %s", exc)

            # Menu admin: chỉ sếp Tưởng (UID whitelist) mới xem info quan trọng.
            # NV thường gõ menu/số → từ chối nhẹ.
            try:
                from modules.expense_chat.lan_menu import handle_menu_if_any
                menu_reply = handle_menu_if_any(sender_uid, sender_name, body)
                if menu_reply:
                    _lan_cooldown_mark(sender_uid, tid)
                    _push_to_zalo(tid, menu_reply, sender_uid, sender_name,
                                  thread_type="user")
                    return jsonify({"ok": True, "menu": True})
            except Exception as exc:
                log.warning("lan_menu fail: %s", exc)

            # Task 1 (gap 5.3): NS gửi 1-1 → hint chuyển sang group team
            # NS không persist được vì chưa biết team_code → hướng dẫn NV
            try:
                from modules.budget_chat.ai_parser import parse_budget_message as _pb_parse
                from modules.budget_chat.lan_personality import is_budget_intent as _pb_intent
                ns_parsed = _pb_parse(body)
                ns_items = len(ns_parsed.get("items") or [])
                if ns_items > 0 or _pb_intent(body):
                    if _lan_cooldown_check(sender_uid, tid):
                        log.info("zalo bridge: cooldown skip NS 1-1 hint uid=%s tid=%s", sender_uid, tid)
                    else:
                        _push_to_zalo(tid,
                                      "🌸 Lan thấy bạn báo NS qua chat riêng. NS phải báo trong "
                                      "NHÓM TEAM tương ứng để Lan ghi đúng team. Bạn copy tin "
                                      "qua nhóm team giúp Lan nhé.",
                                      sender_uid, sender_name, thread_type="user")
                        _lan_cooldown_mark(sender_uid, tid)
                    return jsonify({"ok": True, "skipped": "ns_via_1to1", "items_detected": ns_items})
            except Exception as exc:
                log.info("1-1 NS detection skip: %s", exc)

            # Ưu tiên: nếu sender đang có pending dup → check 'đúng/khác'
            # KHÔNG áp silent filter cho dup-pending (user trả lời rất ngắn "đúng/khác")
            has_dup_pending = False
            try:
                has_dup_pending = _load_dup_pending(sender_uid) is not None
            except Exception:
                has_dup_pending = False

            if has_dup_pending:
                dup_reply = handle_dup_pending_reply(tid, sender_uid, sender_name, body, is_private=True)
                if dup_reply:
                    _lan_cooldown_mark(sender_uid, tid)
                    return jsonify(dup_reply)

            # Trong chat 1-1, NV chủ đích nhắn Lan → KHÔNG silent dù không có tiền.
            # Nếu không phải NS/CP/follow-up → AI chit chat theo persona.
            ep = _parse_expense_text(body)
            if int(ep.get("amount_vnd") or 0) > 0:
                # VẪN save data dù cooldown — cooldown chỉ skip reply (xử lý trong handle_expense_text)
                # Mark cooldown nếu không trong window để handle_expense_text reply tự nhiên
                if _lan_cooldown_check(sender_uid, tid):
                    log.info("zalo bridge: cooldown 1-1 expense uid=%s tid=%s (vẫn save)", sender_uid, tid)
                # handle_expense_text tự push reply; mark cooldown trước khi nó push
                _lan_cooldown_mark(sender_uid, tid)
                return handle_expense_text(tid, sender_uid, sender_name, body,
                                           zalo_msg_id=msg_id_zalo, is_private=True)
            # Không có số → thử follow-up cho item vừa ghi (sửa loại/note)
            fu = handle_expense_follow_up(tid, sender_uid, sender_name, body, is_private=True)
            if fu:
                _lan_cooldown_mark(sender_uid, tid)
                return jsonify(fu)
            # Không match NS/CP/follow-up:
            # - Nếu body có dấu hiệu liên quan tiền (số/k/tr/keyword) nhưng amount=0
            #   → có vẻ đang báo CP nhưng thiếu rõ ràng → hỏi ghi rõ.
            # - Ngược lại → chit chat thuần → AI persona reply (cooldown 3s).
            if _lan_cooldown_check(sender_uid, tid):
                log.info("zalo bridge: cooldown skip 1-1 reply uid=%s tid=%s", sender_uid, tid)
                return jsonify({"ok": True, "skipped": "1to1_cooldown"})
            _lan_cooldown_mark(sender_uid, tid)
            # Phân biệt: 'có signal tiền nhưng parse không ra' (báo CP thiếu rõ)
            # vs 'chit chat thuần' (chào hỏi / hỏi vu vơ). Tin chỉ có 'lan' KHÔNG
            # tính là money signal → đi AI chit chat.
            if _has_money_signal(body):
                _push_to_zalo(tid,
                              "🤔 Lan đọc tin nhưng chưa thấy số tiền rõ. Bạn ghi rõ giúp Lan "
                              "(vd 'chi VPP 200k' / 'thanh toán đo đạc 600k') hoặc gửi ảnh hoá đơn nhé 🌸",
                              sender_uid, sender_name, thread_type="user")
                return jsonify({"ok": True, "skipped": "1to1_no_amount"})
            # Chit chat thuần → AI multi-turn với memory ring buffer
            try:
                from modules.budget_chat.lan_personality import ai_chat_1to1
                nv_name = sender_name or "bạn"
                reply = ai_chat_1to1(sender_uid, sender_name, body)
                if not reply:
                    reply = f"Dạ Lan đây ạ {nv_name} 🌸 Có gì Lan giúp gì nè?"
                _push_to_zalo(tid, reply, sender_uid, sender_name, thread_type="user")
                return jsonify({"ok": True, "ai_chat": True})
            except Exception as exc:
                log.warning("1-1 ai chat reply fail: %s", exc)
                _push_to_zalo(tid, f"Dạ Lan đây ạ {sender_name or 'bạn'} 🌸",
                              sender_uid, sender_name, thread_type="user")
                return jsonify({"ok": True, "ai_chat_fallback": True})
        except Exception as exc:
            log.error("1-1 expense error: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    # 2. Map thread → team_code qua app_config (NS team)
    from app_ctx import load_config
    cfg = load_config()
    team_code = (cfg.get(f"zalo_thread_{tid}") or "").strip()

    # Nếu thread KHÔNG phải team NS (vd group Inbox Chi phí) → thử expense parse luôn
    if not team_code:
        try:
            from modules.expense_chat import (
                _parse_expense_text, handle_expense_text, handle_expense_follow_up,
                handle_dup_pending_reply, handle_stop_signal_if_any,
            )
            # Stop signal
            if handle_stop_signal_if_any(tid, sender_uid, sender_name, body, is_private=False):
                return jsonify({"ok": True, "stopped": True})
            # Pending dup priority
            dup_reply = handle_dup_pending_reply(tid, sender_uid, sender_name, body, is_private=False)
            if dup_reply:
                _lan_cooldown_mark(sender_uid, tid)
                return jsonify(dup_reply)
            ep = _parse_expense_text(body)
            if int(ep.get("amount_vnd") or 0) > 0:
                if _lan_cooldown_check(sender_uid, tid):
                    log.info("zalo bridge: cooldown non-team expense uid=%s tid=%s (vẫn save)", sender_uid, tid)
                _lan_cooldown_mark(sender_uid, tid)
                return handle_expense_text(tid, sender_uid, sender_name, body, zalo_msg_id=msg_id_zalo)
            # Follow-up cho item gần nhất (cross-thread, vd inbox group bổ sung context cho 1-1)
            fu = handle_expense_follow_up(tid, sender_uid, sender_name, body, is_private=False)
            if fu:
                _lan_cooldown_mark(sender_uid, tid)
                return jsonify(fu)
        except Exception as exc:
            log.info("expense parse for non-team thread skip: %s", exc)
        # Group Inbox có nhiều tin tag @NV (Lan duyệt/đẩy summary tag NV) → DỄ
        # trigger nhầm. Không tự reply hint nữa, để im lặng.
        log.info("zalo bridge: thread %s chưa map team — skip", tid)
        return jsonify({"ok": False, "error": "thread not mapped", "thread_id": tid}), 200

    # 3. Match sender → user
    user_id = None
    _matched_via_name = False
    has_zalo_uid = False
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Ưu tiên zalo_uid nếu users có cột này
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                     WHERE table_name='users' AND column_name='zalo_uid'
                """)
                has_zalo_uid = cur.fetchone() is not None
                if has_zalo_uid and sender_uid:
                    cur.execute("SELECT id FROM users WHERE zalo_uid = %s LIMIT 1", (sender_uid,))
                    r = cur.fetchone()
                    if r:
                        user_id = int(r[0])
                if not user_id and sender_name:
                    # Match full_name không phân biệt hoa thường, bỏ space dư
                    norm = " ".join(sender_name.split()).lower()
                    cur.execute("""
                        SELECT id FROM users
                         WHERE LOWER(TRIM(full_name)) = %s
                            OR LOWER(TRIM(username))  = %s
                         LIMIT 1
                    """, (norm, norm))
                    r = cur.fetchone()
                    if r:
                        user_id = int(r[0])
                        _matched_via_name = True
                # Fuzzy: bỏ dấu + token match 2 chiều.
                #  - Zalo "Tiến Anh"(2 tok) ⊂ DB "Đinh Tiến Anh"(3 tok) → match
                #  - DB "Đình Quân"(2 tok) ⊂ Zalo "Nguyen Tran Dinh Quan"(4 tok) → match
                if not user_id and sender_name:
                    import unicodedata as _ud
                    def _strip_diacritics(s):
                        # NFD bỏ dấu thanh + đổi đ→d (chữ đ là code point riêng, NFD không tách)
                        s = (s or "").replace("đ", "d").replace("Đ", "D")
                        return "".join(c for c in _ud.normalize("NFD", s)
                                       if _ud.category(c) != "Mn").lower()
                    z_tokens = set(t for t in _strip_diacritics(sender_name).split() if len(t) >= 2)
                    if len(z_tokens) >= 2:
                        cur.execute("""
                            SELECT id, full_name, username, role FROM users
                             WHERE full_name IS NOT NULL AND TRIM(full_name) <> ''
                               AND (LOWER(username) NOT LIKE '%test%'
                                    AND LOWER(username) NOT LIKE '%demo%'
                                    AND LOWER(username) NOT LIKE '%domo%')
                        """)
                        candidates = []
                        for uid, fn, un, rl in cur.fetchall():
                            db_tokens = set(t for t in _strip_diacritics(fn).split() if len(t) >= 2)
                            if not db_tokens:
                                continue
                            # Match nếu 1 phía là subset của phía kia + giao thoa ≥2 token
                            common = z_tokens & db_tokens
                            if len(common) >= 2 and (db_tokens.issubset(z_tokens)
                                                     or z_tokens.issubset(db_tokens)):
                                candidates.append((uid, fn, len(common)))
                        # Ưu tiên candidate có nhiều token chung nhất
                        candidates.sort(key=lambda x: -x[2])
                        if len(candidates) == 1 or (len(candidates) >= 2
                                                    and candidates[0][2] > candidates[1][2]):
                            user_id = int(candidates[0][0])
                            _matched_via_name = True
                            log.info("zalo bridge: fuzzy match '%s' → user #%s '%s' (%d common)",
                                     sender_name, user_id, candidates[0][1], candidates[0][2])
                        elif len(candidates) > 1:
                            log.info("zalo bridge: fuzzy ambiguous '%s' → %d tied, skip",
                                     sender_name, len(candidates))
    except Exception as exc:
        log.warning("zalo bridge: lookup user error: %s", exc)

    # Self-heal: vừa match qua tên + user chưa có zalo_uid → ghi UID để lần sau match thẳng.
    if user_id and _matched_via_name and sender_uid and has_zalo_uid:
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE users SET zalo_uid = %s
                         WHERE id = %s AND (zalo_uid IS NULL OR zalo_uid = '')
                    """, (sender_uid, user_id))
                    if cur.rowcount > 0:
                        log.info("zalo bridge: auto-link user #%s zalo_uid=%s (matched via name)",
                                 user_id, sender_uid)
                    conn.commit()
        except Exception as exc:
            log.warning("zalo bridge: auto-link zalo_uid fail: %s", exc)

    # Skip hint khi sender_name rỗng — "Chào bạn!" generic không có ích, dễ rối nhóm.
    if not user_id and not (sender_name or "").strip():
        log.info("zalo bridge: sender uid=%s name=EMPTY → silent (catchup orphan)", sender_uid)
        return jsonify({"ok": False, "error": "sender empty name", "sender_uid": sender_uid}), 200

    if not user_id:
        log.info("zalo bridge: sender %r (uid=%s) chưa match user — log pending", sender_name, sender_uid)
        # Ghi nhận pending để admin map qua UI
        try:
            from db import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO zalo_pending_senders
                            (sender_uid, sender_name, last_thread_id, message_count, last_body_snippet)
                        VALUES (%s, %s, %s, 1, %s)
                        ON CONFLICT (sender_uid) DO UPDATE SET
                            sender_name = EXCLUDED.sender_name,
                            last_thread_id = EXCLUDED.last_thread_id,
                            message_count = zalo_pending_senders.message_count + 1,
                            last_seen = NOW(),
                            last_body_snippet = EXCLUDED.last_body_snippet
                    """, (sender_uid or "_", sender_name or "(không tên)", tid, body[:100]))
                    conn.commit()
        except Exception as exc:
            log.warning("zalo bridge: log pending error: %s", exc)
        # Push hint 1 lần (cooldown dài 1h) cho NV biết phải báo IT map UID.
        # Chỉ push khi tin có vẻ liên quan công việc (NS/CP/Lan/TK QC) để không spam chit chat.
        try:
            from modules.expense_chat import (
                _is_relevant_to_lan as _exp_relevant,
                _push_to_zalo as _exp_push,
            )
            import time as _t_unmapped
            if sender_uid and _exp_relevant(body):
                cd_key = f"unmapped_{sender_uid}"
                now_ts = _t_unmapped.time()
                last = _REPLY_COOLDOWN.get(cd_key, 0)
                if now_ts - last >= 3600:  # 1h cooldown riêng cho hint unmapped
                    _REPLY_COOLDOWN[cd_key] = now_ts
                    hint = (f"👋 Chào {sender_name or 'bạn'}! Lan chưa nhận diện được "
                            "tài khoản Zalo của bạn nên chưa ghi nhận được tin.\n\n"
                            "Bạn nhờ IT/admin map giúp UID Zalo nhé "
                            "(báo tên đầy đủ + công ty), Lan sẽ phục vụ ngay ạ 🌸")
                    _exp_push(tid, hint, sender_uid, sender_name or "",
                              thread_type="user" if is_private else "group")
        except Exception as exc:
            log.warning("zalo bridge: unmapped hint push fail: %s", exc)
        return jsonify({"ok": False, "error": "sender not matched",
                        "sender_name": sender_name, "sender_uid": sender_uid}), 200

    # 4. Reuse pipeline budget_chat
    try:
        from db import get_conn
        from modules.budget_chat.repository import (
            insert_message, save_parse_result, insert_lan_message,
            fill_missing_date_for_user,
        )
        from modules.budget_chat.ai_parser import parse_budget_message
        from modules.budget_chat.lan_personality import (
            is_budget_intent, ai_reply_parse_fail, ai_reply_ask_date,
            ai_reply_fill_date, extract_fill_date_intent,
        )
        try:
            from tz_utils import now_hcm
            today = now_hcm().date()
        except Exception:
            from datetime import date as _date
            today = _date.today()
        from datetime import timedelta as _td
        today_iso = today.isoformat()
        tomorrow_iso = (today + _td(days=1)).isoformat()

        # 4a. Insert message (idempotent qua zalo_msg_id)
        with get_conn() as conn:
            with conn.cursor() as cur:
                msg_id = insert_message(cur, team_code, user_id, body, zalo_msg_id=msg_id_zalo or None)
                conn.commit()

        # 4b. Fill-date intent
        fill_date = extract_fill_date_intent(body, today_iso, tomorrow_iso)
        if fill_date:
            user_name = sender_name or f"NV#{user_id}"
            with get_conn() as conn:
                with conn.cursor() as cur:
                    filled = fill_missing_date_for_user(cur, user_id, team_code, fill_date, user_id)
                    if filled:
                        from datetime import date as _d
                        try:
                            for_date_vn = _d.fromisoformat(fill_date).strftime("%d/%m/%Y")
                        except Exception:
                            for_date_vn = fill_date
                        summary = ", ".join(
                            f"TK {it['tk_name_raw']}"
                            + (f" thẻ {it['card_last4']}" if it['card_last4'] else "")
                            + f" {it['amount_vnd']:,}đ".replace(",", ".")
                            for it in filled[:5]
                        )
                        lan_text = ai_reply_fill_date(user_name, for_date_vn, summary)
                        insert_lan_message(cur, team_code, lan_text)
                    conn.commit()
            if filled:
                try:
                    from modules.budget_chat import _push_lan_to_zalo
                    _push_lan_to_zalo(team_code, lan_text, user_id=user_id)
                except Exception:
                    pass
                return jsonify({"ok": True, "msg_id": msg_id, "filled": len(filled), "source": "zalo"})

        # 4c. Parse báo NS trước
        parsed = parse_budget_message(body)
        items_count = len(parsed.get("items") or [])
        lan_text_for_zalo = None

        # Nếu KHÔNG phải tin NS → thử parse như chi phí Cty
        # (vd "mua giấy 200k", "lương Nam 5tr", "tiền điện 2tr5")
        # Nếu tin có liên quan TK quảng cáo (vd "tài khoản LinhTH16.4") mà parser NS không bắt được items
        # → đây vẫn là NS, chỉ là format chưa chuẩn → ask back, KHÔNG rơi xuống expense
        try:
            from modules.expense_chat import _is_ad_account_context, _push_to_zalo as _exp_push
            if items_count == 0 and _is_ad_account_context(body):
                user_name = sender_name or f"NV#{user_id}"
                if _lan_cooldown_check(sender_uid, tid):
                    log.info("zalo bridge: cooldown skip TK QC hint uid=%s tid=%s", sender_uid, tid)
                    return jsonify({"ok": True, "msg_id": msg_id, "asked_ns_format": False,
                                    "skipped": "cooldown", "source": "zalo"})
                _exp_push(tid,
                          "🤔 Lan thấy bạn nhắc tới TÀI KHOẢN QUẢNG CÁO. Bạn gõ chuẩn format giúp Lan "
                          "ghi NS nhé: `tk <Tên TK> chạy <Số tiền> ngày <ngày>` "
                          "(vd: 'tk LinhTH16.4 chạy 500k ngày mai') 🌸",
                          sender_uid, user_name)
                _lan_cooldown_mark(sender_uid, tid)
                return jsonify({"ok": True, "msg_id": msg_id, "asked_ns_format": True, "source": "zalo"})
        except Exception as exc:
            log.info("ad_account NS reroute skip: %s", exc)

        if items_count == 0 and not is_budget_intent(body):
            try:
                from modules.expense_chat import (
                    handle_expense_text, _parse_expense_text, handle_expense_follow_up,
                    handle_dup_pending_reply, handle_stop_signal_if_any,
                )
                # Stop signal — kiểm tra trước mọi parse
                if handle_stop_signal_if_any(tid, sender_uid, sender_name, body, is_private=False):
                    return jsonify({"ok": True, "msg_id": msg_id, "stopped": True, "source": "zalo"})
                # Pending dup priority
                dup_reply = handle_dup_pending_reply(tid, sender_uid, sender_name, body, is_private=False)
                if dup_reply:
                    _lan_cooldown_mark(sender_uid, tid)
                    return jsonify(dup_reply)
                ep = _parse_expense_text(body)
                if int(ep.get("amount_vnd") or 0) > 0:
                    if _lan_cooldown_check(sender_uid, tid):
                        log.info("zalo bridge: cooldown team expense uid=%s tid=%s (vẫn save)", sender_uid, tid)
                    _lan_cooldown_mark(sender_uid, tid)
                    return handle_expense_text(team_code, sender_uid, sender_name, body, zalo_msg_id=msg_id_zalo)
                # Không có số → thử coi là follow-up cho item gần nhất (sửa loại/note)
                fu = handle_expense_follow_up(tid, sender_uid, sender_name, body, is_private=False)
                if fu:
                    _lan_cooldown_mark(sender_uid, tid)
                    return jsonify(fu)
                # Smalltalk: NV tag @Lan nhưng không có NS/CP → reply thân thiện
                from modules.budget_chat.lan_personality import is_lan_mention, ai_reply_smalltalk
                if is_lan_mention(body):
                    if _lan_cooldown_check(sender_uid, tid):
                        log.info("zalo bridge: cooldown skip smalltalk uid=%s tid=%s", sender_uid, tid)
                    else:
                        _lan_cooldown_mark(sender_uid, tid)
                        from modules.budget_chat import _push_lan_to_zalo
                        st_text = ai_reply_smalltalk(sender_name or f"NV#{user_id}", body)
                        try:
                            _push_lan_to_zalo(team_code, st_text, user_id=user_id)
                        except Exception as exc:
                            log.warning("smalltalk push fail: %s", exc)
                        return jsonify({"ok": True, "msg_id": msg_id, "smalltalk": True, "source": "zalo"})
            except Exception as exc:
                log.info("expense fallback skip: %s", exc)

        with get_conn() as conn:
            with conn.cursor() as cur:
                save_parse_result(cur, msg_id, parsed)
                user_name = sender_name or f"NV#{user_id}"
                if items_count == 0 and is_budget_intent(body):
                    lan_text = ai_reply_parse_fail(user_name, body)
                    insert_lan_message(cur, team_code, lan_text)
                    lan_text_for_zalo = lan_text
                elif items_count > 0 and not parsed.get("for_date"):
                    items_summary = ", ".join(
                        f"TK {it.get('tk_name','?')}"
                        + (f" thẻ {it.get('card_last4')}" if it.get('card_last4') else "")
                        + f" {int(it.get('amount_vnd') or 0):,}đ".replace(",", ".")
                        for it in (parsed.get("items") or [])[:5]
                    )
                    lan_text = ai_reply_ask_date(user_name, body, items_summary)
                    insert_lan_message(cur, team_code, lan_text)
                    lan_text_for_zalo = lan_text
                elif items_count > 0 and parsed.get("for_date"):
                    # Happy path: NS đầy đủ items + ngày → xác nhận ghi nhận
                    items_summary = ", ".join(
                        f"TK {it.get('tk_name','?')}"
                        + (f" thẻ {it.get('card_last4')}" if it.get('card_last4') else "")
                        + f" {int(it.get('amount_vnd') or 0):,}đ".replace(",", ".")
                        for it in (parsed.get("items") or [])[:5]
                    )
                    try:
                        from datetime import date as _d
                        _fd = _d.fromisoformat(parsed["for_date"])
                        for_date_vn = _fd.strftime("%d/%m/%Y")
                    except Exception:
                        for_date_vn = str(parsed.get("for_date") or "")
                    from modules.budget_chat.lan_personality import ai_reply_ack_edit
                    lan_text = ai_reply_ack_edit(user_name, items_summary, for_date_vn)
                    insert_lan_message(cur, team_code, lan_text)
                    lan_text_for_zalo = lan_text
                conn.commit()

        if lan_text_for_zalo:
            if _lan_cooldown_check(sender_uid, tid):
                log.info("zalo bridge: cooldown skip NS lan reply uid=%s tid=%s", sender_uid, tid)
            else:
                try:
                    from modules.budget_chat import _push_lan_to_zalo
                    _push_lan_to_zalo(team_code, lan_text_for_zalo, user_id=user_id)
                    _lan_cooldown_mark(sender_uid, tid)
                except Exception:
                    pass

        return jsonify({"ok": True, "msg_id": msg_id, "items": items_count, "source": "zalo"})
    except Exception as exc:
        log.error("zalo bridge: pipeline error: %s", exc, exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Cookie sync endpoint — Chrome extension đẩy cookie Zalo tự động
# Verify X-Sync-Secret, atomic write .env, restart zalo-bridge service.
# ---------------------------------------------------------------------------
_ORDER_STATUS_INT = {"shipped": 2, "received": 3, "returned": 5}
_ORDER_STATUS_LABEL_VN = {"shipped": "Đang giao", "received": "Đã nhận", "returned": "Đã hoàn"}


@app.route("/api/dashboard/order-list")
def api_dashboard_order_list():
    """List đơn theo trạng thái POS + kỳ — gọi Pancake API LIVE (đúng current status).

    Params: status=shipped|received|returned, date_from, date_to (YYYY-MM-DD).
    Chỉ gọi API các shop có count>0 trong shop_order_status_cache (nhẹ).
    Cap 300 đơn để không treo. Khớp số khối xanh dashboard theo kỳ.
    """
    from flask import session as _sess
    if not _sess.get("user_id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    status = (request.args.get("status") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    status_int = _ORDER_STATUS_INT.get(status)
    if not status_int or not date_from or not date_to:
        return jsonify({"ok": False, "error": "missing/invalid params"}), 400

    CAP = 300
    import requests as _rq
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    # 1. Tìm shop có count>0 cho status này (per-shop cache) + scope quyền
    try:
        from app_ctx import (load_order_status_from_status_cache_range,
                             get_allowed_shop_keys_for_current_user)
        allowed = get_allowed_shop_keys_for_current_user()
    except Exception:
        allowed = None
    field = {"shipped": "sent_orders", "received": "received_orders",
             "returned": "returned_orders"}[status]
    per_shop = load_order_status_from_status_cache_range(
        date_from, date_to, allowed_shop_keys=allowed) or {}
    target_shops = [sk for sk, d in per_shop.items() if int(d.get(field, 0) or 0) > 0]
    if not target_shops:
        return jsonify({"ok": True, "orders": [], "total_count": 0,
                        "status_label": _ORDER_STATUS_LABEL_VN.get(status, status)})

    # 2. Map shop_key → (pancake_shop_id, api_key, shop_name)
    shop_api = {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.shop_key, s.pancake_shop_id, s.shop_name, ws.pos_api_key
                      FROM shops s
                      JOIN wh_shops ws ON ws.pos_shop_id = s.pancake_shop_id
                     WHERE s.shop_key = ANY(%s)
                       AND ws.pos_api_key IS NOT NULL AND ws.pos_api_key != ''
                """, (target_shops,))
                for sk, psid, sname, key in cur.fetchall():
                    shop_api[sk] = (str(psid), key, sname or sk)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"db: {exc}"}), 500

    # 3. UTC range theo ngày VN
    def _vn_range(d_from, d_to):
        f = _dt.strptime(d_from, "%Y-%m-%d"); t = _dt.strptime(d_to, "%Y-%m-%d")
        su = _dt(f.year, f.month, f.day) - _td(hours=7)
        eu = _dt(t.year, t.month, t.day, 23, 59, 59) - _td(hours=7)
        return su.strftime("%Y-%m-%dT%H:%M:%S.000Z"), eu.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    start_iso, end_iso = _vn_range(date_from, date_to)

    orders = []
    truncated = False
    for sk, (psid, key, sname) in shop_api.items():
        if len(orders) >= CAP:
            truncated = True
            break
        page = 1
        while len(orders) < CAP:
            try:
                r = _rq.post(
                    f"https://pos.pancake.vn/api/v1/shops/{psid}/orders/get_orders",
                    params=[("api_key", key), ("page_size", 100), ("page", page),
                            ("updateStatus", "inserted_at"), ("editorId", "none"),
                            ("option_sort", "inserted_at_desc"),
                            ("timeRange[]", start_iso), ("timeRange[]", end_iso),
                            ("statuses[]", status_int)],
                    json={}, timeout=20,
                )
                data = r.json() if r.status_code == 200 else {}
            except Exception:
                break
            recs = data.get("data") or []
            if not recs:
                break
            for o in recs:
                if int(o.get("status", -1)) != status_int:
                    continue
                orders.append({
                    "shop": sname,
                    "order_id": o.get("system_id") or o.get("id") or "",
                    "customer": (o.get("bill_full_name") or
                                 (o.get("customer") or {}).get("name") or ""),
                    "amount": int(o.get("total_price") or o.get("cod") or 0),
                    "tracking": ((o.get("partner") or {}).get("extend_code") or ""),
                    "inserted_at": (o.get("inserted_at") or "")[:10],
                })
                if len(orders) >= CAP:
                    truncated = True
                    break
            if len(recs) < 100:
                break
            page += 1

    return jsonify({
        "ok": True,
        "orders": orders,
        "total_count": len(orders),
        "truncated": truncated,
        "cap": CAP,
        "status_label": _ORDER_STATUS_LABEL_VN.get(status, status),
    })


@app.route("/zalo-bridge/extension")
def zalo_bridge_extension_page():
    """Redirect cũ → tab Settings mới (UX nhất quán)."""
    return redirect("/settings?tab=zalo_bridge")


def _zalo_bridge_extension_page_LEGACY_UNUSED():
    """Trang cũ — giữ code không reachable để tham khảo, đã thay bằng Settings tab."""
    from flask import session, abort, render_template_string
    from app_ctx import load_users
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("auth.login"))
    users = load_users() or []
    me = next((u for u in users if int(u.get("id") or 0) == int(uid)), None)
    role = (me.get("role") or "").lower() if me else ""
    if role not in {"admin", "superadmin", "manager", "it"}:
        abort(403)
    secret = (os.environ.get("ZALO_COOKIE_SYNC_SECRET") or "").strip()
    endpoint = request.url_root.rstrip("/") + "/api/zalo-bridge/cookie-sync"
    html = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<title>🧸 Chrome Extension Zalo Cookie Sync</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
<style>
body { background:#f5f6fa; font-family:'Segoe UI',sans-serif; padding:24px; }
.wrap { max-width:780px; margin:0 auto; }
.card { background:#fff; border-radius:12px; padding:18px 22px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.06); }
h3 { color:#F59E0B; margin:0 0 12px; font-size:18px; }
code, .copy { background:#f3f4f6; padding:6px 10px; border-radius:6px; font-family:monospace; font-size:13px; word-break:break-all; display:inline-block; }
.copy { cursor:pointer; border:1px dashed #d1d5db; }
.copy:hover { background:#e5e7eb; }
.step { padding:10px 14px; background:#fff7ed; border-left:4px solid #F59E0B; border-radius:6px; margin:8px 0; }
.btn-download { background:#16a34a; color:#fff; padding:14px 28px; font-size:16px; font-weight:600; border-radius:10px; text-decoration:none; display:inline-block; }
.btn-download:hover { background:#15803d; color:#fff; }
</style></head>
<body><div class="wrap">

<div class="card">
  <h3>🧸 Chrome Extension — Zalo Cookie Sync</h3>
  <p>Extension tự đồng bộ cookie Zalo Web của tài khoản "Trợ lý Lan" về server, để bridge online liên tục mà không phải paste cookie thủ công mỗi 60 ngày.</p>
  <a class="btn-download" href="{{ url_for('zalo_bridge_extension_zip') }}">⬇ Tải extension (.zip)</a>
</div>

<div class="card">
  <h3>📦 Cài đặt Chrome</h3>
  <div class="step"><b>1.</b> Giải nén file vừa tải về 1 folder bất kỳ (vd <code>~/Downloads/tieuhiem-zalo-sync</code>).</div>
  <div class="step"><b>2.</b> Mở Chrome → vào <code>chrome://extensions/</code> → bật <b>Developer mode</b> (góc trên phải).</div>
  <div class="step"><b>3.</b> Bấm <b>"Load unpacked"</b> → chọn folder vừa giải nén → extension xuất hiện với icon cam 🟧.</div>
  <div class="step"><b>4.</b> Bấm icon extension trên thanh Chrome → popup hiện ra.</div>
</div>

<div class="card">
  <h3>🔑 Cấu hình trong popup</h3>
  <p><b>Endpoint server</b> (đã pre-fill mặc định):</p>
  <div class="copy" onclick="navigator.clipboard.writeText(this.innerText); this.innerText='✓ Copied!'; setTimeout(()=>this.innerText='{{ endpoint }}', 1500)">{{ endpoint }}</div>

  <p class="mt-3"><b>X-Sync-Secret</b> (paste vào ô Secret):</p>
  {% if secret %}
  <div class="copy" onclick="navigator.clipboard.writeText(this.innerText); this.innerText='✓ Copied!'; setTimeout(()=>this.innerText='{{ secret }}', 1500)">{{ secret }}</div>
  {% else %}
  <div class="alert alert-danger">⚠ Server chưa cấu hình <code>ZALO_COOKIE_SYNC_SECRET</code> — liên hệ IT.</div>
  {% endif %}

  <div class="step mt-3"><b>5.</b> Sau khi paste → bấm <b>💾 Lưu</b> → <b>🔄 Sync ngay</b>.</div>
  <div class="step"><b>6.</b> Nếu hiện <code>✓ Sync OK · N cookies</code> → xong. Bridge tự restart, Lan online.</div>
</div>

<div class="card">
  <h3>🔄 Auto-sync</h3>
  <ul style="line-height:1.8">
    <li>Khi mở <code>chat.zalo.me</code> → tự gửi cookie.</li>
    <li>Khi Zalo refresh cookie (xpw_sek đổi) → tự gửi.</li>
    <li>Mỗi 60 phút → tự gửi phòng hờ.</li>
    <li>Cookie hết hạn → anh chỉ cần login Zalo Web lại → extension tự sync.</li>
  </ul>
</div>

<div class="card">
  <h3>🚨 Trouble-shooting</h3>
  <p>Popup hiện <b>✗ Sync fail</b>:</p>
  <ul style="line-height:1.8">
    <li><code>HTTP 401</code> → Secret sai. Copy lại từ trang này, dán đúng.</li>
    <li><code>HTTP 400 missing cookies/imei/user_agent</code> → Anh chưa login Zalo Web, hoặc tab Zalo chưa load xong. Mở <code>chat.zalo.me</code> rồi bấm Sync ngay.</li>
    <li><code>HTTP 503 server-side secret unset</code> → Server chưa cấu hình. Báo IT.</li>
    <li>Không thấy popup → Refresh tab <code>chrome://extensions/</code>, kiểm tra extension không có nút "Lỗi" đỏ.</li>
  </ul>
</div>

<p class="text-muted small text-center mt-4">
  <a href="/">← Về trang chủ</a> ·
  <a href="{{ url_for('budget_chat.zalo_mapping') }}">Map Zalo</a>
</p>

</div></body></html>"""
    return render_template_string(html, secret=secret, endpoint=endpoint)


@app.route("/zalo-bridge/extension.zip")
def zalo_bridge_extension_zip():
    """Build folder chrome_extension/ thành .zip on-the-fly để tải."""
    from flask import session, abort, send_file
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("auth.login"))
    from app_ctx import load_users
    users = load_users() or []
    me = next((u for u in users if int(u.get("id") or 0) == int(uid)), None)
    role = (me.get("role") or "").lower() if me else ""
    if role not in {"admin", "superadmin", "manager", "it"}:
        abort(403)

    import io, zipfile
    ext_dir = "/home/admin1/tieuhiemsoft/chrome_extension"
    if not os.path.isdir(ext_dir):
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(ext_dir):
            for fn in files:
                full = os.path.join(root, fn)
                arcname = "tieuhiem-zalo-sync/" + os.path.relpath(full, ext_dir)
                zf.write(full, arcname)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="tieuhiem-zalo-sync.zip",
    )


@app.route("/api/zalo-bridge/cookie-sync", methods=["POST"])
def zalo_bridge_cookie_sync():
    import logging as _log
    log = _log.getLogger("zalo_cookie_sync")

    # Auth
    secret = (os.environ.get("ZALO_COOKIE_SYNC_SECRET") or "").strip()
    if not secret:
        return jsonify({"ok": False, "error": "server-side secret unset"}), 503
    got = (request.headers.get("X-Sync-Secret") or "").strip()
    if got != secret:
        log.warning("zalo cookie sync bad-secret ip=%s", request.remote_addr)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True, force=True) or {}
    cookies = payload.get("cookies") or []
    imei = (payload.get("imei") or "").strip()
    ua = (payload.get("user_agent") or "").strip()
    if not cookies or not imei or not ua:
        return jsonify({"ok": False, "error": "missing cookies/imei/user_agent"}), 400

    try:
        import json as _json
        # Tách theo từng instance (cpqc dùng bridge riêng, KHÔNG đụng tieuhiem)
        env_path = os.environ.get("ZALO_BRIDGE_ENV_PATH", "/home/admin1/tieuhiemsoft/zalo_bridge/.env")
        # Đọc env hiện tại
        try:
            cur = open(env_path).read()
        except FileNotFoundError:
            cur = ""

        # Helper replace dòng KEY=...
        def _set_line(text: str, key: str, value: str) -> str:
            import re as _re
            line = f"{key}={value}"
            pat = _re.compile(rf"^{_re.escape(key)}=.*$", _re.MULTILINE)
            if pat.search(text):
                return pat.sub(line, text)
            return (text.rstrip("\n") + ("\n" if text else "") + line + "\n")

        cookies_json = _json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
        new_env = cur
        new_env = _set_line(new_env, "ZALO_COOKIES", f"'{cookies_json}'")
        new_env = _set_line(new_env, "ZALO_IMEI", imei)
        new_env = _set_line(new_env, "ZALO_USER_AGENT", ua)

        # Atomic write
        tmp = env_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(new_env)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)

        # Restart bridge (cần sudoers NOPASSWD)
        import subprocess as _sp
        _bridge_svc = os.environ.get("ZALO_BRIDGE_SERVICE", "zalo-bridge")
        try:
            r = _sp.run(["sudo", "/bin/systemctl", "restart", _bridge_svc],
                        capture_output=True, text=True, timeout=15)
            restart_ok = (r.returncode == 0)
            restart_msg = r.stderr[:200] if not restart_ok else "restarted"
        except Exception as exc:
            restart_ok = False
            restart_msg = f"sudo error: {exc}"

        log.info("zalo cookie sync OK %d cookies, restart=%s", len(cookies), restart_msg)
        return jsonify({
            "ok": True,
            "cookies_count": len(cookies),
            "restart_ok": restart_ok,
            "restart_msg": restart_msg,
        })
    except Exception as exc:
        log.error("zalo cookie sync error: %s", exc, exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


def _bridge_base_url() -> str:
    """Base URL của bridge HTTP server (mặc định 127.0.0.1:5051)."""
    raw = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
    return raw.rsplit("/", 1)[0]  # bỏ /send


@app.route("/lan-qr")
def lan_qr_page():
    """Trang quét QR đăng nhập Zalo cho Lan Moon — admin/manager/IT."""
    from flask import session as _sess, render_template as _rt, redirect as _rd
    if not _sess.get("user_id"):
        return _rd("/login")
    if str(_sess.get("role", "")).lower() not in ("admin", "superadmin", "manager", "it"):
        return "Chỉ admin/quản lý được vào trang này", 403
    return _rt("lan_qr.html")


@app.route("/api/zalo-bridge/qr-start", methods=["POST"])
def zalo_bridge_qr_start():
    """Proxy → bridge /login-qr/start. Chỉ admin/IT (session)."""
    from flask import session as _sess
    if not _sess.get("user_id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    import requests as _rq
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    try:
        r = _rq.post(_bridge_base_url() + "/login-qr/start",
                     headers={"X-Bridge-Secret": secret}, timeout=15)
        return jsonify(r.json() if r.headers.get("content-type","").startswith("application/json")
                       else {"ok": r.status_code == 202}), r.status_code
    except Exception as exc:
        return jsonify({"ok": False, "error": f"bridge: {exc}"}), 502


@app.route("/api/zalo-bridge/qr-status", methods=["GET"])
def zalo_bridge_qr_status():
    """Proxy → bridge /login-qr/status. Khi success → tự restart bridge để dùng cookie mới."""
    from flask import session as _sess
    if not _sess.get("user_id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    import requests as _rq
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    try:
        r = _rq.get(_bridge_base_url() + "/login-qr/status",
                    headers={"X-Bridge-Secret": secret}, timeout=10)
        data = r.json()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"bridge: {exc}"}), 502
    # Login OK → restart bridge để boot bằng cookie mới vừa lưu
    if data.get("status") == "success":
        try:
            import subprocess as _sp
            _sp.run(["sudo", "/bin/systemctl", "restart",
                    os.environ.get("ZALO_BRIDGE_SERVICE", "zalo-bridge")],
                    capture_output=True, text=True, timeout=15)
            data["restarted"] = True
        except Exception as exc:
            data["restarted"] = False
            data["restart_error"] = str(exc)
    return jsonify(data)


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

    # Cửa sổ ngày cần sync — bắt cả NV khai bù cho các ngày trước (mặc định 7 ngày).
    _SYNC_WINDOW_DAYS = int(os.environ.get("POS_DAILY_SYNC_WINDOW_DAYS", "7"))

    while True:
        try:
            today = _dt.now(_VN_TZ).strftime("%Y-%m-%d")
            yesterday = (_dt.now(_VN_TZ) - _td(days=1)).strftime("%Y-%m-%d")
            # Bước 1: Kéo data từ Pancake API → JSON files cho N ngày gần nhất
            _window_dates = [
                (_dt.now(_VN_TZ) - _td(days=_i)).strftime("%Y-%m-%d")
                for _i in range(_SYNC_WINDOW_DAYS - 1, -1, -1)  # cũ → mới
            ]
            for _date in _window_dates:
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
            # Bước 3: Sync orders → orders table (chỉ hôm qua + hôm nay — đơn cũ không đổi)
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
