from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from db import get_conn

from .schema_phase1 import ensure_phase1_schema


def _f(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def _n(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def _row_config(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "config_code": str(row[1] or ""),
        "config_name": str(row[2] or ""),
        "branch_code": None if row[3] is None else str(row[3]),
        "is_active": bool(row[4]),
        "deduct_vat": bool(row[5]),
        "deduct_shipping_fee": bool(row[6]),
        "notes": None if row[7] is None else str(row[7]),
        "created_at": row[8],
        "updated_at": row[9],
    }


def _row_tier(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "salary_config_id": int(row[1]),
        "min_cpqc_percent": _f(row[2]),
        "max_cpqc_percent": _n(row[3]),
        "commission_percent": _f(row[4]),
        "require_import_price_lte": _n(row[5]),
        "stop_run": bool(row[6]),
        "sort_order": int(row[7] or 0),
        "is_active": bool(row[8]),
        "created_at": row[9],
        "updated_at": row[10],
    }


def _row_product(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "product_code": str(row[1] or ""),
        "product_name": str(row[2] or ""),
        "import_price": _f(row[3]),
        "cpqc_standard_per_order": _f(row[4]),
        "vat_rate": _n(row[5]),
        "is_commission_enabled": bool(row[6]),
        "notes": None if row[7] is None else str(row[7]),
        "created_at": row[8],
        "updated_at": row[9],
    }


def _row_employee(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "employee_code": str(row[1] or ""),
        "employee_name": str(row[2] or ""),
        "branch_code": None if row[3] is None else str(row[3]),
        "team_code": None if row[4] is None else str(row[4]),
        "leader_code": None if row[5] is None else str(row[5]),
        "is_active": bool(row[6]),
        "created_at": row[7],
        "updated_at": row[8],
    }


def _audit(
    cur: Any,
    config_type: str,
    config_ref_id: int,
    action_type: str,
    old_data: Optional[Dict[str, Any]],
    new_data: Optional[Dict[str, Any]],
    changed_by: str,
) -> None:
    cur.execute(
        """
        INSERT INTO salary_config_audit_logs (
            config_type, config_ref_id, action_type, old_data_json, new_data_json, changed_by
        ) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """,
        (
            config_type,
            int(config_ref_id),
            action_type,
            json.dumps(old_data or {}, ensure_ascii=False, default=str),
            json.dumps(new_data or {}, ensure_ascii=False, default=str),
            changed_by[:100] if changed_by else "system",
        ),
    )


def ensure_salary_schema() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            ensure_phase1_schema(cur)


def list_salary_configs() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, config_code, config_name, branch_code, is_active, deduct_vat,
                       deduct_shipping_fee, notes, created_at, updated_at
                FROM salary_configs
                ORDER BY config_code ASC
                """
            )
            rows = cur.fetchall()
    return [_row_config(r) for r in rows]


def get_salary_config(config_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, config_code, config_name, branch_code, is_active, deduct_vat,
                       deduct_shipping_fee, notes, created_at, updated_at
                FROM salary_configs WHERE id = %s
                """,
                (int(config_id),),
            )
            row = cur.fetchone()
    return _row_config(row) if row else None


def get_salary_config_by_code(config_code: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, config_code, config_name, branch_code, is_active, deduct_vat,
                       deduct_shipping_fee, notes, created_at, updated_at
                FROM salary_configs WHERE config_code = %s
                """,
                (config_code.strip(),),
            )
            row = cur.fetchone()
    return _row_config(row) if row else None


def create_salary_config(data: Dict[str, Any], changed_by: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_configs (
                    config_code, config_name, branch_code, is_active,
                    deduct_vat, deduct_shipping_fee, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(data["config_code"]).strip(),
                    str(data["config_name"]).strip(),
                    (str(data["branch_code"]).strip() or None) if data.get("branch_code") else None,
                    bool(data.get("is_active", True)),
                    bool(data.get("deduct_vat", True)),
                    bool(data.get("deduct_shipping_fee", True)),
                    (str(data.get("notes") or "").strip() or None),
                ),
            )
            new_id = int(cur.fetchone()[0])
            _audit(cur, "salary_config", new_id, "create", None, data, changed_by)
            return new_id


def update_salary_config(config_id: int, data: Dict[str, Any], changed_by: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, config_code, config_name, branch_code, is_active, deduct_vat,
                       deduct_shipping_fee, notes, created_at, updated_at
                FROM salary_configs WHERE id = %s
                """,
                (int(config_id),),
            )
            old_row = cur.fetchone()
            if not old_row:
                raise ValueError("salary_config not found")
            old = _row_config(old_row)
            cur.execute(
                """
                UPDATE salary_configs SET
                    config_code = %s,
                    config_name = %s,
                    branch_code = %s,
                    is_active = %s,
                    deduct_vat = %s,
                    deduct_shipping_fee = %s,
                    notes = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(data["config_code"]).strip(),
                    str(data["config_name"]).strip(),
                    (str(data["branch_code"]).strip() or None) if data.get("branch_code") else None,
                    bool(data.get("is_active", True)),
                    bool(data.get("deduct_vat", True)),
                    bool(data.get("deduct_shipping_fee", True)),
                    (str(data.get("notes") or "").strip() or None),
                    int(config_id),
                ),
            )
            cur.execute(
                """
                SELECT id, config_code, config_name, branch_code, is_active, deduct_vat,
                       deduct_shipping_fee, notes, created_at, updated_at
                FROM salary_configs WHERE id = %s
                """,
                (int(config_id),),
            )
            new_row = cur.fetchone()
            new = _row_config(new_row) if new_row else old
            _audit(cur, "salary_config", int(config_id), "update", old, new, changed_by)


def list_cpqc_tiers(salary_config_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, salary_config_id, min_cpqc_percent, max_cpqc_percent, commission_percent,
                       require_import_price_lte, stop_run, sort_order, is_active, created_at, updated_at
                FROM salary_cpqc_tiers
                WHERE salary_config_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (int(salary_config_id),),
            )
            rows = cur.fetchall()
    return [_row_tier(r) for r in rows]


def get_cpqc_tier(tier_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, salary_config_id, min_cpqc_percent, max_cpqc_percent, commission_percent,
                       require_import_price_lte, stop_run, sort_order, is_active, created_at, updated_at
                FROM salary_cpqc_tiers WHERE id = %s
                """,
                (int(tier_id),),
            )
            row = cur.fetchone()
    return _row_tier(row) if row else None


def create_cpqc_tier(salary_config_id: int, data: Dict[str, Any], changed_by: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_cpqc_tiers (
                    salary_config_id, min_cpqc_percent, max_cpqc_percent, commission_percent,
                    require_import_price_lte, stop_run, sort_order, is_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    int(salary_config_id),
                    _f(data.get("min_cpqc_percent")),
                    data.get("max_cpqc_percent") if data.get("max_cpqc_percent") not in ("", None) else None,
                    _f(data.get("commission_percent")),
                    data.get("require_import_price_lte") if data.get("require_import_price_lte") not in ("", None) else None,
                    bool(data.get("stop_run", False)),
                    int(data.get("sort_order", 0) or 0),
                    bool(data.get("is_active", True)),
                ),
            )
            tid = int(cur.fetchone()[0])
            _audit(cur, "cpqc_tier", tid, "create", None, {**data, "salary_config_id": salary_config_id}, changed_by)
            return tid


def update_cpqc_tier(tier_id: int, data: Dict[str, Any], changed_by: str) -> None:
    old_t = get_cpqc_tier(int(tier_id))
    if not old_t:
        raise ValueError("tier not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE salary_cpqc_tiers SET
                    min_cpqc_percent = %s,
                    max_cpqc_percent = %s,
                    commission_percent = %s,
                    require_import_price_lte = %s,
                    stop_run = %s,
                    sort_order = %s,
                    is_active = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    _f(data.get("min_cpqc_percent")),
                    data.get("max_cpqc_percent") if data.get("max_cpqc_percent") not in ("", None) else None,
                    _f(data.get("commission_percent")),
                    data.get("require_import_price_lte") if data.get("require_import_price_lte") not in ("", None) else None,
                    bool(data.get("stop_run", False)),
                    int(data.get("sort_order", 0) or 0),
                    bool(data.get("is_active", True)),
                    int(tier_id),
                ),
            )
            cur.execute(
                """
                SELECT id, salary_config_id, min_cpqc_percent, max_cpqc_percent, commission_percent,
                       require_import_price_lte, stop_run, sort_order, is_active, created_at, updated_at
                FROM salary_cpqc_tiers WHERE id = %s
                """,
                (int(tier_id),),
            )
            nr = cur.fetchone()
            new_t = _row_tier(nr) if nr else old_t
            _audit(cur, "cpqc_tier", int(tier_id), "update", old_t, new_t, changed_by)


