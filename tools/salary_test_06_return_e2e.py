#!/usr/bin/env python3
"""
BÀI TEST 06 — Hoàn hàng / hoàn một phần: input có returned_* → tính lương theo after_return.

Chạy: set -a && . deploy/pos-dashboard.env.example && set +a && .venv/bin/python tools/salary_test_06_return_e2e.py
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
EMP = "NV004"


def _unlock_period_draft(pk: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE salary_period_employee_results SET status = 'draft', finalized_at = NULL WHERE period_key = %s",
                (pk,),
            )
            cur.execute(
                "UPDATE salary_periods SET status = 'draft', finalized_at = NULL, finalized_by = NULL WHERE period_key = %s",
                (pk,),
            )


def main() -> int:
    if not os.getenv("DATABASE_URL", "").strip():
        print("FAIL: DATABASE_URL chưa set.")
        return 2

    repo.ensure_salary_schema()
    cfg = repo.get_salary_config_by_code("default_main")
    if not cfg:
        print("FAIL: Thiếu default_main config.")
        return 2

    kim = repo.get_product_salary_config_by_code("KIM_A")
    if not kim:
        print("FAIL: Thiếu product KIM_A.")
        return 2

    _unlock_period_draft(PERIOD)

    if not repo.get_employee(EMP):
        repo.create_employee(
            {
                "employee_code": EMP,
                "employee_name": "NV004 Test 06",
                "branch_code": "main",
                "is_active": True,
            },
            changed_by="salary_test_06",
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
        changed_by="salary_test_06",
    )

    repo.delete_period_input_lines_for_employee(PERIOD, EMP)
    repo.upsert_period_input_line(
        PERIOD,
        EMP,
        "KIM_A",
        revenue_gross=2_200_000,
        quantity=20,
        orders_count=20,
        ads_cost=300_000,
        shipping_fee=200_000,
        returned_revenue_gross=550_000,
        returned_quantity=5,
        returned_orders_count=5,
        shipping_fee_return_delta=50_000,
    )

    period_engine.recalculate_period(PERIOD)
    lines = repo.list_salary_period_product_lines(PERIOD, EMP)
    pl = lines[0] if lines else {}
    emp_rows = [r for r in repo.list_employee_results_for_period(PERIOD) if r["employee_code"] == EMP]
    emp_row = emp_rows[0] if emp_rows else None

    snap = (pl.get("calc_snapshot_json") or {}) if pl else {}
    before_return = snap.get("before_return") or {}
    return_delta = snap.get("return_delta") or {}
    after_return = snap.get("after_return") or {}

    # Sai nếu tính trên số trước hoàn (minh hoạ)
    gross_pre = 2_200_000
    net_pre = gross_pre / 1.1
    rfc_pre = net_pre - 200_000
    ads_po_pre = 300_000 / 20
    comm_wrong = round(rfc_pre * 0.035, 2)

    exp_after = {
        "revenue_gross": 1_650_000.0,
        "quantity": 15.0,
        "orders_count": 15,
        "shipping_fee": 150_000.0,
        "ads_cost": 300_000.0,
    }
    exp_net = 1_500_000.0
    exp_rfc = 1_350_000.0
    exp_ads_po = 20_000.0
    exp_cpqc = round((20_000.0 / 30_000.0) * 100, 2)
    exp_comm_pct = 0.035
    exp_comm_amt = 47_250.0
    exp_final = 5_247_250.0

    ok_line_count = len(lines) == 1
    ok_after = all(
        abs(float(after_return.get(k, 0)) - float(exp_after[k])) < 0.01 for k in exp_after
    )
    ok_gross_col = pl and abs(float(pl.get("revenue_gross", 0)) - exp_after["revenue_gross"]) < 0.01
    ok_net = pl and abs(float(pl.get("revenue_net", 0)) - exp_net) < 0.01
    ok_rfc = pl and abs(float(pl.get("revenue_for_commission", 0)) - exp_rfc) < 0.01
    ok_ads_po = pl and abs(float(pl.get("ads_per_order", 0)) - exp_ads_po) < 0.01
    ok_cpqc = pl and abs(float(pl.get("cpqc_percent", 0)) - exp_cpqc) < 0.05
    ok_comm = pl and abs(float(pl.get("commission_amount", 0)) - exp_comm_amt) < 0.01
    ok_emp = (
        emp_row
        and abs(float(emp_row["total_commission_amount"]) - exp_comm_amt) < 0.01
        and abs(float(emp_row["final_salary"]) - exp_final) < 0.01
    )
    ok_not_pre_return = pl and abs(float(pl.get("commission_amount", 0)) - comm_wrong) > 0.01

    overall = all(
        [
            ok_line_count,
            ok_after,
            ok_gross_col,
            ok_net,
            ok_rfc,
            ok_ads_po,
            ok_cpqc,
            ok_comm,
            ok_emp,
            ok_not_pre_return,
        ]
    )

    print("--- before_return (raw / trước hoàn) ---")
    print(json.dumps(before_return, ensure_ascii=False, indent=2))
    print("--- return_delta (phần hoàn) ---")
    print(json.dumps(return_delta, ensure_ascii=False, indent=2))
    print("--- after_return (dùng tính lương) ---")
    print(json.dumps(after_return, ensure_ascii=False, indent=2))
    print("--- payroll line (product_line) ---")
    if pl:
        print(
            json.dumps(
                {
                    "revenue_net": pl.get("revenue_net"),
                    "revenue_for_commission": pl.get("revenue_for_commission"),
                    "ads_per_order": pl.get("ads_per_order"),
                    "cpqc_percent": pl.get("cpqc_percent"),
                    "commission_percent": pl.get("commission_percent"),
                    "commission_amount": pl.get("commission_amount"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    out = {
        "test": "BÀI TEST 06 - RETURN / HOÀN HÀNG",
        "status": "PASS" if overall else "FAIL",
        "period_key": PERIOD,
        "employee_code": EMP,
        "before_return": before_return,
        "return_delta": return_delta,
        "after_return": after_return,
        "expected_after_return": exp_after,
        "expected_payroll_metrics": {
            "revenue_net": exp_net,
            "revenue_for_commission": exp_rfc,
            "ads_per_order": exp_ads_po,
            "cpqc_percent_approx": exp_cpqc,
            "commission_percent": exp_comm_pct,
            "commission_amount": exp_comm_amt,
            "final_salary": exp_final,
        },
        "wrong_if_used_pre_return": {
            "commission_amount_illustration": comm_wrong,
            "note": "Nếu hệ thống dùng gross/shipping/orders trước hoàn, HH sẽ gần số này (FAIL nghiệp vụ).",
        },
        "salary_period_employee_results": (
            {
                "total_commission_amount": emp_row["total_commission_amount"],
                "final_salary": emp_row["final_salary"],
            }
            if emp_row
            else None
        ),
        "assertions": {
            "one_product_line": ok_line_count,
            "after_return_matches_spec": ok_after,
            "revenue_gross_column_is_net_of_return": ok_gross_col,
            "revenue_net": ok_net,
            "revenue_for_commission": ok_rfc,
            "ads_per_order_uses_final_orders": ok_ads_po,
            "cpqc_percent": ok_cpqc,
            "commission_amount_47250": ok_comm,
            "final_salary_5247250": ok_emp,
            "commission_differs_from_pre_return_scenario": ok_not_pre_return,
        },
    }
    if not overall:
        out["fail_reason"] = "Một hoặc nhiều assertion false — có thể vẫn tính trước hoàn hoặc lệch làm tròn."

    print("--- JSON ---")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
