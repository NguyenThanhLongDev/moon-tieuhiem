"""Leader Brain — LB-1.2: giám sát team kinh doanh + tách bạch TIỀN TEST vs TIỀN ĐỐT.

Thiết kế: LEADER_BRAIN_DESIGN.md. Phạm vi: CHỈ team kinh_doanh có NV giữ TK QC.

Luật nghiệp vụ sếp chốt 12/06:
- Tiền vào ads 0 đơn phải CHIA RÕ: trên page TEST (R&D hợp lệ, có hạn mức)
  vs trên page WIN (đốt thật — không bao biện được).
- Hạn mức test: 3,4tr / 1 mã hàng (campaign). Vượt mà page chưa lên WIN → báo đỏ.
  Đổi hạn mức: app_config key 'lb_test_limit_vnd'.
- Nhãn WIN/TEST đọc từ fb_page_auto_phan_loai (đang vận hành ở /chi-phi-qc),
  lấy nhãn MỚI NHẤT của mỗi page.

Quyền (12/06): full-view scoreboard tất cả; leader CHỈ team mình; NV chỉ /me.
"""
from __future__ import annotations

import logging
from datetime import date as _date
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

leader_brain_bp = Blueprint(
    "leader_brain", __name__,
    template_folder="templates",
    url_prefix="/leader-brain",
)

_FULL_VIEW_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}
ADS_COST_MULTIPLIER = 1.113
_BAD_STATUSES = ("cancelled", "returned", "returning")
_DEFAULT_TEST_LIMIT = 3_400_000

# Nhãn page (ma_win / ma_test) HIỆU LỰC TẠI CUỐI KỲ (%s = d_to) — không áp nhãn
# hôm nay hồi tố về quá khứ (audit 12/06: 12 page đổi nhãn → phạt/giấu ngược).
_LBL_CTE = """
    lbl AS (
        SELECT DISTINCT ON (page_id) page_id, phan_loai
        FROM fb_page_auto_phan_loai
        WHERE date <= %s
        ORDER BY page_id, date DESC
    )
"""

# Gán TK QC THEO NGÀY HIỆU LỰC (audit 12/06: 36 lần chuyển TK/30 ngày — spend
# quá khứ phải tính cho người giữ LÚC ĐÓ, không phải người đang giữ).
# Dùng kèm: FROM mb_fb_entity_daily e {_ACC_JOIN} WHERE {who_clause} ...
_ACC_JOIN = """
    JOIN user_ad_account_assignments ua ON ua.ad_account_id = e.account_id
        AND e.metric_date >= ua.assigned_from
        AND (ua.assigned_to IS NULL OR e.metric_date <= ua.assigned_to)
    JOIN users u ON u.id = ua.user_id
"""

# Mốc bắt đầu có dữ liệu ad-level đáng tin cho túi test
_TEST_TRACK_FROM = "2026-06-04"


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username") and not session.get("user_id"):
            return redirect("/login?next=/leader-brain/")
        return f(*args, **kwargs)
    return decorated


def _test_limit(cur) -> float:
    try:
        rows = _q(cur, "SELECT value FROM app_config WHERE key = 'lb_test_limit_vnd'")
        if rows:
            return float(rows[0]["value"])
    except Exception:
        pass
    return float(_DEFAULT_TEST_LIMIT)


def _viewer():
    """('full', None) | ('leader', team_id) | ('nv', user_id) | (None, None)."""
    role = str(session.get("role") or "").strip().lower()
    uid = session.get("user_id")
    if role in _FULL_VIEW_ROLES:
        return "full", None
    if not uid:
        return None, None
    with get_conn() as conn:
        with conn.cursor() as cur:
            if role in ("leader", "sale_leader"):
                rows = _q(cur, "SELECT id FROM teams WHERE leader_user_id = %s LIMIT 1", (int(uid),))
                if not rows:
                    rows = _q(cur, "SELECT team_id AS id FROM users WHERE id = %s AND team_id IS NOT NULL", (int(uid),))
                if rows:
                    return "leader", int(rows[0]["id"])
            rows = _q(cur, """
                SELECT 1 FROM user_ad_account_assignments
                WHERE user_id = %s AND assigned_to IS NULL LIMIT 1
            """, (int(uid),))
            if rows:
                return "nv", int(uid)
    return None, None


