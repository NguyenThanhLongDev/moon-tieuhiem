"""
DB helpers cho module Chấm công & Phân công việc.
Sử dụng chung connection pool với Posbot.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Import db pool từ posbot
_BASE = os.path.join(os.path.dirname(__file__), '..', '..')
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from db import get_conn  # noqa: E402


# ─── GPS helpers ──────────────────────────────────────────

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Khoảng cách Haversine giữa 2 toạ độ GPS (mét)."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ─── Settings ──────────────────────────────────────────

def get_cc_settings() -> dict:
    """Trả về dict cài đặt chấm công (office_lat, office_lng, …)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM cc_settings")
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}


def upsert_cc_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
        conn.commit()


# ─── Offices ──────────────────────────────────────────

def get_all_offices() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, lat, lng, radius_m, note FROM cc_offices ORDER BY name")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def get_offices_for_team(team_code: str) -> list[dict]:
    """Trả về các văn phòng đang được gán cho bất kỳ NV nào thuộc team_code."""
    if not team_code:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT o.id, o.name, o.lat, o.lng, o.radius_m, o.note
                    FROM cc_offices o
                    JOIN cc_employees e ON e.office_id = o.id
                    LEFT JOIN users u ON u.id::text = e.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE t.team_code = %s OR e.department = %s
                    ORDER BY o.name
                """, (team_code, team_code))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def office_teams(office_id: int) -> set[str]:
    """Trả về tập team_code đang dùng office này (qua cc_employees)."""
    if not office_id:
        return set()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT COALESCE(t.team_code, e.department)
                    FROM cc_employees e
                    LEFT JOIN users u ON u.id::text = e.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE e.office_id = %s
                """, (office_id,))
                return {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        return set()


def office_teams_map() -> dict[int, list[dict]]:
    """Map office_id → list team đã gán (đọc từ cc_team_office, kèm tên team).

    Nguồn tin cậy là cc_team_office (ghi khi gán team, persist kể cả team
    chưa có NV nào) — khác office_teams() suy từ cc_employees.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT to_.office_id, to_.team_code,
                           COALESCE(t.team_name, to_.team_code) AS team_name
                    FROM cc_team_office to_
                    LEFT JOIN teams t ON t.team_code = to_.team_code
                    WHERE to_.office_id IS NOT NULL
                    ORDER BY team_name
                """)
                out: dict[int, list[dict]] = {}
                for office_id, team_code, team_name in cur.fetchall():
                    out.setdefault(int(office_id), []).append(
                        {'team_code': team_code, 'team_name': team_name})
                return out
    except Exception:
        return {}


def get_office(office_id: int) -> dict | None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, lat, lng, radius_m, note FROM cc_offices WHERE id=%s", (office_id,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
    except Exception:
        return None


