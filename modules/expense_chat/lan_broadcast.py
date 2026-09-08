"""Lan broadcast — sếp gõ 'gửi <nhóm>: <nội dung>' trong DM với Lan → Lan forward.

Cú pháp:
    gửi <target>: <message>
    broadcast <target>: <message>
    thông báo <target>: <message>

Target examples:
    cty / toàn cty / toàn công ty / all      → group CTY lớn
    kinh doanh / sale / sales / all team     → 7 group team-* (sale leader)
    nam / team nam                           → 2 group team-nam
    nhật / minh / thanh / ken / công minh    → group team đó
    nhật, minh, nam (CSV)                    → nhiều team cùng lúc

Whitelist sếp qua app_config['lan_admin_zalo_uids'] (giống lan_menu).

Mỗi broadcast lưu vào bảng `lan_broadcasts` để audit.
"""
from __future__ import annotations
import logging
import os
import re
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Prefix lệnh — phải đứng đầu message
_TRIGGERS = ("gửi ", "gui ", "broadcast ", "thông báo ", "thong bao ", "tb ")

# Target alias → list team_code | "ALL_CTY" | "ALL_SALE"
_TARGET_ALIAS = {
    # Toàn cty (1 group lớn)
    "cty": "ALL_CTY",
    "toàn cty": "ALL_CTY",
    "toàn công ty": "ALL_CTY",
    "công ty": "ALL_CTY",
    "all": "ALL_CTY",
    "tất cả": "ALL_CTY",
    "moi nguoi": "ALL_CTY",
    "mọi người": "ALL_CTY",

    # Toàn bộ team sale
    "kinh doanh": "ALL_SALE",
    "kd": "ALL_SALE",
    "sale": "ALL_SALE",
    "sales": "ALL_SALE",
    "tất cả team": "ALL_SALE",
    "all team": "ALL_SALE",
    "toàn team": "ALL_SALE",

    # Team con (key chuẩn trong app_config zalo_thread_*)
    "nam": ["team-nam"],
    "team nam": ["team-nam"],

    "nhật": ["team-nhat"],
    "nhat": ["team-nhat"],
    "team nhật": ["team-nhat"],
    "team nhat": ["team-nhat"],

    "minh": ["team-minh"],
    "team minh": ["team-minh"],

    "thanh": ["team-thanh"],
    "team thanh": ["team-thanh"],

    "ken": ["team-ken"],
    "team ken": ["team-ken"],

    "công minh": ["team-congminh"],
    "cong minh": ["team-congminh"],
    "congminh": ["team-congminh"],
    "team công minh": ["team-congminh"],
    "team congminh": ["team-congminh"],
}


def _is_admin_uid(sender_uid: str) -> bool:
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