def _windows():
    """Kỳ tiền + nhịp đập theo ?date_from/&date_to, mặc định kỳ ĐÃ CHÍN (D-13→D-7 —
    audit 12/06: D-4 mới ~70% đơn gắn mã, chấm trên đó là phạt oan).
    Nhịp đập chạy theo kỳ lọc, cap 31 ngày cuối."""
    today = now_hcm().date()
    d_from = (request.args.get("date_from") or "").strip() or (today - timedelta(days=13)).isoformat()
    d_to = (request.args.get("date_to") or "").strip() or (today - timedelta(days=7)).isoformat()
    try:
        df, dt = _date.fromisoformat(d_from), _date.fromisoformat(d_to)
        if dt < df:
            df, dt = dt, df
        n_days = min((dt - df).days + 1, 31)
        hb_days = [dt - timedelta(days=i) for i in range(n_days - 1, -1, -1)]
    except ValueError:
        d_from = (today - timedelta(days=13)).isoformat()
        d_to = (today - timedelta(days=7)).isoformat()
        hb_days = [(today - timedelta(days=i)) for i in range(13, 6, -1)]
    return d_from, d_to, hb_days


def _score_window():
    """Kỳ CHẤM ĐIỂM cố định D-13→D-7 (vùng chín) — KHÔNG theo kỳ lọc của user
    (audit 12/06 P0: lọc 3 ngày gần → 6 leader đỏ giả). Điểm luôn tính trên kỳ này."""
    today = now_hcm().date()
    d_from, d_to = today - timedelta(days=13), today - timedelta(days=7)
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    return d_from.isoformat(), d_to.isoformat(), days


def _kd_teams(cur):
    return _q(cur, """
        SELECT t.id, t.team_name,
               COALESCE(
                   (SELECT COALESCE(NULLIF(u.full_name,''), u.username) FROM users u WHERE u.id = t.leader_user_id),
                   (SELECT COALESCE(NULLIF(u.full_name,''), u.username) FROM users u
                    WHERE u.team_id = t.id AND u.role::text IN ('leader','sale_leader') LIMIT 1),
                   '—') AS leader_name,
               (SELECT COUNT(DISTINCT u.id) FROM users u
                JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                WHERE u.team_id = t.id) AS nv_ads
        FROM teams t
        WHERE t.team_type = 'kinh_doanh'
          AND EXISTS (SELECT 1 FROM users u
                      JOIN user_ad_account_assignments ua ON ua.user_id = u.id AND ua.assigned_to IS NULL
                      WHERE u.team_id = t.id)
        ORDER BY t.team_name
    """)


