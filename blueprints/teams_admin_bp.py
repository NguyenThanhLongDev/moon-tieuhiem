"""Teams admin — CRUD teams + assign team_type.

Routes:
  GET  /teams              → list + create form
  POST /teams/create       → thêm team mới
  POST /teams/<id>/update  → sửa tên / type / status / leader
  POST /teams/<id>/delete  → xoá (chỉ khi không có thành viên)

Permission: admin / superadmin / manager.
"""
from __future__ import annotations

import logging
import re
from functools import wraps

from flask import (
    Blueprint, flash, redirect, render_template_string, request, session, url_for, abort,
)

logger = logging.getLogger(__name__)

teams_admin_bp = Blueprint("teams_admin", __name__, url_prefix="/teams")

_ADMIN_ROLES = {"admin", "superadmin", "manager"}
_TEAM_TYPES = ["kinh_doanh", "sale", "khac"]
_TEAM_TYPE_LABEL = {
    "kinh_doanh": "Kinh doanh (báo NS)",
    "sale": "Sale",
    "khac": "Khác (kho, hỗ trợ...)",
}
_TEAM_TYPE_BADGE = {
    "kinh_doanh": "bg-success",
    "sale": "bg-info",
    "khac": "bg-secondary",
}


def _login_admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        role = (session.get("role") or "").lower()
        if role not in _ADMIN_ROLES:
            abort(403)
        return f(*a, **kw)
    return wrapper


