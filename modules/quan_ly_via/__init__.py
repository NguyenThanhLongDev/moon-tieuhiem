"""Quản lý kho tài khoản VIA Facebook — import từ Google Sheets, CRUD, phân quyền theo team.

Phân quyền:
- admin/superadmin/it/manager: xem + CRUD toàn bộ.
- leader: chỉ xem VIA team mình (read-only).
- các role khác (staff/sale/ketoan/kho): 403.
"""
from __future__ import annotations

import csv
import io
import logging
import urllib.parse
from datetime import datetime
from functools import wraps
from typing import Any, Dict, List, Optional

import requests
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from db import get_conn

logger = logging.getLogger(__name__)

quan_ly_via_bp = Blueprint(
    "quan_ly_via", __name__,
    url_prefix="/quan-ly-via",
    template_folder="templates",
)

# ── Sheet → team mapping ──────────────────────────────────────────────────────
# Tên tab trong Google Sheets → team_code chuẩn của hệ thống (mapping với teams.team_code)
SHEET_TAB_TO_TEAM = {
    "Team Minh":        ("team-minh",      "Team Minh"),
    "tem ken":          ("team-ken",       "Team Ken"),
    "team-thanh":       ("team-thanh",     "Team Thanh"),
    "team-nhat":        ("team-nhat",      "Team Nhật"),
    "team-nam":         ("team-nam",       "Team Nam"),
    "Via Kho- Kế toán": ("kho-ketoan",     "Via Kho · Kế toán"),
    "team-sale":        ("team-sale",      "Team Sale"),
    "team-cong-minh":   ("team-cong-minh", "Team Công Minh"),
    "via chưa cấp":     ("chua-cap",       "Chưa cấp"),
}

GSHEET_ID_DEFAULT = "13yHfrUmEmhR2UkNRlKXeFQfmWt_dOq8g09Bl79iquBs"


# ── Permission ────────────────────────────────────────────────────────────────
_FULL_ROLES = {"admin", "superadmin", "it", "manager"}
_VIEW_ROLES = _FULL_ROLES | {"leader"}


def _role() -> str:
    return str(session.get("role") or "").strip().lower()


def _is_logged_in() -> bool:
    return bool(session.get("user_id"))


def _can_view() -> bool:
    return _role() in _VIEW_ROLES


def _can_edit() -> bool:
    return _role() in _FULL_ROLES


def require_view(fn):
    @wraps(fn)
    def w(*a, **kw):
        if not _is_logged_in():
            return redirect(url_for("auth.login"))
        if not _can_view():
            abort(403)
        return fn(*a, **kw)
    return w


def require_edit(fn):
    @wraps(fn)
    def w(*a, **kw):
        if not _is_logged_in():
            return redirect(url_for("auth.login"))
        if not _can_edit():
            abort(403)
        return fn(*a, **kw)
    return w


def _current_user_team_code() -> Optional[str]:
    """Lookup team_code của user hiện tại (qua users.team_id → teams.team_code)."""
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        with get_conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT t.team_code FROM users u "
                "LEFT JOIN teams t ON t.id = u.team_id "
                "WHERE u.id = %s",
                (uid,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as exc:
        logger.warning("user_team_code error: %s", exc)
        return None


def _allowed_team_codes() -> Optional[List[str]]:
    """None = full view; list = filter theo team_code."""
    role = _role()
    if role in _FULL_ROLES:
        return None  # admin/IT xem hết
    if role == "leader":
        tc = _current_user_team_code()
        codes = [tc] if tc else []
        # Quy ước: Minh leader (team-minh) cũng quản team-sale
        if tc == "team-minh":
            codes.append("team-sale")
        return codes
    return []


# ── Sheets import ─────────────────────────────────────────────────────────────
def _fetch_sheet_csv(sheet_id: str, tab_name: str) -> List[List[str]]:
    """Fetch 1 tab Google Sheets về CSV (public sheet)."""
    enc = urllib.parse.quote(tab_name)
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={enc}"
    )
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    text = r.text
    if not text.strip():
        return []
    return list(csv.reader(io.StringIO(text)))