def _money(cur, who_clause: str, who_param, d_from, d_to):
    """Khối tiền cho 1 phạm vi (team hoặc 1 NV) — TÁCH test/đốt theo nhãn page.

    who_clause: 'u.team_id = %s' | 'u.id = %s'. Mọi chỉ số "đốt"/"dưới hòa vốn"
    chỉ tính trên ads NGOÀI page test (test là R&D, đo riêng bằng hạn mức)."""
    rows = _q(cur, f"""
        WITH {_LBL_CTE},
        sp AS (
            SELECT e.ad_id, MAX(e.page_id) AS page_id, SUM(e.spend) AS spend
            FROM mb_fb_entity_daily e
            {_ACC_JOIN}
            WHERE {who_clause} AND e.metric_date BETWEEN %s AND %s
            GROUP BY e.ad_id
        ),
        spl AS (
            SELECT sp.*, COALESCE(l.phan_loai, 'ma_win') AS phan_loai
            FROM sp LEFT JOIN lbl l ON l.page_id = sp.page_id
        ),
        od AS (
            SELECT a.ad_id, COUNT(*) AS orders,
                   COUNT(*) FILTER (WHERE o.order_status IN %s) AS bad_orders,
                   -- net_revenue = TIỀN THỰC THU (sau giảm giá) — total_amount là giá niêm yết, CẤM dùng (INCIDENTS 11/06)
                   COALESCE(SUM(o.net_revenue) FILTER (WHERE o.order_status NOT IN %s), 0) AS rev
            FROM mb_order_attribution a JOIN orders o ON o.id = a.order_id
            WHERE a.order_date BETWEEN %s AND %s
              AND a.ad_id IN (SELECT ad_id FROM spl)
            GROUP BY a.ad_id
        ),
        cg AS (
            SELECT a.ad_id, SUM(p.gia_nhap * oi.quantity) AS cogs
            FROM mb_order_attribution a
            JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
            JOIN order_items oi ON oi.order_id = a.order_id
            JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
            JOIN wh_products p ON p.id = vm.product_id
            WHERE a.order_date BETWEEN %s AND %s
              AND a.ad_id IN (SELECT ad_id FROM spl)
            GROUP BY a.ad_id
        )
        SELECT COALESCE(SUM(s.spend), 0) AS spend,
               COALESCE(SUM(s.spend) FILTER (WHERE s.phan_loai = 'ma_test'), 0) AS spend_test,
               COALESCE(SUM(od.rev), 0) AS rev,
               COALESCE(SUM(cg.cogs), 0) AS cogs,
               COALESCE(SUM(od.orders), 0) AS orders,
               COALESCE(SUM(od.bad_orders), 0) AS bad_orders,
               COALESCE(SUM(s.spend) FILTER (
                   WHERE COALESCE(od.orders,0) = 0 AND s.phan_loai <> 'ma_test'), 0) AS spend_0don,
               COALESCE(SUM(s.spend) FILTER (
                   WHERE COALESCE(od.rev,0) < s.spend * %s AND s.phan_loai <> 'ma_test'), 0) AS spend_lo
        FROM spl s
        LEFT JOIN od ON od.ad_id = s.ad_id
        LEFT JOIN cg ON cg.ad_id = s.ad_id
    """, (d_to, who_param, d_from, d_to, _BAD_STATUSES, _BAD_STATUSES, d_from, d_to,
          _BAD_STATUSES, d_from, d_to, ADS_COST_MULTIPLIER))
    m = {k: (float(v) if v is not None else 0.0) for k, v in rows[0].items()}
    spend = m["spend"]
    nontest = spend - m["spend_test"]
    m["spend_nontest"] = nontest
    m["spend_vat"] = round(spend * ADS_COST_MULTIPLIER)
    m["profit"] = round(m["rev"] - m["cogs"] - spend * ADS_COST_MULTIPLIER)
    m["orders"] = int(m["orders"])
    m["bad_orders"] = int(m["bad_orders"])
    m["pct_test"] = round(100.0 * m["spend_test"] / spend, 1) if spend else 0.0
    m["pct_lo"] = round(100.0 * m["spend_lo"] / nontest, 1) if nontest else 0.0
    m["pct_0don"] = round(100.0 * m["spend_0don"] / nontest, 1) if nontest else 0.0
    m["pct_hoan"] = round(100.0 * m["bad_orders"] / m["orders"], 1) if m["orders"] else 0.0
    m["pct_ads_dt"] = round(100.0 * spend * ADS_COST_MULTIPLIER / m["rev"], 1) if m["rev"] else None
    return m


