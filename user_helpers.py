"""
user_helpers.py — Helper dùng chung để load users từ PostgreSQL.
Tất cả modules (cham_cong, chi_phi_qc, salary, kho_vat_ly...) import từ đây
thay vì đọc trực tiếp users.json.

Fallback: nếu DB không khả dụng → đọc users.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent
_USERS_JSON = _REPO_ROOT / "users.json"

# Roles có quyền xem tất cả shops (không cần filter)
FULL_VIEW_ROLES = frozenset({"admin", "accountant", "manager", "superadmin"})


def load_all_users() -> List[Dict]:
    """
    Load tất cả users — ưu tiên DB, fallback về users.json.
    Trả về list dict với các key: id, username, full_name, role, status, team_id, assigned_shops.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.username, u.full_name, u.role::text,
                       u.status::text, t.team_code,
                       COALESCE(u.resigned_at, e.resigned_at) AS resigned_at
                FROM users u
                LEFT JOIN teams t ON t.id = u.team_id
                LEFT JOIN cc_employees e ON e.user_id = u.id::text
                ORDER BY u.id
            """)
            rows = cur.fetchall()
            if not rows:
                raise ValueError("DB trả về danh sách rỗng")
            # Lấy shop assignments + ngày hiệu lực (assigned_from)
            cur.execute("""
                SELECT usa.user_id, s.shop_key, usa.assigned_from
                FROM user_shop_assignments usa
                JOIN shops s ON s.id = usa.shop_id
                WHERE usa.assigned_to IS NULL
            """)
            shops_by_user: Dict[int, List[str]] = {}
            assign_from_by_user: Dict[int, Any] = {}
            for uid, sk, afrom in cur.fetchall():
                shops_by_user.setdefault(uid, []).append(sk)
                # Giữ ngày hiệu lực MỚI NHẤT để pre-fill ô "Ngày áp dụng"
                if afrom and (uid not in assign_from_by_user or afrom > assign_from_by_user[uid]):
                    assign_from_by_user[uid] = afrom
            result = []
            for uid, username, full_name, role, status, team_code, resigned_at in rows:
                assigned = shops_by_user.get(uid, [])
                if role in FULL_VIEW_ROLES and not assigned:
                    assigned = ["*"]
                _af = assign_from_by_user.get(uid)
                result.append({
                    "id": uid,
                    "username": username,
                    "full_name": full_name or "",
                    "role": role,
                    "status": status,
                    "team_id": team_code or "",
                    "assigned_shops": assigned,
                    "assigned_from": _af.isoformat() if _af else None,
                    "resigned_at": resigned_at.isoformat() if resigned_at else None,
                })
            return result
    except Exception:
        pass
    # Fallback: đọc từ JSON
    try:
        data = json.loads(_USERS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_user_by_id(user_id) -> Optional[Dict]:
    """Tìm user theo id (int hoặc string)."""
    uid_str = str(user_id)
    for u in load_all_users():
        if str(u.get("id", "")) == uid_str:
            return u
    return None


def get_user_by_username(username: str) -> Optional[Dict]:
    """Tìm user theo username."""
    uname = str(username or "").strip()
    for u in load_all_users():
        if u.get("username") == uname:
            return u
    return None


def get_active_users(roles: Optional[frozenset] = None) -> List[Dict]:
    """Lấy danh sách users active, lọc theo roles nếu cần."""
    result = [u for u in load_all_users() if u.get("status") == "active"]
    if roles:
        result = [u for u in result if u.get("role") in roles]
    return result


def get_team_members(team_id: str) -> List[Dict]:
    """Lấy tất cả users active trong team_id (team_code)."""
    if not team_id:
        return []
    return [
        u for u in load_all_users()
        if u.get("status") == "active"
        and str(u.get("team_id", "")).strip() == str(team_id).strip()
    ]
