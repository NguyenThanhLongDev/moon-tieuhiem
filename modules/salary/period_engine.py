"""
Tính kỳ lương + chốt kỳ. Không sửa calculator.py (logic cũ theo ads/đơn giữ nguyên).
recalculate_period: đọc salary_period_input_lines → ghi salary_period_product_lines → cộng salary_period_employee_results.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import repositories as repo
from .cpqc_product import pick_commission_from_cpqc_tiers

_PERIOD_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def normalize_period_key(raw: str) -> str:
    s = (raw or "").strip()
    if not _PERIOD_KEY_RE.match(s):
        raise ValueError("period_key không hợp lệ (dùng dạng YYYY-MM, ví dụ 2026-02).")
    return s


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_global_config_snapshot() -> Dict[str, Any]:
    configs = repo.list_salary_configs()
    blocks: List[Dict[str, Any]] = []
    for c in configs:
        cid = int(c["id"])
        blocks.append({"salary_config": c, "cpqc_tiers": repo.list_cpqc_tiers(cid)})
    return {
        "snapshot_kind": "salary_period_global",
        "captured_at": _utc_iso(),
        "salary_configs_with_tiers": blocks,
        "product_salary_configs": repo.list_product_salary_configs(),
    }


def build_employee_finalize_snapshot(period_key: str, employee_code: str, actor: str) -> Dict[str, Any]:
    ess = repo.get_employee_salary_setting(employee_code)
    cfg = None
    tiers: List[Dict[str, Any]] = []
    if ess and ess.get("salary_config_id"):
        cfg = repo.get_salary_config(int(ess["salary_config_id"]))
        if cfg:
            tiers = repo.list_cpqc_tiers(int(cfg["id"]))
    return {
        "snapshot_kind": "salary_period_employee_finalize",
        "period_key": period_key,
        "employee_code": employee_code,
        "finalized_by": actor,
        "finalized_at": _utc_iso(),
        "employee_salary_setting": ess,
        "salary_config": cfg,
        "cpqc_tiers": tiers,
    }


def _money(x: Any) -> float:
    try:
        return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def _num(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def settle_input_line_for_payroll(inp: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], List[str]]:
    """
    Trước hoàn / delta hoàn / sau hoàn (dùng tính lương). ads_cost không trừ theo hoàn (theo spec test).
    """
    notes: List[str] = []
    before_return = {
        "revenue_gross": _money(_num(inp.get("revenue_gross"))),
        "quantity": _money(_num(inp.get("quantity"))),
        "orders_count": max(int(inp.get("orders_count") or 1), 1),
        "shipping_fee": _money(_num(inp.get("shipping_fee"))),
        "ads_cost": _money(_num(inp.get("ads_cost"))),
    }
    return_delta = {
        "returned_revenue_gross": _money(_num(inp.get("returned_revenue_gross"))),
        "returned_quantity": _money(_num(inp.get("returned_quantity"))),
        "returned_orders_count": int(inp.get("returned_orders_count") or 0),
        "shipping_fee_return_delta": _money(_num(inp.get("shipping_fee_return_delta"))),
    }
    gross_f = _money(before_return["revenue_gross"] - return_delta["returned_revenue_gross"])
    qty_f = _money(before_return["quantity"] - return_delta["returned_quantity"])
    oc_raw = int(before_return["orders_count"]) - int(return_delta["returned_orders_count"])
    if oc_raw < 0:
        notes.append("Cảnh báo: orders_count sau hoàn âm — ghi nhận 0 (kiểm tra dữ liệu nguồn)")
        oc_f = 0
    elif oc_raw == 0:
        notes.append(
            "Hoàn hết đơn: orders_count sau hoàn = 0 — không chia ads/đơn; CPQC/HH không áp dụng theo đơn."
        )
        oc_f = 0
    else:
        oc_f = oc_raw
    ship_f = _money(before_return["shipping_fee"] - return_delta["shipping_fee_return_delta"])
    if gross_f < 0 or qty_f < 0 or ship_f < 0:
        notes.append("Cảnh báo: revenue/quantity/shipping_net âm sau hoàn (kiểm tra dữ liệu nguồn)")
    after_return = {
        "revenue_gross": gross_f,
        "quantity": qty_f,
        "orders_count": oc_f,
        "shipping_fee": ship_f,
        "ads_cost": before_return["ads_cost"],
    }
    return before_return, return_delta, after_return, notes


def _compute_product_line_row(
    period_key: str,
    inp: Dict[str, Any],
    prod: Dict[str, Any],
    emp_setting: Dict[str, Any],
    emp_profile: Dict[str, Any],
    cfg: Optional[Dict[str, Any]],
    tiers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Một dòng đầy đủ để upsert salary_period_product_lines."""
    before_return, return_delta, after_return, settle_notes = settle_input_line_for_payroll(inp)
    deduct_vat = bool(cfg.get("deduct_vat", True)) if cfg else True
    deduct_ship = bool(cfg.get("deduct_shipping_fee", True)) if cfg else True
    gross = after_return["revenue_gross"]
    vat = float(prod.get("vat_rate") or 0)
    if deduct_vat and vat > 0:
        revenue_net = _money(gross / (1.0 + vat))
    else:
        revenue_net = _money(gross)
    ship = after_return["shipping_fee"]
    if deduct_ship:
        rfc = _money(revenue_net - ship)
    else:
        rfc = _money(revenue_net)
    oc = int(after_return["orders_count"])
    ads = after_return["ads_cost"]
    std = _num(prod.get("cpqc_standard_per_order"))
    imp_snap = _num(prod.get("import_price"))
    if oc > 0:
        ads_po = _money(ads / float(oc))
        if std > 0:
            cpqc_pct = _money((ads_po / std) * 100.0)
        else:
            cpqc_pct = 0.0
        comm_pct, stop_run, tier_note = pick_commission_from_cpqc_tiers(cpqc_pct, imp_snap, tiers)
    else:
        ads_po = 0.0
        cpqc_pct = 0.0
        comm_pct = 0.0
        stop_run = False
        tier_note = (
            "orders_count sau hoàn = 0 — không tính ads_per_order/CPQC; "
            "commission_percent = 0 (không có đơn để phân bổ ads)."
        )
    enabled = bool(prod.get("is_commission_enabled", True))
    if not enabled:
        comm_pct = 0.0
        stop_run = False
        tier_note = (tier_note + "; is_commission_enabled=false") if tier_note else "is_commission_enabled=false"
    comm_amt = _money(rfc * comm_pct) if enabled else 0.0
    cfg_id = int(cfg["id"]) if cfg and cfg.get("id") is not None else None
    note_extra = "; ".join(settle_notes) if settle_notes else ""
    calc_note = "; ".join(x for x in (tier_note, note_extra) if x)
    snap = {
        "source_table": "salary_period_input_lines",
        "input_line_id": inp.get("id"),
        "tier_note": tier_note,
        "before_return": before_return,
        "return_delta": return_delta,
        "after_return": after_return,
        "settle_notes": settle_notes,
    }
    return {
        "period_key": period_key,
        "employee_code": str(inp["employee_code"]).strip(),
        "employee_name_snapshot": str(emp_setting.get("employee_name") or emp_profile.get("employee_name") or inp["employee_code"]),
        "branch_code": (str(emp_profile.get("branch_code")).strip() or None) if emp_profile.get("branch_code") else None,
        "product_code": str(inp["product_code"]).strip(),
        "product_name_snapshot": str(prod.get("product_name") or inp["product_code"]),
        "revenue_gross": gross,
        "quantity": after_return["quantity"],
        "orders_count": max(oc, 0),
        "ads_cost": ads,
        "shipping_fee": ship,
        "vat_rate": vat,
        "revenue_net": revenue_net,
        "revenue_for_commission": rfc,
        "cpqc_standard_per_order": std,
        "ads_per_order": ads_po,
        "cpqc_percent": cpqc_pct,
        "commission_percent": comm_pct,
        "commission_amount": comm_amt,
        "import_price_snapshot": imp_snap,
        "stop_run": stop_run and enabled,
        "calc_note": (calc_note[:2000] if calc_note else None),
        "salary_config_id": cfg_id,
        "calc_snapshot_json": snap,
    }