def upsert_office(name: str, lat: float, lng: float, radius_m: int = 300,
                  note: str = '', office_id: int | None = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if office_id:
                cur.execute("""
                    UPDATE cc_offices SET name=%s, lat=%s, lng=%s, radius_m=%s, note=%s
                    WHERE id=%s RETURNING id
                """, (name, lat, lng, radius_m, note, office_id))
                row = cur.fetchone()
                new_id = row[0] if row else office_id
            else:
                cur.execute("""
                    INSERT INTO cc_offices (name, lat, lng, radius_m, note)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id
                """, (name, lat, lng, radius_m, note))
                new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def delete_office(office_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE cc_employees SET office_id=NULL WHERE office_id=%s", (office_id,))
            cur.execute("DELETE FROM cc_offices WHERE id=%s", (office_id,))
        conn.commit()


def assign_office_to_employees(office_id: int | None, user_ids: list[str]) -> None:
    """Gán office_id cho danh sách nhân viên. office_id=None để bỏ gán."""
    if not user_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for uid in user_ids:
                cur.execute("UPDATE cc_employees SET office_id=%s WHERE user_id=%s", (office_id, uid))
        conn.commit()


def assign_office_to_department(office_id: int | None, department: str) -> int:
    """Gán office_id cho toàn bộ nhân viên trong 1 bộ phận/team."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cc_employees SET office_id=%s WHERE department=%s",
                (office_id, department)
            )
            n = cur.rowcount
        conn.commit()
        return n


def get_all_teams_from_db() -> list[dict]:
    """Lấy danh sách team từ bảng teams trong DB."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT team_code, team_name FROM teams WHERE status='active' ORDER BY team_name"
                )
                rows = cur.fetchall()
        if rows:
            return [{'team_code': r[0], 'team_name': r[1]} for r in rows if r[0]]
    except Exception:
        pass
    # Fallback: lấy từ user_helpers
    try:
        import sys; sys.path.insert(0, _BASE)
        from user_helpers import load_all_users
        seen: dict[str, str] = {}
        for u in load_all_users():
            if u.get('status') == 'inactive':
                continue
            tc = str(u.get('team_id', '')).strip()
            if tc and tc not in seen:
                seen[tc] = tc
        return [{'team_code': k, 'team_name': v} for k, v in sorted(seen.items())]
    except Exception:
        return []


def assign_office_to_team_code(office_id: int | None, team_code: str) -> int:
    """Gán office_id cho nhân viên thuộc team_code — đọc từ DB users thay vì users.json."""
    import json
    user_ids: list = []
    # Thử đọc từ bảng users DB trước
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.id::text FROM users u
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE t.team_code = %s AND u.status = 'active'
                """, (team_code,))
                user_ids = [r[0] for r in cur.fetchall()]
    except Exception:
        pass
    # Fallback: đọc từ users.json
    if not user_ids:
        users_path = os.path.join(_BASE, 'users.json')
        try:
            with open(users_path, encoding='utf-8') as f:
                users = json.load(f)
            user_ids = [
                str(u.get('id', '')).strip()
                for u in users
                if str(u.get('team_id', '')).strip() == team_code
                   and u.get('status') != 'inactive'
                   and str(u.get('id', '')).strip()
            ]
        except Exception:
            pass
    # Lưu mapping team → văn phòng mặc định (nhớ để NV mới/đổi team tự nhận).
    # Làm trước cả khi team chưa có NV nào, để mapping vẫn được ghi.
    set_team_default_office(team_code, office_id)

    if not user_ids:
        return 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            placeholders = ','.join(['%s'] * len(user_ids))
            cur.execute(
                f"UPDATE cc_employees SET office_id=%s WHERE user_id IN ({placeholders})",
                [office_id] + user_ids
            )
            n = cur.rowcount
        conn.commit()
        return n


def set_team_default_office(team_code: str, office_id: int | None) -> None:
    """Lưu/cập nhật văn phòng MẶC ĐỊNH của 1 team (bảng cc_team_office).

    office_id=None → xoá mapping (team không còn văn phòng mặc định).
    """
    team_code = (team_code or '').strip()
    if not team_code:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if office_id is None:
                    cur.execute("DELETE FROM cc_team_office WHERE team_code=%s", (team_code,))
                else:
                    cur.execute("""
                        INSERT INTO cc_team_office (team_code, office_id, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (team_code) DO UPDATE
                            SET office_id = EXCLUDED.office_id, updated_at = NOW()
                    """, (team_code, office_id))
            conn.commit()
    except Exception as exc:
        logger.warning("set_team_default_office error: %s", exc)


def get_office_for_team(team_code: str) -> int | None:
    """Văn phòng mặc định của team (cc_team_office). None nếu chưa cấu hình."""
    team_code = (team_code or '').strip()
    if not team_code:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT office_id FROM cc_team_office WHERE team_code=%s", (team_code,))
                r = cur.fetchone()
                return r[0] if r and r[0] else None
    except Exception as exc:
        logger.warning("get_office_for_team error: %s", exc)
        return None


def apply_team_office_to_user(user_id, team_code: str, full_name: str = "") -> bool:
    """Set văn phòng chấm công của 1 NV theo văn phòng mặc định của team.

    Gọi khi TẠO user mới (có team) hoặc khi ĐỔI team của user → NV tự nhận đúng
    văn phòng để chấm công, không cần gán tay / restart.
    Tạo dòng cc_employees nếu chưa có. Ghi đè office_id cũ (đổi team thì đổi luôn).
    Trả True nếu đã set; False nếu team chưa có văn phòng mặc định.
    """
    uid = str(user_id or '').strip()
    if not uid:
        return False
    office_id = get_office_for_team(team_code)
    if office_id is None:
        return False  # team chưa cấu hình văn phòng → không đụng office hiện tại
    nm = (full_name or '').strip() or uid
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cc_employees (user_id, full_name, cc_role, office_id)
                    VALUES (%s, %s, 'sale', %s)
                    ON CONFLICT (user_id) DO UPDATE
                        SET office_id = EXCLUDED.office_id
                """, (uid, nm, office_id))
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("apply_team_office_to_user error: %s", exc)
        return False


# ─── Công (công nhật) helper ──────────────────────────────────────────

def calc_cong_value(check_in_ts, check_out_ts,
                    work_start_str: str = '08:00',
                    work_end_str: str = '17:30',
                    min_hours: float = 9.0,
                    half_hours: float = 0.0) -> float:
    """
    Tính số công cho Marketing (adser).
    - >= min_hours giờ (trong cửa sổ HC) → 1.0 công (đủ ca)
    - >= half_hours giờ (nếu half_hours > 0) → 0.5 công (nửa công)
    - còn lại → 0.0
    Mỗi cty cấu hình min_hours/half_hours riêng trong Cài đặt chấm công.
    """
    if not check_in_ts or not check_out_ts:
        return 0.0
    from datetime import datetime, time as dtime
    try:
        import pytz
        tz = pytz.timezone('Asia/Ho_Chi_Minh')
        ci = check_in_ts.astimezone(tz) if check_in_ts.tzinfo else pytz.utc.localize(check_in_ts).astimezone(tz)
        co = check_out_ts.astimezone(tz) if check_out_ts.tzinfo else pytz.utc.localize(check_out_ts).astimezone(tz)
        wsh, wsm = map(int, work_start_str.split(':'))
        weh, wem = map(int, work_end_str.split(':'))
        base = ci.date()
        ws = tz.localize(datetime.combine(base, dtime(wsh, wsm)))
        we = tz.localize(datetime.combine(base, dtime(weh, wem)))
        eff_start = max(ci, ws)
        eff_end   = min(co, we)
        if eff_end <= eff_start:
            return 0.0
        hours = (eff_end - eff_start).total_seconds() / 3600
        if hours >= min_hours:
            return 1.0
        if half_hours and half_hours > 0 and hours >= half_hours:
            return 0.5
        return 0.0
    except Exception:
        return 0.0


CC_ROLES = {
    'admin':      ('Quản lý', 'bi-shield-check', '#6366f1'),
    'leader':     ('Leader', 'bi-star-fill', '#0ea5e9'),
    'it':         ('IT', 'bi-code-slash', '#0891b2'),
    'adser':      ('Chạy Ads', 'bi-megaphone', '#f59e0b'),
    'sale':       ('Sale / Chốt đơn', 'bi-chat-dots', '#10b981'),
    'packing':    ('Đóng hàng', 'bi-box-seam', '#3b82f6'),
    'accounting': ('Kế toán', 'bi-calculator', '#8b5cf6'),
    'purchasing': ('Mua hàng', 'bi-bag-check', '#ec4899'),
}


def cc_role_label(role: str) -> str:
    return CC_ROLES.get(role, ('?', '', '#888'))[0]


def cc_role_icon(role: str) -> str:
    return CC_ROLES.get(role, ('?', 'bi-person', '#888'))[1]


def cc_role_color(role: str) -> str:
    return CC_ROLES.get(role, ('?', '', '#888'))[2]


# POS role (users.role) → CC role mapping
# Dùng để đồng bộ khi tạo user mới hoặc khi role web thay đổi cho các vai trò
# có ánh xạ 1-1. Với web role 'staff', cc_role có thể là 'sale' / 'adser' /
# 'packing' / 'purchasing' — không ép ghi đè nếu đã được chọn thủ công.
_POS_TO_CC_ROLE = {
    'admin':        'admin',
    'superadmin':   'admin',
    'manager':      'admin',
    'leader':       'leader',
    'sale_leader':  'leader',
    'it':           'it',
    'accountant':   'accounting',
    'ketoan':       'accounting',
    'kho':          'packing',
    'warehouse':    'packing',
    'staff':        'sale',
    'sale':         'sale',
}

# Những web role có ánh xạ 1-1 tới cc_role — khi web role này được gán,
# luôn ghi đè cc_role. 'staff' không thuộc nhóm này vì 1 staff có thể là
# sale / adser / packing / purchasing tuỳ chức năng thực tế.
_HARD_MAPPED_WEB_ROLES = {
    'admin', 'superadmin', 'manager',
    'leader', 'sale_leader',
    'it', 'accountant', 'ketoan',
    'kho', 'warehouse',
    'sale',
}


def sync_cc_role_from_web_role(user_id: str, web_role: str) -> None:
    """Đồng bộ cc_employees.cc_role theo users.role.

    - Nếu web_role ánh xạ tới cc_role cứng (admin/leader/it/accounting) → luôn ghi đè.
    - Nếu web_role = 'staff' (hoặc tương tự) → giữ cc_role hiện tại nếu đã là
      sub-function hợp lệ (sale/adser/packing/purchasing), ngược lại mặc định 'sale'.
    """
    if not user_id or not web_role:
        return
    target_cc = _POS_TO_CC_ROLE.get(web_role, 'sale')
    hard_override_ccs = {'admin', 'leader', 'it', 'accounting', 'packing'}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if web_role in _HARD_MAPPED_WEB_ROLES:
                    cur.execute(
                        "UPDATE cc_employees SET cc_role=%s WHERE user_id=%s",
                        (target_cc, str(user_id)),
                    )
                else:
                    # Web role = staff: nếu cc_role hiện tại là role-cứng (mâu thuẫn) → reset về sale.
                    # Nếu đã là 1 sub-function hợp lệ (sale/adser/packing/purchasing) → giữ nguyên.
                    cur.execute(
                        "SELECT cc_role FROM cc_employees WHERE user_id=%s",
                        (str(user_id),),
                    )
                    row = cur.fetchone()
                    cur_cc = (row[0] if row else None) or ''
                    if cur_cc in hard_override_ccs or not cur_cc:
                        cur.execute(
                            "UPDATE cc_employees SET cc_role=%s WHERE user_id=%s",
                            (target_cc, str(user_id)),
                        )
            conn.commit()
    except Exception as e:
        import logging
        logging.warning(f"[CC] sync_cc_role_from_web_role({user_id}, {web_role}): {e}")


def sync_all_cc_roles_from_users() -> int:
    """Quét tất cả user active và đồng bộ cc_role — chạy 1 lần sau migration."""
    n = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id::text, role::text FROM users WHERE status='active'")
                users = cur.fetchall()
        for uid, role in users:
            sync_cc_role_from_web_role(uid, role or 'staff')
            n += 1
    except Exception as e:
        import logging
        logging.warning(f"[CC] sync_all_cc_roles_from_users: {e}")
    return n


# ─── Init tables ──────────────────────────────────────────

def init_cc_tables() -> None:
    for fname in ('030_create_cham_cong.sql', '031_cham_cong_location.sql',
                  '032_cc_offices.sql', '033_cc_multi_sessions.sql'):
        sql_path = os.path.join(_BASE, 'migrations', fname)
        try:
            with open(sql_path, encoding='utf-8') as f:
                sql = f.read()
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
        except Exception as e:
            import logging
            logging.warning(f"[CC] Migration {fname} warning: {e}")
    # Bảng văn phòng MẶC ĐỊNH theo team (migration 063) — tạo đảm bảo mỗi boot,
    # idempotent, để mapping team→office luôn sẵn sàng kể cả khi auto_migrate lỡ skip.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS cc_team_office (
                        team_code  VARCHAR(64) PRIMARY KEY,
                        office_id  INTEGER REFERENCES cc_offices(id) ON DELETE CASCADE,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                # Backfill 1 lần khi bảng còn rỗng: suy văn phòng chính hiện tại của
                # mỗi team từ NV đang dùng (loại WFH bán kính lớn). Có dữ liệu rồi → bỏ qua.
                cur.execute("SELECT COUNT(*) FROM cc_team_office")
                if (cur.fetchone() or [0])[0] == 0:
                    cur.execute("""
                        INSERT INTO cc_team_office (team_code, office_id)
                        SELECT t.team_code, x.office_id
                        FROM teams t
                        JOIN LATERAL (
                            SELECT e2.office_id
                            FROM cc_employees e2
                            JOIN users u2 ON u2.id::text = e2.user_id
                            JOIN cc_offices o2 ON o2.id = e2.office_id
                            WHERE u2.team_id = t.id
                              AND COALESCE(o2.radius_m, 0) < 1000000
                            GROUP BY e2.office_id
                            ORDER BY COUNT(*) DESC
                            LIMIT 1
                        ) x ON TRUE
                        WHERE t.team_code IS NOT NULL
                        ON CONFLICT (team_code) DO NOTHING
                    """)
            conn.commit()
    except Exception as e:
        import logging
        logging.warning(f"[CC] init cc_team_office warning: {e}")


def seed_employees_from_users() -> int:
    """Tự động import tất cả user active từ DB users vào cc_employees.
    Dùng ON CONFLICT DO UPDATE — cập nhật tên nếu đã có."""
    import json
    users = []
    # Ưu tiên đọc từ DB
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.id::text, u.username, u.full_name, u.role::text, u.status::text
                    FROM users u WHERE u.status = 'active'
                """)
                for uid, uname, fname, role, status in cur.fetchall():
                    users.append({"id": str(uid), "username": uname,
                                  "full_name": fname or "", "role": role, "status": status})
    except Exception:
        pass
    # Fallback: đọc từ users.json
    if not users:
        users_path = os.path.join(_BASE, 'users.json')
        try:
            with open(users_path, encoding='utf-8') as f:
                users = json.load(f)
        except Exception:
            return 0

    count = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for u in users:
                if u.get('status') == 'inactive':
                    continue
                uid = str(u.get('id', '')).strip()
                if not uid:
                    continue
                display_name = u.get('full_name') or u.get('username', uid)
                pos_role = u.get('role', 'staff')
                cc_role = _POS_TO_CC_ROLE.get(pos_role, 'sale')
                cur.execute("""
                    INSERT INTO cc_employees (user_id, full_name, cc_role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        full_name = EXCLUDED.full_name
                """, (uid, display_name, cc_role))
                count += cur.rowcount
                # Xóa bản standalone cũ (u-*) nếu cùng full_name với user vừa upsert
                cur.execute("""
                    DELETE FROM cc_employees
                    WHERE full_name = %s AND user_id ~ '^u-'
                """, (display_name,))
            # Auto-gán văn phòng cho NV CHƯA có office → văn phòng chính của team
            # (office mà nhiều NV cùng team đang dùng nhất; loại WFH bán kính lớn).
            # Chỉ fill khi office_id IS NULL → KHÔNG đụng ai đã gán tay / WFH.
            cur.execute("""
                WITH team_office AS (
                  SELECT u.team_id,
                    (SELECT e2.office_id FROM cc_employees e2
                       JOIN users u2 ON u2.id::text = e2.user_id
                       JOIN cc_offices o2 ON o2.id = e2.office_id
                      WHERE u2.team_id = u.team_id
                        AND COALESCE(o2.radius_m, 0) < 1000000
                      GROUP BY e2.office_id
                      ORDER BY COUNT(*) DESC
                      LIMIT 1) AS office_id
                  FROM users u
                  WHERE u.team_id IS NOT NULL
                  GROUP BY u.team_id
                )
                UPDATE cc_employees e
                   SET office_id = t.office_id
                  FROM users u, team_office t
                 WHERE u.id::text = e.user_id
                   AND u.team_id = t.team_id
                   AND e.office_id IS NULL
                   AND t.office_id IS NOT NULL
            """)
        conn.commit()
    return count


# ─── Employees ──────────────────────────────────────────

_EMP_COL_NAMES = ('user_id', 'full_name', 'cc_role', 'department', 'phone',
                  'position', 'office_id', 'resigned_at', 'resigned_note',
                  'resigned_team', 'resigned_leader')
_EMP_COLS = ', '.join(_EMP_COL_NAMES)
_EMP_COLS_E = ', '.join('e.' + c for c in _EMP_COL_NAMES)


def get_employee(user_id) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_EMP_COLS} FROM cc_employees WHERE user_id = %s",
                (str(user_id),)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def get_all_employees(department: str | None = None,
                      ref_date=None,
                      include_resigned: bool = False) -> list[dict]:
    """
    Liệt kê NV.
    - department: lọc theo team (cc_employees.department hoặc users.teams.team_code).
    - ref_date: ngày tham chiếu (mặc định = hôm nay).
    - include_resigned=True: lấy cả NV đã nghỉ lâu.

    Rule nghỉ việc (2026-05-30): NV nghỉ ngày X → VẪN HIỆN đến X + 1 tháng
    (kế toán đủ thời gian tính công + chốt sổ). Sang ngày X+1tháng mới ẩn.
    Vd nghỉ 13/5 → còn hiện đến 12/6, từ 13/6 ẩn.
    """
    extra_where = ""
    params_tail: tuple = ()
    if not include_resigned:
        # Hiện nếu: chưa nghỉ HOẶC (ngày nghỉ + 1 tháng) > today.
        extra_where = " AND (e.resigned_at IS NULL OR (e.resigned_at + INTERVAL '1 month') > %s)"
        from datetime import date as _date
        ref = ref_date or _date.today()
        params_tail = (ref,)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if department:
                cur.execute(f"""
                    SELECT {_EMP_COLS_E}
                    FROM cc_employees e
                    LEFT JOIN users u ON u.id::text = e.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE (e.department = %s OR t.team_code = %s){extra_where}
                    ORDER BY (e.resigned_at IS NOT NULL), e.full_name
                """, (department, department) + params_tail)
            else:
                cur.execute(f"""
                    SELECT {_EMP_COLS_E}
                    FROM cc_employees e
                    WHERE 1=1{extra_where}
                    ORDER BY (e.resigned_at IS NOT NULL), e.full_name
                """, params_tail)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def resign_employee(user_id: str, note: str, by_user_id: str,
                    resigned_at=None) -> dict:
    """Đánh dấu NV nghỉ việc: lưu ngày nghỉ + ghi chú + snapshot team/leader,
    đồng thời ĐỔI MẬT KHẨU người dùng sang mật khẩu mặc định 'Long78596@'
    (để NV cũ không dùng được mật khẩu họ đã biết).

    KHÔNG khoá tài khoản (users.status vẫn 'active') — admin/kế toán có thể
    đăng nhập hộ nếu cần xem lại dữ liệu.
    Việc ẩn NV ở các trang chấm công dựa vào resigned_at (date-aware).

    Nếu resigned_at None → mặc định hôm nay.
    """
    from datetime import date as _date
    rd = resigned_at or _date.today()
    emp = get_employee(user_id)
    if not emp:
        return {"ok": False, "msg": "Không tìm thấy nhân viên."}
    if emp.get('resigned_at'):
        return {"ok": False, "msg": "Nhân viên này đã được đánh dấu nghỉ rồi."}

    # Snapshot team + leader hiện tại (để sau này vẫn biết ai từng quản lý)
    team_snap = ''
    leader_snap = ''
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Tên team: ưu tiên teams.name / team_code qua users.team_id;
            # fallback: cc_employees.department.
            cur.execute("""
                SELECT t.team_code, t.team_name, t.leader_user_id, lu.full_name
                FROM users u
                LEFT JOIN teams t ON t.id = u.team_id
                LEFT JOIN users lu ON lu.id = t.leader_user_id
                WHERE u.id::text = %s
            """, (str(user_id),))
            row = cur.fetchone()
            if row:
                t_code, t_name, _lid, leader_name = row
                team_snap = (t_name or t_code or emp.get('department') or '').strip()
                leader_snap = (leader_name or '').strip()
            if not team_snap:
                team_snap = (emp.get('department') or '').strip()

            cur.execute("""
                UPDATE cc_employees
                SET resigned_at=%s, resigned_note=%s,
                    resigned_team=%s, resigned_leader=%s, resigned_by=%s
                WHERE user_id=%s
            """, (rd, (note or '').strip(), team_snap, leader_snap,
                  str(by_user_id), str(user_id)))
            # KHÔNG đổi users.status — tài khoản vẫn đăng nhập được bình thường.
            # Reset mật khẩu sang mặc định 'Long78596@' (để NV cũ không dùng pw đã biết).
            try:
                uid_int = int(user_id)
                import bcrypt as _bcrypt
                _hash = _bcrypt.hashpw(b'Long78596@', _bcrypt.gensalt(12)).decode()
                cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                            (_hash, uid_int))
            except (ValueError, TypeError):
                pass
            except Exception:
                # Nếu lỗi bcrypt (thiếu module) thì bỏ qua — vẫn đánh dấu nghỉ thành công
                pass
        conn.commit()
    return {
        "ok": True,
        "msg": f"Đã đánh dấu nghỉ việc từ ngày {rd.strftime('%d/%m/%Y')}. Mật khẩu đã được reset.",
        "resigned_at": rd.isoformat(),
        "team": team_snap,
        "leader": leader_snap,
    }


def restore_employee(user_id: str) -> dict:
    """Mở lại nhân viên đã đánh dấu nghỉ (lỡ nhầm).
    Xoá thông tin nghỉ + bật lại tài khoản (users.status='active').
    """
    emp = get_employee(user_id)
    if not emp:
        return {"ok": False, "msg": "Không tìm thấy nhân viên."}
    if not emp.get('resigned_at'):
        return {"ok": False, "msg": "Nhân viên đang active, không cần mở lại."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_employees
                SET resigned_at=NULL, resigned_note=NULL,
                    resigned_team=NULL, resigned_leader=NULL, resigned_by=NULL
                WHERE user_id=%s
            """, (str(user_id),))
            # Không đổi users.status — tính năng nghỉ việc không khoá đăng nhập.
        conn.commit()
    return {"ok": True, "msg": "Đã mở lại nhân viên (active)."}


def upsert_employee(user_id: str, full_name: str, cc_role: str,
                    department: str = '', phone: str = '', position: str = '',
                    office_id: int | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_employees (user_id, full_name, cc_role, department, phone, position, office_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    full_name  = EXCLUDED.full_name,
                    cc_role    = EXCLUDED.cc_role,
                    department = EXCLUDED.department,
                    phone      = EXCLUDED.phone,
                    position   = EXCLUDED.position,
                    office_id  = COALESCE(EXCLUDED.office_id, cc_employees.office_id)
            """, (user_id, full_name, cc_role, department, phone, position, office_id))
        conn.commit()


def create_standalone_employee(full_name: str, cc_role: str,
                               department: str = '', phone: str = '',
                               position: str = '') -> str:
    """Tạo nhân viên mới không cần tài khoản POS (dùng cho nhân viên kho, sale...).
    Trả về user_id được tạo."""
    import uuid
    uid = 'emp-' + uuid.uuid4().hex[:8]
    upsert_employee(uid, full_name, cc_role, department, phone, position)
    return uid


def delete_employee(user_id: str) -> None:
    """Xoá nhân viên độc lập (chỉ xoá được emp-* không xoá được user POS)."""
    if not user_id.startswith('emp-'):
        return  # chỉ xoá standalone employee
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cc_employees WHERE user_id=%s", (user_id,))
        conn.commit()


# ─── Attendance ──────────────────────────────────────────

def get_attendance(user_id: str, date: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, date, check_in, check_out, shift_id, status, note
                FROM cc_attendance WHERE user_id=%s AND date=%s
            """, (user_id, date))
            row = cur.fetchone()
    if not row:
        return None
    return dict(zip(['id', 'user_id', 'date', 'check_in', 'check_out', 'shift_id', 'status', 'note'], row))


def check_in(user_id: str, date: str, ts, shift_id: int | None = None,
             lat: float | None = None, lng: float | None = None,
             distance_m: int | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_attendance
                    (user_id, date, check_in, shift_id, status,
                     check_in_lat, check_in_lng, check_in_distance_m)
                VALUES (%s, %s, %s, %s, 'present', %s, %s, %s)
                ON CONFLICT (user_id, date) DO UPDATE SET
                    check_in = EXCLUDED.check_in,
                    shift_id = COALESCE(EXCLUDED.shift_id, cc_attendance.shift_id),
                    check_in_lat = COALESCE(EXCLUDED.check_in_lat, cc_attendance.check_in_lat),
                    check_in_lng = COALESCE(EXCLUDED.check_in_lng, cc_attendance.check_in_lng),
                    check_in_distance_m = COALESCE(EXCLUDED.check_in_distance_m, cc_attendance.check_in_distance_m)
            """, (user_id, date, ts, shift_id, lat, lng, distance_m))
        conn.commit()


def check_out(user_id: str, date: str, ts) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_attendance SET check_out = %s
                WHERE user_id=%s AND date=%s
            """, (ts, user_id, date))
        conn.commit()


# ─── Multi-session attendance ──────────────────────────────────────────

def get_any_open_session(user_id: str) -> dict | None:
    """Trả về phiên chấm công đang mở của user (BẤT KỂ NGÀY).
    Dùng để chặn check-in mới khi phiên cũ chưa check-out — kể cả phiên từ ngày trước."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, date, check_in, check_out,
                           check_in_lat, check_in_lng, check_in_distance_m
                    FROM cc_attendance_sessions
                    WHERE user_id=%s AND check_out IS NULL
                    ORDER BY check_in DESC LIMIT 1
                """, (user_id,))
                row = cur.fetchone()
        if not row:
            return None
        cols = ['id', 'user_id', 'date', 'check_in', 'check_out',
                'check_in_lat', 'check_in_lng', 'check_in_distance_m']
        return dict(zip(cols, row))
    except Exception:
        return None


def get_open_session(user_id: str, date: str) -> dict | None:
    """Trả về phiên chấm công đang mở (đã check-in chưa check-out), nếu có."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, date, check_in, check_out,
                           check_in_lat, check_in_lng, check_in_distance_m
                    FROM cc_attendance_sessions
                    WHERE user_id=%s AND date=%s AND check_out IS NULL
                    ORDER BY check_in DESC LIMIT 1
                """, (user_id, date))
                row = cur.fetchone()
        if not row:
            return None
        cols = ['id', 'user_id', 'date', 'check_in', 'check_out',
                'check_in_lat', 'check_in_lng', 'check_in_distance_m']
        return dict(zip(cols, row))
    except Exception:
        return None


