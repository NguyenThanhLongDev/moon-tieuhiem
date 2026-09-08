"""
bulk_confirm_pending.py
=======================
Xác nhận hàng loạt đơn pending:
  - wh_outbound_requests: tất cả pending → confirmed
  - wh_return_receipts  : pending → received_ok
                          TRỪ những đơn pancake_return_status=4 ("đang hoàn")

Cách dùng trên VPS:
  # Bước 1 — chạy thử, KHÔNG ghi DB
  python3 scripts/bulk_confirm_pending.py --dry-run

  # Bước 2 — chạy thật khi kết quả dry-run đúng
  python3 scripts/bulk_confirm_pending.py

Yêu cầu:
  - Biến môi trường DATABASE_URL đã được set (giống web app)
  - pip install psycopg2-binary  (nếu chưa có)
"""

import os
import sys
import argparse
from datetime import datetime

try:
    import psycopg2
except ImportError:
    print("❌ Thiếu psycopg2. Cài: pip install psycopg2-binary")
    sys.exit(1)


# ─── Config ───────────────────────────────────────────────────────────────────
WH_ID       = 1          # Kho Vĩnh Xá
NOTE_OUT    = "Xác nhận đơn xuất tồn đọng (batch)"
NOTE_RETURN = "Xác nhận nhận hoàn hàng tồn đọng (batch)"
NOTE_TOPUP  = "Nhập bù tồn kho để xác nhận đơn xuất tồn đọng"
# ──────────────────────────────────────────────────────────────────────────────


def get_conn():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("❌ Chưa set biến môi trường DATABASE_URL")
        sys.exit(1)
    return psycopg2.connect(db_url)


