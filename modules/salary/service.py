from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import repositories as repo
from . import period_engine

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_USERS_JSON_PATH = _REPO_ROOT / "users.json"

# Tài khoản web (Cài đặt / users.json) — role được đưa vào module lương KPI.
_SYNC_USER_ROLES = frozenset({"staff", "leader"})


def init_salary_module() -> None:
    repo.ensure_salary_schema()
    run_phase1_seed(changed_by="system")


def run_phase1_seed(changed_by: str = "system") -> Dict[str, Any]:
    """Idempotent seed: chỉ tạo default_main + tiers + mẫu nếu chưa có."""
    created: List[str] = []
    if repo.get_salary_config_by_code("default_main") is None:
        cid = repo.create_salary_config(
            {
                "config_code": "default_main",
                "config_name": "Cấu hình lương mặc định",
                "branch_code": None,
                "is_active": True,
                "deduct_vat": True,
                "deduct_shipping_fee": True,
                "notes": "Seed phase 1",
            },
            changed_by=changed_by,
        )
        created.append(f"salary_config id={cid}")
        tiers = [
            {"min_cpqc_percent": 0, "max_cpqc_percent": 70, "commission_percent": 0.035, "sort_order": 1},
            {"min_cpqc_percent": 70.01, "max_cpqc_percent": 85, "commission_percent": 0.030, "sort_order": 2},
            {"min_cpqc_percent": 85.01, "max_cpqc_percent": 100, "commission_percent": 0.025, "sort_order": 3},
            {"min_cpqc_percent": 100.01, "max_cpqc_percent": 110, "commission_percent": 0.021, "sort_order": 4},
            {"min_cpqc_percent": 110.01, "max_cpqc_percent": 130, "commission_percent": 0.015, "sort_order": 5},
            {
                "min_cpqc_percent": 130.01,
                "max_cpqc_percent": 150,
                "commission_percent": 0.011,
                "require_import_price_lte": 32000,
                "sort_order": 6,
            },
            {
                "min_cpqc_percent": 150.01,
                "max_cpqc_percent": None,
                "commission_percent": 0,
                "stop_run": True,
                "sort_order": 7,
            },
        ]
        for t in tiers:
            repo.create_cpqc_tier(cid, t, changed_by=changed_by)
        created.append("cpqc_tiers x7")

    if repo.get_employee("NV001") is None:
        repo.create_employee(
            {
                "employee_code": "NV001",
                "employee_name": "Nhân viên mẫu 1",
                "branch_code": "main",
                "is_active": True,
            },
            changed_by=changed_by,
        )
        created.append("employee NV001")

    if not any(p["product_code"] == "KIM_A" for p in repo.list_product_salary_configs()):
        repo.create_product_salary_config(
            {
                "product_code": "KIM_A",
                "product_name": "Kìm A",
                "import_price": 25000,
                "cpqc_standard_per_order": 30000,
                "vat_rate": 0.10,
                "is_commission_enabled": True,
            },
            changed_by=changed_by,
        )
        created.append("product KIM_A")
    if not any(p["product_code"] == "KEO_B" for p in repo.list_product_salary_configs()):
        repo.create_product_salary_config(
            {
                "product_code": "KEO_B",
                "product_name": "Kéo B",
                "import_price": 35000,
                "cpqc_standard_per_order": 25000,
                "vat_rate": 0.08,
                "is_commission_enabled": True,
            },
            changed_by=changed_by,
        )
        created.append("product KEO_B")

    cfg = repo.get_salary_config_by_code("default_main")
    if cfg and repo.get_employee_salary_setting("NV001") is None:
        repo.upsert_employee_salary_setting(
            {
                "employee_code": "NV001",
                "base_salary": 5200000,
                "allowance": 0,
                "default_penalty": 0,
                "default_advance": 0,
                "salary_config_id": int(cfg["id"]),
                "is_active": True,
            },
            changed_by=changed_by,
        )
        created.append("employee_salary_settings NV001")

    return {"ok": True, "created": created}


# --- Thin service facades for routes ---