def _load_group_map() -> Tuple[Dict[str, List[Dict[str, str]]], Optional[str]]:
    """Đọc app_config → trả về:
       - groups_by_team: {team_code: [{thread_id, name}]}
       - cty_thread_id: thread_id của group cty lớn
    """
    groups_by_team: Dict[str, List[Dict[str, str]]] = {}
    cty_thread_id: Optional[str] = None
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT key, value FROM app_config
                    WHERE key LIKE 'zalo_thread_%'
                       OR key = 'zalo_expense_inbox'
                """)
                rows = cur.fetchall()
        for key, value in rows:
            if key == "zalo_expense_inbox":
                cty_thread_id = str(value).strip()
                continue
            if not key.startswith("zalo_thread_"):
                continue
            thread_id = key.replace("zalo_thread_", "")
            team_code = str(value).strip()
            groups_by_team.setdefault(team_code, []).append(
                {"thread_id": thread_id, "label": team_code}
            )
    except Exception as exc:
        logger.warning("load_group_map fail: %s", exc)
    return groups_by_team, cty_thread_id


def _resolve_targets(target_str: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """Parse target_str (vd 'nam' hoặc 'nam, minh') → list groups + list unknown labels."""
    groups_by_team, cty_id = _load_group_map()
    targets: List[Dict[str, str]] = []
    unknown: List[str] = []
    seen_threads: set = set()

    # Split CSV
    parts = [p.strip().lower() for p in re.split(r"[,/;]+", target_str) if p.strip()]
    if not parts:
        return [], []

    for part in parts:
        alias = _TARGET_ALIAS.get(part)
        if alias is None:
            unknown.append(part)
            continue

        if alias == "ALL_CTY":
            if cty_id and cty_id not in seen_threads:
                targets.append({"thread_id": cty_id, "label": "Toàn cty"})
                seen_threads.add(cty_id)
        elif alias == "ALL_SALE":
            for team_code, group_list in groups_by_team.items():
                if not team_code.startswith("team-"):
                    continue
                for g in group_list:
                    if g["thread_id"] not in seen_threads:
                        targets.append(g)
                        seen_threads.add(g["thread_id"])
        else:
            # alias là list team_code
            for tc in alias:
                for g in groups_by_team.get(tc, []):
                    if g["thread_id"] not in seen_threads:
                        targets.append(g)
                        seen_threads.add(g["thread_id"])

    return targets, unknown


# Trigger để xin LIST nhóm (không có nội dung gửi)
_LIST_TRIGGERS = ("gửi", "gui", "gửi?", "gui?", "gửi ?", "gui ?",
                  "nhóm", "nhom", "nhóm?", "nhom?",
                  "lan gửi gì", "lan gui gi",
                  "danh sách nhóm", "ds nhóm", "ds nhom")


def _is_list_request(body: str) -> bool:
    b = (body or "").strip().lower().rstrip(":?.! ")
    return b in _LIST_TRIGGERS


def _parse_command(body: str) -> Optional[Tuple[str, str]]:
    """Parse 'gửi nam: hello world' → ('nam', 'hello world').
    Return None nếu không match.
    """
    if not body:
        return None
    b = body.strip()
    bl = b.lower()
    matched_trigger = None
    for t in _TRIGGERS:
        if bl.startswith(t):
            matched_trigger = t
            break
    if not matched_trigger:
        return None
    remainder = b[len(matched_trigger):].strip()
    # Cần dấu ':' tách target và message
    if ":" not in remainder:
        return None
    target_str, message = remainder.split(":", 1)
    target_str = target_str.strip()
    message = message.strip()
    if not target_str or not message:
        return None
    return target_str, message


def _build_group_list_text() -> str:
    """Sinh menu liệt kê tất cả nhóm sếp có thể gửi."""
    groups_by_team, cty_id = _load_group_map()
    lines = ["🌷 Lan đang ở các nhóm sau ạ sếp:"]
    lines.append("")

    # Group lớn
    if cty_id:
        lines.append("📢 TOÀN CTY:")
        lines.append("   • cty / toàn cty / all → group CTY lớn")
        lines.append("")

    # Team sale
    team_groups = {k: v for k, v in groups_by_team.items()
                   if k.startswith("team-")}
    if team_groups:
        lines.append("👥 KINH DOANH (gõ 'kinh doanh' = gửi cả 7 team):")
        team_label = {
            "team-nhat": ("nhật", "Nhật"),
            "team-nam": ("nam", "Nam"),
            "team-minh": ("minh", "Minh"),
            "team-thanh": ("thanh", "Thanh"),
            "team-ken": ("ken", "Ken"),
            "team-congminh": ("công minh", "Công Minh"),
        }
        for code, (alias, display) in team_label.items():
            grs = team_groups.get(code, [])
            if grs:
                cnt = len(grs)
                suffix = f" ({cnt} group)" if cnt > 1 else ""
                lines.append(f"   • {alias} → team {display}{suffix}")

    lines.append("")
    lines.append("📝 CÁCH DÙNG:")
    lines.append("   gửi <nhóm>: <nội dung>")
    lines.append("")
    lines.append("💡 VÍ DỤ:")
    lines.append("   gửi nam: 7h sáng mai họp")
    lines.append("   gửi cty: Nghỉ lễ 30/4-1/5")
    lines.append("   gửi kinh doanh: KPI mới đã update")
    lines.append("   gửi nhật, minh: Test nhiều team")
    return "\n".join(lines)


def _send_one(thread_id: str, text: str, image_url: str = "") -> Tuple[bool, str]:
    """Gửi 1 tin qua bridge (text + optional image). Return (success, err_msg)."""
    import requests as _rq
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        return False, "ZALO_BRIDGE_SECRET chưa set"
    url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
    payload = {"thread_id": thread_id, "text": text, "thread_type": "group"}
    if image_url:
        payload["image_url"] = image_url
    try:
        # Image cần thêm thời gian download + upload
        timeout = 30 if image_url else 8
        r = _rq.post(url, json=payload,
                     headers={"X-Bridge-Secret": secret}, timeout=timeout)
        if r.status_code == 200 and '"ok":true' in r.text:
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:
        return False, str(exc)


def _log_broadcast(sender_uid: str, sender_name: str, target_label: str,
                   targets: List[Dict[str, str]], message: str,
                   results: List[Dict[str, Any]]):
    try:
        from db import get_conn
        import json
        ok_count = sum(1 for r in results if r.get("ok"))
        fail_count = len(results) - ok_count
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lan_broadcasts
                        (sender_uid, sender_name, target_label, target_groups,
                         message_text, results, success_count, fail_count)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                """, (
                    sender_uid, sender_name, target_label,
                    json.dumps(targets, ensure_ascii=False),
                    message,
                    json.dumps(results, ensure_ascii=False),
                    ok_count, fail_count,
                ))
                conn.commit()
    except Exception as exc:
        logger.warning("log_broadcast fail: %s", exc)


