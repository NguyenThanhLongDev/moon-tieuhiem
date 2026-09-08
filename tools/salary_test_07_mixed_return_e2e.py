#!/usr/bin/env python3
"""
BÀI TEST 07 — 1 NV, 2 SP cùng kỳ: 1 có hoàn (KIM_A), 1 không hoàn (KEO_B).

Chạy: set -a && . deploy/pos-dashboard.env.example && set +a && .venv/bin/python tools/salary_test_07_mixed_return_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from db import get_conn  # noqa: E402
from modules.salary import period_engine  # noqa: E402
from modules.salary import repositories as repo  # noqa: E402

PERIOD = "2026-02"
EMP = "NV005"


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


def _line_by_code(lines: List[Dict[str, Any]], code: str) -> Optional[Dict[str, Any]]:
    for x in lines:
        if x.get("product_code") == code:
            return x
    return None


def _product_report(pl: Dict[str, Any]) -> Dict[str, Any]:
    snap = pl.get("calc_snapshot_json") or {}
    return {
        "product_code": pl.get("product_code"),
        "before_return": snap.get("before_return"),
        "return_delta": snap.get("return_delta"),
        "after_return": snap.get("after_return"),
        "revenue_net": pl.get("revenue_net"),
        "revenue_for_commission": pl.get("revenue_for_commission"),
        "ads_per_order": pl.get("ads_per_order"),
        "cpqc_percent": pl.get("cpqc_percent"),
        "commission_percent": pl.get("commission_percent"),
        "commission_amount": pl.get("commission_amount"),
    }


def main() -> int:
    if not os.getenv("DATABASE_URL", "").strip():
        print("FAIL: DATABASE_URL chưa set.")
        return 2

    repo.ensure_salary_schema()
    cfg = repo.get_salary_config_by_code("default_main")
    if not cfg:
        print("FAIL: Thiếu default_main config.")
        return 2

    if not repo.get_product_salary_config_by_code("KIM_A") or not repo.get_product_salary_config_by_code("KEO_B"):
        print("FAIL: Thiếu KIM_A hoặc KEO_B trong product_salary_configs.")
        return 2

    _unlock_period_draft(PERIOD)

    if not repo.get_employee(EMP):
        repo.create_employee(
            {
                "employee_code": EMP,
                "employee_name": "NV005 Test 07",
                "branch_code": "main",
                "is_active": True,
            },
            changed_by="salary_test_07",
        )

    repo.upsert_employee_salary_setting(
        {
            "employee_code": EMP,
            "base_salary": 5_200_000,
            "allowance": 200_000,
            "default_penalty": 100_000,
            "default_advance": 300_000,
            "salary_config_id": int(cfg["id"]),
            "is_active": True,
        },
        changed_by="salary_test_07",
    )

    keo = repo.get_product_salary_config_by_code("KEO_B")
    if keo:
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
            changed_by="salary_test_07",
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
    repo.upsert_period_input_line(
        PERIOD,
        EMP,
        "KEO_B",
        revenue_gross=1_100_000,
        quantity=10,
        orders_count=10,
        ads_cost=200_000,
        shipping_fee=100_000,
        returned_revenue_gross=0,
        returned_quantity=0,
        returned_orders_count=0,
        shipping_fee_return_delta=0,
    )

    period_engine.recalculate_period(PERIOD)
    lines = sorted(
        repo.list_salary_period_product_lines(PERIOD, EMP),
        key=lambda x: x["product_code"],
    )
    emp_rows = [r for r in repo.list_employee_results_for_period(PERIOD) if r["employee_code"] == EMP]
    emp_row = emp_rows[0] if emp_rows else None

    kim = _line_by_code(lines, "KIM_A")
    keo_pl = _line_by_code(lines, "KEO_B")

    # Kỳ vọng số học
    exp_kim_comm = 47_250.0
    exp_keo_comm = 27_000.0
    exp_total_comm = 74_250.0
    exp_final = 5_074_250.0  # 5200000+200000+74250-100000-300000

    reports = [_product_report(x) for x in lines]

    # KIM_A: có hoàn
    kim_snap = (kim or {}).get("calc_snapshot_json") or {}
    kim_after = kim_snap.get("after_return") or {}
    kim_delta = kim_snap.get("return_delta") or {}

    # KEO_B: không hoàn — không được dính delta của KIM_A
    keo_snap = (keo_pl or {}).get("calc_snapshot_json") or {}
    keo_after = keo_snap.get("after_return") or {}
    keo_delta = keo_snap.get("return_delta") or {}
    keo_before = keo_snap.get("before_return") or {}

    ok_two_lines = len(lines) == 2
    ok_kim_comm = kim and abs(float(kim.get("commission_amount", 0)) - exp_kim_comm) < 0.02
    ok_keo_comm = keo_pl and abs(float(keo_pl.get("commission_amount", 0)) - exp_keo_comm) < 0.02
    ok_total_comm = emp_row and abs(float(emp_row["total_commission_amount"]) - exp_total_comm) < 0.02
    ok_final = emp_row and abs(float(emp_row["final_salary"]) - exp_final) < 0.02

    ok_kim_after_gross = abs(float(kim_after.get("revenue_gross", 0)) - 1_650_000) < 0.02
    ok_keo_no_return_delta = (
        float(keo_delta.get("returned_revenue_gross", -1)) == 0
        and float(keo_delta.get("returned_quantity", -1)) == 0
        and int(keo_delta.get("returned_orders_count", -1)) == 0
        and float(keo_delta.get("shipping_fee_return_delta", -1)) == 0
    )
    ok_keo_after_equals_before = (
        abs(float(keo_after.get("revenue_gross", 0)) - 1_100_000) < 0.02
        and abs(float(keo_after.get("shipping_fee", 0)) - 100_000) < 0.02
        and int(keo_after.get("orders_count", 0)) == 10
    )
    # Nếu gộp hoàn KIM sang KEO: gross KEO sẽ lệch hoặc delta KEO khác 0
    ok_no_cross = ok_keo_no_return_delta and ok_keo_after_equals_before

    overall = all(
        [
            ok_two_lines,
            ok_kim_comm,
            ok_keo_comm,
            ok_total_comm,
            ok_final,
            ok_kim_after_gross,
            ok_no_cross,
        ]
    )

    for r in reports:
        print(f"=== {r['product_code']} ===")
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    out: Dict[str, Any] = {
        "test": "BÀI TEST 07 - MIXED RETURN BY PRODUCT",
        "status": "PASS" if overall else "FAIL",
        "period_key": PERIOD,
        "employee_code": EMP,
        "products": reports,
        "total_commission_amount": float(emp_row["total_commission_amount"]) if emp_row else None,
        "expected_total_commission_amount": exp_total_comm,
        "final_salary": float(emp_row["final_salary"]) if emp_row else None,
        "expected_final_salary": exp_final,
        "assertions": {
            "two_product_lines": ok_two_lines,
            "KIM_A_commission_47250": ok_kim_comm,
            "KEO_B_commission_27000": ok_keo_comm,
            "total_commission_74250": ok_total_comm,
            "final_salary_5074250": ok_final,
            "KIM_A_after_gross_1650000": ok_kim_after_gross,
            "KEO_B_return_delta_all_zero": ok_keo_no_return_delta,
            "KEO_B_after_equals_no_return_case": ok_keo_after_equals_before,
            "no_return_leak_KIM_to_KEO": ok_no_cross,
        },
    }
    if not overall:
        out["fail_reason"] = (
            "FAIL: gộp hoàn sai SKU, thiếu dòng, hoặc tổng HH / final_salary lệch."
            if not ok_no_cross
            else "Một assertion khác false — xem chi tiết."
        )

    print("--- JSON ---")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