def list_configs() -> List[Dict[str, Any]]:
    return repo.list_salary_configs()


def get_config(config_id: int) -> Optional[Dict[str, Any]]:
    return repo.get_salary_config(config_id)


def create_config(data: Dict[str, Any], changed_by: str) -> int:
    return repo.create_salary_config(data, changed_by)


def update_config(config_id: int, data: Dict[str, Any], changed_by: str) -> None:
    repo.update_salary_config(config_id, data, changed_by)


def list_tiers(config_id: int) -> List[Dict[str, Any]]:
    return repo.list_cpqc_tiers(config_id)


def get_tier(tier_id: int) -> Optional[Dict[str, Any]]:
    return repo.get_cpqc_tier(tier_id)


def create_tier(config_id: int, data: Dict[str, Any], changed_by: str) -> int:
    return repo.create_cpqc_tier(config_id, data, changed_by)


def update_tier(tier_id: int, data: Dict[str, Any], changed_by: str) -> None:
    repo.update_cpqc_tier(tier_id, data, changed_by)


def delete_tier(tier_id: int, changed_by: str) -> None:
    repo.delete_cpqc_tier(tier_id, changed_by)


def list_products() -> List[Dict[str, Any]]:
    return repo.list_product_salary_configs()


def get_product(pid: int) -> Optional[Dict[str, Any]]:
    return repo.get_product_salary_config(pid)


def create_product(data: Dict[str, Any], changed_by: str) -> int:
    return repo.create_product_salary_config(data, changed_by)


def update_product(pid: int, data: Dict[str, Any], changed_by: str) -> None:
    repo.update_product_salary_config(pid, data, changed_by)


def list_emps() -> List[Dict[str, Any]]:
    return repo.list_employees()


def get_emp(code: str) -> Optional[Dict[str, Any]]:
    return repo.get_employee(code)


def create_emp(data: Dict[str, Any], changed_by: str) -> int:
    return repo.create_employee(data, changed_by)


def update_emp(code: str, data: Dict[str, Any], changed_by: str) -> None:
    repo.update_employee(code, data, changed_by)


def list_emp_settings() -> List[Dict[str, Any]]:
    return repo.list_employee_salary_settings()


def get_emp_setting(code: str) -> Optional[Dict[str, Any]]:
    return repo.get_employee_salary_setting(code)


def save_emp_setting(data: Dict[str, Any], changed_by: str) -> int:
    return repo.upsert_employee_salary_setting(data, changed_by)


def config_options() -> List[Dict[str, Any]]:
    return repo.list_salary_configs_for_select()


def normalize_period_key(raw: str) -> str:
    return period_engine.normalize_period_key(raw)


def list_period_summaries() -> List[Dict[str, Any]]:
    return repo.list_period_summaries()


def get_period_registry(period_key: str) -> Optional[Dict[str, Any]]:
    return repo.get_period_registry(period_engine.normalize_period_key(period_key))


def list_employee_results_for_period(period_key: str) -> List[Dict[str, Any]]:
    return repo.list_employee_results_for_period(period_engine.normalize_period_key(period_key))


def is_period_locked(period_key: str) -> bool:
    return repo.is_period_locked(period_engine.normalize_period_key(period_key))


def recalculate_period(period_key: str) -> Dict[str, Any]:
    return period_engine.recalculate_period(period_key)


def finalize_period(period_key: str, actor: str, confirmed: bool) -> Dict[str, Any]:
    return period_engine.finalize_period(period_key, actor, confirmed)


def list_period_input_lines(period_key: str) -> List[Dict[str, Any]]:
    return repo.list_period_input_lines(period_engine.normalize_period_key(period_key))