def _test_board(cur, who_clause: str, who_param, limit_vnd: float):
    """Túi test: campaign trên page TEST — spend TÍCH LŨY từ 04/06/2026 (mốc có
    data ad-level đáng tin), so hạn mức bằng số ĐÃ VAT (chuẩn quy tiền kế toán).
    Vượt hạn mức mà page chưa WIN → flag đỏ. Đơn chỉ đếm đơn thực (loại hoàn/hủy)."""
    today_iso = now_hcm().date().isoformat()
    rows = _q(cur, f"""
        WITH {_LBL_CTE},
        camp AS (
            SELECT e.campaign_id, MAX(e.campaign_name) AS campaign_name,
                   MAX(e.page_id) AS page_id, MAX(e.account_id) AS account_id,
                   SUM(e.spend) AS spend_all,
                   MIN(e.metric_date) AS started,
                   ARRAY_AGG(DISTINCT e.ad_id) AS ad_ids
            FROM mb_fb_entity_daily e
            {_ACC_JOIN}
            WHERE {who_clause} AND e.campaign_id <> '' AND e.metric_date >= %s
            GROUP BY e.campaign_id
        )
        SELECT c.campaign_name, c.spend_all, c.started,
               (SELECT COUNT(*) FROM mb_order_attribution a
                JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
                WHERE a.ad_id = ANY(c.ad_ids)) AS orders,
               (SELECT COALESCE(NULLIF(us.full_name,''), us.username)
                FROM user_ad_account_assignments ua JOIN users us ON us.id = ua.user_id
                WHERE ua.ad_account_id = c.account_id AND ua.assigned_to IS NULL LIMIT 1) AS nv_name
        FROM camp c
        JOIN lbl l ON l.page_id = c.page_id AND l.phan_loai = 'ma_test'
        WHERE c.spend_all > 0
        ORDER BY c.spend_all DESC
        LIMIT 25
    """, (today_iso, who_param, _TEST_TRACK_FROM, _BAD_STATUSES))
    limit_safe = max(float(limit_vnd or 0), 1.0)
    for r in rows:
        sp_vat = float(r["spend_all"]) * ADS_COST_MULTIPLIER
        r["spend_all"] = round(sp_vat)
        r["pct_limit"] = round(100.0 * sp_vat / limit_safe, 1)
        r["over"] = sp_vat > limit_safe
    return rows


def _heartbeat(cur, nv_rows, hb_days, d_from, d_to):
    """Lưới nhịp đập + khối tiền per-NV (đã tách test)."""
    nv_ids = [r["id"] for r in nv_rows]
    if not nv_ids:
        return []
    hb_from, hb_to = hb_days[0].isoformat(), hb_days[-1].isoformat()
    hb_ads = {(r["user_id"], r["d"]) for r in _q(cur, """
        SELECT DISTINCT ua.user_id, e.metric_date AS d
        FROM mb_fb_entity_daily e
        JOIN user_ad_account_assignments ua ON ua.ad_account_id = e.account_id
            AND e.metric_date >= ua.assigned_from
            AND (ua.assigned_to IS NULL OR e.metric_date <= ua.assigned_to)
        WHERE ua.user_id = ANY(%s) AND e.metric_date BETWEEN %s AND %s AND e.spend > 0
    """, (nv_ids, hb_from, hb_to))}
    hb_orders = {(r["user_id"], r["d"]) for r in _q(cur, """
        SELECT DISTINCT ua.user_id, a.order_date AS d
        FROM mb_order_attribution a
        JOIN mb_fb_entity_daily e ON e.ad_id = a.ad_id
        JOIN user_ad_account_assignments ua ON ua.ad_account_id = e.account_id
            AND a.order_date >= ua.assigned_from
            AND (ua.assigned_to IS NULL OR a.order_date <= ua.assigned_to)
        WHERE ua.user_id = ANY(%s) AND a.order_date BETWEEN %s AND %s
    """, (nv_ids, hb_from, hb_to))}
    hb_gps = {(int(r["user_id"]), r["d"]) for r in _q(cur, """
        SELECT DISTINCT user_id, date AS d FROM cc_attendance
        WHERE user_id = ANY(%s) AND date BETWEEN %s AND %s AND check_in IS NOT NULL
    """, ([str(i) for i in nv_ids], hb_from, hb_to))}

    out = []
    for nv in nv_rows:
        m = _money(cur, "u.id = %s", nv["id"], d_from, d_to)
        cells, silent_streak, streak = [], 0, 0
        for d in hb_days:
            traces = {"ads": (nv["id"], d) in hb_ads,
                      "don": (nv["id"], d) in hb_orders,
                      "gps": (nv["id"], d) in hb_gps}
            n = sum(traces.values())
            level = "good" if n >= 2 else ("warn" if n == 1 else "dead")
            streak = streak + 1 if level == "dead" else 0
            silent_streak = max(silent_streak, streak)
            cells.append({"d": d.strftime("%d/%m"), "level": level, **traces})
        out.append({**nv, "cells": cells, "silent_streak": silent_streak,
                    "spend": round(m["spend"]), "rev": round(m["rev"]),
                    "orders": m["orders"],
                    "spend_test": round(m["spend_test"]),
                    "spend_0don": round(m["spend_0don"]),
                    "pct_ads_dt": m["pct_ads_dt"]})
    out.sort(key=lambda x: (-x["silent_streak"], -x["spend_0don"]))
    return out


