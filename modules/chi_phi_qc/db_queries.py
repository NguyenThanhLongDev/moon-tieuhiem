"""Truy vấn DB cho trang Chi phí QC.

Tách từ __init__.py — mọi hàm chạm PostgreSQL (SELECT/INSERT/ensure-table)
gom về đây. Chỉ phụ thuộc helpers.py (tầng dưới), không import logic/routes.
"""
from __future__ import annotations

import logging
from datetime import date

from modules.chi_phi_qc.helpers import (
    _get_team_members,
    _get_user_by_id,
    _is_full_view,
    _is_senior,
    _load_users_json,
)

logger = logging.getLogger(__name__)


# ── Mapping shop/user → tài khoản quảng cáo ──────────────────────────
def _shop_keys_to_account_ids(shop_keys: list, date_from: str = "", date_to: str = "") -> list:
    """Map shop_keys → fb_ad_account_id via fb_ad_account_mappings.

    Date-aware: có date_from/date_to → lấy mapping theo CỬA SỔ HIỆU LỰC giao với kỳ
    (bảng versioned migration 036; row đóng có status='inactive' nên KHÔNG filter
    status ở nhánh này). Vd TK AnhTh20.1 map shop Nam Leader từ 1/6: lọc tháng 5
    KHÔNG trả TK này cho shop Nam Leader → spend tháng 5 ở lại với chủ cũ (anhth).

    Không có ngày → giữ hành vi cũ: mapping active hiện tại.
    """
    if not shop_keys:
        return []
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if date_from and date_to:
                    cur.execute("""
                        SELECT DISTINCT m.fb_ad_account_id
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE s.shop_key = ANY(%s)
                          AND m.assigned_from <= %s::date
                          AND (m.assigned_to IS NULL OR m.assigned_to >= %s::date)
                    """, (shop_keys, date_to, date_from))
                else:
                    cur.execute("""
                        SELECT DISTINCT m.fb_ad_account_id
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE s.shop_key = ANY(%s) AND m.status = 'active'
                    """, (shop_keys,))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_shop_keys_to_account_ids error: %s", exc)
        return []


def _user_direct_account_ids(user_id, date_from: str = "", date_to: str = "") -> list:
    """Return ad_account_ids directly assigned to this user trong khoảng [date_from, date_to].

    Date-aware: đọc từ `user_ad_account_assignments` (uaa) và filter theo overlap với date range.
    → Báo cáo lịch sử (lọc ngày trước) hiển thị đúng chủ TẠI THỜI ĐIỂM ĐÓ, không phải chủ hiện tại.

    Nếu date_from/date_to rỗng → fallback: trả về TK active (assigned_to IS NULL) hiện tại.
    Dùng cho ngữ cảnh không có date filter (vd: cache/permissions).
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if date_from and date_to:
                    # Overlap rule: TK có uaa giao với khoảng filter
                    # (uaa.assigned_from <= date_to) AND (uaa.assigned_to IS NULL OR uaa.assigned_to >= date_from)
                    cur.execute("""
                        SELECT DISTINCT ad_account_id
                          FROM user_ad_account_assignments
                         WHERE user_id = %s
                           AND assigned_from <= %s::date
                           AND (assigned_to IS NULL OR assigned_to >= %s::date)
                    """, (int(user_id), date_to, date_from))
                else:
                    # Fallback: chỉ TK active hiện tại
                    cur.execute("""
                        SELECT DISTINCT ad_account_id
                          FROM user_ad_account_assignments
                         WHERE user_id = %s AND assigned_to IS NULL
                    """, (int(user_id),))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_user_direct_account_ids error: %s", exc)
        return []

# ── Page & chi phí theo ngày ─────────────────────────────────────────
def _active_pages_for_user(user_id, role, allowed_account_ids=None):
    """Return distinct active pages. Each page appears once with a JSON-agg of ad accounts.
    allowed_account_ids=None → no restriction (admin/ketoan).
    allowed_account_ids=[...] → filter pages to those mapped to these ad account IDs (leader/staff).
    """
    _PAGE_AGG = """
        SELECT
            p.page_id,
            p.page_name,
            p.picture_url,
            p.page_category,
            COALESCE(
                json_agg(
                    json_build_object(
                        'ad_account_id',   m.ad_account_id,
                        'ad_account_name', m.ad_account_name,
                        'shop_key',        m.shop_key
                    ) ORDER BY m.ad_account_name
                ) FILTER (WHERE m.ad_account_id IS NOT NULL),
                '[]'::json
            ) AS ad_accounts
        FROM fb_pages p
        {join_clause}
        WHERE p.is_active = TRUE {extra_where}
        GROUP BY p.page_id, p.page_name, p.picture_url, p.page_category
        ORDER BY p.page_name
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_full_view(role):
                    # Admin/ketoan/manager: all pages, no filter
                    sql = _PAGE_AGG.format(
                        join_clause="LEFT JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                        extra_where="",
                    )
                    cur.execute(sql)
                elif role == "leader":
                    # Leader: pages whose ad accounts belong to their team
                    if not allowed_account_ids:
                        return []
                    sql = _PAGE_AGG.format(
                        join_clause="JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                        extra_where="AND m.ad_account_id = ANY(%s)",
                    )
                    cur.execute(sql, (allowed_account_ids,))
                else:
                    # Staff: use account-based filter if they have mapped accounts
                    # (same logic as leader), otherwise return empty (fb_page_assignments
                    # uses bigint user_id which is incompatible with string user IDs)
                    if allowed_account_ids:
                        sql = _PAGE_AGG.format(
                            join_clause="JOIN fb_page_ad_account_map m ON m.page_id = p.page_id",
                            extra_where="AND m.ad_account_id = ANY(%s)",
                        )
                        cur.execute(sql, (allowed_account_ids,))
                    else:
                        return []
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_active_pages_for_user error: %s", exc)
        return []


