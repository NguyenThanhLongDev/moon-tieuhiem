#!/usr/bin/env python3
"""⛔ DEPRECATED 12/06/2026 — KHÔNG CHẠY SCRIPT NÀY.

Sếp đã chốt bỏ mô hình lương cũ (/salary, tier CPQC per SP) — dùng /salary-2b.
Audit 12/06: script này SUM(order_items.line_total) = GIÁ NIÊM YẾT → doanh thu
tính lương phồng ~8,5% (ratio net/total = 0,9218). 327 dòng input kỳ 2026-06
đã bị xóa. Muốn hồi sinh: phải nhân tỷ lệ net_revenue/total_amount của đơn
(xem modules/marketing_brain Top SKU) và được sếp duyệt lại.

ETL kỳ lương — tự động đổ dữ liệu (NV × Sản phẩm × Tháng) vào salary_period_input_lines.

Nguồn (100% tự động, không nhập tay):
  - Đơn + doanh thu : mb_order_attribution (đơn gắn mã ads) × order_items × wh_products
  - Chi phí ads     : mb_fb_entity_daily (spend từng ad, đã ×1.113 VAT) chia về từng SKU
                      theo tỷ trọng doanh thu SKU trong các đơn của ad đó
  - NV phụ trách    : ad → TK QC → NV (user_ad_account_assignments, đúng theo ngày hiệu lực)
  - Hoàn/hủy        : đơn status cancelled/returned/returning → cột returned_* (engine tự trừ)
  - Ship            : orders.shipping_fee chia về SKU theo tỷ trọng doanh thu trong đơn

LƯU Ý NGHIỆP VỤ:
  - Chỉ tính ĐƠN CÓ GẮN MÃ ADS (paid). Đơn organic chưa tính vào lương
    (mapping organic→NV chưa chắc chắn — chờ sếp chốt riêng).
  - Spend chưa VAT từ FB ×1.113 trước khi chia (CLAUDE.md §11.4).
  - Chạy lại an toàn: XÓA toàn bộ input lines của kỳ rồi ghi mới (số chỉnh tay sẽ mất).

Usage:
    python scripts/salary_etl_build_period.py --period 2026-06            # chạy thử, chỉ in
    python scripts/salary_etl_build_period.py --period 2026-06 --write    # ghi DB thật
"""
from __future__ import annotations

import argparse
import calendar
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import get_conn  # noqa: E402

ADS_COST_MULTIPLIER = 1.113
_BAD = ("cancelled", "returned", "returning")


