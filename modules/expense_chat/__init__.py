"""Chi phí công ty (kế toán) — riêng với chi phí QC FB Ads.

Workflow:
- Admin/accountant gửi tin (text/ảnh) vào Zalo group "Chi phí Cty" (map qua zalo_expense_thread_<tid>).
- Bridge route tin → Flask phân loại: nếu group là expense → vào module này.
- Text: DeepSeek parse {amount, category, note, date}.
- Image: Gemini Vision parse tương tự.
- Lan reply trong group + lưu DB.
- Trang /chi-phi/khai-bao: list + filter + Excel + zalo-mapping.
- Permission: chỉ accountant/admin/manager — NV không vào được.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from functools import wraps

logger = logging.getLogger(__name__)

expense_chat_bp = Blueprint(
    "expense_chat", __name__,
    url_prefix="/chi-phi",
    template_folder="templates",
)

_ALLOWED_ROLES = {"admin", "superadmin", "manager", "accountant", "ketoan", "it"}

CATEGORY_LABEL = {
    "ads":     "🎯 Quảng cáo",
    "salary":  "💰 Lương",
    "office":  "🏢 Văn phòng",
    "utility": "💡 Tiện ích",
    "other":   "📦 Khác",
}


def _login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return f(*a, **kw)
    return w


def _current_user() -> dict:
    uid = session.get("user_id")
    if not uid:
        return {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, username, full_name, role FROM users WHERE id=%s", (int(uid),))
                r = cur.fetchone()
                if not r:
                    return {}
                return {"id": int(r[0]), "username": r[1] or "",
                        "full_name": r[2] or r[1] or "", "role": (r[3] or "").lower()}
    except Exception:
        return {}


def _check_perm():
    user = _current_user()
    role = (user.get("role") or "").lower()
    if role not in _ALLOWED_ROLES:
        abort(403)
    return user


@expense_chat_bp.route("/")
@_login_required
def index():
    return redirect(url_for("expense_chat.khai_bao"))


@expense_chat_bp.route("/khai-bao")
@_login_required
def khai_bao():
    user = _check_perm()

    try:
        from tz_utils import now_hcm
        today = now_hcm().date()
    except Exception:
        today = date.today()

    df = request.args.get("date_from") or (today - timedelta(days=30)).isoformat()
    dt = request.args.get("date_to") or today.isoformat()
    cat = (request.args.get("category") or "").strip()
    src = (request.args.get("source") or "").strip()
    status = (request.args.get("status") or "").strip()

    try:
        date_from = date.fromisoformat(df)
        date_to = date.fromisoformat(dt)
    except Exception:
        date_from, date_to = today - timedelta(days=30), today

    from db import get_conn
    where = ["deleted_at IS NULL",
             "(occurred_date IS NULL OR (occurred_date >= %s AND occurred_date <= %s))"]
    params: list = [date_from, date_to]
    if cat:
        where.append("category = %s"); params.append(cat)
    if src:
        where.append("source = %s"); params.append(src)
    if status:
        where.append("status = %s"); params.append(status)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, occurred_date, amount_vnd, category, note,
                       source, source_url, confidence, raw_body,
                       zalo_thread_id, zalo_sender_name, user_id,
                       status, created_at, is_private
                  FROM company_expense_items
                 WHERE {' AND '.join(where)}
                 ORDER BY occurred_date DESC NULLS LAST, id DESC
                 LIMIT 500
            """, params)
            rows = cur.fetchall()
            items = [{
                "id": r[0], "occurred_date": r[1], "amount_vnd": int(r[2] or 0),
                "category": r[3], "note": r[4] or "", "source": r[5],
                "source_url": r[6], "confidence": r[7],
                "raw_body": r[8] or "", "zalo_thread_id": r[9],
                "zalo_sender_name": r[10] or "", "user_id": r[11],
                "status": r[12], "created_at": r[13],
                "is_private": bool(r[14]),
            } for r in rows]

            # Tổng theo category
            cur.execute(f"""
                SELECT category, COUNT(*), COALESCE(SUM(amount_vnd),0)
                  FROM company_expense_items
                 WHERE {' AND '.join(where)}
                 GROUP BY category
                 ORDER BY SUM(amount_vnd) DESC
            """, params)
            by_cat = [{"cat": r[0], "count": int(r[1]), "total": int(r[2] or 0)} for r in cur.fetchall()]

    grand_total = sum(b["total"] for b in by_cat)
    pending_count = sum(1 for it in items if it["status"] == "pending")

    return render_template(
        "expense_chat/khai_bao.html",
        items=items, by_cat=by_cat,
        grand_total=grand_total, pending_count=pending_count,
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        cat_filter=cat, src_filter=src, status_filter=status,
        category_label=CATEGORY_LABEL,
        current_user=user,
    )


@expense_chat_bp.route("/items/<int:item_id>/confirm", methods=["POST"])
@_login_required
def confirm_item(item_id: int):
    user = _check_perm()
    from db import get_conn
    new_status = (request.form.get("status") or "confirmed").strip()
    new_amount = request.form.get("amount_vnd")
    new_cat = (request.form.get("category") or "").strip()
    new_note = request.form.get("note")
    new_date = (request.form.get("occurred_date") or "").strip() or None
    item_info = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            sets = ["status = %s", "confirmed_by = %s", "confirmed_at = NOW()", "updated_at = NOW()"]
            params: list = [new_status, int(user["id"])]
            if new_amount:
                sets.append("amount_vnd = %s"); params.append(int(new_amount))
            if new_cat:
                sets.append("category = %s"); params.append(new_cat)
            if new_note is not None:
                sets.append("note = %s"); params.append(new_note)
            if new_date:
                sets.append("occurred_date = %s"); params.append(new_date)
            params.append(item_id)
            cur.execute(f"UPDATE company_expense_items SET {', '.join(sets)} WHERE id = %s", params)
            # Đọc lại item để forward thông báo
            cur.execute("""
                SELECT amount_vnd, category, note, zalo_sender_id, zalo_sender_name,
                       is_private, zalo_thread_id
                  FROM company_expense_items WHERE id=%s
            """, (item_id,))
            r = cur.fetchone()
            if r:
                item_info = {"amount": int(r[0] or 0), "category": r[1],
                             "note": r[2] or "", "sender_uid": r[3] or "",
                             "sender_name": r[4] or "",
                             "is_private": bool(r[5]),
                             "source_thread_id": r[6] or ""}
            conn.commit()

    # Thông báo vào group Inbox: sếp/kế toán đã duyệt/từ chối
    if item_info:
        try:
            _notify_admin_decision(user, item_id, new_status, item_info)
        except Exception as exc:
            logger.info("notify decision skip: %s", exc)

    flash("Đã cập nhật", "success")
    return redirect(url_for("expense_chat.khai_bao"))


def _notify_admin_decision(user: dict, item_id: int, new_status: str, item: dict):
    """Khi admin bấm ✓/✗ trên web → Lan thông báo vào group Inbox tag NV gửi.

    Skip nếu category='ads' — nhóm Inbox CTY không hiển thị khoản QC.
    """
    # Item báo riêng (1-1) → notify vào nhóm chi phí riêng (test group)
    if item.get("is_private"):
        inbox_tid = _get_private_inbox_thread_id()
    else:
        inbox_tid = _get_inbox_thread_id()
    if not inbox_tid:
        return
    if (item.get("category") or "").lower() == "ads":
        return
    admin_name = user.get("full_name") or user.get("username") or "Admin"
    cat_lbl = CATEGORY_LABEL.get(item["category"], "📦 Khác")
    amount_str = _fmt_money(item["amount"])
    note = item["note"][:120]

    if new_status == "confirmed":
        head = f"✅ {admin_name} đã DUYỆT CHI"
        verb = "duyệt chi"
    elif new_status == "rejected":
        head = f"❌ {admin_name} TỪ CHỐI"
        verb = "từ chối"
    else:
        head = f"📝 {admin_name} cập nhật trạng thái: {new_status}"
        verb = new_status

    lines = [
        head,
        f"💰 Tiền: {amount_str}",
        f"🏷 Loại: {cat_lbl}",
    ]
    if note:
        lines.append(f"📝 Nội dung: {note}")
    if item.get("sender_name"):
        lines.append(f"👤 NV báo: {item['sender_name']}")
    lines.append(f"🔗 #{item_id}")
    text = "\n".join(lines)

    # Tag NV được duyệt (nếu có zalo_uid) để họ nhận noti
    if item.get("sender_uid") and item.get("sender_name"):
        tag = f"@{item['sender_name']}"
        new_text = text + f"\n\n{tag} nhé!"
        pos = sum(2 if ord(c) > 0xFFFF else 1 for c in text + "\n\n")
        utf16_tag = sum(2 if ord(c) > 0xFFFF else 1 for c in tag)
        mentions_payload = [{"pos": pos, "uid": item["sender_uid"], "len": utf16_tag}]
        _push_to_zalo_raw(inbox_tid, new_text, mentions=mentions_payload)
    else:
        new_text = text
        mentions_payload = []
        _push_to_zalo_raw(inbox_tid, text)

    # Push thêm về SOURCE thread (nơi NV gửi yêu cầu) để họ thấy kết quả ngay
    # tại nhóm/chat họ báo, không phải tìm trên inbox CTY.
    src_tid = (item.get("source_thread_id") or "").strip()
    if src_tid and src_tid != inbox_tid:
        try:
            src_kwargs = {}
            # Nếu tin gốc là chat 1-1 (is_private=True) → push type=user; group → group.
            if item.get("is_private"):
                src_kwargs["thread_type"] = "user"
            else:
                src_kwargs["thread_type"] = "group"
            if mentions_payload:
                _push_to_zalo_raw(src_tid, new_text, mentions=mentions_payload, **src_kwargs)
            else:
                _push_to_zalo_raw(src_tid, new_text, **src_kwargs)
        except Exception as exc:
            logger.info("notify decision src-thread push fail (tid=%s): %s", src_tid, exc)


