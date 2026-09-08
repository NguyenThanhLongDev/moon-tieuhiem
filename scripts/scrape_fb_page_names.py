#!/usr/bin/env python3
"""Lấy tên FB page bằng cách scrape <title> của m.facebook.com/{page_id}.

KHÔNG cần token, KHÔNG cần permission. Dùng được cho mọi page public.
- Endpoint: GET https://m.facebook.com/{page_id}
- Parse <title>...</title> → đó là tên page (có thể kèm " | <location>")

Lưu vào:
  fb_ads_page_daily_spend.page_name
  fb_pages.page_name

Chạy: python scripts/scrape_fb_page_names.py
"""
from __future__ import annotations
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_conn  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
HEADERS = {"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8"}
TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
TIMEOUT = 12
MAX_WORKERS = 20


def find_missing() -> list:
    sql = """
        SELECT DISTINCT page_id
          FROM fb_ads_page_daily_spend
         WHERE (page_name IS NULL OR page_name = '' OR page_name LIKE 'Page %')
           AND page_id IS NOT NULL AND page_id <> ''
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [str(r[0]) for r in cur.fetchall() if r[0]]


_BAD_EXACT = {"facebook", "facebook | facebook", "like", "thích"}
_BAD_CONTAINS = ("đăng nhập", "log in", "log into", "sign up", "đăng ký",
                 "content isn't available", "not available", "page not found",
                 "nội dung này hiện không có")
_PLUGIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _is_bad_name(nm: str) -> bool:
    low = (nm or "").lower().strip()
    return (not low) or low in _BAD_EXACT or any(b in low for b in _BAD_CONTAINS)


def fetch_page_name_plugin(page_id: str) -> Optional[str]:
    """Lấy tên page qua FB Page Plugin (facebook.com/plugins/page.php) — CÔNG KHAI,
    không cần login/token (giống ảnh avatar). Đây là nguồn tin cậy nhất hiện tại."""
    import html
    try:
        url = ("https://www.facebook.com/plugins/page.php?href="
               f"https%3A%2F%2Fwww.facebook.com%2F{page_id}"
               "&tabs&width=340&height=130&adapt_container_width=true")
        r = requests.get(url, headers={"User-Agent": _PLUGIN_UA,
                                       "Accept-Language": "vi-VN,vi;q=0.9"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        _generic = {"facebook", "xem thêm", "like", "thích", "follow",
                    "theo dõi", "see more", "learn more", "tìm hiểu thêm"}
        # Tên page = text của <a href="...facebook.com...">NAME</a> (FB Page Plugin
        # render tên vào link trỏ về trang). aria-label cũng thử (fallback).
        for m in re.finditer(r'<a\b[^>]*href="[^"]*facebook\.com[^"]*"[^>]*>([^<]{2,60})</a>',
                             r.text):
            nm = html.unescape(m.group(1)).strip()
            if nm.lower() not in _generic and not _is_bad_name(nm):
                return nm
        m = re.search(r'aria-label="([^"]+)"', r.text)
        if m:
            nm = html.unescape(m.group(1)).strip()
            if not _is_bad_name(nm):
                return nm
        return None
    except Exception:
        return None


def fetch_page_name(page_id: str) -> Optional[str]:
    """Tên page: ưu tiên Page Plugin (công khai) → fallback m.facebook.com <title>."""
    nm = fetch_page_name_plugin(page_id)
    if nm:
        return nm
    try:
        url = f"https://m.facebook.com/{page_id}"
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None
        m = TITLE_RX.search(r.text)
        if not m:
            return None
        title = m.group(1).strip()
        # Decode HTML entities
        try:
            import html
            title = html.unescape(title)
        except Exception:
            pass
        # Bỏ qua title RÁC khi FB đá về trang login / generic (page bị giới hạn,
        # cần đăng nhập). KHÔNG lưu, để COALESCE fallback tên khác.
        _low = title.lower().strip()
        _bad_exact = {"facebook", "facebook | facebook"}
        _bad_contains = ("đăng nhập", "log in", "log into", "sign up",
                         "đăng ký", "content isn't available", "not available",
                         "page not found", "nội dung này hiện không có")
        if _low in _bad_exact or any(b in _low for b in _bad_contains):
            return None
        # Bỏ phần " | Da Nang" / " | Ho Chi Minh City" (location FB tự thêm)
        # Giữ nguyên tên gốc, chỉ cắt phần location
        title = re.sub(r"\s*\|\s*(Da Nang|Ho Chi Minh City|Hanoi|Vietnam|.+? City|.+? Province)\s*$",
                       "", title)
        return title.strip() or None
    except Exception:
        return None


def upsert(page_id: str, name: str) -> bool:
    if not name:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE fb_ads_page_daily_spend
                       SET page_name = %s
                     WHERE page_id = %s
                       AND (page_name IS NULL OR page_name = '' OR page_name LIKE 'Page %%')
                """, (name, page_id))
                cur.execute("""
                    INSERT INTO fb_pages (page_id, page_name)
                    VALUES (%s, %s)
                    ON CONFLICT (page_id) DO UPDATE
                      SET page_name = EXCLUDED.page_name
                      WHERE fb_pages.page_name IS NULL OR fb_pages.page_name = ''
                         OR fb_pages.page_name LIKE 'Page %%'
                """, (page_id, name))
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("DB error %s: %s", page_id, exc)
        return False


def main():
    t0 = time.time()
    missing = find_missing()
    logger.info("Cần scrape %d page.", len(missing))
    if not missing:
        return

    resolved = 0
    failed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_page_name, pid): pid for pid in missing}
        for i, fut in enumerate(as_completed(futures), 1):
            pid = futures[fut]
            name = fut.result()
            if name:
                if upsert(pid, name):
                    resolved += 1
                    if resolved % 20 == 0:
                        logger.info("  [%d/%d] %s → %s", i, len(missing), pid, name)
            else:
                failed.append(pid)

    logger.info("=== DONE %.1fs ===", time.time() - t0)
    logger.info("  Resolved: %d/%d (%.0f%%)",
                resolved, len(missing), 100 * resolved / len(missing) if missing else 0)
    logger.info("  Failed:   %d", len(failed))
    if failed:
        logger.warning("  Sample failed (page bị xoá / private): %s", failed[:10])


if __name__ == "__main__":
    main()
