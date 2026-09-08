"""
fix_bulk_confirm_duplicates.py — SQL bulk approach (nhanh hơn loop Python)
Chạy: .venv/bin/python3 scripts/fix_bulk_confirm_duplicates.py [--dry-run]
"""
import argparse, os, sys
import psycopg2, psycopg2.extras

DB_URL = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
              "deploy", "pos-dashboard.env")).read()
DB_URL = [x for x in DB_URL.split("\n") if x.startswith("DATABASE_URL=")][0].split("=", 1)[1].strip()


def run(dry_run: bool):
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur  = conn.cursor()

    # ── A: Tìm outbound_confirmed thừa (giữ id nhỏ nhất mỗi cặp) ─────────────
    cur.execute("""
        SELECT id, product_id, warehouse_id, qty, ref_order_id
        FROM wh_stock_movements
        WHERE type = 'outbound_confirmed' AND status = 'active'
          AND ref_order_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM wh_stock_movements
              WHERE type = 'outbound_confirmed' AND status = 'active'
                AND ref_order_id IS NOT NULL
              GROUP BY product_id, ref_order_id
          )
    """)
    extra_oc = cur.fetchall()
    print(f"[A] extra outbound_confirmed: {len(extra_oc)} rows, "
          f"total qty = {sum(r['qty'] or 0 for r in extra_oc)}")

    # ── B: Trong số đó, tìm cái có auto_adjust kèm (qty_before=0) ─────────────
    # Những cái KHÔNG có auto_adjust → cần cộng lại inventory
    # Dùng SQL để phân loại nhanh
    if extra_oc:
        extra_oc_ids = [r["id"] for r in extra_oc]

        # Tìm ids có paired auto_adjust (qty_before=0, cùng product+order+qty)
        cur.execute("""
            SELECT DISTINCT oc.id
            FROM wh_stock_movements oc
            JOIN wh_stock_movements aa
              ON aa.product_id   = oc.product_id
             AND aa.ref_order_id = oc.ref_order_id
             AND aa.qty          = oc.qty
             AND aa.type         = 'admin_auto_adjust'
             AND aa.status       = 'active'
             AND aa.qty_before   = 0
            WHERE oc.id = ANY(%s)
        """, (extra_oc_ids,))
        has_aa_set = {r["id"] for r in cur.fetchall()}

        no_aa = [r for r in extra_oc if r["id"] not in has_aa_set]
        inv_delta: dict[tuple, int] = {}  # (product_id, warehouse_id) → qty to add
        for r in no_aa:
            if r["warehouse_id"] and r["qty"] and r["qty"] > 0:
                key = (r["product_id"], r["warehouse_id"])
                inv_delta[key] = inv_delta.get(key, 0) + r["qty"]

        print(f"  → có auto_adjust: {len(has_aa_set)} (no inv change)")
        print(f"  → KHÔNG có auto_adjust: {len(no_aa)} rows "
              f"→ cộng lại {sum(inv_delta.values())} units")
    else:
        has_aa_set = set()
        inv_delta  = {}

    # ── C: admin_auto_adjust thừa ─────────────────────────────────────────────
    cur.execute("""
        SELECT id FROM wh_stock_movements
        WHERE type = 'admin_auto_adjust' AND status = 'active'
          AND ref_order_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM wh_stock_movements
              WHERE type = 'admin_auto_adjust' AND status = 'active'
                AND ref_order_id IS NOT NULL
              GROUP BY product_id, ref_order_id
          )
    """)
    extra_aa_ids = [r["id"] for r in cur.fetchall()]
    print(f"[C] extra admin_auto_adjust: {len(extra_aa_ids)} rows")

    if dry_run:
        print("\n[DRY-RUN] Không thay đổi DB.")
        conn.close()
        return

    # ── Thực thi ──────────────────────────────────────────────────────────────
    cancel_ids = [r["id"] for r in extra_oc] + extra_aa_ids
    if cancel_ids:
        cur.execute("""
            UPDATE wh_stock_movements
            SET status        = 'cancelled',
                cancelled_at  = now()::text,
                cancelled_by  = 'system',
                cancel_reason = 'bulk_confirm_duplicate_cleanup'
            WHERE id = ANY(%s)
        """, (cancel_ids,))
        print(f"  ✓ Cancelled {cur.rowcount} movements")

    inv_fixed = 0
    for (pid, wh_id), qty in inv_delta.items():
        cur.execute("""
            UPDATE wh_inventory SET qty = qty + %s, updated_at = now()::text
            WHERE product_id = %s AND warehouse_id = %s
        """, (qty, pid, wh_id))
        inv_fixed += qty
    if inv_delta:
        print(f"  ✓ Cộng lại {inv_fixed} units vào wh_inventory ({len(inv_delta)} rows)")

    conn.commit()
    conn.close()
    print(f"\n✅ Xong: huỷ {len(cancel_ids)} movements, +{inv_fixed} units vào kho")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