def _daily_spend_by_page(date_from: str, date_to: str, user_id, role):
    """Return spend aggregated by page_id for a given date range (inclusive).
    Returns dict: {page_id: {win_chua, win_co, test_chua, test_co, tong, count}}
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    if role == "leader":
                        # Use JSON-based team members (DB users table may be empty)
                        team_ids = _get_leader_team_member_ids(user_id)
                        placeholders = ",".join(["%s"] * len(team_ids))
                        user_filter = f"AND d.user_id IN ({placeholders})"
                        params = [date_from, date_to] + team_ids
                    else:
                        user_filter = ""
                        params = [date_from, date_to]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [date_from, date_to, user_id]

                cur.execute(f"""
                    SELECT
                        d.page_id,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.tien_chua_thue ELSE 0 END), 0) as win_chua,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_win'  THEN d.thanh_tien     ELSE 0 END), 0) as win_co,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.tien_chua_thue ELSE 0 END), 0) as test_chua,
                        COALESCE(SUM(CASE WHEN d.phan_loai='ma_test' THEN d.thanh_tien     ELSE 0 END), 0) as test_co,
                        COALESCE(SUM(d.thanh_tien), 0) as tong,
                        COUNT(d.id) as count
                    FROM fb_page_spend_declarations d
                    WHERE d.spend_date BETWEEN %s AND %s {user_filter}
                    GROUP BY d.page_id
                """, params)
                result = {}
                for row in cur.fetchall():
                    pid = row[0]
                    result[pid] = {
                        "win_chua": float(row[1]),
                        "win_co":   float(row[2]),
                        "test_chua":float(row[3]),
                        "test_co":  float(row[4]),
                        "tong":     float(row[5]),
                        "count":    int(row[6]),
                    }
                return result
    except Exception as exc:
        logger.error("_daily_spend_by_page error: %s", exc)
        return {}


def _declarations_for_page_date(page_id, spend_date, user_id, role):
    """Return all declarations for a specific page + date (for the detail modal)."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    user_filter = ""
                    params = [page_id, spend_date]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [page_id, spend_date, user_id]

                cur.execute(f"""
                    SELECT d.id, d.spend_date, d.ad_account_id, d.ad_account_name,
                           d.phan_loai, d.tien_chua_thue, d.thue_rate, d.thanh_tien,
                           d.note, d.created_at, d.user_id, d.page_name_manual
                    FROM fb_page_spend_declarations d
                    WHERE d.page_id = %s AND d.spend_date = %s {user_filter}
                    ORDER BY d.created_at DESC
                """, params)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                # Enrich user info from users.json (users DB table may be empty)
                uid_to_user = {str(u.get("id", "")): u for u in _load_users_json()}
                for row in rows:
                    uid = str(row.get("user_id") or "")
                    u = uid_to_user.get(uid, {})
                    row["full_name"] = u.get("full_name") or u.get("username") or uid
                    row["username"]  = u.get("username") or uid
                return rows
    except Exception as exc:
        logger.error("_declarations_for_page_date error: %s", exc)
        return []


