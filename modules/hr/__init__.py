"""HR Module — Hồ sơ nhân sự (Phase 1, 2026-05-17).

Routes:
  GET  /hr/my-profile             — NV xem + sửa hồ sơ của mình
  POST /hr/my-profile/save        — Lưu draft (NV chưa gửi duyệt)
  POST /hr/my-profile/submit      — Gửi hồ sơ chờ duyệt
  POST /hr/my-profile/upload      — Upload ảnh selfie / CCCD (kèm field=photo|id_front|id_back)

  GET  /hr/employees              — Admin/HR/Kế toán: list hồ sơ tất cả NV (filter status)
  GET  /hr/employee/<id>          — Admin chi tiết hồ sơ 1 NV
  POST /hr/employee/<id>/approve  — Admin duyệt → status=approved
  POST /hr/employee/<id>/reject   — Admin yêu cầu bổ sung (kèm note) → status=rejected
  POST /hr/employee/<id>/upload   — Admin/Kế toán upload CCCD/photo thay NV

Permissions:
  hr_profile  — NV xem hồ sơ của chính mình (mọi role login)
  hr_admin    — Quản lý hồ sơ tất cả NV (admin/manager/it/accountant — duyệt + xem all)

Storage: /mnt/nvme/hr_uploads/{photo,id_card}/<user_id>_<type>_<ts>.jpg
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

hr_bp = Blueprint(
    "hr", __name__,
    template_folder="templates",
    url_prefix="/hr",
)

UPLOAD_ROOT = Path("/mnt/nvme/hr_uploads")
PHOTO_DIR = UPLOAD_ROOT / "photo"
ID_CARD_DIR = UPLOAD_ROOT / "id_card"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
ID_CARD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "heic"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login_required():
    if not session.get("username"):
        return redirect(url_for("auth.login"))
    return None


def _current_user_id() -> Optional[int]:
    uid = session.get("user_id")
    try:
        return int(uid) if uid else None
    except (TypeError, ValueError):
        return None


def _is_hr_admin() -> bool:
    role = (session.get("role") or "").lower()
    if role in {"admin", "superadmin", "manager", "it"}:
        return True
    try:
        from perm_utils import has_permission
        return has_permission(role, "hr_admin")
    except Exception:
        return False


def _can_upload_id_card_for(target_user_id: int) -> bool:
    """Cho phép upload CCCD nếu là chính mình, hoặc admin/kế toán."""
    if target_user_id == _current_user_id():
        return True
    role = (session.get("role") or "").lower()
    if role in {"admin", "superadmin", "manager", "it", "accountant", "ketoan"}:
        return True
    try:
        from perm_utils import has_permission
        return has_permission(role, "hr_admin")
    except Exception:
        return False


def _get_profile(cur, user_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT hp.*, u.username, u.full_name, u.role, u.team_id,
               t.team_name, t.team_code
        FROM users u
        LEFT JOIN hr_profiles hp ON hp.user_id = u.id
        LEFT JOIN teams t ON t.id = u.team_id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    rec = dict(zip(cols, row))
    return rec


def _ensure_profile_row(cur, user_id: int) -> None:
    cur.execute(
        "INSERT INTO hr_profiles (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


def _save_uploaded_file(file_storage, target_user_id: int, kind: str) -> Optional[str]:
    """Lưu file upload. kind ∈ {'photo','id_front','id_back'}. Return relative path."""
    if not file_storage or not file_storage.filename:
        return None
    fname = secure_filename(file_storage.filename)
    ext = (fname.rsplit(".", 1)[-1] if "." in fname else "").lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Định dạng .{ext} không hỗ trợ (chỉ jpg/png/webp/heic)")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    safe_name = f"{target_user_id}_{kind}_{ts}_{rand}.{ext}"
    if kind == "photo":
        target_dir = PHOTO_DIR
    else:
        target_dir = ID_CARD_DIR
    full_path = target_dir / safe_name
    file_storage.save(str(full_path))
    # Trả relative path để lưu DB (KHÔNG lưu absolute path để dễ migrate)
    return str(full_path.relative_to(UPLOAD_ROOT))


def _delete_file_if_exists(rel_path: Optional[str]) -> None:
    if not rel_path:
        return
    try:
        full = UPLOAD_ROOT / rel_path
        if full.is_file() and str(full.resolve()).startswith(str(UPLOAD_ROOT.resolve())):
            full.unlink()
    except Exception as exc:
        logger.warning("delete file %s error: %s", rel_path, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Self-service (NV)
# ─────────────────────────────────────────────────────────────────────────────

@hr_bp.route("/my-profile", methods=["GET"])
def my_profile():
    if (resp := _login_required()):
        return resp
    uid = _current_user_id()
    if not uid:
        flash("Phiên đăng nhập không hợp lệ.", "danger")
        return redirect(url_for("auth.login"))

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_profile_row(cur, uid)
            conn.commit()
            profile = _get_profile(cur, uid)

    return render_template("hr/my_profile.html", profile=profile, is_admin=_is_hr_admin())


@hr_bp.route("/my-profile/save", methods=["POST"])
def my_profile_save():
    if (resp := _login_required()):
        return resp
    uid = _current_user_id()
    if not uid:
        return redirect(url_for("auth.login"))

    fields = _collect_profile_fields(request.form)
    full_name = (request.form.get("full_name") or "").strip()

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_profile_row(cur, uid)
            # Khi save draft, nếu đang ở 'rejected' → reset về 'draft' để NV chỉnh lại
            cur.execute("SELECT status FROM hr_profiles WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            cur_status = row[0] if row else "draft"
            new_status = "draft" if cur_status in ("rejected", "draft") else cur_status
            # Update hr_profiles
            sets = ", ".join(f"{k} = %s" for k in fields.keys())
            params = list(fields.values()) + [new_status, uid]
            cur.execute(
                f"UPDATE hr_profiles SET {sets}, status=%s WHERE user_id = %s",
                params,
            )
            # Update users.full_name nếu khác
            _sync_to_users_and_cc_employees(cur, uid, full_name, fields)
            conn.commit()

    flash("Đã lưu hồ sơ. Khi đầy đủ, bấm 'Gửi duyệt' để admin xem xét.", "success")
    return redirect(url_for("hr.my_profile"))


@hr_bp.route("/my-profile/submit", methods=["POST"])
def my_profile_submit():
    if (resp := _login_required()):
        return resp
    uid = _current_user_id()
    if not uid:
        return redirect(url_for("auth.login"))

    fields = _collect_profile_fields(request.form)
    full_name = (request.form.get("full_name") or "").strip()

    # BƯỚC 1: LƯU TRƯỚC (luôn lưu data NV đã điền, dù còn thiếu) ─────
    from db import get_conn
    file_paths: Dict[str, Any] = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_profile_row(cur, uid)
            sets = ", ".join(f"{k} = %s" for k in fields.keys())
            params = list(fields.values()) + [uid]
            cur.execute(
                f"UPDATE hr_profiles SET {sets} WHERE user_id = %s",
                params,
            )
            _sync_to_users_and_cc_employees(cur, uid, full_name, fields)
            # Lấy file paths để validate ảnh đã upload chưa
            cur.execute(
                "SELECT photo_path, id_card_front_path, id_card_back_path FROM hr_profiles WHERE user_id=%s",
                (uid,),
            )
            r = cur.fetchone() or (None, None, None)
            file_paths = {
                "photo_path": r[0],
                "id_card_front_path": r[1],
                "id_card_back_path": r[2],
            }
            conn.commit()

    # BƯỚC 2: VALIDATE — nếu thiếu thì chỉ báo, KHÔNG đổi status ─────
    missing = _validate_required_for_submit(fields, full_name, file_paths=file_paths)
    if missing:
        flash(f"⚠️ Đã lưu nhưng còn thiếu: {', '.join(missing)}. Bổ sung rồi bấm 'Gửi duyệt' lại.", "warning")
        return redirect(url_for("hr.my_profile"))

    # BƯỚC 3: Đủ thông tin → chuyển status sang submitted ────────────
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hr_profiles
                SET status='submitted', submitted_at=NOW(), admin_note=NULL
                WHERE user_id=%s
                """,
                (uid,),
            )
            conn.commit()

    flash("✓ Đã gửi hồ sơ chờ admin duyệt.", "success")
    return redirect(url_for("hr.my_profile"))


