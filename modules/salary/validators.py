from __future__ import annotations

from typing import Any, Dict, Optional


def _to_float(value: Any, field_name: str) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số.") from exc


def _to_opt_float(value: Any, field_name: str) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số hoặc để trống.") from exc


def _to_int(value: Any, field_name: str) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số nguyên.") from exc


def _to_opt_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số nguyên hoặc để trống.") from exc


def _str(v: Any, key: str, required: bool = True) -> str:
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise ValueError(f"{key} bắt buộc.")
    return s


def parse_salary_config_form(form: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "config_code": _str(form.get("config_code"), "Mã cấu hình"),
        "config_name": _str(form.get("config_name"), "Tên cấu hình"),
        "branch_code": (str(form.get("branch_code") or "").strip() or None),
        "is_active": form.get("is_active") in ("1", "on", "true", True, "yes"),
        "deduct_vat": form.get("deduct_vat") in ("1", "on", "true", True, "yes"),
        "deduct_shipping_fee": form.get("deduct_shipping_fee") in ("1", "on", "true", True, "yes"),
        "notes": (str(form.get("notes") or "").strip() or None),
    }


def parse_cpqc_tier_form(form: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "min_cpqc_percent": _to_float(form.get("min_cpqc_percent"), "min_cpqc_percent"),
        "max_cpqc_percent": _to_opt_float(form.get("max_cpqc_percent"), "max_cpqc_percent"),
        "commission_percent": _to_float(form.get("commission_percent"), "commission_percent"),
        "require_import_price_lte": _to_opt_float(form.get("require_import_price_lte"), "require_import_price_lte"),
        "stop_run": form.get("stop_run") in ("1", "on", "true", True, "yes"),
        "sort_order": _to_int(form.get("sort_order"), "sort_order"),
        "is_active": form.get("is_active") in ("1", "on", "true", True, "yes"),
    }


def parse_product_form(form: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_code": _str(form.get("product_code"), "Mã SP"),
        "product_name": _str(form.get("product_name"), "Tên SP"),
        "import_price": _to_float(form.get("import_price"), "Giá nhập"),
        "cpqc_standard_per_order": _to_float(form.get("cpqc_standard_per_order"), "CPQC chuẩn/đơn"),
        "vat_rate": _to_opt_float(form.get("vat_rate"), "VAT"),
        "is_commission_enabled": form.get("is_commission_enabled") in ("1", "on", "true", True, "yes"),
        "notes": (str(form.get("notes") or "").strip() or None),
    }


def parse_employee_form(form: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "employee_code": _str(form.get("employee_code"), "Mã NV"),
        "employee_name": _str(form.get("employee_name"), "Tên NV"),
        "branch_code": (str(form.get("branch_code") or "").strip() or None),
        "team_code": (str(form.get("team_code") or "").strip() or None),
        "leader_code": (str(form.get("leader_code") or "").strip() or None),
        "is_active": form.get("is_active") in ("1", "on", "true", True, "yes"),
    }


def parse_employee_setting_form(form: Dict[str, Any]) -> Dict[str, Any]:
    sid = _to_opt_int(form.get("salary_config_id"), "salary_config_id")
    return {
        "employee_code": _str(form.get("employee_code"), "Mã NV"),
        "base_salary": _to_float(form.get("base_salary"), "Lương cứng"),
        "allowance": _to_float(form.get("allowance"), "Phụ cấp"),
        "default_penalty": _to_float(form.get("default_penalty"), "Phạt mặc định"),
        "default_advance": _to_float(form.get("default_advance"), "Tạm ứng mặc định"),
        "salary_config_id": sid,
        "is_active": form.get("is_active") in ("1", "on", "true", True, "yes"),
    }


# Giữ hàm cũ cho test calculator (modules/salary/test_salary.py) — không dùng trong phase 1 route.
def validate_calculation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object.")
    revenue_gross = _to_float(payload.get("revenue_gross"), "revenue_gross")
    vat_rate = _to_float(payload.get("vat_rate"), "vat_rate")
    shipping_fee = _to_float(payload.get("shipping_fee"), "shipping_fee")
    ads_cost = _to_float(payload.get("ads_cost"), "ads_cost")
    orders_count = _to_int(payload.get("orders_count"), "orders_count")
    base_salary = _to_float(payload.get("base_salary"), "base_salary")
    allowance = _to_float(payload.get("allowance"), "allowance")
    penalty = _to_float(payload.get("penalty"), "penalty")
    advance = _to_float(payload.get("advance"), "advance")
    if revenue_gross < 0:
        raise ValueError("revenue_gross cannot be negative.")
    if vat_rate < 0:
        raise ValueError("vat_rate cannot be negative.")
    if shipping_fee < 0:
        raise ValueError("shipping_fee cannot be negative.")
    if ads_cost < 0:
        raise ValueError("ads_cost cannot be negative.")
    if orders_count < 0:
        raise ValueError("orders_count cannot be negative.")
    payload["revenue_gross"] = revenue_gross
    payload["vat_rate"] = vat_rate
    payload["shipping_fee"] = shipping_fee
    payload["ads_cost"] = ads_cost
    payload["orders_count"] = orders_count
    payload["base_salary"] = base_salary
    payload["allowance"] = allowance
    payload["penalty"] = penalty
    payload["advance"] = advance
    return payload
