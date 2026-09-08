"""DB queries cho budget_chat."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def insert_message(cur, team_id: str, user_id: int, body: str,
                   zalo_msg_id: Optional[str] = None) -> int:
    """Insert tin chat. Nếu zalo_msg_id trùng → trả id row đã có (idempotent)."""
    if zalo_msg_id:
        cur.execute("SELECT id FROM budget_chat_messages WHERE zalo_msg_id = %s LIMIT 1", (zalo_msg_id,))
        r = cur.fetchone()
        if r:
            return int(r[0])
    cur.execute(
        """
        INSERT INTO budget_chat_messages (team_id, user_id, body, zalo_msg_id)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (team_id, user_id, body, zalo_msg_id or None),
    )
    return int(cur.fetchone()[0])


def insert_lan_message(cur, team_id: str, body: str) -> int:
    """Lan post tin nhắn vào team chat (user_id = LAN_USER_ID = 0).
    Đánh dấu parsed_at = NOW() để KHÔNG bị AI parse lại (Lan tự nói).
    """
    from modules.budget_chat.lan_personality import LAN_USER_ID
    cur.execute(
        """
        INSERT INTO budget_chat_messages (team_id, user_id, body, parsed_at, parsed_json, model_used)
        VALUES (%s, %s, %s, NOW(), '{"items":[],"by":"lan"}'::jsonb, 'lan')
        RETURNING id
        """,
        (team_id, LAN_USER_ID, body),
    )
    return int(cur.fetchone()[0])


def save_parse_result(cur, msg_id: int, parsed: dict):
    """Lưu parsed_json + tạo budget_items rows."""
    cur.execute(
        """
        UPDATE budget_chat_messages
           SET parsed_at = NOW(),
               parsed_json = %s,
               parse_error = %s,
               model_used = %s
         WHERE id = %s
        """,
        (
            json.dumps(parsed, ensure_ascii=False),
            parsed.get("error"),
            parsed.get("model"),
            msg_id,
        ),
    )
    # Build items — cho phép for_date NULL khi NV quên ghi ngày.
    # Item NULL date không vào tổng hợp đến khi NV bổ sung qua nút "Sửa ngày".
    for_date = parsed.get("for_date")  # có thể None
    if not parsed.get("items"):
        return 0

    cur.execute("SELECT team_id, user_id FROM budget_chat_messages WHERE id=%s", (msg_id,))
    row = cur.fetchone()
    if not row:
        return 0
    team_id, user_id = row[0], int(row[1])

    from modules.budget_chat.tk_matcher import match_tk_name
    inserted = 0
    for it in parsed["items"]:
        tk_raw = it.get("tk_name") or ""
        amt = int(it.get("amount_vnd") or 0)
        card = it.get("card_last4")
        if amt <= 0 or not tk_raw:
            continue
        aid, conf, _ = match_tk_name(tk_raw)
        cur.execute(
            """
            INSERT INTO budget_items
                (message_id, user_id, team_id, for_date, tk_name_raw,
                 fb_ad_account_id, card_last4, amount_vnd, match_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (msg_id, user_id, team_id, for_date, tk_raw, aid, card, amt, conf or 0.0),
        )
        inserted += 1
    return inserted


def list_messages(cur, team_id: str, limit: int = 100, after_id: int = 0) -> list[dict]:
    """List messages of a team, newest first. Filter by id > after_id để poll.
    user_id = 0 → Lan; enrich username/full_name kèm flag is_lan.
    """
    from modules.budget_chat.lan_personality import LAN_USER_ID, LAN_USERNAME, LAN_FULL_NAME
    cur.execute(
        """
        SELECT m.id, m.user_id, m.body, m.sent_at, m.parsed_json, m.parse_error,
               u.username, u.full_name
          FROM budget_chat_messages m
          LEFT JOIN users u ON u.id = m.user_id
         WHERE m.team_id = %s
           AND m.deleted_at IS NULL
           AND m.id > %s
         ORDER BY m.sent_at DESC, m.id DESC
         LIMIT %s
        """,
        (team_id, after_id, limit),
    )
    out = []
    for r in cur.fetchall():
        uid = int(r[1])
        is_lan = (uid == LAN_USER_ID)
        out.append({
            "id": int(r[0]),
            "user_id": uid,
            "body": r[2],
            "sent_at": r[3].isoformat() if r[3] else None,
            "parsed_json": r[4],
            "parse_error": r[5],
            "username": LAN_USERNAME if is_lan else (r[6] or ""),
            "full_name": LAN_FULL_NAME if is_lan else (r[7] or r[6] or ""),
            "is_lan": is_lan,
        })
    return out


def list_items_for_message(cur, msg_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT id, tk_name_raw, fb_ad_account_id, card_last4, amount_vnd,
               match_confidence, for_date
          FROM budget_items
         WHERE message_id = %s AND deleted_at IS NULL
         ORDER BY id
        """,
        (msg_id,),
    )
    return [{
        "id": int(r[0]),
        "tk_name_raw": r[1],
        "fb_ad_account_id": r[2],
        "card_last4": r[3],
        "amount_vnd": int(r[4]),
        "match_confidence": float(r[5] or 0),
        "for_date": r[6].isoformat() if r[6] else None,
    } for r in cur.fetchall()]


