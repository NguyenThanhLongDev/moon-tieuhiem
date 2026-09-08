"""Adapter NGUỒN: Pancake Chat (pages.fm public API) → LÕI fraud_detect.

ĐÃ VERIFY với token thật (2026-06-28):
  GET /api/public_api/v1/pages/{page_id}/conversations
      ?access_token=&since=<unix>&until=<unix>&page_number=N   (range ≤ 1 tháng)
  GET .../conversations/{conv_id}/messages?access_token=
      → mỗi tin có field `is_removed` (true = đã xoá) ← bắt gian lận trực tiếp.

LƯU Ý: 1 token = 1 PAGE (id trong JWT = page_id). Nhiều page → nhiều token (loop).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from . import store

API = "https://pages.fm/api/public_api/v1"


def _get(url: str, params: Dict[str, Any]) -> Any:
    try:
        r = requests.get(url, params=params, timeout=30)
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:200], "success": False}
    except Exception as e:
        return {"_err": str(e), "success": False}


def _customer_of(c: Dict[str, Any]) -> Dict[str, str]:
    custs = c.get("customers") or []
    if custs:
        cu = custs[0]
        return {"id": str(cu.get("fb_id") or cu.get("id") or ""), "name": cu.get("name") or ""}
    frm = c.get("from") or {}
    return {"id": str(frm.get("id") or ""), "name": frm.get("name") or ""}


def _sale_of(c: Dict[str, Any], page_id: str) -> Optional[str]:
    # Ưu tiên NV được gán (assignee), rồi người gửi cuối nếu là admin người (bỏ bot)
    for u in (c.get("current_assign_users") or []):
        if u.get("name"):
            return u["name"]
    lsb = c.get("last_sent_by") or {}
    nm = lsb.get("admin_name")
    if nm and nm.lower() not in ("botcake", "bot", "system"):
        return nm
    return None


def _fetch_messages(page_id: str, conv_id: str, token: str) -> List[Dict[str, Any]]:
    js = _get(f"{API}/pages/{page_id}/conversations/{conv_id}/messages", {"access_token": token})
    out = []
    for m in (js.get("messages") or []):
        mid = str(m.get("id") or "")
        if not mid:
            continue
        frm = m.get("from") or {}
        is_page = str(frm.get("id") or "") == str(page_id)
        out.append({
            "msg_key": mid,
            "direction": "out" if is_page else "in",   # out = sale/page, in = khách
            "content": (m.get("message") or "")[:2000],
            "sent_at": m.get("inserted_at") or None,
            "is_removed": bool(m.get("is_removed")),
        })
    return out


def sync_page(page_id: str, token: str, since_unix: int, until_unix: int,
              page_name: str = "", max_pages: int = 50, fetch_msgs: bool = True) -> Dict[str, Any]:
    """Kéo hội thoại 1 page (1 token) trong [since, until] (≤1 tháng) → ingest vào LÕI."""
    conv_n = msg_n = del_n = 0
    pn = 1
    while pn <= max_pages:
        js = _get(f"{API}/pages/{page_id}/conversations", {
            "access_token": token, "since": since_unix, "until": until_unix, "page_number": pn,
        })
        if not js.get("success"):
            if pn == 1:
                return {"page_id": page_id, "error": js.get("message") or js.get("_err") or "fail"}
            break
        convs = js.get("conversations") or []
        if not convs:
            break
        for c in convs:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            cust = _customer_of(c)
            canon = {
                "conv_key": f"pancake:{cid}",
                "page_id": str(c.get("page_id") or page_id),
                "page_name": page_name,
                "customer_id": cust["id"],
                "customer_name": cust["name"],
                "assigned_sale": _sale_of(c, page_id),
                "status": "open",
                "last_customer_msg_at": None,
                "last_sale_msg_at": c.get("updated_at"),
            }
            msgs = _fetch_messages(page_id, cid, token) if fetch_msgs else []
            res = store.ingest_conversation(canon, msgs, source="pancake")
            conv_n += 1
            msg_n += res.get("messages", 0)
            del_n += res.get("deleted_detected", 0)
            if fetch_msgs:
                time.sleep(0.1)
        pn += 1
        time.sleep(0.15)
    return {"page_id": page_id, "conversations": conv_n, "messages": msg_n, "deleted_detected": del_n}
