"""
Sync đơn ĐVVC từ Pancake POS → wh_outbound_requests (PostgreSQL).
Adapted từ posbot/sync_orders.py — chỉ thay DB layer sang PostgreSQL pool.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
)
try:
    from tz_utils import now_hcm, fmt_hcm, today_hcm
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from tz_utils import now_hcm, fmt_hcm, today_hcm

try:
    from pancake_auth import get_api_key as _get_pancake_key, is_valid_hex_api_key as _is_valid_key, get_shop_api_key as _get_shop_api_key
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from pancake_auth import get_api_key as _get_pancake_key, is_valid_hex_api_key as _is_valid_key, get_shop_api_key as _get_shop_api_key

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .wh_db import wh_db as db, SYNC_LOCK, OUTBOUND_MANUAL_SYNC_LOCK, FAST_SHIPPED_SYNC_LOCK, FAST_NEW_ORDERS_SYNC_LOCK

log = logging.getLogger(__name__)

# Session Pancake — đọc từ env var hoặc file posbot/data/session.json
_BASE = os.path.dirname(__file__)
SESSION_PATH = os.getenv(
    "PANCAKE_SESSION_PATH",
    os.path.abspath(os.path.join(_BASE, "..", "..", "session.json")),
)

PARTNER_ID_MAP: Dict[int, str] = {
    1: "GHN", 2: "GHTK", 3: "ViettelPost", 4: "J&T Express",
    5: "Ninja Van", 6: "BEST Express", 7: "Ahamove",
    9: "Vietnam Post", 10: "Kerry Express", 42: "Shopee Express",
}
TRACKING_PREFIX_MAP = [
    ("SPXVN", "Shopee Express"), ("SPXID", "Shopee Express"),
    ("GHN", "GHN"), ("GHTK", "GHTK"), ("VTP", "ViettelPost"),
    ("VT", "ViettelPost"), ("BEST", "BEST Express"), ("JT", "J&T Express"),
    ("NV", "Ninja Van"), ("AHM", "Ahamove"), ("MB-", "GHN"),
]


def detect_carrier_name(order: Dict[str, Any]) -> str:
    partner = order.get("partner") or {}
    delivery_name = (partner.get("delivery_name") or "").strip()
    if delivery_name:
        return delivery_name
    pid = partner.get("partner_id")
    if pid and int(pid) in PARTNER_ID_MAP:
        return PARTNER_ID_MAP[int(pid)]
    tracking = (partner.get("extend_code") or "").strip().upper()
    if tracking:
        for prefix, name in TRACKING_PREFIX_MAP:
            if tracking.startswith(prefix.upper()):
                return name
    ghn_code = (partner.get("order_id_ghn") or "").strip()
    if ghn_code:
        return "GHN"
    sp = partner.get("service_partner") or {}
    sp_pid = sp.get("partner_id")
    if sp_pid and int(sp_pid) in PARTNER_ID_MAP:
        return PARTNER_ID_MAP[int(sp_pid)]
    return "Không rõ ĐVVC"


def get_tracking_code(order: Dict[str, Any]) -> str:
    partner = order.get("partner") or {}
    return (partner.get("extend_code") or partner.get("order_id_ghn")
            or partner.get("order_number_vtp") or "") or ""


def _get_session() -> Tuple[str, str]:
    """Deprecated: dùng pancake_auth.get_api_key() thay thế. Giữ lại để backward-compat."""
    return _get_pancake_key(), ""


_VN_TZ = timezone(timedelta(hours=7))

def _day_ts(date_str: str, end: bool = False) -> int:
    """Trả về timestamp milliseconds theo giờ Việt Nam."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    dt_vn = dt.replace(tzinfo=_VN_TZ)
    return int(dt_vn.timestamp() * 1000)


