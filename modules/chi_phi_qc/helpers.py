"""Helper thuần: vai trò, người dùng, team, shop-key.

Tách từ __init__.py — KHÔNG chạm DB (đọc users.json / user_helpers).
Tầng thấp nhất: không import bất kỳ module nào khác của chi_phi_qc.
"""
from __future__ import annotations

import decimal
import json

# ── Role helpers ─────────────────────────────────────────────────────
_SENIOR_ROLES    = {"admin", "superadmin", "manager", "ketoan", "accountant", "leader", "it"}
_FULL_VIEW_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}
# Role KHÔNG chạy quảng cáo → không hiện trong tab "Phân theo nhân viên"
# (kho/sale/sale_leader/it/packing: không phụ trách TK QC nên ẩn cho gọn).
_NON_ADS_ROLES   = {"kho", "sale", "sale_leader", "it", "packing"}

def _is_senior(role):
    return role in _SENIOR_ROLES

def _is_full_view(role):
    """Admin/kế toán see everything."""
    return role in _FULL_VIEW_ROLES

# ── User/team helpers from users.json ────────────────────────────────
import os as _os

def _load_users_json() -> list:
    """Load all users từ DB (ưu tiên) hoặc users.json (fallback)."""
    try:
        import sys
        _base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        sys.path.insert(0, _base)
        from user_helpers import load_all_users
        return load_all_users()
    except Exception:
        pass
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    path = _os.path.join(base, "users.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("users", [])
    except Exception:
        return []

def _normalize_shops(value) -> list:
    """Convert assigned_shops to a list of shop_keys, excluding '*'."""
    if isinstance(value, list):
        return [s.strip() for s in value if s and str(s).strip() not in ("", "*")]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip() and s.strip() != "*"]
    return []

def _get_user_by_id(user_id) -> dict:
    for u in _load_users_json():
        if str(u.get("id", "")) == str(user_id):
            return u
    return {}

def _get_team_members(leader_team_id: str) -> list:
    """Return all active staff+leader in the given team_id (from users.json).
    Bao gồm cả NV đã nghỉ — caller tự lọc theo ngày nếu cần (xem _is_member_visible)."""
    if not leader_team_id:
        return []
    return [
        u for u in _load_users_json()
        if u.get("status", "active") == "active"
        and str(u.get("team_id", "")).strip() == leader_team_id
    ]

def _get_allowed_shop_keys_for_user(user_id, role) -> list | None:
    """
    Returns the set of shop_keys this user can see, or None (= all shops).
    admin/ketoan/manager/superadmin → None (all)
    leader → all shops of their team members (union)
    staff  → only their assigned_shops
    """
    if _is_full_view(role):
        return None  # no filter

    user = _get_user_by_id(user_id)
    if not user:
        return []

    if role == "leader":
        team_id = str(user.get("team_id", "")).strip()
        members = _get_team_members(team_id) if team_id else [user]
        shops: set = set()
        for m in members:
            shops.update(_normalize_shops(m.get("assigned_shops", [])))
        # also include leader's own assigned_shops
        shops.update(_normalize_shops(user.get("assigned_shops", [])))
        return list(shops) if shops else []

    # staff / other
    shops = _normalize_shops(user.get("assigned_shops", []))
    return shops


# ── JSON serialize ───────────────────────────────────────────────────
def _serialize(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj
