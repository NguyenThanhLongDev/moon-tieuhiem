#!/usr/bin/env python3
"""
Wrapper subprocess: sync đơn hoàn từ Pancake → wh_return_receipts.

Chạy qua subprocess từ scheduler.py → process exit → OS reclaim RAM.

Lookback mặc định: 21 ngày (đơn hoàn xử lý lâu hơn đơn xuất).
Lý do giảm từ 90 → 21 ngày: data cũ đã sync & lưu DB rồi.

Truyền tham số: [days]  (mặc định 21)
"""
import os, sys, logging
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync_kho_returns")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    date_from = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = date.today().strftime("%Y-%m-%d")

    log.info("[sync_kho_returns] Sync %s → %s (%d ngày)", date_from, date_to, days)

    from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range
    result = sync_returns_for_date_range(date_from, date_to)

    errors = result.get("errors", [])
    log.info(
        "[sync_kho_returns] Xong: inserted=%d updated=%d skipped=%d errors=%d",
        result.get("inserted", 0), result.get("updated", 0),
        result.get("skipped",  0), len(errors),
    )
    for e in errors[:5]:
        log.warning("[sync_kho_returns] ERR: %s", e)


if __name__ == "__main__":
    main()
