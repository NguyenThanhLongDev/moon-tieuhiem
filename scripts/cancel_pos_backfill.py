"""
cancel_pos_backfill.py — Huỷ các inbound movement "Backfill từ POS" (double-count)
Chạy: .venv/bin/python3 scripts/cancel_pos_backfill.py [--dry-run]
"""
import argparse, os
import psycopg2, psycopg2.extras

DB_URL = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
              "deploy", "pos-dashboard.env")).read()
DB_URL = [x for x in DB_URL.split("\n") if x.startswith("DATABASE_URL=")][0].split("=", 1)[1].strip()


def run(dry_run: bool):
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur  = conn.cursor()

    # Tìm tất cả inbound backfill còn active
    cur.execute("""
        SELECT id, product_id, warehouse_id, qty
        FROM wh_stock_movements
        WHERE type = 'inbound' AND status = 'active'
          AND note LIKE '%Backfill từ POS%'
    """)
    rows = cur.fetchall()

    total_movements = len(rows)
    total_qty       = sum(r["qty"] or 0 for r in rows)

    # Tính inventory delta: (product_id, warehouse_id) → qty cần trừ
    inv_delta: dict[tuple, int] = {}
    for r in rows:
        if r["warehouse_id"] and r["qty"] and r["qty"] > 0:
            key = (r["product_id"], r["warehouse_id"])
            inv_delta[key] = inv_delta.get(key, 0) + r["qty"]

    # Tổng KVL trước và sau (ước tính)
    cur.execute("SELECT COALESCE(SUM(qty),0) AS t FROM wh_inventory")
    kvl_before = cur.fetchone()["t"]
    kvl_after  = kvl_before - total_qty

    cur.execute("SELECT COALESCE(SUM(qty_pos),0) AS t FROM wh_shop_inventory")
    pos_total = cur.fetchone()["t"]

    print(f"=== Backfill từ POS — sẽ cancel ===")
    print(f"  Số movements: {total_movements}")
    print(f"  Tổng qty sẽ trừ: {total_qty:,}")
    print(f"  KVL trước: {kvl_before:,}")
    print(f"  KVL sau:   {kvl_after:,}")
    print(f"  POS hiện:  {pos_total:,}")
    print(f"  Gap sau fix: KVL {kvl_after:,} vs POS {pos_total:,} (+{kvl_after - pos_total:,})")
    print(f"  Số inventory rows cần trừ: {len(inv_delta)}")

    if dry_run:
        print("\n[DRY-RUN] Không thay đổi DB.")
        conn.close()
        return

    # Cancel movements
    ids = [r["id"] for r in rows]
    cur.execute("""
        UPDATE wh_stock_movements
        SET status        = 'cancelled',
            cancelled_at  = now()::text,
            cancelled_by  = 'system',
            cancel_reason = 'pos_backfill_double_count_cleanup'
        WHERE id = ANY(%s)
    """, (ids,))
    print(f"\n  ✓ Cancelled {cur.rowcount} movements")

    # Trừ inventory
    inv_fixed = 0
    for (pid, wh_id), qty in inv_delta.items():
        cur.execute("""
            UPDATE wh_inventory SET qty = qty - %s, updated_at = now()::text
            WHERE product_id = %s AND warehouse_id = %s
        """, (qty, pid, wh_id))
        inv_fixed += qty
    print(f"  ✓ Trừ {inv_fixed:,} units khỏi wh_inventory ({len(inv_delta)} rows)")

    conn.commit()
    conn.close()

    print(f"\n✅ Xong: huỷ {len(ids)} movements, trừ {inv_fixed:,} units khỏi kho")
    print(f"   KVL mới ≈ {kvl_after:,} | POS = {pos_total:,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
