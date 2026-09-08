#!/usr/bin/env python3
"""
Chạy E2E module Kho hàng (safe cleanup có phạm vi, opening, xuất, hoàn, assert).

  cd /path/to/posbottieuhiem
  set -a && . deploy/pos-dashboard.env && set +a
  .venv/bin/python test_warehouse_flow.py              # nên dùng (cùng env với web)
  python3 test_warehouse_flow.py --dry-run

Nếu lỗi «No module named psycopg2»: web chạy trong .venv đã cài requirements.runtime.txt;
  script phải gọi bằng `.venv/bin/python` hoặc `pip install psycopg2-binary`.

Safe mode luôn bật: không DELETE toàn bảng; chỉ xóa dữ liệu test (SKU_TEST, e2e_wh_flow, E2E_*).
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
    parser = argparse.ArgumentParser(description="Warehouse module E2E (safe scoped cleanup).")
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

    from warehouse.e2e_flow import RERUN_CLI, RERUN_ROUTE, run_warehouse_e2e_flow

    try:
        report = run_warehouse_e2e_flow(get_conn, dry_run=args.dry_run)
        print(report)
        return 0
    except Exception as exc:  # noqa: BLE001
        print("", file=sys.stderr)
        print("==========", file=sys.stderr)
        print("RESULT: FAIL", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        print(f"Rerun CLI: {RERUN_CLI}", file=sys.stderr)
        print(f"Rerun URL: {RERUN_ROUTE}", file=sys.stderr)
        print("==========", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
