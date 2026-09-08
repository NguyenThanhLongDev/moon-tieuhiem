#!/usr/bin/env python3
"""Cleanup đơn ảo "waiting" trong wh_outbound_requests.

Bug: polling chỉ kéo đơn ở status active từ Pancake → khi đơn rời status 9
(đã giao/hoàn/hủy) thì DB không biết, vẫn ghi pancake_status='waiting'.
→ Tab "Chờ chuyển hàng" phồng dần (hôm nay 2,879 vs Pancake thật 720).

Script này:
  1. Lấy tất cả đơn DB có status='pending' AND pancake_status='waiting' >2 ngày
  2. Gọi Pancake API GET /shops/{shop}/orders/{id} cho từng đơn
  3. Nếu Pancake trả status khác 0/9 → UPDATE pancake_status trong DB
  4. Throttle 0.05s/request để không bị rate-limit

Chạy:
  cd ~/tieuhiemsoft/posbottieuhiem
  set -a && source deploy/pos-dashboard.env && set +a
  .venv/bin/python3 scripts/cleanup_stale_waiting_orders.py
  .venv/bin/python3 scripts/cleanup_stale_waiting_orders.py --days 0   # check mọi đơn waiting
  .venv/bin/python3 scripts/cleanup_stale_waiting_orders.py --dry-run  # chỉ in, không UPDATE
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, Optional

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_conn  # noqa: E402

# Mapping Pancake status int → pancake_status string trong DB
# Đồng bộ với modules/kho_vat_ly/wh_sync_orders.py:_active_configs
STATUS_MAP = {
    0: "waiting",     # Mới
    1: "confirmed",   # Đã xác nhận
    4: "confirmed",   # Đang đóng hàng
    7: "confirmed",   # Đã xác nhận thường
    9: "waiting",     # Chờ chuyển hàng
    2: "shipped",     # Đã giao ĐVVC
    3: "received",    # Đã nhận
    5: "returned",    # Đã hoàn
    6: "cancelled",   # Đã hủy
}


def fetch_order_status(pos_shop_id: str, order_id: str, api_key: str,
                       timeout: int = 10) -> Optional[Dict[str, Any]]:
    """Gọi Pancake API lấy 1 đơn. Return None nếu lỗi/không tồn tại."""
    url = f"https://pos.pages.fm/api/v1/shops/{pos_shop_id}/orders/{order_id}"
    try:
        r = requests.get(url, params={"api_key": api_key}, timeout=timeout)
        if r.status_code == 404:
            return {"_not_found": True}
        if r.status_code != 200:
            return None
        data = r.json()
        # Pancake có thể wrap trong {"order": {...}} hoặc {"data": {...}} hoặc trực tiếp
        if isinstance(data, dict):
            for k in ("order", "data"):
                if isinstance(data.get(k), dict) and "id" in data[k]:
                    return data[k]
            if "id" in data:
                return data
    except Exception:
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2,
                    help="Chỉ check đơn waiting cũ hơn N ngày (default 2). "
                         "Đặt 0 để check mọi đơn waiting.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Tối đa bao nhiêu đơn xử lý (0 = không giới hạn)")
    ap.add_argument("--throttle", type=float, default=0.05,
                    help="Sleep giữa request (giây, default 0.05)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Chỉ in thay đổi, không UPDATE DB")
    ap.add_argument("--shop", type=str, default="",
                    help="Lọc 1 shop_name (vd 'kenleader'), mặc định tất cả")
    ap.add_argument("--shop-from", type=str, default="",
                    help="Chỉ shop có name >= prefix này (cho parallel)")
    ap.add_argument("--shop-to", type=str, default="",
                    help="Chỉ shop có name < prefix này (cho parallel)")
    args = ap.parse_args()

    print(f"=== Cleanup stale waiting orders ===")
    print(f"  days_threshold={args.days}  limit={args.limit or 'unlimited'}  "
          f"throttle={args.throttle}s  dry_run={args.dry_run}  "
          f"shop_filter={args.shop or 'ALL'}")
    print()

    # 1. Lấy danh sách đơn cần check + map shop → api_key
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Map shop_name → (pos_shop_id, api_key)
            cur.execute(
                "SELECT shop_name, pos_shop_id, pos_api_key FROM wh_shops "
                "WHERE pos_api_key IS NOT NULL AND pos_api_key <> ''"
            )
            shop_map: Dict[str, Dict[str, str]] = {}
            for shop_name, pos_id, api_key in cur.fetchall():
                shop_map[shop_name] = {"pos_id": str(pos_id), "api_key": api_key}

            # Đơn cần check — DEDUP theo (shop_name, order_id_external)
            # vì 1 đơn có nhiều rows (mỗi sản phẩm 1 row)
            where_extra = ""
            params = []
            if args.days > 0:
                where_extra += " AND created_at::timestamp < NOW() - %s::interval"
                params.append(f"{args.days} days")
            if args.shop:
                where_extra += " AND shop_name = %s"
                params.append(args.shop)
            if args.shop_from:
                where_extra += " AND shop_name >= %s"
                params.append(args.shop_from)
            if args.shop_to:
                where_extra += " AND shop_name < %s"
                params.append(args.shop_to)

            sql = f"""
                SELECT DISTINCT shop_name, order_id_external, order_code
                FROM wh_outbound_requests
                WHERE status='pending' AND pancake_status='waiting'
                  {where_extra}
                ORDER BY shop_name, order_id_external
            """
            cur.execute(sql, params)
            stale_orders = cur.fetchall()

    print(f"Tổng số đơn (unique) cần check: {len(stale_orders)}")
    if args.limit and args.limit < len(stale_orders):
        stale_orders = stale_orders[:args.limit]
        print(f"  → giới hạn xử lý {args.limit} đơn đầu tiên")
    print()

    # 2. Loop check + update
    stats = Counter()
    updated_orders = []
    skipped_no_key = 0

    t_start = time.time()
    for idx, (shop_name, order_id_external, order_code) in enumerate(stale_orders, 1):
        if shop_name not in shop_map:
            stats["no_shop_config"] += 1
            skipped_no_key += 1
            continue
        info = shop_map[shop_name]
        pos_id = info["pos_id"]
        api_key = info["api_key"]

        order = fetch_order_status(pos_id, order_id_external, api_key)
        if order is None:
            stats["api_error"] += 1
            if idx % 50 == 0:
                print(f"  [{idx}/{len(stale_orders)}] api_error shop={shop_name} order={order_id_external}")
            time.sleep(args.throttle)
            continue
        if order.get("_not_found"):
            stats["not_found"] += 1
            updated_orders.append((shop_name, order_id_external, order_code, "not_found", "deleted"))
            time.sleep(args.throttle)
            continue

        try:
            real_status_int = int(order.get("status", -1))
        except (TypeError, ValueError):
            real_status_int = -1
        real_label = STATUS_MAP.get(real_status_int)

        if real_label is None or real_label == "waiting":
            stats["still_waiting"] += 1
            time.sleep(args.throttle)
            continue

        # Status thay đổi → cần UPDATE
        partner = order.get("partner") or {}
        new_tracking = (partner.get("extend_code") or "").strip()
        new_carrier = ""
        try:
            from modules.kho_vat_ly.wh_sync_orders import detect_carrier_name
            new_carrier = detect_carrier_name(order) or ""
        except Exception:
            new_carrier = (partner.get("partner_name") or "").strip()
        new_picked_up = ""
        if real_status_int in (2, 3):
            for h in (order.get("histories") or []):
                if str(h.get("status", "")) == "2":
                    new_picked_up = h.get("updated_at") or h.get("inserted_at") or ""
                    break

        stats[f"to_{real_label}"] += 1
        updated_orders.append((shop_name, order_id_external, order_code,
                               f"waiting→{real_label}", new_tracking))

        if not args.dry_run:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE wh_outbound_requests
                               SET pancake_status = %s,
                                   carrier_name = CASE WHEN COALESCE(%s,'')<>''
                                                       THEN %s ELSE carrier_name END,
                                   tracking_code = CASE WHEN COALESCE(%s,'')<>''
                                                       THEN %s ELSE tracking_code END,
                                   carrier_picked_up_at = CASE WHEN COALESCE(%s,'')<>''
                                                       THEN %s ELSE carrier_picked_up_at END
                             WHERE shop_name = %s
                               AND order_id_external = %s
                               AND status = 'pending'
                               AND pancake_status = 'waiting'
                        """, (real_label, new_carrier, new_carrier,
                              new_tracking, new_tracking,
                              new_picked_up, new_picked_up,
                              shop_name, order_id_external))
                    conn.commit()
            except Exception as e:
                print(f"  DB update fail shop={shop_name} order={order_id_external}: {e}")
                stats["db_error"] += 1

        if idx % 100 == 0:
            elapsed = time.time() - t_start
            rate = idx / elapsed if elapsed > 0 else 0
            eta_min = (len(stale_orders) - idx) / rate / 60 if rate > 0 else 0
            print(f"  [{idx}/{len(stale_orders)}] {rate:.1f} req/s  "
                  f"ETA {eta_min:.1f} phút  stats={dict(stats)}")

        time.sleep(args.throttle)

    elapsed = time.time() - t_start
    print()
    print(f"=== Hoàn tất sau {elapsed:.1f}s ===")
    print(f"  Tổng check: {len(stale_orders)}")
    print(f"  Skipped (không có shop config): {skipped_no_key}")
    print(f"  Stats: {dict(stats)}")
    print()

    if updated_orders:
        print(f"=== {len(updated_orders)} đơn cần update — sample 20 đơn đầu ===")
        for shop, oid, ocode, change, tracking in updated_orders[:20]:
            print(f"  {shop:25s} | order={oid:12s} ({ocode:>10s}) | {change:25s} | track={tracking}")
        if len(updated_orders) > 20:
            print(f"  ... còn {len(updated_orders) - 20} đơn nữa")

    if args.dry_run:
        print("\n*** DRY-RUN: KHÔNG UPDATE DB ***")
        print("    Bỏ --dry-run để áp dụng thay đổi thật.")
    else:
        print(f"\n✓ Đã UPDATE {len(updated_orders) - stats.get('db_error', 0)} đơn thành công.")
        print(f"  Vào https://tieuhiem.com/kho-vat-ly/outbound check badge 'Chờ chuyển hàng'.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
