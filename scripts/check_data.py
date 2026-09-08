"""
Kiểm tra số liệu Posbot — chạy độc lập, ra kết quả rõ ràng.

Cách chạy trên VPS:
    cd ~/tieuhiemsoft/posbottieuhiem
    set -a && source deploy/pos-dashboard.env && set +a
    python3 scripts/check_data.py

Cách chạy trên Replit Shell:
    cd posbottieuhiem && python3 scripts/check_data.py
"""
from __future__ import annotations
import json, os, sys, time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

PASS  = "✓ PASS"
FAIL  = "✗ FAIL"
WARN  = "⚠ WARN"

results: list[tuple[str, str, str]] = []  # (status, name, detail)

def check(name: str, ok: bool, detail: str = "", warning: bool = False):
    status = WARN if (not ok and warning) else (PASS if ok else FAIL)
    results.append((status, name, detail))
    icon = status
    print(f"  {icon}  {name}" + (f" — {detail}" if detail else ""))

def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


# ─────────────────────────────────────────────────────────────
# 1. FILE CẤU HÌNH
# ─────────────────────────────────────────────────────────────
section("1. File cấu hình")

# shops.json
shops_file = BASE_DIR / "shops.json"
if shops_file.exists():
    shops = json.loads(shops_file.read_text("utf-8"))
    active = [s for s in shops if s.get("status") == "active"]
    shop_keys = [s.get("shop_key") for s in shops if s.get("shop_key")]
    dup_keys = len(shop_keys) != len(set(shop_keys))
    check("shops.json tồn tại", True)
    check("Có shop active", len(active) >= 1, f"{len(active)} shop active / {len(shops)} tổng")
    check("Không trùng shop_key", not dup_keys, f"{'Có' if dup_keys else 'Không'} trùng")
else:
    check("shops.json tồn tại", False, "File không tồn tại")
    shops, active = [], []

# session.json
session_file = BASE_DIR / "session.json"
if session_file.exists():
    sess = json.loads(session_file.read_text("utf-8"))
    token = str(sess.get("access_token", "") or "").strip()
    cookie = str(sess.get("cookie", "") or "").strip()
    check("session.json tồn tại", True)
    check("access_token hợp lệ", len(token) > 10, f"{len(token)} ký tự")
    check("cookie hợp lệ", len(cookie) > 10, f"{len(cookie)} ký tự")
else:
    check("session.json tồn tại", False, "File không tồn tại")

# DATABASE_URL
db_url = os.environ.get("DATABASE_URL", "")
check("DATABASE_URL được set", bool(db_url.strip()), db_url[:30] + "..." if db_url else "")


# ─────────────────────────────────────────────────────────────
# 2. KẾT NỐI DATABASE
# ─────────────────────────────────────────────────────────────
section("2. Kết nối Database")

try:
    from modules.kho_vat_ly.wh_db import wh_db
    with wh_db() as conn:
        row = conn.execute("SELECT 1 AS ok").fetchone()
    check("Kết nối PostgreSQL", True)
    DB_OK = True
except Exception as e:
    check("Kết nối PostgreSQL", False, str(e)[:80])
    DB_OK = False

if DB_OK:
    # Kiểm tra bảng tồn tại
    required_tables = [
        "wh_outbound_requests", "wh_return_receipts",
        "wh_shops", "wh_inventory", "wh_products",
    ]
    with wh_db() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
        existing = {r["table_name"] for r in rows}
    for t in required_tables:
        check(f"Bảng {t}", t in existing)


# ─────────────────────────────────────────────────────────────
# 3. SỐ LIỆU KHO VẬT LÝ
# ─────────────────────────────────────────────────────────────
section("3. Kho Vật Lý — wh_outbound_requests")

