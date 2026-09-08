from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parents[1]

import sys  # noqa: E402

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402


def _parse_period_key(raw: str) -> Tuple[str, datetime, datetime]:
    text = (raw or "").strip()
    try:
        first = datetime.strptime(text + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"Invalid period_key (expected YYYY-MM): {raw!r}")
    if not (2000 <= first.year <= 2100):
        raise SystemExit(f"period_key out of expected range: {raw!r}")
    # Last day of month: next month - 1 day
    if first.month == 12:
        last = first.replace(year=first.year + 1, month=1, day=1)
    else:
        last = first.replace(month=first.month + 1, day=1)
    from datetime import timedelta  # local import to avoid clutter

    last = last - timedelta(days=1)
    return text, datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.min.time())


def _run_build_for_period(period_key: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """
    Build / upsert salary_period_input_lines from POS + Ads for one period_key (YYYY-MM).

    Phase 1 rules:
    - employee_code = shop_id::text
    - ads_product_day = ads_shop_day * (revenue_product_day / total_revenue_shop_day)
    - returned_* = 0 (chờ manual review / phase 2)
    - chỉ lấy sản phẩm có mapping trong pos_product_mappings (is_active = true)
    """
    pk, dfrom_dt, dto_dt = _parse_period_key(period_key)
    date_from = dfrom_dt.date().strftime("%Y-%m-%d")
    date_to = dto_dt.date().strftime("%Y-%m-%d")

    sql = """
WITH
  params AS (
    SELECT
      %(period_key)s::text AS period_key,
      %(date_from)s::date AS date_from,
      %(date_to)s::date   AS date_to
  ),

  pos_agg AS (
    SELECT
      o.shop_id,
      DATE(o.created_at_pos) AS metric_date,
      COALESCE(
        NULLIF(TRIM(oi.external_product_id), ''),
        oi.product_id::text,
        NULLIF(TRIM(oi.sku), ''),
        'name:' || oi.product_name
      ) AS pos_product_key,
      SUM(oi.line_total)::numeric(18,2)    AS revenue_product_day,
      SUM(oi.quantity)::numeric(18,2)      AS quantity_product_day,
      COUNT(DISTINCT oi.order_id)          AS orders_product_day,
      -- Phase 1: chưa auto phân bổ ship; placeholder 0, chờ rule chính thức.
      0::numeric(18,2)                     AS shipping_fee_product_day
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.id AND oi.shop_id = o.shop_id
    JOIN params p ON DATE(o.created_at_pos) BETWEEN p.date_from AND p.date_to
    GROUP BY o.shop_id, DATE(o.created_at_pos), pos_product_key
  ),

  shop_revenue AS (
    SELECT
      shop_id,
      metric_date,
      SUM(revenue_product_day) AS total_revenue_shop_day
    FROM pos_agg
    GROUP BY shop_id, metric_date
  ),

  ads_shop AS (
    SELECT
      f.shop_id,
      f.metric_date,
      COALESCE(SUM(f.spend), 0)::numeric(18,2) AS ads_shop_day
    FROM fb_ads_daily_metrics f
    JOIN params p ON f.metric_date BETWEEN p.date_from AND p.date_to
    GROUP BY f.shop_id, f.metric_date
  ),

  product_with_ads AS (
    SELECT
      pa.shop_id,
      pa.metric_date,
      pa.pos_product_key,
      pa.revenue_product_day,
      pa.quantity_product_day,
      pa.orders_product_day,
      pa.shipping_fee_product_day,
      COALESCE(a.ads_shop_day, 0)::numeric(18,2) AS ads_shop_day,
      COALESCE(sr.total_revenue_shop_day, 0)::numeric(18,2) AS total_revenue_shop_day,
      CASE
        WHEN COALESCE(a.ads_shop_day, 0) <= 0 THEN 0::numeric(18,2)
        WHEN COALESCE(sr.total_revenue_shop_day, 0) <= 0 THEN 0::numeric(18,2)
        ELSE (a.ads_shop_day * (pa.revenue_product_day / NULLIF(sr.total_revenue_shop_day, 0)))::numeric(18,2)
      END AS ads_alloc_product_day
    FROM pos_agg pa
    LEFT JOIN shop_revenue sr
      ON sr.shop_id = pa.shop_id AND sr.metric_date = pa.metric_date
    LEFT JOIN ads_shop a
      ON a.shop_id = pa.shop_id AND a.metric_date = pa.metric_date
  ),

  mapped_products AS (
    SELECT
      pw.shop_id,
      pw.metric_date,
      m.product_id,
      pw.revenue_product_day,
      pw.quantity_product_day,
      pw.orders_product_day,
      pw.shipping_fee_product_day,
      pw.ads_alloc_product_day
    FROM product_with_ads pw
    JOIN pos_product_mappings m
      ON m.pos_product_key = pw.pos_product_key
     AND m.is_active = TRUE
  ),

  agg_period AS (
    SELECT
      to_char(metric_date, 'YYYY-MM') AS period_key,
      shop_id                         AS employee_code,
      psc.product_code,
      SUM(revenue_product_day)        AS revenue_gross,
      SUM(quantity_product_day)       AS quantity,
      SUM(orders_product_day)         AS orders_count,
      SUM(ads_alloc_product_day)      AS ads_cost,
      SUM(shipping_fee_product_day)   AS shipping_fee
    FROM mapped_products mp
    JOIN product_salary_configs psc ON psc.id = mp.product_id
    GROUP BY
      to_char(metric_date, 'YYYY-MM'),
      shop_id,
      psc.product_code
  )

SELECT
  COUNT(*)                                  AS rows_period,
  COALESCE(SUM(ads_cost), 0)::numeric(18,2) AS total_ads_cost,
  COALESCE(SUM(revenue_gross), 0)::numeric(18,2) AS total_revenue
FROM agg_period ap
JOIN params p ON ap.period_key = p.period_key;
"""

    upsert_sql = """
WITH
  params AS (
    SELECT
      %(period_key)s::text AS period_key,
      %(date_from)s::date AS date_from,
      %(date_to)s::date   AS date_to
  ),
  pos_agg AS (
    SELECT
      o.shop_id,
      DATE(o.created_at_pos) AS metric_date,
      COALESCE(
        NULLIF(TRIM(oi.external_product_id), ''),
        oi.product_id::text,
        NULLIF(TRIM(oi.sku), ''),
        'name:' || oi.product_name
      ) AS pos_product_key,
      SUM(oi.line_total)::numeric(18,2)    AS revenue_product_day,
      SUM(oi.quantity)::numeric(18,2)      AS quantity_product_day,
      COUNT(DISTINCT oi.order_id)          AS orders_product_day,
      0::numeric(18,2)                     AS shipping_fee_product_day
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.id AND oi.shop_id = o.shop_id
    JOIN params p ON DATE(o.created_at_pos) BETWEEN p.date_from AND p.date_to
    GROUP BY o.shop_id, DATE(o.created_at_pos), pos_product_key
  ),
  shop_revenue AS (
    SELECT
      shop_id,
      metric_date,
      SUM(revenue_product_day) AS total_revenue_shop_day
    FROM pos_agg
    GROUP BY shop_id, metric_date
  ),
  ads_shop AS (
    SELECT
      f.shop_id,
      f.metric_date,
      COALESCE(SUM(f.spend), 0)::numeric(18,2) AS ads_shop_day
    FROM fb_ads_daily_metrics f
    JOIN params p ON f.metric_date BETWEEN p.date_from AND p.date_to
    GROUP BY f.shop_id, f.metric_date
  ),
  product_with_ads AS (
    SELECT
      pa.shop_id,
      pa.metric_date,
      pa.pos_product_key,
      pa.revenue_product_day,
      pa.quantity_product_day,
      pa.orders_product_day,
      pa.shipping_fee_product_day,
      COALESCE(a.ads_shop_day, 0)::numeric(18,2) AS ads_shop_day,
      COALESCE(sr.total_revenue_shop_day, 0)::numeric(18,2) AS total_revenue_shop_day,
      CASE
        WHEN COALESCE(a.ads_shop_day, 0) <= 0 THEN 0::numeric(18,2)
        WHEN COALESCE(sr.total_revenue_shop_day, 0) <= 0 THEN 0::numeric(18,2)
        ELSE (a.ads_shop_day * (pa.revenue_product_day / NULLIF(sr.total_revenue_shop_day, 0)))::numeric(18,2)
      END AS ads_alloc_product_day
    FROM pos_agg pa
    LEFT JOIN shop_revenue sr
      ON sr.shop_id = pa.shop_id AND sr.metric_date = pa.metric_date
    LEFT JOIN ads_shop a
      ON a.shop_id = pa.shop_id AND a.metric_date = pa.metric_date
  ),
  mapped_products AS (
    SELECT
      pw.shop_id,
      pw.metric_date,
      m.product_id,
      pw.revenue_product_day,
      pw.quantity_product_day,
      pw.orders_product_day,
      pw.shipping_fee_product_day,
      pw.ads_alloc_product_day
    FROM product_with_ads pw
    JOIN pos_product_mappings m
      ON m.pos_product_key = pw.pos_product_key
     AND m.is_active = TRUE
  ),
  agg_period AS (
    SELECT
      to_char(metric_date, 'YYYY-MM') AS period_key,
      shop_id                         AS employee_code,
      psc.product_code,
      SUM(revenue_product_day)        AS revenue_gross,
      SUM(quantity_product_day)       AS quantity,
      SUM(orders_product_day)         AS orders_count,
      SUM(ads_alloc_product_day)      AS ads_cost,
      SUM(shipping_fee_product_day)   AS shipping_fee
    FROM mapped_products mp
    JOIN product_salary_configs psc ON psc.id = mp.product_id
    GROUP BY
      to_char(metric_date, 'YYYY-MM'),
      shop_id,
      psc.product_code
  )
INSERT INTO salary_period_input_lines AS spil (
    period_key,
    employee_code,
    product_code,
    revenue_gross,
    quantity,
    orders_count,
    ads_cost,
    shipping_fee,
    returned_revenue_gross,
    returned_quantity,
    returned_orders_count,
    shipping_fee_return_delta
)
SELECT
  ap.period_key,
  ap.employee_code::varchar(100),
  ap.product_code,
  COALESCE(ap.revenue_gross, 0)::numeric(18,2),
  COALESCE(ap.quantity, 0)::numeric(18,2),
  COALESCE(ap.orders_count, 0)::int,
  COALESCE(ap.ads_cost, 0)::numeric(18,2),
  COALESCE(ap.shipping_fee, 0)::numeric(18,2),
  0::numeric(18,2) AS returned_revenue_gross,
  0::numeric(18,2) AS returned_quantity,
  0::int           AS returned_orders_count,
  0::numeric(18,2) AS shipping_fee_return_delta
FROM agg_period ap
JOIN params p ON ap.period_key = p.period_key
ON CONFLICT (period_key, employee_code, product_code)
DO UPDATE SET
  revenue_gross            = EXCLUDED.revenue_gross,
  quantity                 = EXCLUDED.quantity,
  orders_count             = EXCLUDED.orders_count,
  ads_cost                 = EXCLUDED.ads_cost,
  shipping_fee             = EXCLUDED.shipping_fee,
  returned_revenue_gross   = EXCLUDED.returned_revenue_gross,
  returned_quantity        = EXCLUDED.returned_quantity,
  returned_orders_count    = EXCLUDED.returned_orders_count,
  shipping_fee_return_delta = EXCLUDED.shipping_fee_return_delta,
  updated_at               = NOW();
"""

    params = {
      "period_key": pk,
      "date_from": date_from,
      "date_to": date_to,
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            preview = {
                "rows_period": int(row[0] or 0),
                "total_ads_cost": float(row[1] or 0),
                "total_revenue": float(row[2] or 0),
            }
        if dry_run:
            return {"ok": True, "dry_run": True, **preview}
        with conn.cursor() as cur:
            cur.execute(upsert_sql, params)
        conn.commit()
        return {"ok": True, "dry_run": False, **preview}


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build salary_period_input_lines from POS + Ads for one period_key (YYYY-MM)."
    )
    ap.add_argument("period_key", help="Kỳ lương dạng YYYY-MM, ví dụ 2026-02")
    ap.add_argument("--dry-run", action="store_true", help="Chỉ tính preview, không ghi DB.")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    out = _run_build_for_period(args.period_key, dry_run=bool(args.dry_run))
    print("ok:", out["ok"])
    print("dry_run:", out["dry_run"])
    print("period_key:", args.period_key)
    print("rows_period:", out["rows_period"])
    print("total_revenue:", out["total_revenue"])
    print("total_ads_cost:", out["total_ads_cost"])


if __name__ == "__main__":
    main()

