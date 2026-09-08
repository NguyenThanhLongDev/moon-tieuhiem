"""Lan menu — sếp gõ 'menu' / '/menu' / '?' / 1-8 trong chat 1-1 → tra DB, trả info.

Whitelist sếp qua app_config['lan_admin_zalo_uids'] = csv list UID Zalo
(default '2859530271883968850' — Phùng Tưởng).

NV khác hỏi (menu hoặc info nhạy cảm) → từ chối nhẹ "chỉ sếp Tưởng xem được".

State 'đang chờ chọn số' = in-memory dict {sender_uid: ts} (TTL 5 phút) —
cho phép sếp gõ thẳng số sau khi xem menu mà không cần 'menu' lại.
"""
from __future__ import annotations
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# State: sếp vừa xem menu → cho phép input số tiếp theo
_MENU_PENDING: dict = {}  # {sender_uid: ts}
_MENU_TTL = 300  # 5 phút

_MENU_TRIGGERS = ("menu", "/menu", "?", "lan menu", "menu lan")


def _is_admin_uid(sender_uid: str) -> bool:
    """Check sender_uid có trong whitelist admin không."""
    if not sender_uid:
        return False
    try:
        from app_ctx import load_config
        raw = (load_config() or {}).get("lan_admin_zalo_uids",
                                        "2859530271883968850")
        uids = {u.strip() for u in str(raw).split(",") if u.strip()}
        return sender_uid in uids
    except Exception:
        return False


def _fmt_money(n) -> str:
    try:
        return f"{int(n or 0):,}".replace(",", ".") + "đ"
    except Exception:
        return str(n)


def _today_iso():
    try:
        from tz_utils import now_hcm
        return now_hcm().date()
    except Exception:
        from datetime import date as _d
        return _d.today()


def _menu_text() -> str:
    return ("🌷 Lan đây ạ sếp! Chọn số nha:\n"
            "1. Tổng NS ngày mai (chuẩn bị chạy)\n"
            "2. Tổng NS hôm nay (đang chạy)\n"
            "3. Tổng CP công ty hôm nay\n"
            "4. CP đang chờ duyệt\n"
            "5. Top 5 NV chi NS nhiều nhất ngày mai\n"
            "6. NS hôm nay vs thực chi FB hôm nay\n"
            "7. Team chưa báo NS ngày mai\n"
            "8. TK QC mới xuất hiện 7 ngày qua\n\n"
            "Gõ số (vd \"1\") hoặc \"menu\" xem lại 🌸")


def _is_menu_trigger(body: str) -> bool:
    b = (body or "").strip().lower()
    return b in _MENU_TRIGGERS


def _is_digit_choice(body: str) -> Optional[int]:
    b = (body or "").strip()
    if b.isdigit() and 1 <= int(b) <= 8:
        return int(b)
    return None


def _mark_menu_shown(sender_uid: str) -> None:
    _MENU_PENDING[sender_uid] = time.time()


def _has_pending_menu(sender_uid: str) -> bool:
    ts = _MENU_PENDING.get(sender_uid, 0)
    if not ts:
        return False
    if time.time() - ts > _MENU_TTL:
        _MENU_PENDING.pop(sender_uid, None)
        return False
    return True


# ── 8 menu items ──────────────────────────────────────────────────────────

def _ns_summary_for(d) -> str:
    """Reuse repository.summary_by_team — cùng logic với /ngan-sach/tong-hop."""
    from db import get_conn
    from modules.budget_chat.repository import summary_by_team, summary_tk_type
    with get_conn() as conn:
        with conn.cursor() as cur:
            teams = summary_by_team(cur, d)
            tk_type = summary_tk_type(cur, d)
    if not teams:
        return f"📊 NS {d.strftime('%d/%m/%Y')}:\n   (chưa team nào báo)"
    lines = [f"📊 NS {d.strftime('%d/%m/%Y')}:"]
    grand = 0
    for t in teams:
        tid = t["team_id"]
        lines.append(f"   {tid:14s} {_fmt_money(t['total']):>14s}  ({t['tk_count']} TK · {t['users']} NV)")
        grand += int(t["total"] or 0)
    lines.append("   " + "─" * 36)
    lines.append(f"   {'CTY (×TH)':14s} {_fmt_money(tk_type['total_cty']):>14s}  ({tk_type['tk_cty']} TK)")
    lines.append(f"   {'HKD':14s} {_fmt_money(tk_type['total_hkd']):>14s}  ({tk_type['tk_hkd']} TK)")
    lines.append(f"   {'TỔNG':14s} {_fmt_money(grand):>14s}")
    lines.append("\n🔗 tieuhiem.com/ngan-sach/tong-hop?date=" + d.isoformat())
    return "\n".join(lines)