def _page_ad_accounts(page_id: str):
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ad_account_id, ad_account_name, shop_key
                    FROM fb_page_ad_account_map
                    WHERE page_id = %s
                    ORDER BY ad_account_name
                """, (page_id,))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_page_ad_accounts error: %s", exc)
        return []


def _kpi_spend_by_page(date_from: str, date_to: str) -> dict:
    """Get actual Facebook spend from fb_ads_daily_metrics per page over a date range.

    Links via:
      1. fb_page_ad_account_map.ad_account_id  → fb_ads_daily_metrics.fb_ad_account_id  (direct)
      2. fb_page_ad_account_map.shop_key → shops.shop_key → shops.id → fb_ads_daily_metrics.shop_id
    Returns {page_id: {cp_ads_fb, cp_ads_fb_vat, impressions, clicks, fb_account_names}}
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # `fb_ads_daily_metrics` lưu trùng spend cho mỗi shop nếu ad account
                # map vào nhiều shop (cùng số liệu nhân N). Direct-match: dedup bằng MAX
                # THEO TỪNG NGÀY rồi SUM qua các ngày (range), tránh nhân trùng per-shop.
                # shop_key fallback giữ SUM vì trong 1 shop các ad_account_id khác nhau
                # là row khác nhau (không trùng).
                cur.execute("""
                    SELECT
                        m.page_id,
                        COALESCE(
                            -- direct ad_account_id match: MAX/ngày rồi SUM cả range
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.spend) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            -- fallback via shop_key
                            (SELECT SUM(dm.spend)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS cp_ads_fb,
                        COALESCE(
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.impressions) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            (SELECT SUM(dm.impressions)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS impressions,
                        COALESCE(
                            (SELECT SUM(d.mx)
                             FROM (SELECT dm.metric_date, MAX(dm.clicks) AS mx
                                   FROM fb_ads_daily_metrics dm
                                   WHERE dm.fb_ad_account_id = m.ad_account_id
                                     AND dm.metric_date BETWEEN %s AND %s
                                   GROUP BY dm.metric_date) d),
                            (SELECT SUM(dm.clicks)
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            0
                        ) AS clicks,
                        COALESCE(
                            (SELECT string_agg(DISTINCT dm.account_name, ', ')
                             FROM fb_ads_daily_metrics dm
                             WHERE dm.fb_ad_account_id = m.ad_account_id
                               AND dm.metric_date BETWEEN %s AND %s),
                            (SELECT string_agg(DISTINCT dm.account_name, ', ')
                             FROM fb_ads_daily_metrics dm
                             JOIN shops s ON s.id = dm.shop_id
                             WHERE s.shop_key = m.shop_key
                               AND dm.metric_date BETWEEN %s AND %s),
                            ''
                        ) AS fb_account_names
                    FROM fb_page_ad_account_map m
                """, (date_from, date_to) * 8)
                result = {}
                for row in cur.fetchall():
                    fb = float(row[1] or 0)
                    result[row[0]] = {
                        "cp_ads_fb":     fb,
                        "cp_ads_fb_vat": round(fb * 1.113, 0),
                        "impressions":   int(row[2] or 0),
                        "clicks":        int(row[3] or 0),
                        "fb_account_names": row[4] or "",
                    }
                return result
    except Exception as exc:
        logger.error("_kpi_spend_by_page error: %s", exc)
        return {}


def _my_ad_accounts(user_id):
    """Return ad accounts mapped to this user from user_ad_account_map."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, ad_account_id, ad_account_name
                    FROM user_ad_account_map
                    WHERE user_id = %s
                    ORDER BY ad_account_name
                """, (user_id,))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_my_ad_accounts error: %s", exc)
        return []