def _detect_columns(header: List[str]) -> Dict[str, int]:
    """Dò vị trí cột từ header row → map field → index."""
    m: Dict[str, int] = {}
    for i, h in enumerate(header):
        hl = (h or "").strip().lower()
        if not hl:
            continue
        if "tên via" in hl or hl == "via" or "ten via" in hl:
            m.setdefault("full_name", i)
        elif "id facebook" in hl or hl == "id fb" or "id fb/mail" in hl or hl == "id":
            m.setdefault("fb_id", i)
        elif hl == "pass" and "pass_email" not in m and "fb_password" not in m:
            m["fb_password"] = i
        elif hl == "2fa" or "2-fa" in hl:
            m.setdefault("fb_2fa", i)
        elif hl == "email" or "mail" in hl:
            m.setdefault("email", i)
        elif "pass email" in hl or "pass mail" in hl:
            m["email_password"] = i
        elif "pass" in hl and "fb_password" in m and "email_password" not in m:
            m["email_password"] = i
        elif "kinh doanh" in hl or "tên nv" in hl or "ten nv" in hl or "via nhân viên" in hl or "via nv" in hl:
            m.setdefault("assigned_label", i)
    return m


def _import_sheet(sheet_id: str, by_user_id: Optional[int]) -> Dict[str, Any]:
    """Import toàn bộ 9 tab từ Google Sheets vào via_accounts (UPSERT theo fb_id).

    Trả về stats: {imported, updated, errors, by_tab}.
    """
    total_imp = total_upd = total_err = 0
    by_tab: Dict[str, Dict[str, int]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        for tab_name, (team_code, team_label) in SHEET_TAB_TO_TEAM.items():
            try:
                rows = _fetch_sheet_csv(sheet_id, tab_name)
            except Exception as exc:
                logger.error("fetch tab %s err: %s", tab_name, exc)
                by_tab[tab_name] = {"imported": 0, "updated": 0, "error": str(exc)[:80]}
                total_err += 1
                continue
            # Dò header: tìm dòng đầu tiên có chứa "Tên Via" hoặc "ID Facebook" hoặc "pass"
            header_idx = -1
            colmap: Dict[str, int] = {}
            for i, r in enumerate(rows[:5]):  # chỉ tìm trong 5 dòng đầu
                joined = " ".join(c.lower() for c in r if c)
                if any(k in joined for k in ("tên via", "ten via", "id facebook", "id fb")):
                    colmap = _detect_columns(r)
                    if colmap.get("full_name") is not None and (colmap.get("fb_id") is not None or colmap.get("fb_password") is not None):
                        header_idx = i
                        break
            if header_idx < 0 or not colmap:
                logger.warning("tab %s: không dò được header, skip", tab_name)
                by_tab[tab_name] = {"imported": 0, "updated": 0, "skipped": "no header"}
                continue
            imp = upd = 0
            for row in rows[header_idx + 1:]:
                def _get(field):
                    idx = colmap.get(field)
                    if idx is None or idx >= len(row):
                        return ""
                    return (row[idx] or "").strip()
                full_name = _get("full_name")
                kinh_doanh = _get("assigned_label")
                fb_id = _get("fb_id")
                fb_pw = _get("fb_password")
                fb_2fa = _get("fb_2fa")
                email = _get("email")
                em_pw = _get("email_password")
                note = ""
                # Xử lý format team-thanh: 1 cell multi-line chứa nhiều trường — split bằng \n
                if fb_id and "\n" in fb_id:
                    parts = [p.strip() for p in fb_id.split("\n") if p.strip()]
                    if len(parts) >= 3:
                        fb_id = parts[0]
                        if not fb_pw and len(parts) > 1:
                            fb_pw = parts[1]
                        if not fb_2fa and len(parts) > 2:
                            fb_2fa = parts[2]
                        if not email and len(parts) > 3:
                            email = parts[3]
                        if not em_pw and len(parts) > 4:
                            em_pw = parts[4]
                if not full_name and not fb_id:
                    continue
                # Status: nếu cột note ghi 'nghỉ' / 'vô hiệu' / 'disabled' → disabled
                # Default theo dữ liệu: có NV → đã cấp; ko có → chưa cấp; có chữ "nghỉ" → vô hiệu
                low = note.lower() if note else ""
                if any(k in low for k in ("nghỉ", "ngừng", "disable", "vô hiệu")):
                    status = "vo_hieu"
                elif kinh_doanh:
                    status = "da_cap"
                else:
                    status = "chua_cap"
                # UPSERT theo fb_id (nếu fb_id rỗng, tạo mới mỗi lần)
                if fb_id:
                    cur.execute(
                        "SELECT id FROM via_accounts WHERE fb_id = %s LIMIT 1",
                        (fb_id,),
                    )
                    found = cur.fetchone()
                else:
                    found = None
                if found:
                    # VIA đã gán NV (assigned_user_id) → team theo NV là nguồn chính:
                    # sheet KHÔNG ghi đè team_code/team_label, chỉ cập nhật các field khác.
                    cur.execute(
                        """
                        UPDATE via_accounts
                           SET team_code = CASE WHEN assigned_user_id IS NULL THEN %s ELSE team_code END,
                               team_label = CASE WHEN assigned_user_id IS NULL THEN %s ELSE team_label END,
                               full_name=%s, fb_password=%s,
                               fb_2fa_secret=%s, email=%s, email_password=%s,
                               assigned_label=%s, status=%s, note=%s, source='sheets',
                               updated_at=NOW(), updated_by=%s
                         WHERE id=%s
                        """,
                        (team_code, team_label, full_name, fb_pw, fb_2fa, email,
                         em_pw, kinh_doanh, status, note, by_user_id, found[0]),
                    )
                    upd += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO via_accounts
                            (team_code, team_label, full_name, fb_id, fb_password,
                             fb_2fa_secret, email, email_password, assigned_label,
                             status, note, source, created_by, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'sheets',%s,%s)
                        """,
                        (team_code, team_label, full_name, fb_id, fb_pw, fb_2fa,
                         email, em_pw, kinh_doanh, status, note, by_user_id, by_user_id),
                    )
                    imp += 1
            by_tab[tab_name] = {"imported": imp, "updated": upd}
            total_imp += imp
            total_upd += upd
        conn.commit()
    return {"imported": total_imp, "updated": total_upd, "errors": total_err, "by_tab": by_tab}


# ── OTP from 2FA secret ───────────────────────────────────────────────────────
def _gen_totp(secret: str) -> Optional[str]:
    """Sinh mã TOTP 6 số từ 2FA secret (base32). Trả None nếu lỗi."""
    secret = (secret or "").replace(" ", "").upper()
    if not secret:
        return None
    try:
        import hmac
        import hashlib
        import base64
        import struct
        import time
        key = base64.b32decode(secret + "=" * ((-len(secret)) % 8), casefold=True)
        counter = int(time.time()) // 30
        msg = struct.pack(">Q", counter)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}"
    except Exception as exc:
        logger.warning("TOTP gen err: %s", exc)
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────
def _list_vias(team_filter: str = "", status_filter: str = "", search: str = "") -> List[Dict[str, Any]]:
    """Liệt kê VIA + filter."""
    allowed = _allowed_team_codes()
    where = []
    args: list = []
    if allowed is not None:
        if not allowed:
            return []
        where.append("v.team_code = ANY(%s)")
        args.append(allowed)
    if team_filter:
        where.append("v.team_code = %s")
        args.append(team_filter)
    if status_filter:
        # Map filter UI → status DB (giữ tương thích cho link cũ)
        _map = {"assigned": "da_cap", "unassigned": "chua_cap",
                "disabled": "vo_hieu", "active": "da_cap",
                "da_cap": "da_cap", "chua_cap": "chua_cap", "vo_hieu": "vo_hieu"}
        s = _map.get(status_filter)
        if s:
            where.append("v.status = %s")
            args.append(s)
    if search:
        where.append("(v.full_name ILIKE %s OR v.fb_id ILIKE %s OR v.email ILIKE %s OR v.assigned_label ILIKE %s)")
        s = f"%{search}%"
        args += [s, s, s, s]
    sql = """
        SELECT v.id, v.team_code, v.team_label, v.full_name, v.fb_id, v.email,
               v.assigned_user_id, v.assigned_label, v.status, v.note, v.source,
               v.updated_at, u.full_name AS assigned_user_name
          FROM via_accounts v
          LEFT JOIN users u ON u.id = v.assigned_user_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY v.team_code, v.full_name"
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(args))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def sync_user_team_to_vias(user_id, team_code) -> int:
    """Đẩy team của 1 user XUỐNG mọi VIA user đó đang giữ (assigned_user_id).

    Gọi khi đổi team của user bên Cài đặt → VIA NV đó đang giữ tự nhảy theo team mới
    (vd user sang team A thì VIA đang team B đổi sang A). last-writer-wins.
    `team_code` rỗng (bỏ team) → giữ nguyên VIA, KHÔNG xoá team đang có.
    Trả số VIA được đổi. Không raise — lỗi DB chỉ log + trả 0.
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
    # team_label: ưu tiên mapping sheet (vd "Team Công Minh"), fallback teams.team_name
    team_label = next((tl for tc, tl in SHEET_TAB_TO_TEAM.values() if tc == team_code), None)
    try:
        with get_conn() as c, c.cursor() as cur:
            if not team_label:
                cur.execute("SELECT team_name FROM teams WHERE team_code=%s", (team_code,))
                _r = cur.fetchone()
                team_label = (_r[0] if _r and _r[0] else team_code)
            cur.execute(
                "UPDATE via_accounts SET team_code=%s, team_label=%s, updated_at=NOW() "
                "WHERE assigned_user_id=%s AND team_code IS DISTINCT FROM %s",
                (team_code, team_label, uid, team_code),
            )
            n = cur.rowcount
            c.commit()
        return n or 0
    except Exception as exc:
        logger.warning("sync_user_team_to_vias error: %s", exc)
        return 0


def _team_options() -> List[Dict[str, str]]:
    """Dropdown team options (distinct trong DB + từ mapping)."""
    seen = set()
    out = []
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT team_code, team_label FROM via_accounts WHERE team_code IS NOT NULL")
        for tc, tl in cur.fetchall():
            if tc not in seen:
                out.append({"code": tc, "label": tl or tc})
                seen.add(tc)
    for tc, tl in SHEET_TAB_TO_TEAM.values():
        if tc not in seen:
            out.append({"code": tc, "label": tl})
            seen.add(tc)
    out.sort(key=lambda x: x["label"])
    return out


def _user_list_for_assign() -> List[Dict[str, Any]]:
    """Danh sách NV để gán VIA (chỉ user active)."""
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, username, full_name FROM users WHERE status='active' ORDER BY full_name"
        )
        return [{"id": r[0], "username": r[1], "full_name": r[2] or r[1]} for r in cur.fetchall()]


def _stats() -> Dict[str, int]:
    allowed = _allowed_team_codes()
    where = ""
    args: tuple = ()
    if allowed is not None:
        if not allowed:
            return {"total": 0, "assigned": 0, "unassigned": 0, "disabled": 0}
        where = " WHERE team_code = ANY(%s)"
        args = (allowed,)
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            f"""SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='da_cap')   AS assigned,
                COUNT(*) FILTER (WHERE status='chua_cap') AS unassigned,
                COUNT(*) FILTER (WHERE status='vo_hieu')  AS disabled
              FROM via_accounts{where}
            """,
            args,
        )
        r = cur.fetchone()
        return {"total": r[0], "assigned": r[1], "unassigned": r[2], "disabled": r[3]}


# ── Routes ────────────────────────────────────────────────────────────────────
@quan_ly_via_bp.route("/")
@require_view
def index():
    team = request.args.get("team", "").strip()
    status = request.args.get("status", "").strip()
    search = request.args.get("q", "").strip()
    vias = _list_vias(team, status, search)
    return render_template(
        "quan_ly_via/index.html",
        vias=vias,
        teams=_team_options(),
        users=_user_list_for_assign() if _can_edit() else [],
        stats=_stats(),
        can_edit=_can_edit(),
        f_team=team, f_status=status, f_search=search,
        sheet_id_default=GSHEET_ID_DEFAULT,
    )


@quan_ly_via_bp.route("/<int:via_id>/detail")
@require_view
def detail(via_id: int):
    """Trả thông tin chi tiết VIA (JSON) — chỉ user có quyền view team đó."""
    allowed = _allowed_team_codes()
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, team_code, team_label, full_name, fb_id, fb_password,
                      fb_2fa_secret, email, email_password, assigned_user_id,
                      assigned_label, status, note
                 FROM via_accounts WHERE id = %s""",
            (via_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "không tìm thấy"}), 404
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
    if allowed is not None and d["team_code"] not in (allowed or []):
        return jsonify({"error": "không có quyền"}), 403
    d["otp"] = _gen_totp(d.get("fb_2fa_secret") or "")
    return jsonify(d)


