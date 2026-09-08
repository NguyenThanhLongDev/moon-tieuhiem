#!/usr/bin/env python3
"""
Sync trạng thái ĐVVC từ Pancake vào wh_outbound_requests (PostgreSQL).

Chạy qua subprocess từ scheduler.py → process exit sau khi xong → OS reclaim RAM.

Lookback mặc định: 14 ngày (đủ bắt các đơn đang active).
Lý do giảm từ 90 → 14 ngày: data cũ đã sync & lưu DB rồi, không cần fetch lại.
Đơn active (waiting/shipped) hiếm khi tồn tại quá 14 ngày.

Truyền tham số: [days] (mặc định 14)
  python sync_kho_outbound.py        → 14 ngày
  python sync_kho_outbound.py 30     → 30 ngày
"""
import os
import sys
import logging
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sync_kho_outbound")


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    date_from = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = date.today().strftime("%Y-%m-%d")

    log.info("[sync_kho_outbound] Sync %s → %s (active_only=True, %d ngày)", date_from, date_to, days)

    from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range

    # active_only=True: chỉ fetch status đang hoạt động (waiting/confirmed/shipped/received)
    # → nhanh hơn ~30%, bỏ qua đơn returned/cancelled đã ổn định
    result = sync_outbound_for_date_range(date_from, date_to, active_only=True)

    errors = result.get("errors", [])
    log.info(
        "[sync_kho_outbound] Xong: inserted=%d updated=%d skipped=%d errors=%d",
        result.get("inserted", 0),
        result.get("updated", 0),
        result.get("skipped", 0),
        len(errors),
    )
    if errors:
        for e in errors[:10]:
            log.warning("[sync_kho_outbound] ERR: %s", e)


if __name__ == "__main__":
    main()
