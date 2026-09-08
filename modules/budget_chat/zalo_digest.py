"""Cron 21:05 — Lan gửi tổng hợp NS NGÀY MAI vào từng Zalo group team.

Flow:
1. Load mọi mapping zalo_thread_<tid> = team_code từ app_config.
2. Với mỗi team đã map → query budget_items for_date = ngày mai.
3. Group theo NV, sort tiền DESC. Format ngắn gọn Zalo.
4. POST sang bridge http://127.0.0.1:5051/send với thread_id + text.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def _fmt_money(n: int) -> str:
    return f"{int(n):,}".replace(",", ".") + "đ"


def _utf16_len(s: str) -> int:
    """Đếm độ dài chuỗi theo UTF-16 code units (JS .length).

    Emoji supplementary plane (🧸 📅 ...) = 2 code units. Zalo mention
    pos/len phải tính theo UTF-16 vì zca-js (JS) chạy trên JS strings.
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _build_digest_for_team(cur, team_code: str, target_date: date):
    """Build (text, mentions[]) cho 1 team. Trả (None, None) nếu rỗng."""
    cur.execute("""
        SELECT bi.user_id, COALESCE(u.full_name, u.username, '#'||bi.user_id::text) AS nv,
               u.zalo_uid,
               bi.tk_name_raw, bi.card_last4, bi.amount_vnd
          FROM budget_items bi
     LEFT JOIN users u ON u.id = bi.user_id
         WHERE bi.team_id = %s
           AND bi.for_date = %s
           AND bi.deleted_at IS NULL
         ORDER BY bi.amount_vnd DESC, bi.id ASC
    """, (team_code, target_date))
    rows = cur.fetchall()
    if not rows:
        return None, None

    by_nv: dict[int, dict] = {}
    for uid, nv, zalo_uid, tk, card, amt in rows:
        d = by_nv.setdefault(int(uid), {"nv": nv, "zalo_uid": zalo_uid, "items": [], "total": 0})
        d["items"].append({"tk": tk or "?", "card": card or "", "amt": int(amt or 0)})
        d["total"] += int(amt or 0)

    grand = sum(d["total"] for d in by_nv.values())
    nv_sorted = sorted(by_nv.values(), key=lambda x: x["total"], reverse=True)

    team_label = (team_code or "").replace("team-", "Team ").title() or "Team"
    date_vn = target_date.strftime("%d/%m/%Y")
    lines: list[str] = []
    mentions: list[dict] = []
    lines.append(f"🧸 TỔNG HỢP NS — {team_label}")
    lines.append(f"📅 Ngày mai: {date_vn}")
    lines.append("─────────────")
    for idx, nv in enumerate(nv_sorted, start=1):
        # Build header với tag @<nv> nếu có zalo_uid
        prefix = f"{idx}. "
        if nv.get("zalo_uid"):
            tag = f"@{nv['nv']}"
            # Pos theo UTF-16 (Zalo/JS dùng UTF-16 code units, không phải char count)
            current_text = "\n".join(lines)
            pos = _utf16_len(current_text) + 1 + _utf16_len(prefix)  # +1 cho '\n' sắp thêm
            mentions.append({"pos": pos, "uid": nv["zalo_uid"], "len": _utf16_len(tag)})
            lines.append(f"{prefix}{tag}  ({_fmt_money(nv['total'])})")
        else:
            lines.append(f"{prefix}{nv['nv']}  ({_fmt_money(nv['total'])})")
        for it in nv["items"]:
            card_s = f" thẻ {it['card']}" if it["card"] else ""
            lines.append(f"   • TK {it['tk']}{card_s}: {_fmt_money(it['amt'])}")
    lines.append("─────────────")
    lines.append(f"💰 Tổng nhóm ngày mai: {_fmt_money(grand)}")
    lines.append("⚠ Có gì sai sót anh chị nhắn lại Lan check nhé!")
    return "\n".join(lines), mentions


def _send_via_bridge(thread_id: str, text: str, mentions: Optional[list] = None) -> bool:
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        logger.warning("zalo_digest: ZALO_BRIDGE_SECRET không có — skip thread %s", thread_id)
        return False
    url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
    payload = {"thread_id": thread_id, "text": text}
    if mentions:
        payload["mentions"] = mentions
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"X-Bridge-Secret": secret},
            timeout=10,
        )
        ok = r.status_code == 200
        logger.info("zalo_digest: thread=%s status=%s body=%s", thread_id, r.status_code, r.text[:120])
        return ok
    except Exception as exc:
        logger.warning("zalo_digest: send error thread=%s: %s", thread_id, exc)
        return False


def send_zalo_team_digest() -> int:
    """Entry point cron. Trả số group đã gửi thành công."""
    try:
        from tz_utils import now_hcm
        today = now_hcm().date()
    except Exception:
        today = date.today()
    target = today + timedelta(days=1)

    from app_ctx import load_config
    from db import get_conn

    cfg = load_config()
    # thread_id → team_code
    threads: dict[str, str] = {}
    for k, v in cfg.items():
        if isinstance(k, str) and k.startswith("zalo_thread_"):
            threads[k[len("zalo_thread_"):]] = str(v).strip()

    if not threads:
        logger.info("zalo_digest: chưa có thread nào được map — skip")
        return 0

    sent = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Group theo team_code để build 1 lần / team (nếu 1 team có nhiều thread)
            team_to_threads: dict[str, list[str]] = {}
            for tid, tcode in threads.items():
                team_to_threads.setdefault(tcode, []).append(tid)

            for team_code, tids in team_to_threads.items():
                text, mentions = _build_digest_for_team(cur, team_code, target)
                if not text:
                    logger.info("zalo_digest: team %s không có NS ngày %s — skip", team_code, target)
                    continue
                for tid in tids:
                    if _send_via_bridge(tid, text, mentions):
                        sent += 1
    logger.info("zalo_digest: hoàn tất, gửi %d group", sent)
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    send_zalo_team_digest()