def delete_cpqc_tier(tier_id: int, changed_by: str) -> None:
    old_t = get_cpqc_tier(int(tier_id))
    if not old_t:
        raise ValueError("tier not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM salary_cpqc_tiers WHERE id = %s", (int(tier_id),))
            _audit(cur, "cpqc_tier", int(tier_id), "delete", old_t, None, changed_by)


def list_product_salary_configs() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, product_code, product_name, import_price, cpqc_standard_per_order,
                       vat_rate, is_commission_enabled, notes, created_at, updated_at
                FROM product_salary_configs
                ORDER BY product_code ASC
                """
            )
            rows = cur.fetchall()
    return [_row_product(r) for r in rows]


def get_product_salary_config(product_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, product_code, product_name, import_price, cpqc_standard_per_order,
                       vat_rate, is_commission_enabled, notes, created_at, updated_at
                FROM product_salary_configs WHERE id = %s
                """,
                (int(product_id),),
            )
            row = cur.fetchone()
    return _row_product(row) if row else None


def get_product_salary_config_by_code(product_code: str) -> Optional[Dict[str, Any]]:
    code = str(product_code or "").strip()
    if not code:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, product_code, product_name, import_price, cpqc_standard_per_order,
                       vat_rate, is_commission_enabled, notes, created_at, updated_at
                FROM product_salary_configs WHERE product_code = %s
                """,
                (code,),
            )
            row = cur.fetchone()
    return _row_product(row) if row else None


def create_product_salary_config(data: Dict[str, Any], changed_by: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product_salary_configs (
                    product_code, product_name, import_price, cpqc_standard_per_order,
                    vat_rate, is_commission_enabled, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(data["product_code"]).strip(),
                    str(data["product_name"]).strip(),
                    _f(data.get("import_price")),
                    _f(data.get("cpqc_standard_per_order")),
                    data.get("vat_rate") if data.get("vat_rate") not in ("", None) else None,
                    bool(data.get("is_commission_enabled", True)),
                    (str(data.get("notes") or "").strip() or None),
                ),
            )
            pid = int(cur.fetchone()[0])
            _audit(cur, "product_salary_config", pid, "create", None, data, changed_by)
            return pid


def update_product_salary_config(product_id: int, data: Dict[str, Any], changed_by: str) -> None:
    old_p = get_product_salary_config(int(product_id))
    if not old_p:
        raise ValueError("product_salary_config not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product_salary_configs SET
                    product_code = %s,
                    product_name = %s,
                    import_price = %s,
                    cpqc_standard_per_order = %s,
                    vat_rate = %s,
                    is_commission_enabled = %s,
                    notes = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(data["product_code"]).strip(),
                    str(data["product_name"]).strip(),
                    _f(data.get("import_price")),
                    _f(data.get("cpqc_standard_per_order")),
                    data.get("vat_rate") if data.get("vat_rate") not in ("", None) else None,
                    bool(data.get("is_commission_enabled", True)),
                    (str(data.get("notes") or "").strip() or None),
                    int(product_id),
                ),
            )
            cur.execute(
                """
                SELECT id, product_code, product_name, import_price, cpqc_standard_per_order,
                       vat_rate, is_commission_enabled, notes, created_at, updated_at
                FROM product_salary_configs WHERE id = %s
                """,
                (int(product_id),),
            )
            nr = cur.fetchone()
            new_p = _row_product(nr) if nr else old_p
            _audit(cur, "product_salary_config", int(product_id), "update", old_p, new_p, changed_by)


def list_employees() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, employee_code, employee_name, branch_code, team_code, leader_code,
                       is_active, created_at, updated_at
                FROM employees
                ORDER BY employee_code ASC
                """
            )
            rows = cur.fetchall()
    return [_row_employee(r) for r in rows]