def get_today_sessions(user_id: str, date: str) -> list[dict]:
    """Trả về tất cả phiên chấm công trong ngày, sắp xếp theo thứ tự thời gian."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, date, check_in, check_out,
                           check_in_lat, check_in_lng, check_in_distance_m
                    FROM cc_attendance_sessions
                    WHERE user_id=%s AND date=%s
                    ORDER BY check_in ASC
                """, (user_id, date))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def create_checkin_session(user_id: str, date: str, ts,
                           lat: float | None = None, lng: float | None = None,
                           distance_m: int | None = None) -> int:
    """Tạo phiên check-in mới. Trả về session id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_attendance_sessions
                    (user_id, date, check_in, check_in_lat, check_in_lng, check_in_distance_m)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (user_id, date, ts, lat, lng, distance_m))
            session_id = cur.fetchone()[0]
        conn.commit()
    _sync_attendance_summary(user_id, date)
    return session_id


def close_open_session(user_id: str, date: str, ts) -> dict | None:
    """Đóng phiên đang mở. Trả về session vừa đóng hoặc None nếu không có."""
    open_sess = get_open_session(user_id, date)
    if not open_sess:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_attendance_sessions SET check_out=%s
                WHERE id=%s
            """, (ts, open_sess['id']))
        conn.commit()
    open_sess['check_out'] = ts
    _sync_attendance_summary(user_id, date)
    return open_sess