@hr_bp.route("/my-profile/upload", methods=["POST"])
def my_profile_upload():
    if (resp := _login_required()):
        return resp
    uid = _current_user_id()
    if not uid:
        return redirect(url_for("auth.login"))
    return _handle_upload(uid, redirect_to=url_for("hr.my_profile"))


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Admin / HR
# ─────────────────────────────────────────────────────────────────────────────

@hr_bp.route("/employees", methods=["GET"])
def employees_list():
    if (resp := _login_required()):
        return resp
    if not _is_hr_admin():
        flash("Bạn không có quyền vào trang quản lý hồ sơ HR.", "danger")
        return redirect(url_for("hr.my_profile"))

    status_filter = (request.args.get("status") or "").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    try:
        team_filter = int(request.args.get("team") or 0) or None
    except (TypeError, ValueError):
        team_filter = None

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            # NV đã nghỉ vẫn hiện kèm badge "Đã nghỉ" — giống trang chấm công.
            # Nguồn nghỉ việc: users.resigned_at HOẶC cc_employees.resigned_at (chấm công ghi vào đây).
            # Tài khoản inactive KHÔNG có resigned_at thì ẩn luôn.
            sql = """
                SELECT u.id, u.username, u.full_name, u.role::text, u.status::text AS user_status,
                       COALESCE(u.resigned_at, ce.resigned_at) AS resigned_at,
                       t.team_name,
                       COALESCE(hp.status, 'none') AS hr_status,
                       hp.phone, hp.current_address,
                       hp.submitted_at, hp.approved_at, hp.updated_at,
                       (hp.photo_path IS NOT NULL) AS has_photo,
                       (hp.id_card_front_path IS NOT NULL OR hp.id_card_back_path IS NOT NULL) AS has_id_card
                FROM users u
                LEFT JOIN hr_profiles hp ON hp.user_id = u.id
                LEFT JOIN teams t ON t.id = u.team_id
                LEFT JOIN cc_employees ce ON ce.user_id = u.id::text
                WHERE (u.status = 'active' OR u.resigned_at IS NOT NULL OR ce.resigned_at IS NOT NULL)
            """
            params: list = []
            if status_filter and status_filter in {"draft", "submitted", "approved", "rejected", "none"}:
                if status_filter == "none":
                    sql += " AND hp.user_id IS NULL"
                else:
                    sql += " AND hp.status = %s"
                    params.append(status_filter)
            if team_filter:
                sql += " AND u.team_id = %s"
                params.append(team_filter)
            if q:
                sql += " AND (LOWER(u.username) LIKE %s OR LOWER(u.full_name) LIKE %s OR LOWER(hp.phone) LIKE %s)"
                params += [f"%{q}%"] * 3
            # NV đã nghỉ xếp xuống cuối, còn lại giữ thứ tự ưu tiên theo trạng thái hồ sơ
            sql += " ORDER BY (COALESCE(u.resigned_at, ce.resigned_at) IS NOT NULL), CASE COALESCE(hp.status,'none') WHEN 'submitted' THEN 0 WHEN 'rejected' THEN 1 WHEN 'draft' THEN 2 WHEN 'none' THEN 3 ELSE 4 END, u.full_name"
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Stats
            cur.execute(
                """
                SELECT COALESCE(hp.status,'none') AS s, COUNT(*)::int
                FROM users u
                LEFT JOIN hr_profiles hp ON hp.user_id=u.id
                LEFT JOIN cc_employees ce ON ce.user_id = u.id::text
                WHERE (u.status='active' OR u.resigned_at IS NOT NULL OR ce.resigned_at IS NOT NULL)
                GROUP BY 1
                """
            )
            stats = {s: c for s, c in cur.fetchall()}

            # Teams cho dropdown lọc (chỉ team có NV đang hiện)
            cur.execute(
                """
                SELECT t.id, t.team_name, COUNT(u.id)::int AS so_nv
                FROM teams t
                JOIN users u ON u.team_id = t.id
                LEFT JOIN cc_employees ce ON ce.user_id = u.id::text
                WHERE (u.status='active' OR u.resigned_at IS NOT NULL OR ce.resigned_at IS NOT NULL)
                GROUP BY t.id, t.team_name
                ORDER BY t.team_name
                """
            )
            teams_list = [{"id": r[0], "team_name": r[1], "so_nv": r[2]} for r in cur.fetchall()]

    return render_template(
        "hr/employees.html",
        employees=rows, stats=stats, teams_list=teams_list,
        status_filter=status_filter, q=q, team_filter=team_filter,
    )