def get_employee(employee_code: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, employee_code, employee_name, branch_code, team_code, leader_code,
                       is_active, created_at, updated_at
                FROM employees WHERE employee_code = %s
                """,
                (employee_code.strip(),),
            )
            row = cur.fetchone()
    return _row_employee(row) if row else None


def create_employee(data: Dict[str, Any], changed_by: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO employees (
                    employee_code, employee_name, branch_code, team_code, leader_code, is_active
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(data["employee_code"]).strip(),
                    str(data["employee_name"]).strip(),
                    (str(data["branch_code"]).strip() or None) if data.get("branch_code") else None,
                    (str(data["team_code"]).strip() or None) if data.get("team_code") else None,
                    (str(data["leader_code"]).strip() or None) if data.get("leader_code") else None,
                    bool(data.get("is_active", True)),
                ),
            )
            eid = int(cur.fetchone()[0])
            _audit(cur, "employee", eid, "create", None, data, changed_by)
            return eid


def update_employee(employee_code: str, data: Dict[str, Any], changed_by: str) -> None:
    old_e = get_employee(employee_code)
    if not old_e:
        raise ValueError("employee not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE employees SET
                    employee_name = %s,
                    branch_code = %s,
                    team_code = %s,
                    leader_code = %s,
                    is_active = %s,
                    updated_at = NOW()
                WHERE employee_code = %s
                """,
                (
                    str(data["employee_name"]).strip(),
                    (str(data["branch_code"]).strip() or None) if data.get("branch_code") else None,
                    (str(data["team_code"]).strip() or None) if data.get("team_code") else None,
                    (str(data["leader_code"]).strip() or None) if data.get("leader_code") else None,
                    bool(data.get("is_active", True)),
                    employee_code.strip(),
                ),
            )
            cur.execute(
                """
                SELECT id, employee_code, employee_name, branch_code, team_code, leader_code,
                       is_active, created_at, updated_at
                FROM employees WHERE employee_code = %s
                """,
                (employee_code.strip(),),
            )
            nr = cur.fetchone()
            new_e = _row_employee(nr) if nr else old_e
            _audit(cur, "employee", int(old_e["id"]), "update", old_e, new_e, changed_by)


def _row_emp_setting(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "employee_code": str(row[1] or ""),
        "base_salary": _f(row[2]),
        "allowance": _f(row[3]),
        "default_penalty": _f(row[4]),
        "default_advance": _f(row[5]),
        "salary_config_id": None if row[6] is None else int(row[6]),
        "is_active": bool(row[7]),
        "created_at": row[8],
        "updated_at": row[9],
        "employee_name": str(row[10] or ""),
    }


