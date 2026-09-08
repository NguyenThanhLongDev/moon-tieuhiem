#!/usr/bin/env python3
"""Sync page → sản phẩm bán nhiều nhất (top product) từ Pancake POS.

1 page ≈ 1 sản phẩm (ổn định) → chỉ cần lấy MẪU đơn gần đây là map đủ.
Với mỗi shop: đọc N trang đơn gần nhất, gom số lượng theo (page_id, product_id),
chọn sản phẩm nhiều nhất mỗi page (kèm tên SKU + ảnh từ variation_info), upsert
vào bảng pos_page_top_product.

Dùng:
  python scripts/sync_page_top_product.py --shop-ids 1720128057 --max-pages 15
  python scripts/sync_page_top_product.py --all --max-pages 15
"""
from __future__ import annotations
import os, sys, time, argparse, logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_conn
from pancake_auth import get_shop_api_key

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_page_top_product")

POS_BASE = "https://pos.pancake.vn/api/v1/shops"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://pos.pancake.vn"}


def _all_active_shops() -> List[Tuple[str, int]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pos_shop_id, id FROM wh_shops "
            "WHERE status='active' AND pos_shop_id IS NOT NULL AND pos_shop_id <> ''"
        )
        return [(str(r[0]), int(r[1])) for r in cur.fetchall()]


def _our_shop_id(pos_shop_id: str) -> Optional[int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM shops WHERE pancake_shop_id=%s LIMIT 1", (str(pos_shop_id),))
        row = cur.fetchone()
        return int(row[0]) if row else None


def _fetch_orders_page(pos_shop_id: str, api_key: str, page_number: int, page_size: int = 100) -> list:
    url = f"{POS_BASE}/{pos_shop_id}/orders"
    r = requests.get(
        url,
        params={"api_key": api_key, "page_size": page_size, "page_number": page_number},
        headers=_HEADERS, timeout=60,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("success") is not True:
        return []
    return d.get("data") or []


def sync_shop(pos_shop_id: str, our_shop_id: Optional[int], max_pages: int) -> int:
    api_key = get_shop_api_key(str(pos_shop_id))
    if not api_key:
        logger.warning("Shop %s: KHÔNG có api_key — bỏ qua", pos_shop_id)
        return 0
    if our_shop_id is None:
        our_shop_id = _our_shop_id(pos_shop_id)
    if our_shop_id is None:
        logger.warning("Shop %s: không map được shops.id — bỏ qua", pos_shop_id)
        return 0

    # (page_id, product_id) -> [qty, name, image]
    agg: Dict[Tuple[str, str], list] = defaultdict(lambda: [0, "", ""])
    for pn in range(1, max_pages + 1):
        try:
            orders = _fetch_orders_page(pos_shop_id, api_key, pn)
        except Exception as exc:
            logger.warning("  shop %s trang %d lỗi: %s", pos_shop_id, pn, str(exc)[:100])
            time.sleep(2)
            continue
        if not orders:
            break
        for o in orders:
            pid = str(o.get("page_id") or "").strip()
            if not pid.isdigit():
                continue
            for it in (o.get("items") or []):
                prod_id = str(it.get("product_id") or "").strip()
                if not prod_id:
                    continue
                vi = it.get("variation_info") or {}
                qty = int(it.get("quantity") or 0)
                imgs = vi.get("images") or []
                key = (pid, prod_id)
                rec = agg[key]
                rec[0] += qty
                if not rec[1]:
                    rec[1] = vi.get("name") or vi.get("product_display_id") or prod_id
                if not rec[2] and imgs:
                    rec[2] = imgs[0]
        time.sleep(0.4)  # nhẹ nhàng tránh rate-limit

    # Chọn sản phẩm top mỗi page
    top: Dict[str, list] = {}  # page_id -> [product_id, name, image, qty]
    for (pid, prod_id), (qty, name, img) in agg.items():
        cur = top.get(pid)
        if cur is None or qty > cur[3]:
            top[pid] = [prod_id, name, img, qty]

    if not top:
        logger.info("  shop %s: không gom được sản phẩm nào", pos_shop_id)
        return 0

    with get_conn() as conn, conn.cursor() as cur:
        for pid, (prod_id, name, img, qty) in top.items():
            cur.execute(
                """
                INSERT INTO pos_page_top_product
                    (shop_id, page_id, product_id, product_name, product_image, qty, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (shop_id, page_id) DO UPDATE SET
                    product_id=EXCLUDED.product_id, product_name=EXCLUDED.product_name,
                    product_image=EXCLUDED.product_image, qty=EXCLUDED.qty, updated_at=NOW()
                """,
                (our_shop_id, pid, prod_id, name, img, qty),
            )
    logger.info("  shop %s → %d page có sản phẩm", pos_shop_id, len(top))
    return len(top)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop-ids", default="", help="pancake_shop_id, phẩy ngăn cách")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-pages", type=int, default=15, help="Số trang đơn (×100) quét mỗi shop")
    args = ap.parse_args()

    targets: List[Tuple[str, Optional[int]]] = []
    if args.all:
        targets = _all_active_shops()
    if args.shop_ids:
        for sid in args.shop_ids.split(","):
            sid = sid.strip()
            if sid:
                targets.append((sid, None))
    if not targets:
        logger.error("Cần --all hoặc --shop-ids")
        sys.exit(1)

    grand = 0
    for pos_shop_id, oid in targets:
        grand += sync_shop(pos_shop_id, oid, args.max_pages)
    logger.info("=== DONE: %d page-product ===", grand)


if __name__ == "__main__":
    main()
