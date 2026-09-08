#!/usr/bin/env python3
"""Marketing Brain — canh khoảnh khắc shipcod bắt đầu gửi ad_id THẬT.

Bối cảnh: shipcod đang sửa khâu bắt referral FB. Đơn về hàng ngày. Script này
soi inbox + tự kiểm: mã ads về chưa, có khớp Ads Manager (mb_fb_entity_daily) chưa,
organic đã tụt về mức thật chưa. In verdict rõ ràng — dùng cho check tay lẫn cron.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import get_conn  # noqa: E402
from tz_utils import now_hcm  # noqa: E402


def main() -> None:
    today = now_hcm().date().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Inbox shipcod hôm nay: tổng / có ad_id / khớp bảng spend
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE i.ad_id <> '') AS co_ad,
                       COUNT(*) FILTER (WHERE i.ad_id <> '' AND EXISTS (
                           SELECT 1 FROM mb_fb_entity_daily e WHERE e.ad_id = i.ad_id)) AS khop_admgr,
                       COUNT(*) FILTER (WHERE UPPER(i.ad_source) = 'ADS' AND i.ad_id = '') AS ads_hut_ma
                FROM mb_attribution_inbox i
                WHERE i.received_at::date = %s
            """, (today,))
            total, co_ad, khop, hut = cur.fetchone()

            # 2. Organic % hôm nay (mức thật ~6-11%)
            cur.execute("""
                SELECT COUNT(*),
                       ROUND(100.0*COUNT(*) FILTER (WHERE ad_id<>'')/GREATEST(COUNT(*),1),0),
                       ROUND(100.0*COUNT(*) FILTER (WHERE is_organic)/GREATEST(COUNT(*),1),0)
                FROM mb_order_attribution WHERE order_date = %s
            """, (today,))
            don, paid_pct, org_pct = cur.fetchone()

            # 3. Mẫu mã ad đầu tiên khớp (để báo shipcod đối chiếu Ads Manager)
            cur.execute("""
                SELECT i.ad_id, i.pos_shop_id, i.pancake_order_id, e.campaign_name
                FROM mb_attribution_inbox i
                JOIN mb_fb_entity_daily e ON e.ad_id = i.ad_id
                WHERE i.received_at::date = %s
                ORDER BY i.received_at DESC LIMIT 3
            """, (today,))
            samples = cur.fetchall()

    print(f"=== MB WATCH ad_id — {today} ===")
    print(f"Inbox shipcod hôm nay: {total} gói | có ad_id: {co_ad} | khớp Ads Manager: {khop} | ADS hụt mã: {hut}")
    print(f"Đơn hôm nay: {don} | paid {paid_pct}% | organic {org_pct}% (thật ~6-11%)")

    if co_ad == 0:
        print("❌ CHƯA CÓ mã ads nào — khâu bắt referral shipcod CHƯA nổ. Tiếp tục canh.")
    elif khop == 0:
        print("⚠️ CÓ ad_id nhưng KHÔNG khớp bảng spend Ads Manager — SAI namespace. Cần soi field shipcod gửi.")
    else:
        print(f"✅ CHẠY RỒI! {khop} đơn có mã ads KHỚP Ads Manager. organic đang về {org_pct}%.")
        for ad, shop, oid, camp in samples:
            print(f"   ad_id={ad} shop={shop} order={oid} camp={(camp or '')[:40]}")
        print("→ Báo shipcod: capture OK, đối chiếu mẫu trên với Ads Manager là chốt.")


if __name__ == "__main__":
    main()