def list_employee_salary_settings() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ess.id, ess.employee_code, ess.base_salary, ess.allowance, ess.default_penalty,
                       ess.default_advance, ess.salary_config_id, ess.is_active, ess.created_at, ess.updated_at,
                       e.employee_name
                FROM employee_salary_settings ess
                JOIN employees e ON e.employee_code = ess.employee_code
                ORDER BY ess.employee_code ASC
                """
            )
            rows = cur.fetchall()
    return [_row_emp_setting(r) for r in rows]


def get_employee_salary_setting(employee_code: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ess.id, ess.employee_code, ess.base_salary, ess.allowance, ess.default_penalty,
                       ess.default_advance, ess.salary_config_id, ess.is_active, ess.created_at, ess.updated_at,
                       e.employee_name
                FROM employee_salary_settings ess
                JOIN employees e ON e.employee_code = ess.employee_code
                WHERE ess.employee_code = %s
                """,
                (employee_code.strip(),),
            )
            row = cur.fetchone()
    return _row_emp_setting(row) if row else None


def upsert_employee_salary_setting(data: Dict[str, Any], changed_by: str) -> int:
    code = str(data["employee_code"]).strip()
    old = get_employee_salary_setting(code)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO employee_salary_settings (
                    employee_code, base_salary, allowance, default_penalty, default_advance,
                    salary_config_id, is_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (employee_code) DO UPDATE SET
                    base_salary = EXCLUDED.base_salary,
                    allowance = EXCLUDED.allowance,
                    default_penalty = EXCLUDED.default_penalty,
                    default_advance = EXCLUDED.default_advance,
                    salary_config_id = EXCLUDED.salary_config_id,
                    is_active = EXCLUDED.is_active,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    code,
                    _f(data.get("base_salary")),
                    _f(data.get("allowance")),
                    _f(data.get("default_penalty")),
                    _f(data.get("default_advance")),
                    int(data["salary_config_id"]) if data.get("salary_config_id") not in (None, "",) else None,
                    bool(data.get("is_active", True)),
                ),
            )
            sid = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT ess.id, ess.employee_code, ess.base_salary, ess.allowance, ess.default_penalty,
                       ess.default_advance, ess.salary_config_id, ess.is_active, ess.created_at, ess.updated_at,
                       e.employee_name
                FROM employee_salary_settings ess
                JOIN employees e ON e.employee_code = ess.employee_code
                WHERE ess.employee_code = %s
                """,
                (code,),
            )
            nr = cur.fetchone()
            new = _row_emp_setting(nr) if nr else (old or {})
            action = "update" if old else "create"
            _audit(cur, "employee_salary_setting", sid, action, old, new, changed_by)
            return sid


def list_salary_configs_for_select() -> List[Dict[str, Any]]:
    return [{"id": c["id"], "label": f'{c["config_code"]} — {c["config_name"]}'} for c in list_salary_configs()]


def _parse_jsonb(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return val


def _row_period_registry(row: tuple) -> Dict[str, Any]:
    return {
        "period_key": str(row[0] or ""),
        "status": str(row[1] or "draft"),
        "finalized_at": row[2],
        "finalized_by": None if row[3] is None else str(row[3]),
        "calc_snapshot_json": _parse_jsonb(row[4]),
        "notes": None if row[5] is None else str(row[5]),
        "created_at": row[6],
        "updated_at": row[7],
    }


def _row_employee_period_result(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "period_key": str(row[1] or ""),
        "employee_code": str(row[2] or ""),
        "employee_name_snapshot": str(row[3] or ""),
        "branch_code": None if row[4] is None else str(row[4]),
        "total_revenue_gross": _f(row[5]),
        "total_revenue_net": _f(row[6]),
        "total_shipping_fee": _f(row[7]),
        "total_ads_cost": _f(row[8]),
        "total_commission_amount": _f(row[9]),
        "base_salary": _f(row[10]),
        "allowance": _f(row[11]),
        "penalty": _f(row[12]),
        "advance_amount": _f(row[13]),
        "final_salary": _f(row[14]),
        "stop_run_flag_count": int(row[15] or 0),
        "calc_snapshot_json": _parse_jsonb(row[16]),
        "status": str(row[17] or "draft"),
        "finalized_at": row[18],
        "created_at": row[19],
        "updated_at": row[20],
    }


def period_has_any_finalized_employee_result(period_key: str) -> bool:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM salary_period_employee_results
                WHERE period_key = %s AND LOWER(TRIM(status)) = 'finalized'
                LIMIT 1
                """,
                (pk,),
            )
            return cur.fetchone() is not None


