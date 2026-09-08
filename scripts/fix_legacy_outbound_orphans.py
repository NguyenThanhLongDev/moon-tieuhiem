"""Fix wh_outbound_requests rows legacy: product_sku lưu UUID thay vì SKU thật,
pos_variation_id rỗng. Chỉ xử lý ORPHAN (không có twin row) để tránh UNIQUE collision.

Trước:  product_sku='42136364-a260-...'  pos_variation_id=''  product_id=NULL
Sau:    product_sku='NAM0008'             pos_variation_id='42136364-...'  product_id=529

Usage:
  python3 scripts/fix_legacy_outbound_orphans.py            # dry-run
  python3 scripts/fix_legacy_outbound_orphans.py --apply    # apply
"""
from __future__ import annotations

import argparse
import os
import sys
import subprocess

import psycopg2

UUID_REGEX = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


def get_db_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    pid = subprocess.check_output(["pgrep", "-f", "gunicorn.*web_app:app"]).decode().split()[0]
    for kv in open(f"/proc/{pid}/environ", "rb").read().split(b"\0"):
        if kv.startswith(b"DATABASE_URL="):
            return kv.decode().split("=", 1)[1]
    raise RuntimeError("DATABASE_URL not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()

    conn = psycopg2.connect(get_db_url())
    conn.autocommit = False

    # Đếm tổng + xác định orphan
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FROM wh_outbound_requests legacy
            WHERE legacy.product_sku ~ %s
              AND (legacy.pos_variation_id IS NULL OR legacy.pos_variation_id = '')
              AND NOT EXISTS (
                  SELECT 1 FROM wh_outbound_requests other
                  WHERE other.order_id_external = legacy.order_id_external
                    AND other.pos_variation_id = legacy.product_sku
                    AND other.id != legacy.id
              )
        """, (UUID_REGEX,))
        total_orphan = cur.fetchone()[0]
    print(f"Tổng ORPHAN legacy rows cần fix: {total_orphan:,}")
    if args.limit:
        print(f"--limit {args.limit} áp dụng → xử lý {min(args.limit, total_orphan):,} rows")

    # Stats trước fix
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE EXISTS(
                    SELECT 1 FROM wh_variation_map vm WHERE vm.pos_variation_id = legacy.product_sku
                )) AS can_match,
                COUNT(*) FILTER (WHERE NOT EXISTS(
                    SELECT 1 FROM wh_variation_map vm WHERE vm.pos_variation_id = legacy.product_sku
                )) AS cannot_match
            FROM wh_outbound_requests legacy
            WHERE legacy.product_sku ~ %s
              AND (legacy.pos_variation_id IS NULL OR legacy.pos_variation_id = '')
              AND NOT EXISTS (
                  SELECT 1 FROM wh_outbound_requests other
                  WHERE other.order_id_external = legacy.order_id_external
                    AND other.pos_variation_id = legacy.product_sku
                    AND other.id != legacy.id
              )
        """, (UUID_REGEX,))
        can_match, cannot_match = cur.fetchone()
    print(f"  Match được product (qua wh_variation_map): {can_match:,}")
    print(f"  Không match được:                          {cannot_match:,} (chỉ move UUID, để product_sku trống)")

    # Sample 5 cái để verify
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT legacy.id, legacy.order_code, legacy.product_sku AS uuid_sku,
                   legacy.product_name, p.id AS new_pid, p.sku AS new_sku
            FROM wh_outbound_requests legacy
            LEFT JOIN wh_variation_map vm ON vm.pos_variation_id = legacy.product_sku
            LEFT JOIN wh_products p ON p.id = vm.product_id
            WHERE legacy.product_sku ~ %s
              AND (legacy.pos_variation_id IS NULL OR legacy.pos_variation_id = '')
              AND NOT EXISTS (
                  SELECT 1 FROM wh_outbound_requests other
                  WHERE other.order_id_external = legacy.order_id_external
                    AND other.pos_variation_id = legacy.product_sku
                    AND other.id != legacy.id
              )
            ORDER BY legacy.id LIMIT 5
        """, (UUID_REGEX,))
        samples = cur.fetchall()
    print("\nSample 5 rows sẽ fix:")
    for s in samples:
        print(f"  outbound_id={s[0]}  order={s[1]}")
        print(f"    BEFORE: product_sku='{s[2][:20]}...' (UUID), pos_variation_id='', product_id=NULL")
        if s[4]:
            print(f"    AFTER:  product_sku='{s[5]}', pos_variation_id='{s[2][:20]}...', product_id={s[4]}")
        else:
            print(f"    AFTER:  product_sku='', pos_variation_id='{s[2][:20]}...', product_id=NULL (ko match được)")

    if not args.apply:
        print(f"\n[DRY-RUN] Chưa apply. Chạy lại với --apply để update {total_orphan:,} rows.")
        return

    if total_orphan == 0:
        print("\nKhông có gì để fix.")
        return

    print(f"\n[APPLY] Bắt đầu fix theo batch {args.batch}...")
    fixed_match = 0
    fixed_unmatched = 0
    skipped = 0
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT legacy.id, legacy.product_sku AS uuid_sku,
                       p.id AS new_pid, p.sku AS new_sku, p.name AS new_name
                FROM wh_outbound_requests legacy
                LEFT JOIN wh_variation_map vm ON vm.pos_variation_id = legacy.product_sku
                LEFT JOIN wh_products p ON p.id = vm.product_id
                WHERE legacy.product_sku ~ %s
                  AND (legacy.pos_variation_id IS NULL OR legacy.pos_variation_id = '')
                  AND legacy.id > %s
                  AND NOT EXISTS (
                      SELECT 1 FROM wh_outbound_requests other
                      WHERE other.order_id_external = legacy.order_id_external
                        AND other.pos_variation_id = legacy.product_sku
                        AND other.id != legacy.id
                  )
                ORDER BY legacy.id
                LIMIT %s
            """, (UUID_REGEX, last_id, args.batch))
            rows = cur.fetchall()
        if not rows:
            break
        if args.limit and (fixed_match + fixed_unmatched + skipped) >= args.limit:
            break

        for row_id, uuid_sku, new_pid, new_sku, new_name in rows:
            try:
                with conn.cursor() as cur:
                    if new_pid:
                        cur.execute("""
                            UPDATE wh_outbound_requests
                            SET pos_variation_id=%s, product_id=%s, product_sku=%s,
                                product_name=COALESCE(NULLIF(product_name,''), %s)
                            WHERE id=%s
                        """, (uuid_sku, new_pid, new_sku, new_name, row_id))
                        fixed_match += 1
                    else:
                        cur.execute("""
                            UPDATE wh_outbound_requests
                            SET pos_variation_id=%s, product_sku=''
                            WHERE id=%s
                        """, (uuid_sku, row_id))
                        fixed_unmatched += 1
                last_id = row_id
            except psycopg2.errors.UniqueViolation as e:
                conn.rollback()
                skipped += 1
                last_id = row_id
                print(f"  [skip] id={row_id}: UNIQUE collision — {e}")
            except Exception as e:
                conn.rollback()
                skipped += 1
                last_id = row_id
                print(f"  [skip] id={row_id}: {e}")
        conn.commit()
        total_done = fixed_match + fixed_unmatched + skipped
        print(f"  ...progress: matched={fixed_match}  unmatched={fixed_unmatched}  skipped={skipped}  (last id={last_id})")

    print(f"\n✓ Hoàn tất:")
    print(f"  Matched (gán product_id+sku đúng):   {fixed_match:,}")
    print(f"  Unmatched (chỉ move UUID):           {fixed_unmatched:,}")
    print(f"  Skipped (collision/error):           {skipped:,}")


if __name__ == "__main__":
    main()