def sum_items_by_team_date(cur, team_id: str, for_date: date) -> dict:
    """Trả {total, count_items, count_tks, count_users}."""
    cur.execute(
        """
        SELECT COALESCE(SUM(amount_vnd),0) AS total,
               COUNT(*)                    AS items,
               COUNT(DISTINCT COALESCE(fb_ad_account_id, tk_name_raw)) AS tks,
               COUNT(DISTINCT user_id)     AS users
          FROM budget_items
         WHERE team_id = %s AND for_date = %s AND deleted_at IS NULL
        """,
        (team_id, for_date),
    )
    r = cur.fetchone()
    return {
        "total": int(r[0] or 0),
        "tk_count": int(r[1] or 0),
        "tks": int(r[2] or 0),
        "users": int(r[3] or 0),
    }


def summary_by_team(cur, for_date: date) -> list[dict]:
    """Tổng hợp cross-team — dùng cho tab Tổng hợp.

    Split tk_name_raw thành 2 loại:
    - cty: TK QC công ty (tên có chứa 'th' không phân biệt hoa thường).
    - hkd: TK HKĐ (còn lại).
    """
    cur.execute(
        """
        SELECT team_id,
               COALESCE(SUM(amount_vnd),0)                                          AS total,
               COUNT(*)                                                             AS items,
               COUNT(DISTINCT user_id)                                              AS users,
               COALESCE(SUM(amount_vnd) FILTER (WHERE tk_name_raw ~* 'th[0-9]'),0) AS total_cty,
               COUNT(*)                FILTER (WHERE tk_name_raw ~* 'th[0-9]')    AS items_cty,
               COALESCE(SUM(amount_vnd) FILTER (WHERE tk_name_raw !~* 'th[0-9]'),0) AS total_hkd,
               COUNT(*)                FILTER (WHERE tk_name_raw !~* 'th[0-9]')    AS items_hkd
          FROM budget_items
         WHERE for_date = %s AND deleted_at IS NULL
         GROUP BY team_id
         ORDER BY SUM(amount_vnd) DESC
        """,
        (for_date,),
    )
    return [{
        "team_id": r[0],
        "total": int(r[1] or 0),
        "tk_count": int(r[2] or 0),
        "users": int(r[3] or 0),
        "total_cty": int(r[4] or 0),
        "tk_cty": int(r[5] or 0),
        "total_hkd": int(r[6] or 0),
        "tk_hkd": int(r[7] or 0),
    } for r in cur.fetchall()]


def summary_tk_type(cur, for_date: date) -> dict:
    """Tổng theo loại TK cho ngày: cty (tên TK chứa 'th') vs hkd."""
    cur.execute(
        """
        SELECT
          COALESCE(SUM(amount_vnd) FILTER (WHERE tk_name_raw ~* 'th[0-9]'),0) AS total_cty,
          COUNT(*)                FILTER (WHERE tk_name_raw ~* 'th[0-9]')    AS tk_cty,
          COALESCE(SUM(amount_vnd) FILTER (WHERE tk_name_raw !~* 'th[0-9]'),0) AS total_hkd,
          COUNT(*)                FILTER (WHERE tk_name_raw !~* 'th[0-9]')    AS tk_hkd
        FROM budget_items
        WHERE for_date = %s AND deleted_at IS NULL
        """,
        (for_date,),
    )
    r = cur.fetchone() or (0, 0, 0, 0)
    return {
        "total_cty": int(r[0] or 0),
        "tk_cty": int(r[1] or 0),
        "total_hkd": int(r[2] or 0),
        "tk_hkd": int(r[3] or 0),
    }