def save_period_input_line(
    period_key: str,
    employee_code: str,
    product_code: str,
    revenue_gross: float,
    quantity: float,
    orders_count: int,
    ads_cost: float,
    shipping_fee: float,
    *,
    returned_revenue_gross: float = 0.0,
    returned_quantity: float = 0.0,
    returned_orders_count: int = 0,
    shipping_fee_return_delta: float = 0.0,
) -> None:
    pk = period_engine.normalize_period_key(period_key)
    repo.upsert_period_input_line(
        pk,
        employee_code,
        product_code,
        revenue_gross,
        quantity,
        orders_count,
        ads_cost,
        shipping_fee,
        returned_revenue_gross=returned_revenue_gross,
        returned_quantity=returned_quantity,
        returned_orders_count=returned_orders_count,
        shipping_fee_return_delta=shipping_fee_return_delta,
    )


def delete_period_input_line(period_key: str, employee_code: str, product_code: str) -> None:
    pk = period_engine.normalize_period_key(period_key)
    repo.delete_period_input_line(pk, employee_code, product_code)


def _load_users_for_salary() -> List[Dict]:
    """Load users từ DB, fallback về users.json."""
    try:
        import sys
        sys.path.insert(0, str(_REPO_ROOT))
        from user_helpers import load_all_users
        return load_all_users()
    except Exception:
        pass
    if _USERS_JSON_PATH.is_file():
        try:
            data = json.loads(_USERS_JSON_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def sync_employees_from_dashboard_users(changed_by: str) -> Dict[str, Any]:
    """
    Đồng bộ nhân sự từ DB users (tài khoản đăng nhập web) vào bảng employees + tạo employee_salary_settings nếu thiếu.

    - Chỉ user active, role staff hoặc leader (bỏ qua admin / accountant / ...).
    - employee_code: ưu tiên khóa JSON salary_employee_code hoặc employee_code, không có thì dùng username.
    - employee_name: display_name hoặc employee_name hoặc username.
    - branch_code: luôn 'main' (có thể sửa tay sau); team_code = team_id từ users DB.
    Không xóa nhân sự đã có nhưng không còn trong DB users.
    """
    users = _load_users_for_salary()
    if not users:
        raise ValueError("Không load được danh sách users — kiểm tra DB hoặc users.json.")

    cfg = repo.get_salary_config_by_code("default_main")
    default_cfg_id = int(cfg["id"]) if cfg else None

    created_emp = 0
    updated_emp = 0
    created_settings = 0
    skipped = 0
    seen_codes: List[str] = []

    for user in users:
        if not isinstance(user, dict):
            skipped += 1
            continue
        if str(user.get("status", "active")).strip().lower() != "active":
            skipped += 1
            continue
        role = str(user.get("role", "")).strip().lower()
        if role not in _SYNC_USER_ROLES:
            skipped += 1
            continue
        username = str(user.get("username", "")).strip()
        if not username:
            skipped += 1
            continue
        code = str(
            user.get("salary_employee_code") or user.get("employee_code") or username
        ).strip()[:100]
        if not code:
            skipped += 1
            continue
        display = (
            str(user.get("display_name") or user.get("employee_name") or username).strip()[:255]
            or username
        )
        team_id = str(user.get("team_id", "")).strip() or None

        payload = {
            "employee_code": code,
            "employee_name": display,
            "branch_code": "main",
            "team_code": team_id,
            "leader_code": None,
            "is_active": True,
        }
        seen_codes.append(code)

        existing = repo.get_employee(code)
        if existing:
            repo.update_employee(code, payload, changed_by=changed_by)
            updated_emp += 1
        else:
            repo.create_employee(payload, changed_by=changed_by)
            created_emp += 1

        if repo.get_employee_salary_setting(code) is None:
            repo.upsert_employee_salary_setting(
                {
                    "employee_code": code,
                    "base_salary": 0,
                    "allowance": 0,
                    "default_penalty": 0,
                    "default_advance": 0,
                    "salary_config_id": default_cfg_id,
                    "is_active": True,
                },
                changed_by=changed_by,
            )
            created_settings += 1

    return {
        "ok": True,
        "source": str(_USERS_JSON_PATH),
        "synced_user_rows": len(seen_codes),
        "employees_created": created_emp,
        "employees_updated": updated_emp,
        "salary_settings_created": created_settings,
        "skipped_users": skipped,
    }
