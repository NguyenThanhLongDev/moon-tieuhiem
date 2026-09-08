"""Loại shop (business_type) động — CRUD + đọc danh sách.

Bảng `shop_business_types` (migration 066). Khách tự thêm/sửa/ẩn loại shop;
mã lưu ở `shops.business_type`. Dùng cho dropdown LOẠI (Cài đặt), nút lọc +
tag màu ở trang Doanh thu.

Mọi hàm fail-safe: lỗi DB → trả default cty/hkd/pos3 để UI không vỡ.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from db import get_conn

# Fallback khi bảng chưa tồn tại / lỗi DB — giữ hành vi cũ.
_DEFAULT_TYPES: List[Dict[str, Any]] = [
    {"code": "cty",  "label": "Công ty",       "icon": "🏢", "color": "#1d4ed8", "bg_color": "#dbeafe", "sort_order": 10, "active": True, "is_builtin": True},
    {"code": "hkd",  "label": "Hộ kinh doanh", "icon": "🏪", "color": "#b45309", "bg_color": "#fef3c7", "sort_order": 20, "active": True, "is_builtin": True},
    {"code": "pos3", "label": "POS 3",         "icon": "📦", "color": "#15803d", "bg_color": "#dcfce7", "sort_order": 30, "active": True, "is_builtin": True},
]

_COLS = "code, label, icon, color, bg_color, sort_order, active, is_builtin"


def _row_to_dict(row) -> Dict[str, Any]:
    return {
        "code": row[0], "label": row[1], "icon": row[2], "color": row[3],
        "bg_color": row[4], "sort_order": int(row[5] or 0),
        "active": bool(row[6]), "is_builtin": bool(row[7]),
    }


def get_shop_business_types(active_only: bool = False) -> List[Dict[str, Any]]:
    """Danh sách loại shop, sắp theo sort_order rồi code."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                q = f"SELECT {_COLS} FROM shop_business_types"
                if active_only:
                    q += " WHERE active = TRUE"
                q += " ORDER BY sort_order, code"
                cur.execute(q)
                rows = [_row_to_dict(r) for r in cur.fetchall()]
        if rows:
            return [r for r in rows if r["active"]] if active_only else rows
    except Exception:
        pass
    return [r for r in _DEFAULT_TYPES if r["active"]] if active_only else list(_DEFAULT_TYPES)


def get_type_map() -> Dict[str, Dict[str, Any]]:
    """{code: type_dict} — gồm cả loại đã ẩn (để render tag shop cũ vẫn ra nhãn)."""
    return {t["code"]: t for t in get_shop_business_types(active_only=False)}


def get_valid_type_codes(active_only: bool = True) -> Set[str]:
    return {t["code"] for t in get_shop_business_types(active_only=active_only)}


def upsert_shop_type(
    code: str, label: str, icon: str = "", color: str = "#1d4ed8",
    bg_color: str = "#dbeafe", sort_order: int = 100, active: bool = True,
    is_builtin: bool = False,
) -> None:
    """Thêm mới hoặc cập nhật. is_builtin chỉ set khi tạo mới builtin (không hạ cấp builtin có sẵn)."""
    code = (code or "").strip().lower()
    if not code:
        raise ValueError("Thiếu mã loại shop")
    label = (label or "").strip() or code.upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shop_business_types
                    (code, label, icon, color, bg_color, sort_order, active, is_builtin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    label = EXCLUDED.label, icon = EXCLUDED.icon,
                    color = EXCLUDED.color, bg_color = EXCLUDED.bg_color,
                    sort_order = EXCLUDED.sort_order, active = EXCLUDED.active
                """,
                (code, label, icon.strip(), color.strip(), bg_color.strip(),
                 int(sort_order or 100), bool(active), bool(is_builtin)),
            )
        conn.commit()


def set_active(code: str, active: bool) -> None:
    code = (code or "").strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE shop_business_types SET active=%s WHERE code=%s", (bool(active), code))
        conn.commit()


def delete_shop_type(code: str) -> str:
    """Xoá loại tuỳ biến. Builtin (cty/hkd/pos3) không xoá → trả lý do.
    Loại đang được shop dùng cũng không xoá để khỏi mồ côi dữ liệu."""
    code = (code or "").strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_builtin FROM shop_business_types WHERE code=%s", (code,))
            row = cur.fetchone()
            if not row:
                return "Loại không tồn tại."
            if row[0]:
                return "Loại mặc định không xoá được — có thể Ẩn thay vì xoá."
            cur.execute("SELECT COUNT(*) FROM shops WHERE lower(business_type)=%s", (code,))
            in_use = int(cur.fetchone()[0] or 0)
            if in_use > 0:
                return f"Đang có {in_use} shop dùng loại này — đổi loại cho shop trước, hoặc Ẩn."
            cur.execute("DELETE FROM shop_business_types WHERE code=%s", (code,))
        conn.commit()
    return ""