def backfill_checkout(user_id: str, date: str, ts) -> dict:
    """Đặt giờ check_out cho một ngày đã check-in nhưng chưa đóng ca.

    - Cập nhật phiên mở mới nhất trong `cc_attendance_sessions`.
    - Nếu không có phiên (dữ liệu legacy), fallback cập nhật `cc_attendance`.
    - Sau đó sync lại `cc_attendance` từ sessions.

    Trả về: {"ok": bool, "msg": str}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, check_in FROM cc_attendance_sessions
                WHERE user_id=%s AND date=%s AND check_out IS NULL
                ORDER BY check_in ASC
            """, (user_id, date))
            open_sessions = cur.fetchall()
            if open_sessions:
                sid, ci = open_sessions[-1]
                if ci and ts <= ci:
                    # Ca qua đêm: giờ ra sớm hơn giờ vào → tính sang ngày hôm sau
                    from datetime import timedelta as _td
                    ts = ts + _td(days=1)
                    if ts <= ci:
                        return {"ok": False, "msg": "Giờ ra phải sau giờ vào."}
                cur.execute(
                    "UPDATE cc_attendance_sessions SET check_out=%s WHERE id=%s",
                    (ts, sid),
                )
                conn.commit()
                _sync_attendance_summary(user_id, date)
                return {"ok": True, "msg": "Đã đóng ca."}
            # Fallback: cập nhật trực tiếp cc_attendance (legacy, không có session row)
            cur.execute(
                "SELECT check_in, check_out FROM cc_attendance "
                "WHERE user_id=%s AND date=%s",
                (user_id, date),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return {"ok": False, "msg": "Chưa có giờ vào cho ngày này."}
            if row[1] is not None:
                return {"ok": False, "msg": "Ngày này đã có giờ ra rồi."}
            if ts <= row[0]:
                # Ca qua đêm: giờ ra sớm hơn giờ vào → tính sang ngày hôm sau
                from datetime import timedelta as _td
                ts = ts + _td(days=1)
                if ts <= row[0]:
                    return {"ok": False, "msg": "Giờ ra phải sau giờ vào."}
            cur.execute(
                "UPDATE cc_attendance SET check_out=%s WHERE user_id=%s AND date=%s",
                (ts, user_id, date),
            )
        conn.commit()
    return {"ok": True, "msg": "Đã đóng ca (legacy)."}


def _sync_attendance_summary(user_id: str, date: str) -> None:
    """Cập nhật bản ghi tóm tắt cc_attendance từ các phiên session."""
    sessions = get_today_sessions(user_id, date)
    if not sessions:
        return
    first_ci = min((s['check_in'] for s in sessions if s['check_in']), default=None)
    closed = [s for s in sessions if s['check_out']]
    last_co = max((s['check_out'] for s in closed), default=None) if closed else None
    open_sess = any(s for s in sessions if not s['check_out'])
    if open_sess:
        last_co = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_attendance (user_id, date, check_in, check_out, status)
                VALUES (%s, %s, %s, %s, 'present')
                ON CONFLICT (user_id, date) DO UPDATE SET
                    check_in  = EXCLUDED.check_in,
                    check_out = EXCLUDED.check_out,
                    status    = 'present'
            """, (user_id, date, first_ci, last_co))
        conn.commit()


def get_session_total_hours(user_id: str, date: str) -> float:
    """Tổng giờ làm từ tất cả phiên đã hoàn thành (check_out != NULL)."""
    sessions = get_today_sessions(user_id, date)
    total = 0.0
    for s in sessions:
        if s['check_in'] and s['check_out']:
            diff = (s['check_out'] - s['check_in']).total_seconds()
            total += max(diff, 0)
    return round(total / 3600, 2)


def get_month_sessions(user_id: str, year: int, month: int) -> list[dict]:
    """Lấy tất cả phiên chấm công trong tháng cho 1 nhân viên."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, date, check_in, check_out,
                           check_in_lat, check_in_lng, check_in_distance_m
                    FROM cc_attendance_sessions
                    WHERE user_id=%s
                      AND EXTRACT(YEAR FROM date)=%s
                      AND EXTRACT(MONTH FROM date)=%s
                    ORDER BY check_in
                """, (user_id, year, month))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def get_month_sessions_all(year: int, month: int,
                           department: str | None = None) -> list[dict]:
    """Lấy tất cả phiên chấm công trong tháng (toàn bộ NV)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if department:
                    cur.execute("""
                        SELECT s.user_id, s.date, s.check_in, s.check_out,
                               e.full_name, e.cc_role
                        FROM cc_attendance_sessions s
                        LEFT JOIN cc_employees e ON e.user_id = s.user_id
                        LEFT JOIN users u ON u.id::text = s.user_id
                        LEFT JOIN teams t ON t.id = u.team_id
                        WHERE EXTRACT(YEAR FROM s.date)=%s
                          AND EXTRACT(MONTH FROM s.date)=%s
                          AND (e.department=%s OR t.team_code=%s)
                        ORDER BY s.check_in
                    """, (year, month, department, department))
                else:
                    cur.execute("""
                        SELECT s.user_id, s.date, s.check_in, s.check_out,
                               e.full_name, e.cc_role
                        FROM cc_attendance_sessions s
                        LEFT JOIN cc_employees e ON e.user_id = s.user_id
                        WHERE EXTRACT(YEAR FROM s.date)=%s
                          AND EXTRACT(MONTH FROM s.date)=%s
                        ORDER BY s.check_in
                    """, (year, month))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def get_admin_monthly_stats(year: int, month: int,
                            department: str | None = None) -> dict:
    """Per-employee monthly stats: tổng giờ, số ca, OT ngày thường, OT ngày lễ, giờ ngày lễ.
    Trả về dict {user_id: {total_h, sessions, reg_ot_h, hol_h, hol_ot_h}}."""
    from datetime import date as _date
    import calendar as _cal
    first = _date(year, month, 1)
    last  = _date(year, month, _cal.monthrange(year, month)[1])
    holidays = get_holidays_range(first, last)
    hol_keys = {k.isoformat() for k in holidays}

    rows = get_month_sessions_all(year, month, department=department)
    by_uid: dict = {}
    for r in rows:
        uid = str(r['user_id'])
        slot = by_uid.setdefault(uid, {
            'full_name': r.get('full_name') or uid,
            'cc_role':   r.get('cc_role') or '',
            'total_min': 0, 'sessions': 0,
            'reg_ot_min': 0,   # OT ngày thường
            'hol_min': 0,      # giờ làm ngày lễ
            'hol_ot_min': 0,   # OT ngày lễ
        })
        slot['sessions'] += 1
        d = r.get('date')
        date_key = d.isoformat() if hasattr(d, 'isoformat') else str(d)
        is_hol = date_key in hol_keys
        reg, ot = split_regular_overtime(r.get('check_in'), r.get('check_out'))
        total = reg + ot
        slot['total_min'] += total
        if is_hol:
            slot['hol_min']    += total
            slot['hol_ot_min'] += ot
        else:
            slot['reg_ot_min'] += ot
    # Chuyển sang giờ
    result = {}
    for uid, s in by_uid.items():
        result[uid] = {
            'full_name':  s['full_name'],
            'cc_role':    s['cc_role'],
            'total_h':    round(s['total_min'] / 60, 1),
            'sessions':   s['sessions'],
            'reg_ot_h':   round(s['reg_ot_min'] / 60, 1),
            'hol_h':      round(s['hol_min']    / 60, 1),
            'hol_ot_h':   round(s['hol_ot_min'] / 60, 1),
        }
    return result


def get_month_attendance(user_id: str, year: int, month: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, date, check_in, check_out, shift_id, status, note,
                       check_in_lat, check_in_lng, check_in_distance_m
                FROM cc_attendance
                WHERE user_id=%s
                  AND EXTRACT(YEAR FROM date)=%s
                  AND EXTRACT(MONTH FROM date)=%s
                ORDER BY date
            """, (user_id, year, month))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_month_sessions_grouped(user_id: str, year: int, month: int) -> dict:
    """Trả về dict {date_str: [session, ...]} cho toàn bộ tháng — dùng cho hiển thị bảng chi tiết."""
    sessions = get_month_sessions(user_id, year, month)
    result: dict = {}
    for s in sessions:
        key = s['date'].isoformat() if hasattr(s['date'], 'isoformat') else str(s['date'])
        result.setdefault(key, []).append(s)
    return result


def get_team_attendance_today(date: str, department: str | None = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if department:
                cur.execute("""
                    SELECT a.user_id, a.check_in, a.check_out, a.status,
                           e.full_name, e.cc_role
                    FROM cc_attendance a
                    LEFT JOIN cc_employees e ON e.user_id = a.user_id
                    LEFT JOIN users u ON u.id::text = a.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE a.date = %s AND (e.department = %s OR t.team_code = %s)
                      AND (e.resigned_at IS NULL OR e.resigned_at > %s)
                    ORDER BY a.check_in NULLS LAST
                """, (date, department, department, date))
            else:
                cur.execute("""
                    SELECT a.user_id, a.check_in, a.check_out, a.status,
                           e.full_name, e.cc_role
                    FROM cc_attendance a
                    LEFT JOIN cc_employees e ON e.user_id = a.user_id
                    WHERE a.date = %s
                      AND (e.resigned_at IS NULL OR e.resigned_at > %s)
                    ORDER BY a.check_in NULLS LAST
                """, (date, date))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_all_employees_status_today(date: str, department: str | None = None) -> list[dict]:
    """
    Trả về TẤT CẢ nhân viên active kèm trạng thái chấm công hôm nay.
    Bao gồm cả người chưa check-in (vắng).
    Nếu department != None, chỉ lấy nhân viên trong bộ phận đó.
    Sắp xếp: đang làm → đã về → chưa đến.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if department:
                cur.execute("""
                    SELECT e.user_id, e.full_name, e.cc_role,
                           a.check_in, a.check_out, a.status,
                           a.check_in_lat, a.check_in_lng, a.check_in_distance_m,
                           CASE
                             WHEN a.check_in IS NOT NULL AND a.check_out IS NULL THEN 1
                             WHEN a.check_in IS NOT NULL AND a.check_out IS NOT NULL THEN 2
                             ELSE 3
                           END AS sort_order
                    FROM cc_employees e
                    LEFT JOIN cc_attendance a ON a.user_id = e.user_id AND a.date = %s
                    LEFT JOIN users u ON u.id::text = e.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE (e.department = %s OR t.team_code = %s)
                      AND (e.resigned_at IS NULL OR e.resigned_at > %s)
                    ORDER BY sort_order, a.check_in ASC NULLS LAST, e.full_name
                """, (date, department, department, date))
            else:
                cur.execute("""
                    SELECT e.user_id, e.full_name, e.cc_role,
                           a.check_in, a.check_out, a.status,
                           a.check_in_lat, a.check_in_lng, a.check_in_distance_m,
                           CASE
                             WHEN a.check_in IS NOT NULL AND a.check_out IS NULL THEN 1
                             WHEN a.check_in IS NOT NULL AND a.check_out IS NOT NULL THEN 2
                             ELSE 3
                           END AS sort_order
                    FROM cc_employees e
                    LEFT JOIN cc_attendance a ON a.user_id = e.user_id AND a.date = %s
                    WHERE e.resigned_at IS NULL OR e.resigned_at > %s
                    ORDER BY sort_order, a.check_in ASC NULLS LAST, e.full_name
                """, (date, date))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_monthly_report(year: int, month: int, settings: dict | None = None,
                       department: str | None = None) -> list[dict]:
    """
    Báo cáo tháng.
    - Kho (packing) + Sale → total_hours (giờ làm)
    - Marketing (adser)   → total_cong  (số công, tính theo giờ hành chính)
    department: lọc theo team (qua cc_employees.department hoặc users.team_id)
    """
    s = settings or {}
    work_start = s.get('work_start', '08:00')
    work_end   = s.get('work_end',   '17:30')
    try:
        min_hours = float(s.get('min_hours_cong', 9))
        half_hours = float(s.get('half_hours_cong', 0) or 0)
    except Exception:
        min_hours = 9.0

    with get_conn() as conn:
        with conn.cursor() as cur:
            if department:
                cur.execute("""
                    SELECT a.user_id, e.full_name, e.cc_role,
                           COUNT(*) as total_days,
                           COUNT(a.check_in) as present_days,
                           COUNT(a.check_out) as full_days,
                           SUM(CASE WHEN a.check_out IS NOT NULL AND a.check_in IS NOT NULL
                               THEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600 ELSE 0 END) as total_hours,
                           array_agg(a.check_in  ORDER BY a.date) FILTER (WHERE a.check_in  IS NOT NULL) as ci_list,
                           array_agg(a.check_out ORDER BY a.date) FILTER (WHERE a.check_out IS NOT NULL) as co_list
                    FROM cc_attendance a
                    LEFT JOIN cc_employees e ON e.user_id = a.user_id
                    LEFT JOIN users u ON u.id::text = a.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE EXTRACT(YEAR FROM a.date)=%s AND EXTRACT(MONTH FROM a.date)=%s
                      AND (e.department=%s OR t.team_code=%s)
                    GROUP BY a.user_id, e.full_name, e.cc_role
                    ORDER BY e.full_name
                """, (year, month, department, department))
            else:
                cur.execute("""
                    SELECT a.user_id, e.full_name, e.cc_role,
                           COUNT(*) as total_days,
                           COUNT(a.check_in) as present_days,
                           COUNT(a.check_out) as full_days,
                           SUM(CASE WHEN a.check_out IS NOT NULL AND a.check_in IS NOT NULL
                               THEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600 ELSE 0 END) as total_hours,
                           array_agg(a.check_in  ORDER BY a.date) FILTER (WHERE a.check_in  IS NOT NULL) as ci_list,
                           array_agg(a.check_out ORDER BY a.date) FILTER (WHERE a.check_out IS NOT NULL) as co_list
                    FROM cc_attendance a
                    LEFT JOIN cc_employees e ON e.user_id = a.user_id
                    WHERE EXTRACT(YEAR FROM a.date)=%s AND EXTRACT(MONTH FROM a.date)=%s
                    GROUP BY a.user_id, e.full_name, e.cc_role
                    ORDER BY e.full_name
                """, (year, month))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    for row in rows:
        ci_list = row.pop('ci_list') or []
        co_list = row.pop('co_list') or []
        if row.get('cc_role') == 'adser':
            cong = sum(
                calc_cong_value(ci, co, work_start, work_end, min_hours, half_hours)
                for ci, co in zip(ci_list, co_list)
            )
            row['total_cong'] = round(cong, 1)
        else:
            row['total_cong'] = None
    return rows


# ─── Tasks ──────────────────────────────────────────

def get_tasks(user_id: str = None, role_type: str = None,
              status: str = None, limit: int = 100) -> list[dict]:
    where = []
    params = []
    if user_id:
        where.append("t.assigned_to = %s")
        params.append(user_id)
    if role_type:
        where.append("(t.role_type = %s OR t.role_type IS NULL)")
        params.append(role_type)
    if status:
        where.append("t.status = %s")
        params.append(status)
    sql = """
        SELECT t.id, t.title, t.description, t.task_type, t.role_type,
               t.assigned_to, t.assigned_by, t.priority, t.status,
               t.due_date, t.completed_at, t.created_at,
               e.full_name as assigned_name,
               b.full_name as creator_name
        FROM cc_tasks t
        LEFT JOIN cc_employees e ON e.user_id = t.assigned_to
        LEFT JOIN cc_employees b ON b.user_id = t.assigned_by
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.priority DESC, t.created_at DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_all_tasks(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.title, t.description, t.task_type, t.role_type,
                       t.assigned_to, t.assigned_by, t.priority, t.status,
                       t.due_date, t.completed_at, t.created_at,
                       e.full_name as assigned_name,
                       b.full_name as creator_name
                FROM cc_tasks t
                LEFT JOIN cc_employees e ON e.user_id = t.assigned_to
                LEFT JOIN cc_employees b ON b.user_id = t.assigned_by
                ORDER BY t.created_at DESC LIMIT %s
            """, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_task(title: str, description: str, task_type: str, role_type: str,
                assigned_to: str, assigned_by: str, priority: str,
                due_date=None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_tasks
                    (title, description, task_type, role_type, assigned_to, assigned_by, priority, due_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (title, description, task_type, role_type or None,
                  assigned_to or None, assigned_by, priority, due_date or None))
            task_id = cur.fetchone()[0]
        conn.commit()
    return task_id


def get_task(task_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.title, t.description, t.task_type, t.role_type,
                       t.assigned_to, t.assigned_by, t.priority, t.status,
                       t.due_date, t.completed_at, t.created_at,
                       e.full_name as assigned_name,
                       b.full_name as creator_name
                FROM cc_tasks t
                LEFT JOIN cc_employees e ON e.user_id = t.assigned_to
                LEFT JOIN cc_employees b ON b.user_id = t.assigned_by
                WHERE t.id = %s
            """, (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def update_task_status(task_id: int, status: str) -> None:
    from datetime import datetime
    completed_at = datetime.now() if status == 'done' else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_tasks SET status=%s, completed_at=%s WHERE id=%s
            """, (status, completed_at, task_id))
        conn.commit()


def delete_task(task_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cc_tasks WHERE id=%s", (task_id,))
        conn.commit()


# ─── Holidays & Overtime (tăng ca) ─────────────────────────
#
# Quy tắc giờ làm:
#   - Giờ hành chính: 08:00 → 22:00 (REG)
#   - Ngoài khoảng đó (trước 8h / sau 22h) → tăng ca (OT)
#   - Ngày lễ: hệ số lương x2 (apply cho cả reg & ot của ngày đó)
# Lưu ý: lưu timestamps ở Asia/Ho_Chi_Minh, split theo giờ local.

REG_START_H = 0     # 00:00 — giờ trước 8h sáng vẫn tính giờ thường
REG_END_H   = 22    # 22:00 — sau 22h mới tính tăng ca

# Chỉ các vai trò này mới được tính tăng ca (sau 22h).
# Các đội khác (adser, kế toán, IT, mua hàng...) bấm khuya vẫn KHÔNG tính OT.
OT_ELIGIBLE_ROLES = {'sale', 'packing'}


def is_ot_eligible(cc_role: str | None) -> bool:
    return (cc_role or '').strip() in OT_ELIGIBLE_ROLES

# Seed VN holidays (solar dates). Lunar holidays (Tết, Giỗ Tổ) cần convert
# hằng năm — seed cho năm hiện tại + 1 năm tiếp theo.
_VN_HOLIDAYS_FIXED = [
    # (month, day, name)
    (1,  1, "Tết Dương lịch"),
    (4, 30, "Ngày Giải phóng miền Nam"),
    (5,  1, "Quốc tế Lao động"),
    (9,  2, "Quốc khánh"),
]
# Lunar holidays per year — cần cập nhật hàng năm
# Key: year, value: list of (solar_month, solar_day, name)
_VN_HOLIDAYS_LUNAR = {
    2025: [
        (1, 28, "Tết Âm lịch (30)"), (1, 29, "Mồng 1 Tết"),
        (1, 30, "Mồng 2 Tết"), (1, 31, "Mồng 3 Tết"), (2, 1, "Mồng 4 Tết"),
        (4, 7, "Giỗ Tổ Hùng Vương"),
    ],
    2026: [
        (2, 16, "Tết Âm lịch (30)"), (2, 17, "Mồng 1 Tết"),
        (2, 18, "Mồng 2 Tết"), (2, 19, "Mồng 3 Tết"), (2, 20, "Mồng 4 Tết"),
        (4, 26, "Giỗ Tổ Hùng Vương"),
    ],
    2027: [
        (2, 6, "Tết Âm lịch (30)"), (2, 7, "Mồng 1 Tết"),
        (2, 8, "Mồng 2 Tết"), (2, 9, "Mồng 3 Tết"), (2, 10, "Mồng 4 Tết"),
        (4, 15, "Giỗ Tổ Hùng Vương"),
    ],
}


def _ensure_attendance_ot_schema() -> None:
    """Tạo bảng cc_holidays + thêm cột OT vào cc_attendance/sessions. Idempotent."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cc_holidays (
                    holiday_date DATE PRIMARY KEY,
                    name         VARCHAR(120) NOT NULL,
                    multiplier   NUMERIC(4,2) NOT NULL DEFAULT 2.0,
                    is_lunar     BOOLEAN DEFAULT FALSE,
                    note         TEXT,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            # Persist OT minutes for faster aggregations (computed on-read, cached here)
            cur.execute("ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS regular_minutes INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS overtime_minutes INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS pay_multiplier NUMERIC(4,2) DEFAULT 1.0")
        conn.commit()


def seed_default_holidays_vn(year: int) -> int:
    """Seed ngày lễ VN cho 1 năm. ON CONFLICT DO NOTHING để không ghi đè sửa tay."""
    _ensure_attendance_ot_schema()
    from datetime import date as _date
    inserted = 0
    rows = [(_date(year, m, d), name, False) for (m, d, name) in _VN_HOLIDAYS_FIXED]
    rows += [(_date(year, m, d), name, True) for (m, d, name) in _VN_HOLIDAYS_LUNAR.get(year, [])]
    with get_conn() as conn:
        with conn.cursor() as cur:
            for hd, name, is_lunar in rows:
                cur.execute("""
                    INSERT INTO cc_holidays (holiday_date, name, multiplier, is_lunar)
                    VALUES (%s, %s, 2.0, %s)
                    ON CONFLICT (holiday_date) DO NOTHING
                """, (hd, name, is_lunar))
                inserted += cur.rowcount
        conn.commit()
    return inserted


def get_holidays_range(start_date, end_date) -> dict:
    """Trả dict {date: {name, multiplier, is_lunar}} trong khoảng [start, end]."""
    _ensure_attendance_ot_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT holiday_date, name, multiplier, is_lunar
                FROM cc_holidays
                WHERE holiday_date BETWEEN %s AND %s
                ORDER BY holiday_date
            """, (start_date, end_date))
            rows = cur.fetchall()
    out = {}
    for hd, name, mult, is_lunar in rows:
        out[hd] = {"name": name, "multiplier": float(mult),
                   "is_lunar": bool(is_lunar)}
    return out


def list_holidays(year: int | None = None) -> list[dict]:
    _ensure_attendance_ot_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if year:
                cur.execute("""
                    SELECT holiday_date, name, multiplier, is_lunar, note
                    FROM cc_holidays
                    WHERE EXTRACT(YEAR FROM holiday_date)=%s
                    ORDER BY holiday_date
                """, (year,))
            else:
                cur.execute("""
                    SELECT holiday_date, name, multiplier, is_lunar, note
                    FROM cc_holidays ORDER BY holiday_date
                """)
            rows = cur.fetchall()
    return [{"holiday_date": r[0].isoformat() if r[0] else None,
             "name": r[1], "multiplier": float(r[2]),
             "is_lunar": bool(r[3]), "note": r[4] or ""} for r in rows]


def upsert_holiday(holiday_date, name: str, multiplier: float = 2.0,
                   is_lunar: bool = False, note: str = "") -> None:
    _ensure_attendance_ot_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_holidays (holiday_date, name, multiplier, is_lunar, note)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (holiday_date) DO UPDATE SET
                    name = EXCLUDED.name,
                    multiplier = EXCLUDED.multiplier,
                    is_lunar = EXCLUDED.is_lunar,
                    note = EXCLUDED.note
            """, (holiday_date, name, multiplier, is_lunar, note))
        conn.commit()


def delete_holiday(holiday_date) -> bool:
    _ensure_attendance_ot_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cc_holidays WHERE holiday_date=%s", (holiday_date,))
            affected = cur.rowcount
        conn.commit()
    return affected > 0


def split_regular_overtime(check_in_ts, check_out_ts,
                           reg_start_h: int = REG_START_H,
                           reg_end_h: int = REG_END_H,
                           ot_eligible: bool = True) -> tuple[int, int]:
    """Chia (check_in, check_out) thành (regular_minutes, overtime_minutes).
    Dùng local time của hai timestamps (giả định cùng ngày / sessions đã split theo ngày).

    - REG window: [reg_start_h:00, reg_end_h:00) — sau 22h luôn tính OT cho mọi role.
    - ot_eligible chỉ ảnh hưởng đến tính lương OT, không ảnh hưởng đến việc ghi nhận giờ OT.
    """
    if not check_in_ts or not check_out_ts:
        return (0, 0)
    try:
        import pytz
        tz = pytz.timezone('Asia/Ho_Chi_Minh')
        ci = check_in_ts.astimezone(tz) if check_in_ts.tzinfo else tz.localize(check_in_ts)
        co = check_out_ts.astimezone(tz) if check_out_ts.tzinfo else tz.localize(check_out_ts)
    except Exception:
        ci, co = check_in_ts, check_out_ts
    if co <= ci:
        return (0, 0)
    # REG window: 00:00 → 22:00, OT: sau 22:00 — áp dụng cho mọi role
    day = ci.date()
    from datetime import datetime, time
    try:
        reg_start = ci.replace(hour=reg_start_h, minute=0, second=0, microsecond=0)
        reg_end   = ci.replace(hour=reg_end_h,   minute=0, second=0, microsecond=0)
    except Exception:
        reg_start = datetime.combine(day, time(reg_start_h, 0))
        reg_end   = datetime.combine(day, time(reg_end_h, 0))
    total    = (co - ci).total_seconds()
    # overlap REG (00:00 → 22:00)
    ov_start = max(ci, reg_start)
    ov_end   = min(co, reg_end)
    reg_sec  = max(0.0, (ov_end - ov_start).total_seconds())
    ot_sec   = max(0.0, total - reg_sec)
    return (int(reg_sec // 60), int(ot_sec // 60))


def get_month_calendar(user_id: str, year: int, month: int) -> dict:
    """Trả về {days: {YYYY-MM-DD: {reg_min, ot_min, multiplier, holiday_name, sessions:[...]}},
               totals: {reg_min, ot_min, pay_hours}}.
    `pay_hours` = (reg + ot) * multiplier (ngày thường multiplier=1)."""
    from datetime import date as _date
    import calendar as _cal
    _ensure_attendance_ot_schema()
    # Range toàn tháng
    first = _date(year, month, 1)
    last  = _date(year, month, _cal.monthrange(year, month)[1])
    holidays = get_holidays_range(first, last)
    sessions = get_month_sessions(user_id, year, month)

    # Xác định nhân viên có được tính OT không (chỉ sale & packing)
    emp = get_employee(user_id)
    ot_ok = is_ot_eligible(emp.get('cc_role') if emp else None)

    days: dict[str, dict] = {}
    for s in sessions:
        d = s.get('date')
        if not d:
            continue
        key = d.isoformat() if hasattr(d, 'isoformat') else str(d)
        slot = days.setdefault(key, {
            'reg_min': 0, 'ot_min': 0, 'sessions': [],
            'holiday_name': None, 'multiplier': 1.0, 'is_lunar': False,
            'ot_eligible': ot_ok,
        })
        reg, ot = split_regular_overtime(s.get('check_in'), s.get('check_out'),
                                         ot_eligible=ot_ok)
        slot['reg_min'] += reg
        slot['ot_min']  += ot
        slot['sessions'].append({
            'check_in':  s['check_in'].isoformat()  if s.get('check_in')  else None,
            'check_out': s['check_out'].isoformat() if s.get('check_out') else None,
            'reg_min': reg, 'ot_min': ot,
        })
    # Gắn holiday vào các ngày có ghi nhận HOẶC toàn bộ holiday trong tháng
    for hd, h in holidays.items():
        key = hd.isoformat()
        slot = days.setdefault(key, {
            'reg_min': 0, 'ot_min': 0, 'sessions': [],
            'holiday_name': None, 'multiplier': 1.0, 'is_lunar': False,
        })
        slot['holiday_name'] = h['name']
        slot['multiplier']   = h['multiplier']
        slot['is_lunar']     = h['is_lunar']

    tot_reg = sum(d['reg_min'] for d in days.values())
    tot_ot  = sum(d['ot_min']  for d in days.values())
    pay_min = 0.0
    for d in days.values():
        pay_min += (d['reg_min'] + d['ot_min']) * d['multiplier']
    return {
        'days': days,
        'ot_eligible': ot_ok,
        'cc_role': (emp.get('cc_role') if emp else None),
        'totals': {
            'reg_min': tot_reg,
            'ot_min':  tot_ot,
            'pay_hours': round(pay_min / 60.0, 2),
            'total_hours': round((tot_reg + tot_ot) / 60.0, 2),
        },
    }


def get_team_overtime_totals(year: int, month: int,
                             department: str | None = None) -> dict:
    """Tổng hợp giờ OT + giờ thường theo từng nhân viên trong team.
    department: 'sale' hoặc 'packing' (hoặc team_code khác).
    Returns {members:[{user_id, full_name, cc_role, reg_h, ot_h, pay_h}], totals:{...}}.
    """
    _ensure_attendance_ot_schema()
    from datetime import date as _date
    import calendar as _cal
    first = _date(year, month, 1)
    last  = _date(year, month, _cal.monthrange(year, month)[1])
    holidays = get_holidays_range(first, last)
    h_mult: dict = {k: v['multiplier'] for k, v in holidays.items()}

    rows = get_month_sessions_all(year, month, department=department)
    by_uid: dict = {}
    for r in rows:
        uid = str(r['user_id']) if r.get('user_id') is not None else None
        if not uid:
            continue
        slot = by_uid.setdefault(uid, {
            'user_id': uid,
            'full_name': r.get('full_name') or uid,
            'cc_role':   r.get('cc_role') or '',
            'reg_min': 0, 'ot_min': 0, 'pay_min': 0.0,
        })
        ot_ok = is_ot_eligible(r.get('cc_role'))
        reg, ot = split_regular_overtime(r.get('check_in'), r.get('check_out'),
                                         ot_eligible=ot_ok)
        slot['reg_min'] += reg
        slot['ot_min']  += ot
        mult = h_mult.get(r.get('date'), 1.0)
        slot['pay_min'] += (reg + ot) * mult

    members = []
    for s in by_uid.values():
        members.append({
            'user_id':   s['user_id'],
            'full_name': s['full_name'],
            'cc_role':   s['cc_role'],
            'reg_h':     round(s['reg_min'] / 60.0, 2),
            'ot_h':      round(s['ot_min']  / 60.0, 2),
            'pay_h':     round(s['pay_min'] / 60.0, 2),
        })
    members.sort(key=lambda x: x['ot_h'], reverse=True)
    tot_reg = sum(s['reg_min'] for s in by_uid.values())
    tot_ot  = sum(s['ot_min']  for s in by_uid.values())
    tot_pay = sum(s['pay_min'] for s in by_uid.values())
    return {
        'department': department or 'all',
        'year': year, 'month': month,
        'members': members,
        'totals': {
            'reg_h':  round(tot_reg / 60.0, 2),
            'ot_h':   round(tot_ot  / 60.0, 2),
            'pay_h':  round(tot_pay / 60.0, 2),
            'staff_count': len(members),
        },
    }


# ─── Task progress updates (reuse cc_task_comments table) ─────────

def _ensure_task_progress_schema() -> None:
    """Idempotent: tạo bảng cc_task_comments + cột progress_type nếu còn thiếu."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cc_task_comments (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES cc_tasks(id) ON DELETE CASCADE,
                    user_id VARCHAR(50) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # progress_type: 'done' (đã xong) | 'pending' (chưa xong / khó khăn) | 'note' (ghi chú)
            cur.execute("ALTER TABLE cc_task_comments ADD COLUMN IF NOT EXISTS progress_type VARCHAR(20) DEFAULT 'note'")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cc_task_comments_task ON cc_task_comments(task_id)")
        conn.commit()


def get_task_progress(task_id: int, limit: int = 100) -> list[dict]:
    _ensure_task_progress_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.task_id, c.user_id, c.content, c.progress_type, c.created_at,
                       e.full_name as user_name
                FROM cc_task_comments c
                LEFT JOIN cc_employees e ON e.user_id = c.user_id
                WHERE c.task_id = %s
                ORDER BY c.created_at DESC
                LIMIT %s
            """, (task_id, limit))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def add_task_progress(task_id: int, user_id: str, content: str,
                      progress_type: str = 'note') -> int:
    _ensure_task_progress_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_task_comments (task_id, user_id, content, progress_type)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (task_id, user_id, content, progress_type))
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def delete_task_progress(progress_id: int, user_id: str, is_manager: bool = False) -> bool:
    """Xoá 1 entry tiến độ. Chỉ cho phép chính chủ hoặc manager."""
    _ensure_task_progress_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if is_manager:
                cur.execute("DELETE FROM cc_task_comments WHERE id=%s", (progress_id,))
            else:
                cur.execute("DELETE FROM cc_task_comments WHERE id=%s AND user_id=%s",
                            (progress_id, user_id))
            affected = cur.rowcount
        conn.commit()
    return affected > 0


def count_task_progress(task_ids: list) -> dict:
    """Đếm số cập nhật tiến độ theo task_id (cho badge trên card). Trả về {task_id: count}."""
    if not task_ids:
        return {}
    _ensure_task_progress_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT task_id, COUNT(*)::int FROM cc_task_comments
                WHERE task_id = ANY(%s) GROUP BY task_id
            """, (list(task_ids),))
            return {row[0]: row[1] for row in cur.fetchall()}


def get_task_report(department: str | None = None) -> list[dict]:
    """Báo cáo công việc tổng hợp theo nhân viên. Lọc theo bộ phận nếu được chỉ định."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if department:
                cur.execute("""
                    SELECT
                        e.user_id,
                        e.full_name,
                        e.cc_role,
                        e.department,
                        COUNT(t.id)                                              AS total,
                        COUNT(t.id) FILTER (WHERE t.status = 'done')            AS done_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'in-progress')     AS in_progress_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'todo')            AS todo_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'cancelled')       AS cancelled_count,
                        MAX(t.completed_at)                                      AS last_done_at
                    FROM cc_employees e
                    LEFT JOIN cc_tasks t ON t.assigned_to = e.user_id
                    LEFT JOIN users u ON u.id::text = e.user_id
                    LEFT JOIN teams tm ON tm.id = u.team_id
                    WHERE e.department = %s OR tm.team_code = %s
                    GROUP BY e.user_id, e.full_name, e.cc_role, e.department
                    ORDER BY done_count DESC, total DESC, e.full_name
                """, (department, department))
            else:
                cur.execute("""
                    SELECT
                        e.user_id,
                        e.full_name,
                        e.cc_role,
                        e.department,
                        COUNT(t.id)                                              AS total,
                        COUNT(t.id) FILTER (WHERE t.status = 'done')            AS done_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'in-progress')     AS in_progress_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'todo')            AS todo_count,
                        COUNT(t.id) FILTER (WHERE t.status = 'cancelled')       AS cancelled_count,
                        MAX(t.completed_at)                                      AS last_done_at
                    FROM cc_employees e
                    LEFT JOIN cc_tasks t ON t.assigned_to = e.user_id
                    GROUP BY e.user_id, e.full_name, e.cc_role, e.department
                    ORDER BY done_count DESC, total DESC, e.full_name
                """)
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                total = d['total'] or 0
                done  = d['done_count'] or 0
                d['completion_rate'] = round(done / total * 100) if total > 0 else 0
                rows.append(d)
            return rows


# ─── KPI ──────────────────────────────────────────

def get_kpi(user_id: str, date: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cc_kpi_daily WHERE user_id=%s AND date=%s",
                        (user_id, date))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def upsert_kpi(user_id: str, date: str, cc_role: str, data: dict) -> None:
    from datetime import datetime
    fields = [
        'ads_budget', 'ads_orders', 'ads_revenue', 'ads_roas', 'ads_new_orders',
        'sale_contacts', 'sale_closed', 'sale_revenue', 'sale_returned',
        'pack_orders', 'pack_returned', 'pack_errors',
        'purch_requests', 'purch_completed', 'note'
    ]
    vals = {f: data.get(f, 0) for f in fields}
    vals['note'] = data.get('note', '')
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_kpi_daily
                    (user_id, date, cc_role,
                     ads_budget, ads_orders, ads_revenue, ads_roas, ads_new_orders,
                     sale_contacts, sale_closed, sale_revenue, sale_returned,
                     pack_orders, pack_returned, pack_errors,
                     purch_requests, purch_completed, note, updated_at)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, date) DO UPDATE SET
                    ads_budget=EXCLUDED.ads_budget, ads_orders=EXCLUDED.ads_orders,
                    ads_revenue=EXCLUDED.ads_revenue, ads_roas=EXCLUDED.ads_roas,
                    ads_new_orders=EXCLUDED.ads_new_orders,
                    sale_contacts=EXCLUDED.sale_contacts, sale_closed=EXCLUDED.sale_closed,
                    sale_revenue=EXCLUDED.sale_revenue, sale_returned=EXCLUDED.sale_returned,
                    pack_orders=EXCLUDED.pack_orders, pack_returned=EXCLUDED.pack_returned,
                    pack_errors=EXCLUDED.pack_errors,
                    purch_requests=EXCLUDED.purch_requests, purch_completed=EXCLUDED.purch_completed,
                    note=EXCLUDED.note, updated_at=EXCLUDED.updated_at
            """, (user_id, date, cc_role,
                  vals['ads_budget'], vals['ads_orders'], vals['ads_revenue'],
                  vals['ads_roas'], vals['ads_new_orders'],
                  vals['sale_contacts'], vals['sale_closed'],
                  vals['sale_revenue'], vals['sale_returned'],
                  vals['pack_orders'], vals['pack_returned'], vals['pack_errors'],
                  vals['purch_requests'], vals['purch_completed'],
                  vals['note'], datetime.now()))
        conn.commit()


