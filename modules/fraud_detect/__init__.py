"""Module "Bắt sale gian lận" — trang báo cáo + API nhận hội thoại.

LÕI độc lập nguồn (xem store.py). Adapter (Pancake/Webhook) gọi /bat-gian-lan/ingest
hoặc store.ingest_conversation() trực tiếp để đổ data về.
"""
import os
from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from . import store

fraud_bp = Blueprint(
    "fraud_detect", __name__,
    template_folder="templates",
    url_prefix="/bat-gian-lan",
)


def _login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect("/login?next=/bat-gian-lan/")
        role = session.get("role", "staff")
        if role not in ("admin", "superadmin", "manager", "it", "accountant"):
            return "Không có quyền xem trang này.", 403
        return f(*args, **kwargs)
    return wrapped


@fraud_bp.route("/conversations")
@_login_required
def conversations():
    """Xem TẤT CẢ hội thoại đã chụp (không chỉ nghi vấn)."""
    args = request.args
    convs = store.list_conversations(
        page_id=args.get("page_id") or None,
        sale=args.get("sale") or None,
        q=args.get("q") or None,
        only_deleted=(args.get("only_deleted") == "1"),
        date_from=args.get("date_from") or None,
        date_to=args.get("date_to") or None,
    )
    return render_template(
        "fraud_detect/conversations.html",
        convs=convs, cc=store.conv_counts(),
        f_sale=args.get("sale", ""), f_q=args.get("q", ""),
        date_from=args.get("date_from", ""), date_to=args.get("date_to", ""),
        only_deleted=(args.get("only_deleted") == "1"),
    )


@fraud_bp.route("/")
@_login_required
def report():
    args = request.args
    flags = store.list_flags(
        date_from=args.get("date_from") or None,
        date_to=args.get("date_to") or None,
        sale=args.get("sale") or None,
        page_id=args.get("page_id") or None,
        flag_type=args.get("flag_type") or None,
        status=args.get("status", "new"),
    )
    # tổng hội thoại đã CHỤP (để user thấy data có thật dù chưa có cờ)
    cap = {"conv": 0, "msg": 0, "pages": 0}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT count(*), COALESCE(SUM(total_messages),0),
                                  count(DISTINCT page_id)
                             FROM fd_conversations WHERE source='facebook'""")
            r = cur.fetchone()
            cap = {"conv": r[0], "msg": int(r[1]), "pages": r[2]}
    except Exception:
        pass
    return render_template(
        "fraud_detect/report.html",
        flags=flags,
        captured=cap,
        counts=store.counts(),
        f_status=args.get("status", "new"),
        f_type=args.get("flag_type", ""),
        f_sale=args.get("sale", ""),
        date_from=args.get("date_from", ""),
        date_to=args.get("date_to", ""),
    )


@fraud_bp.route("/conv/<path:conv_key>")
@_login_required
def conv_detail(conv_key):
    """JSON toàn bộ hội thoại (bản chụp cpqc) — tin bị xoá vẫn còn, kèm deleted_at."""
    from db import get_conn
    out = {"conv": None, "messages": []}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conv_key, source, page_id, page_name, customer_id, customer_name,
                          COALESCE(customer_avatar,''), assigned_sale, status, total_messages
                     FROM fd_conversations WHERE conv_key=%s""", (conv_key,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "không tìm thấy hội thoại"}), 404
            out["conv"] = {
                "conv_key": r[0], "source": r[1], "page_id": r[2], "page_name": r[3],
                "customer_id": r[4], "customer_name": r[5], "customer_avatar": r[6],
                "sale": r[7], "status": r[8], "total_messages": r[9],
            }
            cur.execute(
                """SELECT msg_key, direction, content, sent_at, deleted_at
                     FROM fd_messages WHERE conv_key=%s
                    ORDER BY sent_at NULLS LAST, msg_key""", (conv_key,))
            for m in cur.fetchall():
                out["messages"].append({
                    "msg_key": m[0], "direction": m[1], "content": m[2],
                    "sent_at": m[3].isoformat() if m[3] else None,
                    "deleted_at": m[4].isoformat() if m[4] else None,
                })
    return jsonify(out)