def get_period_registry(period_key: str) -> Optional[Dict[str, Any]]:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT period_key, status, finalized_at, finalized_by, calc_snapshot_json,
                       notes, created_at, updated_at
                FROM salary_periods WHERE period_key = %s
                """,
                (pk,),
            )
            row = cur.fetchone()
    return _row_period_registry(row) if row else None


def is_period_locked(period_key: str) -> bool:
    if period_has_any_finalized_employee_result(period_key):
        return True
    meta = get_period_registry(period_key)
    if meta and str(meta.get("status", "")).lower().strip() == "finalized":
        return True
    return False


def list_distinct_period_keys() -> List[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT period_key FROM (
                    SELECT period_key FROM salary_periods
                    UNION
                    SELECT period_key FROM salary_period_employee_results
                    UNION
                    SELECT period_key FROM salary_period_product_lines
                    UNION
                    SELECT period_key FROM salary_period_input_lines
                ) t
                WHERE period_key IS NOT NULL AND TRIM(period_key) <> ''
                ORDER BY period_key DESC
                """
            )
            return [str(r[0]) for r in cur.fetchall()]


def upsert_period_registry_draft(period_key: str) -> None:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_periods (period_key, status)
                VALUES (%s, 'draft')
                ON CONFLICT (period_key) DO UPDATE SET
                    updated_at = NOW(),
                    status = CASE
                        WHEN salary_periods.status = 'finalized' THEN salary_periods.status
                        ELSE 'draft'
                    END
                """,
                (pk,),
            )


def _row_period_input_line(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "period_key": str(row[1] or ""),
        "employee_code": str(row[2] or ""),
        "product_code": str(row[3] or ""),
        "revenue_gross": _f(row[4]),
        "quantity": _f(row[5]),
        "orders_count": int(row[6] or 0),
        "ads_cost": _f(row[7]),
        "shipping_fee": _f(row[8]),
        "returned_revenue_gross": _f(row[9]),
        "returned_quantity": _f(row[10]),
        "returned_orders_count": int(row[11] or 0),
        "shipping_fee_return_delta": _f(row[12]),
        "created_at": row[13],
        "updated_at": row[14],
    }


def upsert_period_input_line(
    period_key: str,
    employee_code: str,
    product_code: str,
    revenue_gross: float,
    quantity: float,
    orders_count: int,
    ads_cost: float,
    shipping_fee: float,
    *,
    returned_revenue_gross: float = 0,
    returned_quantity: float = 0,
    returned_orders_count: int = 0,
    shipping_fee_return_delta: float = 0,
) -> None:
    pk = period_key.strip()
    ec = employee_code.strip()
    pc = product_code.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_period_input_lines (
                    period_key, employee_code, product_code,
                    revenue_gross, quantity, orders_count, ads_cost, shipping_fee,
                    returned_revenue_gross, returned_quantity, returned_orders_count,
                    shipping_fee_return_delta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (period_key, employee_code, product_code) DO UPDATE SET
                    revenue_gross = EXCLUDED.revenue_gross,
                    quantity = EXCLUDED.quantity,
                    orders_count = EXCLUDED.orders_count,
                    ads_cost = EXCLUDED.ads_cost,
                    shipping_fee = EXCLUDED.shipping_fee,
                    returned_revenue_gross = EXCLUDED.returned_revenue_gross,
                    returned_quantity = EXCLUDED.returned_quantity,
                    returned_orders_count = EXCLUDED.returned_orders_count,
                    shipping_fee_return_delta = EXCLUDED.shipping_fee_return_delta,
                    updated_at = NOW()
                """,
                (
                    pk,
                    ec,
                    pc,
                    _f(revenue_gross),
                    _f(quantity),
                    int(orders_count or 1),
                    _f(ads_cost),
                    _f(shipping_fee),
                    _f(returned_revenue_gross),
                    _f(returned_quantity),
                    int(returned_orders_count or 0),
                    _f(shipping_fee_return_delta),
                ),
            )


def list_period_input_lines(period_key: str) -> List[Dict[str, Any]]:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, period_key, employee_code, product_code,
                       revenue_gross, quantity, orders_count, ads_cost, shipping_fee,
                       returned_revenue_gross, returned_quantity, returned_orders_count,
                       shipping_fee_return_delta,
                       created_at, updated_at
                FROM salary_period_input_lines
                WHERE period_key = %s
                ORDER BY employee_code ASC, product_code ASC
                """,
                (pk,),
            )
            rows = cur.fetchall()
    return [_row_period_input_line(r) for r in rows]


def delete_period_input_lines(period_key: str) -> None:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM salary_period_input_lines WHERE period_key = %s", (pk,))


def delete_period_input_lines_for_employee(period_key: str, employee_code: str) -> None:
    pk = period_key.strip()
    ec = employee_code.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM salary_period_input_lines WHERE period_key = %s AND employee_code = %s",
                (pk, ec),
            )


def delete_period_input_line(period_key: str, employee_code: str, product_code: str) -> None:
    """Xóa một dòng input (NV + SP) trong kỳ — dùng lớp review kế toán."""
    pk = period_key.strip()
    ec = employee_code.strip()
    pc = product_code.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM salary_period_input_lines
                WHERE period_key = %s AND employee_code = %s AND product_code = %s
                """,
                (pk, ec, pc),
            )