if DB_OK:
    with wh_db() as conn:
        # Tổng đơn
        total_row = conn.execute("SELECT COUNT(*) AS c FROM wh_outbound_requests").fetchone()
        total = int(total_row["c"] or 0)
        check("Có đơn xuất kho (>= 1000)", total >= 1000, f"{total:,} records")

        # Theo status
        status_rows = conn.execute(
            "SELECT pancake_status, COUNT(DISTINCT order_code) AS c "
            "FROM wh_outbound_requests GROUP BY pancake_status ORDER BY c DESC"
        ).fetchall()
        print("\n  📊 Phân bổ theo pancake_status:")
        status_map: dict[str, int] = {}
        valid_statuses = {"waiting", "confirmed", "shipped", "received", "returned", "returning", "cancelled"}
        for r in status_rows:
            s, c = r["pancake_status"], int(r["c"] or 0)
            status_map[s] = c
            marker = "" if s in valid_statuses else " ← KHÔNG HỢP LỆ"
            print(f"     {str(s or 'NULL'):<12} {c:>8,}{marker}")

        shipped   = status_map.get("shipped", 0)
        received  = status_map.get("received", 0)
        returned  = status_map.get("returned", 0)
        waiting   = status_map.get("waiting", 0)

        check("shipped > 0", shipped > 0, f"{shipped:,} đơn đang giao")
        check("received >= shipped", received >= shipped, f"received={received:,} shipped={shipped:,}")
        check("Không có NULL/status lạ", all(s in valid_statuses for s in status_map), "")

        # NULL status
        null_row = conn.execute(
            "SELECT COUNT(*) AS c FROM wh_outbound_requests WHERE pancake_status IS NULL"
        ).fetchone()
        null_count = int(null_row["c"] or 0)
        check("Không có NULL pancake_status", null_count == 0, f"{null_count} dòng NULL")

        # Không âm qty
        neg_row = conn.execute(
            "SELECT COUNT(*) AS c FROM wh_outbound_requests WHERE qty_ordered < 0"
        ).fetchone()
        check("qty_ordered không âm", int(neg_row["c"] or 0) == 0, f"{neg_row['c']} dòng âm")

        # Duplicate
        dup_row = conn.execute(
            """SELECT COUNT(*) AS c FROM (
                SELECT order_id_external, product_sku, COUNT(*) cnt
                FROM wh_outbound_requests
                WHERE order_id_external != ''
                GROUP BY order_id_external, product_sku
                HAVING COUNT(*) > 1
            ) d"""
        ).fetchone()
        dup_count = int(dup_row["c"] or 0)
        check("Không có duplicate (order_id, sku)", dup_count == 0, f"{dup_count:,} cặp trùng")

        # Dữ liệu mới trong 7 ngày
        cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        recent_row = conn.execute(
            "SELECT COUNT(*) AS c FROM wh_outbound_requests WHERE order_inserted_at >= %s",
            [cutoff]
        ).fetchone()
        recent = int(recent_row["c"] or 0)
        check("Có đơn mới 7 ngày qua", recent > 0, f"{recent:,} đơn từ {cutoff}", warning=True)

        # Performance
        t0 = time.time()
        conn.execute(
            "SELECT pancake_status, COUNT(DISTINCT order_code) AS c "
            "FROM wh_outbound_requests GROUP BY pancake_status"
        ).fetchall()
        elapsed = time.time() - t0
        check("Query KPI < 5 giây", elapsed < 5.0, f"{elapsed:.2f}s")


# ─────────────────────────────────────────────────────────────
# 4. SỐ LIỆU ĐƠN TRẢ HÀNG
# ─────────────────────────────────────────────────────────────
section("4. Đơn Trả Hàng — wh_return_receipts")