@fraud_bp.route("/sync-facebook", methods=["POST"])
@_login_required
def sync_facebook():
    """Kéo hội thoại từ Facebook ngay (nút bấm trên trang)."""
    from flask import flash
    try:
        from . import adapter_facebook
        adapter_facebook.sync_all()
        flash("Đã kéo hội thoại từ Facebook xong.", "success")
    except Exception as exc:
        flash(f"Lỗi sync Facebook: {exc}", "danger")
    return redirect(url_for("fraud_detect.report"))


@fraud_bp.route("/flag/<int:fid>/<action>", methods=["POST"])
@_login_required
def review(fid, action):
    new_status = {"confirm": "confirmed", "dismiss": "dismissed", "reopen": "new"}.get(action)
    if new_status:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE fd_flags SET review_status=%s WHERE id=%s", (new_status, fid))
            conn.commit()
    return redirect(request.referrer or url_for("fraud_detect.report"))


@fraud_bp.route("/ingest", methods=["POST"])
def ingest():
    """API nhận hội thoại từ adapter (webhook/Pancake-pusher). Bảo vệ bằng X-MB-Secret.

    Body: { "source": "...", "conversation": {...}, "messages": [...] }
    hoặc batch: { "items": [ {conversation, messages}, ... ] }
    """
    secret = (os.getenv("MB_SECRET", "") or "").strip()
    got = (request.headers.get("X-MB-Secret") or "").strip()
    if secret and got != secret:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not items:
        items = [{"conversation": data.get("conversation") or {}, "messages": data.get("messages") or []}]
    results = []
    for it in items:
        try:
            results.append(store.ingest_conversation(
                it.get("conversation") or {}, it.get("messages") or [],
                source=(data.get("source") or it.get("source") or "api"),
            ))
        except Exception as e:
            results.append({"error": str(e)})
    return jsonify({"ok": True, "results": results})


@fraud_bp.route("/demo", methods=["POST"])
@_login_required
def demo():
    """Nạp dữ liệu MẪU để test thử (mô phỏng 3 kiểu gian lận). Không cần token Pancake."""
    # 1) Sale 'chien' xoá tin: lần đầu có 3 tin, sync lại còn 1 → bắt xoá
    base1 = {"conv_key": "demo:1", "page_id": "DEMO", "page_name": "Thời Trang Demo",
             "customer_name": "Nguyễn Văn A", "assigned_sale": "chien", "status": "open"}
    store.ingest_conversation(base1, [
        {"msg_key": "d1a", "direction": "in", "content": "Shop còn áo này không?"},
        {"msg_key": "d1b", "direction": "out", "content": "Dạ còn, 250k ạ"},
        {"msg_key": "d1c", "direction": "in", "content": "Lấy 1 cái nhé, sđt 09xx"},
    ], source="demo")
    store.ingest_conversation(base1, [
        {"msg_key": "d1a", "direction": "in", "content": "Shop còn áo này không?"},
    ], source="demo")  # d1b, d1c "bị xoá"

    # 2) Sale 'locleo' chặn khách
    store.ingest_conversation(
        {"conv_key": "demo:2", "page_id": "DEMO", "page_name": "Thời Trang Demo",
         "customer_name": "Trần Thị B", "assigned_sale": "locleo", "status": "blocked"},
        [{"msg_key": "d2a", "direction": "in", "content": "Cho hỏi giá ạ"}], source="demo")

    # 3) Sale 'vu' xoá tin sau khi chốt
    base3 = {"conv_key": "demo:3", "page_id": "DEMO", "page_name": "Phụ Kiện Demo",
             "customer_name": "Lê Văn C", "assigned_sale": "vu", "status": "open"}
    store.ingest_conversation(base3, [
        {"msg_key": "d3a", "direction": "in", "content": "Đặt 2 cái"},
        {"msg_key": "d3b", "direction": "out", "content": "Ok em chốt đơn nhé"},
    ], source="demo")
    store.ingest_conversation(base3, [], source="demo")  # xoá sạch tin
    return redirect(url_for("fraud_detect.report"))


@fraud_bp.route("/demo-clear", methods=["POST"])
@_login_required
def demo_clear():
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fd_flags WHERE conv_key LIKE 'demo:%'")
            cur.execute("DELETE FROM fd_messages WHERE conv_key LIKE 'demo:%'")
            cur.execute("DELETE FROM fd_conversations WHERE conv_key LIKE 'demo:%'")
        conn.commit()
    return redirect(url_for("fraud_detect.report"))


def register_fraud_detect(app):
    app.register_blueprint(fraud_bp)
