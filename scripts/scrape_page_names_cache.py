#!/usr/bin/env python3
"""Scrape tên page THẬT (m.facebook.com) → bảng fb_page_names. KHÔNG cần token.

Lấy mọi page_id đang chạy ads (fb_ads_page_daily_spend) → scrape <title> → lưu/ghi đè
fb_page_names. Trang báo cáo (Page → TK & Camp, Chi phí QC) ưu tiên dùng tên này.

Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/scrape_page_names_cache.py
Cron:  0 6 * * *  (1 lần/sáng — page mới chạy ads tự có tên)
"""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("scrape_page_names_cache")


def main():
    from db import get_conn
    from scripts.scrape_fb_page_names import fetch_page_name

    with get_conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT page_id FROM fb_ads_page_daily_spend WHERE page_id <> ''")
            pids = [str(r[0]) for r in cur.fetchall()]
            # bỏ qua page đã có tên scrape gần đây (<7 ngày) để nhẹ
            cur.execute("SELECT page_id FROM fb_page_names WHERE scraped_at > now() - interval '7 days'")
            recent = {str(r[0]) for r in cur.fetchall()}
    todo = [p for p in pids if p not in recent]
    logger.info("=== scrape tên page: %d cần làm (%d đã có gần đây) ===", len(todo), len(recent))

    got = 0
    with get_conn() as c:
        # 3 luồng thôi: FB Page Plugin chặn nếu gọi ồ ạt (20 luồng → 1/423, 3 luồng → OK)
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(fetch_page_name, p): p for p in todo}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    nm = fut.result()
                except Exception:
                    nm = None
                if nm:
                    with c.cursor() as cur:
                        cur.execute(
                            """INSERT INTO fb_page_names (page_id, name, scraped_at)
                               VALUES (%s,%s, now())
                               ON CONFLICT (page_id) DO UPDATE SET name=EXCLUDED.name, scraped_at=now()""",
                            (p, nm),
                        )
                    got += 1
        c.commit()
    logger.info("=== DONE: lấy tên thật %d/%d page ===", got, len(todo))


if __name__ == "__main__":
    main()