def _load_account_meta(date_from: str = "", date_to: str = "") -> dict:
    """Return dict keyed by ad_account_id with name + assigned employee info.

    Merges data from:
      1. fb_ads_daily_metrics.account_name  (real names from FB API)
      2. user_ad_account_assignments (time-aware: NV phụ trách TK tại ngày đang xem)
         Fallback: user_ad_account_map nếu không có date range
    Result: {account_id: {"name": str, "owner_id": int|None, "owner_name": str|None, "owner_username": str|None}}
    """
    meta: dict = {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Account names from metrics table (populated by sync_facebook_ads_to_db)
                cur.execute("""
                    SELECT DISTINCT fb_ad_account_id, account_name
                    FROM fb_ads_daily_metrics
                    WHERE account_name IS NOT NULL AND account_name <> ''
                """)
                for aid, aname in cur.fetchall():
                    meta.setdefault(aid, {})["name"] = aname

                # 2. Account names from fb_ad_account_info cache (populated by sync_fb_ads_by_page)
                try:
                    cur.execute("""
                        SELECT ad_account_id, account_name
                        FROM fb_ad_account_info
                        WHERE account_name IS NOT NULL AND account_name <> ''
                    """)
                    for aid, aname in cur.fetchall():
                        meta.setdefault(aid, {}).setdefault("name", aname)
                except Exception:
                    pass  # table may not exist yet

                # 3. Employee assignment — time-aware nếu có date range,
                #    fallback user_ad_account_map (snapshot hiện tại) khi không có ngày.
                uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
                if date_from and date_to:
                    # Time-aware: lấy NV phụ trách TK trong khoảng [date_from, date_to]
                    # Nếu giữa kỳ đổi NV → lấy NV có window CUỐI (ORDER BY assigned_from DESC)
                    # để ưu tiên NV mới nhất trong khoảng (DISTINCT ON lấy dòng đầu tiên theo thứ tự).
                    cur.execute("""
                        SELECT DISTINCT ON (ad_account_id)
                               ad_account_id, ad_account_name, user_id
                          FROM user_ad_account_assignments
                         WHERE assigned_from <= %s::date
                           AND (assigned_to IS NULL OR assigned_to >= %s::date)
                         ORDER BY ad_account_id, assigned_from DESC
                    """, (date_to, date_from))
                else:
                    # Fallback: snapshot hiện tại từ user_ad_account_map
                    cur.execute("""
                        SELECT ad_account_id, ad_account_name, user_id
                          FROM user_ad_account_map
                         ORDER BY ad_account_id
                    """)
                for aid, aname, uid in cur.fetchall():
                    entry = meta.setdefault(aid, {})
                    if "name" not in entry and aname:
                        entry["name"] = aname
                    u = uid_to_user.get(str(uid) if uid else "", {})
                    entry["owner_id"]       = str(uid) if uid else None
                    entry["owner_name"]     = u.get("full_name") or u.get("username") or str(uid or "")
                    entry["owner_username"] = u.get("username") or str(uid or "")

                # 3. Fallback: nếu ad account chưa có owner → lấy từ fb_ad_account_mappings → shops
                #    (shop_name = username NV, vì mỗi shop gắn 1 NV)
                try:
                    cur.execute("""
                        SELECT m.fb_ad_account_id, s.shop_name, s.shop_key
                        FROM fb_ad_account_mappings m
                        JOIN shops s ON s.id = m.shop_id
                        WHERE m.status = 'active'
                    """)
                    for aid, shop_name, shop_key in cur.fetchall():
                        entry = meta.setdefault(aid, {})
                        if not entry.get("owner_name"):
                            display = shop_name or shop_key or ""
                            entry["owner_id"]       = None
                            entry["owner_name"]     = display
                            entry["owner_username"] = display
                except Exception as _fe:
                    logger.debug("fb_ad_account_mappings fallback skip: %s", _fe)
    except Exception as exc:
        logger.error("_load_account_meta error: %s", exc)
    return meta

# ── Time-aware: tách TK theo từng NV phụ trách trong khoảng lọc ──
def _account_owner_segments(date_from: str, date_to: str) -> dict:
    """Trả về {ad_account_id: [list segments]} cho các TK có assignment overlap
    với khoảng [date_from, date_to]. Segment được clamp về khoảng lọc.

    Mỗi segment: {owner_id, owner_name, owner_username, name_snapshot,
                  seg_from (date), seg_to (date)}
    Dùng để: khi TK đổi tay GIỮA kỳ → trả về nhiều segments → UI tách thành nhiều dòng.
    """
    segments: dict = {}
    if not date_from or not date_to:
        return segments
    try:
        from db import get_conn
        df = date.fromisoformat(date_from)
        dt = date.fromisoformat(date_to)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ad_account_id, ad_account_name, user_id, assigned_from, assigned_to
                      FROM user_ad_account_assignments
                     WHERE assigned_from <= %s::date
                       AND (assigned_to IS NULL OR assigned_to >= %s::date)
                     ORDER BY ad_account_id, assigned_from
                """, (date_to, date_from))
                uid_to_user = {str(u.get("id","")): u for u in _load_users_json()}
                for aid, aname, uid, a_from, a_to in cur.fetchall():
                    user_info = uid_to_user.get(str(uid) if uid else "", {})
                    seg_from = max(a_from, df) if a_from else df
                    seg_to   = min(a_to, dt) if a_to else dt
                    if seg_from > seg_to:
                        continue
                    segments.setdefault(aid, []).append({
                        "owner_id":       str(uid) if uid else None,
                        "owner_name":     user_info.get("full_name") or user_info.get("username") or str(uid or ""),
                        "owner_username": user_info.get("username") or str(uid or ""),
                        "name_snapshot":  aname or "",
                        "seg_from":       seg_from,
                        "seg_to":         seg_to,
                    })
    except Exception as exc:
        logger.error("_account_owner_segments error: %s", exc)
    return segments


def _page_account_spend_in_range(page_id: str, account_id: str,
                                 seg_from, seg_to) -> dict:
    """Lấy spend/impressions/clicks cho 1 cặp (page, account) trong sub-range.
    Dùng khi tách dòng theo segment NV phụ trách.
    """
    out = {"spend": 0.0, "impressions": 0, "clicks": 0}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(spend),0), COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0)
                      FROM fb_ads_page_daily_spend
                     WHERE page_id = %s AND fb_ad_account_id = %s
                       AND metric_date BETWEEN %s AND %s
                """, (page_id, account_id, seg_from, seg_to))
                row = cur.fetchone()
                if row:
                    out = {"spend": float(row[0] or 0),
                           "impressions": int(row[1] or 0),
                           "clicks": int(row[2] or 0)}
    except Exception as exc:
        logger.error("_page_account_spend_in_range error: %s", exc)
    return out