def recalculate_period(period_key: str) -> Dict[str, Any]:
    """
    Tính lại dữ liệu kỳ (draft). Xóa product_lines + employee_results (giữ salary_period_input_lines).
    Nếu có input_lines: mỗi dòng → một dòng product_line; cộng dồn theo nhân viên.
    Nếu không có input: chỉ tạo employee_results mặc định (HH = 0) như trước.
    """
    pk = normalize_period_key(period_key)
    if repo.is_period_locked(pk):
        raise ValueError("Kỳ đã chốt lương, không được tính lại.")
    repo.delete_period_payroll_data(pk)
    inputs = repo.list_period_input_lines(pk)
    agg: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {
            "total_revenue_gross": 0.0,
            "total_revenue_net": 0.0,
            "total_shipping_fee": 0.0,
            "total_ads_cost": 0.0,
            "total_commission_amount": 0.0,
            "stop_run_flag_count": 0.0,
        }
    )
    product_lines_written = 0
    skipped_inputs: List[Dict[str, Any]] = []

    for inp in inputs:
        code = str(inp["employee_code"]).strip()
        ess = repo.get_employee_salary_setting(code)
        if not ess or not ess.get("is_active", True):
            skipped_inputs.append({**inp, "reason": "no_active_employee_salary_setting"})
            continue
        prod = repo.get_product_salary_config_by_code(str(inp["product_code"]))
        if not prod:
            skipped_inputs.append({**inp, "reason": "product_salary_config_not_found"})
            continue
        emp_profile = repo.get_employee(code) or {}
        cfg = None
        tiers: List[Dict[str, Any]] = []
        if ess.get("salary_config_id"):
            cfg = repo.get_salary_config(int(ess["salary_config_id"]))
            if cfg:
                tiers = repo.list_cpqc_tiers(int(cfg["id"]))
        line = _compute_product_line_row(pk, inp, prod, ess, emp_profile, cfg, tiers)
        repo.upsert_salary_period_product_line(line)
        product_lines_written += 1
        a = agg[code]
        a["total_revenue_gross"] += line["revenue_gross"]
        a["total_revenue_net"] += line["revenue_net"]
        a["total_shipping_fee"] += line["shipping_fee"]
        a["total_ads_cost"] += line["ads_cost"]
        a["total_commission_amount"] += line["commission_amount"]
        if line.get("stop_run"):
            a["stop_run_flag_count"] += 1.0

    n = 0
    for emp in repo.list_employee_salary_settings():
        if not emp.get("is_active", True):
            continue
        base_full = _f(emp.get("base_salary"))
        # Lương cơ bản theo công: ngày ≥8,5h / 26 (cap 100%) — chốt 11/06
        att_days = _attendance_days(pk, str(emp["employee_code"]))
        att_factor = min(att_days, ATTENDANCE_FULL_DAYS) / float(ATTENDANCE_FULL_DAYS)
        base = _money(base_full * att_factor)
        allowance = _f(emp.get("allowance"))
        penalty = _f(emp.get("default_penalty"))
        advance = _f(emp.get("default_advance"))
        code = str(emp["employee_code"])
        a = agg.get(code)
        if a:
            tgross = _money(a["total_revenue_gross"])
            tnet = _money(a["total_revenue_net"])
            tship = _money(a["total_shipping_fee"])
            tads = _money(a["total_ads_cost"])
            tcomm = _money(a["total_commission_amount"])
            stops = int(a["stop_run_flag_count"])
        else:
            tgross = tnet = tship = tads = tcomm = 0.0
            stops = 0
        final = _money(base + allowance + tcomm - penalty - advance)
        repo.upsert_employee_period_result_draft(
            pk,
            code,
            str(emp.get("employee_name") or code),
            (str(emp.get("branch_code")).strip() or None) if emp.get("branch_code") else None,
            {
                "total_revenue_gross": tgross,
                "total_revenue_net": tnet,
                "total_shipping_fee": tship,
                "total_ads_cost": tads,
                "total_commission_amount": tcomm,
                "base_salary": base,
                "allowance": allowance,
                "penalty": penalty,
                "advance_amount": advance,
                "final_salary": final,
                "stop_run_flag_count": stops,
            },
            calc_snapshot={
                "attendance": {
                    "days_dat_chuan": att_days,
                    "nguong_gio": ATTENDANCE_MIN_HOURS,
                    "cong_chuan_thang": ATTENDANCE_FULL_DAYS,
                    "he_so": round(att_factor, 4),
                    "luong_cb_hop_dong": base_full,
                    "luong_cb_theo_cong": base,
                },
            },
        )
        n += 1
    return {
        "ok": True,
        "period_key": pk,
        "employee_rows": n,
        "input_lines": len(inputs),
        "product_lines_written": product_lines_written,
        "skipped_inputs": skipped_inputs,
    }


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# Chuẩn công: ngày làm ≥ 8,5 giờ (chốt với sếp 11/06 — thay vì 9h cứng vì GPS
# chấm lệch vài phút). Đủ 26 công = hưởng trọn lương cơ bản.
ATTENDANCE_MIN_HOURS = 8.5
ATTENDANCE_FULL_DAYS = 26


