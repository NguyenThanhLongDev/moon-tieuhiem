from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass
class SalaryConfig:
    id: int
    code: str
    name: str
    branch_code: str
    is_active: bool
    default_vat_rate: float
    deduct_shipping_fee: bool
    stop_ads_threshold: Optional[float]
    notes: str
    created_at: Any = None
    updated_at: Any = None


@dataclass
class SalaryKpiTier:
    id: int
    salary_config_id: int
    min_ads_per_order: float
    max_ads_per_order: Optional[float]
    kpi_percent: float
    is_stop_run: bool
    sort_order: int


@dataclass
class SalaryEmployeeSetting:
    id: int
    employee_code: str
    employee_name: str
    branch_code: str
    base_salary: float
    allowance: float
    config_id: int
    is_active: bool
    created_at: Any = None
    updated_at: Any = None


@dataclass
class SalaryCalcResult:
    period_key: str
    employee_code: str
    employee_name: str
    branch_code: str
    revenue_gross: float
    vat_rate: float
    revenue_net: float
    shipping_fee: float
    revenue_for_kpi: float
    ads_cost: float
    orders_count: int
    ads_per_order: float
    kpi_percent: float
    kpi_salary: float
    base_salary: float
    allowance: float
    penalty: float
    advance: float
    final_salary: float
    stop_ads: bool
    calc_snapshot_json: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