def run(dry_run: bool):
    mode = "🔍 DRY-RUN (không ghi DB)" if dry_run else "🚀 CHẠY THẬT"
    print(f"\n{'='*55}")
    print(f"  bulk_confirm_pending.py  |  {mode}")
    print(f"{'='*55}\n")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    cur  = conn.cursor()

    try:
        # ════════════════════════════════════════════════════════
        # PHẦN 1: XUẤT HÀNG (wh_outbound_requests)
        # ════════════════════════════════════════════════════════
        print("─── PHẦN 1: ĐƠN XUẤT HÀNG (wh_outbound_requests) ───")

        # Đếm hiện tại
        cur.execute("SELECT COUNT(*) FROM wh_outbound_requests WHERE status='pending'")
        total_out = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM wh_outbound_requests WHERE status='pending' AND product_id IS NULL")
        null_out = cur.fetchone()[0]
        matchable_out = 0
        if null_out > 0:
            cur.execute("""
                SELECT COUNT(*) FROM wh_outbound_requests o
                JOIN wh_products p ON p.pos_variation_id = o.product_sku
                WHERE o.status='pending' AND o.product_id IS NULL
            """)
            matchable_out = cur.fetchone()[0]

        print(f"  Tổng pending xuất    : {total_out:,}")
        print(f"  NULL product_id      : {null_out:,}")
        print(f"  Tra được qua SKU     : {matchable_out:,}")
        print(f"  Không tra được       : {null_out - matchable_out:,} (vẫn xác nhận, không ảnh hưởng kho)")

        if not dry_run and total_out > 0:
            # Bước 1a: cập nhật product_id từ pos_variation_id
            cur.execute("""
                UPDATE wh_outbound_requests o
                SET product_id = p.id
                FROM wh_products p
                WHERE p.pos_variation_id = o.product_sku
                  AND o.status = 'pending'
                  AND o.product_id IS NULL
            """)
            print(f"  ✔ Cập nhật product_id : {cur.rowcount} rows")

            # Bước 1b: nhập bù tồn kho (INSERT mới)
            cur.execute("""
                INSERT INTO wh_inventory (product_id, warehouse_id, qty, updated_at)
                SELECT o.product_id, %s, SUM(o.qty_ordered), %s
                FROM wh_outbound_requests o
                WHERE o.status='pending' AND o.product_id IS NOT NULL
                GROUP BY o.product_id
                ON CONFLICT DO NOTHING
            """, (WH_ID, now_str))
            print(f"  ✔ Inventory insert    : {cur.rowcount} sản phẩm mới")

            # Bước 1c: ghi movement inbound bù
            cur.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
                SELECT 'inbound', o.product_id, %s, SUM(o.qty_ordered), 0, SUM(o.qty_ordered),
                       NULL, %s, %s
                FROM wh_outbound_requests o
                WHERE o.status='pending' AND o.product_id IS NOT NULL
                GROUP BY o.product_id
            """, (WH_ID, NOTE_TOPUP, now_str))
            print(f"  ✔ Inbound movements   : {cur.rowcount}")

            # Bước 1d: xác nhận có product_id
            cur.execute("""
                UPDATE wh_outbound_requests
                SET status='confirmed', qty_confirmed=qty_ordered,
                    note=%s, warehouse_id=%s
                WHERE status='pending' AND product_id IS NOT NULL
            """, (NOTE_OUT, WH_ID))
            print(f"  ✔ Confirmed (có SP)   : {cur.rowcount:,}")

            # Bước 1e: xác nhận không có product_id
            cur.execute("""
                UPDATE wh_outbound_requests
                SET status='confirmed', qty_confirmed=qty_ordered,
                    note=%s, warehouse_id=%s
                WHERE status='pending' AND product_id IS NULL
            """, (NOTE_OUT, WH_ID))
            print(f"  ✔ Confirmed (ko SP)   : {cur.rowcount:,}")

            # Bước 1f: trừ tồn về 0 (đã xuất hết)
            cur.execute("""
                UPDATE wh_inventory SET qty=0, updated_at=%s
                WHERE warehouse_id=%s AND qty > 0
            """, (now_str, WH_ID))
            print(f"  ✔ Inventory zeroed    : {cur.rowcount} rows")

        elif total_out == 0:
            print("  ℹ️  Không có đơn xuất pending — bỏ qua")

        # ════════════════════════════════════════════════════════
        # PHẦN 2: HOÀN HÀNG (wh_return_receipts)
        # ════════════════════════════════════════════════════════
        print()
        print("─── PHẦN 2: ĐƠN HOÀN HÀNG (wh_return_receipts) ───")

        cur.execute("SELECT COUNT(*) FROM wh_return_receipts WHERE status='pending'")
        total_ret = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM wh_return_receipts
            WHERE status='pending' AND pancake_return_status=4
        """)
        dang_hoan = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM wh_return_receipts
            WHERE status='pending' AND (pancake_return_status IS NULL OR pancake_return_status != 4)
        """)
        can_confirm = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM wh_return_receipts
            WHERE status='pending' AND product_id IS NOT NULL
              AND (pancake_return_status IS NULL OR pancake_return_status != 4)
        """)
        ret_with_product = cur.fetchone()[0]

        print(f"  Tổng pending hoàn    : {total_ret:,}")
        print(f"  ⚠️  Đang hoàn (skip) : {dang_hoan:,}  ← GIỮ NGUYÊN pending")
        print(f"  Sẽ xác nhận          : {can_confirm:,}")
        print(f"    → có product_id    : {ret_with_product:,}  (cộng vào kho)")
        print(f"    → không có product : {can_confirm - ret_with_product:,}  (chỉ đổi status)")

        if not dry_run and can_confirm > 0:
            # Bước 2a: cộng stock (UPDATE existing)
            cur.execute("""
                UPDATE wh_inventory wi
                SET qty = wi.qty + sub.total_return, updated_at = %s
                FROM (
                    SELECT product_id, SUM(qty_expected) AS total_return
                    FROM wh_return_receipts
                    WHERE status='pending' AND product_id IS NOT NULL
                      AND (pancake_return_status IS NULL OR pancake_return_status != 4)
                    GROUP BY product_id
                ) sub
                WHERE wi.product_id = sub.product_id AND wi.warehouse_id = %s
            """, (now_str, WH_ID))
            upd = cur.rowcount
            print(f"  ✔ Inventory updated   : {upd} rows")

            # Bước 2b: INSERT nếu chưa có row
            cur.execute("""
                INSERT INTO wh_inventory (product_id, warehouse_id, qty, updated_at)
                SELECT sub.product_id, %s, sub.total_return, %s
                FROM (
                    SELECT product_id, SUM(qty_expected) AS total_return
                    FROM wh_return_receipts
                    WHERE status='pending' AND product_id IS NOT NULL
                      AND (pancake_return_status IS NULL OR pancake_return_status != 4)
                    GROUP BY product_id
                ) sub
                WHERE NOT EXISTS (
                    SELECT 1 FROM wh_inventory wi
                    WHERE wi.product_id = sub.product_id AND wi.warehouse_id = %s
                )
            """, (WH_ID, now_str, WH_ID))
            print(f"  ✔ Inventory insert    : {cur.rowcount} rows mới")

            # Bước 2c: movements
            cur.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after, ref_order_id, note, created_at)
                SELECT 'return_received', product_id, %s, SUM(qty_expected), 0, SUM(qty_expected),
                       NULL, %s, %s
                FROM wh_return_receipts
                WHERE status='pending' AND product_id IS NOT NULL
                  AND (pancake_return_status IS NULL OR pancake_return_status != 4)
                GROUP BY product_id
            """, (WH_ID, NOTE_RETURN, now_str))
            print(f"  ✔ Return movements    : {cur.rowcount}")

            # Bước 2d: đổi status → received_ok (BỎ QUA đang hoàn)
            cur.execute("""
                UPDATE wh_return_receipts
                SET status='received_ok',
                    qty_received=qty_expected,
                    received_good_qty=qty_expected,
                    received_damaged_qty=0,
                    missing_qty=0,
                    good_ledger_posted_qty=qty_expected,
                    received_at=%s,
                    note=%s,
                    warehouse_id=COALESCE(warehouse_id,%s)
                WHERE status='pending'
                  AND (pancake_return_status IS NULL OR pancake_return_status != 4)
            """, (now_str, NOTE_RETURN, WH_ID))
            print(f"  ✔ Confirmed           : {cur.rowcount:,}")

        elif can_confirm == 0:
            print("  ℹ️  Không có đơn hoàn nào cần xác nhận — bỏ qua")

        # ════════════════════════════════════════════════════════
        # TỔNG KẾT
        # ════════════════════════════════════════════════════════
        print()
        print("─── TỔNG KẾT SAU KHI CHẠY ───")
        cur.execute("SELECT status, COUNT(*) FROM wh_outbound_requests GROUP BY status ORDER BY status")
        print("  wh_outbound_requests:")
        for r in cur.fetchall():
            print(f"    {r[0]}: {r[1]:,}")

        cur.execute("SELECT status, COUNT(*) FROM wh_return_receipts GROUP BY status ORDER BY status")
        print("  wh_return_receipts:")
        for r in cur.fetchall():
            print(f"    {r[0]}: {r[1]:,}")

        cur.execute("SELECT COUNT(*), COALESCE(SUM(qty),0) FROM wh_inventory WHERE warehouse_id=%s", (WH_ID,))
        r = cur.fetchone()
        print(f"  wh_inventory (kho {WH_ID}): {r[0]} sản phẩm, tổng qty={r[1]:,}")

        if dry_run:
            conn.rollback()
            print(f"\n✅ DRY-RUN xong — DB KHÔNG bị thay đổi.")
            print("   Nếu số liệu trên đúng, chạy lại KHÔNG có --dry-run để ghi thật.\n")
        else:
            conn.commit()
            print("\n✅ HOÀN THÀNH — Đã commit toàn bộ!\n")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ LỖI: {e}")
        print("   Đã rollback — DB không bị thay đổi.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk confirm pending outbound + returns")
    parser.add_argument("--dry-run", action="store_true",
                        help="Chỉ đọc và in kết quả, KHÔNG ghi vào DB")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
