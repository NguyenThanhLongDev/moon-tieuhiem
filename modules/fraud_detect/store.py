"""LÕI phát hiện sale gian lận — kho hội thoại chuẩn + logic phát hiện.

ĐỘC LẬP NGUỒN: mọi adapter (Pancake Chat / FB Webhook / tool khác) chỉ cần gọi
`ingest_conversation()` để đổ hội thoại + tin nhắn vào đây. Đổi nguồn → chỉ thay
adapter, phần này KHÔNG đổi.

Nguyên lý bắt gian lận:
- Mỗi lần sync, ta LƯU lại mọi tin nhắn (bản riêng của cpqc).
- Lần sync sau, tin nào đã lưu mà KHÔNG còn trong dữ liệu nguồn → bị XOÁ → đánh dấu
  + dựng cờ. (Sale xoá trên FB/Pancake sau bao nhiêu, bản cpqc vẫn còn để đối chiếu.)
- Hội thoại status='blocked' → cờ chặn khách.
"""
from __future__ import annotations

from typing import Any, Dict, List

from db import get_conn

_BLOCK_STATUS = {"blocked", "block", "banned", "spam"}


def _flag(cur, conv_key, flag_type, sale, page_id, order_ref, detail):
    cur.execute(
        """INSERT INTO fd_flags (conv_key, flag_type, assigned_sale, page_id, order_ref, detail)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (conv_key, flag_type) DO UPDATE SET
             assigned_sale=EXCLUDED.assigned_sale, page_id=EXCLUDED.page_id,
             order_ref=EXCLUDED.order_ref, detail=EXCLUDED.detail, detected_at=now()""",
        (conv_key, flag_type, sale, page_id, order_ref, detail),
    )


def ingest_conversation(conv: Dict[str, Any], messages: List[Dict[str, Any]],
                        source: str = "pancake") -> Dict[str, Any]:
    """Đổ 1 hội thoại + tin nhắn vào kho chuẩn + chạy phát hiện.

    conv: {conv_key(*), page_id, page_name, customer_id, customer_name,
           assigned_sale, status, last_customer_msg_at, last_sale_msg_at}
    messages: [{msg_key(*), direction('in'|'out'), content, sent_at}, ...]
    """
    conv_key = str(conv.get("conv_key") or "").strip()
    if not conv_key:
        return {"error": "thiếu conv_key"}
    cur_keys = [str(m.get("msg_key")) for m in messages if m.get("msg_key")]
    deleted_n = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fd_conversations
                     (conv_key, source, page_id, page_name, customer_id, customer_name,
                      customer_avatar, assigned_sale, status, total_messages,
                      last_customer_msg_at, last_sale_msg_at, last_synced_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (conv_key) DO UPDATE SET
                     source=EXCLUDED.source, page_id=EXCLUDED.page_id, page_name=EXCLUDED.page_name,
                     customer_id=EXCLUDED.customer_id, customer_name=EXCLUDED.customer_name,
                     customer_avatar=CASE WHEN EXCLUDED.customer_avatar <> ''
                                          THEN EXCLUDED.customer_avatar
                                          ELSE fd_conversations.customer_avatar END,
                     assigned_sale=EXCLUDED.assigned_sale, status=EXCLUDED.status,
                     total_messages=EXCLUDED.total_messages,
                     last_customer_msg_at=EXCLUDED.last_customer_msg_at,
                     last_sale_msg_at=EXCLUDED.last_sale_msg_at, last_synced_at=now()""",
                (conv_key, source, conv.get("page_id"), conv.get("page_name"),
                 conv.get("customer_id"), conv.get("customer_name"),
                 conv.get("customer_avatar") or "",
                 conv.get("assigned_sale"), conv.get("status"), len(messages),
                 conv.get("last_customer_msg_at"), conv.get("last_sale_msg_at")),
            )
            # Lưu bản riêng mọi tin (tin xuất hiện lại → bỏ cờ xoá)
            removed_keys = []
            for m in messages:
                mk = str(m.get("msg_key") or "").strip()
                if not mk:
                    continue
                cur.execute(
                    """INSERT INTO fd_messages (msg_key, conv_key, direction, content, sent_at)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (msg_key) DO UPDATE SET
                         content=EXCLUDED.content, direction=EXCLUDED.direction, deleted_at=NULL""",
                    (mk, conv_key, m.get("direction"), m.get("content"), m.get("sent_at")),
                )
                if m.get("is_removed"):
                    removed_keys.append(mk)
            # Pancake tự đánh dấu is_removed → tin đã bị xoá (tín hiệu trực tiếp)
            if removed_keys:
                cur.execute(
                    "UPDATE fd_messages SET deleted_at=now() WHERE conv_key=%s AND msg_key = ANY(%s)",
                    (conv_key, removed_keys),
                )
                _flag(cur, conv_key, "deleted_msg", conv.get("assigned_sale"),
                      conv.get("page_id"), None,
                      f"{len(removed_keys)} tin bị xoá (Pancake đánh dấu is_removed)")
                deleted_n += len(removed_keys)
            cur_keys = [k for k in cur_keys if k not in set(removed_keys)]
            # PHÁT HIỆN XOÁ: tin đã lưu (chưa đánh dấu) mà không còn trong lần kéo này
            cur.execute(
                "SELECT msg_key FROM fd_messages WHERE conv_key=%s AND deleted_at IS NULL",
                (conv_key,),
            )
            stored = set(r[0] for r in cur.fetchall())
            disappeared = list(stored - set(cur_keys))
            # SAFEGUARD chống vu oan: chỉ coi là "sale xoá" khi lần kéo này CÓ tin
            # (messages không rỗng). Kéo rỗng = hội thoại tải hụt/rate-limit, KHÔNG
            # phải khách/sale xoá → bỏ qua, khỏi dựng cờ oan. Ngoài ra nếu >90% tin
            # biến mất cùng lúc thì nghi tải hụt hơn là xoá → cũng bỏ qua.
            _pull_ok = bool(messages)
            _mass = stored and len(disappeared) >= max(5, int(len(stored) * 0.9))
            if disappeared and _pull_ok and not _mass:
                deleted_n = len(disappeared)
                cur.execute(
                    "UPDATE fd_messages SET deleted_at=now() WHERE conv_key=%s AND msg_key = ANY(%s)",
                    (conv_key, disappeared),
                )
                _flag(cur, conv_key, "deleted_msg", conv.get("assigned_sale"),
                      conv.get("page_id"), None,
                      f"{deleted_n} tin nhắn đã chụp bị biến mất (nghi sale xoá)")
            # PHÁT HIỆN CHẶN
            if str(conv.get("status") or "").lower() in _BLOCK_STATUS:
                _flag(cur, conv_key, "blocked", conv.get("assigned_sale"),
                      conv.get("page_id"), None, "Hội thoại bị đánh dấu chặn khách")
        conn.commit()
    return {"conv_key": conv_key, "messages": len(messages), "deleted_detected": deleted_n}


def list_flags(date_from=None, date_to=None, sale=None, page_id=None,
               flag_type=None, status="new") -> List[Dict[str, Any]]:
    """Danh sách cờ nghi vấn cho trang báo cáo."""
    where = ["1=1"]
    params: List[Any] = []
    if date_from:
        where.append("f.detected_at::date >= %s"); params.append(date_from)
    if date_to:
        where.append("f.detected_at::date <= %s"); params.append(date_to)
    if sale:
        where.append("f.assigned_sale = %s"); params.append(sale)
    if page_id:
        where.append("f.page_id = %s"); params.append(page_id)
    if flag_type:
        where.append("f.flag_type = %s"); params.append(flag_type)
    if status and status != "all":
        where.append("f.review_status = %s"); params.append(status)
    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT f.id, f.conv_key, f.flag_type, f.assigned_sale, f.page_id,
                          f.order_ref, f.detail, f.detected_at, f.review_status,
                          c.customer_name, c.page_name, c.total_messages,
                          COALESCE(c.customer_avatar,''), c.customer_id
                     FROM fd_flags f
                     LEFT JOIN fd_conversations c ON c.conv_key = f.conv_key
                    WHERE {' AND '.join(where)}
                    ORDER BY f.detected_at DESC LIMIT 1000""",
                params,
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "conv_key": r[1], "flag_type": r[2], "sale": r[3],
                    "page_id": r[4], "order_ref": r[5], "detail": r[6],
                    "detected_at": r[7], "review_status": r[8],
                    "customer_name": r[9], "page_name": r[10], "total_messages": r[11],
                    "customer_avatar": r[12], "customer_id": r[13],
                })
    return rows