def _camp_prices(cur, who_clause: str, who_param, d_from, d_to, min_spend=300000):
    rows = _q(cur, f"""
        WITH {_LBL_CTE},
        sp AS (
            SELECT e.campaign_id, MAX(e.campaign_name) AS campaign_name, MAX(e.page_id) AS page_id,
                   SUM(e.spend) AS spend, ARRAY_AGG(DISTINCT e.ad_id) AS ad_ids
            FROM mb_fb_entity_daily e
            {_ACC_JOIN}
            WHERE {who_clause} AND e.metric_date BETWEEN %s AND %s AND e.campaign_id <> ''
            GROUP BY e.campaign_id
            HAVING SUM(e.spend) >= %s
        )
        SELECT sp.campaign_name, sp.spend,
               COALESCE(l.phan_loai, 'ma_win') AS phan_loai,
               (SELECT COUNT(*) FROM mb_order_attribution a
                JOIN orders o ON o.id = a.order_id AND o.order_status NOT IN %s
                WHERE a.order_date BETWEEN %s AND %s AND a.ad_id = ANY(sp.ad_ids)) AS orders
        FROM sp LEFT JOIN lbl l ON l.page_id = sp.page_id
    """, (d_to, who_param, d_from, d_to, min_spend, _BAD_STATUSES, d_from, d_to))
    for c in rows:
        c["cp_don"] = round(float(c["spend"]) / c["orders"]) if c["orders"] else None
    rows.sort(key=lambda x: (x["cp_don"] is None, x["cp_don"] or 0))
    return rows[:12]


def _team_pulse_health(cur, team_id, hb_days):
    """% NV-ngày có ÍT NHẤT 1 dấu vết (ads/đơn/GPS) trong cửa sổ nhịp đập.
    1 query nhẹ cho scoreboard (không loop _money per NV)."""
    hb_from, hb_to = hb_days[0].isoformat(), hb_days[-1].isoformat()
    rows = _q(cur, """
        WITH nvs AS (
            SELECT u.id FROM users u
            WHERE u.team_id = %s
              AND EXISTS (SELECT 1 FROM user_ad_account_assignments ua
                          WHERE ua.user_id = u.id AND ua.assigned_to IS NULL)
        ),
        traces AS (
            SELECT DISTINCT ua.user_id AS uid, e.metric_date AS d
            FROM mb_fb_entity_daily e
            JOIN user_ad_account_assignments ua ON ua.ad_account_id = e.account_id
                AND e.metric_date >= ua.assigned_from
                AND (ua.assigned_to IS NULL OR e.metric_date <= ua.assigned_to)
            WHERE ua.user_id IN (SELECT id FROM nvs)
              AND e.metric_date BETWEEN %s AND %s AND e.spend > 0
            UNION
            SELECT DISTINCT ua.user_id, a.order_date
            FROM mb_order_attribution a
            JOIN mb_fb_entity_daily e ON e.ad_id = a.ad_id
            JOIN user_ad_account_assignments ua ON ua.ad_account_id = e.account_id
                AND a.order_date >= ua.assigned_from
                AND (ua.assigned_to IS NULL OR a.order_date <= ua.assigned_to)
            WHERE ua.user_id IN (SELECT id FROM nvs)
              AND a.order_date BETWEEN %s AND %s
            UNION
            SELECT DISTINCT ca.user_id::int, ca.date
            FROM cc_attendance ca
            WHERE ca.user_id IN (SELECT id::text FROM nvs)
              AND ca.date BETWEEN %s AND %s AND ca.check_in IS NOT NULL
        )
        SELECT (SELECT COUNT(*) FROM nvs) AS nv_count,
               (SELECT COUNT(*) FROM traces t JOIN nvs ON nvs.id = t.uid) AS active_days
    """, (team_id, hb_from, hb_to, hb_from, hb_to, hb_from, hb_to))
    nv_count = int(rows[0]["nv_count"] or 0)
    total = nv_count * len(hb_days)
    if not total:
        return None
    return min(100.0, round(100.0 * int(rows[0]["active_days"] or 0) / total, 1))