@quan_ly_via_bp.route("/<int:via_id>/otp")
@require_view
def otp(via_id: int):
    allowed = _allowed_team_codes()
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT team_code, fb_2fa_secret FROM via_accounts WHERE id=%s", (via_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "không tìm thấy"}), 404
    if allowed is not None and row[0] not in (allowed or []):
        return jsonify({"error": "không có quyền"}), 403
    return jsonify({"otp": _gen_totp(row[1] or ""), "expires_in": 30 - int(__import__("time").time()) % 30})


@quan_ly_via_bp.route("/sync-sheets", methods=["POST"])
@require_edit
def sync_sheets():
    sheet_id = (request.form.get("sheet_id") or GSHEET_ID_DEFAULT).strip()
    by = session.get("user_id")
    try:
        stats = _import_sheet(sheet_id, by)
        flash(f"Đồng bộ xong: +{stats['imported']} mới, {stats['updated']} cập nhật, {stats['errors']} lỗi", "success")
    except Exception as exc:
        logger.error("sync sheets err: %s", exc)
        flash(f"Lỗi đồng bộ: {exc}", "danger")
    return redirect(url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/add", methods=["POST"])
@require_edit
def add():
    by = session.get("user_id")
    f = request.form
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO via_accounts
               (team_code, team_label, full_name, fb_id, fb_password, fb_2fa_secret,
                email, email_password, assigned_label, status, note, source, created_by, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual',%s,%s)""",
            (
                (f.get("team_code") or "").strip(),
                next((tl for tc, tl in SHEET_TAB_TO_TEAM.values() if tc == (f.get("team_code") or "").strip()), (f.get("team_code") or "").strip()),
                (f.get("full_name") or "").strip(),
                (f.get("fb_id") or "").strip(),
                (f.get("fb_password") or "").strip(),
                (f.get("fb_2fa_secret") or "").strip(),
                (f.get("email") or "").strip(),
                (f.get("email_password") or "").strip(),
                (f.get("assigned_label") or "").strip(),
                (f.get("status") or "chua_cap").strip(),
                (f.get("note") or "").strip(),
                by, by,
            ),
        )
        c.commit()
    flash("Đã thêm VIA mới", "success")
    return redirect(url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/<int:via_id>/edit", methods=["POST"])
@require_edit
def edit(via_id: int):
    """Cập nhật VIA.

    AN TOÀN: Các trường bí mật `fb_password`, `fb_2fa_secret`, `email_password`
    chỉ được UPDATE khi user nhập giá trị mới (non-empty). Để trống = GIỮ
    NGUYÊN DB — đúng với label hiển thị trên modal Sửa. Trước 2026-06-08
    backend wipe data mỗi lần Lưu kể cả khi field trống → đã làm mất dữ liệu
    pass/2FA của VIA thật. Sửa theo workflow §10B (đã hỏi user OK trước).
    """
    by = session.get("user_id")
    f = request.form
    # Gán NV (assigned_user_id) — '' nghĩa là KHÔNG gán
    assigned_raw = (f.get("assigned_user_id") or "").strip()
    assigned_uid = int(assigned_raw) if assigned_raw.isdigit() else None

    team_code = (f.get("team_code") or "").strip()
    team_label = next(
        (tl for tc, tl in SHEET_TAB_TO_TEAM.values() if tc == team_code),
        team_code,
    )

    # SET clause base — luôn UPDATE các field thường
    sets = [
        "team_code=%s", "team_label=%s", "full_name=%s", "fb_id=%s",
        "email=%s", "assigned_label=%s", "assigned_user_id=%s",
        "status=%s", "note=%s", "updated_at=NOW()", "updated_by=%s",
    ]
    vals: list = [
        team_code, team_label,
        (f.get("full_name") or "").strip(),
        (f.get("fb_id") or "").strip(),
        (f.get("email") or "").strip(),
        (f.get("assigned_label") or "").strip(),
        assigned_uid,
        (f.get("status") or "chua_cap").strip(),
        (f.get("note") or "").strip(),
        by,
    ]
    # 3 field bí mật — chỉ UPDATE khi non-empty
    fb_password = (f.get("fb_password") or "").strip()
    fb_2fa      = (f.get("fb_2fa_secret") or "").strip()
    email_pw    = (f.get("email_password") or "").strip()
    if fb_password:
        sets.append("fb_password=%s");    vals.append(fb_password)
    if fb_2fa:
        sets.append("fb_2fa_secret=%s");  vals.append(fb_2fa)
    if email_pw:
        sets.append("email_password=%s"); vals.append(email_pw)

    vals.append(via_id)
    sql = f"UPDATE via_accounts SET {', '.join(sets)} WHERE id=%s"
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(vals))
        c.commit()
    flash("Đã cập nhật VIA", "success")
    return redirect(url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/<int:via_id>/assign", methods=["POST"])
@require_edit
def assign(via_id: int):
    user_id = request.form.get("assigned_user_id", "").strip()
    by = session.get("user_id")
    uid = int(user_id) if user_id.isdigit() else None
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE via_accounts SET assigned_user_id=%s, updated_at=NOW(), updated_by=%s WHERE id=%s",
            (uid, by, via_id),
        )
        c.commit()
    flash("Đã gán VIA", "success")
    return redirect(url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/<int:via_id>/revoke", methods=["POST"])
@require_edit
def revoke(via_id: int):
    by = session.get("user_id")
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE via_accounts SET assigned_user_id=NULL, updated_at=NOW(), updated_by=%s WHERE id=%s",
            (by, via_id),
        )
        c.commit()
    flash("Đã thu hồi VIA", "success")
    return redirect(url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/<int:via_id>/set-status", methods=["POST"])
@require_edit
def set_status(via_id: int):
    """Đổi status sang đúng 1 trong 3: da_cap / chua_cap / vo_hieu."""
    by = session.get("user_id")
    new = (request.form.get("status") or "").strip()
    if new not in ("da_cap", "chua_cap", "vo_hieu"):
        flash("Trạng thái không hợp lệ", "danger")
        return redirect(url_for("quan_ly_via.index"))
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE via_accounts SET status=%s, updated_at=NOW(), updated_by=%s WHERE id=%s",
            (new, by, via_id),
        )
        c.commit()
    labels = {"da_cap": "Đã cấp", "chua_cap": "Chưa cấp", "vo_hieu": "Vô hiệu hoá"}
    flash(f"Đã đổi VIA sang **{labels[new]}**", "success")
    return redirect(request.referrer or url_for("quan_ly_via.index"))


@quan_ly_via_bp.route("/<int:via_id>/delete", methods=["POST"])
@require_edit
def delete(via_id: int):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM via_accounts WHERE id=%s", (via_id,))
        c.commit()
    flash("Đã xoá VIA", "success")
    return redirect(url_for("quan_ly_via.index"))