def get_kpi_history(user_id: str, year: int, month: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM cc_kpi_daily
                WHERE user_id=%s
                  AND EXTRACT(YEAR FROM date)=%s
                  AND EXTRACT(MONTH FROM date)=%s
                ORDER BY date DESC
            """, (user_id, year, month))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─── Announcements ──────────────────────────────────────────

def get_announcements(target_role: str = None, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if target_role and target_role != 'admin':
                cur.execute("""
                    SELECT a.id, a.title, a.content, a.author_id, a.is_pinned, a.created_at,
                           e.full_name as author_name
                    FROM cc_announcements a
                    LEFT JOIN cc_employees e ON e.user_id = a.author_id
                    WHERE a.target_role IS NULL OR a.target_role = %s
                    ORDER BY a.is_pinned DESC, a.created_at DESC LIMIT %s
                """, (target_role, limit))
            else:
                cur.execute("""
                    SELECT a.id, a.title, a.content, a.author_id, a.is_pinned, a.created_at,
                           e.full_name as author_name
                    FROM cc_announcements a
                    LEFT JOIN cc_employees e ON e.user_id = a.author_id
                    ORDER BY a.is_pinned DESC, a.created_at DESC LIMIT %s
                """, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_announcement(title: str, content: str, author_id: str,
                        target_role: str = None, is_pinned: bool = False) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_announcements (title, content, author_id, target_role, is_pinned)
                VALUES (%s, %s, %s, %s, %s)
            """, (title, content, author_id, target_role or None, is_pinned))
        conn.commit()


