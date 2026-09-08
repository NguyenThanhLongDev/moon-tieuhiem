"""
Script migrate dữ liệu từ SQLite (posbot/warehouse.db) sang PostgreSQL (wh_* tables).
Chạy 1 lần: python3 migrate_sqlite_to_pg.py
"""
from __future__ import annotations

import os
import sys
import sqlite3

# Thêm thư mục posbottieuhiem vào path để import db
sys.path.insert(0, os.path.dirname(__file__))

from modules.kho_vat_ly.wh_db import init_wh_tables

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "posbot", "warehouse.db")


def get_sqlite_conn():
    if not os.path.exists(DB_PATH):
        print(f"❌ Không tìm thấy SQLite DB: {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_table(sqlite_conn, pg_conn, table: str, pg_table: str,
                  columns: list[str], insert_sql: str, value_fn=None) -> int:
    cur = sqlite_conn.cursor()
    cur.execute(f"SELECT {', '.join(columns)} FROM {table}")
    rows = cur.fetchall()
    if not rows:
        print(f"  • {table}: 0 bản ghi")
        return 0

    pg_cur = pg_conn.cursor()
    count = 0
    for row in rows:
        values = value_fn(row) if value_fn else tuple(row[c] for c in columns)
        try:
            pg_cur.execute(insert_sql, values)
            count += 1
        except Exception as e:
            print(f"    ⚠️ Bỏ qua hàng {dict(row)}: {e}")
            pg_conn.rollback()
    pg_conn.commit()
    print(f"  ✅ {table} → {pg_table}: {count}/{len(rows)} bản ghi")
    return count