@hr_bp.route("/employee/<int:uid>", methods=["GET"])
def employee_detail(uid: int):
    if (resp := _login_required()):
        return resp
    if not _is_hr_admin() and uid != _current_user_id():
        flash("Bạn không có quyền xem hồ sơ của người khác.", "danger")
        return redirect(url_for("hr.my_profile"))

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_profile_row(cur, uid)
            conn.commit()
            profile = _get_profile(cur, uid)

    if not profile:
        flash("Không tìm thấy nhân viên.", "danger")
        return redirect(url_for("hr.employees_list"))

    return render_template("hr/employee_detail.html", profile=profile, is_admin=_is_hr_admin())


@hr_bp.route("/employee/<int:uid>/approve", methods=["POST"])
def employee_approve(uid: int):
    if (resp := _login_required()):
        return resp
    if not _is_hr_admin():
        abort(403)
    admin_id = _current_user_id()

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hr_profiles
                SET status='approved', approved_at=NOW(), approved_by=%s, admin_note=NULL
                WHERE user_id=%s
                """,
                (admin_id, uid),
            )
            conn.commit()
    flash("✓ Đã duyệt hồ sơ.", "success")
    return redirect(url_for("hr.employee_detail", uid=uid))


@hr_bp.route("/employee/<int:uid>/reject", methods=["POST"])
def employee_reject(uid: int):
    if (resp := _login_required()):
        return resp
    if not _is_hr_admin():
        abort(403)
    admin_id = _current_user_id()
    note = (request.form.get("admin_note") or "").strip()
    if not note:
        flash("Vui lòng ghi rõ lý do yêu cầu bổ sung.", "warning")
        return redirect(url_for("hr.employee_detail", uid=uid))

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hr_profiles
                SET status='rejected', rejected_at=NOW(), rejected_by=%s, admin_note=%s
                WHERE user_id=%s
                """,
                (admin_id, note, uid),
            )
            conn.commit()
    flash("Đã yêu cầu NV bổ sung thông tin.", "info")
    return redirect(url_for("hr.employee_detail", uid=uid))


@hr_bp.route("/employee/<int:uid>/upload", methods=["POST"])
def employee_upload(uid: int):
    if (resp := _login_required()):
        return resp
    if not _can_upload_id_card_for(uid):
        abort(403)
    return _handle_upload(uid, redirect_to=url_for("hr.employee_detail", uid=uid))


@hr_bp.route("/file/<path:rel_path>", methods=["GET"])
def serve_file(rel_path: str):
    """Serve file upload. Auth: chỉ chính chủ + hr_admin xem được."""
    if (resp := _login_required()):
        return resp
    full = (UPLOAD_ROOT / rel_path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT.resolve())):
        abort(403)
    if not full.is_file():
        abort(404)
    # Parse user_id từ filename: "<uid>_<kind>_<ts>_<rand>.<ext>"
    fname = full.name
    parts = fname.split("_", 2)
    try:
        owner_uid = int(parts[0])
    except (ValueError, IndexError):
        owner_uid = 0
    if owner_uid != _current_user_id() and not _is_hr_admin():
        abort(403)
    return send_file(str(full))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

PROFILE_FIELDS = [
    "dob", "gender", "id_card_number",
    "hometown_address", "current_address",
    "phone", "personal_email",
    "emergency_contact_name", "emergency_contact_phone",
    "hire_date",
]

# Field bắt buộc khi submit (text fields trong form)
REQUIRED_FOR_SUBMIT = [
    ("full_name", "Họ và tên"),
    ("phone", "Số điện thoại"),
    ("current_address", "Nơi ở hiện tại"),
    ("dob", "Ngày sinh"),
    ("id_card_number", "Số CCCD"),
    ("emergency_contact_name", "Họ tên người thân (liên hệ khẩn cấp)"),
    ("emergency_contact_phone", "SĐT người thân"),
]

# File ảnh bắt buộc khi submit — kiểm tra qua DB
REQUIRED_FILES_FOR_SUBMIT = [
    ("photo_path", "Ảnh chân dung (selfie)"),
    ("id_card_front_path", "Ảnh CCCD mặt trước"),
    ("id_card_back_path", "Ảnh CCCD mặt sau"),
]


def _collect_profile_fields(form) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in PROFILE_FIELDS:
        v = (form.get(k) or "").strip()
        if k in ("dob", "hire_date"):
            out[k] = v if v else None
        elif k == "gender":
            out[k] = v.lower() if v.lower() in ("male", "female", "other") else None
        else:
            out[k] = v or None
    return out


def _sync_to_users_and_cc_employees(cur, user_id: int, full_name: str, fields: Dict[str, Any]) -> None:
    """Đồng bộ thông tin HR → users + cc_employees để mọi nơi hiển thị nhất quán.

    - users.full_name / phone / email ← HR
    - cc_employees.full_name / phone   ← HR (nếu user đã có row trong cc_employees)
    """
    phone = fields.get("phone") or None
    email = fields.get("personal_email") or None
    # users: full_name + phone + email
    sets, params = [], []
    if full_name:
        sets.append("full_name = %s"); params.append(full_name)
    if phone is not None:
        sets.append("phone = %s"); params.append(phone)
    if email is not None:
        sets.append("email = %s"); params.append(email)
    if sets:
        params.append(user_id)
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", params)

    # cc_employees: chỉ update nếu user đã có row (không tự tạo)
    cur.execute(
        """
        UPDATE cc_employees
        SET full_name = COALESCE(NULLIF(%s, ''), full_name),
            phone     = COALESCE(NULLIF(%s, ''), phone)
        WHERE user_id = %s
        """,
        (full_name, phone or "", str(user_id)),
    )


def _validate_required_for_submit(fields: Dict[str, Any], full_name: str,
                                  file_paths: Optional[Dict[str, Any]] = None) -> List[str]:
    """Kiểm tra các field/file bắt buộc trước khi submit.

    file_paths: dict {photo_path, id_card_front_path, id_card_back_path} từ DB.
    """
    missing: List[str] = []
    for k, label in REQUIRED_FOR_SUBMIT:
        if k == "full_name":
            if not full_name.strip():
                missing.append(label)
        elif not fields.get(k):
            missing.append(label)
    if file_paths is not None:
        for k, label in REQUIRED_FILES_FOR_SUBMIT:
            if not file_paths.get(k):
                missing.append(label)
    return missing