def _leader_score(cur, team_id, money, hb_days):
    """🎖️ Điểm leader = 60 KỶ LUẬT (30 xử lý thẻ + 30 nhịp đập)
    + 40 TIỀN (15 %dưới hòa vốn · 10 %0 đơn · 15 %Ads/DT). Công thức công khai.

    <70 hai tháng liên tiếp = thay người (luật sếp 11/06)."""
    def _linear(value, full_at, zero_at, points):
        """value ≤ full_at → trọn điểm; ≥ zero_at → 0; giữa → tuyến tính."""
        if value is None:
            return points / 2.0
        if value <= full_at:
            return float(points)
        if value >= zero_at:
            return 0.0
        return points * (zero_at - value) / (zero_at - full_at)

    st = _alert_stats(cur, team_id)
    # Không có thẻ nào trong 30 ngày = team sạch → trọn điểm
    p_alert = 30.0 if not st["total"] else round(30.0 * st["acted"] / st["total"], 1)
    pulse = _team_pulse_health(cur, team_id, hb_days)
    p_pulse = round(30.0 * (pulse or 0) / 100.0, 1)
    p_lo = round(_linear(money["pct_lo"], 10, 30, 15), 1)
    p_0don = round(_linear(money["pct_0don"], 3, 15, 10), 1)
    # rev=0 mà VẪN đốt tiền = tệ nhất, 0 điểm — không được nửa điểm free (audit 12/06)
    if money["pct_ads_dt"] is None and money["spend"] > 0:
        p_adsdt = 0.0
    else:
        p_adsdt = round(_linear(money["pct_ads_dt"], 25, 45, 15), 1)
    total = round(p_alert + p_pulse + p_lo + p_0don + p_adsdt)
    return {
        "score": total,
        "p_alert": p_alert, "alert_acted": st["acted"], "alert_total": st["total"],
        "p_pulse": p_pulse, "pulse": pulse,
        "p_lo": p_lo, "p_0don": p_0don, "p_adsdt": p_adsdt,
        "grade": "xuất sắc" if total >= 85 else ("đạt" if total >= 70 else "cảnh báo"),
    }


def _open_alerts(cur, team_id=None, nv_user_id=None):
    cond, params = "", []
    if team_id is not None:
        cond = "AND team_id = %s"
        params.append(team_id)
    if nv_user_id is not None:
        cond += " AND nv_user_id = %s"
        params.append(nv_user_id)
    today = now_hcm().date()  # KHÔNG dùng CURRENT_DATE — DB chạy UTC, lệch sau 17h VN
    return _q(cur, f"""
        SELECT id, alert_date, alert_type, campaign_name, page_name, detail, nv_name, status
        FROM lb_alert_log
        WHERE status = 'open' AND alert_date >= %s::date - 7 {cond}
        ORDER BY alert_date DESC, id DESC
        LIMIT 30
    """, (today.isoformat(),) + tuple(params))