if DB_OK:
    with wh_db() as conn:
        ret_row = conn.execute("SELECT COUNT(*) AS c FROM wh_return_receipts").fetchone()
        ret_total = int(ret_row["c"] or 0)
        check("Có đơn trả hàng", ret_total > 0, f"{ret_total:,} records")

        neg_ret = conn.execute(
            "SELECT COUNT(*) AS c FROM wh_return_receipts WHERE qty_expected < 0"
        ).fetchone()
        check("qty_expected không âm", int(neg_ret["c"] or 0) == 0)

        dup_ret = conn.execute(
            """SELECT COUNT(*) AS c FROM (
                SELECT order_id_external, product_sku, COUNT(*) cnt
                FROM wh_return_receipts
                WHERE order_id_external != ''
                GROUP BY order_id_external, product_sku
                HAVING COUNT(*) > 1
            ) d"""
        ).fetchone()
        check("Không duplicate returns", int(dup_ret["c"] or 0) == 0, f"{dup_ret['c']} cặp trùng")

        # Sanity: returns < outbound
        out_codes = conn.execute(
            "SELECT COUNT(DISTINCT order_code) AS c FROM wh_outbound_requests"
        ).fetchone()
        ret_codes = conn.execute(
            "SELECT COUNT(DISTINCT order_code) AS c FROM wh_return_receipts"
        ).fetchone()
        check(
            "returns < outbound (hợp lý)",
            int(ret_codes["c"] or 0) <= int(out_codes["c"] or 0),
            f"returns={ret_codes['c']:,} outbound={out_codes['c']:,}"
        )


# ─────────────────────────────────────────────────────────────
# 5. SHOPS CONSISTENCY
# ─────────────────────────────────────────────────────────────
section("5. Shop Consistency")

if DB_OK and active:
    with wh_db() as conn:
        db_shops_row = conn.execute(
            "SELECT COUNT(*) AS c FROM wh_shops WHERE status='active'"
        ).fetchone()
        db_active = int(db_shops_row["c"] or 0)
    diff = abs(db_active - len(active))
    pct = diff / max(len(active), 1) * 100
    check(
        "wh_shops active ≈ shops.json active",
        pct <= 10,
        f"DB={db_active} JSON={len(active)} chênh={pct:.0f}%",
        warning=pct > 10,
    )


# ─────────────────────────────────────────────────────────────
# 6. SCHEDULER
# ─────────────────────────────────────────────────────────────
section("6. Scheduler Config")

scheduler_file = BASE_DIR / "scheduler.py"
if scheduler_file.exists():
    src = scheduler_file.read_text("utf-8")
    check("scheduler.py tồn tại", True)
    check("sync_kho_outbound đã đăng ký", "sync_kho_outbound" in src)
    check("Chạy in-process (không subprocess)", "sync_outbound_for_date_range" in src)
    check("_python_for_subjobs() tồn tại", "_python_for_subjobs" in src)
    check("Interval <= 30 phút", "*/30" in src or "*/15" in src or "*/10" in src,
          "Tìm thấy */30 hoặc tương đương", warning=True)
else:
    check("scheduler.py tồn tại", False)


# ─────────────────────────────────────────────────────────────
# 7. SYNC LOCK
# ─────────────────────────────────────────────────────────────
section("7. Sync Lock")

if DB_OK:
    try:
        from modules.kho_vat_ly.wh_db import SYNC_LOCK
        locked = not SYNC_LOCK.acquire(blocking=False)
        if not locked:
            SYNC_LOCK.release()
        check("SYNC_LOCK không bị giữ", not locked,
              "Lock đang bị giữ — có sync đang chạy?" if locked else "")
    except Exception as e:
        check("SYNC_LOCK import", False, str(e))


# ─────────────────────────────────────────────────────────────
# TÓM TẮT
# ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("  KẾT QUẢ")
print('='*60)

passed  = sum(1 for r in results if r[0] == PASS)
failed  = sum(1 for r in results if r[0] == FAIL)
warned  = sum(1 for r in results if r[0] == WARN)
total_c = len(results)

print(f"  ✓ PASS: {passed}/{total_c}")
if warned:  print(f"  ⚠ WARN: {warned}/{total_c}")
if failed:  print(f"  ✗ FAIL: {failed}/{total_c}")

if failed:
    print("\n  ── Các mục FAIL cần xử lý: ──")
    for status, name, detail in results:
        if status == FAIL:
            print(f"     ✗  {name}" + (f": {detail}" if detail else ""))

print()
sys.exit(0 if failed == 0 else 1)