def _safe_parse_dt(value: Any) -> Optional[datetime]:
    """Parse datetime từ Pancake API → datetime VN (UTC+7), naive.

    BUG FIX 2026-04-30: Pancake trả ISO 8601 dạng NAIVE (không có Z/+00:00)
    nhưng giờ là UTC. Code cũ chỉ +7h khi tzinfo != None → naive datetimes
    bị giữ nguyên giờ UTC → carrier_picked_up_at hiển thị 04:52 thay vì 11:52.
    Fix: treat naive ISO 8601 as UTC, always +7h.
    Datetime numeric timestamps cũng treat as UTC (Unix epoch là UTC).
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:
            ts //= 1000
        # Unix timestamp → UTC datetime → +7h cho VN
        return datetime.utcfromtimestamp(ts) + timedelta(hours=7)
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            # Có TZ marker (Z hoặc +00:00) → strip + cộng 7h
            dt = dt.replace(tzinfo=None) + timedelta(hours=7)
        else:
            # NAIVE ISO 8601 — Pancake trả format này, thực tế là UTC → cộng 7h
            dt = dt + timedelta(hours=7)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            # strptime không có TZ info → treat as UTC, +7h
            return datetime.strptime(text, fmt) + timedelta(hours=7)
        except Exception:
            continue
    return None


def _extract_pickup_time(order: Dict[str, Any]) -> Optional[str]:
    """Trả về ngày ĐVVC thực tế quét lấy hàng = status_history[status=2].updated_at.
    Đây là ngày Pancake UI dùng cho filter 'ĐVVC lấy hàng' — khớp 100%.
    Fallback: status=9 (ngày shop đẩy lên ĐVVC) nếu status=2 chưa có.
    """
    # Ưu tiên: ngày ĐVVC thực sự đến lấy (status 2 = Đang giao)
    for h in order.get("status_history") or []:
        if str(h.get("status", "")) == "2":
            dt = _safe_parse_dt(h.get("updated_at"))
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")
    # Fallback: ngày shop đẩy lên ĐVVC (status 9 = Chờ chuyển hàng)
    for h in order.get("status_history") or []:
        if str(h.get("status", "")) == "9":
            dt = _safe_parse_dt(h.get("updated_at"))
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")
    return None


def _extract_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        qty = int(float(item.get("quantity") or 0))
        if qty <= 0:
            continue
        variation_info = item.get("variation_info") or {}
        # custom_id = mã SKU do người dùng đặt trên Pancake (VD: QUANDN0001)
        # product_id = UUID nội bộ Pancake — KHÔNG phải mã hàng, KHÔNG dùng làm SKU
        sku = variation_info.get("custom_id") or ""
        product_name = (variation_info.get("name") or item.get("note_product")
                        or item.get("note_product_internal") or "")
        if not product_name:
            product_name = "Không rõ tên"
        variation_id = item.get("variation_id") or ""
        # Safety: nếu custom_id trả về UUID (code cũ hoặc Pancake lỗi), chuyển về variation_id
        if sku and _UUID_RE.match(sku) and not variation_id:
            variation_id = sku
            sku = ""
        result.append({
            "sku": str(sku).strip(),
            "product_name": str(product_name).strip(),
            "variation_id": variation_id,
            "qty": qty,
        })
    return result


def fetch_orders_for_shop(
    pos_shop_id: str, api_key: str = "",
    date_from: str = "", date_to: str = "",
    *, pancake_status: int = 2, update_status: Optional[str] = None,
    page_size: int = 100, max_pages: int = 500,
    cursor_inserted_at: str = "",
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch đơn theo status từ Pancake API (sort `inserted_at_desc`).

    Pancake API không hỗ trợ filter date — phải tail-poll + early-stop client-side.

    Param `cursor_inserted_at` (ISO timestamp string, vd "2026-04-29T08:30:00"):
      - Nếu set → loop paginate đến khi gặp order với inserted_at <= cursor → STOP.
      - Trim các order ≤ cursor khỏi all_orders trước khi return.
      - Implements "No filter / is_data_feed" pattern (Airbyte) cho API kiểu Pancake.
      - cursor=""  → fetch full như cũ (backward compat, lần đầu / disable cursor).

    Param `date_from` (YYYY-MM-DD): early-termination cũ theo ngày — dùng song song
    với cursor (cursor finer-grained hơn). Khi cả 2 set, cursor sẽ stop sớm hơn.

    carrier_picked_up_at extract từ status_history khi lưu DB.
    """
    api_key = api_key or _get_pancake_key()
    url = f"https://pos.pancake.vn/api/v1/shops/{pos_shop_id}/orders/get_orders"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{pos_shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }
    all_orders: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params: list = [
            ("api_key", api_key), ("page_size", page_size),
            ("status", pancake_status), ("page", page),
            ("option_sort", "inserted_at_desc"),
        ]
        last_err: Optional[str] = None
        for attempt in range(3):
            try:
                r = requests.post(url, params=params, headers=headers, json={}, timeout=30)
                r.raise_for_status()
                data = r.json()
                last_err = None
                break
            except Exception as e:
                last_err = f"Lỗi API shop {pos_shop_id}: {e}"
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if last_err:
            return all_orders, last_err
        if data.get("error") or not data.get("success", True):
            msg = data.get("message") or data.get("error") or "API trả về lỗi"
            return all_orders, msg
        page_orders = data.get("data") or []
        all_orders.extend(page_orders)
        total_pages_api = int(data.get("total_pages") or 1)
        total_entries = int(data.get("total_entries") or 0)
        if page >= total_pages_api or len(page_orders) < page_size:
            break
        if total_entries > 0 and len(all_orders) >= total_entries:
            break
        # ── Early-termination theo CURSOR (tail polling pattern) ──────────
        # Nếu page cuối có order với inserted_at <= cursor, các page sau cũng ≤
        # (sort inserted_at_desc) → STOP. Trim order ≤ cursor khỏi kết quả.
        # ISO 8601 string compare = chronological compare (đảm bảo bởi Pancake).
        if cursor_inserted_at and page_orders:
            _last = page_orders[-1]
            _last_ts = str(_last.get("inserted_at") or _last.get("created_at") or "")
            if _last_ts and _last_ts <= cursor_inserted_at:
                # Trim orders ≤ cursor (đã sync trong lần trước)
                all_orders = [
                    _o for _o in all_orders
                    if str(_o.get("inserted_at") or _o.get("created_at") or "") > cursor_inserted_at
                ]
                break
        # ── Early-termination theo date_from ──────────────────────────────
        # API đã sort inserted_at_desc → khi page cuối cùng đã older hơn
        # date_from thì các page sau chắc chắn còn cũ hơn → dừng luôn.
        # Giảm peak RAM cực lớn: thay vì fetch full 50k đơn lịch sử,
        # chỉ fetch các đơn trong cửa sổ ngày cần sync.
        if date_from and page_orders:
            _last = page_orders[-1]
            _last_raw = str(_last.get("inserted_at") or _last.get("created_at") or "")
            # Chuỗi ISO "YYYY-MM-DDTHH:MM:SS..." → so sánh prefix 10 ký tự
            if len(_last_raw) >= 10 and _last_raw[:10] < date_from:
                # Trim các đơn cũ hơn date_from khỏi all_orders để không phình DB phase
                _cutoff = date_from
                all_orders = [
                    _o for _o in all_orders
                    if str(_o.get("inserted_at") or _o.get("created_at") or "")[:10] >= _cutoff
                ]
                break
        time.sleep(0.3)
    return all_orders, None


