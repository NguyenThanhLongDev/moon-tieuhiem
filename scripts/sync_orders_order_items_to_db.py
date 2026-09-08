from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402

from order_pickup_utils import order_counts_for_carrier_pickup_day  # noqa: E402


def read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync orders + order_items into DB (phase B.1 safe mode)")
    parser.add_argument("--date", default="", help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--shop-key", default="", help="Sync only one shop_key")
    parser.add_argument("--page-size", type=int, default=100, help="POS API page size")
    parser.add_argument("--max-pages", type=int, default=200, help="Safety cap for pagination")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse only, do not write DB")
    parser.add_argument(
        "--update-status",
        default="inserted_at",
        choices=("inserted_at", "carrier_picked_up_at"),
        help="POS get_orders updateStatus: inserted_at = đơn tạo trong ngày; carrier_picked_up_at = ĐVVC lấy trong ngày (xuất hàng)",
    )
    return parser.parse_args()


def parse_target_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now().strftime("%Y-%m-%d")
    datetime.strptime(text, "%Y-%m-%d")
    return text


def get_day_range_params(target_date_str: str) -> Tuple[int, int, str, str]:
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_local = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_local = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    start_utc = start_local - timedelta(hours=7)
    end_utc = end_local - timedelta(hours=7)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return start_ts, end_ts, start_iso, end_iso


def map_order_status(value: Any) -> str:
    key = str(value)
    mapping = {
        "0": "new",
        "1": "confirmed",
        "2": "shipping",
        "3": "delivered",
        "4": "returning",
        "5": "returned",
        "6": "cancelled",
        # Pancake status 9 = "Chờ chuyển hàng" (đã đóng, chờ ĐVVC lấy) — KHÔNG phải hoàn.
        # Gộp vào "confirmed" để khớp cột "Chờ ch.hàng" + convention shop_order_status_cache.
        "9": "confirmed",
    }
    return mapping.get(key, "unknown")


def map_payment_status(order: Dict[str, Any]) -> str:
    paid = float(order.get("paid_amount", 0) or 0)
    total = float(order.get("total_price", 0) or 0)
    if total <= 0 and paid <= 0:
        return "unknown"
    if paid <= 0:
        return "unpaid"
    if 0 < paid < total:
        return "partial"
    if paid >= total:
        return "paid"
    return "unknown"


def parse_pos_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing timestamp")
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts)
    # Support POS variants: with/without Z, with microseconds, optional timezone offset.
    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
        return dt.replace(tzinfo=None) + timedelta(hours=7) if dt.tzinfo else dt
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    raise ValueError(f"unrecognized timestamp format: {text}")


def to_business_local_datetime(pos_dt: datetime) -> datetime:
    """
    Normalize POS timestamp to business local time (UTC+7).
    POS API often returns UTC-like values without timezone suffix.
    """
    return pos_dt + timedelta(hours=7)


def request_orders_page(
    shop_code: str,
    api_key: str,
    target_date_str: str,
    page: int,
    page_size: int,
    update_status: str = "inserted_at",
    only_status_sent: bool = False,
) -> Dict[str, Any]:
    start_ts, end_ts, start_iso, end_iso = get_day_range_params(target_date_str)
    url = f"https://pos.pancake.vn/api/v1/shops/{shop_code}/orders/get_orders"
    params = [
        ("api_key", api_key),
        ("page_size", page_size),
        ("page", page),
        ("updateStatus", update_status),
        ("editorId", "none"),
        ("option_sort", "inserted_at_desc"),
        ("startDateTime", str(start_ts)),
        ("endDateTime", str(end_ts)),
        ("timeRange[]", start_iso),
        ("timeRange[]", end_iso),
    ]
    if only_status_sent:
        params.append(("status", 2))
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pos.pancake.vn",
        "Referer": f"https://pos.pancake.vn/shop/{shop_code}/order",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        res = requests.post(url, params=params, headers=headers, json={}, timeout=45)
        return res.json()
    except Exception as exc:
        return {"error": str(exc)}


