#!/usr/bin/env python3
"""
Sync đơn hoàn từ Pancake vào wh_return_receipts (PostgreSQL).

Chạy tự động mỗi 2 giờ bởi APScheduler (scheduler.py).
An toàn để chạy lại nhiều lần (idempotent).

Business rules:
  - Chỉ xử lý active shops có pos_api_key hợp lệ (hex 32 ký tự).
  - KHÔNG dùng access_token/cookie cũ — dùng pos_api_key riêng từng shop.
  - Pancake status 4 = đang hoàn (ĐVVC đang chuyển về).
  - Pancake status 5 = đã hoàn (hàng đã về tay shop, chờ nhập kho).

3 việc làm trong 1 lần chạy:
  1. Fetch tất cả đơn status=4 và status=5 từ Pancake (full order object gồm items).
  2. Cập nhật pancake_return_status cho các đơn ĐÃ CÓ trong wh_return_receipts.
  3. TẠO MỚI rows cho các đơn CHƯA CÓ trong wh_return_receipts (backfill tự động).
     → Đơn không còn ở status 4/5 → set pancake_return_status=NULL.
"""
import json
import os
import sys
import time
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from db import get_conn  # noqa: E402

SHOPS_FILE = os.path.join(BASE_DIR, "shops.json")
PAGE_SIZE  = 100
DELAY      = 0.3


# ─── Helpers ────────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def is_valid_api_key(key: str) -> bool:
    """Kiểm tra api_key hợp lệ: hex 32 ký tự (không phải JWT cũ bắt đầu bằng eyJ)."""
    if not key:
        return False
    k = str(key).strip()
    return len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k)


def fetch_orders_by_status(shop_id: str, api_key: str, status: int) -> list:
    """Fetch full order objects (bao gồm items) với status từ Pancake API.
    Dùng api_key (hex 32 ký tự) thay vì access_token/cookie cũ.
    """
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_id}/orders/get_orders"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_id}/order",
        "User-Agent": "Mozilla/5.0",
    }
    result = []
    page = 1
    while True:
        params = [
            ("api_key", api_key),
            ("page_size", PAGE_SIZE),
            ("page", page),
            ("status", status),
            ("option_sort", "inserted_at_desc"),
        ]
        try:
            resp = requests.post(url, params=params, headers=headers, json={}, timeout=45)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"    [ERR] API status={status} page={page}: {exc}")
            break
        if data.get("error") or not data.get("success", True):
            print(f"    [ERR] API status={status}: {data.get('message') or 'lỗi không rõ'}")
            break
        orders = data.get("data") or []
        result.extend([o for o in orders if isinstance(o, dict)])
        total_pages = int(data.get("total_pages") or 1)
        if page >= total_pages or len(orders) < PAGE_SIZE:
            break
        page += 1
        time.sleep(DELAY)
    return result


def extract_carrier_tracking(o: dict) -> tuple:
    """Trích xuất (carrier_name, tracking_code) từ order object."""
    carrier = str(o.get("carrier_name") or "").strip()
    if not carrier:
        c = o.get("carrier")
        if isinstance(c, dict):
            carrier = str(c.get("name") or c.get("carrier_name") or "").strip()
        elif isinstance(c, str):
            carrier = c.strip()
    tracking = str(o.get("tracking_code") or o.get("tracking_number") or "").strip()
    return carrier, tracking


def extract_returned_at(o: dict) -> str:
    """Trích xuất thời điểm hoàn từ order object."""
    for field in ("return_at", "returned_at", "return_date", "carrier_returned_at", "updated_at"):
        val = str(o.get(field) or "").strip()
        if val:
            return val
    return ""


def extract_items(o: dict) -> list:
    """
    Trích xuất items từ order.
    Trả về list of {sku, product_name, qty}.
    Nếu không có items → 1 row placeholder (vẫn track đơn).
    """
    raw = o.get("items") or []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        vi = item.get("variation_info") or {}
        sku = str(vi.get("custom_id") or item.get("variation_id") or item.get("sku") or "").strip()
        name = (
            str(vi.get("name") or "").strip()
            or str(item.get("note_product") or "").strip()
            or str(item.get("name") or "").strip()
            or "Không rõ tên"
        )
        qty = max(1, int(float(item.get("quantity", 1) or 1)))
        result.append({"sku": sku, "name": name, "qty": qty})
    if not result:
        result.append({"sku": "", "name": "Không rõ sản phẩm", "qty": 1})
    return result