@expense_chat_bp.route("/items/<int:item_id>/delete", methods=["POST"])
@_login_required
def delete_item(item_id: int):
    _check_perm()
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE company_expense_items SET deleted_at = NOW() WHERE id = %s", (item_id,))
            conn.commit()
    flash("Đã xoá", "info")
    return redirect(url_for("expense_chat.khai_bao"))


@expense_chat_bp.route("/export-excel")
@_login_required
def export_excel():
    _check_perm()
    df = request.args.get("date_from") or ""
    dt = request.args.get("date_to") or ""
    try:
        date_from = date.fromisoformat(df); date_to = date.fromisoformat(dt)
    except Exception:
        try:
            from tz_utils import now_hcm
            today = now_hcm().date()
        except Exception:
            today = date.today()
        date_from, date_to = today - timedelta(days=30), today

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT occurred_date, amount_vnd, category, note,
                       source, zalo_sender_name, status, created_at
                  FROM company_expense_items
                 WHERE deleted_at IS NULL
                   AND (occurred_date IS NULL OR (occurred_date BETWEEN %s AND %s))
                 ORDER BY occurred_date DESC NULLS LAST, id DESC
            """, (date_from, date_to))
            rows = cur.fetchall()

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    import io
    wb = Workbook(); ws = wb.active
    ws.title = "Chi phí Cty"

    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill("solid", fgColor="4F81BD")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    right_align = Alignment(horizontal="right", vertical="center")
    thin = Side(border_style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws["A1"] = "Start date"; ws["B1"] = date_from.strftime("%d/%m/%Y")
    ws["A2"] = "End date";   ws["B2"] = date_to.strftime("%d/%m/%Y")
    grand = sum(int(r[1] or 0) for r in rows)
    ws["D1"] = "TỔNG CHI PHÍ"; ws["D1"].font = Font(bold=True)
    ws["E1"] = grand; ws["E1"].number_format = '#,##0'; ws["E1"].font = Font(bold=True, color="C00000", size=12)

    headers = ["#", "Ngày", "Số tiền", "Loại", "Ghi chú", "Nguồn", "Người gửi", "Trạng thái", "Tạo lúc"]
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center; c.border = border

    r = 5
    for idx, row in enumerate(rows, 1):
        occ, amt, cat, note, src, sender, st, created = row
        ws.cell(row=r, column=1, value=idx).alignment = center
        ws.cell(row=r, column=2, value=occ.strftime("%d/%m/%Y") if occ else "—").alignment = center
        c = ws.cell(row=r, column=3, value=int(amt or 0))
        c.number_format = '#,##0'; c.alignment = right_align; c.font = Font(bold=True)
        ws.cell(row=r, column=4, value=CATEGORY_LABEL.get(cat, cat))
        ws.cell(row=r, column=5, value=note or "")
        ws.cell(row=r, column=6, value="🖼 Ảnh" if src == "image" else "📝 Chữ").alignment = center
        ws.cell(row=r, column=7, value=sender or "")
        ws.cell(row=r, column=8, value=st).alignment = center
        ws.cell(row=r, column=9, value=created.strftime("%d/%m %H:%M") if created else "")
        for ci in range(1, 10):
            ws.cell(row=r, column=ci).border = border
        r += 1

    widths = {1: 5, 2: 12, 3: 14, 4: 16, 5: 36, 6: 9, 7: 18, 8: 11, 9: 13}
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = "A5"

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    from flask import send_file
    return send_file(
        buf, as_attachment=True,
        download_name=f"chi_phi_cty_{date_from.isoformat()}_{date_to.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@expense_chat_bp.route("/zalo-mapping", methods=["GET", "POST"])
@_login_required
def zalo_mapping():
    user = _current_user()
    role = (user.get("role") or "").lower()
    # Cấu hình bridge — admin/IT only, KHÔNG cho kế toán đụng vào
    if role not in {"admin", "superadmin", "manager", "it"}:
        abort(403)
    from app_ctx import load_config, save_config_key
    from db import get_conn

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        tid = (request.form.get("thread_id") or "").strip()
        if action == "set_inbox":
            save_config_key("zalo_expense_inbox", tid)
            flash(f"Đã chọn group inbox: {tid[:14]}…" if tid else "Đã gỡ inbox", "success")
            return redirect(url_for("expense_chat.zalo_mapping"))
        if tid:
            key = f"zalo_expense_thread_{tid}"
            if action == "remove":
                try:
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM app_config WHERE key=%s", (key,))
                            conn.commit()
                    flash("Đã gỡ map", "info")
                except Exception as e:
                    flash(f"Lỗi: {e}", "danger")
            else:
                label = (request.form.get("label") or "Chi phí Cty").strip()
                save_config_key(key, label)
                flash(f"Đã map {tid[:14]}… → {label}", "success")
        return redirect(url_for("expense_chat.zalo_mapping"))

    cfg = load_config()
    threads = []
    for k, v in cfg.items():
        if isinstance(k, str) and k.startswith("zalo_expense_thread_"):
            threads.append({"thread_id": k[len("zalo_expense_thread_"):], "label": str(v)})

    # Auto-scan groups (loại trừ những đã map ở budget_chat hoặc expense)
    auto_groups = []
    try:
        import requests as _rq
        secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
        if secret:
            r = _rq.get("http://127.0.0.1:5051/list-groups",
                        headers={"X-Bridge-Secret": secret}, timeout=8)
            if r.status_code == 200:
                gs = r.json().get("groups", [])
                budget_tids = {k[len("zalo_thread_"):] for k in cfg if k.startswith("zalo_thread_")}
                expense_tids = {t["thread_id"] for t in threads}
                for g in gs:
                    if g["thread_id"] in budget_tids or g["thread_id"] in expense_tids:
                        continue
                    auto_groups.append(g)
    except Exception as exc:
        logger.info("expense zalo-mapping autoscan skip: %s", exc)

    inbox_tid = str(cfg.get("zalo_expense_inbox") or "").strip()

    # Tất cả group có Lan + group đã map budget để pick inbox từ
    all_groups = list(auto_groups)
    # Thêm group đã map (budget hoặc expense) vào danh sách pick được
    try:
        import requests as _rq
        secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
        if secret:
            r = _rq.get("http://127.0.0.1:5051/list-groups",
                        headers={"X-Bridge-Secret": secret}, timeout=8)
            if r.status_code == 200:
                gs = r.json().get("groups", [])
                # Dedup
                seen = {g["thread_id"] for g in all_groups}
                for g in gs:
                    if g["thread_id"] not in seen:
                        all_groups.append(g)
    except Exception:
        pass

    return render_template(
        "expense_chat/zalo_mapping.html",
        threads=threads, auto_groups=auto_groups,
        all_groups=all_groups, inbox_tid=inbox_tid,
        current_user=_current_user(),
    )


# ──────────────────────────────────────────────────────────────────
# Helpers cho bridge gọi vào — text + image
# ──────────────────────────────────────────────────────────────────

def _push_to_zalo(thread_id: str, text: str, mention_uid: str = "", mention_name: str = "",
                  thread_type: str = "group"):
    """thread_type='user' khi gửi 1-1; mention bị Zalo bỏ qua trong 1-1 nhưng giữ tag text."""
    import requests as _rq, time as _t
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        return
    payload = {"thread_id": thread_id, "text": text, "thread_type": thread_type}
    # 1-1: không cần mention native (Zalo không hỗ trợ mention trong user chat)
    if thread_type == "group" and mention_uid and mention_name:
        tag = f"@{mention_name}"
        payload["text"] = tag + " " + text
        utf16 = sum(2 if ord(c) > 0xFFFF else 1 for c in tag)
        payload["mentions"] = [{"pos": 0, "uid": mention_uid, "len": utf16}]
    url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
    for attempt in range(3):
        try:
            r = _rq.post(url, json=payload, headers={"X-Bridge-Secret": secret}, timeout=5)
            if r.status_code == 200 and r.text.find('"ok":true') >= 0:
                return
            logger.info("push retry %d: %s", attempt, r.text[:120])
        except Exception as exc:
            logger.info("push exc retry %d: %s", attempt, exc)
        _t.sleep(1.5 * (attempt + 1))


def _get_inbox_thread_id() -> Optional[str]:
    """Group "Chi phí Cty Inbox" — Lan forward mọi chi phí về đây cho sếp xem.

    Config qua app_config['zalo_expense_inbox'] = <thread_id>.
    """
    try:
        from app_ctx import load_config
        v = (load_config() or {}).get("zalo_expense_inbox")
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None
    except Exception:
        return None


def _get_private_inbox_thread_id() -> Optional[str]:
    """Group "nhóm ghi chép chi phí riêng" — Lan forward chi phí NV báo qua 1-1 vào đây.

    Mặc định: 638544760243854625 (nhóm test ban đầu, xác nhận 2026-05-18).
    Config qua app_config['zalo_expense_private_inbox'] = <thread_id> để override.
    """
    default_tid = "638544760243854625"
    try:
        from app_ctx import load_config
        v = (load_config() or {}).get("zalo_expense_private_inbox")
        if v is None:
            return default_tid
        s = str(v).strip()
        return s if s else default_tid
    except Exception:
        return default_tid


def _forward_to_inbox(amount: int, category: str, note: str,
                     occurred_date, sender_uid: str, sender_name: str,
                     source: str, source_url: str = "",
                     source_thread_id: str = "",
                     item_id: Optional[int] = None,
                     is_private: bool = False) -> None:
    """Gửi summary chi phí sang group inbox. No-op nếu chưa setup.

    Format:
      💵 Chi phí mới ghi nhận
      👤 NV: @<tên>
      💰 Tiền: 200.000đ
      🏷 Loại: 🏢 Văn phòng
      📝 Nội dung: Mua giấy in
      📅 Ngày: 18/05/2026
      📌 Nguồn: 📝 Chữ (KD Nam - Đà Nẵng)
    """
    # Tin báo riêng (1-1) → forward sang nhóm ghi chép chi phí RIÊNG (test group),
    # không lẫn vào nhóm Chi phí CTY chính.
    if is_private:
        inbox_tid = _get_private_inbox_thread_id()
    else:
        inbox_tid = _get_inbox_thread_id()
    if not inbox_tid:
        return
    if inbox_tid == source_thread_id:
        # Tin đã ở chính inbox, không cần forward lại
        return
    # KHÔNG forward khoản category='ads' về Inbox CTY — chỉ chi phí thuần
    # (lương, VPP, điện nước, dịch vụ ngoài...) mới vào nhóm này.
    # Kế toán xem item ads trên web /chi-phi/khai-bao nếu cần.
    if (category or "").lower() == "ads":
        logger.info("forward_to_inbox skip ads item #%s (amount=%s)", item_id, amount)
        return

    cat_lbl = CATEGORY_LABEL.get(category, "📦 Khác")
    src_lbl = "🖼 Ảnh" if source == "image" else "📝 Chữ"

    # Lấy tên group nguồn từ config (label đã set khi map)
    src_group_name = ""
    try:
        from app_ctx import load_config
        cfg = load_config() or {}
        # Tìm thread_id trong map (budget hay expense)
        src_group_name = (cfg.get(f"zalo_thread_{source_thread_id}") or "").replace("team-", "Team ").title()
    except Exception:
        pass

    header = "🔒 Chi phí RIÊNG (NV báo qua 1-1)" if is_private else "💵 Chi phí mới ghi nhận"
    lines = [header]
    if sender_name:
        lines.append(f"👤 NV: {sender_name}")  # mention sẽ chèn @ ở dưới
    lines.append(f"💰 Tiền: {_fmt_money(amount)}")
    lines.append(f"🏷 Loại: {cat_lbl}")
    if note:
        lines.append(f"📝 Nội dung: {note}")
    if occurred_date:
        try:
            d_vn = occurred_date.strftime("%d/%m/%Y") if hasattr(occurred_date, "strftime") else str(occurred_date)
            lines.append(f"📅 Ngày: {d_vn}")
        except Exception:
            pass
    if is_private:
        src_str = "📌 Nguồn: 🔒 Chat riêng 1-1 với Lan"
    else:
        src_str = f"📌 Nguồn: {src_lbl}"
        if src_group_name:
            src_str += f" · {src_group_name}"
    lines.append(src_str)
    if item_id:
        lines.append(f"🔗 #{item_id}")

    text = "\n".join(lines)

    # Tag NV: thay "NV: <name>" thành "NV: @<name>" + build mention
    if sender_uid and sender_name:
        prefix_before_tag = f"{header}\n👤 NV: "
        tag = f"@{sender_name}"
        new_text = text.replace(f"👤 NV: {sender_name}", f"👤 NV: {tag}", 1)
        pos = sum(2 if ord(c) > 0xFFFF else 1 for c in prefix_before_tag)
        utf16_tag = sum(2 if ord(c) > 0xFFFF else 1 for c in tag)
        _push_to_zalo_raw(inbox_tid, new_text,
                          mentions=[{"pos": pos, "uid": sender_uid, "len": utf16_tag}])
    else:
        _push_to_zalo_raw(inbox_tid, text)


def _push_to_zalo_raw(thread_id: str, text: str, mentions: Optional[list] = None,
                      thread_type: str = "group") -> None:
    """Low-level push: text + mentions array tuỳ ý (không tự tag).

    thread_type: 'group' (default) hoặc 'user' (chat 1-1).
    """
    import requests as _rq
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        return
    payload = {"thread_id": thread_id, "text": text, "thread_type": thread_type}
    if mentions:
        payload["mentions"] = mentions
    try:
        url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
        _rq.post(url, json=payload, headers={"X-Bridge-Secret": secret}, timeout=5)
    except Exception:
        pass


def _fmt_money(n: int) -> str:
    return f"{int(n):,}".replace(",", ".") + "đ"


_CATEGORY_KEYWORDS = [
    ("ads",     ["qc", "quảng cáo", "quang cao", "ads", "fb ads", "google ads"]),
    ("salary",  ["lương", "luong", "thưởng", "thuong", "phụ cấp", "phu cap"]),
    ("office",  ["văn phòng", "van phong", "vpp", "office", "giấy", "in ấn",
                 "máy", "sửa", "đo đạc", "do dac"]),
    ("utility", ["tiện ích", "tien ich", "điện", "dien", "nước", "nuoc",
                 "internet", "wifi", "fpt", "vnpt", "viettel", "evn"]),
    ("other",   ["khác", "khac", "nhà cung cấp", "nha cung cap", "ncc",
                 "vận chuyển", "van chuyen", "taxi", "vé"]),
]


def _detect_category_from_text(body: str) -> Optional[str]:
    """Tìm category trong tin follow-up. Vd 'Khác nhé' → 'other'."""
    import unicodedata as _ud
    s = _ud.normalize("NFD", body or "")
    s = "".join(c for c in s if _ud.category(c) != "Mn").lower()
    for cat, kws in _CATEGORY_KEYWORDS:
        for kw in kws:
            kw_norm = _ud.normalize("NFD", kw)
            kw_norm = "".join(c for c in kw_norm if _ud.category(c) != "Mn").lower()
            if kw_norm in s:
                return cat
    return None


def _find_duplicate_item(cur, sender_uid: str, amount: int,
                         within_minutes: int = 10, tolerance: float = 0.05,
                         exact_within_hours: int = 48) -> Optional[dict]:
    """Tìm item gần nhất của sender có khả năng là cùng 1 khoản.

    2 case bắt:
    1) Trong N phút, lệch < tolerance% → covers text+image gần nhau (NV vừa
       gõ text rồi gửi ảnh xác nhận).
    2) Trong M giờ, EXACT match số tiền → covers upload biên lai MUỘN (vd
       gõ text hôm qua, đến hôm nay mới gửi ảnh biên lai cùng số). Cần exact
       match vì cửa sổ rộng, lỏng quá dễ false-positive.

    Trả {id, amount_vnd, source, source_url, note} hoặc None.
    """
    if not sender_uid or amount <= 0:
        return None
    lo = int(amount * (1 - tolerance))
    hi = int(amount * (1 + tolerance))
    cur.execute("""
        SELECT id, amount_vnd, source, source_url, note
          FROM company_expense_items
         WHERE zalo_sender_id = %s
           AND deleted_at IS NULL
           AND status = 'pending'
           AND (
                (amount_vnd BETWEEN %s AND %s
                 AND created_at >= NOW() - (%s || ' minutes')::interval)
                OR
                (amount_vnd = %s
                 AND created_at >= NOW() - (%s || ' hours')::interval)
           )
         ORDER BY id DESC LIMIT 1
    """, (sender_uid, lo, hi, within_minutes, amount, exact_within_hours))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "amount_vnd": int(r[1] or 0),
            "source": r[2], "source_url": r[3] or "",
            "note": r[4] or ""}


def _merge_into_existing(cur, existing_id: int, new_amount: int, new_note: str,
                         new_source: str, new_source_url: str = "",
                         new_category: str = "", new_occurred_date=None) -> None:
    """Gộp candidate vào item cũ sau khi NV xác nhận 'là 1 khoản'.

    Quy tắc cập nhật (revised 2026-06-04):
    - amount: LẤY new_amount nếu khác cur (NV đang sửa số → tin số mới = số đúng,
      VD đang đọc biên lai gõ lại). Bỏ rule cũ "max()" vì giả định
      "ảnh OCR luôn > text" sai khi text gốc là đoán bừa (vd "Tiền điện kho" → 3tr).
    - category: nếu item cũ là 'other' (default DeepSeek khi không chắc) và item
      mới có loại cụ thể (utility/office/salary/ads) → upgrade. Vd: text "Sửa lại
      2.886.728đ" parse ra 'other', sau merge với ảnh biên lai điện → 'utility'.
    - occurred_date: nếu nguồn mới là ẢNH (biên lai) → tin ngày mới (biên lai
      luôn có timestamp chính xác). Text gõ tay thì giữ ngày cũ.
    - note: append nếu chưa có (giữ context cả 2 nguồn).
    - source: upgrade text → image nếu candidate là ảnh.
    """
    cur.execute("SELECT amount_vnd, note, source, category, occurred_date "
                "FROM company_expense_items WHERE id=%s", (existing_id,))
    r = cur.fetchone()
    if not r:
        return
    cur_amount = int(r[0] or 0)
    cur_note = (r[1] or "")
    cur_source = (r[2] or "text")
    cur_category = (r[3] or "other")
    cur_date = r[4]

    sets: list = []
    params: list = []

    # Amount: NV vừa xác nhận 'là 1' → tin số mới (kể cả nhỏ hơn cur).
    if new_amount and new_amount != cur_amount:
        sets.append("amount_vnd = %s"); params.append(int(new_amount))

    # Category: chỉ upgrade khi cũ là 'other' và mới CỤ THỂ.
    # Không downgrade chiều ngược (ads/utility → other) vì có thể mất thông tin.
    if (new_category and new_category != cur_category
            and cur_category == "other" and new_category != "other"):
        sets.append("category = %s"); params.append(new_category)

    # Date: tin nguồn ảnh (biên lai có timestamp giao dịch).
    if (new_occurred_date and new_source == "image"
            and new_occurred_date != cur_date):
        sets.append("occurred_date = %s"); params.append(new_occurred_date)

    # Note append.
    if new_note and new_note not in cur_note:
        merged = (cur_note + " · " + new_note).strip(" ·") if cur_note else new_note
        sets.append("note = %s"); params.append(merged)

    # Source upgrade text → image.
    if new_source == "image" and cur_source == "text" and new_source_url:
        sets.append("source = %s"); params.append("image")
        sets.append("source_url = %s"); params.append(new_source_url)

    if sets:
        sets.append("updated_at = NOW()")
        params.append(existing_id)
        cur.execute(f"UPDATE company_expense_items SET {', '.join(sets)} WHERE id = %s", params)


# ── Pending duplicate prompt (state app_config, TTL 5p) ──

def _save_dup_pending(sender_uid: str, data: dict) -> None:
    import json, time
    if not sender_uid:
        return
    from app_ctx import save_config_key
    data["ts"] = int(time.time())
    save_config_key(f"dup_pending_{sender_uid}", json.dumps(data, ensure_ascii=False))


def _load_dup_pending(sender_uid: str, ttl_sec: int = 300) -> Optional[dict]:
    if not sender_uid:
        return None
    import json, time
    try:
        from app_ctx import load_config
        raw = (load_config() or {}).get(f"dup_pending_{sender_uid}")
        if not raw:
            return None
        data = json.loads(raw) if isinstance(raw, str) else raw
        if time.time() - int(data.get("ts", 0)) > ttl_sec:
            _clear_dup_pending(sender_uid)
            return None
        return data
    except Exception:
        return None


def _clear_dup_pending(sender_uid: str) -> None:
    if not sender_uid:
        return
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_config WHERE key=%s", (f"dup_pending_{sender_uid}",))
                conn.commit()
    except Exception:
        pass


def _classify_dup_intent(body: str) -> str:
    """Phân loại trả lời của NV cho câu hỏi 'là 1 hay 2 khoản?'.

    Trả: 'same' (1 khoản, gộp) | 'different' (2 khoản, riêng) | 'unclear'.

    Dùng DeepSeek để hiểu linh hoạt (đừng cố từ khóa cứng).
    Fallback regex nếu API fail.
    """
    body_s = (body or "").strip()
    if not body_s:
        return "unclear"

    # Fast path keyword (đỡ tốn API call cho câu rõ ràng)
    import unicodedata as _ud
    norm = _ud.normalize("NFD", body_s)
    norm = "".join(c for c in norm if _ud.category(c) != "Mn").lower().strip()
    if norm in ("1", "1 khoan", "mot khoan", "mot", "dung", "yes", "y", "ok", "gop", "trung",
                "phai", "co", "uh", "ừ", "ừm"):
        return "same"
    if norm in ("2", "2 khoan", "hai khoan", "hai", "khac", "khong", "khong phai",
                "no", "rieng", "moi", "khac nhau"):
        return "different"

    # AI fallback
    try:
        from ai_keys import get_ai_key
        api_key = get_ai_key("deepseek") or ""
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return "unclear"

    import requests as _rq, json
    prompt = (
        "Lan vừa hỏi NV: 'Khoản này là 1 khoản hay 2 khoản khác nhau?'. "
        f"NV trả lời: \"{body_s}\"\n\n"
        "Phân loại ý NV thành 1 trong 3 nhãn:\n"
        "- 'same': NV xác nhận là CÙNG 1 khoản (gộp lại). Vd: '1', 'đúng', 'cùng 1', 'cùng đó', "
        "'là 1 khoản thôi', 'trùng', 'gộp đi', 'phải', 'đúng rồi', 'uh', 'ok'...\n"
        "- 'different': NV nói là 2 KHOẢN KHÁC NHAU. Vd: '2', 'khác', 'không phải', '2 khoản', "
        "'hai khoản riêng', 'mới', 'khác nhau', 'không trùng', 'khoản riêng'...\n"
        "- 'unclear': không rõ NV nói gì hoặc tin không liên quan câu hỏi.\n\n"
        "Output JSON CHỈ: {\"label\": \"same\"|\"different\"|\"unclear\"}"
    )
    try:
        r = _rq.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"},
                  "temperature": 0, "max_tokens": 30},
            timeout=10,
        )
        if r.status_code != 200:
            return "unclear"
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        lbl = (out.get("label") or "").lower().strip()
        return lbl if lbl in ("same", "different", "unclear") else "unclear"
    except Exception:
        return "unclear"


def handle_dup_pending_reply(thread_id: str, sender_uid: str, sender_name: str,
                              body: str, is_private: bool = False) -> Optional[dict]:
    """Nếu sender có pending dup + body có ý 'same/different' → resolve."""
    pending = _load_dup_pending(sender_uid)
    if not pending:
        return None
    intent = _classify_dup_intent(body)
    if intent == "unclear":
        return None
    is_yes = (intent == "same")
    is_no  = (intent == "different")

    from db import get_conn
    if is_yes:
        # Gộp candidate vào existing — lấy số mới + upgrade category/date nếu cần.
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Parse occurred_date string → date object (psycopg2 nhận cả 2)
                occ_date = pending.get("occurred_date") or None
                _merge_into_existing(
                    cur, int(pending["existing_id"]),
                    int(pending.get("amount", 0)),
                    pending.get("note", ""),
                    pending.get("kind", "text") if pending.get("kind") == "image" else "text",
                    pending.get("source_url", ""),
                    new_category=pending.get("category", "") or "",
                    new_occurred_date=occ_date,
                )
                # Lấy lại số tiền cuối sau khi merge
                cur.execute("SELECT amount_vnd FROM company_expense_items WHERE id=%s",
                            (int(pending["existing_id"]),))
                final = cur.fetchone()
                final_amount = int(final[0] or 0) if final else 0
                conn.commit()
        _clear_dup_pending(sender_uid)
        _push_to_zalo(thread_id,
                      f"✓ Lan gộp vô #{pending['existing_id']} rồi nha — chốt {_fmt_money(final_amount)} 🌸",
                      sender_uid, sender_name,
                      thread_type="user" if is_private else "group")
        return {"ok": True, "action": "merged", "into_id": pending["existing_id"]}

    # is_no → tạo mới candidate
    with get_conn() as conn:
        with conn.cursor() as cur:
            if pending.get("kind") == "image":
                cur.execute("""
                    INSERT INTO company_expense_items
                        (occurred_date, amount_vnd, category, note, source, source_url,
                         confidence, raw_body, zalo_thread_id, zalo_sender_id,
                         zalo_sender_name, user_id, zalo_msg_id, is_private)
                    VALUES (%s, %s, %s, %s, 'image', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (pending.get("occurred_date"), int(pending["amount"]),
                      pending.get("category", "other"), pending.get("note", ""),
                      pending.get("source_url", ""), float(pending.get("confidence", 0)),
                      pending.get("caption", ""), pending.get("thread_id", thread_id),
                      sender_uid, sender_name, _resolve_user_id(sender_uid),
                      pending.get("zalo_msg_id") or None, bool(pending.get("is_private"))))
            else:
                cur.execute("""
                    INSERT INTO company_expense_items
                        (occurred_date, amount_vnd, category, note, source, confidence,
                         raw_body, zalo_thread_id, zalo_sender_id, zalo_sender_name, user_id,
                         zalo_msg_id, is_private)
                    VALUES (%s, %s, %s, %s, 'text', %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (pending.get("occurred_date"), int(pending["amount"]),
                      pending.get("category", "other"), pending.get("note", ""),
                      float(pending.get("confidence", 0)), pending.get("body", ""),
                      pending.get("thread_id", thread_id), sender_uid, sender_name,
                      _resolve_user_id(sender_uid),
                      pending.get("zalo_msg_id") or None, bool(pending.get("is_private"))))
            new_id = cur.fetchone()[0]
            conn.commit()
    _clear_dup_pending(sender_uid)
    cat_lbl = CATEGORY_LABEL.get(pending.get("category", "other"), "📦 Khác")
    _push_to_zalo(thread_id,
                  f"✓ Lan tạo khoản RIÊNG #{new_id}: {_fmt_money(int(pending['amount']))} · {cat_lbl} 🌸",
                  sender_uid, sender_name,
                  thread_type="user" if is_private else "group")

    # Forward vào inbox nếu cần
    try:
        from datetime import date as _d
        occ_d = _d.fromisoformat(pending["occurred_date"]) if pending.get("occurred_date") else None
        _forward_to_inbox(int(pending["amount"]), pending.get("category", "other"),
                          pending.get("note", ""), occ_d, sender_uid, sender_name,
                          pending.get("kind", "text"), pending.get("source_url", ""),
                          pending.get("thread_id", thread_id), new_id,
                          is_private=bool(pending.get("is_private")))
    except Exception:
        pass
    return {"ok": True, "action": "created_separate", "item_id": new_id}


def _get_recent_pending_item(cur, sender_uid: str, thread_id: str = "",
                            within_minutes: int = 15) -> Optional[dict]:
    """Lấy item pending gần nhất của sender.

    Ưu tiên item cùng thread; nếu không có → fallback bất kỳ thread nào
    (NV có thể gửi 1-1 rồi follow-up trong inbox group, hoặc ngược lại).
    """
    if thread_id:
        cur.execute("""
            SELECT id, amount_vnd, category, note
              FROM company_expense_items
             WHERE zalo_sender_id = %s AND zalo_thread_id = %s
               AND deleted_at IS NULL AND status = 'pending'
               AND created_at >= NOW() - INTERVAL '%s minutes'
             ORDER BY id DESC LIMIT 1
        """, (sender_uid, thread_id, within_minutes))
        r = cur.fetchone()
        if r:
            return {"id": r[0], "amount_vnd": int(r[1] or 0),
                    "category": r[2], "note": r[3] or ""}
    # Fallback: cùng sender, bất kỳ thread
    cur.execute("""
        SELECT id, amount_vnd, category, note
          FROM company_expense_items
         WHERE zalo_sender_id = %s
           AND deleted_at IS NULL AND status = 'pending'
           AND created_at >= NOW() - INTERVAL '%s minutes'
         ORDER BY id DESC LIMIT 1
    """, (sender_uid, within_minutes))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "amount_vnd": int(r[1] or 0),
            "category": r[2], "note": r[3] or ""}


def handle_expense_follow_up(thread_id: str, sender_uid: str, sender_name: str,
                             body: str, is_private: bool = False) -> Optional[dict]:
    """Tin text không có số → thử coi là follow-up cho item gần nhất.

    Trả dict {ok, action, item_id} nếu xử lý được, None nếu không apply.
    """
    if not sender_uid or not thread_id or not body:
        return None
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            it = _get_recent_pending_item(cur, sender_uid, thread_id, within_minutes=15)
            if not it:
                return None

            new_cat = _detect_category_from_text(body)
            updates = []
            params = []
            actions = []

            if new_cat and new_cat != it["category"]:
                updates.append("category = %s"); params.append(new_cat)
                actions.append(f"loại → {CATEGORY_LABEL.get(new_cat, new_cat)}")

            # Tin có chữ "thanh toán/chuyển khoản/mua/chi..." → ghi vào note
            body_trim = body.strip()
            if len(body_trim) >= 4 and len(body_trim) <= 200:
                # Append note nếu khác note cũ
                merged_note = (it["note"] + " · " + body_trim).strip(" ·") if it["note"] else body_trim
                if merged_note != it["note"]:
                    updates.append("note = %s"); params.append(merged_note)
                    actions.append("ghi chú")

            if not updates:
                return None

            updates.append("updated_at = NOW()")
            params.append(it["id"])
            cur.execute(f"UPDATE company_expense_items SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()

    # Gọi AI sinh reply tự nhiên thay vì template cứng
    reply = None
    try:
        from modules.budget_chat.lan_personality import _ai_lan_reply
        ai_prompt = (
            f"NV '{sender_name}' vừa bổ sung thông tin cho khoản chi {_fmt_money(it['amount_vnd'])} "
            f"đã ghi cách đây vài phút. Nội dung bổ sung: \"{body.strip()}\". "
            f"Lan vừa cập nhật: {', '.join(actions)}. "
            "Reply 1 câu xác nhận ngắn, dễ thương, ghi rõ đã cập nhật + cảm ơn NV bổ sung."
        )
        reply = _ai_lan_reply(ai_prompt)
    except Exception:
        pass
    if not reply:
        reply = f"✓ Lan cập nhật xong {', '.join(actions)} cho khoản {_fmt_money(it['amount_vnd'])} rồi nha 🌸"

    _push_to_zalo(thread_id, reply, sender_uid, sender_name,
                  thread_type="user" if is_private else "group")
    return {"ok": True, "action": "follow_up", "item_id": it["id"]}


_AD_ACCOUNT_PATTERNS: list = [
    # ⚠ ĐÃ XOÁ keyword chung chung (2026-05-25):
    # 'tài khoản quảng cáo', 'tk QC', 'fb ads', 'ad account'... → TRÙNG với tin
    # CK cho cty dịch vụ QC bên ngoài (vd 'CTY CP QUANG CAO CONG VIET NAM',
    # caption 'Ngân sách quảng cáo khác'). Tin đó là CHI PHÍ ADS THẬT, không
    # phải TK nội bộ.
    # CHỈ giữ regex bên dưới — pattern MÃ TK nội bộ rõ ràng
    # (LinhTH16.4 / Long15.3 / TungTH22.1...).
]
import re as _re_global
_AD_ACCOUNT_REGEX = _re_global.compile(
    r"\b(?:tk|tài\s*khoản|tai\s*khoan)\s*[:\-]?\s*(?:[a-zA-Z]+\s*){1,3}\d|"  # "tk Long15" / "tk linh tk16"
    r"\b[a-zA-Z]+TH\s*\d|"                                         # LinhTH16 / Linh TH16/4
    r"\b[a-zA-Z]{3,}\d+\.\d+\b|"                                   # Long15.3 / Hai10.5
    # Tên + space + DD[./]M (chữ 2-15 ký tự, số ngày 1-31, tháng 1-12).
    # Loại trừ "phòng/version/ver/build/chương" bằng negative lookahead.
    r"\b(?!(?:phòng|phong|version|ver|build|chương|chuong)\b)"
    r"[a-zA-Z]{2,15}\s+(?:[12]?\d|3[01])[\./](?:1[0-2]|[1-9])\b",
    _re_global.IGNORECASE,
)


def _is_ad_account_context(text: str) -> bool:
    """Detect tin/ảnh có liên quan tài khoản QC → phải là NS, không phải chi phí."""
    if not text:
        return False
    import unicodedata as _ud
    norm = _ud.normalize("NFD", text)
    norm = "".join(c for c in norm if _ud.category(c) != "Mn").lower()
    for kw in _AD_ACCOUNT_PATTERNS:
        kw_norm = _ud.normalize("NFD", kw)
        kw_norm = "".join(c for c in kw_norm if _ud.category(c) != "Mn").lower()
        if kw_norm in norm:
            return True
    if _AD_ACCOUNT_REGEX.search(text):
        return True
    return False


_RE_MONEY_TOKEN = _re_global.compile(
    r"\d+\s*(?:k|tr|triệu|trieu|ngàn|ngan|nghìn|nghin|đồng|dong|vnd|vnđ|đ)\b",
    flags=_re_global.IGNORECASE,
)
# Số tiền có CONTEXT rõ ràng — tránh false positive với SĐT / mã đơn:
#   • Dạng "1.000.000" / "200,000" (có dấu nhóm hàng nghìn)
#   • Số ≥ 4 chữ số kèm hậu tố tiền (đ/vnd/vnđ/đồng/dong)
# Bỏ rule cũ "\d{4,}" trần — quá rộng.
_RE_BIG_NUMBER = _re_global.compile(
    r"\b\d{1,3}(?:[.,]\d{3})+\s*(?:đ|vnd|vnđ|đồng|dong)?\b|"
    r"\b\d{4,}\s*(?:đ|vnd|vnđ|đồng|dong)\b",
    flags=_re_global.IGNORECASE,
)
_MONEY_KEYWORDS = (
    "tiền", "tien", "chi phí", "chi phi", "thanh toán", "thanh toan",
    "chuyển khoản", "chuyen khoan", "lương", "luong", "hoá đơn", "hoa don",
    "vpp", "ngân sách", "ngan sach", "phí", "phi", "mua", "trả", "tra",
)


# Lan vừa hỏi NV gì gần đây — để biết khi NV bảo "thôi/dừng" thì context nào
# cần dừng. Key = sender_uid, value = (asked_at_ts, thread_id).
_LAN_ASKED: dict = {}


def _mark_lan_asked(sender_uid: str, thread_id: str) -> None:
    """Lan vừa push 1 câu hỏi (no_amount, follow-up, ask_format...) → ghi nhận
    để 5 phút sau nếu NV bảo 'thôi/dừng' thì biết stop ngữ cảnh nào."""
    if not sender_uid:
        return
    import time as _t
    _LAN_ASKED[sender_uid] = (_t.time(), thread_id)
    # Sweep stale > 30 phút
    if len(_LAN_ASKED) > 200:
        now = _t.time()
        stale = [k for k, v in _LAN_ASKED.items() if now - v[0] > 1800]
        for k in stale:
            _LAN_ASKED.pop(k, None)


_STOP_KEYWORDS = (
    "dừng", "dung lai", "dừng lại", "thôi", "thoi", "ko phải", "không phải",
    "kp", "ko cần", "không cần", "bỏ qua", "bo qua", "skip", "stop",
    "ko phai", "ko can", "khong can", "khong phai",
)


def _is_stop_signal(body: str) -> bool:
    """Tin có chứa tín hiệu 'dừng / thôi'? (cho phép linh hoạt tiếng Việt
    có/không dấu, có thể đi kèm 'Lan')."""
    if not body:
        return False
    import unicodedata as _ud
    s = _ud.normalize("NFD", body).lower()
    s = "".join(c for c in s if _ud.category(c) != "Mn")
    s = s.replace("đ", "d")
    # Match exact token (tránh "thôi nôi" sai), nhưng cho phép ngắn gọn
    for kw in _STOP_KEYWORDS:
        kw_norm = _ud.normalize("NFD", kw).lower()
        kw_norm = "".join(c for c in kw_norm if _ud.category(c) != "Mn").replace("đ", "d")
        if kw_norm in s:
            return True
    return False


def handle_stop_signal_if_any(thread_id: str, sender_uid: str, sender_name: str,
                              body: str, is_private: bool = False) -> bool:
    """Nếu NV vừa bảo 'thôi/dừng' VÀ Lan đã hỏi gì đó trong 5 phút qua →
    Lan ack dừng + xoá context, return True. Ngược lại return False (luồng
    chính xử lý tiếp).
    """
    if not _is_stop_signal(body):
        return False
    if not sender_uid:
        return False
    import time as _t
    rec = _LAN_ASKED.get(sender_uid)
    if not rec:
        return False
    asked_at, _ = rec
    if _t.time() - asked_at > 300:  # 5 phút
        _LAN_ASKED.pop(sender_uid, None)
        return False
    # Có context → Lan ack dừng
    _LAN_ASKED.pop(sender_uid, None)
    _push_to_zalo(thread_id,
                  f"Dạ Lan dừng nha {sender_name or 'bạn'} 🌸 Khi nào cần Lan, "
                  "sếp tag @Lan lại giúp em nhé.",
                  sender_uid, sender_name,
                  thread_type="user" if is_private else "group")
    return True


def _is_relevant_to_lan(body: str) -> bool:
    """Tin có cần Lan trả lời không?

    Trả True khi:
      - Có gọi tên Lan ("lan ơi", "@lan", "lan ghi", ...)
      - Có tài khoản QC (TK QC) — `_is_ad_account_context`
      - Có dấu hiệu tiền: số ≥ 1000, hoặc token "200k"/"5tr", hoặc keyword
        chi phí (tiền/chi phí/thanh toán/lương...)
    Tin chit chat thông thường → False, Lan im lặng.
    """
    if not body:
        return False
    import unicodedata as _ud
    def _norm(s):
        s = _ud.normalize("NFD", s or "").lower()
        return "".join(c for c in s if _ud.category(c) != "Mn")
    low = _norm(body)
    # 1. Gọi tên Lan
    if _re_global.search(r"\blan\b", low):
        return True
    # 2. TK QC context
    if _is_ad_account_context(body):
        return True
    # 3. Money signals
    if _RE_MONEY_TOKEN.search(body):
        return True
    if _RE_BIG_NUMBER.search(body):
        return True
    for kw in _MONEY_KEYWORDS:
        if kw in low:
            return True
    return False


def _has_money_signal(body: str) -> bool:
    """Tin có dấu hiệu nói về tiền/chi phí? (không tính 'lan' mention).
    Dùng để phân biệt 'chat chit' vs 'báo CP không rõ số tiền'."""
    if not body:
        return False
    import unicodedata as _ud
    low = _ud.normalize("NFD", body).lower()
    low = "".join(c for c in low if _ud.category(c) != "Mn")
    if _is_ad_account_context(body):
        return True
    if _RE_MONEY_TOKEN.search(body):
        return True
    if _RE_BIG_NUMBER.search(body):
        return True
    for kw in _MONEY_KEYWORDS:
        if kw in low:
            return True
    return False


def _is_non_ads_sender(sender_name: str) -> bool:
    """Người gửi đã được whitelist là KHÔNG báo chi phí QC.

    Cấu hình: app_config['expense_non_ads_senders'] = 'TÊN1, TÊN2, ...'
    Mặc định: 'Vợ, Huyền' (chị Huyền — vợ sếp, gửi sao kê khoản cá nhân/VP).
    Match case-insensitive + bỏ dấu, substring trong tên hiển thị Zalo.
    """
    if not sender_name:
        return False
    try:
        from app_ctx import load_config
        raw = (load_config() or {}).get("expense_non_ads_senders", "Vợ, Huyền")
        names = [n.strip() for n in str(raw).split(",") if n.strip()]
        if not names:
            return False
        import unicodedata as _ud
        def _norm(s):
            s = _ud.normalize("NFD", s or "")
            return "".join(c for c in s if _ud.category(c) != "Mn").upper()
        haystack = _norm(sender_name)
        for n in names:
            if _norm(n) in haystack:
                return True
    except Exception:
        pass
    return False


def _force_ads_if_owner_transfer(note: str, body: str = "") -> bool:
    """Check nếu tin/ảnh có tên chủ chuyển ngân sách QC → force category=ads.

    Cấu hình: app_config['expense_ads_payers'] = 'TÊN1, TÊN2, ...'
    Match không phân biệt hoa thường + bỏ dấu.
    """
    try:
        from app_ctx import load_config
        raw = (load_config() or {}).get("expense_ads_payers", "")
        if not raw:
            return False
        names = [n.strip() for n in str(raw).split(",") if n.strip()]
        if not names:
            return False
        import unicodedata as _ud
        def _norm(s):
            s = _ud.normalize("NFD", s or "")
            return "".join(c for c in s if _ud.category(c) != "Mn").upper()
        haystack = _norm((note or "") + " " + (body or ""))
        for n in names:
            if _norm(n) in haystack:
                return True
    except Exception:
        pass
    return False


# Parse text qua DeepSeek (đơn giản hơn budget — chỉ extract 1 chi phí)
_TEXT_PROMPT = """Bạn parse tin nhắn chi phí công ty (kế toán, KHÔNG phải FB Ads).

QUAN TRỌNG về note: trong field 'note', KHÔNG được nhắc đến tên công nghệ
AI / model nào (KHÔNG Gemini/DeepSeek/GPT/Claude/OpenAI/Google/API/OCR...).
Chỉ mô tả nghiệp vụ ngắn gọn về chi phí.

Tin có thể là: chi VPP 200k, lương t5 5tr, tiền điện 2tr5, chuyển khoản 10tr...

Output CHỈ JSON (không markdown):
{
  "amount_vnd": <số nguyên VND>,
  "category": "ads" | "salary" | "office" | "utility" | "other",
  "note": "<mô tả ngắn>",
  "occurred_date": "YYYY-MM-DD" | null,
  "confidence": 0.0-1.0
}

Quy ước số tiền:
- 300k=300000, 1tr=1000000, 4tr5=4500000, 1tr2=1200000.

Quy ước CATEGORY (đọc kỹ — CHỈ phân loại khi có BẰNG CHỨNG TRỰC TIẾP
trong tin, không suy luận từ tên người):

- ads = QUẢNG CÁO. Chỉ khi có chữ "quảng cáo", "ads", "QC", "FB ads",
  "Google ads", hoặc tên cty chuyên QC. Thường chi phí Cty KHÔNG
  phải ads → mặc định KHÔNG dùng ads.

- salary = LƯƠNG. CHỈ khi có:
  * "lương" / "thưởng" / "phụ cấp" + ai đó, HOẶC
  * "T1"/"T2"/.../"T12" / "tháng 1"/.../"tháng 12" (chỉ kỳ lương).
  ❌ KHÔNG xếp là salary chỉ vì tin có tên người (vd "chuyển khoản
  cho Nguyễn Văn A" KHÔNG phải lương — đó là chuyển khoản thông
  thường, xếp 'other').

- office = VPP / máy móc / sửa chữa / in ấn / nội thất / giấy mực.
  Chỉ khi có chữ "VPP", "giấy", "in", "máy", "sửa", "mua đồ", "văn
  phòng phẩm"...

- utility = ĐIỆN / NƯỚC / INTERNET / ĐIỆN THOẠI. Chỉ khi có chữ
  "điện", "nước", "EVN", "internet", "FPT", "VNPT", "viettel", "wifi"...

- other = MẶC ĐỊNH. Khi không khớp các pattern trên, hoặc chỉ là
  "chuyển khoản" / "thanh toán" cho ai đó không rõ mục đích → other.

Tin không phải chi phí (vd chat thường) → amount_vnd=0, confidence=0."""


def _parse_expense_text(body: str) -> dict:
    import json
    import requests as _rq
    try:
        from ai_keys import get_ai_key
        api_key = get_ai_key("deepseek")
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return {"amount_vnd": 0, "confidence": 0, "category": "other", "note": body[:80]}
    try:
        try:
            from tz_utils import now_hcm
            _today_iso = now_hcm().date().isoformat()
        except Exception:
            from datetime import date as _date
            _today_iso = _date.today().isoformat()
        sys_prompt = _TEXT_PROMPT + f"\n\nHÔM NAY là {_today_iso}. Khi NV chỉ ghi 'ngày DD/MM' không kèm năm → DÙNG NĂM CỦA HÔM NAY."
        r = _rq.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": body},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=20,
        )
        if r.status_code != 200:
            logger.warning("expense parse HTTP %s", r.status_code)
            return {"amount_vnd": 0, "confidence": 0, "category": "other", "note": body[:80]}
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        # P1 (2026-06-04) — Anti-hallucination guard:
        # Nếu AI trả về amount > 0 NHƯNG body không có DIGIT nào và không có
        # tiếng Việt số ("một/hai/ba... triệu/nghìn") → AI đang đoán bừa.
        # Vd "Tiền điện kho" → DeepSeek bịa 3.000.000đ. Reject để tránh ghi sai.
        try:
            ai_amount = int(out.get("amount_vnd") or 0)
            if ai_amount > 0:
                has_digit = bool(_re_global.search(r"\d", body or ""))
                _WORD_AMOUNT_RE = _re_global.compile(
                    r"\b(?:một|hai|ba|bốn|bon|năm|nam|sáu|sau|bảy|bay|tám|tam|chín|chin|mười|muoi|trăm|tram|nghìn|nghin|ngàn|ngan|triệu|trieu|tỷ|ty)\b",
                    flags=_re_global.IGNORECASE,
                )
                has_word_amount = bool(_WORD_AMOUNT_RE.search(body or ""))
                if not has_digit and not has_word_amount:
                    logger.info("expense parse REJECT hallucination: body=%r → AI bịa amount=%s",
                                (body or "")[:80], ai_amount)
                    out["amount_vnd"] = 0
                    out["confidence"] = 0
                    out["_hallucination"] = True
        except Exception:
            pass
        # Coerce năm: nếu model trả về năm < năm hiện tại mà DD-MM không vượt today → đẩy về năm hiện tại
        try:
            from datetime import date as _d
            from tz_utils import now_hcm as _nh
            today_d = _nh().date()
            od = out.get("occurred_date")
            if od:
                d = _d.fromisoformat(od)
                if d.year < today_d.year:
                    cand = d.replace(year=today_d.year)
                    if cand <= today_d:
                        out["occurred_date"] = cand.isoformat()
        except Exception:
            pass
        return out
    except Exception as exc:
        logger.warning("expense parse error: %s", exc)
        return {"amount_vnd": 0, "confidence": 0, "category": "other", "note": body[:80]}


def _resolve_user_id(zalo_uid: str) -> Optional[int]:
    if not zalo_uid:
        return None
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE zalo_uid=%s LIMIT 1", (zalo_uid,))
                r = cur.fetchone()
                return int(r[0]) if r else None
    except Exception:
        return None


def handle_expense_text(thread_id: str, sender_uid: str, sender_name: str, body: str,
                        zalo_msg_id: str = "", is_private: bool = False):
    """Bridge gọi khi tin text vào expense group hoặc chat 1-1."""
    parsed = _parse_expense_text(body)
    amount = int(parsed.get("amount_vnd") or 0)
    category = (parsed.get("category") or "other").strip()
    note = (parsed.get("note") or "").strip() or body[:120]
    occ = parsed.get("occurred_date") or None
    conf = float(parsed.get("confidence") or 0)

    # Fallback ngày: nếu Lan không parse được → dùng ngày hôm nay (lúc nhận tin)
    if not occ:
        try:
            from tz_utils import now_hcm
            occ = now_hcm().date().isoformat()
        except Exception:
            from datetime import date as _date
            occ = _date.today().isoformat()

    # Override: nếu thấy tên chủ chuyển NS QC → force category = ads
    if _force_ads_if_owner_transfer(note, body):
        category = "ads"

    # Whitelist người gửi KHÔNG báo QC (vd chị Huyền). Demote ads → other.
    if category == "ads" and _is_non_ads_sender(sender_name):
        category = "other"
        logger.info("demote ads→other: sender '%s' is whitelisted non-ads", sender_name)

    if amount <= 0:
        # TK QC context nhưng chưa có số tiền → hint NS, KHÔNG bảo "ghi rõ số tiền" mơ hồ.
        if _is_ad_account_context(body):
            _push_to_zalo(thread_id,
                          "🤔 Lan thấy bạn nhắc TK QC. Số tiền chạy bao nhiêu ạ?\n"
                          "Gõ kiểu: `tk LinhTH16.4 chạy 500k ngày mai` để Lan ghi NS team 🌸",
                          sender_uid, sender_name,
                          thread_type="user" if is_private else "group")
            return jsonify({"ok": True, "saved": False, "reason": "ad_account_no_amount"})
        # Tin chit chat không liên quan tiền/Lan/TK → im lặng, không làm rối nhóm.
        if not _is_relevant_to_lan(body):
            return jsonify({"ok": True, "saved": False, "reason": "irrelevant_silent"})
        _push_to_zalo(thread_id,
                      "🤔 Lan chưa hiểu được số tiền trong tin này. Bạn ghi rõ giúp Lan nhé "
                      "(vd: 'chi VPP 200k' hoặc 'lương Nam 5tr').",
                      sender_uid, sender_name)
        _mark_lan_asked(sender_uid, thread_id)
        return jsonify({"ok": True, "saved": False, "reason": "no_amount"})

    # Chốt cứng: tin liên quan TK quảng cáo → KHÔNG ghi chi phí, là NS
    if _is_ad_account_context(body) or _is_ad_account_context(note):
        _push_to_zalo(thread_id,
                      "🤔 Tin này có vẻ liên quan tới TÀI KHOẢN QUẢNG CÁO ạ. Đây là báo ngân "
                      "sách QC chứ không phải chi phí Cty.\n\n"
                      "Bạn gõ giúp Lan theo dạng: `tk <Tên TK> chạy <Số tiền> ngày <ngày>` "
                      "(vd: 'tk LinhTH16.4 chạy 500k ngày mai') để Lan ghi vào NS team nhé 🌸",
                      sender_uid, sender_name,
                      thread_type="user" if is_private else "group")
        _mark_lan_asked(sender_uid, thread_id)
        return jsonify({"ok": True, "saved": False, "reason": "ad_account_context"})

    user_id = _resolve_user_id(sender_uid)
    # Check khả năng trùng với item gần đây (cùng sender, ±5% số, 10 phút)
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if zalo_msg_id:
                    cur.execute("SELECT id FROM company_expense_items WHERE zalo_msg_id=%s LIMIT 1", (zalo_msg_id,))
                    if cur.fetchone():
                        conn.commit()
                        return jsonify({"ok": True, "skipped": "duplicate"})
                dup = _find_duplicate_item(cur, sender_uid, amount, within_minutes=10, tolerance=0.05)
        if dup:
            # Lưu candidate vào pending + hỏi NV
            _save_dup_pending(sender_uid, {
                "kind": "text", "existing_id": dup["id"],
                "amount": amount, "category": category, "note": note,
                "occurred_date": occ, "confidence": conf,
                "body": body, "thread_id": thread_id,
                "zalo_msg_id": zalo_msg_id or "", "is_private": is_private,
            })
            ask = (f"🤔 Lan thấy khoản này giống với #{dup['id']} ({_fmt_money(dup['amount_vnd'])}) "
                   f"vừa ghi. Đây là 1 khoản hay 2 khoản khác nhau ạ?")
            _push_to_zalo(thread_id, ask, sender_uid, sender_name,
                          thread_type="user" if is_private else "group")
            return jsonify({"ok": True, "asked_dup": True, "candidate_for": dup["id"]})
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO company_expense_items
                        (occurred_date, amount_vnd, category, note, source, confidence,
                         raw_body, zalo_thread_id, zalo_sender_id, zalo_sender_name, user_id,
                         zalo_msg_id, is_private)
                    VALUES (%s, %s, %s, %s, 'text', %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (occ, amount, category, note, conf, body, thread_id, sender_uid, sender_name, user_id,
                      zalo_msg_id or None, is_private))
                new_id = cur.fetchone()[0]
                conn.commit()
    except Exception as exc:
        logger.error("expense save text error: %s", exc, exc_info=True)
        _push_to_zalo(thread_id, f"😢 Lan lưu chi phí lỗi ({exc}). Báo IT giúp.", sender_uid, sender_name)
        return jsonify({"ok": False, "error": str(exc)}), 500

    # Reply ngắn ở group/1-1 nguồn
    cat_lbl = CATEGORY_LABEL.get(category, "📦 Khác")
    short_reply = f"✓ Lan ghi: {_fmt_money(amount)} · {cat_lbl}"
    if is_private:
        short_reply = f"🔒 Lan ghi (báo riêng): {_fmt_money(amount)} · {cat_lbl}"
    if conf < 0.7:
        short_reply += f" (⚠ confidence {conf:.0%})"
    _push_to_zalo(thread_id, short_reply, sender_uid, sender_name,
                  thread_type="user" if is_private else "group")

    # Forward chi tiết về inbox cho sếp/kế toán (private có flag 🔒)
    _forward_to_inbox(amount, category, note, occ, sender_uid, sender_name,
                      "text", "", thread_id, new_id, is_private=is_private)

    return jsonify({"ok": True, "saved": True, "item_id": new_id, "private": is_private})


def _insert_image_skip_stub(zalo_msg_id: str, image_url: str, caption: str,
                            thread_id: str, sender_uid: str, sender_name: str,
                            is_private: bool, note: str = "ảnh bỏ qua"):
    """Ghi 1 row 'rejected' + deleted để catchup/replay không xử lý lại cùng ảnh.
    Không hiện trên UI, không tính vào tổng. Dedup qua zalo_msg_id (UNIQUE).
    No-op nếu không có zalo_msg_id."""
    if not zalo_msg_id:
        return
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO company_expense_items
                        (occurred_date, amount_vnd, category, note, source, source_url,
                         confidence, raw_body, zalo_thread_id, zalo_sender_id,
                         zalo_sender_name, user_id, zalo_msg_id, is_private,
                         status, deleted_at)
                    VALUES (NULL, 0, 'other', %s, 'image', %s,
                            0, %s, %s, %s, %s, NULL, %s, %s,
                            'rejected', NOW())
                    ON CONFLICT (zalo_msg_id) DO NOTHING
                """, (note, image_url, caption,
                      thread_id, sender_uid, sender_name, zalo_msg_id, is_private))
                conn.commit()
    except Exception as exc:
        logger.info("image skip stub insert skip: %s", exc)


