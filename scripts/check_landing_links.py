#!/usr/bin/env python3
"""Check link landing SỐNG hay CHẾT → cập nhật fb_ad_landing_links.

Mỗi link distinct: GET (follow redirect, UA trình duyệt, timeout 15s).
- 2xx/3xx           → alive = TRUE
- DNS fail / lỗi kết nối / 4xx/5xx → alive = FALSE
Ghi: link_alive, link_status (mã HTTP, 0 = không kết nối được), link_checked_at.

Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/check_landing_links.py
"""
import concurrent.futures as cf
import logging
import ssl
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("check_landing_links")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False           # ladipage hay lỗi cert lặt vặt — vẫn coi là sống
_CTX.verify_mode = ssl.CERT_NONE


def ensure_columns() -> None:
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE fb_ad_landing_links
                ADD COLUMN IF NOT EXISTS link_alive      BOOLEAN,
                ADD COLUMN IF NOT EXISTS link_status     INT,
                ADD COLUMN IF NOT EXISTS link_checked_at TIMESTAMPTZ
        """)
        conn.commit()


def check_one(link: str, retries: int = 2) -> tuple:
    """(link, alive, http_status). status 0 = DNS/kết nối fail.
    RETRY khi lỗi kết nối (status 0) — tránh false-dead do mạng chớp/nghẽn burst."""
    for attempt in range(retries + 1):
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15, context=_CTX) as resp:
                return link, 200 <= resp.status < 400, resp.status
        except urllib.error.HTTPError as e:
            # 403/405/429 nhiều site chặn bot nhưng trang vẫn sống → coi là sống
            return link, e.code in (403, 405, 429), e.code
        except Exception:
            if attempt < retries:
                time.sleep(1.2)   # nghỉ rồi thử lại
                continue
    return link, False, 0


def main() -> None:
    from db import get_conn
    ensure_columns()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT link FROM fb_ad_landing_links")
            links = [r[0] for r in cur.fetchall() if r[0]]
        logger.info("=== check %d link landing ===", len(links))
        results = []
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for link, ok, status in ex.map(check_one, links):
                results.append((link, ok, status))
        alive = sum(1 for _, ok, _ in results if ok)
        dead = len(results) - alive
        # GUARD (04/08): cả mẻ ~0 sống trên nhiều link = MẠNG SERVER nghẽn/chặn burst,
        # KHÔNG phải mọi site chết → BỎ QUA cập nhật, giữ nguyên data cũ (tránh wipe hết thành CHẾT).
        if alive == 0 and len(results) >= 5:
            logger.warning("⚠ 0/%d link sống — nghi mạng server lỗi, BỎ QUA cập nhật (giữ data cũ).", len(results))
            return
        for link, ok, status in results:
            with conn.cursor() as cur:
                cur.execute("""UPDATE fb_ad_landing_links
                                  SET link_alive=%s, link_status=%s, link_checked_at=NOW()
                                WHERE link=%s""", (ok, status, link))
            conn.commit()
            if not ok:
                logger.info("  CHẾT (%s): %s", status or "no-conn", link[:80])
        logger.info("=== DONE: %d sống / %d chết ===", alive, dead)


if __name__ == "__main__":
    main()