def list_conversations(page_id=None, sale=None, q=None, only_deleted=False,
                       date_from=None, date_to=None, limit=500) -> List[Dict[str, Any]]:
    """Danh sách hội thoại ĐÃ CHỤP (để xem, không chỉ nghi vấn).
    date_from/date_to lọc theo NGÀY hoạt động cuối (tin khách/sale mới nhất)."""
    where = ["1=1"]
    params: List[Any] = []
    _act = "COALESCE(c.last_customer_msg_at, c.last_sale_msg_at, c.last_synced_at)::date"
    if page_id:
        where.append("c.page_id = %s"); params.append(page_id)
    if sale:
        where.append("c.assigned_sale = %s"); params.append(sale)
    if date_from:
        where.append(f"{_act} >= %s"); params.append(date_from)
    if date_to:
        where.append(f"{_act} <= %s"); params.append(date_to)
    if q:
        where.append("(c.customer_name ILIKE %s OR c.page_name ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if only_deleted:
        where.append("EXISTS (SELECT 1 FROM fd_messages m WHERE m.conv_key=c.conv_key AND m.deleted_at IS NOT NULL)")
    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT c.conv_key, c.page_id, c.page_name, c.customer_name,
                          COALESCE(c.customer_avatar,''), c.assigned_sale, c.status,
                          c.total_messages, c.last_synced_at,
                          (SELECT count(*) FROM fd_messages m WHERE m.conv_key=c.conv_key AND m.deleted_at IS NOT NULL) AS del_n
                     FROM fd_conversations c
                    WHERE {' AND '.join(where)}
                    ORDER BY c.last_synced_at DESC NULLS LAST LIMIT %s""",
                params + [limit],
            )
            for r in cur.fetchall():
                rows.append({
                    "conv_key": r[0], "page_id": r[1], "page_name": r[2],
                    "customer_name": r[3], "customer_avatar": r[4], "sale": r[5],
                    "status": r[6], "total_messages": r[7], "last_synced_at": r[8],
                    "deleted_n": r[9],
                })
    return rows


def conv_counts() -> Dict[str, int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), COALESCE(SUM(total_messages),0) FROM fd_conversations")
        c, m = cur.fetchone()
        cur.execute("SELECT count(DISTINCT conv_key) FROM fd_messages WHERE deleted_at IS NOT NULL")
        d = cur.fetchone()[0]
    return {"conversations": int(c or 0), "messages": int(m or 0), "with_deleted": int(d or 0)}


def counts() -> Dict[str, int]:
    out = {"deleted_msg": 0, "blocked": 0, "cut_contact": 0, "total": 0}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT flag_type, count(*) FROM fd_flags WHERE review_status='new' GROUP BY flag_type")
            for ft, n in cur.fetchall():
                out[ft] = n; out["total"] += n
    return out