def _auto_pages_for_date(date_from: str, date_to: str, allowed_account_ids: list | None = None) -> list:
    """Load pages discovered from fb_ads_page_daily_spend for a date range (inclusive).
    allowed_account_ids=None → no filter (admin/ketoan).
    allowed_account_ids=[]   → no accounts allowed (empty result).
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if allowed_account_ids is None:
                    cur.execute("""
                        SELECT
                            s.page_id,
                            COALESCE(
                                NULLIF(fpn.name, ''),
                                NULLIF(p.page_name, ''),
                                NULLIF(MAX(s.page_name), ''),
                                'Page ' || RIGHT(s.page_id, 8)
                            ) AS page_name,
                            p.picture_url,
                            p.page_category,
                            s.fb_ad_account_id,
                            SUM(s.spend)       AS spend,
                            SUM(s.impressions) AS impressions,
                            SUM(s.clicks)      AS clicks
                        FROM fb_ads_page_daily_spend s
                        LEFT JOIN fb_pages p ON p.page_id = s.page_id
                        LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                        WHERE s.metric_date BETWEEN %s AND %s
                        GROUP BY s.page_id, fpn.name, p.page_name, p.picture_url, p.page_category, s.fb_ad_account_id
                        ORDER BY SUM(s.spend) DESC
                    """, (date_from, date_to))
                elif not allowed_account_ids:
                    return []
                else:
                    cur.execute("""
                        SELECT
                            s.page_id,
                            COALESCE(
                                NULLIF(fpn.name, ''),
                                NULLIF(p.page_name, ''),
                                NULLIF(MAX(s.page_name), ''),
                                'Page ' || RIGHT(s.page_id, 8)
                            ) AS page_name,
                            p.picture_url,
                            p.page_category,
                            s.fb_ad_account_id,
                            SUM(s.spend)       AS spend,
                            SUM(s.impressions) AS impressions,
                            SUM(s.clicks)      AS clicks
                        FROM fb_ads_page_daily_spend s
                        LEFT JOIN fb_pages p ON p.page_id = s.page_id
                        LEFT JOIN fb_page_names fpn ON fpn.page_id = s.page_id
                        WHERE s.metric_date BETWEEN %s AND %s
                          AND s.fb_ad_account_id = ANY(%s)
                        GROUP BY s.page_id, fpn.name, p.page_name, p.picture_url, p.page_category, s.fb_ad_account_id
                        ORDER BY SUM(s.spend) DESC
                    """, (date_from, date_to, allowed_account_ids))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_auto_pages_for_date error: %s", exc)
        return []


def _has_page_spend_table() -> bool:
    """Check if fb_ads_page_daily_spend table exists."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'fb_ads_page_daily_spend'
                """)
                return cur.fetchone() is not None
    except Exception:
        return False


def _ensure_auto_phan_loai_table():
    """Create fb_page_auto_phan_loai table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_page_auto_phan_loai (
                        page_id    VARCHAR(100) NOT NULL,
                        date       DATE         NOT NULL,
                        phan_loai  VARCHAR(20)  NOT NULL DEFAULT 'ma_win',
                        updated_by VARCHAR(60),
                        updated_at TIMESTAMP    DEFAULT NOW(),
                        PRIMARY KEY (page_id, date)
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_page_auto_phan_loai: %s", e)


def _ensure_sync_log_table():
    """Create fb_sync_log table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_sync_log (
                        id         SERIAL PRIMARY KEY,
                        sync_date  DATE         NOT NULL,
                        synced_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
                        synced_by  VARCHAR(60),
                        status     VARCHAR(20)  NOT NULL DEFAULT 'ok',
                        message    TEXT
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_sync_log: %s", e)


def _log_sync(sync_date: str, synced_by: str, status: str = "ok", message: str = ""):
    """Record a sync event in fb_sync_log."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fb_sync_log (sync_date, synced_by, status, message)
                    VALUES (%s, %s, %s, %s)
                """, (sync_date, synced_by, status, message[:500] if message else ""))
            conn.commit()
    except Exception as e:
        logger.warning("Could not log sync: %s", e)


def _get_last_sync(sync_date: str) -> dict | None:
    """Return the most recent sync record for a given date, or None."""
    try:
        _ensure_sync_log_table()
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT synced_at, synced_by, status
                    FROM fb_sync_log
                    WHERE sync_date = %s AND status = 'ok'
                    ORDER BY synced_at DESC
                    LIMIT 1
                """, (sync_date,))
                row = cur.fetchone()
                if row:
                    return {"synced_at": row[0], "synced_by": row[1], "status": row[2]}
    except Exception as e:
        logger.warning("Could not get last sync: %s", e)
    return None


