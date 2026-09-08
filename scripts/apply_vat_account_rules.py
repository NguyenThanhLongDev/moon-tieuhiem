#!/usr/bin/env python3
"""Tự động áp VAT theo TK QC kể từ 1 mốc ngày (page-level), KHÔNG đụng lịch sử trước mốc.

Bối cảnh: bảng `fb_ad_account_vat_options` chỉ có 1 mức/TK (không chia ngày), còn
`fb_page_vat_rate` thì theo (page, NGÀY) và dùng chung mọi TK. Khi 1 TK đổi chủ giữa
kỳ (vd tung3.6 → tuyet 20/6) mà NV mới có mức VAT khác, KHÔNG thể đặt TK-default vì sẽ
đổi cả lịch sử của chủ cũ. Script này tick mức mới cho TỪNG (page, ngày ≥ from_date)
mà TK thực sự có spend → chủ cũ (ngày < from_date) giữ nguyên.

An toàn:
- CHỈ ghi tick cho ngày >= from_date (không bao giờ chạm ngày cũ).
- Idempotent: ON CONFLICT chỉ update tick do CHÍNH script này đặt (updated_by=MARK);
  tick do người chỉnh tay (updated_by khác) được GIỮ NGUYÊN (người luôn thắng).

Chạy: .venv/bin/python scripts/apply_vat_account_rules.py
Cron khuyến nghị: vài giờ/lần (sau khi sync page-spend). Tick là upsert nhẹ.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402

MARK = "system-vat-rule"

# (fb_ad_account_id, vat_rate, from_date)  — thêm dòng mới nếu có TK khác cần
RULES = [
    # TK 1244449290606561: tuyet20.6 nhận 20/6 với VAT 6.1%; lịch sử tung3.6 (≤19/6) giữ 11.3%
    ("1244449290606561", 0.061, "2026-06-20"),
]


def apply_rule(cur, ad_account_id: str, rate: float, from_date: str) -> int:
    cur.execute(
        """
        INSERT INTO fb_page_vat_rate (page_id, date, vat_rate, updated_by, updated_at)
        SELECT DISTINCT s.page_id, s.metric_date, %s, %s, NOW()
          FROM fb_ads_page_daily_spend s
         WHERE s.fb_ad_account_id = %s
           AND s.metric_date >= %s::date
        ON CONFLICT (page_id, date) DO UPDATE
           SET vat_rate = EXCLUDED.vat_rate, updated_at = NOW()
         WHERE fb_page_vat_rate.updated_by = %s
        """,
        (rate, MARK, ad_account_id, from_date, MARK),
    )
    return cur.rowcount


def main() -> None:
    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ad_account_id, rate, from_date in RULES:
                n = apply_rule(cur, ad_account_id, rate, from_date)
                total += n
                print(f"[vat-rule] TK {ad_account_id} → {rate*100:.1f}% từ {from_date}: {n} (page,ngày)")
        conn.commit()
    print(f"[vat-rule] DONE — {total} tick áp/cập nhật.")


if __name__ == "__main__":
    main()
