"""shop_helpers.py — Module trung tâm load danh sách shops.

Ưu tiên PostgreSQL DB, fallback về shops.json.
Các engine scripts và modules đều nên import từ đây.

Fields trả về (chuẩn hóa):
    shop_key        str   — "shop1", "shop2" ...
    shop_name       str   — tên shop
    shop_id         str   — pancake_shop_id (alias để tương thích cũ)
    pancake_shop_id str   — pancake_shop_id (giống shop_id)
    status          str   — "active" / "inactive"
    pos_api_key     str   — API key Pancake (hex 32 ký tự hoặc "")
    team_id         str   — team ID (nếu có)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE_DIR = Path(__file__).resolve().parent
_SHOPS_JSON = _BASE_DIR / "shops.json"


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    """Chuyển DB row thành dict chuẩn (tương thích cả shop_id lẫn pancake_shop_id)."""
    shop_key, shop_name, pancake_shop_id, status, pos_api_key, team_id = row
    pid = pancake_shop_id or ""
    return {
        "shop_key": shop_key or "",
        "shop_name": shop_name or "",
        "shop_id": pid,           # alias cũ — nhiều script dùng shop_id
        "pancake_shop_id": pid,
        "status": status or "active",
        "pos_api_key": pos_api_key or "",
        "team_id": team_id or "",
    }


def _load_from_db() -> List[Dict[str, Any]]:
    """Đọc shops từ PostgreSQL (shops JOIN wh_shops)."""
    try:
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.shop_key,
                       s.shop_name,
                       s.pancake_shop_id,
                       s.status::text,
                       w.pos_api_key,
                       COALESCE(w.team_id, s.team_id::text)
                FROM shops s
                LEFT JOIN wh_shops w ON w.shop_key = s.shop_key
                ORDER BY s.shop_key
            """)
            rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows if r[0]]
    except Exception:
        return []


def _load_from_json() -> List[Dict[str, Any]]:
    """Fallback: đọc shops từ shops.json và chuẩn hóa fields."""
    if not _SHOPS_JSON.is_file():
        return []
    try:
        data = json.loads(_SHOPS_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        result = []
        for s in data:
            if not isinstance(s, dict):
                continue
            pid = str(s.get("pancake_shop_id") or s.get("shop_id") or "")
            result.append({
                "shop_key": str(s.get("shop_key") or ""),
                "shop_name": str(s.get("shop_name") or ""),
                "shop_id": pid,
                "pancake_shop_id": pid,
                "status": str(s.get("status") or "active"),
                "pos_api_key": str(s.get("pos_api_key") or ""),
                "team_id": str(s.get("team_id") or ""),
            })
        return result
    except Exception:
        return []


def load_all_shops() -> List[Dict[str, Any]]:
    """DB-first: load tất cả shops. Fallback về shops.json."""
    shops = _load_from_db()
    if shops:
        return shops
    return _load_from_json()


def load_active_shops() -> List[Dict[str, Any]]:
    """Chỉ trả về shops có status == 'active'."""
    return [s for s in load_all_shops() if s.get("status") == "active"]


def get_shop_by_key(shop_key: str) -> Optional[Dict[str, Any]]:
    """Tìm shop theo shop_key."""
    for s in load_all_shops():
        if s["shop_key"] == shop_key:
            return s
    return None


def get_shop_by_pancake_id(pancake_shop_id: str) -> Optional[Dict[str, Any]]:
    """Tìm shop theo pancake_shop_id."""
    pid = str(pancake_shop_id)
    for s in load_all_shops():
        if s["pancake_shop_id"] == pid or s["shop_id"] == pid:
            return s
    return None


def build_shop_map(key_field: str = "shop_id", value_field: str = "shop_name") -> Dict[str, str]:
    """
    Xây dựng dict {key_field: value_field} từ danh sách shops.
    Mặc định: {shop_id: shop_name} — nhiều script cũ cần map này.
    """
    result: Dict[str, str] = {}
    for s in load_all_shops():
        k = str(s.get(key_field) or "")
        v = str(s.get(value_field) or "")
        if k:
            result[k] = v
    return result


def build_shop_key_map() -> Dict[str, str]:
    """Trả về {shop_key: shop_name}."""
    return build_shop_map(key_field="shop_key", value_field="shop_name")
