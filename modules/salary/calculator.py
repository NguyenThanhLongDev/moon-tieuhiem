from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import SalaryCalcResult


def _pick_tier(ads_per_order: float, tiers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for tier in sorted(tiers, key=lambda item: int(item.get("sort_order", 0))):
        min_value = float(tier.get("min_ads_per_order", 0) or 0)
        max_raw = tier.get("max_ads_per_order")
        max_value = float(max_raw) if max_raw is not None else None
        if ads_per_order < min_value:
            continue
        if max_value is not None and ads_per_order > max_value:
            continue
        return tier
    return None


def calculate_salary(payload: Dict[str, Any], config: Dict[str, Any], tiers: List[Dict[str, Any]]) -> SalaryCalcResult:
    revenue_gross = float(payload.get("revenue_gross", 0) or 0)
    vat_rate = float(payload.get("vat_rate", config.get("default_vat_rate", 0)) or 0)
    shipping_fee = float(payload.get("shipping_fee", 0) or 0)
    ads_cost = float(payload.get("ads_cost", 0) or 0)
    orders_count = int(payload.get("orders_count", 0) or 0)
    base_salary = float(payload.get("base_salary", 0) or 0)
    allowance = float(payload.get("allowance", 0) or 0)
    penalty = float(payload.get("penalty", 0) or 0)
    advance = float(payload.get("advance", 0) or 0)

    revenue_net = revenue_gross / (1 + vat_rate) if (1 + vat_rate) > 0 else 0.0
    deduct_shipping_fee = bool(config.get("deduct_shipping_fee", False))
    revenue_for_kpi = revenue_net - shipping_fee if deduct_shipping_fee else revenue_net
    if revenue_for_kpi < 0:
        revenue_for_kpi = 0.0

    ads_per_order = ads_cost / orders_count if orders_count > 0 else 0.0
    tier = _pick_tier(ads_per_order, tiers)
    kpi_percent = float(tier.get("kpi_percent", 0) or 0) if tier else 0.0
    tier_stop = bool(tier.get("is_stop_run", False)) if tier else False

    stop_threshold = config.get("stop_ads_threshold")
    stop_ads = tier_stop
    if stop_threshold is not None:
        stop_ads = stop_ads or ads_per_order > float(stop_threshold)

    if stop_ads:
        kpi_percent = 0.0

    kpi_salary = revenue_for_kpi * kpi_percent
    final_salary = base_salary + allowance + kpi_salary - penalty - advance

    snapshot = {
        "config": {
            "id": config.get("id"),
            "code": config.get("code"),
            "branch_code": config.get("branch_code"),
            "default_vat_rate": config.get("default_vat_rate"),
            "deduct_shipping_fee": config.get("deduct_shipping_fee"),
            "stop_ads_threshold": config.get("stop_ads_threshold"),
        },
        "selected_tier": tier,
        "effective_values": {
            "vat_rate": vat_rate,
            "deduct_shipping_fee": deduct_shipping_fee,
        },
    }

    return SalaryCalcResult(
        period_key=str(payload.get("period_key", "")),
        employee_code=str(payload.get("employee_code", "")),
        employee_name=str(payload.get("employee_name", "")),
        branch_code=str(payload.get("branch_code", config.get("branch_code", ""))),
        revenue_gross=revenue_gross,
        vat_rate=vat_rate,
        revenue_net=revenue_net,
        shipping_fee=shipping_fee,
        revenue_for_kpi=revenue_for_kpi,
        ads_cost=ads_cost,
        orders_count=orders_count,
        ads_per_order=ads_per_order,
        kpi_percent=kpi_percent,
        kpi_salary=kpi_salary,
        base_salary=base_salary,
        allowance=allowance,
        penalty=penalty,
        advance=advance,
        final_salary=final_salary,
        stop_ads=stop_ads,
        calc_snapshot_json=snapshot,
    )
