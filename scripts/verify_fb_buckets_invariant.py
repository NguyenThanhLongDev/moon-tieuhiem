#!/usr/bin/env python3
"""Cron 30p/lần — kiểm tra bất biến FB ads bucket cho 7 ngày gần nhất.

Bất biến: SUM(allocated.spend) == SUM(combined source) cho mỗi ngày.
Nếu lệch dù 1đ → Telegram alert + log error.

Cron line (crontab -e):
    */30 * * * * cd /home/admin1/tieuhiemsoft/posbottieuhiem && \
        .venv/bin/python scripts/verify_fb_buckets_invariant.py >> logs/fb_invariant.log 2>&1
"""
from __future__ import annotations
import os
import sys
from datetime import date, timedelta

# Bootstrap path
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Load env if present
env_file = os.path.join(ROOT, "deploy", "pos-dashboard.env")
if os.path.isfile(env_file) and not os.environ.get("DATABASE_URL"):
    for line in open(env_file):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from modules.chi_phi_qc import _verify_fb_bucket_invariant
from tz_utils import now_hcm

try:
    from telegram_common import safe_send
except Exception:
    def safe_send(text, audience="default"):
        print(f"[NO_TELEGRAM] {text}")


def main():
    today = now_hcm().date()
    drifts = []
    print(f"=== FB buckets invariant check @ {today} ===")
    for offset in range(0, 7):
        d = (today - timedelta(days=offset)).isoformat()
        inv = _verify_fb_bucket_invariant(d, d)
        status = "OK" if inv["ok"] else "DRIFT"
        print(f"  {d}: truth={inv['truth']:>14,.0f}  buckets={inv['buckets']:>14,.0f}  diff={inv['diff']:>10,.0f}  [{status}]")
        if not inv["ok"]:
            drifts.append((d, inv))

    if drifts:
        lines = [
            "🚨 *FB ADS BUCKETS DRIFT*",
            "Số liệu báo cáo chi phí QC đang LỆCH — KHÔNG chốt sổ kế toán.",
            "",
        ]
        for d, inv in drifts:
            lines.append(
                f"📅 {d}: chênh `{inv['diff']:,.0f}đ` "
                f"(truth `{inv['truth']:,.0f}` vs buckets `{inv['buckets']:,.0f}`)"
            )
        lines += ["", "🔧 IT check `_FB_ALLOCATED_CTE` + page/uaa binding overlap."]
        safe_send("\n".join(lines), audience="default")
        print(f"\n→ Sent {len(drifts)} drift alerts to Telegram")
        sys.exit(2)

    print("\n→ All 7 days OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