def fetch_all_orders_for_shop(
    shop_code: str,
    api_key: str,
    target_date_str: str,
    page_size: int,
    max_pages: int,
    update_status: str = "inserted_at",
    only_status_sent: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    all_orders: List[Dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        payload = request_orders_page(
            shop_code,
            api_key,
            target_date_str,
            page,
            page_size,
            update_status=update_status,
            only_status_sent=only_status_sent,
        )
        if payload.get("error"):
            return [], str(payload["error"])
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return [], "Invalid API payload: data is not a list"
        all_orders.extend([r for r in rows if isinstance(r, dict)])
        if len(rows) < page_size:
            break
        page += 1
    return all_orders, None


def load_db_shop_map(cur) -> Dict[str, int]:
    cur.execute("SELECT id, shop_key FROM shops")
    result: Dict[str, int] = {}
    for row in cur.fetchall():
        shop_id, shop_key = row
        if shop_key:
            result[str(shop_key)] = int(shop_id)
    return result


def load_active_shops_with_api_keys(cur) -> List[Dict[str, Any]]:
    """Load active shops that have valid hex API keys from DB."""
    cur.execute("""
        SELECT s.id, s.shop_key, s.pancake_shop_id, w.pos_api_key
        FROM shops s
        JOIN wh_shops w ON w.shop_key = s.shop_key
        WHERE s.status = 'active'
          AND w.status = 'active'
          AND w.pos_api_key IS NOT NULL
          AND length(w.pos_api_key) = 32
    """)
    result = []
    for row in cur.fetchall():
        db_id, shop_key, pancake_shop_id, api_key = row
        key = str(api_key or "").strip()
        if shop_key and pancake_shop_id and len(key) == 32:
            result.append({
                "db_id": int(db_id),
                "shop_key": str(shop_key),
                "shop_code": str(pancake_shop_id),
                "api_key": key,
            })
    return result


def upsert_order(cur, db_shop_id: int, order: Dict[str, Any]) -> int:
    external_order_id = str(order.get("id") or order.get("order_id") or "").strip()
    if not external_order_id:
        external_order_id = f"missing-{hash(json.dumps(order, ensure_ascii=False))}"
    created_at_pos_raw = parse_pos_datetime(order.get("inserted_at") or order.get("created_at"))
    created_at_pos = to_business_local_datetime(created_at_pos_raw)
    order_code = str(order.get("order_number") or order.get("code") or "").strip() or None
    customer_name = str(order.get("customer_name") or "").strip() or None
    # Pancake KHÔNG trả customer_phone ở cấp gốc — số nằm trong shipping_address /
    # bill_phone_number. Thiếu bước này thì cột customer_phone rỗng toàn bộ,
    # đối soát đơn LadiPage ↔ POS (theo SĐT) sẽ không khớp được đơn nào.
    customer_phone = str(order.get("customer_phone") or "").strip() or None
    if not customer_phone:
        _sa = order.get("shipping_address") or {}
        customer_phone = (str(_sa.get("phone_number") or "").strip()
                          or str(order.get("bill_phone_number") or "").strip() or None)
    order_status = map_order_status(order.get("status"))
    payment_status = map_payment_status(order)

    subtotal_amount = float(order.get("sub_total_price", 0) or 0)
    discount_amount = float(order.get("discount_price", 0) or 0)
    shipping_fee = float(order.get("shipping_fee", 0) or 0)
    other_fee = float(order.get("other_fee", 0) or 0)
    total_amount = float(order.get("total_price", 0) or 0)
    # net_revenue = TIỀN THỰC THU của đơn (sau giảm giá) — verify 65.030/65.030 đơn
    # khớp cod+prepaid. total_price chỉ là tổng giá niêm yết SP (đừng dùng làm doanh thu).
    net_revenue = float(order.get("total_price_after_sub_discount") or order.get("price") or total_amount or 0)

    cur.execute(
        """
        INSERT INTO orders (
            shop_id, external_order_id, order_code, customer_name, customer_phone,
            order_status, payment_status, created_at_pos,
            subtotal_amount, discount_amount, shipping_fee, other_fee, total_amount, net_revenue,
            raw_payload_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (shop_id, external_order_id)
        DO UPDATE SET
            order_code = EXCLUDED.order_code,
            customer_name = EXCLUDED.customer_name,
            customer_phone = EXCLUDED.customer_phone,
            order_status = EXCLUDED.order_status,
            payment_status = EXCLUDED.payment_status,
            created_at_pos = EXCLUDED.created_at_pos,
            subtotal_amount = EXCLUDED.subtotal_amount,
            discount_amount = EXCLUDED.discount_amount,
            shipping_fee = EXCLUDED.shipping_fee,
            other_fee = EXCLUDED.other_fee,
            total_amount = EXCLUDED.total_amount,
            net_revenue = EXCLUDED.net_revenue,
            raw_payload_json = EXCLUDED.raw_payload_json,
            synced_at = NOW()
        RETURNING id
        """,
        (
            db_shop_id,
            external_order_id,
            order_code,
            customer_name,
            customer_phone,
            order_status,
            payment_status,
            created_at_pos,
            subtotal_amount,
            discount_amount,
            shipping_fee,
            other_fee,
            total_amount,
            net_revenue,
            json.dumps(order, ensure_ascii=False),
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_order_attribution(cur, order_id: int, db_shop_id: int, order: Dict[str, Any]) -> None:
    """Marketing Brain: extract attribution per-order từ payload Pancake → mb_order_attribution.

    Cùng dữ liệu với backfill SQL từ raw_payload_json — giữ 2 nơi đồng nhất khi sửa.
    """
    ad_id = str(order.get("ad_id") or "").strip()
    post_id = str(order.get("post_id") or "").strip()
    page_id = str(order.get("page_id") or "").strip()
    page_name = str((order.get("page") or {}).get("name") or "").strip()[:255]
    conversation_id = str(order.get("conversation_id") or "").strip()
    ads_source = str(order.get("ads_source") or "").strip()
    is_livestream = bool(order.get("is_livestream") or False)
    is_organic = (ad_id == "" and page_id != "")
    created_raw = parse_pos_datetime(order.get("inserted_at") or order.get("created_at"))
    order_date = to_business_local_datetime(created_raw).date()
    cur.execute(
        """
        INSERT INTO mb_order_attribution
            (order_id, shop_id, ad_id, post_id, page_id, page_name,
             conversation_id, ads_source, is_livestream, is_organic, order_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (order_id) DO UPDATE SET
            -- ad fields: KHÔNG ghi đè khi nguồn là shipcod (authoritative sau khi bỏ Pancake
            -- Chat — payload hết ad_id). Chỉ payload-origin mới cho payload cập nhật.
            ad_id = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                         THEN mb_order_attribution.ad_id ELSE EXCLUDED.ad_id END,
            post_id = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                           THEN mb_order_attribution.post_id ELSE EXCLUDED.post_id END,
            page_id = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                           THEN mb_order_attribution.page_id ELSE EXCLUDED.page_id END,
            conversation_id = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                                   THEN mb_order_attribution.conversation_id ELSE EXCLUDED.conversation_id END,
            ads_source = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                              THEN mb_order_attribution.ads_source ELSE EXCLUDED.ads_source END,
            is_organic = CASE WHEN mb_order_attribution.ad_source_origin = 'shipcod'
                              THEN mb_order_attribution.is_organic ELSE EXCLUDED.is_organic END,
            -- facts của đơn: luôn cập nhật từ payload
            page_name = EXCLUDED.page_name, is_livestream = EXCLUDED.is_livestream,
            order_date = EXCLUDED.order_date, updated_at = NOW()
        """,
        (order_id, db_shop_id, ad_id, post_id, page_id, page_name,
         conversation_id, ads_source, is_livestream, is_organic, order_date),
    )


def replace_order_items(cur, order_id: int, db_shop_id: int, order: Dict[str, Any]) -> int:
    cur.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
    inserted = 0
    for item in order.get("items", []):
        if not isinstance(item, dict):
            continue
        variation_info = item.get("variation_info", {}) or {}
        product_name = (
            str(variation_info.get("name") or "").strip()
            or str(item.get("note_product") or "").strip()
            or str(item.get("name") or "").strip()
            or "Không rõ tên"
        )
        variant_name = str(item.get("variation_name") or "").strip() or None
        sku = str(variation_info.get("custom_id") or item.get("sku") or "").strip() or None
        qty = float(item.get("quantity", 0) or 0)
        # Payload Pancake KHÔNG có item['price']/['total_price'] — giá bán thực nằm ở
        # variation_info.retail_price (đã verify khớp orders.total_amount kể cả đơn nhiều món).
        # Bug cũ: đọc item['price'] không tồn tại → unit_price/line_total = 0 toàn bộ.
        unit_price = float(item.get("price", 0) or variation_info.get("retail_price", 0) or 0)
        discount_amount = float(item.get("discount", 0) or item.get("total_discount", 0) or 0)
        line_total = float(item.get("total_price", 0) or 0) or max(qty * unit_price - discount_amount, 0.0)
        external_product_id = str(item.get("product_id") or variation_info.get("product_id") or "").strip() or None
        external_variant_id = str(item.get("variation_id") or "").strip() or None

        cur.execute(
            """
            INSERT INTO order_items (
                order_id, shop_id, external_product_id, external_variant_id,
                product_name, variant_name, sku, quantity, unit_price, discount_amount, line_total
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                db_shop_id,
                external_product_id,
                external_variant_id,
                product_name,
                variant_name,
                sku,
                qty,
                unit_price,
                discount_amount,
                line_total,
            ),
        )
        inserted += 1
    return inserted


def main() -> None:
    args = parse_args()
    target_date = parse_target_date(args.date)
    update_status = str(args.update_status or "inserted_at").strip()
    pickup_mode = update_status == "carrier_picked_up_at"

    total_api_orders = 0
    total_upserted_orders = 0
    total_upserted_items = 0
    failed_shops: List[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            active_shops = load_active_shops_with_api_keys(cur)

            if args.shop_key:
                active_shops = [s for s in active_shops if s["shop_key"] == args.shop_key.strip()]

            if not active_shops:
                raise SystemExit("No active shops with valid API keys found in DB")

            for shop in active_shops:
                shop_key = shop["shop_key"]
                shop_code = shop["shop_code"]
                db_shop_id = shop["db_id"]
                api_key = shop["api_key"]

                orders, err = fetch_all_orders_for_shop(
                    shop_code=shop_code,
                    api_key=api_key,
                    target_date_str=target_date,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    update_status=update_status,
                    only_status_sent=pickup_mode,
                )
                if err:
                    failed_shops.append(f"{shop_key}:{err}")
                    continue

                total_api_orders += len(orders)
                if args.dry_run:
                    continue

                for order in orders:
                    if pickup_mode:
                        if not order_counts_for_carrier_pickup_day(
                            order,
                            target_date,
                            trust_carrier_window_when_pickup_unknown=True,
                            trust_carrier_pickup_es_scope=False,
                        ):
                            continue
                    else:
                        created_raw = parse_pos_datetime(order.get("inserted_at") or order.get("created_at"))
                        created_local = to_business_local_datetime(created_raw)
                        if created_local.strftime("%Y-%m-%d") != target_date:
                            continue
                    order_id = upsert_order(cur, db_shop_id=db_shop_id, order=order)
                    total_upserted_orders += 1
                    total_upserted_items += replace_order_items(cur, order_id=order_id, db_shop_id=db_shop_id, order=order)
                    try:
                        upsert_order_attribution(cur, order_id=order_id, db_shop_id=db_shop_id, order=order)
                    except Exception as exc:
                        # Attribution là phụ — không được làm gãy sync đơn chính
                        print(f"[mb_attribution] order {order_id} skip: {exc}")

    # Marketing Brain: merge inbox shipcod cho đơn vừa sync (shipcod bắn TRƯỚC khi đơn về
    # → inbox chờ; giờ đơn đã tồn tại nên áp dụng được). Idempotent, an toàn chạy lại.
    try:
        from modules.marketing_brain.attribution_ingest import apply_pending_inbox
        with get_conn() as _conn:
            with _conn.cursor() as _cur:
                _applied = apply_pending_inbox(_cur)
            _conn.commit()
        if _applied:
            print(f"mb_attribution_applied={_applied}")
    except Exception as _exc:
        print(f"[mb_attribution] apply_pending_inbox skip: {_exc}")

    print("Sync completed")
    print(f"update_status={update_status}")
    print(f"target_date={target_date}")
    print(f"shops_processed={len(active_shops)}")
    print(f"api_orders={total_api_orders}")
    print(f"db_orders_upserted={total_upserted_orders}")
    print(f"db_order_items_upserted={total_upserted_items}")
    print(f"failed_shops={len(failed_shops)}")
    if failed_shops:
        print("failed_shop_details:")
        for item in failed_shops:
            print(f"- {item}")


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required. Example: export DATABASE_URL=postgresql://user:pass@host:5432/db")
    main()
