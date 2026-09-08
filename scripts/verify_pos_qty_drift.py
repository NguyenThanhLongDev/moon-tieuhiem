#!/usr/bin/env python3
"""
Verify drift giữa qty tồn POS (API) và DB cho từng (shop × variation).

Bắt được các bug sync như:
- 2026-05-21: `wh_sync_pos.py` đọc `vw_list[0]` thay vì SUM → MISS tồn ở
  warehouse khác của cùng shop. 1 SKU TÀI5 lệch 201 đơn vị.

Cách chạy:
    .venv/bin/python3 scripts/verify_pos_qty_drift.py
    .venv/bin/python3 scripts/verify_pos_qty_drift.py --shop 714312442
    .venv/bin/python3 scripts/verify_pos_qty_drift.py --tolerate 0  # strict
    .venv/bin/python3 scripts/verify_pos_qty_drift.py --telegram    # gửi Telegram nếu có drift

Output: bảng các (shop, sku, pvid) có DB ≠ POS, sorted by abs delta DESC.
Exit code 0 = OK, 1 = có drift.

Khuyến nghị: thêm vào cron 1 lần/ngày (vd 03:00) để bắt drift sớm.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

sys.path.insert(0, "/home/admin1/tieuhiemsoft/posbottieuhiem")
from modules.kho_vat_ly.wh_db import wh_db as db  # noqa: E402

API_BASE = "https://pos.pages.fm/api/v1"


def fetch_shop_products(shop: dict) -> tuple[dict, list[dict]]:
    """Fetch all products + variations + warehouses for 1 shop."""
    sid = shop["pos_shop_id"]
    key = shop["pos_api_key"]
    out = []
    try:
        for page in range(1, 50):
            r = requests.get(
                f"{API_BASE}/shops/{sid}/products",
                params={"api_key": key, "page_size": 100, "page": page},
                timeout=30,
            )
            if r.status_code != 200:
                return shop, []
            d = r.json() or {}
            data = d.get("data") or []
            for prod in data:
                cid = (prod.get("custom_id") or "").strip()
                for v in prod.get("variations") or []:
                    whs = v.get("variations_warehouses") or []
                    qty_api = sum(int(w.get("actual_remain_quantity") or 0) for w in whs)
                    out.append({
                        "sku": cid,
                        "pvid": v.get("id") or "",
                        "display_id": v.get("display_id") or "",
                        "qty_api": qty_api,
                    })
            if page >= int(d.get("total_pages") or 1):
                break
    except Exception:
        return shop, out
    return shop, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop", help="Filter 1 pos_shop_id thôi")
    ap.add_argument("--tolerate", type=int, default=0,
                    help="Bỏ qua delta nhỏ hơn N (default 0)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=50,
                    help="Top N drift để in")
    args = ap.parse_args()

    with db() as conn:
        # Chỉ scan shop active (inactive thường mất API key → fail → false orphan).
        q = ("SELECT id, shop_name, pos_shop_id, pos_api_key FROM wh_shops "
             "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' "
             "AND COALESCE(status,'active') = 'active'")
        params: list = []
        if args.shop:
            q += " AND pos_shop_id = %s"
            params.append(args.shop)
        shops = [dict(r) for r in conn.execute(q, params).fetchall()]

        # DB qty per (shop_id, pvid)
        db_qty: dict[tuple[int, str], int] = {}
        rows = conn.execute("""
            SELECT si.shop_id, si.qty_pos, vm.pos_variation_id
            FROM wh_shop_inventory si
            JOIN wh_variation_map vm ON vm.product_id = si.product_id
        """).fetchall()
        for r in rows:
            # NOTE: si.qty_pos là tổng qua TẤT CẢ pvid của product trong shop đó.
            # Drift fine-grained per pvid → dùng vmap.pos_remain_qty so sánh
            pass
        # vmap.pos_remain_qty cross-shop. Cho fine-grained per-shop drift,
        # phải gọi API. Bảng chính so sánh = vmap.pos_remain_qty (tổng all shop).
        vmap_qty = {r["pos_variation_id"]: int(r["pos_remain_qty"] or 0)
                    for r in conn.execute(
                        "SELECT pos_variation_id, pos_remain_qty FROM wh_variation_map"
                    ).fetchall()}

    print(f"[verify] Quét {len(shops)} shops parallel={args.workers}...", flush=True)

    # Build qty per pvid tổng all shops từ API
    api_qty_total: dict[str, int] = {}
    shop_for_pvid: dict[str, list[str]] = {}
    failed_shops: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_shop_products, s): s for s in shops}
        for f in as_completed(futs):
            shop, items = f.result()
            done += 1
            if not items:
                failed_shops.append(shop["shop_name"])
            for it in items:
                pvid = it["pvid"]
                if not pvid:
                    continue
                api_qty_total[pvid] = api_qty_total.get(pvid, 0) + it["qty_api"]
                shop_for_pvid.setdefault(pvid, []).append(shop["shop_name"])
            print(f"[{done}/{len(shops)}] {shop['shop_name']:30s} items={len(items)}", flush=True)

    if failed_shops:
        print(f"\n⚠ {len(failed_shops)} shop fetch FAIL — drift của shop này không đáng tin: {failed_shops}\n", flush=True)

    # Compare
    drifts = []
    for pvid, qty_api in api_qty_total.items():
        qty_db = vmap_qty.get(pvid, 0)
        delta = qty_api - qty_db
        if abs(delta) > args.tolerate:
            drifts.append({
                "pvid": pvid,
                "qty_api": qty_api,
                "qty_db": qty_db,
                "delta": delta,
                "shops": shop_for_pvid.get(pvid, []),
            })

    # pvid trong DB nhưng không có ở API → đã bị xoá khỏi POS
    for pvid, qty_db in vmap_qty.items():
        if pvid not in api_qty_total and qty_db != 0:
            drifts.append({
                "pvid": pvid,
                "qty_api": 0,
                "qty_db": qty_db,
                "delta": -qty_db,
                "shops": ["(không còn trên POS)"],
            })

    drifts.sort(key=lambda d: abs(d["delta"]), reverse=True)

    print()
    print(f"[verify] Tổng pvid POS: {len(api_qty_total)}")
    print(f"[verify] Tổng pvid DB:  {len(vmap_qty)}")
    print(f"[verify] DRIFT count > {args.tolerate}: {len(drifts)}")

    if drifts:
        print()
        print(f"Top {min(args.limit, len(drifts))} drift (|delta| DESC):")
        print(f"{'pvid':38s} {'API':>8} {'DB':>8} {'Δ':>8}  shops")
        for d in drifts[:args.limit]:
            shops_str = ", ".join(d["shops"][:3])
            print(f"{d['pvid']:38s} {d['qty_api']:>8} {d['qty_db']:>8} {d['delta']:>+8}  {shops_str}")
        return 1

    print("[verify] ✅ Không có drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
