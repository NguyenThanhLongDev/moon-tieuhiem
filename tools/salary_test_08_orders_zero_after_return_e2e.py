#!/usr/bin/env python3
"""
BÀI TEST 08 — Hoàn hết: orders_count sau hoàn = 0, không chia 0, HH = 0.

Chạy: set -a && . deploy/pos-dashboard.env.example && set +a && .venv/bin/python tools/salary_test_08_orders_zero_after_return_e2e.py
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
EMP = "NV006"


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
    if not cfg or not repo.get_product_salary_config_by_code("KIM_A"):
        print("FAIL: Thiếu default_main hoặc KIM_A.")
        return 2

    _unlock_period_draft(PERIOD)

    if not repo.get_employee(EMP):
        repo.create_employee(
            {
                "employee_code": EMP,
                "employee_name": "NV006 Test 08",
                "branch_code": "main",
                "is_active": True,
            },
            changed_by="salary_test_08",
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
        changed_by="salary_test_08",
    )

    repo.delete_period_input_lines_for_employee(PERIOD, EMP)
    repo.upsert_period_input_line(
        PERIOD,
        EMP,
        "KIM_A",
        revenue_gross=550_000,
        quantity=5,
        orders_count=5,
        ads_cost=100_000,
        shipping_fee=50_000,
        returned_revenue_gross=550_000,
        returned_quantity=5,
        returned_orders_count=5,
        shipping_fee_return_delta=50_000,
    )

    try:
        period_engine.recalculate_period(PERIOD)
    except ZeroDivisionError:
        print(json.dumps({"test": "BÀI TEST 08", "status": "FAIL", "reason": "ZeroDivisionError"}, indent=2))
        return 1

    lines = repo.list_salary_period_product_lines(PERIOD, EMP)
    pl = lines[0] if lines else {}
    emp_rows = [r for r in repo.list_employee_results_for_period(PERIOD) if r["employee_code"] == EMP]
    emp_row = emp_rows[0] if emp_rows else None
    snap = pl.get("calc_snapshot_json") or {}

    before_return = snap.get("before_return") or {}
    return_delta = snap.get("return_delta") or {}
    after_return = snap.get("after_return") or {}
    calc_note = pl.get("calc_note") or ""

    report = {
        "before_return": before_return,
        "return_delta": return_delta,
        "after_return": after_return,
        "revenue_net": pl.get("revenue_net"),
        "revenue_for_commission": pl.get("revenue_for_commission"),
        "ads_per_order": pl.get("ads_per_order"),
        "cpqc_percent": pl.get("cpqc_percent"),
        "commission_percent": pl.get("commission_percent"),
        "commission_amount": pl.get("commission_amount"),
        "calc_note": calc_note,
        "final_salary": float(emp_row["final_salary"]) if emp_row else None,
    }

    for k, v in report.items():
        print(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}")

    exp_after = {
        "revenue_gross": 0.0,
        "quantity": 0.0,
        "orders_count": 0,
        "shipping_fee": 0.0,
        "ads_cost": 100_000.0,
    }
    ok_after = all(
        abs(float(after_return.get(x, -1)) - float(exp_after[x])) < 0.02
        if x != "orders_count"
        else int(after_return.get("orders_count", -1)) == exp_after["orders_count"]
        for x in exp_after
    )
    _oc = pl.get("orders_count")
    ok_orders_col = _oc is not None and int(_oc) == 0
    ok_no_crash = True
    _camt = pl.get("commission_amount")
    ok_comm_zero = _camt is not None and abs(float(_camt)) < 0.01
    ok_final = emp_row and abs(float(emp_row["final_salary"]) - 5_200_000) < 0.02
    _apo = pl.get("ads_per_order")
    ok_ads_po_zero = _apo is not None and abs(float(_apo)) < 0.01
    _cpqc = pl.get("cpqc_percent")
    ok_cpqc_zero = _cpqc is not None and abs(float(_cpqc)) < 0.01
    _cpct = pl.get("commission_percent")
    ok_comm_pct_zero = _cpct is not None and abs(float(_cpct)) < 0.01
    ok_note = (
        "Hoàn hết đơn" in calc_note
        or "orders_count sau hoàn = 0" in calc_note
        or "không chia ads" in calc_note.lower()
        or "không tính ads_per_order" in calc_note
    )

    # Không được giả lãi bằng cách ép 1 đơn mà vẫn trả HH > 0
    no_fake_payout = ok_comm_zero and ok_comm_pct_zero

    overall = all(
        [
            len(lines) == 1,
            ok_after,
            ok_orders_col,
            ok_no_crash,
            ok_comm_zero,
            ok_final,
            ok_ads_po_zero,
            ok_cpqc_zero,
            ok_comm_pct_zero,
            ok_note,
            no_fake_payout,
        ]
    )

    out = {
        "test": "BÀI TEST 08 - RETURN FULL / ORDERS = 0",
        "status": "PASS" if overall else "FAIL",
        "period_key": PERIOD,
        "employee_code": EMP,
        "report": report,
        "expected_after_return": exp_after,
        "proof_no_fake_orders_divisor": {
            "orders_count_in_product_line": pl.get("orders_count"),
            "ads_per_order": pl.get("ads_per_order"),
            "commission_amount": pl.get("commission_amount"),
            "note": "orders_count=0 và ads_per_order=0 — không chia 100k/1; HH=0.",
        },
        "assertions": {
            "one_line": len(lines) == 1,
            "after_return_matches": ok_after,
            "orders_count_column_zero": ok_orders_col,
            "no_crash": ok_no_crash,
            "commission_amount_zero": ok_comm_zero,
            "final_salary_5200000": ok_final,
            "ads_per_order_zero_not_100000": ok_ads_po_zero,
            "cpqc_percent_zero": ok_cpqc_zero,
            "commission_percent_zero": ok_comm_pct_zero,
            "calc_note_warns": ok_note,
            "no_commission_despite_zero_orders": no_fake_payout,
        },
    }
    if not overall:
        out["fail_reason"] = "Xem assertions — có thể HH > 0, crash, hoặc ép orders làm sai CPQC."

    print("--- JSON ---")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
