"""Marketing Brain — GĐ2 (V1.1): Ad ↔ Đơn ↔ Doanh thu ↔ Lãi + lọc & phân quyền 3 tầng.

Nguyên tắc module (chuẩn bị tách SaaS):
- Bảng prefix mb_*, KHÔNG FK cứng vào bảng core (soft key qua order_id/ad_id/page_id).
- Blueprint riêng url_prefix /marketing-brain.

Phân quyền 3 tầng:
- FULL (admin/superadmin/manager/ketoan/accountant/it): thấy tất cả, đủ bộ lọc.
- LEADER: chỉ thấy team mình (teams.leader_user_id → fallback users.team_id).
- NV: chỉ thấy TK QC đang được gán cho mình (user_ad_account_assignments).

Bộ lọc: Team · Nhân viên · TK quảng cáo · Shop (page→shop binding) · khoảng ngày.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session

try:
    from tz_utils import now_hcm
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", ".."))
    from tz_utils import now_hcm

from db import get_conn

logger = logging.getLogger(__name__)

marketing_brain_bp = Blueprint(
    "marketing_brain", __name__,
    template_folder="templates",
    url_prefix="/marketing-brain",
)

_FULL_VIEW_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}

# CP Ads FB chưa VAT — kế toán cần đã VAT (§11.4 CLAUDE.md)
ADS_COST_MULTIPLIER = 1.113

# Đơn KHÔNG tính vào doanh thu thực
_BAD_STATUSES = ("cancelled", "returned", "returning")


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username") and not session.get("user_id"):
            return redirect("/login?next=/marketing-brain/")
        return f(*args, **kwargs)
    return decorated


def _parse_period():
    """date_from/date_to từ query string, mặc định 7 ngày gần nhất.

    Lưu ý: tz_utils.today_hcm() trả STRING — dùng now_hcm().date() để tính toán.
    """
    today = now_hcm().date()
    date_to = (request.args.get("date_to") or "").strip() or today.isoformat()
    date_from = (request.args.get("date_from") or "").strip() or (today - timedelta(days=6)).isoformat()
    return date_from, date_to


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _accounts_of_users(cur, user_ids):
    if not user_ids:
        return []
    rows = _q(cur, """
        SELECT DISTINCT ad_account_id FROM user_ad_account_assignments
        WHERE user_id = ANY(%s) AND assigned_to IS NULL
    """, (list(user_ids),))
    return [r["ad_account_id"] for r in rows]


def _resolve_scope(cur):
    """Trả dict: tier 'full'|'leader'|'nv', accounts (None=tất cả | list),
    team_id (leader), team_user_ids (leader), label (hiện banner)."""
    role = str(session.get("role") or "").strip().lower()
    uid = session.get("user_id")
    if role in _FULL_VIEW_ROLES:
        return {"tier": "full", "accounts": None, "team_id": None,
                "team_user_ids": None, "label": ""}

    if role in ("leader", "sale_leader"):
        team = _q(cur, "SELECT id, team_name FROM teams WHERE leader_user_id = %s LIMIT 1",
                  (int(uid),))
        if not team:
            team = _q(cur, """
                SELECT t.id, t.team_name FROM teams t
                JOIN users u ON u.team_id = t.id WHERE u.id = %s LIMIT 1
            """, (int(uid),))
        if team:
            team_id = team[0]["id"]
            members = _q(cur, "SELECT id FROM users WHERE team_id = %s", (team_id,))
            user_ids = sorted({r["id"] for r in members} | {int(uid)})
            return {"tier": "leader", "accounts": _accounts_of_users(cur, user_ids),
                    "team_id": team_id, "team_user_ids": user_ids,
                    "label": f"👥 Team: {team[0]['team_name']}"}
        # Leader không gắn team nào → coi như NV (chỉ TK của mình)

    accounts = _accounts_of_users(cur, [int(uid)] if uid else [])
    return {"tier": "nv", "accounts": accounts, "team_id": None,
            "team_user_ids": [int(uid)] if uid else [],
            "label": f"👤 {len(accounts)} TK QC của bạn"}


def _intersect(base, new):
    """base None = chưa giới hạn."""
    if base is None:
        return list(new)
    s = set(new)
    return [a for a in base if a in s]


@marketing_brain_bp.route("/huong-dan-kiem-tra")
@_login_required
def huong_dan_kiem_tra():
    """Hướng dẫn kế toán đối chiếu chéo số liệu Marketing Brain với POS/FB/chi-phi-qc."""
    return render_template("marketing_brain/huong_dan.html")


@marketing_brain_bp.route("/tra-cuu")
@_login_required
def tra_cuu():
    """Tra cứu 1 đơn → full GIA PHẢ: Page(WIN/TEST) → Chiến dịch → Adset → Ad → Post,
    + NV chạy QC + chi phí ad cho đúng đơn này (CPA ngày). Tìm theo mã đơn / SĐT / mã vận đơn.
    Đây là đơn vị tính gốc + công cụ truy vết để đánh giá NV (auditable)."""
    q = (request.args.get("q") or "").strip()
    results = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            scope = _resolve_scope(cur)
            accounts = scope["accounts"]  # None = full view
            if q:
                orders = _q(cur, """
                    SELECT o.id, o.external_order_id, o.order_code, o.customer_name,
                           o.customer_phone, o.total_amount, o.net_revenue,
                           o.order_status::text AS order_status, o.created_at_pos,
                           COALESCE(s.shop_name, '') AS shop_name,
                           o.raw_payload_json->'partner'->>'extend_code' AS tracking
                    FROM orders o LEFT JOIN shops s ON s.id = o.shop_id
                    WHERE o.external_order_id = %s OR o.order_code = %s
                       OR o.customer_phone = %s
                       OR o.raw_payload_json->'partner'->>'extend_code' = %s
                    ORDER BY o.created_at_pos DESC LIMIT 20
                """, (q, q, q, q))
                for o in orders:
                    lin = _q(cur, """
                        SELECT a.ad_id, a.post_id, a.page_id, a.page_name, a.conversation_id,
                               a.is_organic, a.ad_source_origin, a.order_date,
                               e.campaign_name, e.adset_name, e.ad_name, e.account_id,
                               (SELECT SUM(spend) FROM mb_fb_entity_daily
                                WHERE ad_id = a.ad_id AND metric_date = a.order_date) AS ad_spend_day,
                               (SELECT COUNT(*) FROM mb_order_attribution a2
                                WHERE a2.ad_id = a.ad_id AND a2.order_date = a.order_date) AS ad_orders_day,
                               lbl.phan_loai,
                               nv.nv_name, nv.account_name
                        FROM mb_order_attribution a
                        LEFT JOIN mb_fb_entity_daily e
                               ON e.ad_id = a.ad_id AND e.metric_date = a.order_date
                        LEFT JOIN fb_page_auto_phan_loai lbl
                               ON lbl.page_id = a.page_id AND lbl.date = a.order_date
                        LEFT JOIN LATERAL (
                            SELECT COALESCE(NULLIF(u.full_name,''), u.username) AS nv_name,
                                   ua.ad_account_name AS account_name
                            FROM user_ad_account_assignments ua JOIN users u ON u.id = ua.user_id
                            WHERE ua.ad_account_id = e.account_id
                              AND a.order_date >= ua.assigned_from
                              AND (ua.assigned_to IS NULL OR a.order_date <= ua.assigned_to)
                            LIMIT 1
                        ) nv ON true
                        WHERE a.order_id = %s
                    """, (o["id"],))
                    a = lin[0] if lin else {}
                    # Phân quyền: NV/leader chỉ tra được đơn thuộc TK trong phạm vi mình
                    if accounts is not None and a.get("account_id"):
                        if a["account_id"] not in accounts:
                            continue
                    # CPA: chi phí ad (đã VAT) ÷ số đơn của ad đó trong ngày.
                    # CHỈ chính xác khi đã QUA NGÀY — spend trong-ngày chưa chốt (FB còn
                    # tiêu + sync trễ). Đơn order_date >= hôm nay → đánh dấu "chưa chốt".
                    cpa = None
                    cpa_pending = (a.get("order_date") is not None
                                   and a["order_date"] >= now_hcm().date())
                    if (not cpa_pending) and a.get("ad_spend_day") and a.get("ad_orders_day"):
                        cpa = round(float(a["ad_spend_day"]) * ADS_COST_MULTIPLIER / a["ad_orders_day"])
                    products = _q(cur, """
                        SELECT product_name, sku, quantity, line_total
                        FROM order_items WHERE order_id = %s ORDER BY line_total DESC
                    """, (o["id"],))
                    results.append({"o": o, "a": a, "cpa": cpa,
                                    "cpa_pending": cpa_pending, "products": products})
    return render_template("marketing_brain/tra_cuu.html", q=q, results=results)


@marketing_brain_bp.route("/")
@_login_required
def index():
    date_from, date_to = _parse_period()
    f_team = (request.args.get("team_id") or "").strip()
    f_nv = (request.args.get("nv_id") or "").strip()
    f_account = (request.args.get("account_id") or "").strip()
    f_shop = (request.args.get("shop_id") or "").strip()
    f_campaign = (request.args.get("campaign_id") or "").strip()  # drill: 1 chiến dịch

    with get_conn() as conn:
        with conn.cursor() as cur:
            scope = _resolve_scope(cur)
            tier = scope["tier"]
            accounts = scope["accounts"]  # None = tất cả
            if accounts == [] :
                return ("Bạn chưa được gán TK quảng cáo nào — liên hệ leader/IT để được cấp quyền xem Marketing Brain.",
                        403)

            # ── Áp bộ lọc (thu hẹp trong phạm vi scope) ───────────────────
            if f_nv:
                try:
                    nv_accounts = _accounts_of_users(cur, [int(f_nv)])
                except (TypeError, ValueError):
                    nv_accounts = []
                accounts = _intersect(accounts, nv_accounts)
            elif f_team and tier == "full":
                try:
                    members = _q(cur, "SELECT id FROM users WHERE team_id = %s", (int(f_team),))
                    accounts = _intersect(accounts, _accounts_of_users(cur, [r["id"] for r in members]))
                except (TypeError, ValueError):
                    pass
            if f_account:
                accounts = _intersect(accounts, [f_account])

            # Page set theo accounts — tính TRƯỚC pages_filter để chặn bypass quyền
            pages_acc = None
            if accounts is not None:
                rows = _q(cur, """
                    SELECT DISTINCT page_id FROM mb_fb_entity_daily
                    WHERE account_id = ANY(%s) AND page_id <> ''
                """, (accounts,))
                pages_acc = [r["page_id"] for r in rows]

            pages_filter = None
            if f_shop:
                try:
                    sid = int(f_shop)
                    # 3-TIER (đồng bộ chi-phi-qc, adspage.md): page thuộc shop khi
                    #   Tier 1 — bind tay vào shop (fb_page_shop_binding), HOẶC
                    #   Tier 2 — chưa bind nhưng TK của page map đúng 1 shop = shop này.
                    # Trước đây chỉ dùng Tier 1 → page chưa bind của TK shop đó bị mất
                    # khi lọc shop (bug 14/06: lọc TK đủ, lọc shop thiếu).
                    rows = _q(cur, """
                        SELECT DISTINCT page_id FROM fb_page_shop_binding
                        WHERE pos_shop_id = %s AND assigned_to IS NULL
                    """, (sid,))
                    _bound = {r["page_id"] for r in rows}
                    rows2 = _q(cur, """
                        WITH tk_single AS (
                            SELECT fb_ad_account_id, MIN(shop_id) AS shop_id
                            FROM fb_ad_account_mappings WHERE assigned_to IS NULL
                            GROUP BY fb_ad_account_id HAVING COUNT(*) = 1
                        )
                        SELECT DISTINCT s.page_id
                        FROM fb_ads_page_daily_spend s
                        JOIN tk_single t
                          ON t.fb_ad_account_id = s.fb_ad_account_id AND t.shop_id = %s
                        LEFT JOIN fb_page_shop_binding b
                          ON b.page_id = s.page_id AND b.assigned_to IS NULL
                        WHERE b.page_id IS NULL
                    """, (sid,))
                    _auto = {r["page_id"] for r in rows2}
                    pages_filter = list(_bound | _auto)
                    # CHẶN BYPASS (audit 12/06 P0#6): NV/leader truyền ?shop_id= shop
                    # người khác → page của shop đó phải GIAO với phạm vi quyền,
                    # không được thay thế.
                    if pages_acc is not None:
                        pages_filter = _intersect(pages_acc, pages_filter)
                except (TypeError, ValueError):
                    pages_filter = None

            # ── Build điều kiện SQL ───────────────────────────────────────
            # Cho attribution (alias a): ad thuộc accounts + page thuộc pages
            attr_cond, attr_params = "", []
            ent_cond, ent_params = "", []
            if accounts is not None:
                attr_cond += " AND a.ad_id IN (SELECT DISTINCT ad_id FROM mb_fb_entity_daily WHERE account_id = ANY(%s))"
                attr_params.append(accounts)
                ent_cond += " AND account_id = ANY(%s)"
                ent_params.append(accounts)
            if pages_filter is not None:
                attr_cond += " AND a.page_id = ANY(%s)"
                attr_params.append(pages_filter)
                ent_cond += " AND page_id = ANY(%s)"
                ent_params.append(pages_filter)

            # Drill xuống 1 chiến dịch (bấm từ bảng Top Chiến dịch)
            if f_campaign:
                attr_cond += " AND a.ad_id IN (SELECT DISTINCT ad_id FROM mb_fb_entity_daily WHERE campaign_id = %s)"
                attr_params.append(f_campaign)
                ent_cond += " AND campaign_id = %s"
                ent_params.append(f_campaign)

            # Page set hiệu lực cho widget theo-page (organic không có ad_id)
            pages_eff = pages_filter if pages_filter is not None else pages_acc
            page_cond, page_params = "", []
            if pages_eff is not None:
                page_cond = " AND a.page_id = ANY(%s)"
                page_params.append(pages_eff)

            attr_params = tuple(attr_params)
            ent_params = tuple(ent_params)
            page_params = tuple(page_params)

            # ── Widget 0: CẢNH BÁO — ads đang đốt tiền (theo scope/filter) ──
            # Cửa sổ D-8 → D-7: vùng attribution ĐÃ CHÍN ~80-90% (audit 12/06:
            # D-2..D-4 mới ~50-70% → phạt oan). Đồng bộ với script Telegram.
            _today = now_hcm().date()
            w_from = (_today - timedelta(days=8)).isoformat()
            w_to = (_today - timedelta(days=7)).isoformat()
            alerts = _q(cur, f"""
                WITH s AS (
                    SELECT ad_id, MAX(campaign_name) AS campaign_name,
                           MAX(account_id) AS account_id, SUM(spend) AS spend
                    FROM mb_fb_entity_daily
                    WHERE metric_date BETWEEN %s AND %s{ent_cond}
                    GROUP BY ad_id
                    HAVING SUM(spend) >= 300000
                ),
                o AS (
                    -- orders chỉ đếm đơn THỰC (ad toàn đơn hủy không được thoát cảnh báo)
                    SELECT a.ad_id,
                           COUNT(*) FILTER (WHERE ord.order_status NOT IN %s) AS orders,
                           COALESCE(SUM(ord.net_revenue) FILTER (WHERE ord.order_status NOT IN %s), 0) AS rev,
                           MAX(a.page_name) AS page_name
                    FROM mb_order_attribution a
                    JOIN orders ord ON ord.id = a.order_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.ad_id
                )
                SELECT s.ad_id, s.campaign_name,
                       COALESCE(o.page_name, '') AS page_name,
                       s.spend, ROUND(s.spend * %s, 0) AS spend_vat,
                       COALESCE(o.orders, 0) AS orders, COALESCE(o.rev, 0) AS rev,
                       CASE WHEN COALESCE(o.orders, 0) = 0 THEN '0 đơn'
                            ELSE 'dưới hòa vốn' END AS alert_type,
                       COALESCE(nv.nv_name, '') AS nv_name
                FROM s
                LEFT JOIN o ON o.ad_id = s.ad_id
                LEFT JOIN LATERAL (
                    SELECT COALESCE(NULLIF(us.full_name, ''), us.username) AS nv_name
                    FROM user_ad_account_assignments ua
                    JOIN users us ON us.id = ua.user_id
                    WHERE ua.ad_account_id = s.account_id AND ua.assigned_to IS NULL
                    LIMIT 1
                ) nv ON true
                WHERE COALESCE(o.rev, 0) < s.spend * %s
                ORDER BY s.spend DESC
                LIMIT 15
            """, (w_from, w_to) + ent_params
                 + (_BAD_STATUSES, _BAD_STATUSES, w_from, w_to) + attr_params
                 + (ADS_COST_MULTIPLIER, ADS_COST_MULTIPLIER))

            # ── Widget tầng CHIẾN DỊCH (giữa Page và Ad) — bấm để drill ───
            top_campaigns = _q(cur, f"""
                WITH ad_spend AS (
                    SELECT ad_id, MAX(campaign_id) AS campaign_id, MAX(campaign_name) AS campaign_name,
                           SUM(spend) AS spend
                    FROM mb_fb_entity_daily
                    WHERE metric_date BETWEEN %s AND %s AND campaign_id <> ''{ent_cond}
                    GROUP BY ad_id
                ),
                ad_orders AS (
                    SELECT a.ad_id,
                           COUNT(*) FILTER (WHERE o.order_status NOT IN %s) AS orders,
                           COALESCE(SUM(o.net_revenue) FILTER (WHERE o.order_status NOT IN %s), 0) AS net_revenue
                    FROM mb_order_attribution a JOIN orders o ON o.id = a.order_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.ad_id
                ),
                ad_cogs AS (
                    SELECT a.ad_id, SUM(p.gia_nhap * oi.quantity) AS cogs
                    FROM mb_order_attribution a
                    JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
                    JOIN order_items oi ON oi.order_id = a.order_id
                    JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
                    JOIN wh_products p ON p.id = vm.product_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.ad_id
                )
                SELECT s.campaign_id, MAX(s.campaign_name) AS campaign_name,
                       COUNT(DISTINCT s.ad_id) AS so_ad,
                       SUM(s.spend) AS spend, ROUND(SUM(s.spend) * %s, 0) AS spend_vat,
                       COALESCE(SUM(o.orders), 0) AS orders,
                       COALESCE(SUM(o.net_revenue), 0) AS net_revenue,
                       COALESCE(SUM(c.cogs), 0) AS cogs,
                       COALESCE(SUM(o.net_revenue), 0) - COALESCE(SUM(c.cogs), 0)
                           - ROUND(SUM(s.spend) * %s, 0) AS gross_profit,
                       CASE WHEN SUM(s.spend) > 0
                            THEN ROUND(COALESCE(SUM(o.net_revenue), 0) / (SUM(s.spend) * %s), 2) END AS roas
                FROM ad_spend s
                LEFT JOIN ad_orders o ON o.ad_id = s.ad_id
                LEFT JOIN ad_cogs c ON c.ad_id = s.ad_id
                GROUP BY s.campaign_id
                ORDER BY orders DESC, spend DESC
                LIMIT 30
            """, (date_from, date_to) + ent_params
                 + (_BAD_STATUSES, _BAD_STATUSES, date_from, date_to) + attr_params
                 + (_BAD_STATUSES, date_from, date_to) + attr_params
                 + (ADS_COST_MULTIPLIER, ADS_COST_MULTIPLIER, ADS_COST_MULTIPLIER))

            # ── Widget 1: Top Ads ─────────────────────────────────────────
            top_ads = _q(cur, f"""
                WITH ad_orders AS (
                    SELECT a.ad_id,
                           MAX(a.page_name) AS page_name,
                           COUNT(*) AS orders,
                           -- Loại đơn cancelled 0đ ảo khỏi "Hoàn/Hủy" (gần nửa bad là 0đ — audit 12/06)
                           COUNT(*) FILTER (WHERE o.order_status IN %s
                                            AND NOT (o.order_status = 'cancelled' AND o.net_revenue = 0)) AS bad_orders,
                           COALESCE(SUM(o.net_revenue) FILTER (WHERE o.order_status NOT IN %s), 0) AS net_revenue
                    FROM mb_order_attribution a
                    JOIN orders o ON o.id = a.order_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.ad_id
                ),
                ad_cogs AS (
                    SELECT a.ad_id, SUM(p.gia_nhap * oi.quantity) AS cogs
                    FROM mb_order_attribution a
                    JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
                    JOIN order_items oi ON oi.order_id = a.order_id
                    JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
                    JOIN wh_products p ON p.id = vm.product_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.ad_id
                ),
                ad_spend AS (
                    SELECT ad_id, MAX(campaign_name) AS campaign_name, MAX(adset_name) AS adset_name,
                           MAX(ad_name) AS ad_name, SUM(spend) AS spend
                    FROM mb_fb_entity_daily
                    WHERE metric_date BETWEEN %s AND %s{ent_cond}
                    GROUP BY ad_id
                )
                SELECT COALESCE(ao.ad_id, asp.ad_id) AS ad_id,
                       COALESCE(asp.campaign_name, '') AS campaign_name,
                       COALESCE(asp.adset_name, '') AS adset_name,
                       COALESCE(asp.ad_name, '') AS ad_name,
                       COALESCE(ao.page_name, '') AS page_name,
                       COALESCE(asp.spend, 0) AS spend,
                       ROUND(COALESCE(asp.spend, 0) * %s, 0) AS spend_vat,
                       COALESCE(ao.orders, 0) AS orders,
                       COALESCE(ao.bad_orders, 0) AS bad_orders,
                       COALESCE(ao.net_revenue, 0) AS net_revenue,
                       COALESCE(c.cogs, 0) AS cogs,
                       COALESCE(ao.net_revenue, 0) - COALESCE(c.cogs, 0)
                           - ROUND(COALESCE(asp.spend, 0) * %s, 0) AS gross_profit,
                       -- ROAS tính trên CP ĐÃ VAT — cùng chuẩn với cột Lãi (audit 12/06 P1#5)
                       CASE WHEN COALESCE(asp.spend, 0) > 0
                            THEN ROUND(COALESCE(ao.net_revenue, 0) / (asp.spend * %s), 2) END AS roas
                FROM ad_orders ao
                FULL OUTER JOIN ad_spend asp ON asp.ad_id = ao.ad_id
                LEFT JOIN ad_cogs c ON c.ad_id = COALESCE(ao.ad_id, asp.ad_id)
                ORDER BY COALESCE(ao.orders, 0) DESC, COALESCE(asp.spend, 0) DESC
                LIMIT 30
            """, (_BAD_STATUSES, _BAD_STATUSES, date_from, date_to) + attr_params
                 + (_BAD_STATUSES, date_from, date_to) + attr_params
                 + (date_from, date_to) + ent_params
                 + (ADS_COST_MULTIPLIER, ADS_COST_MULTIPLIER, ADS_COST_MULTIPLIER))

            # ── Widget 2: Top SKU theo Ads ────────────────────────────────
            top_skus = _q(cur, f"""
                SELECT p.sku, MAX(p.name) AS name,
                       COUNT(DISTINCT a.order_id) AS orders,
                       SUM(oi.quantity) AS qty,
                       -- line_total dựa giá niêm yết → quy về tiền thực thu theo tỷ lệ của đơn
                       SUM(oi.line_total * CASE WHEN o.total_amount > 0
                           THEN o.net_revenue / o.total_amount ELSE 1 END) AS revenue,
                       SUM(p.gia_nhap * oi.quantity) AS cogs,
                       SUM(oi.line_total * CASE WHEN o.total_amount > 0
                           THEN o.net_revenue / o.total_amount ELSE 1 END)
                         - SUM(p.gia_nhap * oi.quantity) AS gross_margin,
                       COUNT(DISTINCT a.ad_id) AS ads_count
                FROM mb_order_attribution a
                JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
                JOIN order_items oi ON oi.order_id = a.order_id
                JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
                JOIN wh_products p ON p.id = vm.product_id
                WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                GROUP BY p.sku
                ORDER BY revenue DESC
                LIMIT 20
            """, (_BAD_STATUSES, date_from, date_to) + attr_params)

            # ── Widget 3: Top Content (post_id) ───────────────────────────
            top_posts = _q(cur, f"""
                WITH post_orders AS (
                    SELECT a.post_id, MAX(a.page_name) AS page_name,
                           COUNT(*) AS orders,
                           COALESCE(SUM(o.net_revenue) FILTER (WHERE o.order_status NOT IN %s), 0) AS net_revenue
                    FROM mb_order_attribution a
                    JOIN orders o ON o.id = a.order_id
                    WHERE a.post_id <> '' AND a.order_date BETWEEN %s AND %s{attr_cond}
                    GROUP BY a.post_id
                ),
                post_spend AS (
                    SELECT post_id, SUM(spend) AS spend, COUNT(DISTINCT ad_id) AS ads_count
                    FROM mb_fb_entity_daily
                    WHERE post_id <> '' AND metric_date BETWEEN %s AND %s{ent_cond}
                    GROUP BY post_id
                )
                SELECT po.post_id, po.page_name, po.orders, po.net_revenue,
                       COALESCE(ps.spend, 0) AS spend, COALESCE(ps.ads_count, 0) AS ads_count
                FROM post_orders po
                LEFT JOIN post_spend ps ON ps.post_id = po.post_id
                ORDER BY po.orders DESC
                LIMIT 20
            """, (_BAD_STATUSES, date_from, date_to) + attr_params
                 + (date_from, date_to) + ent_params)

            # ── Widget 4: Top Pages (theo page set hiệu lực) ──────────────
            top_pages = _q(cur, f"""
                SELECT a.page_id,
                       COALESCE(NULLIF(MAX(a.page_name), ''), a.page_id) AS page_name,
                       COUNT(*) AS orders,
                       COUNT(*) FILTER (WHERE a.is_organic) AS organic_orders,
                       COALESCE(SUM(o.net_revenue) FILTER (WHERE o.order_status NOT IN %s), 0) AS net_revenue
                FROM mb_order_attribution a
                JOIN orders o ON o.id = a.order_id
                WHERE a.page_id <> '' AND a.order_date BETWEEN %s AND %s{page_cond}
                GROUP BY a.page_id
                ORDER BY net_revenue DESC
                LIMIT 30
            """, (_BAD_STATUSES, date_from, date_to) + page_params)

            # ── Widget 5: Organic vs Paid (trong page set hiệu lực) ───────
            split = _q(cur, f"""
                SELECT
                    COUNT(*) FILTER (WHERE a.ad_id <> '') AS paid_orders,
                    COUNT(*) FILTER (WHERE a.is_organic) AS organic_orders,
                    COUNT(*) FILTER (WHERE a.ad_id = '' AND a.page_id = '') AS unknown_orders,
                    COUNT(*) AS total_orders
                FROM mb_order_attribution a
                WHERE a.order_date BETWEEN %s AND %s{page_cond}
            """, (date_from, date_to) + page_params)[0]

            # ── Widget 6: Coverage — dòng 1 THEO PHẠM VI LỌC (đồng nhất với
            # các widget khác, kẻo đọc nhầm "Chí Năm 1.856 đơn/ngày"), dòng 2 toàn hệ.
            coverage = _q(cur, f"""
                SELECT 'Phạm vi đang lọc' AS scope, COUNT(*) AS orders,
                    ROUND(100.0*COUNT(*) FILTER (WHERE a.ad_id <> '')/GREATEST(COUNT(*),1),1) AS pct_ad,
                    ROUND(100.0*COUNT(*) FILTER (WHERE a.post_id <> '')/GREATEST(COUNT(*),1),1) AS pct_post,
                    ROUND(100.0*COUNT(*) FILTER (WHERE a.page_id <> '')/GREATEST(COUNT(*),1),1) AS pct_page,
                    ROUND(100.0*COUNT(*) FILTER (WHERE a.conversation_id <> '')/GREATEST(COUNT(*),1),1) AS pct_conv
                FROM mb_order_attribution a WHERE a.order_date BETWEEN %s AND %s{page_cond}
                UNION ALL
                SELECT 'Toàn hệ (mọi thời điểm)', COUNT(*),
                    ROUND(100.0*COUNT(*) FILTER (WHERE ad_id <> '')/GREATEST(COUNT(*),1),1),
                    ROUND(100.0*COUNT(*) FILTER (WHERE post_id <> '')/GREATEST(COUNT(*),1),1),
                    ROUND(100.0*COUNT(*) FILTER (WHERE page_id <> '')/GREATEST(COUNT(*),1),1),
                    ROUND(100.0*COUNT(*) FILTER (WHERE conversation_id <> '')/GREATEST(COUNT(*),1),1)
                FROM mb_order_attribution
            """, (date_from, date_to) + page_params)

            spend_range = _q(cur, """
                SELECT MIN(metric_date) AS d_min, MAX(metric_date) AS d_max,
                       COUNT(DISTINCT ad_id) AS ads, COUNT(DISTINCT account_id) AS accounts
                FROM mb_fb_entity_daily
            """)[0]

            # ── Option lists cho dropdown — LỌC DÂY CHUYỀN ────────────────
            # Team → NV của team → TK của NV → Shop của các TK đó.
            # vis_user_ids: None = mọi user (full); leader = team; nv = bản thân.
            if tier == "full":
                vis_user_ids = None
                opt_teams = _q(cur, """
                    SELECT DISTINCT t.id, t.team_name
                    FROM teams t
                    JOIN users u ON u.team_id = t.id
                    JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                    ORDER BY t.team_name
                """)
                if f_team:
                    try:
                        rows = _q(cur, "SELECT id FROM users WHERE team_id = %s", (int(f_team),))
                        vis_user_ids = [r["id"] for r in rows]
                    except (TypeError, ValueError):
                        pass
            elif tier == "leader":
                vis_user_ids = scope["team_user_ids"]
                opt_teams = []
            else:  # nv
                vis_user_ids = scope["team_user_ids"]  # = [chính mình]
                opt_teams = []

            if tier == "nv":
                opt_nv = []
            elif vis_user_ids is None:
                opt_nv = _q(cur, """
                    SELECT DISTINCT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name
                    FROM users u
                    JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                    ORDER BY 2
                """)
            else:
                opt_nv = _q(cur, """
                    SELECT DISTINCT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name
                    FROM users u
                    JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                    WHERE u.id = ANY(%s) ORDER BY 2
                """, (vis_user_ids,))

            # TK QC: theo NV đã chọn > theo team/scope
            acc_user_ids = None
            if f_nv:
                acc_user_ids = [int(f_nv)]
            elif vis_user_ids is not None:
                acc_user_ids = vis_user_ids
            if acc_user_ids is None:
                opt_accounts = _q(cur, """
                    SELECT ad_account_id AS id, MAX(COALESCE(NULLIF(ad_account_name,''), ad_account_id)) AS name
                    FROM user_ad_account_assignments WHERE assigned_to IS NULL
                    GROUP BY ad_account_id ORDER BY 2
                """)
            else:
                opt_accounts = _q(cur, """
                    SELECT ad_account_id AS id, MAX(COALESCE(NULLIF(ad_account_name,''), ad_account_id)) AS name
                    FROM user_ad_account_assignments
                    WHERE assigned_to IS NULL AND user_id = ANY(%s)
                    GROUP BY ad_account_id ORDER BY 2
                """, (acc_user_ids,))

            # Shop: chỉ shop có page mà các TK trong phạm vi đang chạy
            # (pages_acc = pages của accounts sau khi áp team/NV/TK, KHÔNG gồm shop filter)
            if pages_acc is None:
                opt_shops = _q(cur, """
                    SELECT DISTINCT s.id, s.shop_name
                    FROM shops s
                    JOIN fb_page_shop_binding b ON b.pos_shop_id = s.id AND b.assigned_to IS NULL
                    ORDER BY s.shop_name
                """)
            else:
                opt_shops = _q(cur, """
                    SELECT DISTINCT s.id, s.shop_name
                    FROM shops s
                    JOIN fb_page_shop_binding b ON b.pos_shop_id = s.id AND b.assigned_to IS NULL
                    WHERE b.page_id = ANY(%s)
                    ORDER BY s.shop_name
                """, (pages_acc,))

            # ── Panel SỨC KHỎE ATTRIBUTION (10 ngày) — đồng hồ đo coverage phục hồi
            # Toàn hệ, KHÔNG theo filter — để theo dõi shipcod đã thông đủ chưa.
            health = _q(cur, """
                SELECT order_date,
                       COUNT(*) AS don,
                       ROUND(100.0*COUNT(*) FILTER (WHERE ad_id<>'')/GREATEST(COUNT(*),1),0) AS paid_pct,
                       ROUND(100.0*COUNT(*) FILTER (WHERE is_organic)/GREATEST(COUNT(*),1),0) AS org_pct,
                       COUNT(*) FILTER (WHERE ad_source_origin='shipcod') AS tu_shipcod
                FROM mb_order_attribution
                WHERE order_date >= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date - 9
                GROUP BY order_date ORDER BY order_date DESC
            """)

    return render_template(
        "marketing_brain/index.html",
        health=health,
        date_from=date_from, date_to=date_to,
        alerts=alerts, alert_from=w_from, alert_to=w_to,
        top_campaigns=top_campaigns,
        top_ads=top_ads, top_skus=top_skus, top_posts=top_posts, top_pages=top_pages,
        split=split, coverage=coverage, spend_range=spend_range,
        tier=tier, scope_label=scope["label"],
        opt_teams=opt_teams, opt_nv=opt_nv, opt_accounts=opt_accounts, opt_shops=opt_shops,
        f_team=f_team, f_nv=f_nv, f_account=f_account, f_shop=f_shop, f_campaign=f_campaign,
    )
