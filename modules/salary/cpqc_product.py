"""Chọn mốc CPQC (theo %% CPQC và giá nhập) — dùng cho tính hoa hồng product-level."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

_EPS = 1e-6


def pick_commission_from_cpqc_tiers(
    cpqc_percent: float,
    import_price: float,
    tiers: List[Dict[str, Any]],
) -> Tuple[float, bool, str]:
    """
    Trả về (commission_percent dạng 0.035, stop_run, calc_note).
    tiers: đã sort sort_order; chỉ tier is_active.
    """
    x = float(cpqc_percent or 0)
    imp = float(import_price or 0)
    for t in tiers:
        if not t.get("is_active", True):
            continue
        mn = float(t.get("min_cpqc_percent") or 0)
        mx_raw = t.get("max_cpqc_percent")
        mx = float(mx_raw) if mx_raw is not None else None
        if x + _EPS < mn:
            continue
        if mx is not None and x > mx + _EPS:
            continue
        comm = float(t.get("commission_percent") or 0)
        req = t.get("require_import_price_lte")
        note_parts = [f"tier id={t.get('id')} min={mn} max={mx_raw}"]
        if req is not None and imp > float(req) + _EPS:
            comm = 0.0
            note_parts.append(f"import_price {imp} > require_import_price_lte {req} → 0%")
        stop = bool(t.get("stop_run"))
        if stop:
            note_parts.append("stop_run")
        return comm, stop, "; ".join(note_parts)
    return 0.0, False, "no_matching_cpqc_tier"
