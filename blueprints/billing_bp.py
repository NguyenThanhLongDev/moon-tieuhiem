# -*- coding: utf-8 -*-
"""Thanh toán thuê bao phần mềm theo tháng qua SePay QR (chuyển từ ShipCOD — bản Moon, prefix MOON riêng).

Cơ chế: QR chỉ là ảnh <img> từ qr.sepay.vn (KHÔNG gọi API tạo QR).
Khách chuyển khoản đúng số tiền + nội dung TIEUHIEM1 → SePay bắn webhook về
/api/billing/sepay-webhook → server cộng hạn (idempotent theo sepay_tx_id).

1 gói duy nhất cho cả công ty (sếp chốt 08/09/2026): base 2tr/tháng +
mỗi tính năng phát sinh 500k. Hết hạn → ân hạn N ngày rồi khóa (gate ở web_app).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, Response, redirect, render_template_string,
                   request, session, url_for)

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__)

_ADMIN_ROLES = {"admin", "superadmin", "manager"}
_OWNER_USERNAME = "admin"    # super admin / chủ phần mềm (Phùng Tưởng) — CHỈ người này thấy cấu hình


def _is_owner() -> bool:
    return str(session.get("username", "")).strip().lower() == _OWNER_USERNAME
_CONTENT_PREFIX = "MOON"          # nội dung CK: MOON1 (id gói = 1) — PHẢI khác TIEUHIEM/SHIPCOD vì chung ví Tialua
_SUB_ID = 1
_CONTENT_RE = re.compile(r"MOON0*(\d{1,9})(?!\d)")


# ── Config (app_config) ────────────────────────────────────────────────
def _cfg():
    try:
        from app_ctx import load_config
        c = load_config() or {}
    except Exception:
        c = {}
    return {
        "bank_code": str(c.get("sepay_bank_code", "") or "").strip(),
        "bank_account": str(c.get("sepay_bank_account", "") or "").strip(),
        "account_name": str(c.get("sepay_account_name", "") or "").strip(),
        "webhook_apikey": str(c.get("sepay_webhook_apikey", "") or "").strip(),
    }


def _bank_short_code(raw: str) -> str:
    """Rút mã NH gọn: 'Ngân hàng Á Châu (ACB)' → ACB. qr.sepay.vn chỉ nhận mã ngắn."""
    s = str(raw or "").strip()
    m = re.search(r"\(([A-Za-z0-9]{2,12})\)\s*$", s)
    if m:
        return m.group(1).upper()
    if re.fullmatch(r"[A-Za-z0-9]{2,12}", s):
        return s.upper()
    m2 = re.search(r"[A-Z]{2,12}(?=[^A-Z]*$)", s)
    return m2.group(0) if m2 else s


def _qr_url(amount: int) -> str:
    from urllib.parse import quote
    c = _cfg()
    if not c["bank_account"] or not c["bank_code"]:
        return ""
    return (f"https://qr.sepay.vn/img?acc={quote(c['bank_account'])}"
            f"&bank={quote(_bank_short_code(c['bank_code']))}"
            f"&amount={int(amount)}&des={quote(_CONTENT_PREFIX + str(_SUB_ID))}")


# ── Subscription helpers ───────────────────────────────────────────────
def get_subscription() -> dict:
    from db import query_all
    r = query_all("SELECT id, base_fee, addon_fee, addon_count, grace_days, "
                  "expires_at, note FROM billing_subscription WHERE id=%s", (_SUB_ID,))
    if not r:
        return {"base_fee": 2000000, "addon_fee": 500000, "addon_count": 0,
                "grace_days": 7, "expires_at": None, "monthly_fee": 2000000}
    row = r[0]
    monthly = int(row[1]) + int(row[3]) * int(row[2])
    return {"base_fee": int(row[1]), "addon_fee": int(row[2]),
            "addon_count": int(row[3]), "grace_days": int(row[4]),
            "expires_at": row[5], "note": row[6], "monthly_fee": monthly}


def billing_status() -> dict:
    """Trạng thái thuê bao để hiện banner + gate khóa. Fail-open nếu DB lỗi."""
    try:
        sub = get_subscription()
    except Exception:
        return {"ok": True, "state": "unknown", "days_left": None}
    exp = sub.get("expires_at")
    today = date.today()
    if not exp:
        return {"ok": True, "state": "chua_kich_hoat", "days_left": None,
                "expires_at": None, "monthly_fee": sub["monthly_fee"], "grace_days": sub["grace_days"]}
    days_left = (exp - today).days
    grace = sub["grace_days"]
    if days_left >= 0:
        state = "active" if days_left > 5 else "sap_het"
    elif days_left >= -grace:
        state = "an_han"          # đã hết hạn nhưng còn ân hạn
    else:
        state = "khoa"            # quá ân hạn → khóa
    return {"ok": state != "khoa", "state": state, "days_left": days_left,
            "expires_at": exp, "monthly_fee": sub["monthly_fee"], "grace_days": grace}


_DEFAULT_PACKAGES = [
    {"label": "1 tháng", "months": 1,  "price": 2000000,  "tag": ""},
    {"label": "6 tháng", "months": 6,  "price": 12000000, "tag": "Phổ biến"},
    {"label": "1 năm",   "months": 12, "price": 24000000, "tag": "Tiết kiệm nhất"},
]


def get_packages_cfg():
    """Danh sách gói cước — chủ phần mềm cấu hình (app_config billing_packages). Có thể
    đặt giá KHÁC bội số tháng (giảm giá gói dài) — webhook khớp theo GIÁ nên vẫn cộng đúng."""
    try:
        from app_ctx import load_config
        raw = (load_config() or {}).get("billing_packages")
        data = json.loads(raw) if isinstance(raw, str) else raw
        out = []
        for p in (data or []):
            try:
                out.append({"label": str(p["label"])[:40], "months": int(p["months"]),
                            "price": int(p["price"]), "tag": str(p.get("tag", ""))[:30]})
            except Exception:
                pass
        if out:
            return out
    except Exception:
        pass
    return [dict(p) for p in _DEFAULT_PACKAGES]


def _packages(monthly: int):
    """Gói khách mua (từ cấu hình chủ phần mềm). amount = giá gói."""
    return [{"months": p["months"], "label": p["label"], "amount": p["price"], "tag": p["tag"]}
            for p in get_packages_cfg()]


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    mo = m % 12 + 1
    from calendar import monthrange
    day = min(d.day, monthrange(y, mo)[1])
    return date(y, mo, day)


def _apply_payment(amount: int, sepay_tx_id: str, content: str, gateway: str, raw: dict) -> bool:
    """Cộng hạn IDEMPOTENT theo sepay_tx_id. Trả True nếu là giao dịch MỚI."""
    from db import get_conn
    sub = get_subscription()
    monthly = max(sub["monthly_fee"], 1)
    # Khớp GIÁ GÓI trước (hỗ trợ gói giảm giá); không khớp → chia theo phí/tháng
    months = next((p["months"] for p in get_packages_cfg() if int(p["price"]) == int(amount)), None)
    if months is None:
        months = int(amount) // monthly
    with get_conn() as conn:
        cur = conn.cursor()
        # Chống trùng: INSERT trước, trùng txId → 0 dòng → dừng
        cur.execute(
            "INSERT INTO billing_payments (amount, content_code, sepay_tx_id, status, gateway, raw) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (sepay_tx_id) DO NOTHING RETURNING id",
            (amount, content[:200], sepay_tx_id,
             ("completed" if months >= 1 else "underpaid"),
             gateway[:40], json.dumps(raw, ensure_ascii=False)),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return False                       # trùng → không cộng lại
        if months >= 1:
            cur.execute("SELECT expires_at FROM billing_subscription WHERE id=%s", (_SUB_ID,))
            cur_exp = cur.fetchone()[0]
            base = max(cur_exp, date.today()) if cur_exp else date.today()
            new_exp = _add_months(base, months)
            cur.execute("UPDATE billing_subscription SET expires_at=%s, updated_at=NOW() WHERE id=%s",
                        (new_exp, _SUB_ID))
            cur.execute("UPDATE billing_payments SET months_added=%s, new_expires=%s WHERE id=%s",
                        (months, new_exp, row[0]))
        conn.commit()
    return True


# ── (B) Webhook nhận tiền — PUBLIC, xác thực Apikey, fail-closed ────────
@billing_bp.route("/api/billing/sepay-webhook", methods=["POST"])
def sepay_webhook():
    apikey = _cfg()["webhook_apikey"]
    if not apikey:
        return {"error": "chưa cấu hình"}, 503          # fail-closed
    got = re.sub(r"^Apikey\s+", "", request.headers.get("Authorization", ""), flags=re.I).strip()
    if got != apikey:
        return {"error": "unauthorized"}, 401
    try:
        b = request.get_json(silent=True) or {}
        ttype = b.get("transferType") or b.get("transfer_type")
        if ttype and ttype != "in":
            return {"ok": True, "skipped": "not_in"}
        amount = int(float(b.get("transferAmount") or b.get("transfer_amount") or 0))
        content = str(b.get("content") or b.get("description") or "")
        tx_id = str(b.get("id") or b.get("referenceCode") or b.get("reference_number") or "")
        gateway = str(b.get("gateway") or "")
        m = _CONTENT_RE.search(content.upper())
        if not m or not amount or not tx_id:
            return {"ok": True, "skipped": "no_match"}   # 200 để SePay không retry vô ích
        applied = _apply_payment(amount, tx_id, content, gateway, b)
        return {"ok": True, "applied": applied}
    except Exception:
        logger.exception("sepay_webhook lỗi")
        return {"error": "internal"}, 500


# ── (C) Poll trạng thái cho FE ─────────────────────────────────────────
@billing_bp.route("/api/billing/payment-status")
def payment_status():
    # since = epoch mili-giây lúc FE mở trang. Dùng to_timestamp (UTC) so với created_at
    # (timestamptz) — tránh lệch múi giờ khi so datetime naive như bản cũ.
    try:
        since_epoch = int(request.args.get("since", "")) / 1000.0
    except Exception:
        since_epoch = 0
    from db import query_all
    r = query_all("SELECT amount, new_expires FROM billing_payments "
                  "WHERE status='completed' AND created_at > to_timestamp(%s) "
                  "ORDER BY id DESC LIMIT 1", (since_epoch,))
    if not r:
        return {"paid": False}
    return {"paid": True, "amount": int(r[0][0]),
            "new_expires": r[0][1].isoformat() if r[0][1] else None}


# ── Trang gia hạn (hiện khi bị khóa / muốn thanh toán) ─────────────────
def _require_login(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("logged_in"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*a, **k)
    return w


def _pkg_qr_map(monthly):
    """Trả list gói kèm URL QR từng gói (mỗi gói 1 số tiền → webhook cộng đúng tháng)."""
    out = []
    for p in _packages(monthly):
        out.append({**p, "qr": _qr_url(p["amount"])})
    return out


@billing_bp.route("/gia-han")
@_require_login
def renew_page():
    sub = get_subscription()
    st = billing_status()
    c = _cfg()
    return render_template_string(
        _RENEW_TEMPLATE, sub=sub, st=st, cfg=c,
        packages=_pkg_qr_map(sub["monthly_fee"]),
        content_code=_CONTENT_PREFIX + str(_SUB_ID),
        configured=bool(c["bank_account"] and c["bank_code"]),
    )


# ── Trang MUA GÓI (khách xem) ──────────────────────────────────────────
@billing_bp.route("/billing")
@_require_login
def billing_page():
    sub = get_subscription()
    st = billing_status()
    c = _cfg()
    from db import query_all
    pays = query_all("SELECT amount, months_added, new_expires, created_at, status "
                     "FROM billing_payments WHERE status='completed' ORDER BY id DESC LIMIT 30")
    history = [{"amount": int(p[0]), "months": p[1], "new_expires": p[2],
                "created_at": p[3]} for p in pays]
    return render_template_string(
        _BILLING_TEMPLATE, sub=sub, st=st, history=history,
        packages=_pkg_qr_map(sub["monthly_fee"]),
        content_code=_CONTENT_PREFIX + str(_SUB_ID),
        configured=bool(c["bank_account"] and c["bank_code"]),
        is_owner=_is_owner(),
        bank=c["bank_code"], acc=c["bank_account"], name=c["account_name"],
    )


# ── Trang CẤU HÌNH (CHỈ super admin / chủ phần mềm) ────────────────────
@billing_bp.route("/billing/setup")
@_require_login
def billing_setup():
    if not _is_owner():
        return "Không có quyền", 403
    sub = get_subscription()
    st = billing_status()
    c = _cfg()
    from db import query_all
    pays = query_all("SELECT amount, content_code, sepay_tx_id, status, months_added, "
                     "new_expires, created_at, gateway FROM billing_payments ORDER BY id DESC LIMIT 50")
    history = [{"amount": int(p[0]), "content": p[1], "tx": p[2], "status": p[3],
                "months": p[4], "new_expires": p[5], "created_at": p[6], "gateway": p[7]} for p in pays]
    return render_template_string(
        _SETUP_TEMPLATE, sub=sub, st=st, cfg=c, history=history,
        qr=_qr_url(sub["monthly_fee"]), content_code=_CONTENT_PREFIX + str(_SUB_ID),
        configured=bool(c["bank_account"] and c["bank_code"]),
        packages=get_packages_cfg(),
    )


@billing_bp.route("/billing/config", methods=["POST"])
@_require_login
def billing_config_save():
    if not _is_owner():
        return "Không có quyền", 403
    from app_ctx import save_config_key
    from db import get_conn
    f = request.form
    for key, field in (("sepay_bank_code", "bank_code"), ("sepay_bank_account", "bank_account"),
                       ("sepay_account_name", "account_name")):
        if field in f:
            save_config_key(key, (f.get(field) or "").strip())
    # API key là BÍ MẬT: chỉ cập nhật khi admin gõ giá trị mới; để trống = giữ key cũ
    new_key = (f.get("webhook_apikey") or "").strip()
    if new_key:
        save_config_key("sepay_webhook_apikey", new_key)
    # tham số gói
    def _int(name, dflt):
        try:
            return int("".join(ch for ch in str(f.get(name, "")) if ch.isdigit()) or dflt)
        except Exception:
            return dflt
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE billing_subscription SET base_fee=%s, addon_fee=%s, "
                    "addon_count=%s, grace_days=%s, updated_at=NOW() WHERE id=%s",
                    (_int("base_fee", 2000000), _int("addon_fee", 500000),
                     _int("addon_count", 0), _int("grace_days", 7), _SUB_ID))
        conn.commit()
    # Gói cước (label/months/price/tag) — chủ phần mềm tự cấu hình
    pkgs = []
    labels = f.getlist("pkg_label"); months = f.getlist("pkg_months")
    prices = f.getlist("pkg_price"); tags = f.getlist("pkg_tag")
    for i, lb in enumerate(labels):
        lb = (lb or "").strip()
        mo = "".join(ch for ch in (months[i] if i < len(months) else "") if ch.isdigit())
        pr = "".join(ch for ch in (prices[i] if i < len(prices) else "") if ch.isdigit())
        if lb and mo and pr:
            pkgs.append({"label": lb[:40], "months": int(mo), "price": int(pr),
                         "tag": (tags[i].strip()[:30] if i < len(tags) else "")})
    if pkgs:
        save_config_key("billing_packages", json.dumps(pkgs, ensure_ascii=False))
    return redirect(url_for("billing.billing_setup"))


@billing_bp.route("/billing/extend-manual", methods=["POST"])
@_require_login
def billing_extend_manual():
    """Chủ phần mềm gia hạn tay (khi nhận tiền mặt / CK ngoài SePay)."""
    if not _is_owner():
        return "Không có quyền", 403
    try:
        months = max(1, min(int(request.form.get("months", 1)), 36))
    except Exception:
        months = 1
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT expires_at FROM billing_subscription WHERE id=%s", (_SUB_ID,))
        cur_exp = cur.fetchone()[0]
        base = max(cur_exp, date.today()) if cur_exp else date.today()
        new_exp = _add_months(base, months)
        cur.execute("UPDATE billing_subscription SET expires_at=%s, updated_at=NOW() WHERE id=%s",
                    (new_exp, _SUB_ID))
        # ghi 1 dòng lịch sử thủ công
        cur.execute("INSERT INTO billing_payments (amount, content_code, sepay_tx_id, status, "
                    "months_added, new_expires, gateway) VALUES (%s,%s,%s,'completed',%s,%s,'manual') "
                    "ON CONFLICT DO NOTHING",
                    (get_subscription()["monthly_fee"] * months, "MANUAL",
                     "manual-" + datetime.now().strftime("%Y%m%d%H%M%S"), months, new_exp))
        conn.commit()
    return redirect(url_for("billing.billing_setup"))


@billing_bp.route("/billing/set-expires", methods=["POST"])
@_require_login
def billing_set_expires():
    """Chủ phần mềm sửa TAY ngày hết hạn: đặt ngày cụ thể / lùi hạn, hoặc đặt lại về chưa kích hoạt.
    KHÔNG ghi lịch sử thanh toán — đây là thao tác chỉnh sửa, không phải giao dịch."""
    if not _is_owner():
        return "Không có quyền", 403
    new_exp = None
    if not request.form.get("reset"):
        raw = (request.form.get("expires") or "").strip()
        if raw:
            try:
                new_exp = date.fromisoformat(raw)
            except Exception:
                new_exp = None
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE billing_subscription SET expires_at=%s, updated_at=NOW() WHERE id=%s",
                    (new_exp, _SUB_ID))
        conn.commit()
    return redirect(url_for("billing.billing_setup"))


def register_billing_module(app):
    app.register_blueprint(billing_bp)
    logger.info("billing module registered")



# ═══════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════

# Bộ chọn gói + QR (dùng chung renew/billing). JS đổi QR theo gói được chọn.
_PKG_PICKER = """
{% if configured %}
<div class="pkg-row">
  {% for p in packages %}
  <button type="button" class="pkg" data-qr="{{ p.qr }}" data-amount="{{ p.amount }}"
          data-label="{{ p.label }}" onclick="ccPickPkg(this)">
    {% if p.tag %}<span class="pkg-tag">{{ p.tag }}</span>{% endif %}
    <div class="pkg-name">{{ p.label }}</div>
    <div class="pkg-price">{{ "{:,.0f}".format(p.amount) }}đ</div>
  </button>
  {% endfor %}