# ─── Core sync logic cho 1 shop ─────────────────────────────────────────────

def sync_shop(conn, wh_shop_id: int, shop_name: str,
              pancake_orders: dict, product_map: dict) -> dict:
    """
    pancake_orders = {order_id_external: {"status": int, "obj": dict}}

    Trả về stats dict.
    """
    cur = conn.cursor()

    # Lấy toàn bộ (order_id_external, product_sku) → pancake_return_status hiện tại
    cur.execute("""
        SELECT order_id_external, product_sku, pancake_return_status
        FROM wh_return_receipts
        WHERE shop_id=%s
    """, (wh_shop_id,))
    existing: dict = {}             # order_id → {status: int|None}
    existing_pairs: set = set()     # (order_id, sku)
    for row in cur.fetchall():
        oid, sku, pst = row
        existing_pairs.add((oid, sku))
        if oid not in existing:
            existing[oid] = {"status": pst}

    stats = {"updated": 0, "cleared": 0, "new_orders": 0, "new_rows": 0, "no_change": 0}

    # ── 1. Cập nhật status cho đơn đã có ──────────────────────────────────
    for oid, info in pancake_orders.items():
        new_status = info["status"]
        if oid in existing:
            if existing[oid]["status"] != new_status:
                cur.execute("""
                    UPDATE wh_return_receipts
                    SET pancake_return_status=%s
                    WHERE shop_id=%s AND order_id_external=%s
                """, (new_status, wh_shop_id, oid))
                stats["updated"] += cur.rowcount
            else:
                stats["no_change"] += 1

    # ── 2. Clear đơn không còn status 4/5 — CHỈ khi fetch thành công (có data) ──
    # Bảo vệ: nếu pancake_orders rỗng thì KHÔNG clear (tránh xóa nhầm khi API lỗi)
    if pancake_orders:
        stale = [oid for oid in existing if oid not in pancake_orders]
        for oid in stale:
            cur.execute("""
                UPDATE wh_return_receipts
                SET pancake_return_status=NULL
                WHERE shop_id=%s AND order_id_external=%s
                  AND pancake_return_status IS NOT NULL
            """, (wh_shop_id, oid))
            stats["cleared"] += cur.rowcount

    # ── 3. Insert đơn mới (backfill) ─────────────────────────────────────
    new_order_ids = [oid for oid in pancake_orders if oid not in existing]
    stats["new_orders"] = len(new_order_ids)

    for oid in new_order_ids:
        o          = pancake_orders[oid]["obj"]
        pstatus    = pancake_orders[oid]["status"]
        carrier, tracking = extract_carrier_tracking(o)
        returned_at       = extract_returned_at(o)
        items             = extract_items(o)

        for item in items:
            sku  = item["sku"]
            pair = (oid, sku)
            if pair in existing_pairs:
                continue
            prod       = product_map.get(sku)
            product_id = prod["id"] if prod else None
            pname      = (prod["name"] if prod else item["name"]) or item["name"]
            cur.execute("""
                INSERT INTO wh_return_receipts
                  (order_code, shop_id, shop_name, product_id, product_sku, product_name,
                   qty_expected, carrier_name, tracking_code, order_id_external,
                   returned_at, signaled_at, source, status, created_at, pancake_return_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pos_sync','pending',%s,%s)
            """, (
                oid, wh_shop_id, shop_name,
                product_id, sku, pname,
                item["qty"], carrier, tracking, oid,
                returned_at, returned_at,
                now_str(), pstatus,
            ))
            existing_pairs.add(pair)
            stats["new_rows"] += 1

    conn.commit()
    cur.close()
    return stats


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    # Đọc shops từ DB (nguồn đúng) — KHÔNG dùng shops.json để tránh sót shop
    # (shops.json có thể thiếu shop → sync hoàn sót shop, lệch số).
    with get_conn() as _c0:
        _cur0 = _c0.cursor()
        _cur0.execute(
            "SELECT shop_key, pancake_shop_id, status::text FROM shops "
            "WHERE status='active' AND COALESCE(pancake_shop_id,'') <> ''"
        )
        active_shops = [{"shop_key": r[0], "shop_id": str(r[1]), "status": r[2]} for r in _cur0.fetchall()]
        _cur0.close()

    # Load lookup tables
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT shop_name, id FROM wh_shops")
        wh_shop_map = {r[0].lower(): r[1] for r in cur.fetchall()}
        cur.execute("SELECT sku, id, name FROM wh_products")
        product_map = {r[0]: {"id": r[1], "name": r[2]} for r in cur.fetchall() if r[0]}
        # Load api_key từ wh_shops DB (nguồn ưu tiên, shops.json không lưu key)
        cur.execute(
            "SELECT shop_key, pos_api_key FROM wh_shops "
            "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' AND status='active'"
        )
        db_api_keys = {r[0]: r[1].strip() for r in cur.fetchall() if r[1]}
        cur.close()

    print(f"=== sync_wh_returns_pg: {len(active_shops)} active shops | {now_str()} ===\n")

    total_updated = total_cleared = total_new_orders = total_new_rows = 0
    skipped_no_key = 0

    for shop in active_shops:
        shop_key   = shop.get("shop_key", "")
        shop_name  = shop.get("shop_name", "")
        pancake_id = str(shop.get("shop_id") or shop.get("pos_shop_id") or "")
        # Ưu tiên key từ DB (wh_shops), fallback về shops.json
        api_key = db_api_keys.get(shop_key) or str(shop.get("pos_api_key") or "").strip()

        # Bỏ qua shop chưa có api_key hợp lệ
        if not is_valid_api_key(api_key):
            print(f"  [SKIP] {shop_key}/{shop_name}: chưa có pos_api_key hợp lệ")
            skipped_no_key += 1
            continue

        wh_shop_id = wh_shop_map.get(shop_name.lower())
        if not wh_shop_id:
            print(f"  [SKIP] {shop_key}/{shop_name}: không có trong wh_shops")
            continue

        # Fetch full orders status=4 và status=5 bằng api_key riêng của shop
        pancake_orders: dict = {}
        fetch_ok = True
        for st in [4, 5]:
            orders = fetch_orders_by_status(pancake_id, api_key, st)
            if orders is None:
                fetch_ok = False
                break
            for o in orders:
                oid = str(o.get("id") or "").strip()
                if not oid:
                    continue
                # Nếu có cả 4 lẫn 5 → ưu tiên status=5
                if oid not in pancake_orders or st > pancake_orders[oid]["status"]:
                    pancake_orders[oid] = {"status": st, "obj": o}

        if not fetch_ok:
            print(f"  [ERR] {shop_key}/{shop_name}: fetch thất bại, bỏ qua shop này")
            continue

        st4 = sum(1 for v in pancake_orders.values() if v["status"] == 4)
        st5 = sum(1 for v in pancake_orders.values() if v["status"] == 5)
        print(f"  [{shop_key}] Pancake: {st4} đang_hoàn + {st5} đã_hoàn", end=" ")

        with get_conn() as conn:
            stats = sync_shop(conn, wh_shop_id, shop_name, pancake_orders, product_map)

        parts = []
        if stats["updated"]:   parts.append(f"updated={stats['updated']}")
        if stats["cleared"]:   parts.append(f"cleared={stats['cleared']}")
        if stats["new_rows"]:  parts.append(f"new_rows={stats['new_rows']}({stats['new_orders']} đơn)")
        if stats["no_change"]: parts.append(f"no_change={stats['no_change']}")
        print("→ " + (", ".join(parts) if parts else "OK (no change)"))

        total_updated    += stats["updated"]
        total_cleared    += stats["cleared"]
        total_new_orders += stats["new_orders"]
        total_new_rows   += stats["new_rows"]
        time.sleep(0.2)

    print(f"\n=== DONE: updated={total_updated} cleared={total_cleared} "
          f"new_orders={total_new_orders} new_rows={total_new_rows} "
          f"skipped_no_key={skipped_no_key} ===")

    # Verify kết quả
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT wr.pancake_return_status, COUNT(DISTINCT wr.order_code)
            FROM wh_return_receipts wr
            JOIN wh_shops ws ON ws.id = wr.shop_id
            WHERE wr.pancake_return_status IN (4, 5)
            GROUP BY wr.pancake_return_status
            ORDER BY wr.pancake_return_status
        """)
        print("\nKết quả cuối:")
        for r in cur.fetchall():
            label = "đang_hoàn" if r[0] == 4 else "đã_hoàn"
            print(f"  status={r[0]} ({label}): {r[1]:,} đơn")
        cur.execute("""
            SELECT COUNT(DISTINCT order_code) FROM wh_return_receipts
            WHERE pancake_return_status=5 AND status='pending'
        """)
        print(f"  pending nhập kho (status=5): {cur.fetchone()[0]:,} đơn")
        cur.close()


if __name__ == "__main__":
    main()