def _item1_ns_tomorrow() -> str:
    """Item 1: NS NGÀY MAI (NV vừa báo, chuẩn bị chạy) — default của web."""
    from datetime import timedelta
    return _ns_summary_for(_today_iso() + timedelta(days=1))


def _item2_ns_today() -> str:
    """Item 2: NS HÔM NAY (đang chạy — NV báo từ hôm qua)."""
    return _ns_summary_for(_today_iso())


def _item3_cp_today() -> str:
    from db import get_conn
    d = _today_iso()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT category, COUNT(*) AS cnt, COALESCE(SUM(amount_vnd),0) AS total
                  FROM company_expense_items
                 WHERE occurred_date = %s
                   AND status = 'confirmed'
                   AND deleted_at IS NULL
                   AND COALESCE(category,'') != 'ads'
                 GROUP BY category
                 ORDER BY total DESC
            """, (d,))
            rows = cur.fetchall()
    if not rows:
        return f"💵 CP công ty hôm nay ({d.strftime('%d/%m/%Y')}):\n   (chưa có khoản nào duyệt)"
    label = {"salary": "Lương", "office": "Văn phòng", "utility": "Tiện ích",
             "other": "Khác"}
    lines = [f"💵 CP công ty đã duyệt hôm nay ({d.strftime('%d/%m/%Y')}):"]
    grand = 0
    for cat, cnt, total in rows:
        lines.append(f"   {label.get(cat, cat):12s} {_fmt_money(total):>14s}  ({cnt})")
        grand += int(total or 0)
    lines.append("   " + "─" * 30)
    lines.append(f"   {'TỔNG':12s} {_fmt_money(grand):>14s}")
    return "\n".join(lines)


def _item4_cp_pending() -> str:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, amount_vnd, LEFT(note,40), zalo_sender_name
                  FROM company_expense_items
                 WHERE status = 'pending' AND deleted_at IS NULL
                 ORDER BY id DESC LIMIT 10
            """)
            rows = cur.fetchall()
            cur.execute("""
                SELECT COUNT(*), COALESCE(SUM(amount_vnd),0)
                  FROM company_expense_items
                 WHERE status = 'pending' AND deleted_at IS NULL
            """)
            cnt, total = cur.fetchone()
    if not rows:
        return "✅ CP chờ duyệt: (không có khoản nào)"
    lines = [f"⏳ CP chờ duyệt: {cnt} khoản — tổng {_fmt_money(total)}"]
    for rid, amt, note, sender in rows[:8]:
        lines.append(f"   #{rid} {_fmt_money(amt):>12s} · {sender}: {note}")
    lines.append("\nDuyệt tại tieuhiem.com/chi-phi/khai-bao 🌸")
    return "\n".join(lines)


def _item5_top_ns_users() -> str:
    from db import get_conn
    from datetime import timedelta
    d = _today_iso() + timedelta(days=1)  # NS ngày mai — đồng bộ với menu 1
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.full_name, bi.team_id,
                       COUNT(*) AS tk_count,
                       COALESCE(SUM(bi.amount_vnd),0) AS total
                  FROM budget_items bi
                  LEFT JOIN users u ON u.id = bi.user_id
                 WHERE bi.for_date = %s
                 GROUP BY u.full_name, bi.team_id
                 ORDER BY total DESC LIMIT 5
            """, (d,))
            rows = cur.fetchall()
    if not rows:
        return f"🏆 Top NV chi NS ngày mai ({d.strftime('%d/%m/%Y')}):\n   (chưa có ai báo)"
    lines = [f"🏆 Top 5 NV chi NS ngày mai ({d.strftime('%d/%m/%Y')}):"]
    for i, (name, tid, cnt, total) in enumerate(rows, 1):
        lines.append(f"   {i}. {name or '?'} ({tid}) — {_fmt_money(total)} · {cnt} TK")
    return "\n".join(lines)


def _item6_ns_vs_actual() -> str:
    from db import get_conn
    d = _today_iso()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(amount_vnd),0) FROM budget_items WHERE for_date = %s
            """, (d,))
            ns_total = int(cur.fetchone()[0] or 0)
            # Dedup TK đa-shop (§4.3): fb_ads_daily_metrics có 1 row/shop —
            # TK gắn 2 shop bị nhân đôi nếu SUM thẳng (đã dính +13,9%, audit 12/06)
            cur.execute("""
                SELECT COALESCE(SUM(s),0) FROM (
                    SELECT MAX(spend) AS s FROM fb_ads_daily_metrics
                    WHERE metric_date = %s GROUP BY fb_ad_account_id
                ) t
            """, (d,))
            actual = float(cur.fetchone()[0] or 0)
    actual_vat = actual * 1.113
    diff = actual_vat - ns_total
    pct = (actual_vat / ns_total * 100) if ns_total > 0 else 0
    sign = "⚠ vượt" if diff > 0 else "✓ trong NS"
    return ("📊 NS đăng ký vs Thực chi FB hôm nay ({}):\n"
            "   NS đăng ký:      {}\n"
            "   Thực chi (×1.113): {}\n"
            "   {} {} ({:.0f}%)").format(
        d.strftime('%d/%m/%Y'),
        _fmt_money(ns_total),
        _fmt_money(actual_vat),
        sign,
        _fmt_money(abs(diff)),
        pct,
    )