def month_range(period_key: str):
    y, m = period_key.split("-")
    y, m = int(y), int(m)
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def fetch_rows(cur, sql, params):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build(period_key: str):
    d1, d2 = month_range(period_key)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Spend từng ad trong tháng + NV phụ trách (theo ngày hiệu lực gán TK)
            spend_rows = fetch_rows(cur, """
                SELECT e.ad_id, LOWER(u.username) AS nv, SUM(e.spend) AS spend_raw
                FROM mb_fb_entity_daily e
                JOIN user_ad_account_assignments uaa ON uaa.ad_account_id = e.account_id
                     AND e.metric_date >= uaa.assigned_from
                     AND e.metric_date <= COALESCE(uaa.assigned_to, DATE '9999-12-31')
                JOIN users u ON u.id = uaa.user_id
                WHERE e.metric_date BETWEEN %s AND %s
                GROUP BY e.ad_id, LOWER(u.username)
            """, (d1, d2))

            # 2. Doanh thu/đơn per (ad × SKU), kèm cờ hoàn/hủy + ship phân bổ trong đơn
            item_rows = fetch_rows(cur, """
                WITH it AS (
                    SELECT a.ad_id, a.order_id,
                           (o.order_status::text IN %s) AS is_bad,
                           o.shipping_fee,
                           p.sku, oi.quantity, oi.line_total,
                           SUM(oi.line_total) OVER (PARTITION BY a.order_id) AS order_rev
                    FROM mb_order_attribution a
                    JOIN orders o ON o.id = a.order_id
                    JOIN order_items oi ON oi.order_id = a.order_id
                    JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
                    JOIN wh_products p ON p.id = vm.product_id
                    WHERE a.ad_id <> '' AND a.order_date BETWEEN %s AND %s
                )
                SELECT ad_id, sku, is_bad,
                       COUNT(DISTINCT order_id) AS orders,
                       SUM(quantity) AS qty,
                       SUM(line_total) AS revenue,
                       SUM(CASE WHEN order_rev > 0
                                THEN shipping_fee * line_total / order_rev
                                ELSE 0 END) AS ship_alloc
                FROM it GROUP BY ad_id, sku, is_bad
            """, (_BAD, d1, d2))

    # 2B. Sổ đăng ký ad→TK (mb_fb_ads_registry, có cả ad đã tắt) → NV theo gán TK
    #     phủ kỳ lương — cứu các ad có đơn về trễ nhưng không còn spend trong kỳ.
    with get_conn() as conn:
        with conn.cursor() as cur:
            registry_rows = fetch_rows(cur, """
                SELECT reg.ad_id, LOWER(u.username) AS nv
                FROM mb_fb_ads_registry reg
                JOIN user_ad_account_assignments uaa ON uaa.ad_account_id = reg.account_id
                     AND uaa.assigned_from <= %s::date
                     AND COALESCE(uaa.assigned_to, DATE '9999-12-31') >= %s::date
                JOIN users u ON u.id = uaa.user_id
            """, (d2, d1))

    # 3. Map ad → NV; tổng doanh thu mỗi ad (trọng số chia spend — dùng MỌI đơn kể cả hoàn,
    #    vì tiền ads đã tiêu thật cho cả đơn sau này hoàn).
    #    Ưu tiên: spend trong kỳ (chính xác theo ngày) > sổ đăng ký (fallback).
    ad_nv = {r["ad_id"]: r["nv"] for r in registry_rows}
    ad_spend_vat = defaultdict(float)
    for r in spend_rows:
        ad_nv[r["ad_id"]] = r["nv"]
        ad_spend_vat[r["ad_id"]] += float(r["spend_raw"]) * ADS_COST_MULTIPLIER

    ad_total_rev = defaultdict(float)
    for r in item_rows:
        ad_total_rev[r["ad_id"]] += float(r["revenue"])

    # 4. Gom về (NV × SKU)
    lines = defaultdict(lambda: {
        "revenue_gross": 0.0, "quantity": 0.0, "orders": set(),
        "ads_cost": 0.0, "shipping_fee": 0.0,
        "returned_revenue_gross": 0.0, "returned_quantity": 0.0,
        "returned_orders": set(), "shipping_fee_return_delta": 0.0,
    })
    orphan_ads = set()   # ad có đơn nhưng không tra được NV (TK chưa gán)
    for r in item_rows:
        ad = r["ad_id"]
        nv = ad_nv.get(ad)
        if not nv:
            orphan_ads.add(ad)
            continue
        key = (nv, r["sku"])
        L = lines[key]
        rev = float(r["revenue"])
        ship = float(r["ship_alloc"] or 0)
        # ads chia theo tỷ trọng doanh thu SKU trong tổng doanh thu của ad
        tot = ad_total_rev.get(ad) or 0.0
        ads_share = ad_spend_vat.get(ad, 0.0) * (rev / tot) if tot > 0 else 0.0
        # revenue_gross = TRƯỚC hoàn (engine tự trừ returned_*)
        L["revenue_gross"] += rev
        L["quantity"] += float(r["qty"])
        L["ads_cost"] += ads_share
        L["shipping_fee"] += ship
        if r["is_bad"]:
            L["returned_revenue_gross"] += rev
            L["returned_quantity"] += float(r["qty"])
            L["shipping_fee_return_delta"] += ship
        # orders đếm distinct — cộng count theo nhóm is_bad là đủ (1 đơn không thể vừa bad vừa không)
        if r["is_bad"]:
            L["returned_orders"].add((ad, "bad", r["orders"]))
        L["orders"].add((ad, r["is_bad"], r["orders"]))

    # orders đếm chuẩn: query trả COUNT DISTINCT theo (ad,sku,is_bad) — cộng các count
    for key, L in lines.items():
        L["orders_count"] = sum(c for (_, _, c) in L["orders"])
        L["returned_orders_count"] = sum(c for (_, _, c) in L["returned_orders"])

    # 5. Ads của NV mà KHÔNG ra đơn nào (spend có, đơn 0) — vẫn phải tính chi phí.
    #    Gom vào dòng SKU ảo "__KHONG_RA_DON__" per NV để kế toán thấy, engine sẽ skip
    #    (không có product config) nhưng số liệu minh bạch trong input lines.
    ads_with_items = {r["ad_id"] for r in item_rows}
    for ad, spend in ad_spend_vat.items():
        if ad in ads_with_items or spend <= 0:
            continue
        nv = ad_nv.get(ad)
        if not nv:
            continue
        L = lines[(nv, "__KHONG_RA_DON__")]
        L["ads_cost"] += spend
        L.setdefault("orders_count", 0)
        L.setdefault("returned_orders_count", 0)

    return lines, orphan_ads, (d1, d2)