</div>
<div class="qr-wrap">
  <img id="ccQrImg" src="{{ packages[0].qr }}" alt="QR chuyển khoản">
  <div class="qr-info">
    <div>Ngân hàng: <b>{{ cfg_bank }}</b></div>
    <div>Số TK: <b>{{ cfg_acc }}</b>{% if cfg_name %} — {{ cfg_name }}{% endif %}</div>
    <div>Số tiền: <b id="ccQrAmt" style="color:#d97706">{{ "{:,.0f}".format(packages[0].amount) }}đ</b>
         · Gói <b id="ccQrLbl">{{ packages[0].label }}</b></div>
    <div>Nội dung: <b style="color:#dc2626">{{ content_code }}</b> <span style="opacity:.6">(giữ nguyên)</span></div>
  </div>
</div>
<div id="ccPaidBox" style="display:none" class="paid-box">✅ Đã nhận thanh toán! Đang cập nhật...</div>
{% else %}
<div class="notcfg">⚠ Kênh thanh toán chưa sẵn sàng. Vui lòng liên hệ nhà cung cấp phần mềm.</div>
{% endif %}
"""

_PKG_JS = """
<script>
function ccPickPkg(el){
  document.querySelectorAll('.pkg').forEach(function(b){ b.classList.remove('on'); });
  el.classList.add('on');
  var img=document.getElementById('ccQrImg'); if(img) img.src=el.dataset.qr;
  var a=document.getElementById('ccQrAmt'); if(a) a.textContent=Number(el.dataset.amount).toLocaleString('vi-VN')+'đ';
  var l=document.getElementById('ccQrLbl'); if(l) l.textContent=el.dataset.label;
}
(function(){ var f=document.querySelector('.pkg'); if(f) f.classList.add('on');
  var since=Date.now();
  setInterval(function(){
    fetch('/api/billing/payment-status?since='+since).then(function(r){return r.json();}).then(function(d){
      if(d.paid){ var b=document.getElementById('ccPaidBox'); if(b) b.style.display='block';
        setTimeout(function(){ location.reload(); }, 1800); }
    }).catch(function(){});
  }, 3000);
})();
</script>
"""

# Style dùng chung cho khối gói + QR
_PKG_CSS = """
.pkg-row{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin:6px 0 18px}
.pkg{position:relative;flex:1 1 140px;max-width:200px;background:var(--pk-bg,#fff);border:2px solid var(--pk-bd,#e5e7eb);
  border-radius:14px;padding:16px 14px;cursor:pointer;text-align:center;transition:.15s}
.pkg:hover{border-color:#F59E0B}
.pkg.on{border-color:#F59E0B;background:var(--pk-on,#fff7ed)}
.pkg-name{font-weight:700;font-size:15px;color:var(--pk-tx,#1a1a1a)}
.pkg-price{color:#d97706;font-weight:800;font-size:18px;margin-top:4px}
.pkg-tag{position:absolute;top:-9px;left:50%;transform:translateX(-50%);background:#F59E0B;color:#1a1a1a;
  font-size:10.5px;font-weight:700;padding:2px 9px;border-radius:99px;white-space:nowrap}
.qr-wrap{text-align:center}
.qr-wrap img{width:240px;height:240px;border-radius:14px;background:#fff;padding:8px;border:1px solid #eee}
.qr-info{margin-top:12px;font-size:14px;line-height:1.9;color:var(--pk-tx2,#555)}
.paid-box{margin-top:14px;background:rgba(29,158,117,.15);border:1px solid rgba(29,158,117,.5);
  color:#0f6e56;border-radius:12px;padding:14px;font-weight:700}
.notcfg{padding:20px;border:1px dashed #f0ad4e;border-radius:12px;color:#92600a;background:#fffbeb}
"""

# ── Trang GIA HẠN / bị khóa (nền tối, không nav) — khách ───────────────
_RENEW_TEMPLATE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gia hạn phần mềm — Tiểu Hiềm</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:linear-gradient(160deg,#1a1a1a,#2a2a2a);color:#f3f3f3;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#222;border:1px solid rgba(245,158,11,.3);border-top:3px solid #F59E0B;border-radius:20px;max-width:560px;width:100%;padding:30px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.badge-st{display:inline-block;padding:5px 14px;border-radius:99px;font-size:13px;font-weight:600;margin-bottom:14px}
a{color:#F59E0B}
:root{--pk-bg:#2a2a2a;--pk-bd:rgba(255,255,255,.14);--pk-on:#3a2f1a;--pk-tx:#f3f3f3;--pk-tx2:#bbb}
""" + _PKG_CSS + """
</style></head><body><div class="card">
  {% if st.state == 'khoa' %}
    <div class="badge-st" style="background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.4)">⛔ Đã hết hạn sử dụng</div>
    <h2 style="margin:0 0 6px">Phần mềm tạm khóa</h2>
    <p style="color:#aaa;font-size:14.5px;margin:0 0 6px">Thuê bao đã hết hạn quá {{ st.grace_days }} ngày ân hạn. Chọn gói và thanh toán để mở lại.</p>
  {% elif st.state == 'an_han' %}
    <div class="badge-st" style="background:rgba(245,158,11,.15);color:#fbbf24;border:1px solid rgba(245,158,11,.4)">⏳ Đang trong thời gian ân hạn</div>
    <h2 style="margin:0 0 6px">Sắp khóa — hãy gia hạn</h2>
    <p style="color:#aaa;font-size:14.5px;margin:0 0 6px">Đã hết hạn {{ -st.days_left }} ngày, còn {{ st.grace_days + st.days_left }} ngày ân hạn.</p>
  {% else %}
    <div class="badge-st" style="background:rgba(29,158,117,.15);color:#5dcaa5;border:1px solid rgba(29,158,117,.4)">Gia hạn thuê bao</div>
    <h2 style="margin:0 0 6px">Chọn gói &amp; thanh toán</h2>
  {% endif %}
  {% set cfg_bank=cfg.bank_code %}{% set cfg_acc=cfg.bank_account %}{% set cfg_name=cfg.account_name %}
  """ + _PKG_PICKER + """
  <div style="margin-top:16px;font-size:13px;color:#888">Sau khi chuyển ~10 giây hệ thống tự nhận. · <a href="/logout">Đăng xuất</a></div>
</div>""" + _PKG_JS + """</body></html>"""

# ── Trang MUA GÓI trong app (khách) ────────────────────────────────────
_BILLING_TEMPLATE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Thuê bao phần mềm — Tiểu Hiềm</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>body{background:#f5f6fa;font-family:'Segoe UI',sans-serif;font-size:14px}
.topbar{background:#1a1a1a;padding:10px 20px;color:#fff}.topbar a{color:#F59E0B;text-decoration:none;margin-right:16px}
.wrap{max-width:760px;margin:18px auto;padding:0 14px}
.cc-card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:20px 22px;margin-bottom:16px}
.cc-card h5{border-bottom:2px solid #F59E0B;padding-bottom:8px}
""" + _PKG_CSS + """
</style></head><body>
<div class="topbar"><a href="/">← Trang chủ</a><b style="color:#F59E0B">Thuê bao phần mềm</b></div>
<div class="wrap">
<div class="cc-card">
  {% set stc={'active':('#dcfce7','#166534','Đang hoạt động'),'sap_het':('#fef3c7','#92600a','Sắp hết hạn'),'an_han':('#fee2e2','#b91c1c','Đang ân hạn'),'khoa':('#fee2e2','#b91c1c','Đã khóa'),'chua_kich_hoat':('#f1f5f9','#475569','Chưa kích hoạt')} %}
  {% set sc=stc.get(st.state,('#f1f5f9','#475569','?')) %}
  <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <span style="background:{{sc[0]}};color:{{sc[1]}};font-weight:700;padding:6px 16px;border-radius:99px;font-size:14px">{{ sc[2] }}</span>
    <div style="font-size:15px">Hết hạn: <b>{{ sub.expires_at.strftime('%d/%m/%Y') if sub.expires_at else '— chưa kích hoạt —' }}</b>
      {% if st.days_left is not none %}{% if st.days_left>=0 %}<span style="color:#16a34a">(còn {{ st.days_left }} ngày)</span>{% else %}<span style="color:#dc2626">(quá hạn {{ -st.days_left }} ngày)</span>{% endif %}{% endif %}
    </div>
    {% if is_owner %}<a href="/billing/setup" class="ms-auto" style="font-size:13px;color:#888;text-decoration:none"><i class="bi bi-gear"></i> Cấu hình (chủ phần mềm)</a>{% endif %}
  </div>
</div>
<div class="cc-card">
  {% set _active = st.state in ('active','sap_het') %}
  {% if _active %}
  <div style="text-align:center;padding:8px 0 4px">
    <div style="font-size:44px;color:#16a34a;line-height:1"><i class="bi bi-check-circle-fill"></i></div>
    <h4 style="margin:10px 0 4px;color:#166534">Phần mềm đang hoạt động</h4>
    <div style="color:#555">Hết hạn <b>{{ sub.expires_at.strftime('%d/%m/%Y') }}</b> · còn <b>{{ st.days_left }}</b> ngày</div>
    <button type="button" class="btn btn-outline-warning btn-sm mt-3" onclick="var e=document.getElementById('renewBox');e.style.display=e.style.display==='none'?'block':'none';">
      <i class="bi bi-arrow-repeat"></i> Gia hạn / mua thêm gói
    </button>
  </div>
  <div id="renewBox" style="display:none;margin-top:16px;border-top:1px solid #f1f1f1;padding-top:16px">
  {% else %}
  <h5 style="text-align:center;border:none">Chọn gói thuê bao</h5>
  <div>
  {% endif %}
  {% set cfg_bank=None %}{% set cfg_acc=None %}{% set cfg_name=None %}
  """ + _PKG_PICKER.replace("{{ cfg_bank }}", "{{ bank }}").replace("{{ cfg_acc }}", "{{ acc }}").replace("{% if cfg_name %} — {{ cfg_name }}{% endif %}", "{% if name %} — {{ name }}{% endif %}") + """
  </div>
</div>
{% if history %}
<div class="cc-card">
  <h5>🧾 Lịch sử thanh toán của bạn</h5>
  <table class="table table-sm"><tr class="text-muted"><th>Ngày</th><th>Số tiền</th><th>Số tháng</th><th>Gia hạn tới</th></tr>
  {% for h in history %}<tr><td>{{ h.created_at.strftime('%d/%m/%Y %H:%M') if h.created_at else '' }}</td>
    <td><b>{{ "{:,.0f}".format(h.amount) }}đ</b></td><td>{{ h.months or '' }}</td>
    <td>{{ h.new_expires.strftime('%d/%m/%Y') if h.new_expires else '' }}</td></tr>{% endfor %}</table>
</div>
{% endif %}
</div>""" + _PKG_JS + """</body></html>"""

# ── Trang CẤU HÌNH (chủ phần mềm / super admin) ────────────────────────
_SETUP_TEMPLATE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cấu hình thuê bao — chủ phần mềm</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>body{background:#f5f6fa;font-family:'Segoe UI',sans-serif;font-size:14px}
.topbar{background:#1a1a1a;padding:10px 20px;color:#fff}.topbar a{color:#F59E0B;text-decoration:none;margin-right:16px}
.wrap{max-width:960px;margin:18px auto;padding:0 14px}
.cc-card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:18px 20px;margin-bottom:16px}
.cc-card h5{border-bottom:2px solid #F59E0B;padding-bottom:8px}
label.fl{font-size:12px;color:#666;font-weight:600;display:block;margin-bottom:3px}</style></head><body>
<div class="topbar"><a href="/">← Trang chủ</a><a href="/billing">Trang mua gói</a><b style="color:#F59E0B">Cấu hình (chủ phần mềm)</b></div>
<div class="wrap">
<div class="cc-card">
  <h5>📅 Trạng thái thuê bao</h5>
  <div class="row g-3 align-items-center">
    <div class="col-md-8">
      {% set stc={'active':('success','Đang hoạt động'),'sap_het':('warning','Sắp hết hạn'),'an_han':('warning','Đang ân hạn'),'khoa':('danger','ĐÃ KHÓA'),'chua_kich_hoat':('secondary','Chưa kích hoạt')} %}
      <span class="badge bg-{{ stc.get(st.state,('secondary',''))[0] }}" style="font-size:14px">{{ stc.get(st.state,('','?'))[1] }}</span>
      <div class="mt-2" style="font-size:15px">Hết hạn: <b>{{ sub.expires_at.strftime('%d/%m/%Y') if sub.expires_at else '— chưa kích hoạt —' }}</b>
        {% if st.days_left is not none %}{% if st.days_left>=0 %}<span class="text-success">(còn {{ st.days_left }} ngày)</span>{% else %}<span class="text-danger">(quá hạn {{ -st.days_left }} ngày)</span>{% endif %}{% endif %}</div>
      <div class="mt-1">Phí/tháng: <b style="color:#b45309">{{ "{:,.0f}".format(sub.monthly_fee) }}đ</b>
        <span class="text-muted">= {{ "{:,.0f}".format(sub.base_fee) }} cơ bản{% if sub.addon_count %} + {{ sub.addon_count }} × {{ "{:,.0f}".format(sub.addon_fee) }}{% endif %}</span></div>
    </div>
    <div class="col-md-4">
      <form method="post" action="/billing/extend-manual" class="d-flex gap-2 align-items-end justify-content-md-end">
        <div><label class="fl">Gia hạn tay (tháng)</label>
          <input name="months" type="number" value="1" min="1" max="36" class="form-control form-control-sm" style="width:90px"></div>
        <button class="btn btn-sm btn-outline-secondary">Cộng hạn</button>
      </form>
      <div class="text-muted mt-1" style="font-size:11px">Khi thu tiền mặt/CK ngoài SePay</div>
    </div>
  </div>
  <hr style="margin:6px 0 14px">
  <div class="d-flex gap-3 flex-wrap align-items-end">
    <form method="post" action="/billing/set-expires" class="d-flex gap-2 align-items-end">
      <div><label class="fl">Sửa ngày hết hạn (đặt tay / lùi hạn)</label>
        <input name="expires" type="date" value="{{ sub.expires_at.isoformat() if sub.expires_at else '' }}" class="form-control form-control-sm" style="width:170px"></div>
      <button class="btn btn-sm btn-outline-primary">Đặt hạn</button>
    </form>
    <form method="post" action="/billing/set-expires" onsubmit="return confirm('Đặt lại về CHƯA KÍCH HOẠT? Ngày hết hạn sẽ bị xoá, phần mềm coi như chưa mua gói.');">
      <input type="hidden" name="reset" value="1">
      <button class="btn btn-sm btn-outline-danger">↺ Đặt lại (chưa kích hoạt)</button>
    </form>
  </div>
  <div class="text-muted mt-1" style="font-size:11px">Sửa tay khi cộng nhầm / test — không ghi vào lịch sử thanh toán.</div>
</div>
<div class="cc-card">
  <h5>⚙️ Cấu hình SePay &amp; gói</h5>
  <form method="post" action="/billing/config">
    <div class="row g-2">
      <div class="col-md-3"><label class="fl">Mã ngân hàng</label><input name="bank_code" value="{{ cfg.bank_code }}" class="form-control form-control-sm"></div>
      <div class="col-md-3"><label class="fl">Số tài khoản nhận</label><input name="bank_account" value="{{ cfg.bank_account }}" class="form-control form-control-sm"></div>
      <div class="col-md-6"><label class="fl">Tên chủ tài khoản</label><input name="account_name" value="{{ cfg.account_name }}" class="form-control form-control-sm"></div>
      <div class="col-md-12"><label class="fl">SePay Webhook API Key (bí mật){% if cfg.webhook_apikey %} <span style="color:#16a34a">✓ đã lưu</span>{% endif %}</label>
        <input name="webhook_apikey" type="password" autocomplete="new-password" value="" class="form-control form-control-sm"
               placeholder="{% if cfg.webhook_apikey %}••••••• (để trống = giữ key cũ){% else %}dán key SePay account Tialua{% endif %}"></div>
    </div><hr>
    <div class="row g-2">
      <div class="col-md-3"><label class="fl">Gói cơ bản/tháng</label><input name="base_fee" value="{{ sub.base_fee }}" class="form-control form-control-sm"></div>
      <div class="col-md-3"><label class="fl">Số tính năng phát sinh</label><input name="addon_count" value="{{ sub.addon_count }}" class="form-control form-control-sm"></div>
      <div class="col-md-3"><label class="fl">Giá/tính năng</label><input name="addon_fee" value="{{ sub.addon_fee }}" class="form-control form-control-sm"></div>
      <div class="col-md-3"><label class="fl">Ân hạn (ngày)</label><input name="grace_days" value="{{ sub.grace_days }}" class="form-control form-control-sm"></div>
    </div>
    <hr>
    <div style="font-weight:700;color:#92600a;margin-bottom:6px">🎟️ Gói cước khách mua</div>
    <div class="text-muted mb-2" style="font-size:12px">Khách bấm gói nào → QR số tiền theo GIÁ gói đó. Webhook khớp theo giá → cộng đúng số tháng (đặt giá giảm cho gói dài vẫn đúng).</div>
    <div class="row g-2 fw-semibold text-muted" style="font-size:11.5px"><div class="col-4">Tên gói</div><div class="col-2">Số tháng</div><div class="col-3">Giá (đ)</div><div class="col-2">Nhãn (vd Phổ biến)</div><div class="col-1"></div></div>
    <div id="pkgRows">
    {% for p in packages %}
      <div class="row g-2 mb-1 pkg-cfg-row">
        <div class="col-4"><input name="pkg_label" value="{{ p.label }}" class="form-control form-control-sm" placeholder="1 tháng"></div>
        <div class="col-2"><input name="pkg_months" value="{{ p.months }}" class="form-control form-control-sm" placeholder="1"></div>
        <div class="col-3"><input name="pkg_price" value="{{ p.price }}" class="form-control form-control-sm" placeholder="2000000"></div>
        <div class="col-2"><input name="pkg_tag" value="{{ p.tag }}" class="form-control form-control-sm" placeholder=""></div>
        <div class="col-1"><button type="button" class="btn btn-sm btn-outline-danger" onclick="this.closest('.pkg-cfg-row').remove()">✕</button></div>
      </div>
    {% endfor %}
    </div>
    <button type="button" class="btn btn-sm btn-outline-secondary mt-1" onclick="addPkgRow()">+ Thêm gói</button>
    <div><button class="btn btn-warning btn-sm mt-3">💾 Lưu cấu hình</button></div>
  </form>
  <script>
  function addPkgRow(){var d=document.createElement('div');d.className='row g-2 mb-1 pkg-cfg-row';
    d.innerHTML='<div class="col-4"><input name="pkg_label" class="form-control form-control-sm" placeholder="tên gói"></div>'+
    '<div class="col-2"><input name="pkg_months" class="form-control form-control-sm" placeholder="tháng"></div>'+
    '<div class="col-3"><input name="pkg_price" class="form-control form-control-sm" placeholder="giá"></div>'+
    '<div class="col-2"><input name="pkg_tag" class="form-control form-control-sm"></div>'+
    '<div class="col-1"><button type="button" class="btn btn-sm btn-outline-danger" onclick="this.closest(\\'.pkg-cfg-row\\').remove()">✕</button></div>';
    document.getElementById('pkgRows').appendChild(d);}
  </script>
  <div class="text-muted mt-2" style="font-size:12px">Webhook URL khai bên SePay: <code>https://moon.tieuhiem.com/api/billing/sepay-webhook</code> · Nội dung CK khách: <code>{{ content_code }}</code></div>
</div>
<div class="cc-card">
  <h5>🧾 Lịch sử thanh toán (đầy đủ)</h5>
  {% if not history %}<div class="text-muted py-3 text-center">Chưa có giao dịch nào.</div>{% else %}
  <table class="table table-sm"><tr class="text-muted"><th>Ngày</th><th>Số tiền</th><th>Nội dung</th><th>Mã GD</th><th>Tháng</th><th>Hạn mới</th><th>Nguồn</th><th>TT</th></tr>
  {% for h in history %}<tr><td>{{ h.created_at.strftime('%d/%m/%Y %H:%M') if h.created_at else '' }}</td>
    <td><b>{{ "{:,.0f}".format(h.amount) }}đ</b></td><td>{{ h.content or '' }}</td>
    <td style="font-size:11px;color:#999">{{ h.tx }}</td><td>{{ h.months or '' }}</td>
    <td>{{ h.new_expires.strftime('%d/%m/%Y') if h.new_expires else '' }}</td><td>{{ h.gateway or '' }}</td>
    <td><span class="badge bg-{{ 'success' if h.status=='completed' else 'warning' }}">{{ h.status }}</span></td></tr>{% endfor %}</table>
  {% endif %}
</div>
</div></body></html>"""