def _item7_teams_no_report() -> str:
    from db import get_conn
    from datetime import timedelta
    d = _today_iso() + timedelta(days=1)  # ngày mai — kế toán nhắc NV chưa báo
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.team_code, t.team_name
                  FROM teams t
                 WHERE t.status = 'active' AND t.team_type = 'kinh_doanh'
                   AND NOT EXISTS (
                     SELECT 1 FROM budget_items bi
                      WHERE bi.team_id = t.team_code AND bi.for_date = %s
                   )
                 ORDER BY t.team_name
            """, (d,))
            rows = cur.fetchall()
    if not rows:
        return f"✅ Tất cả team kinh doanh đã báo NS ngày mai ({d.strftime('%d/%m/%Y')})"
    lines = [f"⚠ Team chưa báo NS ngày mai ({d.strftime('%d/%m/%Y')}):"]
    for code, name in rows:
        lines.append(f"   • {name} ({code})")
    return "\n".join(lines)


def _item8_new_accounts_7d() -> str:
    from db import get_conn
    from datetime import timedelta
    today = _today_iso()
    last7_start = today - timedelta(days=6)
    cutoff = last7_start - timedelta(days=1)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT fb_ad_account_id, account_name
                  FROM fb_ads_daily_metrics
                 WHERE metric_date >= %s
                   AND fb_ad_account_id NOT IN (
                     SELECT DISTINCT fb_ad_account_id FROM fb_ads_daily_metrics
                      WHERE metric_date < %s
                   )
                 ORDER BY account_name
            """, (last7_start, last7_start))
            rows = cur.fetchall()
    if not rows:
        return "✨ TK QC mới 7 ngày qua: (không có TK nào mới)"
    lines = [f"✨ TK QC mới xuất hiện 7 ngày qua ({len(rows)} TK):"]
    for aid, name in rows[:15]:
        lines.append(f"   • {name or '?'} ({aid})")
    if len(rows) > 15:
        lines.append(f"   ... và {len(rows)-15} TK khác")
    return "\n".join(lines)


_HANDLERS = {
    1: _item1_ns_tomorrow,
    2: _item2_ns_today,
    3: _item3_cp_today,
    4: _item4_cp_pending,
    5: _item5_top_ns_users,
    6: _item6_ns_vs_actual,
    7: _item7_teams_no_report,
    8: _item8_new_accounts_7d,
}


def handle_menu_if_any(sender_uid: str, sender_name: str, body: str) -> Optional[str]:
    """Xử lý trigger menu/digit. Trả text reply hoặc None nếu không phải menu input.

    Caller (web_app dispatcher) tự push reply về Zalo.
    """
    body = (body or "").strip()
    is_trigger = _is_menu_trigger(body)
    digit = _is_digit_choice(body)

    if not is_trigger and digit is None:
        return None

    # Có vẻ là menu input — check quyền
    if not _is_admin_uid(sender_uid):
        # NV thường gõ menu/số → từ chối nhẹ
        return (f"🤫 Thông tin này chỉ sếp Tưởng mới xem được ạ. "
                f"Bạn {sender_name or ''} có thắc mắc nghiệp vụ NS/CP, "
                "Lan vẫn hỗ trợ bình thường nhé 🌸").strip()

    # Sếp gõ menu trigger
    if is_trigger:
        _mark_menu_shown(sender_uid)
        return _menu_text()

    # Sếp gõ digit (1-8)
    if digit is not None:
        # Nếu chưa từng xem menu trong 5 phút → vẫn cho phép (admin tin được)
        try:
            text = _HANDLERS[digit]()
        except Exception as exc:
            logger.warning("Lan menu item %d error: %s", digit, exc, exc_info=True)
            return f"😢 Lan tra DB lỗi ({exc}). Sếp gõ 'menu' lại sau ít phút giúp ạ."
        _mark_menu_shown(sender_uid)
        return text + "\n\nGõ \"menu\" xem lại 🌸"

    return None