def handle_expense_image(thread_id: str, sender_uid: str, sender_name: str,
                        image_url: str, caption: str = "", zalo_msg_id: str = "",
                        is_private: bool = False):
    """Bridge gọi khi ảnh vào expense group. Dùng Gemini Vision."""
    import base64, json, requests as _rq
    from ai_keys import get_ai_key, get_gemini_model

    gkey = get_ai_key("gemini")
    if not gkey:
        _push_to_zalo(thread_id,
                      "🥺 Lan chưa đọc được ảnh lúc này. IT đang xử lý nhé, cả nhà tạm gõ tin chữ giúp Lan.",
                      sender_uid, sender_name)
        return jsonify({"ok": False, "error": "no vision key"}), 200
    model = get_gemini_model()

    try:
        ir = _rq.get(image_url, timeout=15)
        if ir.status_code != 200:
            raise RuntimeError(f"download HTTP {ir.status_code}")
        img = ir.content
        mime = ir.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
    except Exception as exc:
        # Tải ảnh lỗi → IM LẶNG (không spam). Không stub để còn thử lại lần sau.
        logger.warning("expense image download error (silent): %s", exc)
        return jsonify({"ok": False, "error": str(exc), "silent": True}), 200

    img_b64 = base64.b64encode(img).decode("ascii")
    try:
        from tz_utils import now_hcm as _nh
        _today_iso = _nh().date().isoformat()
    except Exception:
        from datetime import date as _date
        _today_iso = _date.today().isoformat()
    _today_hdr = ("HÔM NAY là " + _today_iso + ". Khi ảnh chỉ ghi ngày dạng "
                  "DD/MM, DD-MM, D/M (không kèm năm) → NĂM phải là năm của "
                  "HÔM NAY, KHÔNG được dùng năm cũ.\n\n")
    vprompt = _today_hdr + """Bạn phân tích ảnh chi phí công ty (SMS bank, hoá đơn, screenshot QR, biên lai...).

QUAN TRỌNG về note: KHÔNG nhắc đến tên công nghệ AI/model (Gemini,
DeepSeek, GPT, Claude, OpenAI, Google, OCR, API, vision...). Chỉ mô tả
nghiệp vụ về chi phí ngắn gọn 1 dòng.

Trả CHỈ JSON:
{
  "amount_vnd": <số nguyên VND>,
  "category": "ads" | "salary" | "office" | "utility" | "other",
  "note": "<mô tả 1 dòng, vd 'Hoá đơn điện EVN tháng 4'>",
  "occurred_date": "YYYY-MM-DD" | null,
  "confidence": 0.0-1.0,
  "is_payment_receipt": true | false
}

is_payment_receipt = PHÂN LOẠI QUAN TRỌNG NHẤT. Chỉ ghi nhận chi phí
khi ảnh THỰC SỰ là 1 chứng từ thanh toán/giao dịch tiền.

= true KHI ảnh là biên lai / chứng từ tiền:
  - Màn hình app/SMS ngân hàng báo "Chuyển tiền thành công" /
    "Giao dịch thành công" (Techcombank, Vietcombank, MB, ACB...).
  - Màn hình chuyển khoản, mã QR thanh toán, biên lai chuyển tiền.
  - Hoá đơn / phiếu thu / phiếu chi / biên lai giấy.
  → Có dấu hiệu: số tiền + người/TK nhận + nội dung chuyển + mã giao dịch.

= false KHI ảnh KHÔNG phải biên lai (TUYỆT ĐỐI KHÔNG ghi nhận):
  - Dashboard / báo cáo / biểu đồ (cột, đường), bảng nhiều cột số liệu.
  - Trình quản lý QC Facebook Ads Manager / Meta / Google Ads
    (Spend / Impressions / CPM / CPC, account "act_...").
  - Ảnh chụp phần mềm / website / báo cáo doanh thu - lợi nhuận,
    bảng tính Excel, thống kê.
  - Ảnh linh tinh không liên quan tiền.
  Dù trong ảnh CÓ con số tiền (vd tổng lợi nhuận) cũng đặt false nếu
  đó là báo cáo/biểu đồ, KHÔNG phải chứng từ thanh toán.

Quy ước số tiền:
- amount_vnd: số nguyên VND. USD/$ → tỷ giá ~25000.

Quy ước CATEGORY (CHỈ phân loại khi có BẰNG CHỨNG TRỰC TIẾP trong
ảnh, KHÔNG suy luận từ tên người nhận):

- ads: ảnh có chữ "quảng cáo", "ads", "QC", logo FB Ads/Google, hoặc
  thanh toán cho cty chuyên QC. Mặc định KHÔNG dùng ads.

- salary: CHỈ khi nội dung ghi rõ "lương", "thưởng", "phụ cấp",
  HOẶC kỳ "T1"-"T12" / "tháng 1"-"tháng 12".
  ❌ TUYỆT ĐỐI KHÔNG xếp salary chỉ vì có tên người nhận. Vd:
  "chuyển khoản cho NGUYEN VAN A" KHÔNG phải lương → xếp 'other'.

- office: ảnh hoá đơn VPP, máy móc, sửa chữa, in ấn, giấy mực, nội
  thất văn phòng.

- utility: hoá đơn điện (EVN), nước, internet (FPT/VNPT/Viettel),
  điện thoại, wifi.

- other: MẶC ĐỊNH. SMS bank thông thường không rõ mục đích, chuyển
  khoản cho cá nhân/cty không xác định loại chi → other.

- note: tóm tắt 1 dòng (ai chuyển cho ai, lý do nếu thấy).
- date: từ ảnh nếu có; không thì null.
- confidence: 0-1, thấp khi không chắc category."""

    try:
        gurl = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gkey}"
        gr = _rq.post(gurl, json={
            "contents": [{"parts": [
                {"text": vprompt},
                {"inline_data": {"mime_type": mime, "data": img_b64}},
            ]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }, timeout=30)
        if gr.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {gr.status_code}: {gr.text[:200]}")
        parsed = json.loads(gr.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as exc:
        # Gemini lỗi/timeout → IM LẶNG (không spam lỗi vào nhóm). Chỉ log.
        # KHÔNG ghi dedup stub → để lần delivery sau (nếu có) còn cơ hội đọc lại.
        logger.warning("expense vision error (silent): %s", exc)
        return jsonify({"ok": False, "error": str(exc), "silent": True}), 200

    # PHÂN LOẠI: chỉ ghi nhận khi ảnh là biên lai/chứng từ thanh toán.
    # Dashboard / báo cáo / biểu đồ (dù có số tiền như tổng lợi nhuận) → BỎ QUA
    # im lặng. Ghi dedup stub (status='rejected', deleted) để catchup không
    # gọi Gemini lại cùng ảnh.
    if not bool(parsed.get("is_payment_receipt")):
        logger.info("expense image skip (not receipt): thread=%s sender=%s", thread_id, sender_name)
        _insert_image_skip_stub(zalo_msg_id, image_url, caption, thread_id,
                                sender_uid, sender_name, is_private,
                                note="ảnh không phải biên lai (dashboard/báo cáo)")
        return jsonify({"ok": True, "saved": False, "reason": "not_receipt"})

    amount = int(parsed.get("amount_vnd") or 0)
    if amount <= 0:
        # Là biên lai nhưng không đọc được số tiền → IM LẶNG bỏ qua + dedup stub.
        logger.info("expense image skip (no_amount): thread=%s sender=%s", thread_id, sender_name)
        _insert_image_skip_stub(zalo_msg_id, image_url, caption, thread_id,
                                sender_uid, sender_name, is_private,
                                note="ảnh biên lai không rõ số tiền")
        return jsonify({"ok": True, "saved": False, "reason": "no_amount"})

    category = (parsed.get("category") or "other").strip()
    note = (parsed.get("note") or "").strip() or caption or "Ảnh chi phí"
    occ = parsed.get("occurred_date") or None
    conf = float(parsed.get("confidence") or 0)

    # Coerce năm: Gemini đôi lúc đoán năm cũ khi ảnh chỉ ghi DD/MM
    if occ:
        try:
            from datetime import date as _d
            from tz_utils import now_hcm as _nh
            today_d = _nh().date()
            d = _d.fromisoformat(occ)
            if d.year < today_d.year:
                cand = d.replace(year=today_d.year)
                if cand <= today_d:
                    occ = cand.isoformat()
        except Exception:
            pass

    # Fallback ngày: nếu ảnh không có ngày rõ → dùng ngày hôm nay
    if not occ:
        try:
            from tz_utils import now_hcm
            occ = now_hcm().date().isoformat()
        except Exception:
            from datetime import date as _date
            occ = _date.today().isoformat()

    # Override: tên chủ chuyển NS QC trong note/caption → ads
    if _force_ads_if_owner_transfer(note, caption):
        category = "ads"

    # Whitelist người gửi KHÔNG báo QC (vd chị Huyền — vợ sếp, gửi sao kê
    # cá nhân/VP). Demote ads → other để khỏi lẫn vào báo cáo NS team.
    if category == "ads" and _is_non_ads_sender(sender_name):
        category = "other"
        logger.info("demote ads→other: sender '%s' is whitelisted non-ads", sender_name)

    user_id = _resolve_user_id(sender_uid)
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if zalo_msg_id:
                    cur.execute("SELECT id FROM company_expense_items WHERE zalo_msg_id=%s LIMIT 1", (zalo_msg_id,))
                    if cur.fetchone():
                        conn.commit()
                        return jsonify({"ok": True, "skipped": "duplicate"})
                dup = _find_duplicate_item(cur, sender_uid, amount, within_minutes=10, tolerance=0.05)
        if dup:
            _save_dup_pending(sender_uid, {
                "kind": "image", "existing_id": dup["id"],
                "amount": amount, "category": category, "note": note,
                "occurred_date": occ, "confidence": conf,
                "source_url": image_url, "caption": caption,
                "thread_id": thread_id,
                "zalo_msg_id": zalo_msg_id or "", "is_private": is_private,
            })
            ask = (f"🤔 Ảnh này có vẻ giống khoản #{dup['id']} ({_fmt_money(dup['amount_vnd'])}) "
                   f"vừa ghi. Đây là 1 khoản hay 2 khoản khác nhau ạ?")
            _push_to_zalo(thread_id, ask, sender_uid, sender_name,
                          thread_type="user" if is_private else "group")
            return jsonify({"ok": True, "asked_dup": True, "candidate_for": dup["id"]})
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO company_expense_items
                        (occurred_date, amount_vnd, category, note, source, source_url,
                         confidence, raw_body, zalo_thread_id, zalo_sender_id,
                         zalo_sender_name, user_id, zalo_msg_id, is_private)
                    VALUES (%s, %s, %s, %s, 'image', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (occ, amount, category, note, image_url, conf, caption,
                      thread_id, sender_uid, sender_name, user_id, zalo_msg_id or None, is_private))
                new_id = cur.fetchone()[0]
                conn.commit()
    except Exception as exc:
        logger.error("expense save image error: %s", exc, exc_info=True)
        _push_to_zalo(thread_id,
                      f"😢 Lan lưu ảnh lỗi ({exc}). Báo IT giúp Lan nhé.",
                      sender_uid, sender_name,
                      thread_type="user" if is_private else "group")
        return jsonify({"ok": False, "error": str(exc)}), 500

    cat_lbl = CATEGORY_LABEL.get(category, "📦 Khác")
    short_reply = f"✓ Lan đọc ảnh: {_fmt_money(amount)} · {cat_lbl}"
    if is_private:
        short_reply = f"🔒 Lan đọc ảnh (báo riêng): {_fmt_money(amount)} · {cat_lbl}"
    if conf < 0.7:
        short_reply += f" (⚠ confidence {conf:.0%})"
    _push_to_zalo(thread_id, short_reply, sender_uid, sender_name,
                  thread_type="user" if is_private else "group")

    # Forward chi tiết về inbox (private có flag 🔒)
    _forward_to_inbox(amount, category, note, occ, sender_uid, sender_name,
                      "image", image_url, thread_id, new_id, is_private=is_private)

    return jsonify({"ok": True, "saved": True, "item_id": new_id, "private": is_private})


def register_expense_chat_module(app):
    app.register_blueprint(expense_chat_bp)
    logger.info("expense_chat module registered")