def _row_period_product_line(row: tuple) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "period_key": str(row[1] or ""),
        "employee_code": str(row[2] or ""),
        "employee_name_snapshot": str(row[3] or ""),
        "branch_code": None if row[4] is None else str(row[4]),
        "product_code": str(row[5] or ""),
        "product_name_snapshot": str(row[6] or ""),
        "revenue_gross": _f(row[7]),
        "quantity": _f(row[8]),
        "orders_count": int(row[9] or 0),
        "ads_cost": _f(row[10]),
        "shipping_fee": _f(row[11]),
        "vat_rate": _f(row[12]),
        "revenue_net": _f(row[13]),
        "revenue_for_commission": _f(row[14]),
        "cpqc_standard_per_order": _f(row[15]),
        "ads_per_order": _f(row[16]),
        "cpqc_percent": _f(row[17]),
        "commission_percent": _f(row[18]),
        "commission_amount": _f(row[19]),
        "import_price_snapshot": _f(row[20]),
        "stop_run": bool(row[21]),
        "calc_note": None if row[22] is None else str(row[22]),
        "salary_config_id": None if row[23] is None else int(row[23]),
        "calc_snapshot_json": _parse_jsonb(row[24]),
        "created_at": row[25],
        "updated_at": row[26],
    }


def upsert_salary_period_product_line(row: Dict[str, Any]) -> int:
    """Ghi một dòng salary_period_product_lines (theo UNIQUE period_key, employee_code, product_code)."""
    pk = str(row["period_key"]).strip()
    if is_period_locked(pk):
        raise ValueError("Kỳ đã chốt, không được cập nhật.")
    snap = row.get("calc_snapshot_json")
    snap_js = json.dumps(snap or {}, ensure_ascii=False, default=str) if snap is not None else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_period_product_lines (
                    period_key, employee_code, employee_name_snapshot, branch_code,
                    product_code, product_name_snapshot,
                    revenue_gross, quantity, orders_count, ads_cost, shipping_fee,
                    vat_rate, revenue_net, revenue_for_commission,
                    cpqc_standard_per_order, ads_per_order, cpqc_percent,
                    commission_percent, commission_amount, import_price_snapshot,
                    stop_run, calc_note, salary_config_id, calc_snapshot_json
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (period_key, employee_code, product_code) DO UPDATE SET
                    employee_name_snapshot = EXCLUDED.employee_name_snapshot,
                    branch_code = EXCLUDED.branch_code,
                    product_name_snapshot = EXCLUDED.product_name_snapshot,
                    revenue_gross = EXCLUDED.revenue_gross,
                    quantity = EXCLUDED.quantity,
                    orders_count = EXCLUDED.orders_count,
                    ads_cost = EXCLUDED.ads_cost,
                    shipping_fee = EXCLUDED.shipping_fee,
                    vat_rate = EXCLUDED.vat_rate,
                    revenue_net = EXCLUDED.revenue_net,
                    revenue_for_commission = EXCLUDED.revenue_for_commission,
                    cpqc_standard_per_order = EXCLUDED.cpqc_standard_per_order,
                    ads_per_order = EXCLUDED.ads_per_order,
                    cpqc_percent = EXCLUDED.cpqc_percent,
                    commission_percent = EXCLUDED.commission_percent,
                    commission_amount = EXCLUDED.commission_amount,
                    import_price_snapshot = EXCLUDED.import_price_snapshot,
                    stop_run = EXCLUDED.stop_run,
                    calc_note = EXCLUDED.calc_note,
                    salary_config_id = EXCLUDED.salary_config_id,
                    calc_snapshot_json = EXCLUDED.calc_snapshot_json,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    pk,
                    str(row["employee_code"]).strip(),
                    str(row["employee_name_snapshot"] or ""),
                    (str(row["branch_code"]).strip() or None) if row.get("branch_code") else None,
                    str(row["product_code"]).strip(),
                    str(row["product_name_snapshot"] or ""),
                    _f(row.get("revenue_gross")),
                    _f(row.get("quantity")),
                    int(row.get("orders_count") or 0),
                    _f(row.get("ads_cost")),
                    _f(row.get("shipping_fee")),
                    _f(row.get("vat_rate")),
                    _f(row.get("revenue_net")),
                    _f(row.get("revenue_for_commission")),
                    _f(row.get("cpqc_standard_per_order")),
                    _f(row.get("ads_per_order")),
                    _f(row.get("cpqc_percent")),
                    _f(row.get("commission_percent")),
                    _f(row.get("commission_amount")),
                    _f(row.get("import_price_snapshot")),
                    bool(row.get("stop_run")),
                    (str(row["calc_note"]).strip() or None) if row.get("calc_note") else None,
                    int(row["salary_config_id"]) if row.get("salary_config_id") not in (None, "") else None,
                    snap_js if snap_js is not None else "{}",
                ),
            )
            return int(cur.fetchone()[0])