def _handle_upload(target_user_id: int, redirect_to: str):
    kind = (request.form.get("kind") or "").strip().lower()
    if kind not in {"photo", "id_front", "id_back"}:
        flash("Loại file không hợp lệ.", "danger")
        return redirect(redirect_to)
    if kind != "photo" and not _can_upload_id_card_for(target_user_id):
        abort(403)

    f = request.files.get("file")
    if not f or not f.filename:
        flash("Chưa chọn file.", "warning")
        return redirect(redirect_to)
    try:
        rel_path = _save_uploaded_file(f, target_user_id, kind)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(redirect_to)
    if not rel_path:
        flash("Upload thất bại.", "danger")
        return redirect(redirect_to)

    field_map = {
        "photo": "photo_path",
        "id_front": "id_card_front_path",
        "id_back": "id_card_back_path",
    }
    col = field_map[kind]

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_profile_row(cur, target_user_id)
            # Xoá file cũ (nếu có) để tránh tích lũy
            cur.execute(f"SELECT {col} FROM hr_profiles WHERE user_id=%s", (target_user_id,))
            old_path = (cur.fetchone() or (None,))[0]
            _delete_file_if_exists(old_path)
            # Lưu path mới
            cur.execute(
                f"UPDATE hr_profiles SET {col}=%s WHERE user_id=%s",
                (rel_path, target_user_id),
            )
            conn.commit()

    flash(f"✓ Đã upload {kind}.", "success")
    return redirect(redirect_to)


# ─────────────────────────────────────────────────────────────────────────────
# HỢP ĐỒNG LAO ĐỘNG — Phase 2 (2026-06-09)
# ─────────────────────────────────────────────────────────────────────────────

CONTRACT_DIR = UPLOAD_ROOT / "contracts"
CONTRACT_DIR.mkdir(parents=True, exist_ok=True)

CONTRACT_TYPE_LABELS = {
    "xac_dinh": "Xác định thời hạn",
    "khong_xac_dinh": "Không xác định thời hạn",
    "thu_viec": "Thử việc",
    "thoi_vu": "Thời vụ",
}

CONTRACT_STATUS_LABELS = {
    "draft": "Bản nháp",
    "pending_sign": "Chờ ký",
    "pending_employee_sign": "Chờ NV ký",
    "pending_company_sign": "Chờ cty ký",
    "signed": "Đã ký đủ",
    "terminated": "Đã chấm dứt",
    "expired": "Hết hạn",
}


def _is_contract_admin() -> bool:
    """Quyền tạo/sửa HĐLĐ: admin/superadmin/manager/it/accountant."""
    role = (session.get("role") or "").lower()
    return role in {"admin", "superadmin", "manager", "it", "accountant", "ketoan"}