def _auto_confirm_pre_confirmed(
    conn, row: Any, carrier_name: str, tracking_code: str,
    picked_up_at: str, order_ext_id: str,
    pancake_status_label: str = "shipped",
) -> bool:
    """Tự động trừ tồn + xác nhận đơn pre_confirmed khi ĐVVC lấy hàng.
    Thiếu tồn máy → bù về đủ rồi trừ (NV đã quét chuẩn bị = hàng thật có trong
    tay, tồn máy sai — chốt với user 2026-06-11, giống nút "Tự động xuất kho").
    Trả về True nếu row được xác nhận (kể cả khi luồng khác đã xác nhận trước).
    """
    import logging as _log
    _l = _log.getLogger(__name__)
    # Import lazily để tránh circular import (wh_sync_orders ← __init__.py)
    from . import _get_variant_inv_outbound, _upsert_inventory, _pick_warehouse_for_outbound

    product_id = row["product_id"]
    qty_actual  = row["qty_ordered"] or 0
    order_code  = row["order_code"] or order_ext_id
    wh_id       = row["warehouse_id"]
    var_id      = (row.get("pos_variation_id") or "").strip() or None

    if not wh_id and row.get("shop_id"):
        sh = conn.execute(
            "SELECT warehouse_id FROM wh_shops WHERE id=%s", (row["shop_id"],)
        ).fetchone()
        wh_id = sh["warehouse_id"] if sh else None
    if not wh_id and row.get("shop_name"):
        sh = conn.execute(
            "SELECT warehouse_id FROM wh_shops WHERE shop_name=%s LIMIT 1", (row["shop_name"],)
        ).fetchone()
        wh_id = sh["warehouse_id"] if sh else None
    if not wh_id and product_id:
        # Chưa có kho → chọn kho có đủ tồn (variant-aware)
        wh_id, _qa = _pick_warehouse_for_outbound(conn, product_id, var_id, qty_actual)

    # Đọc tồn variant-aware: (pid, wh_id, pos_variation_id)
    # Dùng _get_variant_inv_outbound để có cross-product fallback (SP bị tách sau nhập kho)
    inv_row, _is_variant = _get_variant_inv_outbound(conn, product_id, wh_id, var_id)
    qty_before = inv_row["qty"] if inv_row else 0
    ts = now_hcm().strftime("%Y-%m-%d %H:%M")

    # Claim row TRƯỚC mọi thao tác tồn kho (WHERE status='pre_confirmed') —
    # chống trừ tồn 2 lần khi webhook + sweep + polling sync cùng xác nhận 1 row.
    claimed = conn.execute("""
        UPDATE wh_outbound_requests
        SET status='confirmed', pancake_status=%s, carrier_name=%s,
            tracking_code=%s, carrier_picked_up_at=%s,
            qty_confirmed=%s, confirmed_by='system_auto', confirmed_at=%s
        WHERE id=%s AND status='pre_confirmed'
    """, (pancake_status_label, carrier_name, tracking_code, picked_up_at,
          qty_actual, ts, row["id"])).rowcount
    if not claimed:
        return True  # luồng khác đã xác nhận xong row này

    if not product_id:
        # Không map được sản phẩm — xác nhận không trừ tồn (giống admin bulk confirm)
        _l.info("[AUTO-CONFIRM] OK đơn %s (không có product map, bỏ trừ tồn)", order_code)
        return True

    if qty_actual > qty_before:
        # Tồn máy thiếu nhưng hàng thật đã xuất (NV quét rồi) → bù về đủ rồi trừ
        deficit = qty_actual - qty_before
        _upsert_inventory(conn, product_id, wh_id, qty_actual, pos_variation_id=var_id,
                          inventory_row_id=inv_row["id"] if inv_row else None)
        conn.execute("""
            INSERT INTO wh_stock_movements
              (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
            VALUES ('admin_auto_adjust', %s, %s, %s, %s, %s, %s, %s, %s)
        """, (product_id, wh_id, deficit, qty_before, qty_actual, order_code,
              f"Tự động bù tồn khi ĐVVC lấy — đơn {order_code}", ts))
        _l.warning("[AUTO-CONFIRM] Bù tồn đơn %s sp_id=%s var=%s wh=%s: %s→%s",
                   order_code, product_id, var_id, wh_id, qty_before, qty_actual)
        qty_before = qty_actual
        inv_row, _is_variant = _get_variant_inv_outbound(conn, product_id, wh_id, var_id)

    qty_after = qty_before - qty_actual
    # Ghi tồn: inventory_row_id để deduct đúng row khi SP đã tách (cross-product)
    _upsert_inventory(conn, product_id, wh_id, qty_after, pos_variation_id=var_id,
                      inventory_row_id=inv_row["id"] if inv_row else None)
    conn.execute("""
        INSERT INTO wh_stock_movements
          (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
        VALUES ('outbound_confirmed', %s, %s, %s, %s, %s, %s, %s, %s)
    """, (product_id, wh_id, qty_actual, qty_before, qty_after, order_code,
          f"Auto-confirm khi ĐVVC lấy — đơn {order_code}" + (f" [var={var_id}]" if var_id else ""), ts))
    _l.info("[AUTO-CONFIRM] OK đơn %s, trừ %s sp_id=%s tồn %s→%s",
            order_code, qty_actual, product_id, qty_before, qty_after)
    return True