def _load_auto_phan_loai_map(date_from: str, date_to: str) -> dict:
    """Return {page_id: 'ma_win'|'ma_test'} for a date range. Default 'ma_win' if not set.
    Nếu 1 page có phân loại khác nhau giữa các ngày → lấy ngày mới nhất trong khoảng."""
    try:
        from db import get_conn
        _ensure_auto_phan_loai_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (page_id) page_id, phan_loai
                    FROM fb_page_auto_phan_loai
                    WHERE date BETWEEN %s AND %s
                    ORDER BY page_id, date DESC
                """, (date_from, date_to))
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not load auto phan_loai: %s", e)
        return {}


def _save_auto_phan_loai(page_id: str, selected_date: str, phan_loai: str, user_id: str):
    """Upsert Win/Test choice for an auto-detected page on a given date."""
    from db import get_conn
    _ensure_auto_phan_loai_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_page_auto_phan_loai (page_id, date, phan_loai, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (page_id, date) DO UPDATE
                    SET phan_loai  = EXCLUDED.phan_loai,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
            """, (page_id, selected_date, phan_loai, user_id))
        conn.commit()


# ── VAT rate per page (6.1% hoặc 11.3%) — NV tự chọn ──────────────────
_VAT_RATES = (0.061, 0.113)   # 6.1% và 11.3%
_DEFAULT_VAT_RATE = 0.0       # cty KHÔNG dùng VAT → mặc định 0%


