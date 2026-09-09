"""Authentication Service — Xử lý login logic"""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

try:
    from db import get_conn as get_db_conn
except Exception:
    get_db_conn = None

from app_constants import DASHBOARD_USERNAME, DASHBOARD_PASSWORD, USERS_FILE

_log = logging.getLogger(__name__)


def hash_password(plain: str) -> str:
    """Hash mật khẩu dùng bcrypt."""
    import bcrypt as _bcrypt
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(12)).decode()


def check_user_password(entered: str, user: Dict) -> bool:
    """Kiểm tra mật khẩu người dùng (bcrypt hoặc plaintext)."""
    pw_hash = user.get("password_hash", "")
    if pw_hash and pw_hash.startswith("$2"):
        import bcrypt as _bcrypt
        try:
            return _bcrypt.checkpw(entered.encode(), pw_hash.encode())
        except Exception:
            return False
    return entered == str(user.get("password", ""))


def load_users_from_db() -> Optional[List[Dict]]:
    """Lấy danh sách users từ PostgreSQL DB."""
    if not get_db_conn:
        return None
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.username, u.password_hash, u.full_name,
                       u.role::text, u.status::text, t.team_code
                FROM users u
                LEFT JOIN teams t ON t.id = u.team_id
            """)
            rows = cur.fetchall()
            if not rows:
                return None
            cur.execute("""
                SELECT usa.user_id, s.shop_key, usa.assigned_from
                FROM user_shop_assignments usa
                JOIN shops s ON s.id = usa.shop_id
                WHERE usa.assigned_to IS NULL
            """)
            shops_by_user: Dict[int, List[str]] = {}
            assign_from_by_user: Dict[int, Any] = {}
            for user_id, shop_key, afrom in cur.fetchall():
                shops_by_user.setdefault(user_id, []).append(shop_key)
                if afrom and (user_id not in assign_from_by_user or afrom > assign_from_by_user[user_id]):
                    assign_from_by_user[user_id] = afrom
            users = []
            for uid, username, pw_hash, full_name, role, status, team_code in rows:
                assigned = shops_by_user.get(uid, [])
                if role in ("admin", "superadmin", "manager", "accountant", "it", "sale", "sale_leader") and not assigned:
                    assigned = ["*"]
                _af = assign_from_by_user.get(uid)
                users.append({
                    "id": uid,
                    "username": username,
                    "password_hash": pw_hash or "",
                    "password": "",
                    "full_name": full_name or "",
                    "role": role,
                    "status": status,
                    "team_id": team_code or "",
                    "assigned_shops": assigned,
                    "assigned_from": _af.isoformat() if _af else None,
                })
            return users
    except Exception:
        return None


def load_users() -> List[Dict[str, Any]]:
    """Lấy danh sách users từ DB hoặc file."""
    from app_ctx import try_parse_json, save_json_file

    db_users = load_users_from_db()
    if db_users is not None:
        return db_users
    if os.path.exists(USERS_FILE):
        try:
            data = try_parse_json(USERS_FILE)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    default_admin = [{
        "id": "u-admin",
        "username": DASHBOARD_USERNAME,
        "password": DASHBOARD_PASSWORD,
        "role": "admin",
        "team_id": "",
        "assigned_shops": ["*"],
        "status": "active",
    }]
    save_json_file(USERS_FILE, default_admin)
    return default_admin


def save_users(users: List[Dict[str, Any]]) -> None:
    """Lưu danh sách users vào file + DB."""
    from app_ctx import save_json_file

    save_json_file(USERS_FILE, users)
    if not get_db_conn:
        return
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT team_code, id FROM teams")
            team_map = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT shop_key, id FROM shops")
            shop_map = {r[0]: r[1] for r in cur.fetchall()}
            for u in users:
                username = str(u.get("username", "")).strip()
                if not username:
                    continue
                role = str(u.get("role", "staff"))
                if role not in {"admin", "leader", "staff", "accountant", "kho", "manager", "it", "superadmin", "sale", "sale_leader"}:
                    role = "staff"
                status = str(u.get("status", "active"))
                if status not in ("active", "inactive"):
                    status = "active"
                team_code = str(u.get("team_id", "")).strip()
                team_db_id = team_map.get(team_code) if team_code else None
                full_name = str(u.get("full_name") or u.get("username", ""))
                plain = str(u.get("password", "")).strip()
                pw_hash = u.get("password_hash", "")
                cur.execute("""
                    INSERT INTO users (username, password_hash, full_name, role, team_id, status)
                    VALUES (%s, %s, %s, %s::user_role, %s, %s::record_status)
                    ON CONFLICT (username) DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        role      = EXCLUDED.role,
                        team_id   = EXCLUDED.team_id,
                        status    = EXCLUDED.status
                    RETURNING id
                """, (username, pw_hash or hash_password("1221"), full_name, role, team_db_id, status))
                db_id = cur.fetchone()[0]
                if plain:
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE id = %s",
                        (hash_password(plain), db_id),
                    )
                assigned = u.get("assigned_shops", [])
                eff = (str(u.get("assign_from") or "").strip()) or None
                if "*" not in assigned:
                    want_ids = {shop_map[k] for k in assigned if shop_map.get(k)}
                    cur.execute(
                        "SELECT shop_id FROM user_shop_assignments"
                        " WHERE user_id = %s AND assigned_to IS NULL", (db_id,))
                    have_ids = {r[0] for r in cur.fetchall()}
                    removed = list(have_ids - want_ids)
                    if removed:
                        cur.execute(
                            "DELETE FROM user_shop_assignments WHERE user_id = %s"
                            " AND shop_id = ANY(%s) AND assigned_to IS NULL"
                            " AND assigned_from >= CURRENT_DATE", (db_id, removed))
                        cur.execute(
                            "UPDATE user_shop_assignments SET assigned_to = CURRENT_DATE - 1"
                            " WHERE user_id = %s AND shop_id = ANY(%s) AND assigned_to IS NULL",
                            (db_id, removed))
                    for shop_db_id in want_ids - have_ids:
                        cur.execute(
                            "INSERT INTO user_shop_assignments (user_id, shop_id, assigned_from)"
                            " VALUES (%s, %s, COALESCE(%s::date, CURRENT_DATE)) ON CONFLICT DO NOTHING",
                            (db_id, shop_db_id, eff))
                    try:
                        from repositories.admin_repo import sync_shop_mappings_for_user
                        sync_shop_mappings_for_user(cur, int(db_id), effective_date=eff)
                    except Exception as exc:
                        _log.warning("sync_shop_mappings_for_user error: %s", exc)
            conn.commit()
    except Exception as e:
        _log.error("save_users DB error: %s", e)

    try:
        from modules.cham_cong.cc_db import sync_cc_role_from_web_role
        with get_db_conn() as conn2:
            cur2 = conn2.cursor()
            for u in users:
                username = str(u.get("username", "")).strip()
                if not username:
                    continue
                role = str(u.get("role", "staff"))
                cur2.execute("SELECT id::text FROM users WHERE username=%s", (username,))
                row = cur2.fetchone()
                if row:
                    sync_cc_role_from_web_role(row[0], role)
    except Exception as e:
        _log.warning("save_users cc_role sync error: %s", e)


def find_user(username: str) -> Optional[Dict[str, Any]]:
    """Tìm user theo username."""
    uname = str(username or "").strip()
    for user in load_users():
        if str(user.get("username", "")).strip() == uname:
            return user
    return None