def list_salary_period_product_lines(
    period_key: str,
    employee_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if employee_code and str(employee_code).strip():
                cur.execute(
                    """
                    SELECT id, period_key, employee_code, employee_name_snapshot, branch_code,
                           product_code, product_name_snapshot,
                           revenue_gross, quantity, orders_count, ads_cost, shipping_fee,
                           vat_rate, revenue_net, revenue_for_commission,
                           cpqc_standard_per_order, ads_per_order, cpqc_percent,
                           commission_percent, commission_amount, import_price_snapshot,
                           stop_run, calc_note, salary_config_id, calc_snapshot_json,
                           created_at, updated_at
                    FROM salary_period_product_lines
                    WHERE period_key = %s AND employee_code = %s
                    ORDER BY product_code ASC
                    """,
                    (pk, str(employee_code).strip()),
                )
            else:
                cur.execute(
                    """
                    SELECT id, period_key, employee_code, employee_name_snapshot, branch_code,
                           product_code, product_name_snapshot,
                           revenue_gross, quantity, orders_count, ads_cost, shipping_fee,
                           vat_rate, revenue_net, revenue_for_commission,
                           cpqc_standard_per_order, ads_per_order, cpqc_percent,
                           commission_percent, commission_amount, import_price_snapshot,
                           stop_run, calc_note, salary_config_id, calc_snapshot_json,
                           created_at, updated_at
                    FROM salary_period_product_lines
                    WHERE period_key = %s
                    ORDER BY employee_code ASC, product_code ASC
                    """,
                    (pk,),
                )
            rows = cur.fetchall()
    return [_row_period_product_line(r) for r in rows]


def delete_period_payroll_data(period_key: str) -> None:
    """Xóa toàn bộ dòng kỳ (product_lines + employee_results). Chỉ gọi khi chắc chắn kỳ chưa chốt."""
    pk = period_key.strip()
    if is_period_locked(pk):
        raise ValueError("Kỳ đã chốt, không được xóa/tính lại dữ liệu.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM salary_period_product_lines WHERE period_key = %s", (pk,))
            cur.execute("DELETE FROM salary_period_employee_results WHERE period_key = %s", (pk,))
            cur.execute(
                """
                INSERT INTO salary_periods (period_key, status)
                VALUES (%s, 'draft')
                ON CONFLICT (period_key) DO UPDATE SET
                    updated_at = NOW(),
                    status = CASE
                        WHEN salary_periods.status = 'finalized' THEN salary_periods.status
                        ELSE 'draft'
                    END,
                    finalized_at = CASE
                        WHEN salary_periods.status = 'finalized' THEN salary_periods.finalized_at
                        ELSE NULL
                    END,
                    finalized_by = CASE
                        WHEN salary_periods.status = 'finalized' THEN salary_periods.finalized_by
                        ELSE NULL
                    END
                """,
                (pk,),
            )


def upsert_employee_period_result_draft(
    period_key: str,
    employee_code: str,
    employee_name_snapshot: str,
    branch_code: Optional[str],
    totals: Dict[str, Any],
    calc_snapshot: Optional[Dict[str, Any]] = None,
) -> int:
    pk = period_key.strip()
    code = employee_code.strip()
    if is_period_locked(pk):
        raise ValueError("Kỳ đã chốt, không được cập nhật.")
    snap_json = json.dumps(calc_snapshot, ensure_ascii=False) if calc_snapshot else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_period_employee_results (
                    period_key, employee_code, employee_name_snapshot, branch_code,
                    total_revenue_gross, total_revenue_net, total_shipping_fee, total_ads_cost,
                    total_commission_amount, base_salary, allowance, penalty, advance_amount,
                    final_salary, stop_run_flag_count, calc_snapshot_json, status, finalized_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, 'draft', NULL
                )
                ON CONFLICT (period_key, employee_code) DO UPDATE SET
                    employee_name_snapshot = EXCLUDED.employee_name_snapshot,
                    branch_code = EXCLUDED.branch_code,
                    total_revenue_gross = EXCLUDED.total_revenue_gross,
                    total_revenue_net = EXCLUDED.total_revenue_net,
                    total_shipping_fee = EXCLUDED.total_shipping_fee,
                    total_ads_cost = EXCLUDED.total_ads_cost,
                    total_commission_amount = EXCLUDED.total_commission_amount,
                    base_salary = EXCLUDED.base_salary,
                    allowance = EXCLUDED.allowance,
                    penalty = EXCLUDED.penalty,
                    advance_amount = EXCLUDED.advance_amount,
                    final_salary = EXCLUDED.final_salary,
                    stop_run_flag_count = EXCLUDED.stop_run_flag_count,
                    calc_snapshot_json = COALESCE(EXCLUDED.calc_snapshot_json, salary_period_employee_results.calc_snapshot_json),
                    updated_at = NOW()
                WHERE LOWER(TRIM(COALESCE(salary_period_employee_results.status, ''))) = 'draft'
                RETURNING id
                """,
                (
                    pk,
                    code,
                    employee_name_snapshot,
                    branch_code,
                    _f(totals.get("total_revenue_gross")),
                    _f(totals.get("total_revenue_net")),
                    _f(totals.get("total_shipping_fee")),
                    _f(totals.get("total_ads_cost")),
                    _f(totals.get("total_commission_amount")),
                    _f(totals.get("base_salary")),
                    _f(totals.get("allowance")),
                    _f(totals.get("penalty")),
                    _f(totals.get("advance_amount")),
                    _f(totals.get("final_salary")),
                    int(totals.get("stop_run_flag_count", 0) or 0),
                    snap_json,
                ),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id FROM salary_period_employee_results WHERE period_key = %s AND employee_code = %s",
                    (pk, code),
                )
                r2 = cur.fetchone()
                if not r2:
                    raise ValueError("Không thể ghi employee_result (kỳ đã chốt?).")
                return int(r2[0])
            return int(row[0])


def list_employee_results_for_period(period_key: str) -> List[Dict[str, Any]]:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, period_key, employee_code, employee_name_snapshot, branch_code,
                       total_revenue_gross, total_revenue_net, total_shipping_fee, total_ads_cost,
                       total_commission_amount, base_salary, allowance, penalty, advance_amount,
                       final_salary, stop_run_flag_count, calc_snapshot_json, status, finalized_at,
                       created_at, updated_at
                FROM salary_period_employee_results
                WHERE period_key = %s
                ORDER BY employee_code ASC
                """,
                (pk,),
            )
            rows = cur.fetchall()
    return [_row_employee_period_result(r) for r in rows]


