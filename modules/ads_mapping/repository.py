from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from db import get_conn

from .constants import ADS_COST_MULTIPLIER, ADS_VAT_RATE


def mapping_schema_ready() -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'ads_product_mapping_rules'
                    LIMIT 1
                    """
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def _shop_filter_sql(allowed_keys: Optional[set]) -> Tuple[str, List[Any]]:
    if allowed_keys is None:
        return "", []
    if not allowed_keys:
        return " AND FALSE ", []
    return " AND s.shop_key = ANY(%s) ", [list(allowed_keys)]


def list_products_for_select() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, product_code, product_name
                FROM product_salary_configs
                ORDER BY product_code
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_mapping_rules() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*, p.product_code, p.product_name
                FROM ads_product_mapping_rules r
                JOIN product_salary_configs p ON p.id = r.product_id
                ORDER BY r.priority ASC, r.id ASC
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_rule(rule_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ads_product_mapping_rules WHERE id = %s", (rule_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))


def list_active_rules_for_engine() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, fb_ad_account_id, match_level, campaign_id, campaign_name,
                       adset_id, adset_name, ad_id, ad_name, name_pattern, product_id,
                       priority, effective_from, effective_to, is_active, note
                FROM ads_product_mapping_rules
                WHERE is_active = TRUE
                ORDER BY priority ASC, id ASC
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_rule(data: Dict[str, Any]) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ads_product_mapping_rules (
                    fb_ad_account_id, match_level, campaign_id, campaign_name,
                    adset_id, adset_name, ad_id, ad_name, name_pattern, product_id,
                    priority, effective_from, effective_to, is_active, note
                ) VALUES (
                    %(fb_ad_account_id)s, %(match_level)s, %(campaign_id)s, %(campaign_name)s,
                    %(adset_id)s, %(adset_name)s, %(ad_id)s, %(ad_name)s, %(name_pattern)s, %(product_id)s,
                    %(priority)s, %(effective_from)s, %(effective_to)s, %(is_active)s, %(note)s
                )
                RETURNING id
                """,
                data,
            )
            return int(cur.fetchone()[0])


def update_rule(rule_id: int, data: Dict[str, Any]) -> None:
    data = dict(data)
    data["id"] = rule_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ads_product_mapping_rules SET
                    fb_ad_account_id = %(fb_ad_account_id)s,
                    match_level = %(match_level)s,
                    campaign_id = %(campaign_id)s,
                    campaign_name = %(campaign_name)s,
                    adset_id = %(adset_id)s,
                    adset_name = %(adset_name)s,
                    ad_id = %(ad_id)s,
                    ad_name = %(ad_name)s,
                    name_pattern = %(name_pattern)s,
                    product_id = %(product_id)s,
                    priority = %(priority)s,
                    effective_from = %(effective_from)s,
                    effective_to = %(effective_to)s,
                    is_active = %(is_active)s,
                    note = %(note)s
                WHERE id = %(id)s
                """,
                data,
            )


def delete_rule(rule_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ads_product_mapping_rules WHERE id = %s", (rule_id,))


def fetch_manual_raw_ids(date_from: str, date_to: str, allowed_keys: Optional[set]) -> set:
    sf, sp = _shop_filter_sql(allowed_keys)
    q = f"""
        SELECT r.ads_raw_id
        FROM ads_product_mapping_results r
        JOIN fb_ads_daily_metrics f ON f.id = r.ads_raw_id
        JOIN shops s ON s.id = f.shop_id
        WHERE r.mapping_method = 'manual'
          AND r.stat_date >= %s::date AND r.stat_date <= %s::date
          {sf}
    """
    params: List[Any] = [date_from, date_to] + sp
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            return {int(row[0]) for row in cur.fetchall()}


def delete_auto_results_in_range(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    fb_account: Optional[str] = None,
) -> int:
    sf, sp = _shop_filter_sql(allowed_keys)
    acc_clause = ""
    params: List[Any] = [date_from, date_to] + sp
    if fb_account:
        acc_clause = " AND f.fb_ad_account_id = %s "
        params.append(fb_account.strip())
    q = f"""
        DELETE FROM ads_product_mapping_results r
        USING fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        WHERE r.ads_raw_id = f.id
          AND r.mapping_method <> 'manual'
          AND f.metric_date >= %s::date AND f.metric_date <= %s::date
          {sf}
          {acc_clause}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            return cur.rowcount


