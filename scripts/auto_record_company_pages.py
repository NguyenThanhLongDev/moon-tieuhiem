#!/usr/bin/env python3
"""Tự ghi nhận page công ty vào company_page_whitelist.

Page đã chạy TK công ty >= 2 ngày (đã ổn định) → coi là page công ty, ghi whitelist.
Page MỚI xuất hiện (<2 ngày) CHƯA ghi → vẫn hiện ở 'Danh sách page lạ' để soi (cảnh báo
page mới/nghi ngờ). Sau 2 ngày nếu vẫn chạy thì tự ghi nhận.

Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/auto_record_company_pages.py
"""
from __future__ import annotations
import sys, logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("auto_record_company_pages")


def main() -> None:
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO company_page_whitelist (page_id, note)
            SELECT page_id, 'auto: chạy TK công ty >=2 ngày'
              FROM (
                SELECT page_id
                  FROM fb_ads_page_daily_spend
                 WHERE COALESCE(page_id,'') <> ''
                 GROUP BY page_id
                HAVING COUNT(DISTINCT metric_date) >= 2
              ) t
            ON CONFLICT (page_id) DO NOTHING
        """)
        n = cur.rowcount
        conn.commit()
    logger.info("=== auto-record page công ty: +%d page mới ===", n)


if __name__ == "__main__":
    main()