def finalize_employee_period_row(
    result_id: int,
    calc_snapshot_json: Dict[str, Any],
    finalized_by: str,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE salary_period_employee_results SET
                    status = 'finalized',
                    finalized_at = NOW(),
                    calc_snapshot_json = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                  AND (LOWER(TRIM(status)) = 'draft' OR status IS NULL)
                """,
                (
                    json.dumps(calc_snapshot_json or {}, ensure_ascii=False, default=str),
                    int(result_id),
                ),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT id, status FROM salary_period_employee_results WHERE id = %s",
                    (int(result_id),),
                )
                ex = cur.fetchone()
                if not ex:
                    raise ValueError("Không tìm thấy bản ghi lương.")
                if str(ex[1] or "").lower().strip() == "finalized":
                    return
                raise ValueError("Không thể chốt bản ghi này.")


def mark_period_registry_finalized(
    period_key: str,
    finalized_by: str,
    calc_snapshot_json: Dict[str, Any],
) -> None:
    pk = period_key.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salary_periods (
                    period_key, status, finalized_at, finalized_by, calc_snapshot_json
                ) VALUES (%s, 'finalized', NOW(), %s, %s::jsonb)
                ON CONFLICT (period_key) DO UPDATE SET
                    status = 'finalized',
                    finalized_at = NOW(),
                    finalized_by = EXCLUDED.finalized_by,
                    calc_snapshot_json = EXCLUDED.calc_snapshot_json,
                    updated_at = NOW()
                """,
                (
                    pk,
                    finalized_by[:100] if finalized_by else "system",
                    json.dumps(calc_snapshot_json or {}, ensure_ascii=False, default=str),
                ),
            )


def run_finalize_period_transaction(
    period_key: str,
    finalized_by: str,
    row_snapshots: List[Tuple[int, Dict[str, Any]]],
    global_snapshot: Dict[str, Any],
) -> None:
    """row_snapshots: list of (result_id, snapshot_dict). Một transaction duy nhất."""
    pk = period_key.strip()
    who = finalized_by[:100] if finalized_by else "system"
    gs = json.dumps(global_snapshot or {}, ensure_ascii=False, default=str)
    with get_conn() as conn:
        with conn.cursor() as cur:
            for rid, snap in row_snapshots:
                cur.execute(
                    """
                    UPDATE salary_period_employee_results SET
                        status = 'finalized',
                        finalized_at = NOW(),
                        calc_snapshot_json = %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                      AND LOWER(TRIM(COALESCE(status, ''))) = 'draft'
                    """,
                    (json.dumps(snap or {}, ensure_ascii=False, default=str), int(rid)),
                )
            cur.execute(
                """
                INSERT INTO salary_periods (
                    period_key, status, finalized_at, finalized_by, calc_snapshot_json
                ) VALUES (%s, 'finalized', NOW(), %s, %s::jsonb)
                ON CONFLICT (period_key) DO UPDATE SET
                    status = 'finalized',
                    finalized_at = NOW(),
                    finalized_by = EXCLUDED.finalized_by,
                    calc_snapshot_json = EXCLUDED.calc_snapshot_json,
                    updated_at = NOW()
                """,
                (pk, who, gs),
            )


def list_period_summaries() -> List[Dict[str, Any]]:
    keys = list_distinct_period_keys()
    out: List[Dict[str, Any]] = []
    for pk in keys:
        reg = get_period_registry(pk)
        rows = list_employee_results_for_period(pk)
        n_final = sum(1 for r in rows if str(r.get("status", "")).lower().strip() == "finalized")
        locked = is_period_locked(pk)
        out.append(
            {
                "period_key": pk,
                "registry_status": (reg or {}).get("status", "draft"),
                "finalized_at": (reg or {}).get("finalized_at"),
                "finalized_by": (reg or {}).get("finalized_by"),
                "employee_rows": len(rows),
                "finalized_rows": n_final,
                "locked": locked,
            }
        )
    return out
