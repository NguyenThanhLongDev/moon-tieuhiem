"""
Quản lý trạng thái sync per-shop trong bảng sync_state.
Cho phép scheduler biết shop nào cần full scan (lần đầu)
và shop nào chỉ cần today scan (đã có dữ liệu).
"""
from __future__ import annotations

import re
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SOURCE_OUTBOUND = "wh_outbound"
SOURCE_RETURNS  = "wh_returns"

FULL_LOOKBACK_DAYS         = 90   # Ngày quét full (lần đầu + nightly safety net)
TODAY_LOOKBACK_DAYS        = 14   # Ngày quét regular outbound (active orders còn sống)
RETURNS_TODAY_LOOKBACK_DAYS = 21  # Ngày quét regular returns (lookback dài hơn)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_all_states(conn, source_name: str) -> Dict[int, Any]:
    """Trả về dict shop_id → sync_state row cho source_name."""
    rows = conn.execute(
        "SELECT * FROM sync_state WHERE source_name = %s", (source_name,)
    ).fetchall()
    return {r["shop_id"]: r for r in rows}


def upsert_state(
    conn, source_name: str, shop_id: int, shop_key: str,
    mode: str, status: str, error: str = "",
) -> None:
    """Insert hoặc update trạng thái sync sau mỗi lần chạy.

    mode='full'  → cập nhật cả last_full_sync_at lẫn last_today_sync_at.
    mode='today' → chỉ cập nhật last_today_sync_at.
    """
    conn.execute("""
        INSERT INTO sync_state
            (source_name, shop_id, shop_key, status, last_error,
             run_count, last_today_sync_at, last_full_sync_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, 1, NOW(), NULL, NOW())
        ON CONFLICT (source_name, shop_id) DO UPDATE
        SET shop_key          = EXCLUDED.shop_key,
            status            = EXCLUDED.status,
            last_error        = EXCLUDED.last_error,
            run_count         = sync_state.run_count + 1,
            last_today_sync_at = NOW(),
            last_full_sync_at = CASE
                WHEN %s = 'full' THEN NOW()
                ELSE sync_state.last_full_sync_at
            END,
            updated_at        = NOW()
    """, (source_name, shop_id, shop_key, status, error[:500],
          # tham số cho CASE WHEN ở UPDATE
          mode))


def upsert_state_bulk(
    conn, source_name: str,
    shops: List[Any],            # list of shop rows with id, shop_key
    error_shop_names: set,       # set of shop_names that errored
    mode: str,
    all_errors: List[str],       # từ result["errors"] để lấy message per shop
) -> None:
    """Cập nhật trạng thái cho nhiều shops cùng lúc sau một batch sync."""
    for shop in shops:
        shop_name = shop["shop_name"] or shop["shop_key"] or str(shop["id"])
        status = "error" if shop_name in error_shop_names else "ok"
        # Lấy error message của shop này (nếu có)
        err_msg = "; ".join(e for e in all_errors if shop_name in e)[:500]
        upsert_state(conn, source_name, shop["id"], shop["shop_key"] or "",
                     mode, status, err_msg)


# ─────────────────────────────────────────────────────────────────────────────
# Decision helpers
# ─────────────────────────────────────────────────────────────────────────────

def needs_full_sync(state_row: Optional[Any]) -> bool:
    """True nếu shop chưa từng được full sync (lần đầu chạy)."""
    if state_row is None:
        return True
    return state_row["last_full_sync_at"] is None


def parse_error_shop_names(errors: List[str]) -> set:
    """Trích xuất set tên shop từ messages lỗi của sync function.

    Format lỗi: "{shop_name} (status={N}): {message}"
    """
    names: set = set()
    pattern = re.compile(r'^(.+?) \(status=\d+\):')
    for err in errors:
        m = pattern.match(err)
        if m:
            names.add(m.group(1).strip())
    return names
