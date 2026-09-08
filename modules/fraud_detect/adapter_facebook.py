"""Adapter FACEBOOK cho Bắt sale gian lận — kéo hội thoại thẳng từ Graph API.

Không cần Pancake Chat. Điều kiện mỗi page:
  1. Một token trong FACEBOOK_ACCESS_TOKENS là ADMIN page (task MESSAGING)
  2. Token có scope `pages_messaging` (đọc hội thoại) — thiếu thì page bị bỏ qua
  3. (tuỳ chọn) scope `pages_manage_engagement` → đọc danh sách khách bị CHẶN

Cơ chế: mỗi lần sync chụp toàn bộ tin nhắn → store.ingest_conversation so bản
chụp cũ, tin biến mất = sale xoá → cờ đỏ. Khách nằm trong /blocked → cờ vàng.

Chạy tay:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python -m modules.fraud_detect.adapter_facebook
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("fd_adapter_facebook")

GRAPH = "https://graph.facebook.com/v20.0"


class RateLimited(Exception):
    """FB trả (#4) Application request limit reached — phải DỪNG, gọi tiếp vô ích."""


def _get(url: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
        except Exception:
            err = {"message": str(e)}
        # (#4)/(#17) = vượt hạn mức app. Gọi tiếp chỉ đốt thêm quota và kéo dài
        # thời gian bị khoá → ném lên để sync_all dừng hẳn (sếp 12/08).
        if err.get("code") in (4, 17, 32, 613) or err.get("is_transient"):
            raise RateLimited(err.get("message", "rate limit"))
        logger.debug("graph error: %s", err.get("message", "")[:100])
        return None
    except Exception as exc:
        logger.debug("graph error: %s", str(exc)[:80])
        return None


def _paginate(url: str, cap: int = 2000) -> List[dict]:
    out: List[dict] = []
    while url:
        d = _get(url)
        if d is None:
            break
        out.extend(d.get("data", []))
        if len(out) >= cap:
            break
        url = (d.get("paging") or {}).get("next") or ""
    return out


def _env_tokens() -> List[str]:
    toks: List[str] = []
    for v in (os.environ.get("FACEBOOK_ACCESS_TOKENS", ""),
              os.environ.get("FACEBOOK_ACCESS_TOKEN", "")):
        for t in v.replace("\n", ",").split(","):
            t = t.strip()
            if t and t not in toks:
                toks.append(t)
    return toks


def _has_messaging_scope(user_token: str) -> bool:
    d = _get(f"{GRAPH}/debug_token?input_token={user_token}&access_token={user_token}")
    try:
        return "pages_messaging" in set((d or {}).get("data", {}).get("scopes", []))
    except Exception:
        return False


def discover_pages() -> List[Dict[str, str]]:
    """Mọi page admin có task MESSAGING → (page_id, name, page_token).

    Page được NHIỀU token quản → ƯU TIÊN page-token sinh từ user-token CÓ
    pages_messaging (nếu không sẽ bị 403). Quét token có quyền tin nhắn TRƯỚC.
    """
    tokens = _env_tokens()
    msg_toks = [t for t in tokens if _has_messaging_scope(t)]
    other_toks = [t for t in tokens if t not in msg_toks]
    seen: set = set()
    pages: List[Dict[str, str]] = []
    for t in msg_toks + other_toks:   # token tin nhắn trước → page overlap lấy token đúng
        d = _get(f"{GRAPH}/me/accounts?fields=id,name,tasks,access_token&limit=200&access_token={t}")
        if not d:
            continue
        for p in d.get("data", []):
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            if "MESSAGING" not in (p.get("tasks") or []):
                continue
            ptok = p.get("access_token") or ""
            if not ptok:
                continue
            seen.add(pid)
            pages.append({"page_id": pid, "page_name": p.get("name", ""), "token": ptok})
    return pages


def _page_sale_map() -> Dict[str, str]:
    """page_id -> username NV phụ trách (qua binding page→shop → NV giữ shop)."""
    out: Dict[str, str] = {}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (b.page_id) b.page_id::text, u.username
                  FROM fb_page_shop_binding b
                  JOIN user_shop_assignments a ON a.shop_id = b.pos_shop_id AND a.assigned_to IS NULL
                  JOIN users u ON u.id = a.user_id
                 WHERE b.assigned_to IS NULL
                 ORDER BY b.page_id, a.assigned_from DESC
            """)
            out = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as exc:
        logger.warning("page→sale map lỗi: %s", str(exc)[:80])
    return out


def _blocked_ids(page_id: str, token: str) -> set:
    """ID khách bị page chặn (cần pages_manage_engagement — thiếu thì trả rỗng)."""
    d = _paginate(f"{GRAPH}/{page_id}/blocked?limit=100&access_token={token}", cap=500)
    return {str(x.get("id")) for x in d if x.get("id")}


def _avatar_url(user_id: str, token: str) -> str:
    """URL ảnh đại diện khách (resolve 1 lần lúc sync, lưu vào DB)."""
    d = _get(f"{GRAPH}/{user_id}/picture?redirect=0&type=normal&access_token={token}")
    try:
        return (d or {}).get("data", {}).get("url", "") or ""
    except Exception:
        return ""


def sync_page(page: Dict[str, str], sale_map: Dict[str, str],
              conv_limit: int = 200, msg_limit: int = 400,
              since_hours: Optional[int] = None,
              want_blocked: bool = True) -> Dict[str, int]:
    """Kéo hội thoại + tin nhắn 1 page → đổ vào store. Trả thống kê.

    since_hours: chỉ xử lý hội thoại có updated_time trong N giờ qua. Mỗi hội
    thoại tốn 1 lượt gọi /messages, nên lọc trước giúp giảm 80-90% số lượt gọi
    Graph API (nguyên nhân lỗi #4 — sếp 12/08). None = quét tất (bản full).
    """
    from modules.fraud_detect import store

    pid, ptok = page["page_id"], page["token"]
    stats = {"convs": 0, "msgs": 0, "deleted": 0, "blocked": 0, "no_perm": 0}

    convs = _paginate(
        f"{GRAPH}/{pid}/conversations?fields=participants,updated_time&limit=50&access_token={ptok}",
        cap=conv_limit)
    # Lọc theo thời gian cập nhật — hội thoại im lìm thì không cần chụp lại
    if since_hours and convs:
        import datetime as _dt
        _cut = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=since_hours)
        _fresh = []
        for _c in convs:
            _ts = str(_c.get("updated_time") or "")
            try:
                if _dt.datetime.fromisoformat(_ts.replace("Z", "+00:00")) >= _cut:
                    _fresh.append(_c)
            except Exception:
                _fresh.append(_c)   # không đọc được ngày → giữ cho an toàn
        stats["skipped_old"] = len(convs) - len(_fresh)
        convs = _fresh
    if not convs:
        # phân biệt: không có hội thoại vs thiếu quyền
        probe = _get(f"{GRAPH}/{pid}/conversations?limit=1&access_token={ptok}")
        if probe is None:
            stats["no_perm"] = 1
            return stats

    # Danh sách khách bị chặn đổi rất chậm → chỉ lấy ở bản FULL (1h sáng),
    # bản nhanh bỏ qua để tiết kiệm ~5 lượt gọi/page.
    blocked = _blocked_ids(pid, ptok) if want_blocked else set()
    stats["blocked_list"] = len(blocked)

    for c in convs:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        # khách = participant khác page
        cust_id, cust_name = "", ""
        for pt in (c.get("participants", {}).get("data") or []):
            if str(pt.get("id")) != pid:
                cust_id, cust_name = str(pt.get("id") or ""), pt.get("name", "")
                break
        msgs_raw = _paginate(
            f"{GRAPH}/{cid}/messages?fields=id,message,from,created_time&limit=100&access_token={ptok}",
            cap=msg_limit)
        messages = []
        last_in = last_out = None
        for m in msgs_raw:
            frm = str((m.get("from") or {}).get("id") or "")
            direction = "out" if frm == pid else "in"
            sent = m.get("created_time")
            if direction == "in":
                last_in = last_in or sent
            else:
                last_out = last_out or sent
            messages.append({
                "msg_key": f"fb_{m.get('id')}",
                "direction": direction,
                "content": (m.get("message") or "").strip() or "[media/sticker]",
                "sent_at": sent,
            })
        conv_status = "blocked" if (cust_id and cust_id in blocked) else "open"
        avatar = _avatar_url(cust_id, ptok) if cust_id else ""
        res = store.ingest_conversation(
            {
                "conv_key": f"fb_{cid}",
                "page_id": pid,
                "page_name": page.get("page_name", ""),
                "customer_id": cust_id,
                "customer_name": cust_name,
                "customer_avatar": avatar,
                "assigned_sale": sale_map.get(pid, ""),
                "status": conv_status,
                "last_customer_msg_at": last_in,
                "last_sale_msg_at": last_out,
            },
            messages, source="facebook")
        stats["convs"] += 1
        stats["msgs"] += len(messages)
        stats["deleted"] += int(res.get("deleted_detected") or 0)
        if conv_status == "blocked":
            stats["blocked"] += 1
    return stats


def sync_all(since_hours: Optional[int] = None, conv_limit: int = 200,
             want_blocked: bool = True) -> None:
    pages = discover_pages()
    mode = f"NHANH ({since_hours}h gần nhất)" if since_hours else "FULL"
    logger.info("=== FB fraud sync [%s]: %d page admin (MESSAGING) ===", mode, len(pages))
    sale_map = _page_sale_map()
    ok = skip = 0
    total_skipped = 0
    for p in pages:
        try:
            st = sync_page(p, sale_map, conv_limit=conv_limit,
                           since_hours=since_hours, want_blocked=want_blocked)
        except RateLimited as exc:
            logger.warning("⛔ FB chặn hạn mức (#4) — DỪNG sync tại page %s: %s",
                           p.get("page_name"), str(exc)[:80])
            logger.warning("   Đã xử lý %d page. Lần chạy sau sẽ tiếp tục.", ok)
            break
        if st.get("no_perm"):
            logger.info("  %s: token THIẾU pages_messaging — bỏ qua", p["page_name"])
            skip += 1
            continue
        total_skipped += st.get("skipped_old", 0)
        logger.info("  %s: %d conv · %d tin · %d tin bị xoá · %d khách bị chặn%s",
                    p["page_name"], st["convs"], st["msgs"], st["deleted"], st["blocked"],
                    (f" · bỏ qua {st['skipped_old']} hội thoại cũ" if st.get("skipped_old") else ""))
        ok += 1
    logger.info("=== DONE [%s]: %d page đọc được, %d page thiếu quyền, "
                "bỏ qua %d hội thoại cũ ===", mode, ok, skip, total_skipped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # --quick: chỉ hội thoại 26h gần nhất, bỏ /blocked → nhẹ hạn mức Graph API.
    _quick = "--quick" in sys.argv
    try:
        if _quick:
            sync_all(since_hours=26, conv_limit=100, want_blocked=False)
        else:
            sync_all()
    except RateLimited as _e:
        # Thoát ÊM (không traceback) — cron log 1 dòng, lần chạy sau tự tiếp tục.
        logger.warning("⛔ FB chặn hạn mức (#4) ngay từ đầu — bỏ lượt này: %s",
                       str(_e)[:90])
        sys.exit(0)
