#!/usr/bin/env python3
"""
Smart sync đơn hàng kho vật lý — subprocess wrapper thông minh.

Logic tự động (--mode auto):
  • Lần đầu (last_full_sync_at IS NULL cho bất kỳ shop): full 90 ngày, tất cả status.
  • Những lần tiếp theo: 14 ngày (outbound) / 21 ngày (returns), active_only=True.

--mode full  : 90 ngày, tất cả status — dùng cho nightly safety net.
--mode today : 14 ngày (outbound) / 21 ngày (returns), active_only=True — dùng cho regular 30-phút.

Subprocess exit → OS thu hồi toàn bộ RAM tức thì.

Usage:
  python sync_orders_smart.py                         # auto, outbound
  python sync_orders_smart.py --mode full             # force full 90d
  python sync_orders_smart.py --mode today            # force 14d (outbound) / 21d (returns)
  python sync_orders_smart.py --source returns        # đơn hoàn
  python sync_orders_smart.py --mode full --source returns
"""
import argparse
import gc
import logging
import os
import resource
import sys
import time
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sync_orders_smart")


def _ram_mb() -> float:
    """RSS memory usage hiện tại (MB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _apply_memory_cap(cap_mb: int = 2048) -> None:
    """Đặt RLIMIT_AS hard cap để subprocess tự crash nếu leak RAM.

    Tránh trường hợp 1 sync ngốn 17GB kéo cả VPS. Nếu lỡ leak, OOM trong
    process này sẽ raise MemoryError → scheduler log lỗi → respawn lần sau.
    KHÔNG ảnh hưởng tới web/scheduler/postgres khác (rlimit chỉ áp process này).
    """
    try:
        cap_bytes = cap_mb * 1024 * 1024
        # Soft = hard = cap_mb. Process khi alloc > cap → MemoryError.
        resource.setrlimit(resource.RLIMIT_AS, (cap_bytes, cap_bytes))
        log.info("Memory cap: RLIMIT_AS = %d MB", cap_mb)
    except Exception as exc:
        log.warning("Không đặt được RLIMIT_AS=%dMB: %s", cap_mb, exc)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart sync kho vật lý")
    p.add_argument(
        "--mode",
        choices=["auto", "today", "full"],
        default="auto",
        help="auto=tự phát hiện; today=14d outbound/21d returns active; full=90 ngày tất cả",
    )
    p.add_argument(
        "--source",
        choices=["outbound", "returns"],
        default="outbound",
        help="outbound=wh_outbound_requests; returns=wh_return_receipts",
    )
    p.add_argument(
        "--shop-ids",
        default="",
        help="Comma-separated pos_shop_id list (để batch sync; rỗng = tất cả shops active)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # Đặt cap RAM 2GB ngay từ đầu — phòng ngừa leak ở module sync.
    # Override qua env: WH_SYNC_MEM_CAP_MB=4096 nếu muốn rộng hơn (full sync 90d).
    _cap_mb = int(os.getenv("WH_SYNC_MEM_CAP_MB", "2048"))
    _apply_memory_cap(_cap_mb)

    from modules.kho_vat_ly.wh_db import wh_db as db
    from modules.kho_vat_ly.wh_sync_state import (
        SOURCE_OUTBOUND, SOURCE_RETURNS,
        FULL_LOOKBACK_DAYS, TODAY_LOOKBACK_DAYS, RETURNS_TODAY_LOOKBACK_DAYS,
        get_all_states, needs_full_sync,
        parse_error_shop_names, upsert_state_bulk,
    )

    source_name = SOURCE_OUTBOUND if args.source == "outbound" else SOURCE_RETURNS
    t_start = time.monotonic()
    ram_start = _ram_mb()

    # ── Load shops + sync_state ──────────────────────────────────────────────
    with db() as conn:
        shops = conn.execute(
            "SELECT id, shop_key, shop_name, pos_shop_id FROM wh_shops WHERE status='active'"
        ).fetchall()
        states = get_all_states(conn, source_name)

    if not shops:
        log.warning("[%s] Không có shop active — thoát", source_name)
        return

    # ── Chọn lookback days theo source ──────────────────────────────────────
    regular_lookback = (
        RETURNS_TODAY_LOOKBACK_DAYS if args.source == "returns"
        else TODAY_LOOKBACK_DAYS
    )

    # ── Quyết định mode ──────────────────────────────────────────────────────
    if args.mode == "full":
        mode = "full"
    elif args.mode == "today":
        mode = "today"
    else:
        # auto: full nếu bất kỳ shop nào chưa có full sync
        any_fresh = any(needs_full_sync(states.get(s["id"])) for s in shops)
        mode = "full" if any_fresh else "today"

    today_dt = date.today()
    if mode == "full":
        date_from = (today_dt - timedelta(days=FULL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        active_only = False
    else:
        date_from = (today_dt - timedelta(days=regular_lookback)).strftime("%Y-%m-%d")
        active_only = True
    date_to = today_dt.strftime("%Y-%m-%d")

    fresh_shops = [s for s in shops if needs_full_sync(states.get(s["id"]))]
    log.info(
        "[%s] mode=%s date=%s→%s active_only=%s shops=%d fresh=%d RAM=%.0fMB",
        source_name, mode, date_from, date_to, active_only,
        len(shops), len(fresh_shops), ram_start,
    )

    # ── Chạy sync ────────────────────────────────────────────────────────────
    result = None
    errors: list = []
    ins = upd = skip = 0

    # ── Parse --shop-ids batch filter ───────────────────────────────────────
    shop_id_set = set(s.strip() for s in (args.shop_ids or "").split(",") if s.strip())
    if shop_id_set:
        log.info("[%s] BATCH mode: filter %d pos_shop_id", source_name, len(shop_id_set))

    try:
        if args.source == "outbound":
            from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range
            result = sync_outbound_for_date_range(
                date_from, date_to,
                only_pos_shop_ids=(shop_id_set or None),
                active_only=active_only,
                wait_for_lock=True,   # dùng manual lock — không tranh SYNC_LOCK với web
            )
        else:
            from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range
            result = sync_returns_for_date_range(
                date_from, date_to,
                only_pos_shop_ids=(shop_id_set or None),
            )

        ins  = result.get("inserted", 0)
        upd  = result.get("updated",  0)
        skip = result.get("skipped",  0)
        errors = result.get("errors", [])

    except Exception as exc:
        log.error("[%s] EXCEPTION: %s", source_name, exc, exc_info=True)
        errors = [str(exc)]

    elapsed = time.monotonic() - t_start
    ram_end = _ram_mb()

    log.info(
        "[%s] DONE mode=%s ins=%d upd=%d skip=%d errs=%d %.1fs RAM%.0f→%.0fMB",
        source_name, mode, ins, upd, skip, len(errors),
        elapsed, ram_start, ram_end,
    )
    for e in errors[:10]:
        log.warning("[%s] ERR: %s", source_name, e)

    # ── Cập nhật sync_state per-shop ─────────────────────────────────────────
    error_shop_names = parse_error_shop_names(errors)
    try:
        with db() as conn:
            upsert_state_bulk(
                conn, source_name,
                list(shops), error_shop_names,
                mode, errors,
            )
        log.info("[%s] sync_state updated cho %d shops", source_name, len(shops))
    except Exception as se:
        log.warning("[%s] Không update sync_state: %s", source_name, se)

    # ── Giải phóng RAM tường minh trước khi exit ─────────────────────────────
    try:
        del result  # type: ignore[name-defined]
    except NameError:
        pass
    gc.collect()


if __name__ == "__main__":
    main()
