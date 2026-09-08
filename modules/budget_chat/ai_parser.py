"""AI parser cho tin nhắn báo ngân sách FB Ads.

Primary: DeepSeek V4 flash (OpenAI-compatible API).
Fallback: regex parser (đơn giản) khi AI lỗi/timeout.

Output dạng dict:
    {
        "for_date": "YYYY-MM-DD" | None,
        "items": [
            {"tk_name": str, "card_last4": str|None, "amount_vnd": int},
            ...
        ],
        "model": str,
        "error": str | None,
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT = 30  # giây

# System prompt — sẽ được prompt cache (DeepSeek hỗ trợ auto cache)
_SYSTEM_PROMPT_TEMPLATE = """Bạn parse tin nhắn báo ngân sách FB Ads của NV chạy quảng cáo.

Quy ước số tiền:
  300k = 300000
  1tr  = 1000000
  4tr  = 4000000
  4tr5 = 4500000
  1tr2 = 1200000
  500K = 500000

Quy ước for_date (NGÀY SẼ CHẠY):
  "ngày mai" / "mai" → {tomorrow_iso}
  "hôm nay" / "today" → {today_iso}
  "DD/M" hoặc "DD/MM" → parse trực tiếp năm {current_year}

Output JSON CHỈ (không markdown, không giải thích):
{{
  "for_date": "YYYY-MM-DD" | null,
  "items": [
    {{
      "tk_name": "tên TK NV viết, vd Dang25.3 / TuyetTH22.1",
      "card_last4": "4 số cuối thẻ hoặc null",
      "amount_vnd": số nguyên đồng VND
    }}
  ]
}}

Nếu không phải tin báo ngân sách (chỉ chat thông thường) → trả {{"for_date":null,"items":[]}}.
"""

# Regex fallback patterns
_RE_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(tr|tr|tỉ|tỷ|k|K|đ|d|vnd)?", re.IGNORECASE)
_RE_TK_LINE = re.compile(
    r"[-•*]?\s*(?:tk|tài\s*khoản)\s*:?\s*([\w.\-]+)"
    r"(?:.*?th[ẻe]\s*(\d{4,}))?"
    r".*?(?:chạy|c[hị]u)?\s*([\d.,]+\s*(?:tr|k|K|đ)?)",
    re.IGNORECASE,
)
_RE_DATE_DDMM = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")


def _now_hcm_date() -> date:
    try:
        from tz_utils import now_hcm
        return now_hcm().date()
    except Exception:
        return datetime.utcnow().date() + timedelta(hours=7)  # rough UTC+7


def _amount_to_vnd(raw: str) -> int:
    """Convert '300k'/'1tr'/'4tr5'/'1,500,000' → int VND."""
    s = raw.strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return 0
    m = re.match(r"(\d+(?:\.\d+)?)(tr|tỉ|tỷ|k|đ|d|vnd)?", s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("tr",):
        # 4tr5 không bắt được ở regex này — xử lý riêng dưới
        return int(num * 1_000_000)
    if unit in ("tỉ", "tỷ"):
        return int(num * 1_000_000_000)
    if unit in ("k",):
        return int(num * 1_000)
    # Số nguyên đồng
    return int(num)


def _regex_fallback_parse(body: str) -> dict:
    """Parser cuối cùng nếu AI fail. Đơn giản hoá, có thể thiếu sót."""
    items = []
    for_date = None
    today = _now_hcm_date()

    # Date: "ngày mai" / "DD/M"
    low = body.lower()
    if "ngày mai" in low or " mai " in low:
        for_date = (today + timedelta(days=1)).isoformat()
    if for_date is None:
        m = _RE_DATE_DDMM.search(body)
        if m:
            try:
                dd, mm = int(m.group(1)), int(m.group(2))
                yy = int(m.group(3)) if m.group(3) else today.year
                if yy < 100:
                    yy += 2000
                for_date = date(yy, mm, dd).isoformat()
            except Exception:
                pass

    # Lines kiểu "- tk XXX [thẻ NNNN] chạy NNNk"
    for ln in body.splitlines():
        # Pre-handle "4tr5" → "4500000"
        ln_norm = re.sub(r"(\d+)\s*tr\s*(\d+)\b", lambda m: str(int(m.group(1)) * 1_000_000 + int(m.group(2)) * 100_000), ln, flags=re.IGNORECASE)
        m = _RE_TK_LINE.search(ln_norm)
        if not m:
            continue
        tk = m.group(1).strip()
        card = (m.group(2) or "").strip() or None
        amt = _amount_to_vnd(m.group(3))
        if amt > 0:
            items.append({"tk_name": tk, "card_last4": card, "amount_vnd": amt})

    return {"for_date": for_date, "items": items, "model": "regex-fallback", "error": None}


def parse_budget_message(body: str) -> dict:
    """Parse 1 tin nhắn báo ngân sách. Trả dict (xem docstring module)."""
    body = (body or "").strip()
    if not body:
        return {"for_date": None, "items": [], "model": "empty", "error": None}

    try:
        from ai_keys import get_ai_key
        api_key = get_ai_key("deepseek") or ""
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.warning("DeepSeek API key chưa cấu hình (Settings → AI Models) — dùng regex fallback")
        return _regex_fallback_parse(body)

    today = _now_hcm_date()
    sys_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        today_iso=today.isoformat(),
        tomorrow_iso=(today + timedelta(days=1)).isoformat(),
        current_year=today.year,
    )
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": body},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=DEEPSEEK_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("DeepSeek HTTP %s: %s", r.status_code, r.text[:200])
            return _regex_fallback_parse(body) | {"error": f"HTTP {r.status_code}"}
        data = r.json()
        raw = data["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
        items = parsed.get("items") or []
        # Sanitize items
        clean = []
        for it in items:
            try:
                amt = int(it.get("amount_vnd") or 0)
                if amt <= 0:
                    continue
                tk = str(it.get("tk_name") or "").strip()
                if not tk:
                    continue
                card = it.get("card_last4")
                card = str(card).strip() if card else None
                clean.append({"tk_name": tk, "card_last4": card or None, "amount_vnd": amt})
            except Exception:
                continue
        # Coerce năm: nếu AI trả for_date năm cũ mà DD-MM không vượt today → đẩy về năm hiện tại
        fd = parsed.get("for_date") or None
        if fd:
            try:
                d = date.fromisoformat(fd)
                if d.year < today.year:
                    cand = d.replace(year=today.year)
                    if cand <= today:
                        fd = cand.isoformat()
            except Exception:
                pass
        return {
            "for_date": fd,
            "items": clean,
            "model": DEEPSEEK_MODEL,
            "error": None,
        }
    except requests.Timeout:
        logger.warning("DeepSeek timeout — fallback regex")
        return _regex_fallback_parse(body) | {"error": "timeout"}
    except Exception as exc:
        logger.warning("DeepSeek error: %s — fallback regex", exc)
        return _regex_fallback_parse(body) | {"error": str(exc)}