def delete_announcement(ann_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cc_announcements WHERE id=%s", (ann_id,))
        conn.commit()


def update_announcement(ann_id: int, title: str, content: str,
                        target_role: str = None, is_pinned: bool = False) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_announcements
                SET title=%s, content=%s, target_role=%s, is_pinned=%s
                WHERE id=%s
            """, (title, content, target_role or None, is_pinned, ann_id))
        conn.commit()


# ─── Bổ sung công (attendance correction requests) ──────────

def create_attendance_request(user_id: str, work_date: str, sessions: list,
                              reason: str = '') -> int:
    """NV gửi đề nghị bổ sung/chỉnh công.

    sessions: [{"check_in": "HH:MM", "check_out": "HH:MM"}, ...]
    Trả về id của request.
    """
    import json as _json
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cc_attendance_requests
                    (user_id, work_date, sessions, reason, status)
                VALUES (%s, %s, %s::jsonb, %s, 'pending')
                RETURNING id
            """, (user_id, work_date, _json.dumps(sessions or []), reason or ''))
            req_id = cur.fetchone()[0]
        conn.commit()
    return req_id


def list_attendance_requests(status: str | None = None,
                             user_id: str | None = None,
                             department: str | None = None,
                             limit: int = 200) -> list[dict]:
    """Danh sách đề nghị bổ sung công.

    - status: lọc theo trạng thái (pending/approved/rejected/cancelled).
    - user_id: chỉ lấy của 1 NV.
    - department: giới hạn theo team (leader chỉ thấy team mình).
    """
    where = []
    params = []
    if status:
        where.append("r.status = %s")
        params.append(status)
    if user_id:
        where.append("r.user_id = %s")
        params.append(user_id)
    if department:
        where.append("(e.department = %s OR t.team_code = %s)")
        params.extend([department, department])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT r.id, r.user_id, r.work_date, r.sessions, r.reason,
                       r.status, r.reviewed_by, r.reviewed_at, r.review_note,
                       r.created_at, r.updated_at,
                       e.full_name, e.department, e.cc_role,
                       rev.full_name AS reviewer_name
                FROM cc_attendance_requests r
                LEFT JOIN cc_employees e ON e.user_id = r.user_id
                LEFT JOIN users u ON u.id::text = r.user_id
                LEFT JOIN teams t ON t.id = u.team_id
                LEFT JOIN cc_employees rev ON rev.user_id = r.reviewed_by
                {where_sql}
                ORDER BY
                    CASE WHEN r.status='pending' THEN 0 ELSE 1 END,
                    r.created_at DESC
                LIMIT %s
            """, tuple(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def count_pending_requests(department: str | None = None) -> int:
    """Đếm số đề nghị đang chờ duyệt (cho badge thông báo)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if department:
                    cur.execute("""
                        SELECT COUNT(*)
                        FROM cc_attendance_requests r
                        LEFT JOIN cc_employees e ON e.user_id = r.user_id
                        LEFT JOIN users u ON u.id::text = r.user_id
                        LEFT JOIN teams t ON t.id = u.team_id
                        WHERE r.status='pending'
                          AND (e.department=%s OR t.team_code=%s)
                    """, (department, department))
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM cc_attendance_requests WHERE status='pending'"
                    )
                return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def get_attendance_request(req_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, r.user_id, r.work_date, r.sessions, r.reason,
                       r.status, r.reviewed_by, r.reviewed_at, r.review_note,
                       r.created_at, r.updated_at,
                       e.full_name, e.department
                FROM cc_attendance_requests r
                LEFT JOIN cc_employees e ON e.user_id = r.user_id
                WHERE r.id = %s
            """, (req_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def cancel_attendance_request(req_id: int, by_user_id: str) -> dict:
    """NV tự huỷ đề nghị của mình khi còn pending."""
    req = get_attendance_request(req_id)
    if not req:
        return {"ok": False, "msg": "Không tìm thấy đề nghị."}
    if str(req['user_id']) != str(by_user_id):
        return {"ok": False, "msg": "Chỉ người gửi mới được huỷ."}
    if req['status'] != 'pending':
        return {"ok": False, "msg": "Đề nghị đã được xử lý, không thể huỷ."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_attendance_requests
                SET status='cancelled', updated_at=now()
                WHERE id=%s
            """, (req_id,))
        conn.commit()
    return {"ok": True, "msg": "Đã huỷ đề nghị."}


