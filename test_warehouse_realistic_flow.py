#!/usr/bin/env python3
"""
E2E2 — kịch bản kho thực chiến (nhiều SKU, xuất thiếu, hoàn một phần, điều chỉnh mất hàng).

  cd /path/to/posbottieuhiem
  set -a && . deploy/pos-dashboard.env && set +a
  .venv/bin/python test_warehouse_realistic_flow.py
  python3 test_warehouse_realistic_flow.py --dry-run

Prefix dữ liệu: E2E2_, shop e2e2_wh_flow. Không xóa toàn bảng; chỉ cleanup có phạm vi.

Nếu lỗi «No module named psycopg2»: dùng `.venv/bin/python` hoặc `pip install psycopg2-binary`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env_file() -> None:
    p = ROOT / "deploy" / "pos-dashboard.env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Warehouse E2E2 realistic flow (scoped cleanup, multi-SKU)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ in các bước sẽ làm, không ghi database.",
    )
    args = parser.parse_args()

    _load_env_file()
    try:
        from db import get_conn
    except Exception as exc:  # noqa: BLE001
        print("Không import db.get_conn:", exc, file=sys.stderr)
        print("Đặt DATABASE_URL (postgresql://...) rồi chạy lại.", file=sys.stderr)
        return 1

    from warehouse.e2e2_realistic_flow import RERUN_CLI, run_e2e2_realistic_flow

    if args.dry_run:
        print(
            "DRY-RUN: sẽ chạy cleanup E2E2 + opening + 2 outbound + return + adjustment; "
            "assert balance & ledger. Bỏ --dry-run để ghi DB."
        )
        print(f"Rerun: {RERUN_CLI}")
        return 0

    try:
        report = run_e2e2_realistic_flow(get_conn)
        print(report)
        return 0
    except Exception as exc:  # noqa: BLE001
        print("", file=sys.stderr)
        print("==========", file=sys.stderr)
        print("RESULT: FAIL", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        print(f"Rerun CLI: {RERUN_CLI}", file=sys.stderr)
        print("==========", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
