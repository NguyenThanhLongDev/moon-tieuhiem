#!/usr/bin/env python3
"""Sweep stale dup_pending_* state khỏi app_config.

Bối cảnh: `expense_chat._save_dup_pending` ghi state hỏi NV "1 hay 2 khoản"
vào `app_config` key `dup_pending_<uid>` (TTL logic là 300s — 5 phút). Khi
NV không trả lời thì row config nằm lại vì code chỉ xoá khi reload đụng
TTL hoặc khi user reply. Lâu ngày → app_config phình.

Script này quét toàn bộ key `dup_pending_*`, parse JSON, check field `ts`
(Unix seconds). Nếu khoản đó cũ hơn ngưỡng (default 3600s = 1 giờ) → DELETE.

Chạy:
  cd ~/tieuhiemsoft/posbottieuhiem
  set -a && source deploy/pos-dashboard.env && set +a
  .venv/bin/python3 scripts/sweep_dup_pending.py
  .venv/bin/python3 scripts/sweep_dup_pending.py --max-age 1800
  .venv/bin/python3 scripts/sweep_dup_pending.py --dry-run

Output: số key tìm thấy / số key xoá, dùng cho cron.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sweep_dup_pending")


def sweep(max_age_sec: int = 3600, dry_run: bool = False) -> Tuple[int, int]:
    """Quét và xoá dup_pending_* stale.

    Returns (total_scanned, deleted_count).
    """
    from db import get_conn

    now = int(time.time())
    cutoff = now - max_age_sec
    scanned = 0
    stale_keys: List[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM app_config WHERE key LIKE %s",
                ("dup_pending_%",),
            )
            rows = cur.fetchall() or []
            for key, value in rows:
                scanned += 1
                ts = 0
                try:
                    data = json.loads(value) if isinstance(value, str) else (value or {})
                    ts = int(data.get("ts") or 0)
                except Exception as exc:
                    log.warning("parse %s lỗi (%s) — coi như stale", key, exc)
                    ts = 0
                if ts <= 0 or ts < cutoff:
                    stale_keys.append(key)

            log.info(
                "scanned=%d stale=%d max_age=%ds cutoff_ts=%d dry_run=%s",
                scanned, len(stale_keys), max_age_sec, cutoff, dry_run,
            )

            if stale_keys and not dry_run:
                # Xoá batch — psycopg2 chấp nhận list qua ANY(%s)
                cur.execute(
                    "DELETE FROM app_config WHERE key = ANY(%s)",
                    (stale_keys,),
                )
                conn.commit()
                log.info("xoá %d key stale.", len(stale_keys))

    return scanned, len(stale_keys)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep dup_pending_* stale ở app_config")
    ap.add_argument("--max-age", type=int, default=3600,
                    help="Số giây ngưỡng (default 3600 = 1 giờ)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Chỉ liệt kê, không xoá")
    args = ap.parse_args()

    try:
        scanned, deleted = sweep(max_age_sec=args.max_age, dry_run=args.dry_run)
    except Exception as exc:
        log.error("sweep error: %s", exc, exc_info=True)
        return 1

    if args.dry_run:
        log.info("DRY-RUN — không xoá.")
    log.info("done. scanned=%d deleted=%d", scanned, deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
