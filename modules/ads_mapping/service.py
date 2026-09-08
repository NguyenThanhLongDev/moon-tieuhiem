from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from . import repository as repo
from .engine import pick_mapping_for_raw

logger = logging.getLogger(__name__)


def rebuild_mapping_for_range(
    date_from: str,
    date_to: str,
    allowed_shop_keys: Optional[set],
    *,
    fb_ad_account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent for auto rows: deletes non-manual results in range, re-applies rules.
    Preserves mapping_method = manual.
    """
    if not repo.mapping_schema_ready():
        raise RuntimeError("Chưa có bảng ads mapping — chạy migration 011_ads_product_mapping.sql")

    rules = repo.list_active_rules_for_engine()
    manual_ids = repo.fetch_manual_raw_ids(date_from, date_to, allowed_shop_keys)
    deleted = repo.delete_auto_results_in_range(date_from, date_to, allowed_shop_keys, fb_ad_account_id)
    repo.clear_open_queue_in_range(date_from, date_to, allowed_shop_keys, fb_ad_account_id)

    raw_rows = repo.list_fb_raw_rows(date_from, date_to, allowed_shop_keys, fb_ad_account_id)
    applied = 0
    queued = 0
    for raw in raw_rows:
        rid = int(raw["id"])
        if rid in manual_ids:
            continue
        md = raw["metric_date"]
        if isinstance(md, str):
            md = date.fromisoformat(md[:10])
        method, pid, rule_id, conf, st, detail = pick_mapping_for_raw(raw, rules, metric_date=md)
        repo.insert_mapping_result(
            rid,
            md,
            pid,
            rule_id,
            method,
            float(conf),
            st,
            detail,
        )
        applied += 1
        if st == "mapped":
            repo.delete_open_queue_for_raw(rid, md)
        if st in ("unmapped", "low_confidence"):
            repo.upsert_unmapped_queue(rid, md, reason=detail[:2000] if detail else st)
            queued += 1

    logger.info(
        "ads_mapping rebuild %s..%s deleted_auto=%s applied=%s queued=%s manual_preserved=%s",
        date_from,
        date_to,
        deleted,
        applied,
        queued,
        len(manual_ids),
    )
    return {
        "deleted_auto_rows": deleted,
        "raw_rows_seen": len(raw_rows),
        "auto_applied": applied,
        "queue_touched": queued,
        "manual_preserved": len(manual_ids),
    }


def rebuild_product_cost_aggregate(
    date_from: str,
    date_to: str,
) -> Dict[str, Any]:
    if not repo.mapping_schema_ready():
        raise RuntimeError("Chưa có bảng ads mapping — chạy migration 011_ads_product_mapping.sql")
    deleted, inserted = repo.rebuild_aggregate_product_cost(date_from, date_to, calc_version=1)
    logger.info("ads_mapping aggregate %s..%s deleted=%s inserted=%s", date_from, date_to, deleted, inserted)
    return {"aggregate_deleted": deleted, "aggregate_inserted": inserted}


def manual_map_row(
    ads_raw_id: int,
    product_id: int,
    actor: str,
    allowed_shop_keys: Optional[set],
) -> None:
    if not repo.mapping_schema_ready():
        raise RuntimeError("Chưa có bảng ads mapping — chạy migration 011_ads_product_mapping.sql")
    row = repo.get_raw_row_for_access(ads_raw_id, allowed_shop_keys)
    if not row:
        raise PermissionError("Không có quyền hoặc không tìm thấy dòng raw.")
    repo.upsert_manual_mapping(ads_raw_id, product_id, actor)
    md = row["metric_date"]
    if isinstance(md, str):
        md = date.fromisoformat(md[:10])
    repo.delete_open_queue_for_raw(ads_raw_id, md)