def _ensure_vat_rate_table():
    """Create fb_page_vat_rate table if it doesn't exist."""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fb_page_vat_rate (
                        page_id    VARCHAR(100)  NOT NULL,
                        date       DATE          NOT NULL,
                        vat_rate   NUMERIC(5,3)  NOT NULL DEFAULT 0.0,
                        updated_by VARCHAR(60),
                        updated_at TIMESTAMP     DEFAULT NOW(),
                        PRIMARY KEY (page_id, date)
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not create fb_page_vat_rate: %s", e)


def _load_vat_rate_map(date_from: str, date_to: str) -> dict:
    """Return {page_id: vat_rate(float)} cho khoảng ngày. Lấy ngày mới nhất nếu khác nhau.
    Page không có trong map → mặc định 11.3% (xử lý ở chỗ dùng)."""
    try:
        from db import get_conn
        _ensure_vat_rate_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (page_id) page_id, vat_rate
                    FROM fb_page_vat_rate
                    WHERE date BETWEEN %s AND %s
                    ORDER BY page_id, date DESC
                """, (date_from, date_to))
                return {row[0]: float(row[1]) for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not load vat_rate: %s", e)
        return {}


def _save_vat_rate(page_id: str, selected_date: str, vat_rate: float, user_id: str):
    """Upsert VAT rate (6.1%/11.3%) cho 1 page trên 1 ngày."""
    from db import get_conn
    _ensure_vat_rate_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fb_page_vat_rate (page_id, date, vat_rate, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (page_id, date) DO UPDATE
                    SET vat_rate   = EXCLUDED.vat_rate,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
            """, (page_id, selected_date, vat_rate, user_id))
        conn.commit()


def _get_leader_team_member_ids(leader_user_id) -> list:
    """Return list of user_id strings for all members of a leader's team (from users.json)."""
    leader = _get_user_by_id(leader_user_id)
    if not leader:
        return [str(leader_user_id)]
    team_id = str(leader.get("team_id", "")).strip()
    members = _get_team_members(team_id) if team_id else []
    ids = [str(m.get("id")) for m in members if m.get("id")]
    if str(leader_user_id) not in ids:
        ids.append(str(leader_user_id))
    return ids or [str(leader_user_id)]