def main():
    import sys as _sys
    if "--force-deprecated" not in _sys.argv:
        print("⛔ DEPRECATED (12/06/2026): mô hình lương cũ đã bỏ — dùng /salary-2b.")
        print("   Script tính DT bằng GIÁ NIÊM YẾT (phồng ~8,5%). Xem docstring đầu file.")
        _sys.exit(1)
    _sys.argv.remove("--force-deprecated")
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, help="Kỳ lương YYYY-MM, ví dụ 2026-06")
    ap.add_argument("--write", action="store_true", help="Ghi DB (mặc định chỉ in thử)")
    args = ap.parse_args()
    period = args.period.strip()

    lines, orphan_ads, (d1, d2) = build(period)

    # Tổng hợp dễ đọc
    nv_set = sorted({k[0] for k in lines})
    sku_set = sorted({k[1] for k in lines if k[1] != "__KHONG_RA_DON__"})
    tot_rev = sum(L["revenue_gross"] for L in lines.values())
    tot_ads = sum(L["ads_cost"] for L in lines.values())
    print(f"=== ETL KỲ LƯƠNG {period} ({d1} → {d2}) ===")
    print(f"Dòng (NV × SP): {len(lines)} | NV: {len(nv_set)} | SP: {len(sku_set)}")
    print(f"Tổng doanh thu (trước hoàn): {tot_rev:,.0f}đ | Tổng ads (đã VAT): {tot_ads:,.0f}đ")
    if orphan_ads:
        print(f"⚠ {len(orphan_ads)} ad có đơn nhưng TK chưa gán NV — tiền/đơn các ad này KHÔNG vào lương ai. Cần gán TK!")

    # Check SP thiếu chuẩn CPQC (engine sẽ skip các dòng này)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT UPPER(product_code) FROM product_salary_configs")
            have_std = {r[0] for r in cur.fetchall()}
    missing_std = [s for s in sku_set if s.upper() not in have_std]
    if missing_std:
        print(f"⚠ {len(missing_std)} SP CHƯA có chuẩn CPQC (hoa hồng các SP này sẽ bị BỎ QUA khi tính):")
        print("   " + ", ".join(missing_std[:20]) + (" ..." if len(missing_std) > 20 else ""))
        print("   → Nhập tại: /salary/chuan-cpqc")

    top = sorted(lines.items(), key=lambda kv: -kv[1]["revenue_gross"])[:10]
    print("\nTop 10 dòng theo doanh thu:")
    for (nv, sku), L in top:
        print(f"  {nv:<14} {sku:<18} đơn={L['orders_count']:>4} hoàn={L['returned_orders_count']:>3} "
              f"DT={L['revenue_gross']:>12,.0f} ads={L['ads_cost']:>11,.0f}")

    if not args.write:
        print("\n[DRY-RUN] Chưa ghi gì. Thêm --write để ghi vào salary_period_input_lines.")
        return

    from modules.salary import repositories as repo
    if repo.is_period_locked(period):
        print(f"❌ Kỳ {period} đã CHỐT — không ghi đè.")
        sys.exit(1)
    # FK: input_lines.employee_code → employees. Chỉ ghi NV đã có hồ sơ trong
    # module lương; NV chưa có → liệt kê để kế toán bấm "Đồng bộ NV" / tạo hồ sơ.
    known = {str(e["employee_code"]).strip().lower() for e in repo.list_employees()}
    missing_nv = sorted({nv for (nv, _) in lines if nv not in known})
    if missing_nv:
        print(f"⚠ {len(missing_nv)} NV CHƯA có hồ sơ lương — dòng của họ KHÔNG được ghi: "
              + ", ".join(missing_nv[:15]) + (" ..." if len(missing_nv) > 15 else ""))
        print("   → /salary/employees → 'Đồng bộ từ users' hoặc thêm tay.")
    repo.delete_period_input_lines(period)
    n = 0
    for (nv, sku), L in lines.items():
        if nv not in known:
            continue
        repo.upsert_period_input_line(
            period, nv, sku,
            revenue_gross=L["revenue_gross"],
            quantity=L["quantity"],
            orders_count=int(L.get("orders_count", 0)),
            ads_cost=L["ads_cost"],
            shipping_fee=L["shipping_fee"],
            returned_revenue_gross=L["returned_revenue_gross"],
            returned_quantity=L["returned_quantity"],
            returned_orders_count=int(L.get("returned_orders_count", 0)),
            shipping_fee_return_delta=L["shipping_fee_return_delta"],
        )
        n += 1
    print(f"\n✅ Đã ghi {n} input lines cho kỳ {period}. "
          f"Tiếp theo: /salary/periods/{period} → bấm 'Tính lại' → review → chốt.")


if __name__ == "__main__":
    main()
