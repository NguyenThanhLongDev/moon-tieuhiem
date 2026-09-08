"""Telegram bridge: forward chat messages + daily digest."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _fmt_money(n: int) -> str:
    return f"{int(n):,}".replace(",", ".") + "đ"


def notify_new_message(user: dict, team_id: str, body: str, parsed: dict) -> None:
    """Mỗi tin nhắn chat → forward Telegram cho manager (group default)."""
    try:
        from telegram_common import safe_send
    except Exception:
        return

    team_name = team_id.replace("team-", "Team ").title()
    nv_name = user.get("full_name") or user.get("username") or f"NV#{user.get('id')}"
    items = parsed.get("items") or []

    lines = [f"💬 [{team_name} · {nv_name}]"]
    # Trích body gọn (max 200 char)
    short_body = body.strip()
    if len(short_body) > 200:
        short_body = short_body[:197] + "..."
    lines.append(f"📝 {short_body}")
    if items:
        for_date = parsed.get("for_date")
        total = sum(int(it.get("amount_vnd") or 0) for it in items)
        lines.append(f"")
        if for_date:
            lines.append(f"🧸 Trợ lý Lan đã đọc — ngày {for_date}:")
        else:
            # Thiếu ngày: đã lưu tạm TK+tiền, Lan đã hỏi lại NV trong chat.
            # KHÔNG cộng vào tổng hợp ngày nào đến khi NV bổ sung ngày.
            lines.append("🧸 Trợ lý Lan đã đọc (⚠ thiếu ngày — đã hỏi lại NV bổ sung):")
        for it in items[:10]:
            tk = it.get("tk_name") or "?"
            amt = int(it.get("amount_vnd") or 0)
            card = it.get("card_last4")
            card_s = f" · thẻ {card}" if card else ""
            lines.append(f"  • {tk}{card_s}: {_fmt_money(amt)}")
        lines.append(f"💰 Tổng tin nhắn: {_fmt_money(total)}")
        if not for_date:
            lines.append("⏳ Chưa áp ngày — chờ NV chọn 'Sửa ngày' trên web chat.")

    text = "\n".join(lines)
    try:
        safe_send(text, audience="default")
    except Exception as e:
        logger.warning("safe_send error: %s", e)


def send_daily_digest(target_date) -> None:
    """Cron 21h: tổng kết ngân sách target_date (default = ngày mai)."""
    try:
        from telegram_common import safe_send
    except Exception:
        return

    from db import get_conn
    from modules.budget_chat.repository import (
        summary_by_team, summary_by_user, list_users_should_report,
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            teams = summary_by_team(cur, target_date)
            users = summary_by_user(cur, target_date)
            should = list_users_should_report(cur, target_date)

    total = sum(t["total"] for t in teams)
    not_reported = [u for u in should if not u["reported"]]
    reported = [u for u in should if u["reported"]]

    lines = [
        f"📊 *NGÂN SÁCH NGÀY {target_date.strftime('%d/%m/%Y')}*",
        f"⏰ Tổng kết 21:00",
        "",
        f"💰 Tổng: *{_fmt_money(total)}*  ({sum(t['tk_count'] for t in teams)} TK · {len(teams)} team)",
        "",
        "▶ Theo team:",
    ]
    for t in teams:
        # Đếm NV trong team đã báo
        team_should = [u for u in should if u["team_id"] == t["team_id"]]
        team_done = [u for u in team_should if u["reported"]]
        icon = "✓" if len(team_done) == len(team_should) and len(team_should) > 0 else "⚠"
        name = t["team_id"].replace("team-", "Team ").title()
        lines.append(f"  {name:18s} {_fmt_money(t['total']):>14s}  {icon} {len(team_done)}/{len(team_should)} NV")
    lines.append("")

    if not_reported:
        lines.append(f"⚠ {len(not_reported)} NV chưa báo:")
        for u in not_reported[:15]:
            team_n = (u["team_id"] or "").replace("team-", "")
            lines.append(f"  • {u['full_name']} ({team_n})")
        if len(not_reported) > 15:
            lines.append(f"  ... và {len(not_reported)-15} NV khác")
    else:
        lines.append("✓ Tất cả NV đã báo đầy đủ.")

    lines.append("")
    lines.append("🔗 Chi tiết: https://tieuhiem.com/ngan-sach/tong-hop")
    safe_send("\n".join(lines), audience="default")


def cron_daily_digest():
    """Entry cho scheduler 21:00. Digest cho ngày mai."""
    from datetime import timedelta
    try:
        from tz_utils import now_hcm
        target = now_hcm().date() + timedelta(days=1)
    except Exception:
        from datetime import date
        target = date.today() + timedelta(days=1)
    send_daily_digest(target)


def cron_lan_nudge_unreported():
    """Sau 21h — Lan post tin nhắc NV chưa báo NS ngày mai vào team chat.

    Mỗi NV chưa báo → 1 tin nhắn riêng (random phrase, tag tên NV).
    Spam guard: tối đa 10 NV/team mỗi lần gọi (nếu >10 → nhóm phần còn lại 1 tin).
    """
    from datetime import timedelta
    from db import get_conn
    from modules.budget_chat.repository import (
        list_users_should_report, insert_lan_message,
    )
    from modules.budget_chat.lan_personality import ai_reply_nudge

    try:
        from tz_utils import now_hcm
        target = now_hcm().date() + timedelta(days=1)
    except Exception:
        from datetime import date
        target = date.today() + timedelta(days=1)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Load list teams kinh_doanh
                cur.execute("""
                    SELECT team_code FROM teams
                     WHERE status='active' AND team_type='kinh_doanh'
                """)
                team_codes = [r[0] for r in cur.fetchall()]

                total_posted = 0
                for team_code in team_codes:
                    should = list_users_should_report(cur, target, team_code)
                    not_reported = [u for u in should if not u["reported"]]
                    if not not_reported:
                        continue

                    # 10 NV đầu → tin riêng
                    individual = not_reported[:10]
                    extra = not_reported[10:]
                    target_vn = target.strftime("%d/%m/%Y")
                    for u in individual:
                        body = ai_reply_nudge(u["full_name"], target_vn)
                        insert_lan_message(cur, team_code, body)
                        total_posted += 1
                    if extra:
                        names = ", ".join(u["full_name"] for u in extra)
                        body = f"Còn {len(extra)} bạn nữa chưa báo NS: {names} 🌸 Mọi người gửi giúp Lan với nhé!"
                        insert_lan_message(cur, team_code, body)
                        total_posted += 1
                conn.commit()
        logger.info(f"cron_lan_nudge_unreported posted {total_posted} reminders for {target}")
    except Exception as exc:
        logger.warning("cron_lan_nudge_unreported error: %s", exc)