def list_items_for_export(cur, date_from: date, date_to: date,
                          team_id: Optional[str] = None) -> list[dict]:
    """List items trong khoảng ngày để xuất Excel.

    Trả list dict: tk_name_raw, card_last4, for_date, amount_vnd, team_id, team_name, who.
    Bao gồm cả items thiếu ngày (for_date IS NULL) để kế toán thấy bị thiếu.
    """
    where = """
        WHERE bi.deleted_at IS NULL
          AND (bi.for_date IS NULL OR (bi.for_date >= %s AND bi.for_date <= %s))
    """
    params: list = [date_from, date_to]
    if team_id:
        where += " AND bi.team_id = %s"
        params.append(team_id)
    cur.execute(
        f"""
        SELECT bi.tk_name_raw, bi.card_last4, bi.for_date, bi.amount_vnd,
               bi.team_id, COALESCE(t.team_name, bi.team_id) AS team_name,
               COALESCE(u.full_name, u.username, '#'||bi.user_id::text) AS who
          FROM budget_items bi
     LEFT JOIN users u ON u.id = bi.user_id
     LEFT JOIN teams t ON t.team_code = bi.team_id
          {where}
         ORDER BY bi.team_id, bi.for_date NULLS LAST, bi.tk_name_raw
        """,
        params,
    )
    return [{
        "tk_name_raw": r[0] or "",
        "card_last4": r[1] or "",
        "for_date": r[2],  # date or None
        "amount_vnd": int(r[3] or 0),
        "team_id": r[4] or "",
        "team_name": r[5] or "",
        "who": r[6] or "",
    } for r in cur.fetchall()]


def list_items_by_tk_type(cur, for_date: date, tk_type: str,
                          team_id: Optional[str] = None) -> list[dict]:
    """List items theo loại TK (cty/hkd) trong ngày, kèm tên NV + team."""
    where_type = "tk_name_raw ~* 'th[0-9]'" if tk_type == "cty" else "tk_name_raw !~* 'th[0-9]'"
    where = f"WHERE bi.for_date = %s AND bi.deleted_at IS NULL AND {where_type}"
    params: list = [for_date]
    if team_id:
        where += " AND bi.team_id = %s"
        params.append(team_id)
    cur.execute(
        f"""
        SELECT bi.tk_name_raw, bi.card_last4, bi.amount_vnd, bi.team_id,
               COALESCE(u.full_name, u.username, '#'||bi.user_id::text) AS who
          FROM budget_items bi
     LEFT JOIN users u ON u.id = bi.user_id
          {where}
         ORDER BY bi.amount_vnd DESC, bi.id DESC
        """,
        params,
    )
    return [{
        "tk_name_raw": r[0],
        "card_last4": r[1],
        "amount_vnd": int(r[2] or 0),
        "team_id": r[3],
        "who": r[4],
    } for r in cur.fetchall()]


def summary_by_user(cur, for_date: date, team_id: Optional[str] = None) -> list[dict]:
    where = "WHERE bi.for_date = %s AND bi.deleted_at IS NULL"
    params: list = [for_date]
    if team_id:
        where += " AND bi.team_id = %s"
        params.append(team_id)
    cur.execute(
        f"""
        SELECT bi.user_id, bi.team_id,
               COALESCE(SUM(bi.amount_vnd),0) AS total,
               COUNT(*)                       AS items,
               MIN(m.sent_at)                 AS first_sent,
               u.full_name, u.username
          FROM budget_items bi
          LEFT JOIN budget_chat_messages m ON m.id = bi.message_id
          LEFT JOIN users u ON u.id = bi.user_id
          {where}
         GROUP BY bi.user_id, bi.team_id, u.full_name, u.username
         ORDER BY SUM(bi.amount_vnd) DESC
        """,
        params,
    )
    return [{
        "user_id": int(r[0]),
        "team_id": r[1],
        "total": int(r[2] or 0),
        "tk_count": int(r[3] or 0),
        "first_sent": r[4].isoformat() if r[4] else None,
        "full_name": r[5] or r[6] or "",
        "username": r[6] or "",
    } for r in cur.fetchall()]


