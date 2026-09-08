#!/usr/bin/env python3
"""Reverse-sync pancake_status — cập nhật NGƯỢC trạng thái POS cho kho vật lý.

VẤN ĐỀ (CLAUDE.md §13.9): kho polling 1 chiều — chỉ kéo đơn VÀO, không biết
khi đơn rời trạng thái. Đơn cũ kẹt pancake_status='shipped'/'waiting' dù
Pancake thực tế đã 'received'/'returned' → dashboard list sai.

GIẢI PHÁP: với mỗi shop, fetch danh sách order_id ở status 3 (đã nhận) +
status 5 (đã hoàn) trong cửa sổ ngày → UPDATE pancake_status của đơn đó trong
DB cho khớp. CHỈ động cột `pancake_status`.

⚠ RANH GIỚI AN TOÀN (BẤT DI BẤT DỊCH — §14):
- CHỈ UPDATE cột `pancake_status`.
- TUYỆT ĐỐI KHÔNG động: status (kho), pre_confirmed_by, confirmed_at,
  carrier_picked_up_at, hay bất kỳ cột nào khác.
- Comparison card "ĐVVC lấy hàng" dùng carrier_picked_up_at + status →
  KHÔNG bị ảnh hưởng.
- Shop fetch lỗi → SKIP (không update để tránh sai).

Map status int → pancake_status:
  3 (Đã nhận)   → 'received'
  5 (Đã hoàn)   → 'returned'
  4 (Đang hoàn) → 'returning'

Chạy:
  cd ~/tieuhiemsoft/posbottieuhiem
  set -a && source deploy/pos-dashboard.env && set +a
  .venv/bin/python3 scripts/sync_pancake_status_reverse.py --dry-run --days-back 90
  .venv/bin/python3 scripts/sync_pancake_status_reverse.py --days-back 90
  .venv/bin/python3 scripts/sync_pancake_status_reverse.py --shop-id 1022007861 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Set, Tuple, List

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_conn  # noqa: E402

# status int Pancake → pancake_status DB (CHỈ các terminal/late status)
STATUS_MAP = {3: "received", 5: "returned", 4: "returning"}


def _utc_range(days_back: int) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    return (start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            now.strftime("%Y-%m-%dT%H:%M:%S.999Z"))


def fetch_status_orders(pos_shop_id: str, api_key: str, status_int: int,
                        start_iso: str, end_iso: str,
                        timeout: int = 30) -> Tuple[Set[str], str]:
    """Fetch tất cả order_id ở 1 status trong cửa sổ ngày. Return (id_set, error)."""
    url = f"https://pos.pancake.vn/api/v1/shops/{pos_shop_id}/orders/get_orders"
    ids: Set[str] = set()
    try:
        for page in range(1, 80):  # tối đa 80×100 = 8000 đơn/status/shop
            params = [
                ("api_key", api_key), ("page_size", 100), ("page", page),
                ("status", status_int), ("updateStatus", "inserted_at"),
                ("option_sort", "inserted_at_desc"),
                ("timeRange[]", start_iso), ("timeRange[]", end_iso),
            ]
            r = requests.post(url, params=params, json={}, timeout=timeout)
            if r.status_code != 200:
                return ids, f"HTTP {r.status_code}"
            data = r.json()
            orders = data.get("data") or []
            for o in orders:
                oid = str(o.get("id") or "")
                if oid:
                    ids.add(oid)
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages or len(orders) < 100:
                break
        return ids, ""
    except Exception as e:
        return ids, str(e)


def process_shop(pos_shop_id: str, api_key: str, shop_name: str,
                 start_iso: str, end_iso: str) -> Dict:
    """Fetch 3 status + tính diff vs DB. Return dict kết quả (chưa update)."""
    result = {"shop_name": shop_name, "pos_shop_id": pos_shop_id,
              "error": "", "updates": {}}  # updates: {pancake_status: set(order_id_external)}
    # 1. Lấy id thật từ Pancake theo từng status
    real: Dict[str, Set[str]] = {}
    for st_int, ps in STATUS_MAP.items():
        ids, err = fetch_status_orders(pos_shop_id, api_key, st_int, start_iso, end_iso)
        if err:
            result["error"] = f"status {st_int}: {err}"
            return result  # lỗi 1 status → skip shop (an toàn)
        real[ps] = ids
    # 2. Lấy đơn DB của shop trong cửa sổ — đơn đang pancake_status KHÁC với thực tế
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT order_id_external, pancake_status
                  FROM wh_outbound_requests
                 WHERE shop_name = %s
                   AND order_id_external IS NOT NULL AND order_id_external <> ''
            """, (shop_name,))
            db_rows = cur.fetchall()
    db_status = {str(r[0]): (r[1] or "") for r in db_rows}
    # 3. Tính: đơn nào Pancake nói received/returned/returning mà DB ghi khác → cần update
    for ps, ids in real.items():
        to_update = set()
        for oid in ids:
            cur_db = db_status.get(oid)
            if cur_db is not None and cur_db != ps:
                to_update.add(oid)
        if to_update:
            result["updates"][ps] = to_update
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Chỉ in diff, không UPDATE")
    ap.add_argument("--days-back", type=int, default=90, help="Cửa sổ ngày fetch (default 90)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--shop-id", type=str, default="", help="Chỉ chạy 1 pos_shop_id (test)")
    args = ap.parse_args()

    start_iso, end_iso = _utc_range(args.days_back)
    print(f"=== Reverse-sync pancake_status (CHỈ cột pancake_status) ===")
    print(f"  cửa sổ {args.days_back} ngày | dry_run={args.dry_run} | workers={args.workers}")
    print()

    with get_conn() as conn:
        with conn.cursor() as cur:
            q = ("SELECT pos_shop_id, pos_api_key, shop_name FROM wh_shops "
                 "WHERE pos_api_key IS NOT NULL AND pos_api_key <> ''")
            params: list = []
            if args.shop_id:
                q += " AND pos_shop_id = %s"
                params.append(args.shop_id)
            cur.execute(q, tuple(params))
            shops = cur.fetchall()

    print(f"Quét {len(shops)} shop...")
    t0 = time.time()
    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_shop, str(s[0]), s[1], s[2], start_iso, end_iso):
                s[2] for s in shops}
        done = 0
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"shop_name": futs[fut], "error": str(e), "updates": {}})
            done += 1
            if done % 10 == 0:
                print(f"  [{done}/{len(shops)}] elapsed {time.time()-t0:.1f}s")

    # Tổng hợp diff
    total_by_ps = {"received": 0, "returned": 0, "returning": 0}
    errored = [r for r in results if r.get("error")]
    have_updates = [r for r in results if r.get("updates")]
    for r in have_updates:
        for ps, ids in r["updates"].items():
            total_by_ps[ps] += len(ids)

    print()
    print(f"=== DIFF (sau {time.time()-t0:.1f}s) ===")
    print(f"  Đơn cần đổi → received:  {total_by_ps['received']}")
    print(f"  Đơn cần đổi → returned:  {total_by_ps['returned']}")
    print(f"  Đơn cần đổi → returning: {total_by_ps['returning']}")
    print(f"  Shop fetch lỗi (SKIP):   {len(errored)}")
    if errored:
        for r in errored[:10]:
            print(f"    - {r['shop_name']}: {r['error']}")
    print()
    # Show vài shop có thay đổi nhiều nhất
    have_updates.sort(key=lambda r: sum(len(v) for v in r["updates"].values()), reverse=True)
    print("  Top shop thay đổi:")
    for r in have_updates[:10]:
        n = sum(len(v) for v in r["updates"].values())
        detail = ", ".join(f"{ps}:{len(ids)}" for ps, ids in r["updates"].items())
        print(f"    {r['shop_name']:30s} {n:5d} đơn ({detail})")

    if args.dry_run:
        print()
        print("*** DRY-RUN: KHÔNG UPDATE DB. Bỏ --dry-run để chạy thật. ***")
        return 0

    # UPDATE thật — CHỈ cột pancake_status
    print()
    print("UPDATE pancake_status...")
    total_updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for r in have_updates:
                sname = r["shop_name"]
                for ps, ids in r["updates"].items():
                    if not ids:
                        continue
                    cur.execute("""
                        UPDATE wh_outbound_requests
                           SET pancake_status = %s
                         WHERE shop_name = %s
                           AND order_id_external = ANY(%s)
                           AND pancake_status <> %s
                    """, (ps, sname, list(ids), ps))
                    total_updated += cur.rowcount
            conn.commit()
    print(f"=== Hoàn tất: {total_updated} rows updated ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