def clear_open_queue_in_range(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    fb_account: Optional[str] = None,
) -> None:
    sf, sp = _shop_filter_sql(allowed_keys)
    acc_clause = ""
    params: List[Any] = [date_from, date_to] + sp
    if fb_account:
        acc_clause = " AND f.fb_ad_account_id = %s "
        params.append(fb_account.strip())
    q = f"""
        DELETE FROM ads_unmapped_queue q
        USING fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        WHERE q.ads_raw_id = f.id
          AND q.stat_date >= %s::date AND q.stat_date <= %s::date
          AND q.status = 'open'
          {sf}
          {acc_clause}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)


def list_fb_raw_rows(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    fb_account: Optional[str] = None,
    campaign_kw: str = "",
) -> List[Dict[str, Any]]:
    sf, sp = _shop_filter_sql(allowed_keys)
    kw_clause = ""
    params: List[Any] = [date_from, date_to] + sp
    if fb_account:
        kw_clause += " AND f.fb_ad_account_id = %s "
        params.append(fb_account.strip())
    if campaign_kw.strip():
        kw_clause += " AND (f.account_name ILIKE %s OR f.fb_ad_account_id ILIKE %s) "
        like = f"%{campaign_kw.strip()}%"
        params.extend([like, like])
    q = f"""
        SELECT f.id, f.metric_date, f.fb_ad_account_id, f.account_name, f.spend,
               s.shop_key, s.shop_name
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        WHERE f.metric_date >= %s::date AND f.metric_date <= %s::date
        {sf}
        {kw_clause}
        ORDER BY f.metric_date DESC, f.id DESC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_mapping_result(
    ads_raw_id: int,
    stat_date: date,
    product_id: Optional[int],
    mapping_rule_id: Optional[int],
    mapping_method: str,
    confidence: float,
    status: str,
    match_detail: Optional[str],
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ads_product_mapping_results (
                    ads_raw_id, stat_date, product_id, mapping_rule_id,
                    mapping_method, confidence, status, match_detail
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ads_raw_id) DO UPDATE SET
                    stat_date = EXCLUDED.stat_date,
                    product_id = EXCLUDED.product_id,
                    mapping_rule_id = EXCLUDED.mapping_rule_id,
                    mapping_method = EXCLUDED.mapping_method,
                    confidence = EXCLUDED.confidence,
                    status = EXCLUDED.status,
                    match_detail = EXCLUDED.match_detail
                WHERE ads_product_mapping_results.mapping_method <> 'manual'
                   OR EXCLUDED.mapping_method = 'manual'
                """,
                (
                    ads_raw_id,
                    stat_date,
                    product_id,
                    mapping_rule_id,
                    mapping_method,
                    confidence,
                    status,
                    match_detail,
                ),
            )


def upsert_manual_mapping(
    ads_raw_id: int,
    product_id: int,
    actor: str,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric_date FROM fb_ads_daily_metrics WHERE id = %s",
                (ads_raw_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("ads_raw_id not found")
            stat_date = row[0]
            cur.execute(
                """
                INSERT INTO ads_product_mapping_results (
                    ads_raw_id, stat_date, product_id, mapping_rule_id,
                    mapping_method, confidence, status, match_detail
                ) VALUES (%s, %s, %s, NULL, 'manual', 100, 'mapped', %s)
                ON CONFLICT (ads_raw_id) DO UPDATE SET
                    stat_date = EXCLUDED.stat_date,
                    product_id = EXCLUDED.product_id,
                    mapping_rule_id = NULL,
                    mapping_method = 'manual',
                    confidence = 100,
                    status = 'mapped',
                    match_detail = EXCLUDED.match_detail
                """,
                (ads_raw_id, stat_date, product_id, f"manual user={actor}"),
            )


def get_raw_row_for_access(ads_raw_id: int, allowed_keys: Optional[set]) -> Optional[Dict[str, Any]]:
    sf, sp = _shop_filter_sql(allowed_keys)
    q = f"""
        SELECT f.id, f.metric_date, f.fb_ad_account_id, f.spend, s.shop_key
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        WHERE f.id = %s
        {sf}
    """
    params: List[Any] = [ads_raw_id] + sp
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))


def delete_open_queue_for_raw(ads_raw_id: int, stat_date: date) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM ads_unmapped_queue
                WHERE ads_raw_id = %s AND stat_date = %s AND status = 'open'
                """,
                (ads_raw_id, stat_date),
            )