def auto_confirm_stuck_preconfirmed(limit: int = 500) -> int:
    """Vớt đơn kẹt 'Chờ xuất kho' (pre_confirmed + ĐVVC đã lấy/đã nhận):
    tự trừ tồn + xác nhận như flow auto-confirm chuẩn. Lưới an toàn cho 2 kẽ hở
    phát hiện 2026-06-11 (263 đơn kẹt/ngày): webhook update pancake_status mà
    không auto-confirm → polling mất transition; và auto-confirm thiếu tồn
    trước đây chỉ thử 1 lần không retry."""
    import logging as _log
    _l = _log.getLogger(__name__)
    done = 0
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM wh_outbound_requests
            WHERE status='pre_confirmed' AND pancake_status IN ('shipped','received')
            ORDER BY id LIMIT %s
        """, (limit,)).fetchall()
        for r in rows:
            try:
                ok = _auto_confirm_pre_confirmed(
                    conn, r, r["carrier_name"] or "", r["tracking_code"] or "",
                    r["carrier_picked_up_at"] or "",
                    str(r["order_id_external"] or r["order_code"] or ""),
                    pancake_status_label=r["pancake_status"] or "shipped",
                )
                if ok:
                    done += 1
            except Exception:
                # Transaction có thể đã hỏng — dừng vòng này, sweep sau retry
                _l.exception("[SWEEP] auto-confirm fail row id=%s — dừng vòng sweep", r["id"])
                break
    if done:
        _l.info("[SWEEP] Vớt %d đơn kẹt 'Chờ xuất kho' → Đã xuất kho", done)
    return done


def sync_outbound_for_date_range(
    date_from: str, date_to: str, *, only_pos_shop_id: Optional[str] = None,
    only_pos_shop_ids: Optional[set] = None,
    active_only: bool = False, wait_for_lock: bool = False,
    shipped_only: bool = False, waiting_only: bool = False,
    progress_cb=None,
) -> Dict[str, Any]:
    """Sync đơn xuất kho từ Pancake POS.
    active_only=True: chỉ fetch status đang hoạt động (1,7,9,2) — dùng cho auto-sync 15 phút.
    active_only=False: fetch toàn bộ (thêm 3,5,6) — dùng cho sync thủ công / full sync.
    shipped_only=True: chỉ fetch status=2 (shipped) — dùng cho fast sync 2 phút.
    wait_for_lock=True: dùng OUTBOUND_MANUAL_SYNC_LOCK riêng (manual sync, không block auto-sync).
    wait_for_lock=False: dùng SYNC_LOCK, bỏ qua nếu đang bận (dùng cho auto-sync).
    """
    if shipped_only and waiting_only:
        # Fast-new-orders (status 9 only) — lock riêng, không bị block bởi fast-shipped
        got = FAST_NEW_ORDERS_SYNC_LOCK.acquire(blocking=False)
        lock_used = FAST_NEW_ORDERS_SYNC_LOCK
    elif shipped_only:
        # Fast sync dùng lock riêng — không bao giờ bị block bởi full auto-sync
        got = FAST_SHIPPED_SYNC_LOCK.acquire(blocking=False)
        lock_used = FAST_SHIPPED_SYNC_LOCK
    elif wait_for_lock:
        got = OUTBOUND_MANUAL_SYNC_LOCK.acquire(blocking=False)
        lock_used = OUTBOUND_MANUAL_SYNC_LOCK
    else:
        got = SYNC_LOCK.acquire(blocking=False)
        lock_used = SYNC_LOCK
    if not got:
        return {"inserted": 0, "updated": 0, "skipped": 0,
                "errors": ["Sync xuất hàng đang chạy, vui lòng đợi hoàn tất rồi thử lại"]}
    try:
        result = _sync_outbound_locked(date_from, date_to,
                                       only_pos_shop_id=only_pos_shop_id,
                                       only_pos_shop_ids=only_pos_shop_ids,
                                       active_only=active_only,
                                       shipped_only=shipped_only,
                                       waiting_only=waiting_only,
                                       progress_cb=progress_cb)
        if not (shipped_only and waiting_only):
            # Lưới an toàn: vớt đơn kẹt "Chờ xuất kho" mỗi vòng sync
            # (bỏ qua mode fast-new-orders để giữ luồng status 9 siêu nhẹ)
            try:
                result["stuck_confirmed"] = auto_confirm_stuck_preconfirmed()
            except Exception:
                import logging as _log
                _log.getLogger(__name__).exception("[SWEEP] lỗi vòng vớt đơn kẹt")
        return result
    finally:
        lock_used.release()


def _sync_outbound_locked(
    date_from: str, date_to: str, *, only_pos_shop_id: Optional[str] = None,
    only_pos_shop_ids: Optional[set] = None,
    active_only: bool = False, shipped_only: bool = False, waiting_only: bool = False,
    progress_cb=None,
) -> Dict[str, Any]:
    with db() as conn:
        shops = conn.execute(
            "SELECT id, shop_key, shop_name, pos_shop_id, pos_api_key, warehouse_id FROM wh_shops WHERE status='active'"
        ).fetchall()
        products = conn.execute(
            "SELECT id, sku, name FROM products"
        ).fetchall()
        var_map_rows = conn.execute(
            "SELECT pos_variation_id, product_id FROM wh_variation_map"
        ).fetchall()

    product_by_id: Dict[int, Dict] = {p["id"]: dict(p) for p in products}
    # var_id_to_product: ánh xạ pvid → product CHỈ qua wh_variation_map (single source of truth).
    # Bỏ fallback `wh_products.pos_variation_id` (legacy, không nhất quán khi multi-variant).
    var_id_to_product: Dict[str, Dict] = {}
    for vm in var_map_rows:
        prod = product_by_id.get(vm["product_id"])
        if prod:
            var_id_to_product[vm["pos_variation_id"]] = prod
    sku_to_product: Dict[str, Dict] = {}
    name_to_product: Dict[str, Dict] = {}
    _name_seen: set = set()
    for p in products:
        sku_key = (p["sku"] or "").upper()
        if sku_key:
            sku_to_product[sku_key] = dict(p)
        # Fallback theo tên (chỉ khi tên là duy nhất, tránh nhầm giữa 2 sp cùng tên)
        name_key = (p["name"] or "").strip().lower()
        if name_key in _name_seen:
            name_to_product.pop(name_key, None)
        elif name_key:
            name_to_product[name_key] = dict(p)
            _name_seen.add(name_key)

    inserted = updated = skipped = 0
    errors: List[str] = []

    # Pancake API không hỗ trợ date filter → fetch TOÀN BỘ từng status, lọc ở DB.
    # active_only=True: fetch nhóm trạng thái vận hành chính (0,1,4,7,9,2,3)
    # để badge kho vật lý (đặc biệt "Chờ xuất kho" = status 2+3) luôn đủ dữ liệu.
    # active_only=False: fetch toàn bộ (thêm 5,6) — chậm (~16k đơn).
    _active_configs = [
        (9, None, "waiting"),    # Chờ chuyển hàng — ĐVVC chưa lấy (thực sự chờ)
        (1, None, "confirmed"),  # Đã xác nhận (shop xác nhận, chưa đóng gói)
        (4, None, "confirmed"),  # Đang đóng hàng — đang chuẩn bị giao ĐVVC
        (7, None, "confirmed"),  # Đã xác nhận thường (shop xác nhận, sắp bàn giao ĐVVC)
        (2, None, "shipped"),    # Đã giao ĐVVC (đang giao)
        (3, None, "received"),   # Đã nhận (khách nhận hàng) — cần cho KPI status 2+3
    ]
    _full_extra_configs = [
        (3, None, "received"),   # Đã nhận (khách nhận hàng) — đã xong, không thay đổi
        (5, None, "returned"),   # Đã hoàn (hàng về kho lại) — loại khỏi tổng xuất
        (6, None, "cancelled"),  # Hủy — loại khỏi tổng xuất kho
    ]
    if shipped_only and waiting_only:
        fetch_configs = [(9, None, "waiting")]  # chỉ status 9 — cực nhẹ, dùng cho fast-new-orders
    elif shipped_only:
        fetch_configs = [
            (9, None, "waiting"),   # fast sync: Chờ chuyển hàng (ĐVVC chưa lấy)
            (2, None, "shipped"),   # fast sync: Đã giao ĐVVC (đang giao)
        ]
    elif active_only:
        fetch_configs = _active_configs
    else:
        fetch_configs = _active_configs + _full_extra_configs

    # Lọc sẵn shops có api_key hợp lệ để tính shop_total cho progress
    valid_shops = []
    for _s in shops:
        _pid = str(_s["pos_shop_id"])
        if only_pos_shop_id and _pid != only_pos_shop_id:
            continue
        if only_pos_shop_ids and _pid not in only_pos_shop_ids:
            continue
        _key = str(_s["pos_api_key"] or "").strip()
        if not _is_valid_key(_key):
            _key = _get_shop_api_key(_pid)
        if _is_valid_key(_key):
            valid_shops.append((_s, _key))
    shop_total = len(valid_shops)

    # ── Bước 1: Fetch song song (I/O-bound — an toàn dùng threads) ──────────────
    _fetch_tasks = []
    for _s, _k in valid_shops:
        for p_status_int, _upd_status, p_status_label in fetch_configs:
            _fetch_tasks.append((_s, _k, p_status_int, p_status_label))

    # ── Cursor incremental ("No filter / is_data_feed" pattern, Airbyte) ──
    # Set WH_SYNC_USE_CURSOR=0 để rollback sang fetch full như cũ.
    _use_cursor = os.environ.get("WH_SYNC_USE_CURSOR", "1") == "1"
    # ❗ shipped_only mode = bắt STATUS CHANGE trên đơn cũ (đơn ship hôm nay nhưng
    # insert vài ngày trước). Cursor theo inserted_at_desc sẽ early-terminate sớm
    # và MISS các status change này. → BỎ cursor cho shipped_only, chỉ dùng date_from.
    # (Bug 2026-05-19: 148 đơn pre_confirmed bị kẹt waiting do cursor lookback 24h.)
    if shipped_only:
        _use_cursor = False
    _cursor_lookback_h = int(os.environ.get("WH_SYNC_CURSOR_LOOKBACK_HOURS", "24"))
    try:
        from redis_cache import cache_get as _cache_get, cache_set as _cache_set
    except Exception:
        _cache_get = _cache_set = None
        _use_cursor = False

    def _cursor_key(shop_id, p_status_int):
        return f"sync_cursor_ob:{shop_id}:{p_status_int}"

    def _do_fetch(task):
        _shop, _api_key, _psi, _psl = task
        # Đọc cursor (nếu có) — trừ lookback để bắt updates muộn (Airbyte recommend)
        _cursor = ""
        if _use_cursor and _cache_get:
            try:
                _raw = _cache_get(_cursor_key(_shop["id"], _psi))
                if _raw:
                    # Trừ lookback_h giờ từ cursor (string ISO → datetime → -lookback → string)
                    from datetime import datetime as _dt2, timedelta as _td2
                    _cursor_dt = _dt2.fromisoformat(str(_raw).replace("Z", "+00:00").split("+")[0])
                    _cursor = (_cursor_dt - _td2(hours=_cursor_lookback_h)).isoformat(sep="T", timespec="seconds")
            except Exception as _e:
                log.warning("[cursor] parse fail shop=%s status=%s: %s — fallback full fetch",
                            _shop.get("shop_name"), _psi, _e)
                _cursor = ""

        _orders, _err = fetch_orders_for_shop(
            str(_shop["pos_shop_id"]), api_key=_api_key,
            date_from=date_from, date_to=date_to,
            pancake_status=_psi,
            cursor_inserted_at=_cursor,
        )

        # KHÔNG save cursor ở đây — phải đợi DB insert xong (Bước 2) mới save
        # để tránh MISS đơn nếu crash giữa fetch và insert.
        return _shop, _psi, _psl, _orders, _err

    def _save_cursor_after_db_commit(shop, p_status_int, orders, err):
        """Save cursor SAU KHI orders đã insert thành công vào DB.
        Đảm bảo: cursor chỉ advance khi data đã persist → crash-safe.
        """
        if err or not orders or not _use_cursor or not _cache_set:
            return
        _newest = orders[0]
        _newest_ts = str(_newest.get("inserted_at") or _newest.get("created_at") or "")
        if not _newest_ts:
            return
        try:
            _cache_set(_cursor_key(shop["id"], p_status_int), _newest_ts, ttl=86400 * 7)
        except Exception:
            pass

    # Default 10. Có thể override qua WH_SYNC_PARALLEL_WORKERS.
    _max_workers = int(os.getenv("WH_SYNC_PARALLEL_WORKERS", "10"))
    _parallel_workers = min(_max_workers, len(_fetch_tasks)) if _fetch_tasks else 1

    # ── STREAM pattern (peak RAM = ~max_workers tasks thay vì TẤT CẢ tasks) ──
    # Cũ: fetch tất cả vào _fetched_results (672 tasks × ~25MB = ~17GB) →
    #     mới process. Khi process tới task thứ N, các task 1..N-1 vẫn nằm RAM.
    # Mới: as each future completes → process ngay → orders ra khỏi scope → GC.
    # Peak in-memory: chỉ các task đang in-flight (≤ _parallel_workers).
    import gc as _gc

    def _result_stream():
        """Yield (shop, psi, psl, orders, err) ngay khi fetch xong, không buffer."""
        if _parallel_workers > 1:
            with ThreadPoolExecutor(max_workers=_parallel_workers) as _pool:
                _futures = {_pool.submit(_do_fetch, t): t for t in _fetch_tasks}
                for _fut in as_completed(_futures):
                    yield _fut.result()
                    # _fut object ra khỏi scope ở iteration sau → GC reclaim
        else:
            for t in _fetch_tasks:
                yield _do_fetch(t)

    # ── Stream-process: fetch + xử lý DB cùng phase (giảm peak RAM ~17×) ────
    _processed_count = 0
    for shop, p_status_int, p_status_label, orders, err in _result_stream():
        if err:
            errors.append(f"{shop['shop_name']} (status={p_status_int}): {err}")
            log.warning("  [%s] status=%s ERR: %s", shop["shop_name"], p_status_int, err)
            continue
        log.info("  [%s] status=%s(%s): %d orders fetched",
                 shop["shop_name"], p_status_int, p_status_label, len(orders))

        for order in orders:
                order_ext_id = str(order.get("id") or "").strip()
                if not order_ext_id:
                    continue
                carrier_name = detect_carrier_name(order)
                tracking_code = get_tracking_code(order)
                # Extract carrier pickup time từ status_history[status=2].updated_at
                # (carrier_picked_up_at field trên đơn thường rỗng — phải lấy từ history)
                picked_up_at = _extract_pickup_time(order) if p_status_int in (2, 3) else ""
                # Ngày tạo đơn trên POS
                _ins_raw = order.get("inserted_at") or order.get("created_at") or ""
                _ins_dt = _safe_parse_dt(_ins_raw)
                order_inserted_at = _ins_dt.strftime("%Y-%m-%d %H:%M") if _ins_dt else ""
                items = _extract_items(order)
                if not items:
                    skipped += 1
                    continue

                with db() as conn:
                    for item in items:
                        variation_id = item["variation_id"]
                        prod = var_id_to_product.get(variation_id)
                        if not prod:
                            sku_up = item["sku"].upper()
                            prod = sku_to_product.get(sku_up)
                        # Fallback 3: match theo tên (chỉ khi tên duy nhất trong wh_products)
                        if not prod:
                            prod = name_to_product.get(item["product_name"].strip().lower())
                        product_id = prod["id"] if prod else None
                        product_sku = (prod["sku"] if prod else item["sku"]) or ""
                        product_name = (prod["name"] if prod else item["product_name"]) or item["product_name"]
                        # Cảnh báo khi Pancake gửi tên khác với tên trong DB — có thể shop đổi sản phẩm
                        if prod and item["product_name"] and item["product_name"] != "Không rõ tên":
                            _pos_name = item["product_name"].strip().lower()
                            _db_name  = (prod["name"] or "").strip().lower()
                            if _pos_name and _pos_name != _db_name:
                                log.warning(
                                    "[sync] Tên SP lệch — pvid=%s SKU=%s | POS='%s' | DB='%s' | shop=%s order=%s",
                                    variation_id, product_sku, item["product_name"], prod["name"],
                                    shop.get("shop_name"), order_ext_id
                                )

                        # Dedup: ưu tiên variation_id → fallback SKU (bất kể variation_id) → product_id → product_name
                        # ⚠ BẮT BUỘC filter shop_id — Pancake đánh order_id_external từ 1 theo TỪNG shop,
                        # nên cùng số đơn có thể xuất hiện ở >30 shop khác nhau. Không filter shop_id
                        # sẽ match nhầm đơn shop khác → skip insert sai (bug 2026-05-13).
                        _shop_db_id = shop["id"]
                        _sel = "SELECT id, status, pancake_status, carrier_picked_up_at, product_id, product_sku, product_name, qty_ordered, shop_id, shop_name, warehouse_id, order_code, pos_variation_id FROM outbound_requests "
                        if variation_id:
                            # 1. Khớp chính xác variation_id
                            existing = conn.execute(
                                _sel + "WHERE order_id_external=? AND shop_id=? AND pos_variation_id=?",
                                (order_ext_id, _shop_db_id, variation_id)
                            ).fetchone()
                            # 2. Cùng SKU nhưng variation_id có thể khác (lookup trả UUID khác nhau mỗi lần)
                            if not existing and product_sku:
                                existing = conn.execute(
                                    _sel + "WHERE order_id_external=? AND shop_id=? AND product_sku=?",
                                    (order_ext_id, _shop_db_id, product_sku)
                                ).fetchone()
                            # 2b. LEGACY format: row cũ lưu UUID vào product_sku, pos_variation_id rỗng
                            #     → match theo product_sku=variation_id (UUID) để bắt row legacy còn sót
                            #     và update structure cho đúng (move UUID sang pos_variation_id).
                            if not existing:
                                existing = conn.execute(
                                    _sel + "WHERE order_id_external=? AND shop_id=? AND product_sku=? AND (pos_variation_id='' OR pos_variation_id IS NULL)",
                                    (order_ext_id, _shop_db_id, variation_id)
                                ).fetchone()
                            # 3. Row cũ có pos_variation_id rỗng, cùng tên sp
                            if not existing:
                                existing = conn.execute(
                                    _sel + "WHERE order_id_external=? AND shop_id=? AND product_name=? AND (pos_variation_id='' OR pos_variation_id IS NULL)",
                                    (order_ext_id, _shop_db_id, item["product_name"])
                                ).fetchone()
                        elif product_sku:
                            # 4. Không có variation_id: dùng SKU (bất kể variation_id)
                            existing = conn.execute(
                                _sel + "WHERE order_id_external=? AND shop_id=? AND product_sku=?",
                                (order_ext_id, _shop_db_id, product_sku)
                            ).fetchone()
                            # 5. Fallback tên (phòng row cũ có UUID trong product_sku)
                            if not existing:
                                existing = conn.execute(
                                    _sel + "WHERE order_id_external=? AND shop_id=? AND product_name=?",
                                    (order_ext_id, _shop_db_id, product_name)
                                ).fetchone()
                        elif product_id:
                            existing = conn.execute(
                                _sel + "WHERE order_id_external=? AND shop_id=? AND product_id=?",
                                (order_ext_id, _shop_db_id, product_id)
                            ).fetchone()
                        else:
                            existing = conn.execute(
                                _sel + "WHERE order_id_external=? AND shop_id=? AND product_name=?",
                                (order_ext_id, _shop_db_id, product_name)
                            ).fetchone()

                        if existing:
                            # Tự sửa product_id=NULL nếu giờ đã biết (backfill inline)
                            if existing["product_id"] is None and product_id:
                                conn.execute(
                                    "UPDATE outbound_requests SET product_id=?, product_sku=?, pos_variation_id=? WHERE id=?",
                                    (product_id, product_sku, variation_id or "", existing["id"])
                                )
                                updated += 1
                            ex_ps = existing["pancake_status"]
                            if p_status_label in ("returned", "cancelled"):
                                # Status 5/6: hoàn/hủy → đánh dấu để loại khỏi tổng xuất kho
                                if ex_ps not in ("returned", "cancelled"):
                                    conn.execute(
                                        "UPDATE outbound_requests SET pancake_status=? WHERE id=?",
                                        (p_status_label, existing["id"])
                                    )
                                    updated += 1
                                else:
                                    skipped += 1
                            elif p_status_label == "received":
                                # Status 3: khách đã nhận → update carrier_picked_up_at luôn
                                # kể cả khi đã là "received" để xóa ngày sai từ fallback cũ
                                if ex_ps not in ("returned", "cancelled"):
                                    new_pickup = picked_up_at or ""
                                    old_pickup = existing["carrier_picked_up_at"] or ""
                                    if ex_ps != "received" or new_pickup != old_pickup:
                                        conn.execute("""
                                            UPDATE outbound_requests
                                            SET pancake_status=?, carrier_name=?,
                                                tracking_code=?, carrier_picked_up_at=?
                                            WHERE id=?
                                        """, ("received", carrier_name, tracking_code,
                                              new_pickup, existing["id"]))
                                        updated += 1
                                    else:
                                        skipped += 1
                                else:
                                    skipped += 1
                            elif p_status_label == "shipped" and ex_ps in ("waiting", "confirmed"):
                                # Status 2: ĐVVC đã lấy hàng
                                ex_status = existing["status"] or ""
                                if ex_status == "pre_confirmed":
                                    # NV đã quét chuẩn bị → tự động trừ tồn + xác nhận
                                    _auto_ok = _auto_confirm_pre_confirmed(
                                        conn, existing, carrier_name, tracking_code,
                                        picked_up_at or "", order_ext_id
                                    )
                                    if not _auto_ok:
                                        # Không đủ tồn: cập nhật pancake_status, giữ pre_confirmed
                                        conn.execute("""
                                            UPDATE outbound_requests
                                            SET pancake_status=?, carrier_name=?,
                                                tracking_code=?, carrier_picked_up_at=?
                                            WHERE id=?
                                        """, ("shipped", carrier_name, tracking_code,
                                              picked_up_at or "", existing["id"]))
                                    updated += 1
                                else:
                                    # pending + shipped → chỉ cập nhật pancake_status
                                    # Hiện ở tab "Lệch POS" — NV chưa quét mà ĐVVC đã lấy
                                    conn.execute("""
                                        UPDATE outbound_requests
                                        SET pancake_status=?, carrier_name=?,
                                            tracking_code=?, carrier_picked_up_at=?
                                        WHERE id=?
                                    """, ("shipped", carrier_name, tracking_code,
                                          picked_up_at or "", existing["id"]))
                                    updated += 1
                            elif p_status_label == "confirmed" and ex_ps == "waiting":
                                # POS status 7 đã được remapped sang "confirmed" — cập nhật lại
                                conn.execute(
                                    "UPDATE outbound_requests SET pancake_status='confirmed' WHERE id=?",
                                    (existing["id"],)
                                )
                                updated += 1
                            elif p_status_label == "waiting" and ex_ps == "waiting":
                                # Vẫn đang chờ — cập nhật carrier/tracking nếu mới biết
                                has_carrier = carrier_name and carrier_name != "Không rõ ĐVVC"
                                has_tracking = bool(tracking_code)
                                if has_carrier or has_tracking:
                                    conn.execute("""
                                        UPDATE outbound_requests
                                        SET carrier_name=?, tracking_code=? WHERE id=?
                                    """, (carrier_name, tracking_code, existing["id"]))
                                    updated += 1
                                else:
                                    skipped += 1
                            elif p_status_label == "waiting" and ex_ps in ("received", "returned", "cancelled"):
                                # REVIVE — Pancake reuse order_id cho đơn mới sau khi đơn cũ
                                # đã kết thúc (received/returned/cancelled). Reset row về
                                # pending/waiting + cập nhật tracking + clear confirmations cũ
                                # để NV quét xử lý đơn mới. Bug fix 2026-06-10 (vụ tinhth/2907).
                                conn.execute("""
                                    UPDATE outbound_requests
                                       SET status='pending', pancake_status='waiting',
                                           carrier_name=?, tracking_code=?,
                                           carrier_picked_up_at='',
                                           pos_variation_id=?,
                                           qty_ordered=?,
                                           confirmed_by='', confirmed_at='',
                                           pre_confirmed_by='', pre_confirmed_at=''
                                     WHERE id=?
                                """, (carrier_name, tracking_code,
                                      variation_id or "", item["qty"], existing["id"]))
                                updated += 1
                            else:
                                skipped += 1
                            continue

                        # Đơn ĐVVC đã lấy từ ngày hôm trước trở về trước →
                        # tự xác nhận xuất kho (hàng đã đi, không cần nghiệp vụ kho nữa)
                        today_vn = now_hcm().strftime("%Y-%m-%d")
                        picked_up_date = (picked_up_at or "")[:10]
                        auto_confirm = (
                            p_status_label in ("shipped", "received")
                            and picked_up_date
                            and picked_up_date < today_vn
                        )
                        init_status    = "confirmed" if auto_confirm else "pending"
                        confirmed_by_v = "auto_sync" if auto_confirm else ""
                        confirmed_at_v = now_hcm().strftime("%Y-%m-%d %H:%M") if auto_confirm else ""

                        conn.execute("""
                            INSERT INTO outbound_requests
                            (order_code, order_id_external, shop_id, shop_name, product_id,
                             product_sku, product_name, qty_ordered, carrier_name,
                             tracking_code, carrier_picked_up_at, order_inserted_at,
                             status, pancake_status, pos_variation_id, created_at,
                             confirmed_by, confirmed_at, warehouse_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            order_ext_id, order_ext_id, shop["id"], shop["shop_name"],
                            product_id, product_sku, product_name, item["qty"],
                            carrier_name, tracking_code, picked_up_at or "",
                            order_inserted_at,
                            init_status, p_status_label,
                            variation_id or "",
                            now_hcm().strftime("%Y-%m-%d %H:%M"),
                            confirmed_by_v, confirmed_at_v,
                            shop.get("warehouse_id"),
                        ))
                        inserted += 1

        # ── Save cursor + free RAM ngay sau khi xử lý xong task này ─────────
        # Crash-safe: cursor advance CHỈ khi orders đã insert thành công.
        # Free RAM: orders ref ra khỏi scope → GC reclaim trước khi fetch task tiếp theo.
        _save_cursor_after_db_commit(shop, p_status_int, orders, None)
        _processed_count += 1
        if progress_cb:
            progress_cb(
                shop_name=shop["shop_name"],
                shop_idx=_processed_count, shop_total=len(_fetch_tasks),
                status_label="processed", fetched=_processed_count,
                inserted=inserted, updated=updated,
            )
        orders = None  # hint GC
        if _processed_count % 20 == 0:
            _gc.collect()

    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "errors": errors, "date_from": date_from, "date_to": date_to}
