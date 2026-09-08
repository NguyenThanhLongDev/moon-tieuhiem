from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .constants import MATCH_LEVELS_ORDER, METHOD_BY_LEVEL

logger = logging.getLogger(__name__)


def _norm(s: Any) -> str:
    return str(s or "").strip()


def raw_text_blob(raw: Dict[str, Any]) -> str:
    """Single string for name_pattern matching (extensible when raw gains campaign/ad fields)."""
    parts = [
        _norm(raw.get("account_name")),
        _norm(raw.get("fb_ad_account_id")),
        _norm(raw.get("campaign_name")),
        _norm(raw.get("adset_name")),
        _norm(raw.get("ad_name")),
        _norm(raw.get("shop_name")),
        _norm(raw.get("shop_key")),
    ]
    return " ".join(p for p in parts if p).lower()


def rule_effective_on(rule: Dict[str, Any], metric_date: date) -> bool:
    ef = rule.get("effective_from")
    et = rule.get("effective_to")
    if ef and metric_date < ef:
        return False
    if et and metric_date > et:
        return False
    return True


def rule_account_matches(rule: Dict[str, Any], raw: Dict[str, Any]) -> bool:
    acc = rule.get("fb_ad_account_id")
    if not acc:
        return True
    return _norm(acc) == _norm(raw.get("fb_ad_account_id"))


def name_pattern_matches(pattern: Optional[str], blob: str) -> bool:
    if not pattern or not str(pattern).strip():
        return False
    p = str(pattern).strip().lower()
    return p in blob


def level_matches(rule: Dict[str, Any], raw: Dict[str, Any], level: str) -> bool:
    if _norm(rule.get("match_level")) != level:
        return False
    if level == "ad":
        rid, rname = _norm(rule.get("ad_id")), _norm(rule.get("ad_name"))
        raw_id, raw_name = _norm(raw.get("ad_id")), _norm(raw.get("ad_name"))
        if rid and raw_id:
            return rid == raw_id
        if rname and raw_name and rname.lower() == raw_name.lower():
            return True
        return False
    if level == "adset":
        rid, rname = _norm(rule.get("adset_id")), _norm(rule.get("adset_name"))
        raw_id, raw_name = _norm(raw.get("adset_id")), _norm(raw.get("adset_name"))
        if rid and raw_id:
            return rid == raw_id
        if rname and raw_name and rname.lower() == raw_name.lower():
            return True
        return False
    if level == "campaign":
        rid, rname = _norm(rule.get("campaign_id")), _norm(rule.get("campaign_name"))
        raw_id, raw_name = _norm(raw.get("campaign_id")), _norm(raw.get("campaign_name"))
        if rid and raw_id:
            return rid == raw_id
        if rname and raw_name and rname.lower() == raw_name.lower():
            return True
        return False
    if level == "name_pattern":
        return name_pattern_matches(rule.get("name_pattern"), raw_text_blob(raw))
    return False


def confidence_for_level(level: str) -> float:
    return {"ad": 100.0, "adset": 95.0, "campaign": 90.0, "name_pattern": 65.0}.get(level, 0.0)


def status_for_match(level: str) -> str:
    if level == "name_pattern":
        return "low_confidence"
    return "mapped"


def pick_mapping_for_raw(
    raw: Dict[str, Any],
    rules: List[Dict[str, Any]],
    *,
    metric_date: date,
) -> Tuple[str, Optional[int], Optional[int], float, str, str]:
    """
    Returns: mapping_method, product_id, rule_id, confidence, status, match_detail
    """
    blob = raw_text_blob(raw)
    active_rules = [
        r
        for r in rules
        if r.get("is_active")
        and rule_effective_on(r, metric_date)
        and rule_account_matches(r, raw)
    ]
    active_rules.sort(key=lambda x: (int(x.get("priority") or 100), int(x.get("id") or 0)))

    for level in MATCH_LEVELS_ORDER:
        for rule in active_rules:
            if not level_matches(rule, raw, level):
                continue
            pid = rule.get("product_id")
            if pid is None:
                continue
            method = METHOD_BY_LEVEL.get(level, "name_pattern")
            conf = confidence_for_level(level)
            st = status_for_match(level)
            detail = f"rule_id={rule.get('id')} match_level={level} priority={rule.get('priority')}"
            logger.debug("ads_mapping match raw_id=%s %s", raw.get("id"), detail)
            return method, int(pid), int(rule["id"]), conf, st, detail

    return "unmapped", None, None, 0.0, "unmapped", "no_rule_matched"


def sort_rules_for_display(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rules, key=lambda x: (int(x.get("priority") or 100), int(x.get("id") or 0)))