def _alert_stats(cur, team_id):
    """Kỷ luật 30 ngày: phát ra / đã hành động THẬT.

    Luật sếp 12/06: bấm "Đã xử lý" thẻ đốt-tiền chưa đủ — hôm sau spend của ad đó
    phải THỰC GIẢM (≤25% mức thẻ) mới tính điểm. Thẻ auto_closed (oan, đơn về sau)
    không tính vào mẫu số."""
    today = now_hcm().date()  # KHÔNG CURRENT_DATE — DB chạy UTC
    rows = _q(cur, """
        SELECT COUNT(*) FILTER (WHERE status <> 'auto_closed') AS total,
               COUNT(*) FILTER (WHERE status NOT IN ('open', 'auto_closed') AND (
                   alert_type <> 'dot_0don'
                   OR status <> 'resolved'
                   OR acted_at::date >= %s
                   OR NOT EXISTS (
                       SELECT 1 FROM mb_fb_entity_daily e
                       WHERE e.ad_id = lb_alert_log.ad_id
                         AND e.metric_date = acted_at::date + 1
                         AND e.spend > lb_alert_log.spend * 0.25)
               )) AS acted
        FROM lb_alert_log
        WHERE team_id = %s AND alert_date >= %s::date - 30
    """, (today.isoformat(), team_id, today.isoformat()))
    r = rows[0]
    r["pct"] = round(100.0 * r["acted"] / r["total"], 0) if r["total"] else None
    return r


@leader_brain_bp.route("/alert/<int:alert_id>/act", methods=["POST"])
@_login_required
def alert_act(alert_id: int):
    tier, scope_id = _viewer()
    action = (request.form.get("action") or "").strip()
    note = (request.form.get("note") or "").strip()[:500]
    if action not in ("resolved", "kept", "transferred"):
        return "Hành động không hợp lệ", 400
    # Luật sếp 12/06: xử lý thẻ phải kèm ghi chú (đã làm gì) — chống bấm cho có
    if not note:
        return "Phải ghi chú đã xử lý thế nào (tắt ad/đổi content/lý do giữ...) — bấm Quay lại và nhập ghi chú.", 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            rows = _q(cur, "SELECT team_id FROM lb_alert_log WHERE id = %s AND status = 'open'", (alert_id,))
            if not rows:
                return redirect(request.referrer or "/leader-brain/")
            if tier == "leader" and scope_id != rows[0]["team_id"]:
                return "Bạn chỉ xử lý được thẻ việc của team mình.", 403
            if tier not in ("full", "leader"):
                return "Chỉ leader/quản lý mới xử lý thẻ việc.", 403
            uid = int(session.get("user_id") or 0)
            uname = str(session.get("full_name") or session.get("username") or "")[:255]
            cur.execute("""
                UPDATE lb_alert_log
                SET status = %s, acted_by = %s, acted_by_name = %s, acted_at = NOW(), note = %s
                WHERE id = %s AND status = 'open'
            """, (action, uid, uname, note, alert_id))
        conn.commit()
    return redirect(request.referrer or "/leader-brain/")


@leader_brain_bp.route("/")
@_login_required
def index():
    tier, scope_id = _viewer()
    if tier is None:
        return "Leader Brain dành cho sếp/quản lý, leader kinh doanh và NV đang giữ TK quảng cáo.", 403
    if tier == "leader":
        return redirect(f"/leader-brain/team/{scope_id}")
    if tier == "nv":
        return redirect("/leader-brain/me")
    d_from, d_to, hb_days = _windows()
    # ĐIỂM luôn chấm trên kỳ chín CỐ ĐỊNH — không theo kỳ user lọc (audit 12/06 P0)
    sd_from, sd_to, s_days = _score_window()
    with get_conn() as conn:
        with conn.cursor() as cur:
            teams = _kd_teams(cur)
            for t in teams:
                t.update(_money(cur, "u.team_id = %s", t["id"], d_from, d_to))
                score_money = (t if (d_from, d_to) == (sd_from, sd_to)
                               else _money(cur, "u.team_id = %s", t["id"], sd_from, sd_to))
                t["lscore"] = _leader_score(cur, t["id"], score_money, s_days)
                t["lscore"]["window"] = f"{sd_from} → {sd_to}"
            teams.sort(key=lambda x: (x["lscore"]["score"], x["profit"]), reverse=True)
    return render_template("leader_brain/index.html",
                           teams=teams, d_from=d_from, d_to=d_to,
                           tier=tier, my_team=None)


