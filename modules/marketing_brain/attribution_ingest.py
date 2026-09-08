"""Marketing Brain — nhận attribution ads từ shipcod.vn (Cách B, spec MARKETING_BRAIN_SHIPCOD_SPEC.md).

Luồng:
1. shipcod POST → ingest_items() ghi vào mb_attribution_inbox (staging, không phụ thuộc
   đơn đã sync chưa).
2. apply_pending_inbox() merge inbox → mb_order_attribution cho các đơn ĐÃ tồn tại,
   set ad_source_origin='shipcod' (bất khả xâm phạm bởi cron sync payload).

Hai chiều thời điểm đều an toàn:
- shipcod trước, đơn sync sau: inbox chờ → cron sync gọi apply_pending_inbox cuối run → nâng cấp.
- đơn sync trước, shipcod sau: endpoint gọi apply_pending_inbox ngay → cập nhật.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _s(v, limit=None):
    out = str(v if v is not None else "").strip()
    return out[:limit] if limit else out


def ingest_items(cur, items: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Upsert các gói shipcod vào mb_attribution_inbox.

    Trả (received, ads_thieu_adid): ads_thieu_adid = số gói khai ad_source='ADS'
    nhưng ad_id rỗng — DẤU HIỆU shipcod bắt hụt mã (Meta đổi field). Trả về để
    endpoint cảnh báo NGAY, không để miscount âm thầm."""
    n = 0
    ads_no_id = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        pos_shop_id = _s(it.get("pos_shop_id"), 64)
        pancake_order_id = _s(it.get("pancake_order_id"), 100)
        if not pos_shop_id or not pancake_order_id:
            continue  # thiếu khóa → bỏ qua (không làm hỏng cả batch)
        if _s(it.get("ad_source")).upper() == "ADS" and not _s(it.get("ad_id")):
            ads_no_id += 1
        cur.execute(
            """
            INSERT INTO mb_attribution_inbox
                (pos_shop_id, pancake_order_id, ad_id, post_id, page_id,
                 conversation_id, ad_source, occurred_at, received_at, applied_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NULL)
            ON CONFLICT (pos_shop_id, pancake_order_id) DO UPDATE SET
                ad_id = EXCLUDED.ad_id, post_id = EXCLUDED.post_id,
                page_id = EXCLUDED.page_id, conversation_id = EXCLUDED.conversation_id,
                ad_source = EXCLUDED.ad_source, occurred_at = EXCLUDED.occurred_at,
                received_at = NOW(), applied_at = NULL
            """,
            (pos_shop_id, pancake_order_id, _s(it.get("ad_id"), 64), _s(it.get("post_id"), 160),
             _s(it.get("page_id"), 64), _s(it.get("conversation_id"), 160),
             _s(it.get("ad_source"), 32), it.get("occurred_at") or None),
        )
        n += 1
    return n, ads_no_id


def apply_pending_inbox(cur, limit: int = 5000) -> int:
    """Merge inbox chưa áp dụng → mb_order_attribution cho đơn đã tồn tại trong `orders`.

    shipcod là nguồn authoritative cho ad_id/post_id/page_id/conversation_id/ads_source
    → ghi đè + đánh dấu ad_source_origin='shipcod'. Các field "facts" của đơn
    (order_date, shop_id, page_name, is_livestream) giữ nguyên nếu đã có; điền khi INSERT mới.
    Trả số đơn đã áp dụng."""
    cur.execute(
        """
        WITH pend AS (
            -- page_id/post_id: ưu tiên shipcod, NHƯNG nếu shipcod gửi rỗng thì GIỮ giá trị
            -- sync đã có (đừng để rỗng xóa data tốt — đơn organic vẫn cần page_id).
            SELECT i.pos_shop_id, i.pancake_order_id,
                   o.id AS order_id, o.shop_id, o.created_at_pos::date AS order_date,
                   -- ad_id/post_id/page_id rỗng KHÔNG xóa giá trị đang có (đừng để bắt-hụt
                   -- xóa mã thật). Organic thật (chưa từng có ad_id) vẫn ra '' → is_organic.
                   COALESCE(NULLIF(i.ad_id, ''), m.ad_id, '') AS ad_id,
                   COALESCE(NULLIF(i.post_id, ''), m.post_id, '') AS post_id,
                   COALESCE(NULLIF(i.page_id, ''), m.page_id, '') AS page_id,
                   i.conversation_id, i.ad_source
            FROM mb_attribution_inbox i
            JOIN shops s ON s.pancake_shop_id = i.pos_shop_id
            JOIN orders o ON o.shop_id = s.id AND o.external_order_id = i.pancake_order_id
            LEFT JOIN mb_order_attribution m ON m.order_id = o.id
            WHERE i.applied_at IS NULL
              -- AN TOÀN: đơn khai ADS mà bắt hụt mã (ad_id rỗng) → KHÔNG ghép, giữ inbox
              -- để soi (tránh đếm nhầm sang organic). Chỉ ghép khi có mã HOẶC organic thật.
              AND NOT (UPPER(i.ad_source) = 'ADS' AND i.ad_id = '')
            ORDER BY i.received_at
            LIMIT %s
        ),
        upsert AS (
            INSERT INTO mb_order_attribution
                (order_id, shop_id, ad_id, post_id, page_id, conversation_id,
                 ads_source, is_organic, order_date, ad_source_origin)
            SELECT order_id, shop_id, ad_id, post_id, page_id, conversation_id,
                   ad_source, (ad_id = '' AND page_id <> ''), order_date, 'shipcod'
            FROM pend
            ON CONFLICT (order_id) DO UPDATE SET
                ad_id = EXCLUDED.ad_id, post_id = EXCLUDED.post_id,
                page_id = EXCLUDED.page_id, conversation_id = EXCLUDED.conversation_id,
                ads_source = EXCLUDED.ads_source, is_organic = EXCLUDED.is_organic,
                ad_source_origin = 'shipcod', updated_at = NOW()
            RETURNING order_id
        )
        UPDATE mb_attribution_inbox i SET applied_at = NOW()
        FROM pend
        WHERE i.pos_shop_id = pend.pos_shop_id AND i.pancake_order_id = pend.pancake_order_id
        """,
        (limit,),
    )
    return cur.rowcount