_TEAMS_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>👥 Quản lý Team</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
<style>
body { background:#f5f6fa; font-family:'Segoe UI',sans-serif; font-size:14px; }
.topbar { background:#1a1a1a; padding:0 20px; min-height:48px; display:flex; align-items:center; gap:12px; box-shadow:0 2px 8px rgba(0,0,0,.25); }
.topbar .brand { font-weight:700; font-size:15px; color:#F59E0B; }
.topbar a { color:rgba(255,255,255,.75); text-decoration:none; font-size:13px; padding:4px 10px; border-radius:6px; border:1px solid rgba(255,255,255,.2); }
.topbar a:hover { background:rgba(255,255,255,.08); color:#fff; }
.page-wrap { max-width:1200px; margin:0 auto; padding:16px; }
.card { box-shadow:0 1px 3px rgba(0,0,0,.06); }
.tbl th { background:#f8fafc; font-size:12.5px; }
.tbl td { vertical-align:middle; font-size:13px; }
.team-name-input { max-width:200px; }
.team-code { font-family:'SF Mono','Menlo',monospace; font-size:12px; color:#64748b; }
</style>
</head>
<body>
<div class="topbar">
  <span class="brand">👥 Quản lý Team</span>
  <a href="/">🏠 Trang chủ</a>
  <a href="/ngan-sach/">💬 Báo NS</a>
</div>

<div class="page-wrap">
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for level, msg in msgs %}
      <div class="alert alert-{{ 'success' if level=='success' else ('warning' if level=='warning' else 'danger') }} alert-dismissible fade show py-2 px-3" role="alert">
        {{ msg }}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
      </div>
    {% endfor %}
  {% endwith %}

  <h4 class="mb-3"><i class="bi bi-people-fill me-2"></i>Quản lý Team</h4>

  <div class="alert alert-info py-2 px-3" style="font-size:13px;border-left:4px solid #0d6efd">
    <i class="bi bi-info-circle-fill me-1"></i>
    <strong>team_type</strong> phân loại team để hệ thống áp đúng nghiệp vụ:
    <ul class="mb-0 mt-1" style="font-size:12.5px">
      <li><span class="badge bg-success">Kinh doanh</span> — chạy FB Ads, phải báo ngân sách hàng ngày.</li>
      <li><span class="badge bg-info">Sale</span> — bán hàng thuần (vd Trọng Nam), không cần báo NS.</li>
      <li><span class="badge bg-secondary">Khác</span> — kho, hỗ trợ, không liên quan ads.</li>
    </ul>
  </div>

  {# ── Form thêm team mới ── #}
  <div class="card p-3 mb-4">
    <h6 class="text-success mb-3"><i class="bi bi-plus-circle me-1"></i>Thêm team mới</h6>
    <form method="post" action="{{ url_for('teams_admin.create') }}" class="row g-2 align-items-end">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Team code <span class="text-danger">*</span></label>
        <input type="text" name="team_code" class="form-control form-control-sm"
               pattern="^[a-z0-9][a-z0-9-]{0,49}$"
               placeholder="vd: team-abc"
               title="a-z, 0-9, '-', max 50 chars" required>
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Tên hiển thị <span class="text-danger">*</span></label>
        <input type="text" name="team_name" class="form-control form-control-sm" placeholder="vd: Team ABC" required>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Loại</label>
        <select name="team_type" class="form-select form-select-sm">
          {% for k in team_types %}
            <option value="{{ k }}" {% if k=='kinh_doanh' %}selected{% endif %}>{{ team_type_label[k] }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Leader (NV)</label>
        <select name="leader_user_id" class="form-select form-select-sm">
          <option value="">— Chưa gán —</option>
          {% for u in active_users %}
            <option value="{{ u.id }}">{{ u.full_name or u.username }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col-md-2">
        <button type="submit" class="btn btn-sm btn-success w-100">
          <i class="bi bi-plus-lg"></i> Tạo team
        </button>
      </div>
    </form>
  </div>

  {# ── Danh sách teams ── #}
  <div class="card p-0">
    <div class="card-header bg-white py-2 px-3">
      <strong><i class="bi bi-list-ul me-1"></i>Danh sách teams</strong>
      <span class="text-muted ms-2" style="font-size:12px">{{ teams|length }} team</span>
    </div>
    <div class="table-responsive">
    <table class="table tbl mb-0">
      <thead>
        <tr>
          <th>#</th>
          <th>Team code</th>
          <th>Tên hiển thị</th>
          <th>Loại</th>
          <th>Trạng thái</th>
          <th>Leader</th>
          <th class="text-center">Thành viên</th>
          <th class="text-end">Hành động</th>
        </tr>
      </thead>
      <tbody>
        {% for t in teams %}
        <tr>
          <td>{{ loop.index }}</td>
          <td><span class="team-code">{{ t.team_code }}</span></td>
          <td>
            <form method="post" action="{{ url_for('teams_admin.update', team_id=t.id) }}" class="d-flex gap-1">
              <input type="hidden" name="field" value="team_name">
              <input type="text" name="value" value="{{ t.team_name }}" class="form-control form-control-sm team-name-input">
              <button type="submit" class="btn btn-sm btn-outline-primary" title="Lưu tên"><i class="bi bi-check-lg"></i></button>
            </form>
          </td>
          <td>
            <form method="post" action="{{ url_for('teams_admin.update', team_id=t.id) }}" class="d-flex gap-1">
              <input type="hidden" name="field" value="team_type">
              <select name="value" class="form-select form-select-sm" onchange="this.form.submit()">
                {% for k in team_types %}
                  <option value="{{ k }}" {% if k==t.team_type %}selected{% endif %}>{{ team_type_label[k] }}</option>
                {% endfor %}
              </select>
            </form>
          </td>
          <td>
            <span class="badge {% if t.status=='active' %}bg-success{% else %}bg-secondary{% endif %}">{{ t.status }}</span>
          </td>
          <td>
            {% if t.leader_user_id %}
              {% set leader = users_map.get(t.leader_user_id|int) %}
              {{ (leader.full_name or leader.username) if leader else '#'+t.leader_user_id|string }}
            {% else %}
              <span class="text-muted">—</span>
            {% endif %}
          </td>
          <td class="text-center">
            <span class="badge bg-light text-dark">{{ t.member_count }}</span>
          </td>
          <td class="text-end">
            {% if t.status=='active' %}
            <form method="post" action="{{ url_for('teams_admin.update', team_id=t.id) }}" class="d-inline">
              <input type="hidden" name="field" value="status">
              <input type="hidden" name="value" value="inactive">
              <button type="submit" class="btn btn-sm btn-outline-warning" title="Tạm vô hiệu">
                <i class="bi bi-pause-fill"></i>
              </button>
            </form>
            {% else %}
            <form method="post" action="{{ url_for('teams_admin.update', team_id=t.id) }}" class="d-inline">
              <input type="hidden" name="field" value="status">
              <input type="hidden" name="value" value="active">
              <button type="submit" class="btn btn-sm btn-outline-success" title="Kích hoạt">
                <i class="bi bi-play-fill"></i>
              </button>
            </form>
            {% endif %}
            <form method="post" action="{{ url_for('teams_admin.delete', team_id=t.id) }}" class="d-inline"
                  onsubmit="return confirm('Xoá team {{ t.team_code }}? (chỉ được nếu không còn thành viên)');">
              <button type="submit" class="btn btn-sm btn-outline-danger" title="Xoá">
                <i class="bi bi-trash"></i>
              </button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


def _load_teams_with_count() -> list[dict]:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.team_code, t.team_name, t.team_type, t.status,
                       t.leader_user_id, COUNT(u.id) AS member_count
                  FROM teams t
                  LEFT JOIN users u ON u.team_id = t.id
                 GROUP BY t.id, t.team_code, t.team_name, t.team_type, t.status, t.leader_user_id
                 ORDER BY t.team_type, t.team_name
            """)
            return [{
                "id": int(r[0]),
                "team_code": r[1],
                "team_name": r[2],
                "team_type": r[3] or "kinh_doanh",
                "status": r[4],
                "leader_user_id": int(r[5]) if r[5] else None,
                "member_count": int(r[6] or 0),
            } for r in cur.fetchall()]


def _load_active_users() -> list[dict]:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, full_name FROM users
                 WHERE COALESCE(status, 'active') = 'active'
                 ORDER BY full_name NULLS LAST, username
            """)
            return [{
                "id": int(r[0]),
                "username": r[1] or "",
                "full_name": r[2] or r[1] or "",
            } for r in cur.fetchall()]


@teams_admin_bp.route("/")
@_login_admin_required
def index():
    teams = _load_teams_with_count()
    users = _load_active_users()
    users_map = {u["id"]: u for u in users}
    return render_template_string(
        _TEAMS_TEMPLATE,
        teams=teams,
        active_users=users,
        users_map=users_map,
        team_types=_TEAM_TYPES,
        team_type_label=_TEAM_TYPE_LABEL,
        team_type_badge=_TEAM_TYPE_BADGE,
    )


@teams_admin_bp.route("/create", methods=["POST"])
@_login_admin_required
def create():
    team_code = (request.form.get("team_code") or "").strip().lower()
    team_name = (request.form.get("team_name") or "").strip()
    team_type = (request.form.get("team_type") or "kinh_doanh").strip()
    leader_user_id = (request.form.get("leader_user_id") or "").strip()

    if not team_code or not team_name:
        flash("Phải nhập team_code và team_name.", "danger")
        return redirect(url_for("teams_admin.index"))
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,49}$", team_code):
        flash("team_code chỉ chứa a-z, 0-9, '-', max 50 ký tự.", "danger")
        return redirect(url_for("teams_admin.index"))
    if team_type not in _TEAM_TYPES:
        team_type = "kinh_doanh"

    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM teams WHERE team_code=%s", (team_code,))
                if cur.fetchone():
                    flash(f"Team code '{team_code}' đã tồn tại.", "danger")
                    return redirect(url_for("teams_admin.index"))
                leader_id = None
                if leader_user_id:
                    cur.execute("SELECT id FROM users WHERE id=%s", (leader_user_id,))
                    row = cur.fetchone()
                    if row:
                        leader_id = int(row[0])
                cur.execute("""
                    INSERT INTO teams (team_code, team_name, status, team_type, leader_user_id)
                    VALUES (%s, %s, 'active', %s, %s)
                    RETURNING id
                """, (team_code, team_name, team_type, leader_id))
                new_id = cur.fetchone()[0]
                if leader_id:
                    cur.execute("UPDATE users SET team_id=%s WHERE id=%s", (new_id, leader_id))
                conn.commit()
        flash(f"Đã tạo team '{team_code}' ({team_name}, loại {_TEAM_TYPE_LABEL.get(team_type)}).", "success")
    except Exception as exc:
        flash(f"Lỗi tạo team: {exc}", "danger")
    return redirect(url_for("teams_admin.index"))


@teams_admin_bp.route("/<int:team_id>/update", methods=["POST"])
@_login_admin_required
def update(team_id: int):
    field = (request.form.get("field") or "").strip()
    value = (request.form.get("value") or "").strip()
    allowed = {"team_name", "team_type", "status", "leader_user_id"}
    if field not in allowed:
        flash("Trường không hợp lệ.", "danger")
        return redirect(url_for("teams_admin.index"))
    if field == "team_type" and value not in _TEAM_TYPES:
        flash("Loại team không hợp lệ.", "danger")
        return redirect(url_for("teams_admin.index"))
    if field == "status" and value not in ("active", "inactive"):
        flash("Trạng thái không hợp lệ.", "danger")
        return redirect(url_for("teams_admin.index"))

    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if field == "leader_user_id":
                    v = int(value) if value else None
                    cur.execute(f"UPDATE teams SET leader_user_id=%s, updated_at=NOW() WHERE id=%s", (v, team_id))
                else:
                    cur.execute(f"UPDATE teams SET {field}=%s, updated_at=NOW() WHERE id=%s", (value, team_id))
                conn.commit()
        flash(f"Đã cập nhật {field} cho team #{team_id}.", "success")
    except Exception as exc:
        flash(f"Lỗi: {exc}", "danger")
    return redirect(url_for("teams_admin.index"))


@teams_admin_bp.route("/<int:team_id>/delete", methods=["POST"])
@_login_admin_required
def delete(team_id: int):
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Check members
                cur.execute("SELECT COUNT(*) FROM users WHERE team_id=%s", (team_id,))
                n = int(cur.fetchone()[0])
                if n > 0:
                    flash(f"Không xoá được team #{team_id}: còn {n} thành viên. Chuyển NV sang team khác trước.", "warning")
                    return redirect(url_for("teams_admin.index"))
                cur.execute("DELETE FROM teams WHERE id=%s", (team_id,))
                conn.commit()
        flash(f"Đã xoá team #{team_id}.", "success")
    except Exception as exc:
        flash(f"Lỗi xoá team: {exc}", "danger")
    return redirect(url_for("teams_admin.index"))