def handle_broadcast_if_any(sender_uid: str, sender_name: str,
                             body: str, image_url: str = "") -> Optional[str]:
    """Entry point: nếu body match cú pháp broadcast → xử lý và trả về reply text.
    None = không phải lệnh broadcast → để handler khác xử lý.

    image_url (optional): nếu tin có ảnh đính kèm, Lan sẽ forward kèm ảnh.
    """
    # Case 1: sếp xin LIST nhóm (gõ "gửi" hoặc "nhóm" lẻ)
    if _is_list_request(body):
        if not _is_admin_uid(sender_uid):
            return None  # NV gõ "nhóm" — bỏ qua, không spam
        return _build_group_list_text()

    parsed = _parse_command(body)
    if not parsed:
        return None

    if not _is_admin_uid(sender_uid):
        return ("🌷 Lan đây ạ! Chỉ sếp Tưởng mới có quyền nhờ Lan gửi thông báo "
                "đến nhóm. Sếp cần gì thêm cứ nói nha 🌸")

    target_str, message = parsed
    targets, unknown = _resolve_targets(target_str)

    if not targets:
        # Sếp gõ tên nhóm sai → hiện thẳng menu để chọn lại
        head = (f"🌷 Lan chưa hiểu nhóm '{', '.join(unknown)}' ạ.\n\n"
                if unknown else "🌷 Lan chưa rõ nhóm nào ạ.\n\n")
        return head + _build_group_list_text()

    # Prepend prefix "📢 [Thông báo từ sếp]"
    final_text = f"📢 Thông báo từ sếp:\n\n{message}"

    # Gửi tuần tự để dễ track
    results: List[Dict[str, Any]] = []
    for tg in targets:
        ok, err = _send_one(tg["thread_id"], final_text, image_url=image_url)
        results.append({
            "thread_id": tg["thread_id"],
            "label": tg["label"],
            "ok": ok,
            "error": err if not ok else "",
        })

    _log_broadcast(sender_uid, sender_name or "", target_str,
                   targets, message, results)

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count

    # Build reply
    lines = [f"🌷 Lan đã gửi xong ạ sếp!"]
    lines.append(f"   ✓ Thành công: {ok_count}/{len(targets)} group")
    if fail_count > 0:
        lines.append(f"   ✗ Lỗi: {fail_count}")
        for r in results:
            if not r["ok"]:
                lines.append(f"      - {r['label']}: {r['error'][:60]}")
    lines.append("")
    lines.append(f"📋 Nội dung đã gửi: '{message[:80]}{'...' if len(message) > 80 else ''}'")
    if unknown:
        lines.append(f"\n⚠️ Nhóm chưa rõ (bỏ qua): {', '.join(unknown)}")
    return "\n".join(lines)