def _to_int_or_none(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", "").replace(".", "").replace(" ", ""))
    except Exception:
        return None


# ── COMPANIES (bên A) ─────────────────────────────────────────────────────────

@hr_bp.route("/companies", methods=["GET"])
def companies_list():
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, company_name, tax_code, address, legal_rep_name,
                       legal_rep_title, phone, email, is_default, is_active,
                       (SELECT COUNT(*) FROM hr_contracts c WHERE c.company_id = hc.id) AS contract_count
                FROM hr_companies hc
                ORDER BY is_default DESC, is_active DESC, company_name
                """
            )
            cols = [d[0] for d in cur.description]
            companies = [dict(zip(cols, r)) for r in cur.fetchall()]
    return render_template("hr/companies.html", companies=companies)


@hr_bp.route("/companies/new", methods=["GET", "POST"])
@hr_bp.route("/companies/<int:cid>/edit", methods=["GET", "POST"])
def company_edit(cid: Optional[int] = None):
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)
    from db import get_conn
    company = None
    if cid:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM hr_companies WHERE id=%s", (cid,))
                row = cur.fetchone()
                if not row:
                    flash("Không tìm thấy công ty.", "danger")
                    return redirect(url_for("hr.companies_list"))
                cols = [d[0] for d in cur.description]
                company = dict(zip(cols, row))

    if request.method == "POST":
        f = request.form
        data = {
            "company_name": (f.get("company_name") or "").strip(),
            "tax_code": (f.get("tax_code") or "").strip() or None,
            "address": (f.get("address") or "").strip() or None,
            "legal_rep_name": (f.get("legal_rep_name") or "").strip() or None,
            "legal_rep_title": (f.get("legal_rep_title") or "").strip() or None,
            "legal_rep_id_card": (f.get("legal_rep_id_card") or "").strip() or None,
            "legal_rep_dob": (f.get("legal_rep_dob") or "").strip() or None,
            "legal_rep_address": (f.get("legal_rep_address") or "").strip() or None,
            "signing_city": (f.get("signing_city") or "").strip() or None,
            "phone": (f.get("phone") or "").strip() or None,
            "email": (f.get("email") or "").strip() or None,
            "bank_account": (f.get("bank_account") or "").strip() or None,
            "bank_name": (f.get("bank_name") or "").strip() or None,
            "is_default": bool(f.get("is_default")),
            "is_active": bool(f.get("is_active", "1")),
            "notes": (f.get("notes") or "").strip() or None,
        }
        if not data["company_name"]:
            flash("Vui lòng nhập tên công ty.", "warning")
            return render_template("hr/company_edit.html", company=company or data)

        with get_conn() as conn:
            with conn.cursor() as cur:
                # Nếu set is_default=True, unset các row khác
                if data["is_default"]:
                    cur.execute("UPDATE hr_companies SET is_default=FALSE")
                if cid:
                    sets = ", ".join(f"{k}=%s" for k in data.keys())
                    cur.execute(
                        f"UPDATE hr_companies SET {sets} WHERE id=%s",
                        list(data.values()) + [cid],
                    )
                else:
                    cols = list(data.keys()) + ["created_by"]
                    vals = list(data.values()) + [_current_user_id()]
                    placeholders = ", ".join(["%s"] * len(cols))
                    cur.execute(
                        f"INSERT INTO hr_companies ({', '.join(cols)}) VALUES ({placeholders})",
                        vals,
                    )
                conn.commit()
        flash("✓ Đã lưu thông tin công ty.", "success")
        return redirect(url_for("hr.companies_list"))

    return render_template("hr/company_edit.html", company=company)


# ── CONTRACTS (per-employee list + new) ───────────────────────────────────────

@hr_bp.route("/employee/<int:uid>/contracts", methods=["GET"])
def employee_contracts(uid: int):
    if (resp := _login_required()):
        return resp
    # Cho NV xem HĐ của chính mình; admin xem tất cả
    if not _is_contract_admin() and uid != _current_user_id():
        abort(403)

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            profile = _get_profile(cur, uid)
            if not profile:
                flash("Không tìm thấy nhân viên.", "danger")
                return redirect(url_for("hr.employees_list"))
            cur.execute(
                """
                SELECT c.id, c.user_id, c.contract_number, c.contract_type, c.status,
                       c.start_date, c.end_date, c.position, c.salary_base,
                       c.docx_path, c.signed_file_path, c.final_docx_path,
                       c.signed_employee, c.signed_employee_at,
                       c.signed_company, c.signed_company_at,
                       c.created_at, hc.company_name
                FROM hr_contracts c
                JOIN hr_companies hc ON hc.id = c.company_id
                WHERE c.user_id = %s
                ORDER BY c.created_at DESC
                """,
                (uid,),
            )
            cols = [d[0] for d in cur.description]
            contracts = [dict(zip(cols, r)) for r in cur.fetchall()]
    return render_template(
        "hr/employee_contracts.html",
        profile=profile, contracts=contracts,
        is_admin=_is_contract_admin(),
        type_labels=CONTRACT_TYPE_LABELS,
        status_labels=CONTRACT_STATUS_LABELS,
    )


@hr_bp.route("/employee/<int:uid>/contract/new", methods=["GET", "POST"])
def contract_new(uid: int):
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            profile = _get_profile(cur, uid)
            if not profile:
                flash("Không tìm thấy nhân viên.", "danger")
                return redirect(url_for("hr.employees_list"))
            cur.execute(
                "SELECT id, company_name, tax_code, is_default FROM hr_companies "
                "WHERE is_active=TRUE ORDER BY is_default DESC, company_name"
            )
            companies = [
                {"id": r[0], "company_name": r[1], "tax_code": r[2], "is_default": r[3]}
                for r in cur.fetchall()
            ]

    if not companies:
        flash("Chưa có công ty nào. Vui lòng thêm công ty trước khi tạo HĐLĐ.", "warning")
        return redirect(url_for("hr.companies_list"))

    if request.method == "POST":
        return _create_contract(uid, profile)

    # Suggest số HĐ tiếp theo: HĐLĐ-YYYY/NNN
    today = datetime.now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM hr_contracts WHERE EXTRACT(YEAR FROM created_at)=%s",
                (today.year,),
            )
            seq = (cur.fetchone()[0] or 0) + 1
    suggested_no = f"HĐLĐ-{today.year}/{seq:03d}"

    return render_template(
        "hr/contract_new.html",
        profile=profile, companies=companies,
        suggested_no=suggested_no,
        type_labels=CONTRACT_TYPE_LABELS,
        today=today.strftime("%Y-%m-%d"),
    )


def _create_contract(uid: int, profile: Dict[str, Any]):
    from db import get_conn
    f = request.form
    company_id = f.get("company_id")
    if not company_id:
        flash("Vui lòng chọn công ty.", "warning")
        return redirect(url_for("hr.contract_new", uid=uid))
    try:
        company_id = int(company_id)
    except (TypeError, ValueError):
        abort(400)

    contract_type = (f.get("contract_type") or "").strip()
    if contract_type not in CONTRACT_TYPE_LABELS:
        flash("Loại hợp đồng không hợp lệ.", "warning")
        return redirect(url_for("hr.contract_new", uid=uid))

    contract_number = (f.get("contract_number") or "").strip()
    start_date = (f.get("start_date") or "").strip() or None
    end_date = (f.get("end_date") or "").strip() or None
    if contract_type == "khong_xac_dinh":
        end_date = None  # luôn rỗng

    data = {
        "user_id": uid,
        "company_id": company_id,
        "contract_number": contract_number,
        "contract_type": contract_type,
        "start_date": start_date,
        "end_date": end_date,
        "probation_months": _to_int_or_none(f.get("probation_months")),
        "position": (f.get("position") or "").strip() or None,
        "department": (f.get("department") or "").strip() or None,
        "workplace": (f.get("workplace") or "").strip() or None,
        "work_hours": (f.get("work_hours") or "").strip() or None,
        "salary_base": _to_int_or_none(f.get("salary_base")),
        "salary_allowance_meal": _to_int_or_none(f.get("salary_allowance_meal")),
        "salary_allowance_fuel": _to_int_or_none(f.get("salary_allowance_fuel")),
        "salary_allowance_phone": _to_int_or_none(f.get("salary_allowance_phone")),
        "salary_allowance_other": _to_int_or_none(f.get("salary_allowance_other")),
        "salary_other_note": (f.get("salary_other_note") or "").strip() or None,
        "notes": (f.get("notes") or "").strip() or None,
        # Field mới khớp template
        "job_duties": (f.get("job_duties") or "").strip() or None,
        "equipment_provided": (f.get("equipment_provided") or "").strip() or None,
        "pay_day": _to_int_or_none(f.get("pay_day")),
        "pay_method": (f.get("pay_method") or "").strip() or None,
        "transport_mode": (f.get("transport_mode") or "").strip() or None,
        "signing_city": (f.get("signing_city") or "").strip() or None,
        # Snapshot NV (lấy từ profile + form override)
        "snapshot_employee_name": profile.get("full_name"),
        "snapshot_employee_dob": profile.get("dob"),
        "snapshot_employee_gender": profile.get("gender"),
        "snapshot_employee_id_card": profile.get("id_card_number"),
        "snapshot_employee_address": profile.get("current_address"),
        "snapshot_employee_phone": profile.get("phone"),
        "snapshot_employee_hometown": profile.get("hometown_address"),
        # Snapshot field mới (nhập trong form)
        "snapshot_place_of_birth": (f.get("place_of_birth") or "").strip() or profile.get("hometown_address"),
        "snapshot_id_card_date": (f.get("id_card_issued_date") or "").strip() or None,
        "snapshot_id_card_place": (f.get("id_card_issued_place") or "").strip() or None,
        "snapshot_nationality": (f.get("nationality") or "Việt Nam").strip(),
        "snapshot_work_permit_no": (f.get("work_permit_no") or "").strip() or None,
        "snapshot_work_permit_date": (f.get("work_permit_date") or "").strip() or None,
        "snapshot_work_permit_place": (f.get("work_permit_place") or "").strip() or None,
        "created_by": _current_user_id(),
        "status": "draft",
    }

    if not contract_number:
        flash("Vui lòng nhập số hợp đồng.", "warning")
        return redirect(url_for("hr.contract_new", uid=uid))

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Lấy info công ty cho DOCX
            cur.execute("SELECT * FROM hr_companies WHERE id=%s", (company_id,))
            row = cur.fetchone()
            if not row:
                flash("Công ty không tồn tại.", "danger")
                return redirect(url_for("hr.contract_new", uid=uid))
            ccols = [d[0] for d in cur.description]
            company = dict(zip(ccols, row))

            # INSERT contract
            cols = list(data.keys())
            vals = list(data.values())
            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO hr_contracts ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
                vals,
            )
            contract_id = cur.fetchone()[0]

            # Generate DOCX
            try:
                from .contracts import generate_contract_docx
                safe_no = "".join(
                    c if c.isalnum() or c in "-_." else "_"
                    for c in contract_number
                )
                rel_path = f"contracts/{uid}/{contract_id}_{safe_no}.docx"
                full_path = UPLOAD_ROOT / rel_path
                generate_contract_docx(
                    contract={**data, "contract_number": contract_number},
                    company=company,
                    employee={
                        "full_name": profile.get("full_name"),
                        "dob": profile.get("dob"),
                        "gender": profile.get("gender"),
                        "id_card_number": profile.get("id_card_number"),
                        "hometown_address": profile.get("hometown_address"),
                        "current_address": profile.get("current_address"),
                        "phone": profile.get("phone"),
                        "personal_email": profile.get("personal_email"),
                        # Mới từ form
                        "place_of_birth": data.get("snapshot_place_of_birth"),
                        "id_card_issued_date": data.get("snapshot_id_card_date"),
                        "id_card_issued_place": data.get("snapshot_id_card_place"),
                        "nationality": data.get("snapshot_nationality"),
                        "work_permit_no": data.get("snapshot_work_permit_no"),
                        "work_permit_date": data.get("snapshot_work_permit_date"),
                        "work_permit_place": data.get("snapshot_work_permit_place"),
                    },
                    out_path=full_path,
                )
                cur.execute(
                    "UPDATE hr_contracts SET docx_path=%s WHERE id=%s",
                    (rel_path, contract_id),
                )
                _log_contract_event(cur, contract_id, "created", _current_user_id(),
                                    f"contract_number={contract_number}")
            except Exception as exc:
                logger.exception("generate_contract_docx fail")
                flash(f"Tạo file DOCX lỗi: {exc}", "danger")
                conn.rollback()
                return redirect(url_for("hr.contract_new", uid=uid))

            conn.commit()
    flash("✓ Đã tạo HĐLĐ và file Word.", "success")
    return redirect(url_for("hr.employee_contracts", uid=uid))


@hr_bp.route("/contract/<int:cid>/download", methods=["GET"])
def contract_download(cid: int):
    if (resp := _login_required()):
        return resp
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, contract_number, docx_path FROM hr_contracts WHERE id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            owner_uid, no, rel_path = row
    if not rel_path:
        flash("Hợp đồng này chưa có file.", "warning")
        return redirect(url_for("hr.employee_contracts", uid=owner_uid))
    if not _is_contract_admin() and owner_uid != _current_user_id():
        abort(403)
    full = (UPLOAD_ROOT / rel_path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT.resolve())) or not full.is_file():
        abort(404)
    download_name = f"{no.replace('/', '_')}.docx"
    return send_file(str(full), as_attachment=True, download_name=download_name)


# ── Upload bản đã ký số (kế toán/cty) ─────────────────────────────────────────

ALLOWED_SIGNED_EXT = {"docx", "doc", "pdf"}
SIGNED_DIR = UPLOAD_ROOT / "contracts_signed"
SIGNED_DIR.mkdir(parents=True, exist_ok=True)

EMP_SIG_DIR = UPLOAD_ROOT / "contract_employee_sigs"
EMP_SIG_DIR.mkdir(parents=True, exist_ok=True)


def _regenerate_final_docx(cur, contract_id: int) -> Optional[str]:
    """Sinh lại file DOCX 'final' có chèn PNG chữ ký NV (và cty nếu có).

    Lấy đầy đủ contract + company + profile (snapshot field) → re-render.
    Trả về rel_path file mới, hoặc None nếu fail.
    """
    cur.execute(
        """
        SELECT c.*, hc.company_name, hc.tax_code, hc.address as company_address,
               hc.legal_rep_name, hc.legal_rep_title, hc.legal_rep_id_card,
               hc.legal_rep_dob, hc.legal_rep_address, hc.signing_city as company_signing_city,
               u.full_name as employee_full_name
        FROM hr_contracts c
        JOIN hr_companies hc ON hc.id = c.company_id
        JOIN users u ON u.id = c.user_id
        WHERE c.id=%s
        """,
        (contract_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    rec = dict(zip(cols, row))

    contract = {
        "contract_number": rec["contract_number"],
        "contract_type": rec["contract_type"],
        "start_date": rec["start_date"],
        "end_date": rec["end_date"],
        "position": rec["position"],
        "department": rec["department"],
        "workplace": rec["workplace"],
        "work_hours": rec["work_hours"],
        "job_duties": rec.get("job_duties"),
        "equipment_provided": rec.get("equipment_provided"),
        "pay_day": rec.get("pay_day"),
        "pay_method": rec.get("pay_method"),
        "transport_mode": rec.get("transport_mode"),
        "signing_city": rec.get("signing_city"),
        "salary_base": rec["salary_base"],
        "salary_allowance_meal": rec["salary_allowance_meal"],
        "salary_allowance_fuel": rec["salary_allowance_fuel"],
        "salary_allowance_phone": rec["salary_allowance_phone"],
        "salary_allowance_other": rec["salary_allowance_other"],
        "salary_other_note": rec["salary_other_note"],
        "notes": rec["notes"],
    }
    company = {
        "company_name": rec["company_name"],
        "tax_code": rec["tax_code"],
        "address": rec["company_address"],
        "legal_rep_name": rec["legal_rep_name"],
        "legal_rep_title": rec["legal_rep_title"],
        "legal_rep_id_card": rec["legal_rep_id_card"],
        "legal_rep_dob": rec["legal_rep_dob"],
        "legal_rep_address": rec["legal_rep_address"],
        "signing_city": rec["company_signing_city"],
    }
    # Dùng snapshot field từ HĐ (để file final khớp dữ liệu lúc tạo)
    employee = {
        "full_name": rec.get("snapshot_employee_name") or rec["employee_full_name"],
        "dob": rec.get("snapshot_employee_dob"),
        "gender": rec.get("snapshot_employee_gender"),
        "id_card_number": rec.get("snapshot_employee_id_card"),
        "hometown_address": rec.get("snapshot_employee_hometown"),
        "current_address": rec.get("snapshot_employee_address"),
        "phone": rec.get("snapshot_employee_phone"),
        "place_of_birth": rec.get("snapshot_place_of_birth"),
        "id_card_issued_date": rec.get("snapshot_id_card_date"),
        "id_card_issued_place": rec.get("snapshot_id_card_place"),
        "nationality": rec.get("snapshot_nationality"),
        "work_permit_no": rec.get("snapshot_work_permit_no"),
        "work_permit_date": rec.get("snapshot_work_permit_date"),
        "work_permit_place": rec.get("snapshot_work_permit_place"),
    }

    emp_sig = rec.get("signed_employee_sig_path")
    comp_sig = rec.get("signed_company_sig_path")
    emp_sig_path = (UPLOAD_ROOT / emp_sig) if emp_sig else None
    comp_sig_path = (UPLOAD_ROOT / comp_sig) if comp_sig else None

    uid = rec["user_id"]
    safe_no = "".join(c if c.isalnum() else "_" for c in (rec["contract_number"] or "X"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rel_path = f"contracts/{uid}/{contract_id}_{safe_no}_FINAL_{ts}.docx"
    full_path = UPLOAD_ROOT / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from .contracts import generate_contract_docx
        generate_contract_docx(
            contract=contract, company=company, employee=employee,
            out_path=full_path,
            employee_signature_path=emp_sig_path,
            company_signature_path=comp_sig_path,
        )
        # Xoá file final cũ
        cur.execute("SELECT final_docx_path FROM hr_contracts WHERE id=%s", (contract_id,))
        old = (cur.fetchone() or (None,))[0]
        if old and old != rel_path:
            _delete_file_if_exists(old)
        return rel_path
    except Exception:
        logger.exception("_regenerate_final_docx fail for contract %s", contract_id)
        return None


def _log_contract_event(cur, contract_id: int, event_type: str,
                        actor_uid: Optional[int] = None, detail: str = ""):
    cur.execute(
        """
        INSERT INTO hr_contract_events
            (contract_id, event_type, actor_user_id, actor_ip, actor_ua, detail)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (contract_id, event_type, actor_uid,
         (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:50],
         (request.headers.get("User-Agent") or "")[:500],
         detail or None),
    )


