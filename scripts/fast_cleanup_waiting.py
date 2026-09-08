#!/usr/bin/env python3
"""Fast cleanup — chiến lược ngược:

1. Fetch danh sách order_id ĐANG ở status 9 (Chờ chuyển hàng) từ MỌI shop trên Pancake
   → Đây là "ground truth" đang waiting THẬT (~720 đơn)
2. Mọi đơn trong DB có pancake_status='waiting' nhưng KHÔNG nằm trong list đó → STALE
   → Bulk UPDATE: pancake_status='cleaned' (1 query, instant)
3. Badge web tự khớp với Pancake.

Chạy:
  cd ~/tieuhiemsoft/posbottieuhiem
  set -a && source deploy/pos-dashboard.env && set +a
  .venv/bin/python3 scripts/fast_cleanup_waiting.py
  .venv/bin/python3 scripts/fast_cleanup_waiting.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Set, Tuple

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_conn  # noqa: E402


def fetch_status_9_orders_for_shop(pos_shop_id: str, api_key: str,
                                    timeout: int = 30) -> Tuple[str, Set[str], str]:
    """Fetch tất cả order_id ở status=9 cho 1 shop. Return (shop_id, order_id_set, error)."""
    url = f"https://pos.pancake.vn/api/v1/shops/{pos_shop_id}/orders/get_orders"
    order_ids: Set[str] = set()
    try:
        for page in range(1, 50):  # tối đa 50 trang × 100 = 5000 đơn/shop
            params = [
                ("api_key", api_key),
                ("page_size", 100),
                ("status", 9),
                ("page", page),
                ("option_sort", "inserted_at_desc"),
            ]
            r = requests.post(url, params=params, json={}, timeout=timeout)
            if r.status_code != 200:
                return pos_shop_id, order_ids, f"HTTP {r.status_code}"
            data = r.json()
            orders = data.get("data") or []
            for o in orders:
                oid = str(o.get("id") or "")
                if oid:
                    order_ids.add(oid)
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages or len(orders) < 100:
                break
        return pos_shop_id, order_ids, ""
    except Exception as e:
        return pos_shop_id, order_ids, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Chỉ in số liệu, không UPDATE DB")
    ap.add_argument("--workers", type=int, default=8,
                    help="Số thread fetch song song (default 8)")
    args = ap.parse_args()

    print(f"=== FAST cleanup waiting orders ===")
    print(f"  workers={args.workers}  dry_run={args.dry_run}")
    print()

    # 1. Lấy danh sách shops + DB rows hiện tại
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pos_shop_id, pos_api_key, shop_name FROM wh_shops "
                "WHERE pos_api_key IS NOT NULL AND pos_api_key <> ''"
            )
            shops = cur.fetchall()
            cur.execute("""
                SELECT shop_name, COUNT(DISTINCT order_code), COUNT(*)
                FROM wh_outbound_requests
                WHERE status='pending' AND pancake_status='waiting'
                GROUP BY shop_name
            """)
            db_counts_by_shop = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            cur.execute("""
                SELECT COUNT(DISTINCT order_code) FROM wh_outbound_requests
                WHERE status='pending' AND pancake_status='waiting'
            """)
            db_total_orders = cur.fetchone()[0]

    print(f"DB hiện tại: {db_total_orders} đơn waiting (qua {len(db_counts_by_shop)} shop)")
    print(f"Fetch status=9 từ {len(shops)} shop trên Pancake...")
    print()

    # 2. Fetch parallel
    t0 = time.time()
    real_waiting_ids_by_shop: dict = {}  # pos_shop_id → set of order_ids
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(fetch_status_9_orders_for_shop, str(s[0]), s[1]): (s[0], s[2])
            for s in shops
        }
        done = 0
        for fut in as_completed(futs):
            pos_id, shop_name = futs[fut]
            try:
                _, order_ids, err = fut.result()
                if err:
                    errors.append(f"{shop_name}({pos_id}): {err}")
                real_waiting_ids_by_shop[str(pos_id)] = order_ids
            except Exception as e:
                errors.append(f"{shop_name}({pos_id}): {e}")
            done += 1
            if done % 10 == 0:
                elapsed = time.time() - t0
                print(f"  [{done}/{len(shops)}] fetched, elapsed {elapsed:.1f}s")

    real_total = sum(len(v) for v in real_waiting_ids_by_shop.values())
    # Set shop nào fetch lỗi → SKIP cleanup (an toàn)
    failed_shop_pids: set = set()
    for err in errors:
        # Format: "shop_name(pos_id): error"
        try:
            pid = err.split("(")[1].split(")")[0]
            failed_shop_pids.add(str(pid))
        except Exception:
            pass
    print()
    print(f"Pancake hiện tại: {real_total} đơn THẬT đang status=9")
    print(f"DB phồng (stale): {db_total_orders - real_total} đơn")
    if errors:
        print(f"Errors {len(errors)} shops sẽ SKIP cleanup:")
        for e in errors:
            print(f"  - {e}")
    print()

    if args.dry_run:
        print("*** DRY-RUN: dừng ở đây, không UPDATE DB ***")
        return 0

    # 3. Bulk UPDATE: mark mọi waiting NOT IN real_ids → 'cleaned'
    # Build list (shop_pos_id, order_id) tuples cho IN clause
    print("Cleanup: UPDATE đơn DB không còn ở status 9 trên Pancake...")

    # Map shop_name → pos_shop_id (để query DB by shop_name)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT shop_name, pos_shop_id FROM wh_shops")
            name_to_pid = {r[0]: str(r[1]) for r in cur.fetchall()}

    total_updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            skipped_failed = 0
            for shop_name, (n_orders, n_rows) in db_counts_by_shop.items():
                pos_id = name_to_pid.get(shop_name)
                if not pos_id:
                    continue
                # AN TOÀN: shop fetch lỗi → skip, không update để tránh xóa nhầm
                if pos_id in failed_shop_pids:
                    skipped_failed += 1
                    print(f"  {shop_name:30s} → SKIP (fetch lỗi)")
                    continue
                real_ids = real_waiting_ids_by_shop.get(pos_id, set())
                if not real_ids:
                    # Pancake không có đơn nào status=9 cho shop này → mọi waiting trong DB đều stale
                    cur.execute("""
                        UPDATE wh_outbound_requests
                           SET pancake_status='received'
                         WHERE shop_name=%s AND status='pending' AND pancake_status='waiting'
                    """, (shop_name,))
                else:
                    real_list = list(real_ids)
                    cur.execute("""
                        UPDATE wh_outbound_requests
                           SET pancake_status='received'
                         WHERE shop_name=%s AND status='pending' AND pancake_status='waiting'
                           AND order_id_external NOT IN %s
                    """, (shop_name, tuple(real_list)))
                rows_affected = cur.rowcount
                total_updated += rows_affected
                if rows_affected > 0:
                    print(f"  {shop_name:30s} → cleaned {rows_affected} stale rows "
                          f"(DB had {n_orders} orders, Pancake real={len(real_ids)})")
            conn.commit()

    print()
    print(f"=== Hoàn tất sau {time.time()-t0:.1f}s ===")
    print(f"Tổng rows updated: {total_updated}")

    # Verify
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT order_code) FROM wh_outbound_requests
                WHERE status='pending' AND pancake_status='waiting'
            """)
            new_count = cur.fetchone()[0]
    print(f"DB sau cleanup: {new_count} đơn waiting (Pancake: {real_total})")
    print(f"Check: https://tieuhiem.com/kho-vat-ly/outbound — badge 'Chờ chuyển hàng'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
