"""
Helper chung cho các báo cáo Telegram (Phase 1 chuẩn hoá 2026-05-14).

Tất cả script báo cáo MỚI phải dùng các helper ở đây để đồng nhất format,
tránh duplicate code, và sẵn sàng cho phase 3 (phân nhóm recipient theo team).

Quy ước:
- Mọi số tiền VND format qua `fmt_money(n)` → "1.234.567đ".
- Mọi báo cáo bắt đầu bằng `fmt_header(title, period)` để header đồng nhất.
- Gửi qua `safe_send(text, audience=...)` thay vì gọi `broadcast_send_text` trực tiếp.
- Lưu state incremental qua `redis_state(key)` thay vì JSON file.
- Timezone: dùng `now_hcm()`, `today_hcm()` từ `tz_utils` — KHÔNG `datetime.now()` trần.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Format helpers
# ─────────────────────────────────────────────────────────────────────────────

def fmt_money(n: Any) -> str:
    """Format số tiền VND theo chuẩn dấu chấm: 1.234.567đ. None/lỗi → '0đ'."""
    try:
        v = int(round(float(n or 0)))
    except (TypeError, ValueError):
        return "0đ"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}{v:,}đ".replace(",", ".")


def fmt_int(n: Any) -> str:
    """Format số nguyên với dấu chấm: 1.234. None/lỗi → '0'."""
    try:
        v = int(round(float(n or 0)))
    except (TypeError, ValueError):
        return "0"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}{v:,}".replace(",", ".")


def fmt_pct(part: Any, total: Any) -> str:
    """Tỷ lệ phần trăm: fmt_pct(8000000, 12345678) → '65%'."""
    try:
        p = float(part or 0)
        t = float(total or 0)
        if t == 0:
            return "0%"
        return f"{round(p * 100 / t):d}%"
    except (TypeError, ValueError):
        return "0%"


def fmt_date_vn(d: Any) -> str:
    """date/datetime → '14/05/2026'. None → ''."""
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.strftime("%d/%m/%Y")
    return str(d)


def fmt_header(title: str, period: Optional[str] = None) -> str:
    """Header chuẩn cho mọi báo cáo Telegram.

    Output:
        📊 <TÊN>
        ⏰ 14/05/2026 09:30
        📅 Kỳ: <period>          ← chỉ hiện khi period != None
        ────────────────────────
    """
    from tz_utils import now_hcm
    now = now_hcm()
    if isinstance(now, datetime):
        stamp = now.strftime("%d/%m/%Y %H:%M")
    else:
        stamp = str(now)
    lines = [f"📊 {title.upper()}", f"⏰ {stamp}"]
    if period:
        lines.append(f"📅 Kỳ: {period}")
    lines.append("─" * 28)
    return "\n".join(lines)


def fmt_footer(tip: Optional[str] = None) -> str:
    """Footer: dòng ngăn cách + (optional) tip."""
    parts = ["─" * 28]
    if tip:
        parts.append(f"💡 {tip}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Send wrapper — hỗ trợ `audience` cho phase 3 (phân team) sau này
# ─────────────────────────────────────────────────────────────────────────────

# Map audience → chat_ids. Hiện tại tất cả trỏ về broadcast mặc định.
# Phase 3 sẽ mở rộng đọc từ DB app_config (key: telegram_audience_<name>).
_AUDIENCE_REGISTRY: dict[str, list[str]] = {}


def safe_send(text: str, *, audience: str = "default", split: bool = True) -> Tuple[int, int]:
    """Gửi text Telegram an toàn — log ok/fail, không raise.

    audience:
        'default' → broadcast như cũ (chat_id từ config.json + subscribers).
        Tên khác → tra `_AUDIENCE_REGISTRY` (phase 3); nếu chưa có thì fallback default.

    Returns: (ok_count, fail_count). 0/0 nếu input rỗng hoặc lỗi config.
    """
    if not (text or "").strip():
        return 0, 0
    try:
        from telegram_notify import broadcast_send_text
        # TODO phase 3: nếu audience != 'default' và có trong _AUDIENCE_REGISTRY,
        # gọi sendMessage trực tiếp tới list chat_id riêng thay vì broadcast.
        if audience != "default" and audience in _AUDIENCE_REGISTRY:
            log.info("[telegram] audience=%s → routed registry", audience)
        ok, fail = broadcast_send_text(text)
        log.info("[telegram] sent audience=%s ok=%d fail=%d", audience, ok, fail)
        if fail and ok == 0:
            print(f"[telegram] FAIL all (audience={audience}): {fail} attempts", file=sys.stderr)
        return ok, fail
    except Exception as exc:
        log.exception("[telegram] safe_send error: %s", exc)
        print(f"[telegram] safe_send exception: {exc}", file=sys.stderr)
        return 0, 1


def register_audience(name: str, chat_ids: list[str]) -> None:
    """Đăng ký audience custom (phase 3). Hiện chưa dùng."""
    _AUDIENCE_REGISTRY[name] = list(chat_ids)


# ─────────────────────────────────────────────────────────────────────────────
# Redis state — thay file JSON cache (race-safe + multi-worker)
# ─────────────────────────────────────────────────────────────────────────────

def redis_state(key: str, value: Any = None, ttl: int = 86400) -> Any:
    """Get/Set state qua Redis.

    - `redis_state('foo')` → get value (None nếu chưa có).
    - `redis_state('foo', {'x': 1})` → set value (TTL mặc định 1 ngày).
    - `redis_state('foo', value, ttl=3600)` → set với TTL custom.

    Namespace: tự prefix 'tg:' để tránh đụng key khác.
    """
    full_key = f"tg:{key}"
    try:
        from redis_cache import cache_get, cache_set
        if value is None:
            return cache_get(full_key)
        cache_set(full_key, value, ttl=ttl)
        return value
    except Exception as exc:
        log.warning("[telegram] redis_state(%s) error: %s", key, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def section(title: str, body: str) -> str:
    """Một section trong body: '▶ TITLE\\n<body>'."""
    return f"▶ {title}\n{body}"
