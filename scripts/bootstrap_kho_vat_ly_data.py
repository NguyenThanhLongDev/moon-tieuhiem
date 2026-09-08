#!/usr/bin/env python3
"""
Bootstrap dữ liệu Kho vật lý lần đầu.

Chạy khi:
  - Server mới, wh_outbound_requests + wh_shop_inventory còn trống
  - Sau khi đã seed wh_shops

Thực hiện:
  1. Sync tồn kho POS từ Pancake cho tất cả active wh_shops
  2. Sync đơn xuất hàng (outbound) N ngày gần nhất (mặc định 30 ngày)

Chạy an toàn nhiều lần (UPSERT / ON CONFLICT).

Cách dùng:
  python3 scripts/bootstrap_kho_vat_ly_data.py
  OUTBOUND_DAYS=60 python3 scripts/bootstrap_kho_vat_ly_data.py
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

OUTBOUND_DAYS = int(os.getenv("OUTBOUND_DAYS", "30"))


def main() -> None:
    from modules.kho_vat_ly.wh_db import wh_db

    with wh_db() as conn:
        # ── 0. Dọn dẹp sản phẩm có SKU xấu (chứa UUID hoặc POS-) ─────────────
        # Xóa inventory (không có FK đi chỗ khác)
        deleted_inv = conn.execute("""
            DELETE FROM shop_inventory
            WHERE product_id IN (
                SELECT id FROM products
                WHERE sku ~ '[0-9a-f]{8}-[0-9a-f]{4}'
                   OR sku LIKE 'POS-%'
            )
        """).rowcount
        # Đổi tên SKU xấu → OLD-{id} (không xóa vì có FK từ return_receipts, outbound...)
        # pos_variation_id = NULL để sync tạo lại bản mới
        updated_prod = conn.execute("""
            UPDATE products
            SET sku = 'OLD-' || id::text,
                pos_variation_id = NULL
            WHERE sku ~ '[0-9a-f]{8}-[0-9a-f]{4}'
               OR sku LIKE 'POS-%'
        """).rowcount
        if updated_prod > 0:
            print(f"[0/2] Đánh dấu {updated_prod} sản phẩm SKU xấu + xóa {deleted_inv} bản ghi inventory")

        # ── 1. Lấy danh sách active wh_shops ─────────────────────────────────
        shops = conn.execute(
            "SELECT * FROM wh_shops WHERE status='active' ORDER BY id"
        ).fetchall()

        if not shops:
            print("WARN: wh_shops trống — chạy bootstrap_wh_shops_from_shops_json.py trước.", file=sys.stderr)
            sys.exit(1)

        print(f"[1/2] Sync POS inventory ({len(shops)} shop)...")
        from modules.kho_vat_ly.wh_sync_pos import sync_all_shops
        result = sync_all_shops(conn, shops)
        ok_count  = len(result.get("shops", []))
        total_syn = result.get("total_synced", 0)
        errors    = result.get("errors", [])
        print(f"  OK: {ok_count} shop, {total_syn} bản ghi inventory sync")
        for r in result.get("shops", []):
            status = f"{r['synced']} bản ghi"
            if r.get("error"):
                status = f"LỖI: {r['error']}"
            elif r["synced"] == 0:
                status = "0 bản ghi (bỏ qua hết)"
            print(f"    {r['shop']}: {status}")
        if errors:
            print(f"  WARN: {len(errors)} lỗi — {errors[:3]}")

    # ── 2. Sync outbound N ngày gần nhất (dùng wh_db riêng vì sync_outbound dùng conn riêng) ──
    today     = datetime.date.today()
    date_from = (today - datetime.timedelta(days=OUTBOUND_DAYS)).isoformat()
    date_to   = today.isoformat()
    print(f"[2/2] Sync outbound {date_from} → {date_to} ({OUTBOUND_DAYS} ngày)...")
    from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range
    result2 = sync_outbound_for_date_range(date_from, date_to)
    inserted = result2.get("inserted", 0)
    updated  = result2.get("updated", 0)
    errors2  = result2.get("errors", [])
    print(f"  OK: {inserted} mới, {updated} cập nhật")
    if errors2:
        print(f"  WARN: {len(errors2)} lỗi — {errors2[:3]}")

    print("bootstrap_kho_vat_ly_data: DONE")


if __name__ == "__main__":
    main()