def _attendance_days(period_key: str, employee_code: str) -> int:
    """Số ngày công đạt chuẩn (≥8,5h) trong tháng của NV.

    employee_code = users.username (theo sync_employees_from_dashboard_users);
    cc_attendance.user_id là varchar chứa users.id. Cột regular_minutes CHẾT
    (0 toàn bộ) — phải tính giờ từ check_in/check_out.
    """
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM cc_attendance a
                JOIN users u ON u.id::text = a.user_id
                WHERE LOWER(u.username) = LOWER(%s)
                  AND a.date >= %s::date
                  AND a.date < (%s::date + INTERVAL '1 month')
                  AND a.check_in IS NOT NULL AND a.check_out IS NOT NULL
                  AND EXTRACT(EPOCH FROM (a.check_out - a.check_in)) / 3600.0 >= %s
                """,
                (employee_code, f"{period_key}-01", f"{period_key}-01", ATTENDANCE_MIN_HOURS),
            )
            return int(cur.fetchone()[0])


def finalize_period(period_key: str, actor: str, confirmed: bool) -> Dict[str, Any]:
    if not confirmed:
        raise ValueError("Cần xác nhận mới được chốt lương.")
    pk = normalize_period_key(period_key)
    if repo.is_period_locked(pk):
        raise ValueError("Kỳ này đã được chốt.")
    rows = repo.list_employee_results_for_period(pk)
    if not rows:
        raise ValueError("Chưa có dữ liệu lương cho kỳ này. Hãy bấm Tính lại trước.")
    row_snaps: List[Tuple[int, Dict[str, Any]]] = []
    for r in rows:
        st = str(r.get("status") or "").lower().strip()
        if st == "finalized":
            continue
        if st != "draft":
            continue
        snap = build_employee_finalize_snapshot(pk, str(r["employee_code"]), actor)
        snap["totals_at_finalize"] = {
            "total_revenue_gross": r.get("total_revenue_gross"),
            "total_revenue_net": r.get("total_revenue_net"),
            "total_shipping_fee": r.get("total_shipping_fee"),
            "total_ads_cost": r.get("total_ads_cost"),
            "total_commission_amount": r.get("total_commission_amount"),
            "base_salary": r.get("base_salary"),
            "allowance": r.get("allowance"),
            "penalty": r.get("penalty"),
            "advance_amount": r.get("advance_amount"),
            "final_salary": r.get("final_salary"),
            "stop_run_flag_count": r.get("stop_run_flag_count"),
        }
        row_snaps.append((int(r["id"]), snap))
    if not row_snaps:
        raise ValueError("Không có dòng draft để chốt (có thể đã chốt trước đó).")
    global_snap = build_global_config_snapshot()
    global_snap["period_key"] = pk
    global_snap["finalized_by"] = actor
    repo.run_finalize_period_transaction(pk, actor, row_snaps, global_snap)
    return {"ok": True, "period_key": pk, "finalized_rows": len(row_snaps)}