@leader_brain_bp.route("/team/<int:team_id>")
@_login_required
def team_detail(team_id: int):
    tier, scope_id = _viewer()
    if tier not in ("full", "leader") or (tier == "leader" and scope_id != team_id):
        return "Bạn chỉ xem được chi tiết team của mình.", 403
    d_from, d_to, hb_days = _windows()
    with get_conn() as conn:
        with conn.cursor() as cur:
            team = next((t for t in _kd_teams(cur) if t["id"] == team_id), None)
            if not team:
                return "Team không thuộc phạm vi Leader Brain (chỉ team kinh doanh có NV chạy ads).", 404
            limit_vnd = _test_limit(cur)
            money = _money(cur, "u.team_id = %s", team_id, d_from, d_to)
            nvs = _q(cur, """
                SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name, u.username
                FROM users u
                WHERE u.team_id = %s
                  AND EXISTS (SELECT 1 FROM user_ad_account_assignments ua
                              WHERE ua.user_id = u.id AND ua.assigned_to IS NULL)
                ORDER BY 2
            """, (team_id,))
            heartbeat = _heartbeat(cur, nvs, hb_days, d_from, d_to)
            camps = _camp_prices(cur, "u.team_id = %s", team_id, d_from, d_to)
            tests = _test_board(cur, "u.team_id = %s", team_id, limit_vnd)
            alerts = _open_alerts(cur, team_id=team_id)
            alert_stats = _alert_stats(cur, team_id)
            # ĐIỂM chấm trên kỳ chín cố định, không theo kỳ lọc (audit 12/06 P0)
            sd_from, sd_to, s_days = _score_window()
            score_money = (money if (d_from, d_to) == (sd_from, sd_to)
                           else _money(cur, "u.team_id = %s", team_id, sd_from, sd_to))
            lscore = _leader_score(cur, team_id, score_money, s_days)
            lscore["window"] = f"{sd_from} → {sd_to}"
    return render_template("leader_brain/team.html",
                           team=team, money=money, heartbeat=heartbeat,
                           camps=camps, tests=tests, test_limit=round(limit_vnd),
                           alerts=alerts, alert_stats=alert_stats, can_act=True,
                           lscore=lscore,
                           d_from=d_from, d_to=d_to, tier=tier, me=False)


@leader_brain_bp.route("/me")
@_login_required
def my_view():
    tier, scope_id = _viewer()
    if tier is None:
        return "Bạn chưa được gán TK quảng cáo nào.", 403
    if tier == "full":
        return redirect("/leader-brain/")
    uid = int(session.get("user_id")) if tier == "leader" else scope_id
    d_from, d_to, hb_days = _windows()
    with get_conn() as conn:
        with conn.cursor() as cur:
            limit_vnd = _test_limit(cur)
            nv = _q(cur, """
                SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name, u.username
                FROM users u WHERE u.id = %s
            """, (uid,))[0]
            money = _money(cur, "u.id = %s", uid, d_from, d_to)
            heartbeat = _heartbeat(cur, [nv], hb_days, d_from, d_to)
            camps = _camp_prices(cur, "u.id = %s", uid, d_from, d_to, min_spend=100000)
            tests = _test_board(cur, "u.id = %s", uid, limit_vnd)
            alerts = _open_alerts(cur, nv_user_id=uid)
    team = {"id": 0, "team_name": f"Của tôi — {nv['name']}", "leader_name": nv["name"], "nv_ads": 1}
    return render_template("leader_brain/team.html",
                           team=team, money=money, heartbeat=heartbeat,
                           camps=camps, tests=tests, test_limit=round(limit_vnd),
                           alerts=alerts, alert_stats=None, can_act=(tier == "leader"),
                           lscore=None,
                           d_from=d_from, d_to=d_to, tier=tier, me=True)
