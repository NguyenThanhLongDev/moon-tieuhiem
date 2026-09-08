"""Tỷ giá USD/VND của ngân hàng ACB — bán ra.

Dùng khi sync FB Ads spend từ ad account currency=USD → convert sang VND.

Chiến lược fetch (fallback chain):
1. Scrape webgia.com/ty-gia/acb/ (parse USD row, lấy cột "Bán")
2. Fallback: open.er-api.com (USD→VND generic, không phải ACB-specific)
3. Last resort: fixed 26000 (số an toàn 2026)

Cache: Redis TTL 1 giờ — tránh scrape mỗi lần sync.

Usage:
    from fb_currency import get_acb_usd_vnd_sell
    rate = get_acb_usd_vnd_sell()  # → 26379.0
    spend_vnd = spend_usd * rate
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

try:
    import redis_cache
    _HAS_REDIS = True
except Exception:
    _HAS_REDIS = False

log = logging.getLogger(__name__)

_CACHE_KEY = "fb:acb_usd_vnd_sell"
_CACHE_TTL = 3600  # 1 giờ
_FALLBACK_RATE = 26000.0  # last-resort nếu mọi source fail
_MIN_REASONABLE = 20000.0
_MAX_REASONABLE = 35000.0


def _parse_webgia_acb() -> Optional[float]:
    """Scrape webgia.com/ty-gia/acb/ → USD sell rate."""
    try:
        r = requests.get(
            "https://webgia.com/ty-gia/acb/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; tieuhiem-sync)"},
        )
        if r.status_code != 200:
            return None
        html = r.text
        m = re.search(r"<tr[^>]*>.*?USD.*?</tr>", html, re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        nums = re.findall(r">([0-9]{1,3}[.,][0-9]{3})\s*<", m.group(0))
        if len(nums) < 3:
            return None
        # Webgia table columns: [mua_tien_mat, mua_chuyen_khoan, ban_chuyen_khoan, ban_tien_mat]
        # Lấy cột bán (index 2 hoặc 3) — pick max trong 2 cuối
        sell_candidates = []
        for s in nums[2:4]:
            try:
                v = float(s.replace(",", ".").replace(".", "")) / 1000
                # Số dạng "26.379" → 26379. Re-parse:
                clean = s.replace(",", "").replace(".", "")
                if len(clean) == 5:
                    v = float(clean)
                    if _MIN_REASONABLE <= v <= _MAX_REASONABLE:
                        sell_candidates.append(v)
            except Exception:
                pass
        if sell_candidates:
            return max(sell_candidates)
    except Exception as e:
        log.warning("[fb_currency] webgia fetch fail: %s", e)
    return None


def _parse_external_api() -> Optional[float]:
    """Fallback: open.er-api.com USD→VND (không phải ACB cụ thể, chỉ tham khảo)."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        v = float(d.get("rates", {}).get("VND") or 0)
        if _MIN_REASONABLE <= v <= _MAX_REASONABLE:
            return v
    except Exception as e:
        log.warning("[fb_currency] external api fail: %s", e)
    return None


def get_acb_usd_vnd_sell(force_refresh: bool = False) -> float:
    """Lấy tỷ giá USD/VND ACB bán ra. Cache Redis 1h.

    Trả về float (VD: 26379.0). Luôn trả số > 0 — fallback fixed nếu mọi source fail.
    """
    # Try cache
    if not force_refresh and _HAS_REDIS:
        try:
            client = redis_cache.get_redis_client()
            if client:
                cached = client.get(_CACHE_KEY)
                if cached:
                    try:
                        v = float(cached)
                        if _MIN_REASONABLE <= v <= _MAX_REASONABLE:
                            return v
                    except Exception:
                        pass
        except Exception:
            pass

    # Fetch fresh
    rate: Optional[float] = _parse_webgia_acb()
    source = "webgia(acb)"
    if rate is None:
        rate = _parse_external_api()
        source = "open.er-api"
    if rate is None:
        rate = _FALLBACK_RATE
        source = "fallback_fixed"

    log.info("[fb_currency] USD/VND sell rate: %s (source=%s)", rate, source)

    # Cache
    if _HAS_REDIS:
        try:
            client = redis_cache.get_redis_client()
            if client:
                client.setex(_CACHE_KEY, _CACHE_TTL, str(rate))
        except Exception:
            pass

    return rate


def convert_to_vnd(amount: float, currency: str) -> float:
    """Convert số tiền theo currency → VND.

    - currency='VND' (hoặc rỗng/None) → trả nguyên amount
    - currency='USD' → amount × tỷ giá ACB bán ra
    - currency khác → log warning, trả nguyên amount (caller xử lý)
    """
    if not amount:
        return 0.0
    cur = (currency or "VND").upper().strip()
    if cur in ("VND", ""):
        return float(amount)
    if cur == "USD":
        return float(amount) * get_acb_usd_vnd_sell()
    log.warning("[fb_currency] unknown currency '%s', giữ nguyên amount", cur)
    return float(amount)


if __name__ == "__main__":
    # CLI test
    logging.basicConfig(level=logging.INFO)
    print(f"ACB USD/VND sell (cached): {get_acb_usd_vnd_sell()}")
    print(f"ACB USD/VND sell (fresh):  {get_acb_usd_vnd_sell(force_refresh=True)}")
    print(f"100 USD → VND:             {convert_to_vnd(100, 'USD'):,.0f}")
    print(f"100 VND → VND:             {convert_to_vnd(100, 'VND'):,.0f}")