@hr_bp.route("/contract/<int:cid>/upload-signed", methods=["POST"])
def contract_upload_signed(cid: int):
    """Kế toán upload file đã ký số (USB token trong Word/PDF)."""
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)

    f = request.files.get("file")
    if not f or not f.filename:
        flash("Chưa chọn file.", "warning")
        return redirect(request.referrer or url_for("hr.companies_list"))

    fname = secure_filename(f.filename)
    ext = (fname.rsplit(".", 1)[-1] if "." in fname else "").lower()
    if ext not in ALLOWED_SIGNED_EXT:
        flash(f"Định dạng .{ext} không hỗ trợ (chỉ .docx/.doc/.pdf).", "danger")
        return redirect(request.referrer or url_for("hr.companies_list"))

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, contract_number, signed_file_path FROM hr_contracts WHERE id=%s", (cid,))
            row = cur.fetchone()
            if not row:
                abort(404)
            uid, contract_no, old_path = row

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rand = secrets.token_hex(3)
            safe_no = "".join(c if c.isalnum() else "_" for c in contract_no)
            rel_path = f"contracts_signed/{uid}/{cid}_{safe_no}_{ts}_{rand}.{ext}"
            full_path = UPLOAD_ROOT / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            f.save(str(full_path))

            # Xóa file cũ nếu có
            if old_path:
                _delete_file_if_exists(old_path)

            actor = _current_user_id()
            # Đánh dấu cty đã ký; nếu NV đã ký trước → final 'signed', ngược lại chờ NV
            cur.execute(
                """
                UPDATE hr_contracts
                SET signed_file_path=%s,
                    signed_file_uploaded_at=NOW(),
                    signed_file_uploaded_by=%s,
                    signed_company=TRUE,
                    signed_company_at=COALESCE(signed_company_at, NOW()),
                    signed_company_by=COALESCE(signed_company_by, %s),
                    status=CASE
                        WHEN signed_employee = TRUE THEN 'signed'
                        ELSE 'pending_employee_sign'
                    END
                WHERE id=%s
                """,
                (rel_path, actor, actor, cid),
            )
            _log_contract_event(cur, cid, "uploaded_signed", actor,
                                f"file={fname} ext={ext}")
            conn.commit()
    flash("✓ Đã upload bản công ty đã ký. NV có thể vào ký tay xác nhận.", "success")
    return redirect(url_for("hr.employee_contracts", uid=uid))