def list_users_should_report(cur, for_date: date, team_code: Optional[str] = None) -> list[dict]:
    """NV phải báo = NV có uaa active trong for_date.

    `team_code` filter: tên team_code (vd 'team-nam'). Convert qua bảng teams.
    Trả [{user_id, full_name, username, team_id (= team_code TEXT), reported(bool)}]
    """
    where = """
        WHERE a.assigned_from <= %s
          AND (a.assigned_to IS NULL OR a.assigned_to >= %s)
    """
    params: list = [for_date, for_date]
    if team_code:
        where += " AND t.team_code = %s"
        params.append(team_code)

    cur.execute(
        f"""
        WITH should_report AS (
            SELECT DISTINCT a.user_id, u.full_name, u.username,
                            COALESCE(t.team_code, '') AS team_code
              FROM user_ad_account_assignments a
              JOIN users u ON u.id = a.user_id
              JOIN teams t ON t.id = u.team_id
                          AND t.team_type = 'kinh_doanh'
              {where}
        ),
        reported AS (
            SELECT DISTINCT user_id FROM budget_items
             WHERE for_date = %s AND deleted_at IS NULL
        )
        SELECT s.user_id, s.full_name, s.username, s.team_code,
               (r.user_id IS NOT NULL) AS reported
          FROM should_report s
          LEFT JOIN reported r ON r.user_id = s.user_id
         ORDER BY s.team_code, s.full_name
        """,
        params + [for_date],
    )
    return [{
        "user_id": int(r[0]),
        "full_name": r[1] or r[2] or "",
        "username": r[2] or "",
        "team_id": r[3] or "",   # = team_code
        "reported": bool(r[4]),
    } for r in cur.fetchall()]


def mark_read(cur, user_id: int, team_id: str, last_seen_id: int):
    cur.execute(
        """
        INSERT INTO budget_chat_reads (user_id, team_id, last_seen_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, team_id) DO UPDATE
          SET last_seen_id = GREATEST(budget_chat_reads.last_seen_id, EXCLUDED.last_seen_id),
              updated_at = NOW()
        """,
        (user_id, team_id, last_seen_id),
    )


def get_unread_count(cur, user_id: int, team_id: str) -> int:
    cur.execute(
        """
        SELECT COUNT(*)
          FROM budget_chat_messages m
         WHERE m.team_id = %s
           AND m.deleted_at IS NULL
           AND m.id > COALESCE(
                (SELECT last_seen_id FROM budget_chat_reads
                  WHERE user_id = %s AND team_id = %s), 0)
        """,
        (team_id, user_id, team_id),
    )
    r = cur.fetchone()
    return int(r[0] or 0) if r else 0


def edit_item(cur, item_id: int, editor_user_id: int,
              tk_name_raw: Optional[str] = None,
              card_last4: Optional[str] = None,
              amount_vnd: Optional[int] = None,
              for_date: Optional[str] = None) -> bool:
    """Sửa 1 budget_item. Tự match lại fb_ad_account_id nếu tk_name_raw đổi."""
    sets = ["edited_at = NOW()", "edited_by = %s"]
    params: list = [editor_user_id]
    if tk_name_raw is not None:
        from modules.budget_chat.tk_matcher import match_tk_name
        aid, conf, _ = match_tk_name(tk_name_raw)
        sets += ["tk_name_raw = %s", "fb_ad_account_id = %s", "match_confidence = %s"]
        params += [tk_name_raw, aid, conf or 0.0]
    if card_last4 is not None:
        sets.append("card_last4 = %s")
        params.append(card_last4 or None)
    if amount_vnd is not None:
        sets.append("amount_vnd = %s")
        params.append(int(amount_vnd))
    if for_date is not None:
        sets.append("for_date = %s")
        params.append(for_date)
    params.append(item_id)
    cur.execute(
        f"UPDATE budget_items SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
        params,
    )
    return cur.rowcount > 0


def fill_missing_date_for_user(cur, user_id: int, team_id: str,
                               for_date: str, editor_user_id: int,
                               within_hours: int = 24) -> list[dict]:
    """Fill for_date cho tất cả items của user trong team này đang NULL date
    và mới tạo trong `within_hours` giờ. Trả list items đã update."""
    cur.execute(
        """
        UPDATE budget_items
           SET for_date = %s, edited_at = NOW(), edited_by = %s
         WHERE user_id = %s AND team_id = %s
               AND for_date IS NULL AND deleted_at IS NULL
               AND created_at >= NOW() - INTERVAL '%s hours'
        RETURNING id, tk_name_raw, card_last4, amount_vnd
        """,
        (for_date, editor_user_id, user_id, team_id, within_hours),
    )
    rows = cur.fetchall()
    return [{
        "id": int(r[0]),
        "tk_name_raw": r[1],
        "card_last4": r[2],
        "amount_vnd": int(r[3]),
    } for r in rows]


def soft_delete_item(cur, item_id: int, editor_user_id: int) -> bool:
    cur.execute(
        """
        UPDATE budget_items
           SET deleted_at = NOW(), edited_by = %s
         WHERE id = %s AND deleted_at IS NULL
        """,
        (editor_user_id, item_id),
    )
    return cur.rowcount > 0
