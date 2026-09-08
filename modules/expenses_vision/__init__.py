"""Vision: NV gửi ảnh chi phí vào Zalo group → Gemini parse → Lan ghi nhận.

MVP — chỉ extract + reply trong group để verify accuracy.
Phase 2 sẽ lưu DB + UI báo cáo cho kế toán.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Optional

import requests
from flask import jsonify

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-2.0-pro-exp"

_PROMPT = """Bạn là AI phân tích ảnh chi phí công ty (FB Ads, SMS ngân hàng, hoá đơn, screenshot...).

Trích thông tin từ ảnh và trả về CHỈ JSON (không kèm markdown/giải thích):
{
  "amount_vnd": <số nguyên VND, vd 7000000>,
  "currency": "VND" | "USD" | <khác>,
  "note": "<mô tả ngắn 1 dòng, vd 'Chi phí FB Ads TK Long15.3 ngày 18/05'>",
  "category": "ads" | "salary" | "office" | "utility" | "other",
  "date_iso": "YYYY-MM-DD" | null,
  "confidence": 0.0-1.0
}

Quy ước:
- Tiền tệ VND mặc định. Nếu thấy ký hiệu USD/$ → convert sang VND theo tỷ giá ~25,000.
- 'amount_vnd' phải là số nguyên đồng. Nếu ảnh không rõ → 0 + confidence thấp.
- 'note' tóm tắt context (ai trả? cho gì? TK nào?). Nếu không đoán được → "Chi phí từ ảnh".
- 'category':
  - ads = FB Ads / Google Ads / quảng cáo
  - salary = lương, thưởng, chuyển khoản NV
  - office = văn phòng phẩm, máy móc, internet văn phòng
  - utility = điện nước, điện thoại
  - other = còn lại
- 'date_iso' từ ngày trên ảnh nếu thấy; không thấy → null.
- 'confidence' = mức độ chắc chắn về số tiền + category."""


def _fmt_money(n: int) -> str:
    return f"{int(n):,}".replace(",", ".") + "đ"


def _call_gemini(image_bytes: bytes, mime: str, gemini_key: str, model: str) -> dict:
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"{_GEMINI_BASE}/{model}:generateContent?key={gemini_key}"
    body = {
        "contents": [{
            "parts": [
                {"text": _PROMPT},
                {"inline_data": {"mime_type": mime, "data": img_b64}},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }
    r = requests.post(url, json=body, timeout=30)
    if r.status_code != 200:
        logger.warning("gemini HTTP %s: %s", r.status_code, r.text[:300])
        raise RuntimeError(f"Gemini HTTP {r.status_code}")
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as exc:
        logger.warning("gemini parse error: %s · raw=%s", exc, json.dumps(data)[:300])
        raise


def _push_to_zalo(thread_id: str, text: str, mention_uid: Optional[str] = None,
                  mention_name: Optional[str] = None):
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    if not secret:
        return
    payload = {"thread_id": thread_id, "text": text}
    if mention_uid and mention_name:
        tag = f"@{mention_name}"
        payload["text"] = tag + " " + text
        utf16 = sum(2 if ord(c) > 0xFFFF else 1 for c in tag)
        payload["mentions"] = [{"pos": 0, "uid": mention_uid, "len": utf16}]
    try:
        url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5051/send")
        requests.post(url, json=payload, headers={"X-Bridge-Secret": secret}, timeout=5)
    except Exception as exc:
        logger.info("push_to_zalo skip: %s", exc)


def handle_zalo_image(thread_id: str, sender_uid: str, sender_name: str,
                      image_url: str, caption: str = ""):
    """Entry từ web_app.py:zalo_bridge_inbound khi kind=image."""
    from ai_keys import get_ai_key, get_gemini_model

    gemini_key = get_ai_key("gemini")
    if not gemini_key:
        _push_to_zalo(thread_id,
                      "🥺 Lan chưa được cấp API key Gemini để đọc ảnh ạ. IT vào Settings → AI Models để cấu hình.",
                      sender_uid, sender_name)
        return jsonify({"ok": False, "error": "no gemini key"}), 200

    model = get_gemini_model()

    # Download ảnh
    try:
        r = requests.get(image_url, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"download HTTP {r.status_code}")
        img = r.content
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
    except Exception as exc:
        logger.warning("download image fail: %s", exc)
        _push_to_zalo(thread_id, f"😅 Lan tải ảnh không được ({exc}). Bạn gửi lại giúp Lan với nhé.",
                      sender_uid, sender_name)
        return jsonify({"ok": False, "error": str(exc)}), 200

    # Gọi Gemini
    try:
        parsed = _call_gemini(img, mime, gemini_key, model)
    except Exception as exc:
        _push_to_zalo(thread_id, f"😢 Lan đọc ảnh chưa được ({exc}). Bạn thử chụp rõ hơn hoặc gõ tay giúp Lan.",
                      sender_uid, sender_name)
        return jsonify({"ok": False, "error": str(exc)}), 200

    amount = int(parsed.get("amount_vnd") or 0)
    note = (parsed.get("note") or "").strip()
    category = (parsed.get("category") or "other").strip()
    date_iso = parsed.get("date_iso") or ""
    conf = float(parsed.get("confidence") or 0)

    cat_label = {
        "ads": "🎯 Quảng cáo", "salary": "💰 Lương",
        "office": "🏢 Văn phòng", "utility": "💡 Tiện ích",
        "other": "📦 Khác",
    }.get(category, "📦 Khác")

    if amount <= 0:
        _push_to_zalo(thread_id,
                      "🤔 Lan đọc ảnh mà chưa thấy số tiền rõ ràng. Bạn chụp lại rõ hơn hoặc gõ giúp Lan: "
                      "<số tiền> + <mô tả>.", sender_uid, sender_name)
        return jsonify({"ok": True, "parsed": parsed, "saved": False})

    reply = (
        f"🧾 Lan đọc được:\n"
        f"  • Tiền: {_fmt_money(amount)}\n"
        f"  • Loại: {cat_label}\n"
        f"  • Ghi chú: {note or '(không rõ)'}\n"
    )
    if date_iso:
        reply += f"  • Ngày: {date_iso}\n"
    if conf < 0.7:
        reply += f"\n⚠ Độ tin cậy thấp ({conf:.0%}) — nhờ anh chị check lại giúp."
    else:
        reply += "\n✅ Bạn xác nhận đúng thì reply 'đúng', sai thì gõ lại số đúng giúp Lan."

    _push_to_zalo(thread_id, reply, sender_uid, sender_name)

    return jsonify({"ok": True, "parsed": parsed, "saved": False, "source": "zalo_image"})