@hr_bp.route("/contract/<int:cid>/download-signed", methods=["GET"])
def contract_download_signed(cid: int):
    if (resp := _login_required()):
        return resp
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, contract_number, signed_file_path FROM hr_contracts WHERE id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            owner_uid, no, rel_path = row
    if not rel_path:
        abort(404)
    if not _is_contract_admin() and owner_uid != _current_user_id():
        abort(403)
    full = (UPLOAD_ROOT / rel_path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT.resolve())) or not full.is_file():
        abort(404)
    ext = full.suffix.lstrip(".")
    download_name = f"{no.replace('/', '_')}_DA_KY.{ext}"
    return send_file(str(full), as_attachment=True, download_name=download_name)


# ── NV ký tay trên web (signature pad) ────────────────────────────────────────

@hr_bp.route("/contract/<int:cid>/sign", methods=["GET", "POST"])
def contract_sign_employee(cid: int):
    """Trang NV ký xác nhận hợp đồng bằng signature pad."""
    if (resp := _login_required()):
        return resp
    uid = _current_user_id()

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, hc.company_name
                FROM hr_contracts c
                JOIN hr_companies hc ON hc.id = c.company_id
                WHERE c.id=%s
                """,
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            cols = [d[0] for d in cur.description]
            contract = dict(zip(cols, row))

    # Chỉ NV chính chủ mới ký được
    if contract["user_id"] != uid:
        flash("Bạn chỉ có thể ký hợp đồng của chính mình.", "danger")
        return redirect(url_for("hr.my_profile"))

    if contract["signed_employee"]:
        flash("Bạn đã ký hợp đồng này rồi.", "info")
        return redirect(url_for("hr.employee_contracts", uid=uid))

    if request.method == "POST":
        return _save_employee_signature(cid, uid, contract)

    return render_template(
        "hr/contract_sign.html",
        contract=contract,
        type_labels=CONTRACT_TYPE_LABELS,
    )


def _save_employee_signature(cid: int, uid: int, contract: Dict[str, Any]):
    """Nhận data:image/png;base64,... từ signature pad, lưu PNG."""
    import base64
    data_url = (request.form.get("signature_data") or "").strip()
    confirm = request.form.get("confirm_agree")
    if not confirm:
        flash("Vui lòng tích vào ô đồng ý trước khi ký.", "warning")
        return redirect(url_for("hr.contract_sign_employee", cid=cid))
    if not data_url.startswith("data:image/png;base64,"):
        flash("Chữ ký không hợp lệ. Vui lòng vẽ lại.", "warning")
        return redirect(url_for("hr.contract_sign_employee", cid=cid))

    try:
        b64 = data_url.split(",", 1)[1]
        png_bytes = base64.b64decode(b64)
        if len(png_bytes) < 200 or len(png_bytes) > 2 * 1024 * 1024:
            raise ValueError("Kích thước chữ ký không hợp lệ")
    except Exception as exc:
        flash(f"Chữ ký không đọc được: {exc}", "danger")
        return redirect(url_for("hr.contract_sign_employee", cid=cid))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    rel_path = f"contract_employee_sigs/{uid}/{cid}_sig_{ts}_{rand}.png"
    full_path = UPLOAD_ROOT / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(png_bytes)

    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:50]

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hr_contracts
                SET signed_employee=TRUE,
                    signed_employee_at=NOW(),
                    signed_employee_ip=%s,
                    signed_employee_sig_path=%s,
                    status=CASE
                        WHEN signed_company = TRUE THEN 'signed'
                        ELSE 'pending_company_sign'
                    END
                WHERE id=%s
                """,
                (ip, rel_path, cid),
            )
            _log_contract_event(cur, cid, "signed_employee", uid,
                                f"ip={ip} bytes={len(png_bytes)}")

            # Sinh lại file DOCX final có chèn PNG chữ ký NV
            final_rel = _regenerate_final_docx(cur, cid)
            if final_rel:
                cur.execute(
                    "UPDATE hr_contracts SET final_docx_path=%s WHERE id=%s",
                    (final_rel, cid),
                )
            conn.commit()

    flash("✓ Đã ký hợp đồng thành công. Cảm ơn bạn!", "success")
    return redirect(url_for("hr.employee_contracts", uid=uid))