def _manual_declared_pages_for_date(date_from: str, date_to: str, user_id, role) -> list:
    """Fetch ALL manually declared pages for a date range visible to this user.
    Tiền (tien_chua_thue/thanh_tien) được SUM qua các ngày trong khoảng cho mỗi page;
    metadata (decl_id, phan_loai, note...) lấy theo bản ghi mới nhất.
    Always includes declared_user_id so team_groups can map pages to members.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if _is_senior(role):
                    if role == "leader":
                        team_ids = _get_leader_team_member_ids(user_id)
                        placeholders = ",".join(["%s"] * len(team_ids))
                        user_filter = f"AND d.user_id IN ({placeholders})"
                        params = [date_from, date_to] + team_ids
                    else:
                        # admin/ketoan: see ALL manual pages
                        user_filter = ""
                        params = [date_from, date_to]
                else:
                    user_filter = "AND d.user_id = %s"
                    params = [date_from, date_to, user_id]

                cur.execute(f"""
                    WITH decl AS (
                        SELECT
                            d.id,
                            d.page_id,
                            COALESCE(d.page_name_manual, d.page_id) AS page_name,
                            d.ad_account_id,
                            d.ad_account_name,
                            d.user_id AS declared_user_id,
                            d.phan_loai,
                            d.tien_chua_thue,
                            d.thanh_tien,
                            d.note,
                            d.created_at
                        FROM fb_page_spend_declarations d
                        WHERE d.spend_date BETWEEN %s AND %s
                          AND d.page_id LIKE 'MANUAL_%%'
                          {user_filter}
                    )
                    SELECT DISTINCT ON (page_id)
                        id AS decl_id,
                        page_id,
                        page_name,
                        ad_account_id,
                        ad_account_name,
                        declared_user_id,
                        phan_loai,
                        SUM(tien_chua_thue) OVER (PARTITION BY page_id) AS tien_chua_thue,
                        SUM(thanh_tien)     OVER (PARTITION BY page_id) AS thanh_tien,
                        note
                    FROM decl
                    ORDER BY page_id, created_at DESC
                """, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("_manual_declared_pages_for_date error: %s", exc)
        return []

# ── POS (Pancake) ────────────────────────────────────────────────────
def _query_pos_page_metrics(date_from: str, date_to: str) -> dict:
    """Return {page_id: {ads, revenue, profit, chot, hoan}} từ POS (Pancake) theo page.

    Số liệu lấy từ pos_page_daily_metrics (sync_pos_page_metrics.py kéo qua endpoint
    analytics/sale split_by Order.source). Dùng để ĐỐI CHIẾU CP QC POS vs CP Ads FB.
    Page chưa có trên POS → không có key → template để trống cột.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.page_id,
                           COALESCE(SUM(m.ads_amount), 0),
                           COALESCE(SUM(m.revenue), 0),
                           COALESCE(SUM(m.profit), 0),
                           COALESCE(SUM(m.success_order_count), 0),
                           COALESCE(SUM(m.returned_order_count), 0),
                           COALESCE(SUM(m.sales), 0),
                           MAX(m.page_name),
                           (SELECT m2.pos_shop_id FROM pos_page_daily_metrics m2
                             WHERE m2.page_id = m.page_id AND m2.metric_date BETWEEN %s AND %s
                               AND m2.pos_shop_id IS NOT NULL
                             GROUP BY m2.pos_shop_id ORDER BY SUM(m2.revenue) DESC NULLS LAST LIMIT 1) AS top_shop
                      FROM pos_page_daily_metrics m
                     WHERE m.metric_date BETWEEN %s AND %s
                     GROUP BY m.page_id
                """, (date_from, date_to, date_from, date_to))
                out = {}
                for r in cur.fetchall():
                    if not r[0]:
                        continue
                    ads, chot = float(r[1]), int(r[4])
                    out[str(r[0])] = {
                        "ads":     ads,
                        "revenue": float(r[2]),
                        "profit":  float(r[3]),
                        "chot":    chot,
                        "hoan":    int(r[5]),
                        "sales":   float(r[6]),  # tiền hàng (trước chiết khấu)
                        "name":    (r[7] or "").strip(),
                        "pos_shop_id": (str(r[8]) if r[8] else None),  # shop POS chính → link Pancake
                        # Chi phí QC / đơn chốt (như cột "Chi phí QC / Đơn hàng" của Pancake)
                        "cp_per_order": round(ads / chot, 0) if chot > 0 else 0,
                    }
                return out
    except Exception as e:
        logger.warning("_query_pos_page_metrics error: %s", e)
        return {}


def _query_pos_known_pages(page_ids=None) -> dict:
    """{page_id: page_name} cho MỌI page TỪNG xuất hiện trên POS (bất kỳ ngày nào).

    Dùng để phân biệt: page là page POS (đã từng có số) nhưng kỳ đang xem chưa có
    số → KHÔNG ghi 'Chưa có trên POS' (gây hiểu nhầm), mà hiện là page POS chưa có
    số trong kỳ. Page chưa bao giờ lên POS mới ghi 'Chưa có trên POS'.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                if page_ids:
                    cur.execute("""
                        SELECT DISTINCT ON (page_id) page_id, page_name
                          FROM pos_page_daily_metrics
                         WHERE page_id = ANY(%s)
                         ORDER BY page_id, metric_date DESC
                    """, (list(page_ids),))
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (page_id) page_id, page_name
                          FROM pos_page_daily_metrics
                         ORDER BY page_id, metric_date DESC
                    """)
                return {str(r[0]): (r[1] or "").strip() for r in cur.fetchall() if r[0]}
    except Exception as e:
        logger.warning("_query_pos_known_pages error: %s", e)
        return {}