def run_migration():
    print("=" * 60)
    print("MIGRATE SQLite (posbot) → PostgreSQL (wh_* tables)")
    print("=" * 60)

    print("\n[1] Khởi tạo bảng PostgreSQL...")
    init_wh_tables()
    print("  ✅ Bảng đã tạo.")

    sqlite_conn = get_sqlite_conn()

    # Lấy pg_conn trực tiếp
    from db import get_conn as _get_pg_conn
    import psycopg2.extras

    print("\n[2] Kiểm tra SQLite tables...")
    cur = sqlite_conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print(f"  Tables: {tables}")

    print("\n[3] Bắt đầu migrate...")

    with _get_pg_conn() as pg_conn:

        # ── PRODUCTS ──────────────────────────────────────────
        if "products" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_products")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_products đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("SELECT id, sku, name, unit, category, pos_product_id, pos_variation_id, created_at FROM products")
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_products (id, sku, name, unit, category, pos_product_id, pos_variation_id, created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(sku) DO NOTHING
                        """, (r["id"], r["sku"], r["name"], r["unit"] or "cái",
                              r["category"] or "", r["pos_product_id"] or "",
                              r["pos_variation_id"] or "", r["created_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ Sản phẩm {r['sku']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                # Sync sequence
                pg_cur.execute("SELECT setval('wh_products_id_seq', (SELECT MAX(id) FROM wh_products))")
                pg_conn.commit()
                print(f"  ✅ products → wh_products: {len(rows)} bản ghi")

        # ── SHOPS ─────────────────────────────────────────────
        if "shops" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_shops")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_shops đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("SELECT id, shop_key, shop_name, pos_shop_id, pos_api_key, team_id, status FROM shops")
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_shops (id, shop_key, shop_name, pos_shop_id, pos_api_key, team_id, status)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(shop_key) DO NOTHING
                        """, (r["id"], r["shop_key"], r["shop_name"], r["pos_shop_id"],
                              r["pos_api_key"] or "", r["team_id"] or "", r["status"] or "active"))
                    except Exception as e:
                        print(f"    ⚠️ Shop {r['shop_key']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_shops_id_seq', (SELECT MAX(id) FROM wh_shops))")
                pg_conn.commit()
                print(f"  ✅ shops → wh_shops: {len(rows)} bản ghi")

        # ── INVENTORY ─────────────────────────────────────────
        if "inventory" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_inventory")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_inventory đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("SELECT id, product_id, qty, updated_at FROM inventory")
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_inventory (id, product_id, qty, updated_at)
                            VALUES (%s,%s,%s,%s)
                        """, (r["id"], r["product_id"], r["qty"] or 0, r["updated_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ Inventory product_id={r['product_id']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_inventory_id_seq', (SELECT MAX(id) FROM wh_inventory))")
                pg_conn.commit()
                print(f"  ✅ inventory → wh_inventory: {len(rows)} bản ghi")

        # ── SHOP_INVENTORY ────────────────────────────────────
        if "shop_inventory" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_shop_inventory")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_shop_inventory đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("SELECT id, shop_id, product_id, qty_pos, synced_at FROM shop_inventory")
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_shop_inventory (id, shop_id, product_id, qty_pos, synced_at)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT(shop_id, product_id) DO UPDATE SET
                                qty_pos=EXCLUDED.qty_pos, synced_at=EXCLUDED.synced_at
                        """, (r["id"], r["shop_id"], r["product_id"], r["qty_pos"] or 0, r["synced_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ ShopInv shop={r['shop_id']} prod={r['product_id']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_shop_inventory_id_seq', (SELECT MAX(id) FROM wh_shop_inventory))")
                pg_conn.commit()
                print(f"  ✅ shop_inventory → wh_shop_inventory: {len(rows)} bản ghi")

        # ── STOCK_MOVEMENTS ───────────────────────────────────
        if "stock_movements" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_stock_movements")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_stock_movements đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("""
                    SELECT id, type, product_id, shop_id, qty, qty_before, qty_after,
                           ref_order_id, note, created_by, created_at
                    FROM stock_movements
                """)
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_stock_movements
                              (id, type, product_id, shop_id, qty, qty_before, qty_after,
                               ref_order_id, note, created_by, created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (r["id"], r["type"], r["product_id"], r["shop_id"],
                              r["qty"] or 0, r["qty_before"] or 0, r["qty_after"] or 0,
                              r["ref_order_id"] or "", r["note"] or "",
                              r["created_by"] or "system", r["created_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ Movement id={r['id']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_stock_movements_id_seq', (SELECT MAX(id) FROM wh_stock_movements))")
                pg_conn.commit()
                print(f"  ✅ stock_movements → wh_stock_movements: {len(rows)} bản ghi")

        # ── OUTBOUND_REQUESTS ─────────────────────────────────
        if "outbound_requests" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_outbound_requests")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_outbound_requests đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("""
                    SELECT id, order_code, order_id_external, shop_id, shop_name,
                           product_id, product_sku, product_name,
                           qty_ordered, qty_confirmed, carrier_name, tracking_code,
                           carrier_picked_up_at, status, pancake_status, note, created_at
                    FROM outbound_requests
                """)
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_outbound_requests
                              (id, order_code, order_id_external, shop_id, shop_name,
                               product_id, product_sku, product_name,
                               qty_ordered, qty_confirmed, carrier_name, tracking_code,
                               carrier_picked_up_at, status, pancake_status, note, created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (r["id"], r["order_code"], r["order_id_external"] or "",
                              r["shop_id"], r["shop_name"] or "",
                              r["product_id"], r["product_sku"] or "", r["product_name"] or "",
                              r["qty_ordered"] or 0, r["qty_confirmed"] or 0,
                              r["carrier_name"] or "", r["tracking_code"] or "",
                              r["carrier_picked_up_at"] or "", r["status"] or "pending",
                              r["pancake_status"] or "shipped", r["note"] or "", r["created_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ Outbound id={r['id']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_outbound_requests_id_seq', (SELECT MAX(id) FROM wh_outbound_requests))")
                pg_conn.commit()
                print(f"  ✅ outbound_requests → wh_outbound_requests: {len(rows)} bản ghi")

        # ── RETURN_RECEIPTS ───────────────────────────────────
        if "return_receipts" in tables:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM wh_return_receipts")
            existing = pg_cur.fetchone()[0]
            if existing > 0:
                print(f"  ⏭  wh_return_receipts đã có {existing} bản ghi, bỏ qua.")
            else:
                cur.execute("""
                    SELECT id, order_code, shop_id, shop_name,
                           product_id, product_sku, product_name,
                           qty_expected, qty_received, return_reason,
                           carrier_name, tracking_code, order_id_external,
                           status, received_at, note, created_at, returned_at, source,
                           received_good_qty, received_damaged_qty, missing_qty,
                           good_ledger_posted_qty, signaled_at
                    FROM return_receipts
                """)
                rows = cur.fetchall()
                for r in rows:
                    try:
                        pg_cur.execute("""
                            INSERT INTO wh_return_receipts
                              (id, order_code, shop_id, shop_name,
                               product_id, product_sku, product_name,
                               qty_expected, qty_received, return_reason,
                               carrier_name, tracking_code, order_id_external,
                               status, received_at, note, created_at, returned_at, source,
                               received_good_qty, received_damaged_qty, missing_qty,
                               good_ledger_posted_qty, signaled_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (r["id"], r["order_code"], r["shop_id"], r["shop_name"] or "",
                              r["product_id"], r["product_sku"] or "", r["product_name"] or "",
                              r["qty_expected"] or 0, r["qty_received"] or 0, r["return_reason"] or "",
                              r["carrier_name"] or "", r["tracking_code"] or "", r["order_id_external"] or "",
                              r["status"] or "pending", r["received_at"] or "", r["note"] or "",
                              r["created_at"] or "", r["returned_at"] or "", r["source"] or "manual",
                              r["received_good_qty"] or 0, r["received_damaged_qty"] or 0,
                              r["missing_qty"] or 0, r["good_ledger_posted_qty"] or 0,
                              r["signaled_at"] or ""))
                    except Exception as e:
                        print(f"    ⚠️ ReturnReceipt id={r['id']}: {e}")
                        pg_conn.rollback()
                pg_conn.commit()
                pg_cur.execute("SELECT setval('wh_return_receipts_id_seq', (SELECT MAX(id) FROM wh_return_receipts))")
                pg_conn.commit()
                print(f"  ✅ return_receipts → wh_return_receipts: {len(rows)} bản ghi")

    print("\n✅ Migrate hoàn tất!")
    sqlite_conn.close()


if __name__ == "__main__":
    run_migration()