@hr_bp.route("/contract/<int:cid>/signature.png", methods=["GET"])
def contract_employee_signature(cid: int):
    """Serve PNG chữ ký NV — chỉ admin + chính chủ xem được."""
    if (resp := _login_required()):
        return resp
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, signed_employee_sig_path FROM hr_contracts WHERE id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            owner_uid, rel_path = row
    if not rel_path:
        abort(404)
    if not _is_contract_admin() and owner_uid != _current_user_id():
        abort(403)
    full = (UPLOAD_ROOT / rel_path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT.resolve())) or not full.is_file():
        abort(404)
    return send_file(str(full), mimetype="image/png")


@hr_bp.route("/contract/<int:cid>/regenerate-final", methods=["POST"])
def contract_regenerate_final(cid: int):
    """Admin sinh lại file final (chèn PNG chữ ký NV) cho HĐ đã ký nhưng chưa có final."""
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, signed_employee FROM hr_contracts WHERE id=%s", (cid,))
            row = cur.fetchone()
            if not row:
                abort(404)
            uid, signed = row
            if not signed:
                flash("HĐ này NV chưa ký, không có gì để sinh.", "warning")
                return redirect(url_for("hr.employee_contracts", uid=uid))
            new_path = _regenerate_final_docx(cur, cid)
            if new_path:
                cur.execute("UPDATE hr_contracts SET final_docx_path=%s WHERE id=%s",
                            (new_path, cid))
                conn.commit()
                flash("✓ Đã sinh lại file có chữ ký NV.", "success")
            else:
                flash("Sinh file thất bại — xem log.", "danger")
    return redirect(url_for("hr.employee_contracts", uid=uid))


@hr_bp.route("/contract/<int:cid>/download-final", methods=["GET"])
def contract_download_final(cid: int):
    """Tải file DOCX final đã chèn chữ ký NV (và cty nếu có)."""
    if (resp := _login_required()):
        return resp
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, contract_number, final_docx_path FROM hr_contracts WHERE id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            owner_uid, no, rel_path = row
    if not rel_path:
        abort(404)
    if not _is_contract_admin() and owner_uid != _current_user_id():
        abort(403)
    full = (UPLOAD_ROOT / rel_path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT.resolve())) or not full.is_file():
        abort(404)
    return send_file(str(full), as_attachment=True,
                     download_name=f"{no.replace('/', '_')}_FINAL.docx")


@hr_bp.route("/contract/<int:cid>/delete", methods=["POST"])
def contract_delete(cid: int):
    if (resp := _login_required()):
        return resp
    if not _is_contract_admin():
        abort(403)
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, docx_path, status FROM hr_contracts WHERE id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            owner_uid, rel_path, status = row
            if status == "signed":
                flash("Hợp đồng đã ký — không thể xóa.", "danger")
                return redirect(url_for("hr.employee_contracts", uid=owner_uid))
            cur.execute("DELETE FROM hr_contracts WHERE id=%s", (cid,))
            conn.commit()
    _delete_file_if_exists(rel_path)
    flash("Đã xóa hợp đồng.", "info")
    return redirect(url_for("hr.employee_contracts", uid=owner_uid))


# ─────────────────────────────────────────────────────────────────────────────
# Module register
# ─────────────────────────────────────────────────────────────────────────────

@hr_bp.context_processor
def _inject_hr_context():
    """Tự động gắn my_pending_sign_count vào mọi template của blueprint hr."""
    uid = _current_user_id()
    if not uid:
        return {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM hr_contracts
                    WHERE user_id=%s
                      AND signed_employee = FALSE
                      AND status NOT IN ('terminated', 'expired')
                    """,
                    (uid,),
                )
                cnt = cur.fetchone()[0] or 0
                return {"my_pending_sign_count": int(cnt)}
    except Exception:
        return {"my_pending_sign_count": 0}


def register_hr_module(app) -> None:
    app.register_blueprint(hr_bp)
    logger.info("hr module registered at /hr/")
