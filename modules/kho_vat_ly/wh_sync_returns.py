"""
Sync hàng hoàn từ Pancake POS → wh_return_receipts (PostgreSQL).
Adapted từ posbot/sync_returns.py.
"""
from __future__ import annotations

import gc
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)
try:
    from tz_utils import now_hcm
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from tz_utils import now_hcm

import requests

from .wh_db import wh_db as db, SYNC_LOCK, RETURNS_SYNC_LOCK, FAST_RETURNS_SYNC_LOCK
from .wh_sync_orders import _day_ts, detect_carrier_name

try:
    from pancake_auth import get_api_key as _get_pancake_key, is_valid_hex_api_key as _is_valid_key, get_shop_api_key as _get_shop_api_key
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from pancake_auth import get_api_key as _get_pancake_key, is_valid_hex_api_key as _is_valid_key, get_shop_api_key as _get_shop_api_key

RETURN_STATUSES = (4, 5)  # 4=đang hoàn (hiển thị cảnh báo), 5=đã hoàn (được tạo phiếu nhập kho)


def _parse_dt_vn(value: Any) -> Optional[datetime]:
    """Parse datetime từ Pancake API → datetime VN (UTC+7), naive.

    BUG FIX 2026-04-30: Pancake trả ISO 8601 NAIVE (không có Z/+00:00)
    nhưng giờ thực là UTC. Code cũ chỉ +7h khi tzinfo != None → naive bị
    giữ nguyên giờ UTC. Fix: treat naive ISO 8601 as UTC, always +7h.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            ts = int(text)
            if ts > 10_000_000_000:
                ts //= 1000
            return datetime.utcfromtimestamp(ts) + timedelta(hours=7)
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None) + timedelta(hours=7)
        else:
            # NAIVE — Pancake trả format này, thực tế là UTC → cộng 7h
            dt = dt + timedelta(hours=7)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt) + timedelta(hours=7)
        except Exception:
            continue
    return None


def _signaled_at(order: dict) -> Optional[datetime]:
    for key in ("last_update_status_at", "updated_at", "inserted_at", "created_at"):
        dt = _parse_dt_vn(order.get(key))
        if dt:
            return dt
    return None


def _signaled_day(order: dict) -> Optional[str]:
    dt = _signaled_at(order)
    return dt.strftime("%Y-%m-%d") if dt else None


def _vn_str(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _get_tracking(order: dict) -> str:
    partner = order.get("partner") or {}
    return (partner.get("extend_code") or "").strip()


def _lines_from_order(order: dict) -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    for item in order.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            qty = int(float(item.get("quantity", 0) or 0))
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        vi = item.get("variation_info", {}) or {}
        name = (vi.get("name") or item.get("note_product") or item.get("note_product_internal")
                or item.get("name") or "Không rõ tên")
        name = str(name).strip() or "Không rõ tên"
        sku = (vi.get("custom_id") or item.get("variation_id") or item.get("product_id")
               or vi.get("product_id") or item.get("sku") or "")
        sku = str(sku).strip() or "UNKNOWN"
        raw.append({"sku": sku, "product_name": name, "qty": qty})

    merged: Dict[str, Dict[str, Any]] = {}
    for r in raw:
        k = r["sku"]
        if k not in merged:
            merged[k] = {"sku": r["sku"], "product_name": r["product_name"], "qty": 0}
        merged[k]["qty"] += r["qty"]
    return list(merged.values())


def _fetch_orders_by_status(
    shop_id: str, date_from: str, date_to: str, status: int,
    update_status: str = "inserted_at",
    api_key: str = "",
    max_pages: int = 500,
    progress_cb=None,
) -> Tuple[List[dict], Optional[str]]:
    """
    Fetch tất cả đơn theo status từ Pancake API.
    Pancake API không hỗ trợ date filter qua params → tải toàn bộ, lọc ngày ở Python.
    Thoát sớm khi inserted_at của đơn cũ nhất trên trang < date_from - 45 ngày buffer.
    """
    api_key = api_key or _get_pancake_key()
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }
    page_size = 100
    all_orders: List[dict] = []
    try:
        _early_cutoff = datetime.strptime(date_from, "%Y-%m-%d") - timedelta(days=45)
    except Exception:
        _early_cutoff = None

    for page in range(1, max_pages + 1):
        if progress_cb:
            try:
                progress_cb(page=page, fetched=len(all_orders), status=status)
            except Exception:
                pass
        params = [
            ("api_key", api_key), ("page_size", page_size),
            ("status", status), ("page", page),
            ("option_sort", "inserted_at_desc"),
        ]
        for attempt in range(3):
            try:
                resp = requests.post(url, params=params, headers=headers, json={}, timeout=45)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return all_orders, f"Lỗi API shop {shop_id} status={status}: {e}"
        if data.get("error") or not data.get("success", True):
            return all_orders, data.get("message") or "API lỗi"
        page_orders = data.get("data") or []
        all_orders.extend(page_orders)
        total_pages_api = int(data.get("total_pages") or 1)
        if page >= total_pages_api or len(page_orders) < page_size:
            break
        # Early exit: tất cả đơn trên trang này đều cũ hơn cutoff → không cần lấy thêm
        if _early_cutoff and page_orders:
            try:
                oldest = None
                for o in page_orders:
                    iat = _parse_dt_vn(o.get("inserted_at") or o.get("created_at"))
                    if iat and (oldest is None or iat < oldest):
                        oldest = iat
                if oldest and oldest < _early_cutoff:
                    log.debug("Early exit page %d: oldest inserted_at %s < cutoff %s", page, oldest, _early_cutoff)
                    break
            except Exception:
                pass
        time.sleep(0.2)
    return all_orders, None


def fetch_returns_for_shop(
    shop_id: str, date_from: str, date_to: str,
    update_status: str = "last_update_status_at",
    api_key: str = "",
    max_pages: int = 500,
    progress_cb=None,
) -> Tuple[List[dict], Optional[str]]:
    merged: Dict[str, dict] = {}
    errors: List[str] = []
    for st in RETURN_STATUSES:
        orders, err = _fetch_orders_by_status(
            shop_id, date_from, date_to, st, update_status, api_key=api_key,
            max_pages=max_pages, progress_cb=progress_cb,
        )
        if err:
            errors.append(err)
            continue
        for o in orders:
            oid = str(o.get("id") or o.get("order_id") or "").strip()
            if not oid:
                continue
            o["_pancake_return_status"] = int(o.get("status") or st)
            if oid not in merged:
                merged[oid] = o
            else:
                # Ưu tiên status cao hơn: 5 (đã hoàn) > 4 (đang hoàn) > 9 (đang đổi)
                existing_st = merged[oid].get("_pancake_return_status", 0)
                new_st = o.get("_pancake_return_status", 0)
                if new_st > existing_st:
                    merged[oid] = o
        time.sleep(0.2)
    if not merged and errors:
        return [], errors[0]
    return list(merged.values()), None


def _sku_to_product(conn, sku: str):
    """Tìm product theo SKU (custom_id) trước, fallback theo pvid qua wh_variation_map.

    KHÔNG fallback theo `wh_products.pos_variation_id` (legacy) — chỉ dùng vmap
    làm single source of truth cho pvid mapping.
    """
    row = conn.execute(
        "SELECT id, name FROM wh_products WHERE UPPER(sku)=UPPER(%s) LIMIT 1", (sku,)
    ).fetchone()
    if row:
        return row["id"], row["name"]
    # Fallback: sku có thể là pvid (UUID) → tra vmap
    row2 = conn.execute(
        "SELECT p.id, p.name FROM wh_variation_map vm "
        "JOIN wh_products p ON p.id=vm.product_id "
        "WHERE vm.pos_variation_id=%s LIMIT 1", (sku,)
    ).fetchone()
    if row2:
        return row2["id"], row2["name"]
    return None, None


def sync_returns_for_date_range(
    date_from: str, date_to: str,
    only_pos_shop_id: str = "",
    only_pos_shop_ids: Optional[set] = None,
    update_status: str = "last_update_status_at",
    progress_cb=None,
    fast_only: bool = False,
) -> dict:
    """fast_only=True: dùng FAST_RETURNS_SYNC_LOCK riêng, chạy song song với full returns sync."""
    lock = FAST_RETURNS_SYNC_LOCK if fast_only else RETURNS_SYNC_LOCK
    if not lock.acquire(blocking=False):
        msg = "Fast returns sync đang chạy" if fast_only else "Sync hoàn đang chạy, bỏ qua lần này để tránh duplicate"
        return {"inserted": 0, "skipped": 0, "errors": [msg]}
    try:
        return _sync_returns_locked(date_from, date_to,
                                    only_pos_shop_id=only_pos_shop_id,
                                    only_pos_shop_ids=only_pos_shop_ids,
                                    update_status=update_status,
                                    progress_cb=progress_cb)
    finally:
        lock.release()


def _sync_returns_locked(
    date_from: str, date_to: str,
    only_pos_shop_id: str = "",
    only_pos_shop_ids: Optional[set] = None,
    update_status: str = "last_update_status_at",
    progress_cb=None,
) -> dict:
    # Tính max_pages từ khoảng ngày: buffer 45 ngày đã có trong early exit
    try:
        days_back = max(1, (datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")).days + 1)
    except Exception:
        days_back = 200
    # Ước tính: tối đa 30 return/ngày, 100/trang, buffer 4x → pages = days*30/100*4
    max_pages = min(500, max(10, int(days_back * 30 / 100 * 4) + 5))
    with db() as conn:
        q = ("SELECT id as shop_id_local, shop_key, shop_name, pos_shop_id, pos_api_key FROM wh_shops WHERE status='active'"
             + (" AND pos_shop_id=%s" if only_pos_shop_id else ""))
        shops = conn.execute(q, ([only_pos_shop_id] if only_pos_shop_id else [])).fetchall()
        # Filter theo only_pos_shop_ids (set) — dùng cho batched per-shop sync
        if only_pos_shop_ids:
            shops = [s for s in shops if str(s["pos_shop_id"]) in only_pos_shop_ids]

    inserted = skipped = 0
    errors: List[str] = []

    # Lọc sẵn shops có api_key hợp lệ — tránh submit task rỗng
    valid_shops: List[Tuple[Any, str]] = []
    for _s in shops:
        _key = str(_s["pos_api_key"] or "").strip()
        if not _is_valid_key(_key):
            _key = _get_shop_api_key(str(_s["pos_shop_id"]))
        if _is_valid_key(_key):
            valid_shops.append((_s, _key))
        else:
            log.info("SKIP %s (%s): chưa có api_key hợp lệ",
                     _s["shop_name"], _s["pos_shop_id"])
    shop_total = len(valid_shops)

    def _do_fetch(task):
        _shop, _api_key = task
        _shop_name = _shop["shop_name"]
        # progress callback per-page (không update inserted vì closure đọc snapshot)
        def _page_cb(page, fetched, status, _sn=_shop_name):
            if progress_cb:
                try:
                    progress_cb(shop_name=_sn, shop_idx=0, shop_total=shop_total,
                                page=page, fetched=fetched, status=status,
                                inserted=inserted)
                except Exception:
                    pass
        _orders, _err = fetch_returns_for_shop(
            str(_shop["pos_shop_id"]), date_from, date_to, update_status,
            api_key=_api_key, max_pages=max_pages, progress_cb=_page_cb,
        )
        return _shop, _orders, _err

    # Default 10. Override qua WH_SYNC_PARALLEL_WORKERS (giống outbound).
    _max_workers = int(os.getenv("WH_SYNC_PARALLEL_WORKERS", "10"))
    _parallel_workers = min(_max_workers, len(valid_shops)) if valid_shops else 1

    def _result_stream():
        """Yield (shop, orders, err) ngay khi fetch xong, không buffer."""
        if _parallel_workers > 1 and valid_shops:
            with ThreadPoolExecutor(max_workers=_parallel_workers) as _pool:
                _futures = {_pool.submit(_do_fetch, t): t for t in valid_shops}
                for _fut in as_completed(_futures):
                    yield _fut.result()
        else:
            for t in valid_shops:
                yield _do_fetch(t)

    # ── Stream pattern: fetch song song + process inline (peak RAM ≪ buffer) ──
    _processed_count = 0
    for shop, orders, err in _result_stream():
        shop_name = shop["shop_name"]
        pos_shop_id = shop["pos_shop_id"]
        shop_id_local = shop["shop_id_local"]
        if err:
            errors.append(f"{shop_name}: {err}")
            _processed_count += 1
            continue

        if progress_cb:
            try:
                progress_cb(shop_name=shop_name, shop_idx=_processed_count + 1,
                            shop_total=shop_total, page=0,
                            fetched=len(orders or []), status=None,
                            inserted=inserted)
            except Exception:
                pass

        with db() as conn:
            for order in (orders or []):
                sig_day = _signaled_day(order)
                if sig_day and (sig_day < date_from or sig_day > date_to):
                    skipped += 1
                    continue

                order_id_ext = str(order.get("id") or "").strip()
                if not order_id_ext:
                    continue

                order_code = str(order.get("order_number") or order.get("code") or order_id_ext).strip()
                tracking = _get_tracking(order)
                carrier = detect_carrier_name(order)
                sig_dt = _signaled_at(order)
                returned_at = _vn_str(sig_dt)
                pancake_return_status = int(order.get("_pancake_return_status") or order.get("status") or 0) or None

                items = _lines_from_order(order)
                if not items:
                    skipped += 1
                    continue

                for item in items:
                    sku_raw = item["sku"]
                    product_id, product_name_db = _sku_to_product(conn, sku_raw)
                    product_name = product_name_db or item["product_name"]

                    # ⚠ BẮT BUỘC filter shop_id — Pancake đánh order_id_external per-shop,
                    # cùng số đơn xuất hiện ở >30 shop khác nhau. Không filter shop_id
                    # sẽ match nhầm đơn shop khác (bug 2026-05-13, same pattern as outbound).
                    existing = conn.execute("""
                        SELECT id, good_ledger_posted_qty, pancake_return_status FROM wh_return_receipts
                        WHERE order_id_external=%s AND shop_id=%s AND product_sku=%s AND source='pos_sync'
                    """, (order_id_ext, shop_id_local, sku_raw)).fetchone()

                    if existing:
                        if (existing["good_ledger_posted_qty"] or 0) > 0:
                            skipped += 1
                            continue
                        conn.execute("""
                            UPDATE wh_return_receipts
                            SET order_code=%s, shop_id=%s, shop_name=%s,
                                product_id=%s, product_name=%s,
                                qty_expected=%s, carrier_name=%s, tracking_code=%s,
                                returned_at=%s, signaled_at=%s, pancake_return_status=%s
                            WHERE id=%s
                        """, (
                            order_code, shop_id_local, shop_name,
                            product_id, product_name,
                            item["qty"], carrier, tracking, returned_at, returned_at,
                            pancake_return_status, existing["id"],
                        ))
                        skipped += 1
                        continue

                    # Lookup pos_variation_id từ đơn xuất gốc
                    _var_row = conn.execute("""
                        SELECT pos_variation_id FROM wh_outbound_requests
                        WHERE order_code=%s AND product_id=%s
                          AND pos_variation_id IS NOT NULL AND pos_variation_id != ''
                        LIMIT 1
                    """, (order_code, product_id)).fetchone() if product_id else None
                    _var_id = (_var_row["pos_variation_id"] if _var_row else None) or None

                    conn.execute("""
                        INSERT INTO wh_return_receipts
                          (order_code, order_id_external, shop_id, shop_name,
                           product_id, product_sku, product_name,
                           qty_expected, carrier_name, tracking_code,
                           returned_at, signaled_at, source, status,
                           pancake_return_status, created_at, pos_variation_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pos_sync','pending',%s,%s,%s)
                    """, (
                        order_code, order_id_ext, shop_id_local, shop_name,
                        product_id, sku_raw, product_name,
                        item["qty"], carrier, tracking, returned_at, returned_at,
                        pancake_return_status,
                        now_hcm().strftime("%Y-%m-%d %H:%M"),
                        _var_id,
                    ))
                    inserted += 1

        # ── Free RAM ngay sau khi xử lý xong shop này ─────────────────────
        # orders ref ra khỏi scope → GC reclaim trước khi process shop tiếp theo.
        _processed_count += 1
        orders = None  # hint GC
        if _processed_count % 20 == 0:
            gc.collect()

    return {"inserted": inserted, "skipped": skipped, "errors": errors}