def upsert_unmapped_queue(ads_raw_id: int, stat_date: date, reason: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ads_unmapped_queue (ads_raw_id, stat_date, reason, status)
                VALUES (%s, %s, %s, 'open')
                ON CONFLICT (ads_raw_id, stat_date) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    updated_at = NOW()
                WHERE ads_unmapped_queue.status = 'open'
                """,
                (ads_raw_id, stat_date, reason),
            )


def list_map_rows_enriched(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    *,
    fb_account: Optional[str] = None,
    campaign_kw: str = "",
    status_filter: str = "",
    product_id: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sf, sp = _shop_filter_sql(allowed_keys)
    params: List[Any] = [date_from, date_to] + sp
    extra = ""
    if fb_account:
        extra += " AND f.fb_ad_account_id = %s "
        params.append(fb_account.strip())
    if campaign_kw.strip():
        extra += " AND (f.account_name ILIKE %s OR f.fb_ad_account_id ILIKE %s) "
        like = f"%{campaign_kw.strip()}%"
        params.extend([like, like])
    if product_id:
        extra += " AND r.product_id = %s "
        params.append(product_id)
    status_clause = ""
    if status_filter == "mapped":
        status_clause = " AND COALESCE(r.status, 'unmapped') IN ('mapped', 'low_confidence') "
    elif status_filter == "unmapped":
        status_clause = " AND (r.id IS NULL OR r.status = 'unmapped') "
    elif status_filter == "low_confidence":
        status_clause = " AND r.status = 'low_confidence' "
    q = f"""
        SELECT f.id AS ads_raw_id, f.metric_date AS stat_date,
               f.fb_ad_account_id, f.account_name, f.spend,
               s.shop_key, s.shop_name,
               r.product_id, r.mapping_method, r.confidence, r.status, r.mapping_rule_id, r.match_detail,
               p.product_code, p.product_name
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        LEFT JOIN ads_product_mapping_results r ON r.ads_raw_id = f.id
        LEFT JOIN product_salary_configs p ON p.id = r.product_id
        WHERE f.metric_date >= %s::date AND f.metric_date <= %s::date
        {sf}
        {extra}
        {status_clause}
        ORDER BY f.metric_date DESC, f.id DESC
        LIMIT {int(limit)}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            cols = [c[0] for c in cur.description]
            out = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if d.get("mapping_method") is None:
                    d["mapping_method"] = "unmapped"
                    d["status"] = "unmapped"
                    d["confidence"] = 0
                out.append(d)
            return out


def list_unmapped_operational(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    limit: int = 300,
) -> List[Dict[str, Any]]:
    sf, sp = _shop_filter_sql(allowed_keys)
    params: List[Any] = [date_from, date_to] + sp
    q = f"""
        SELECT f.id AS ads_raw_id, f.metric_date AS stat_date,
               f.fb_ad_account_id, f.account_name, f.spend,
               s.shop_key, s.shop_name,
               r.status AS result_status, r.mapping_method, r.confidence, r.match_detail,
               q.id AS queue_id, q.status AS queue_status, q.reason AS queue_reason
        FROM fb_ads_daily_metrics f
        JOIN shops s ON s.id = f.shop_id
        LEFT JOIN ads_product_mapping_results r ON r.ads_raw_id = f.id
        LEFT JOIN ads_unmapped_queue q ON q.ads_raw_id = f.id AND q.stat_date = f.metric_date
        WHERE f.metric_date >= %s::date AND f.metric_date <= %s::date
        {sf}
          AND (
            r.id IS NULL OR r.status IN ('unmapped', 'low_confidence')
          )
        ORDER BY f.metric_date DESC, f.id DESC
        LIMIT {int(limit)}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_product_ads_cost_daily(
    date_from: str,
    date_to: str,
    allowed_keys: Optional[set],
    product_id: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sf, sp = _shop_filter_sql(allowed_keys)
    params: List[Any] = [date_from, date_to]
    scope_clause = ""
    if sp:
        scope_clause = f"""
          AND EXISTS (
            SELECT 1 FROM ads_product_mapping_results r
            JOIN fb_ads_daily_metrics f ON f.id = r.ads_raw_id
            JOIN shops s ON s.id = f.shop_id
            WHERE r.product_id = d.product_id AND f.metric_date = d.stat_date
            {sf}
          )
        """
        params.extend(sp)
    prod_clause = ""
    if product_id:
        prod_clause = " AND d.product_id = %s "
        params.append(product_id)
    q = f"""
        SELECT d.stat_date, d.product_id, d.total_spend, d.total_vat, d.total_cost,
               d.source_rows_count, d.calc_version,
               p.product_code, p.product_name
        FROM product_ads_cost_daily d
        JOIN product_salary_configs p ON p.id = d.product_id
        WHERE d.stat_date >= %s::date AND d.stat_date <= %s::date
          AND d.calc_version = 1
        {scope_clause}
        {prod_clause}
        ORDER BY d.stat_date DESC, p.product_code
        LIMIT {int(limit)}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def rebuild_aggregate_product_cost(
    date_from: str,
    date_to: str,
    calc_version: int = 1,
) -> Tuple[int, int]:
    """
    Full recompute for date range (global). Caller must restrict to admin/accountant.
    Returns (deleted_rows, inserted_rows).
    """
    params_del: List[Any] = [calc_version, date_from, date_to]
    insert_params: List[Any] = [
        ADS_VAT_RATE,
        ADS_COST_MULTIPLIER,
        calc_version,
        date_from,
        date_to,
    ]
    q_del = """
        DELETE FROM product_ads_cost_daily
        WHERE calc_version = %s
          AND stat_date >= %s::date AND stat_date <= %s::date
    """
    q_ins = """
        INSERT INTO product_ads_cost_daily (
            stat_date, product_id, total_spend, total_vat, total_cost, source_rows_count, calc_version
        )
        SELECT f.metric_date, r.product_id,
               SUM(f.spend)::numeric(18,2),
               SUM(f.spend * %s::numeric)::numeric(18,2),
               SUM(f.spend * %s::numeric)::numeric(18,2),
               COUNT(*)::int,
               %s
        FROM ads_product_mapping_results r
        JOIN fb_ads_daily_metrics f ON f.id = r.ads_raw_id
        WHERE r.product_id IS NOT NULL
          AND r.status IN ('mapped', 'low_confidence')
          AND f.metric_date >= %s::date AND f.metric_date <= %s::date
        GROUP BY f.metric_date, r.product_id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q_del, params_del)
            deleted = cur.rowcount
            cur.execute(q_ins, insert_params)
            inserted = cur.rowcount
            return deleted, inserted