def reject_attendance_request(req_id: int, reviewer_id: str, note: str = '') -> dict:
    req = get_attendance_request(req_id)
    if not req:
        return {"ok": False, "msg": "Không tìm thấy đề nghị."}
    if req['status'] != 'pending':
        return {"ok": False, "msg": "Đề nghị đã được xử lý."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cc_attendance_requests
                SET status='rejected', reviewed_by=%s, reviewed_at=now(),
                    review_note=%s, updated_at=now()
                WHERE id=%s
            """, (reviewer_id, note or '', req_id))
        conn.commit()
    return {"ok": True, "msg": "Đã từ chối đề nghị."}


def approve_attendance_request(req_id: int, reviewer_id: str, note: str = '') -> dict:
    """Duyệt đề nghị: tạo các phiên cc_attendance_sessions cho ngày được đề nghị,
    rồi tính lại tóm tắt cc_attendance bằng _sync_attendance_summary.
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    _VN = ZoneInfo('Asia/Ho_Chi_Minh')

    req = get_attendance_request(req_id)
    if not req:
        return {"ok": False, "msg": "Không tìm thấy đề nghị."}
    if req['status'] != 'pending':
        return {"ok": False, "msg": "Đề nghị đã được xử lý."}

    uid = str(req['user_id'])
    work_date = req['work_date']
    date_str = work_date.strftime('%Y-%m-%d') if hasattr(work_date, 'strftime') else str(work_date)
    raw_sessions = req['sessions'] or []
    if isinstance(raw_sessions, str):
        import json as _json
        try:
            raw_sessions = _json.loads(raw_sessions)
        except Exception:
            raw_sessions = []

    def _parse(hhmm: str):
        hhmm = (hhmm or '').strip()
        if not hhmm:
            return None
        fmt = '%H:%M:%S' if hhmm.count(':') == 2 else '%H:%M'
        return _dt.strptime(hhmm, fmt)

    base_d = _dt.strptime(date_str, '%Y-%m-%d')
    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ca in raw_sessions:
                ci_t = _parse(ca.get('check_in'))
                co_t = _parse(ca.get('check_out'))
                if ci_t is None:
                    continue
                ci = _dt(base_d.year, base_d.month, base_d.day,
                         ci_t.hour, ci_t.minute, 0, tzinfo=_VN)
                co = None
                if co_t is not None:
                    co = _dt(base_d.year, base_d.month, base_d.day,
                             co_t.hour, co_t.minute, 0, tzinfo=_VN)
                    if co <= ci:
                        # Ca qua đêm: giờ ra <= giờ vào → sang ngày hôm sau
                        co = co + _td(days=1)
                cur.execute("""
                    INSERT INTO cc_attendance_sessions
                        (user_id, date, check_in, check_out)
                    VALUES (%s, %s, %s, %s)
                """, (uid, date_str, ci, co))
                inserted += 1
            cur.execute("""
                UPDATE cc_attendance_requests
                SET status='approved', reviewed_by=%s, reviewed_at=now(),
                    review_note=%s, updated_at=now()
                WHERE id=%s
            """, (reviewer_id, note or '', req_id))
        conn.commit()

    # Tính lại tóm tắt ngày công sau khi đã thêm phiên
    _sync_attendance_summary(uid, date_str)
    return {"ok": True, "msg": f"Đã duyệt và bổ sung {inserted} ca.", "sessions_added": inserted}


# ─── Chấm công hộ (admin chỉnh sửa NV bất kỳ) ───────────────

def admin_set_attendance_sessions(user_id: str, work_date: str,
                                  sessions: list) -> dict:
    """Admin ghi/sửa toàn bộ ca chấm công của 1 NV trong 1 ngày.

    Hành vi: XOÁ HẾT phiên cũ của ngày đó → INSERT lại theo sessions mới
    → tính lại cc_attendance summary.
    sessions: [{'check_in': 'HH:MM', 'check_out': 'HH:MM' hoặc ''}, ...]
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    _VN = ZoneInfo('Asia/Ho_Chi_Minh')

    try:
        base_d = _dt.strptime(work_date, '%Y-%m-%d')
    except ValueError:
        return {'ok': False, 'msg': 'Ngày không hợp lệ.'}

    def _parse(hhmm: str):
        hhmm = (hhmm or '').strip()
        if not hhmm:
            return None
        return _dt.strptime(hhmm, '%H:%M')

    clean = []
    for ca in (sessions or []):
        try:
            ci_t = _parse(ca.get('check_in'))
        except ValueError:
            return {'ok': False, 'msg': f"Giờ vào không hợp lệ: {ca.get('check_in')}"}
        if ci_t is None:
            continue
        try:
            co_t = _parse(ca.get('check_out'))
        except ValueError:
            return {'ok': False, 'msg': f"Giờ ra không hợp lệ: {ca.get('check_out')}"}
        ci = _dt(base_d.year, base_d.month, base_d.day,
                 ci_t.hour, ci_t.minute, 0, tzinfo=_VN)
        co = None
        if co_t is not None:
            co = _dt(base_d.year, base_d.month, base_d.day,
                     co_t.hour, co_t.minute, 0, tzinfo=_VN)
            if co <= ci:
                co = co + _td(days=1)  # ca qua đêm
        clean.append((ci, co))

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Xoá hết ca cũ của ngày này
            cur.execute("DELETE FROM cc_attendance_sessions WHERE user_id=%s AND date=%s",
                        (str(user_id), work_date))
            # Insert lại
            for ci, co in clean:
                cur.execute("""
                    INSERT INTO cc_attendance_sessions (user_id, date, check_in, check_out)
                    VALUES (%s, %s, %s, %s)
                """, (str(user_id), work_date, ci, co))
            # Nếu không còn ca nào → xoá luôn dòng tổng hợp cc_attendance để khỏi treo
            if not clean:
                cur.execute("DELETE FROM cc_attendance WHERE user_id=%s AND date=%s",
                            (str(user_id), work_date))
        conn.commit()

    if clean:
        _sync_attendance_summary(str(user_id), work_date)
    return {'ok': True,
            'msg': f"Đã ghi {len(clean)} ca cho ngày {work_date}.",
            'count': len(clean)}
