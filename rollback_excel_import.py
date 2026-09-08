#!/usr/bin/env python3
"""
Script hoàn tác (rollback) các phiếu nhập kho từ Excel.
Chạy trên VPS trong thư mục posbottieuhiem:
    python3 /tmp/rollback_excel_import.py --dry-run
    python3 /tmp/rollback_excel_import.py --confirm
"""
import os, sys, argparse
import psycopg2, psycopg2.extras

DB_URL = os.getenv("DATABASE_URL", "")
if not DB_URL:
    # Thử đọc từ .env hoặc config nếu không có env var
    try:
        import importlib.util, pathlib
        env_file = pathlib.Path(__file__).parent / "posbottieuhiem" / ".env"
        if not env_file.exists():
            env_file = pathlib.Path(".env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    DB_URL = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass

if not DB_URL:
    print("❌  Không tìm thấy DATABASE_URL. Export biến môi trường trước:")
    print("    export DATABASE_URL='postgresql://...'")
    sys.exit(1)

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true", help="Chỉ xem, không thay đổi gì")
parser.add_argument("--confirm", action="store_true", help="Thực sự rollback")
parser.add_argument("--note", default="Nhập từ Excel", help="Nội dung ghi chú cần rollback (mặc định: 'Nhập từ Excel')")
parser.add_argument("--from-time", default=None, help="Lọc từ thời gian, VD: '2026-04-18 00:00'")
parser.add_argument("--to-time", default=None, help="Lọc đến thời gian, VD: '2026-04-19 00:00'")
args = parser.parse_args()

if not args.dry_run and not args.confirm:
    print("Dùng --dry-run để xem trước, hoặc --confirm để thực sự rollback.")
    sys.exit(1)

conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
conn.autocommit = False
cur = conn.cursor()

# Tìm các movement cần rollback
query = """
    SELECT m.id, p.sku, p.name, m.qty, m.qty_before, m.qty_after,
           m.warehouse_id, m.note, m.created_at
    FROM wh_stock_movements m
    JOIN wh_products p ON p.id = m.product_id
    WHERE m.type = 'inbound'
      AND m.note ILIKE %s
      AND (m.status IS NULL OR m.status != 'cancelled')
"""
params = [f"%{args.note}%"]
if args.from_time:
    query += " AND m.created_at >= %s"; params.append(args.from_time)
if args.to_time:
    query += " AND m.created_at <= %s"; params.append(args.to_time)
query += " ORDER BY m.created_at DESC"

cur.execute(query, params)
rows = cur.fetchall()

if not rows:
    print(f"⚠️  Không tìm thấy phiếu nhập nào với ghi chú chứa '{args.note}'.")
    print("Thử chỉ định --note với nội dung khác, hoặc xem danh sách ghi chú:")
    cur.execute("SELECT DISTINCT note, count(*), max(created_at) FROM wh_stock_movements WHERE type='inbound' GROUP BY note ORDER BY max(created_at) DESC LIMIT 20")
    for r in cur.fetchall():
        print(f"   note='{r['note']}' | {r['count']} records | last={r['max']}")
    conn.close(); sys.exit(0)

print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Tìm thấy {len(rows)} phiếu nhập cần rollback:\n")
print(f"{'ID':>8} | {'SKU':<16} | {'Tên':<35} | {'SL':>6} | {'Kho':>6} | Thời gian")
print("-"*95)
total_products = 0
for r in rows:
    print(f"{r['id']:>8} | {(r['sku'] or ''):16} | {(r['name'] or '')[:35]:<35} | {r['qty']:>6} | {str(r['warehouse_id'] or ''):>6} | {r['created_at']}")
    total_products += 1
print(f"\nTổng: {total_products} dòng | Tổng SL sẽ trừ: {sum(r['qty'] for r in rows)}")

if args.dry_run:
    print("\n✅  Dry run xong. Dùng --confirm để thực sự rollback.")
    conn.close(); sys.exit(0)

# Thực hiện rollback
print("\n⚡ Đang rollback...")
ok = 0; failed = 0
for r in rows:
    try:
        mid = r["id"]
        qty = r["qty"]
        pid = None
        # Lấy product_id từ movement
        cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur2.execute("SELECT product_id FROM wh_stock_movements WHERE id=%s", (mid,))
        mv = cur2.fetchone()
        if not mv:
            print(f"  ⚠️  Movement {mid} không còn tồn tại, bỏ qua.")
            failed += 1; continue
        pid = mv["product_id"]
        wh_id = r["warehouse_id"]

        # Trừ lại tồn kho
        if wh_id:
            cur2.execute(
                "UPDATE wh_inventory SET qty = GREATEST(0, qty - %s) WHERE product_id=%s AND warehouse_id=%s",
                (qty, pid, wh_id)
            )
        else:
            cur2.execute(
                "UPDATE wh_inventory SET qty = GREATEST(0, qty - %s) WHERE product_id=%s",
                (qty, pid)
            )

        # Đánh dấu movement là đã huỷ
        cur2.execute(
            "UPDATE wh_stock_movements SET status='cancelled', cancel_reason='Rollback Excel import' WHERE id=%s",
            (mid,)
        )
        ok += 1
        print(f"  ✅  [{mid}] {r['sku']} | trừ {qty} | kho {wh_id}")
    except Exception as e:
        print(f"  ❌  [{r['id']}] Lỗi: {e}")
        failed += 1

conn.commit()
conn.close()
print(f"\n🎉  Rollback xong: {ok} thành công, {failed} lỗi.")
