"""Quét tất cả wh_products có sku LIKE 'POS-uuid', gọi Pancake API tìm
mã thật (display_id), UPDATE in-place. Không động tồn kho/lịch sử/khoá ngoại
(chỉ đổi cột wh_products.sku).

Usage:
  python3 scripts/cleanup_pos_uuid_skus.py             # dry-run (xem trước)
  python3 scripts/cleanup_pos_uuid_skus.py --apply     # apply UPDATE thật
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from modules.kho_vat_ly.wh_sync_pos import _get_sku  # logic mới đã sửa


def get_db_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    # Đọc từ env của process gunicorn đang chạy
    import subprocess
    try:
        pid = subprocess.check_output(
            ["pgrep", "-f", "gunicorn.*web_app:app"]
        ).decode().split()[0]
        for kv in open(f"/proc/{pid}/environ", "rb").read().split(b"\0"):
            if kv.startswith(b"DATABASE_URL="):
                return kv.decode().split("=", 1)[1]
    except Exception:
        pass
    raise RuntimeError("Không tìm thấy DATABASE_URL (env hoặc gunicorn process)")


def fetch_var_from_pancake(pos_shop_id: str, api_key: str, target_var_id: str,
                           cache: dict) -> tuple[dict, dict] | None:
    """Quét tối đa 50 trang × 200 sp của shop để tìm variation cụ thể.
    Cache theo pos_shop_id để 1 shop chỉ fetch 1 lần / chạy script.
    """
    if pos_shop_id in cache:
        return cache[pos_shop_id].get(target_var_id)

    var_index: dict[str, tuple[dict, dict]] = {}
    for page in range(1, 51):
        try:
            r = requests.get(
                f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/products",
                params={"api_key": api_key, "page": page, "limit": 200},
                timeout=30,
            )
            data = r.json().get("data") or []
        except Exception as e:
            print(f"    [warn] shop {pos_shop_id} page {page}: {e}")
            break
        if not data:
            break
        for prod in data:
            for var in prod.get("variations") or []:
                vid = str(var.get("id") or "")
                if vid:
                    var_index[vid] = (prod, var)
        time.sleep(0.2)
    cache[pos_shop_id] = var_index
    return var_index.get(target_var_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Apply UPDATE thật")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số sản phẩm xử lý (debug)")
    args = ap.parse_args()

    conn = psycopg2.connect(get_db_url())
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, sku, name, pos_variation_id, pos_product_id
            FROM wh_products
            WHERE sku LIKE 'POS-%'
            ORDER BY id
        """)
        bad_products = cur.fetchall()
    print(f"Tìm thấy {len(bad_products)} sản phẩm SKU 'POS-uuid'")
    if args.limit:
        bad_products = bad_products[: args.limit]
        print(f"--limit {args.limit} → xử lý {len(bad_products)} sp đầu tiên")

    pancake_cache: dict[str, dict] = {}
    plan_update: list[tuple[int, str, str, str]] = []   # (id, old_sku, new_sku, name)
    plan_skip:   list[tuple[int, str, str, str]] = []   # (id, old_sku, name, reason)

    for prod_id, old_sku, name, pos_var_id, pos_prod_id in bad_products:
        # Tìm 1 shop bán sp này có api_key
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.pos_shop_id, s.pos_api_key, s.shop_name, si.qty_pos
                FROM wh_shop_inventory si
                JOIN wh_shops s ON s.id = si.shop_id
                WHERE si.product_id=%s AND s.pos_api_key != ''
                ORDER BY si.qty_pos DESC, s.id
                LIMIT 1
            """, (prod_id,))
            shop_row = cur.fetchone()

        if not shop_row:
            plan_skip.append((prod_id, old_sku, name, "Không shop nào có api_key"))
            continue
        pos_shop_id, api_key, shop_name, qty_pos = shop_row

        result = fetch_var_from_pancake(str(pos_shop_id), api_key, pos_var_id, pancake_cache)
        if not result:
            plan_skip.append((prod_id, old_sku, name, f"Không tìm trên Pancake (shop {shop_name})"))
            continue
        prod_data, var_data = result
        new_sku = _get_sku(var_data, prod_data)

        if new_sku == old_sku:
            plan_skip.append((prod_id, old_sku, name, "_get_sku mới vẫn trả 'POS-uuid' (Pancake thật sự không có mã)"))
            continue
        if new_sku.startswith("POS-"):
            plan_skip.append((prod_id, old_sku, name, f"Logic mới vẫn fallback → {new_sku}"))
            continue

        # SKU quá generic (1 ký tự, hoặc số đơn thuần ngắn) → bỏ qua, dễ trùng
        if len(new_sku) < 2 or (new_sku.isdigit() and len(new_sku) <= 2):
            plan_skip.append((prod_id, old_sku, name,
                              f"SKU mới '{new_sku}' quá generic, sẽ trùng nhiều sp → giữ POS-uuid"))
            continue

        # Check trùng với SKU đã tồn tại trong DB
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM wh_products WHERE UPPER(sku)=UPPER(%s) AND id != %s LIMIT 1",
                        (new_sku, prod_id))
            dup = cur.fetchone()
        if dup:
            plan_skip.append((prod_id, old_sku, name,
                              f"Trùng SKU '{new_sku}' với product id={dup[0]} '{dup[1]}' → cần merge thủ công"))
            continue

        plan_update.append((prod_id, old_sku, new_sku, name))

    # Detect intra-batch collision: nhiều sp khác nhau cùng map ra 1 new_sku
    sku_groups: dict[str, list] = {}
    for entry in plan_update:
        sku_groups.setdefault(entry[2].upper(), []).append(entry)
    safe_updates = []
    for sku, entries in sku_groups.items():
        if len(entries) == 1:
            safe_updates.append(entries[0])
        else:
            # Trùng trong batch → tất cả move sang skip
            ids = ", ".join(f"id={e[0]}({e[3][:30]})" for e in entries)
            for e in entries:
                plan_skip.append((e[0], e[1], e[3],
                                  f"Cùng new_sku '{sku}' với {len(entries)-1} sp khác: {ids} → cần merge thủ công"))
    plan_update = safe_updates

    print()
    print("=" * 100)
    print(f"SẼ UPDATE: {len(plan_update)} sản phẩm")
    print("=" * 100)
    for pid, old, new, name in plan_update:
        print(f"  id={pid:5d}  '{old[:48]}' → '{new}'  ({name})")
    print()
    print("=" * 100)
    print(f"BỎ QUA: {len(plan_skip)} sản phẩm")
    print("=" * 100)
    for pid, old, name, reason in plan_skip:
        print(f"  id={pid:5d}  '{old[:48]}'  ({name[:40]})")
        print(f"          → {reason}")

    if not args.apply:
        print()
        print(f"\n[DRY-RUN] Chưa update. Chạy lại với --apply để thực hiện {len(plan_update)} UPDATE.")
        return

    if not plan_update:
        print("\nKhông có gì để update.")
        return

    print(f"\n[APPLY] Bắt đầu UPDATE {len(plan_update)} rows...")
    with conn.cursor() as cur:
        for pid, old, new, name in plan_update:
            cur.execute("UPDATE wh_products SET sku=%s WHERE id=%s", (new, pid))
        conn.commit()
    print(f"✓ Xong. Đã đổi SKU cho {len(plan_update)} sản phẩm.")
    print("→ Refresh trang /kho-vat-ly/san-pham để xem.")


if __name__ == "__main__":
    main()
