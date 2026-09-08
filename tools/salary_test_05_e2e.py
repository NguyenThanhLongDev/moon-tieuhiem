#!/usr/bin/env python3
"""
BÀI TEST 05 — Ghi DB end-to-end: salary_period_input_lines → recalculate_period
→ salary_period_product_lines + salary_period_employee_results.

Cần DATABASE_URL (vd: source deploy/pos-dashboard.env.example).
Chạy: .venv/bin/python tools/salary_test_05_e2e.py
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from db import get_conn  # noqa: E402
from modules.salary import period_engine  # noqa: E402
from modules.salary import repositories as repo  # noqa: E402

PERIOD = "2026-02"
EMP = "NV003"


def _unlock_period_draft(pk: str) -> None:
    """Cho phép tính lại kỳ đang draft/finalized (chỉ dùng cho test/môi trường dev)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE salary_period_employee_results
                SET status = 'draft', finalized_at = NULL
                WHERE period_key = %s
                """,
                (pk,),
            )
            cur.execute(
                """
                UPDATE salary_periods
                SET status = 'draft', finalized_at = NULL, finalized_by = NULL
                WHERE period_key = %s
                """,
                (pk,),
            )


def main() -> int:
    if not os.getenv("DATABASE_URL", "").strip():
        print("FAIL: DATABASE_URL chưa set.")
        return 2

    repo.ensure_salary_schema()
    cfg = repo.get_salary_config_by_code("default_main")
    if not cfg:
        print("FAIL: Chưa có salary_configs default_main — chạy seed phase1 trước.")
        return 2

    _unlock_period_draft(PERIOD)

    if not repo.get_employee(EMP):
        repo.create_employee(
            {
                "employee_code": EMP,
                "employee_name": "NV003 Test 05",
                "branch_code": "main",
                "is_active": True,
            },
            changed_by="salary_test_05",
        )

    repo.upsert_employee_salary_setting(
        {
            "employee_code": EMP,
            "base_salary": 5_200_000,
            "allowance": 0,
            "default_penalty": 0,
            "default_advance": 0,
            "salary_config_id": int(cfg["id"]),
            "is_active": True,
        },
        changed_by="salary_test_05",
    )

    kim = repo.get_product_salary_config_by_code("KIM_A")
    keo = repo.get_product_salary_config_by_code("KEO_B")
    if not kim or not keo:
        print("FAIL: Thiếu product_salary_configs KIM_A hoặc KEO_B.")
        return 2

    repo.update_product_salary_config(
        int(keo["id"]),
        {
            "product_code": "KEO_B",
            "product_name": keo.get("product_name") or "Kéo B",
            "import_price": 35_000,
            "cpqc_standard_per_order": 25_000,
            "vat_rate": 0.10,
            "is_commission_enabled": True,
        },
        changed_by="salary_test_05",
    )

    repo.delete_period_input_lines(PERIOD)
    repo.upsert_period_input_line(
        PERIOD,
        EMP,
        "KIM_A",
        revenue_gross=880_000,
        quantity=2,
        orders_count=1,
        ads_cost=0,
        shipping_fee=80_000,
    )
    repo.upsert_period_input_line(
        PERIOD,
        EMP,
        "KEO_B",
        revenue_gross=1_100_000,
        quantity=1,
        orders_count=1,
        ads_cost=20_000,
        shipping_fee=100_000,
    )

    rec = period_engine.recalculate_period(PERIOD)
    lines = repo.list_salary_period_product_lines(PERIOD, EMP)
    all_lines_period = repo.list_salary_period_product_lines(PERIOD, None)
    emp_rows = [r for r in repo.list_employee_results_for_period(PERIOD) if r["employee_code"] == EMP]
    emp_row = emp_rows[0] if emp_rows else None

    by_code = {x["product_code"]: x for x in lines}
    kim_amt = by_code.get("KIM_A", {}).get("commission_amount")
    keo_amt = by_code.get("KEO_B", {}).get("commission_amount")

    pass_lines = len(lines) == 2
    pass_not_merged = len(all_lines_period) >= 2
    pass_kim = kim_amt == 25_200.0
    pass_keo = keo_amt == 27_000.0
    pass_comm = emp_row and float(emp_row["total_commission_amount"]) == 52_200.0
    pass_final = emp_row and float(emp_row["final_salary"]) == 5_252_200.0

    overall = all([pass_lines, pass_not_merged, pass_kim, pass_keo, pass_comm, pass_final])

    out = {
        "test": "BÀI TEST 05 - DB PERSISTENCE",
        "status": "PASS" if overall else "FAIL",
        "period_key": PERIOD,
        "employee_code": EMP,
        "recalculate_meta": {
            "input_lines": rec.get("input_lines"),
            "product_lines_written": rec.get("product_lines_written"),
            "skipped_inputs": rec.get("skipped_inputs"),
        },
        "salary_period_product_lines_count_nv003": len(lines),
        "salary_period_product_lines_count_period_total": len(all_lines_period),
        "product_lines_nv003": [
            {
                "product_code": x["product_code"],
                "revenue_net": x["revenue_net"],
                "revenue_for_commission": x["revenue_for_commission"],
                "ads_per_order": x["ads_per_order"],
                "cpqc_percent": x["cpqc_percent"],
                "commission_percent": x["commission_percent"],
                "commission_amount": x["commission_amount"],
            }
            for x in sorted(lines, key=lambda z: z["product_code"])
        ],
        "salary_period_employee_results_nv003": (
            {
                "total_commission_amount": emp_row["total_commission_amount"],
                "final_salary": emp_row["final_salary"],
                "total_revenue_gross": emp_row["total_revenue_gross"],
                "base_salary": emp_row["base_salary"],
            }
            if emp_row
            else None
        ),
        "assertions": {
            "two_product_lines_for_nv003": pass_lines,
            "not_single_merged_line_for_period": pass_not_merged,
            "KIM_A_commission_25200": pass_kim,
            "KEO_B_commission_27000": pass_keo,
            "total_commission_52200": pass_comm,
            "final_salary_5252200": pass_final,
        },
    }

    if not pass_lines:
        out["fail_reason"] = "Thiếu hoặc thừa dòng salary_period_product_lines cho NV003 (kỳ vọng 2)."
    elif len(all_lines_period) == 1:
        out["fail_reason"] = "FAIL: Cả kỳ chỉ còn 1 dòng product_line (gộp order-level)."
    elif not overall:
        out["fail_reason"] = "Số liệu DB không khớp kỳ vọng (xem assertions)."

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
