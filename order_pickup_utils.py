"""Shared ĐVVC / warehouse pickup time parsing — keep in sync with xuất hàng logic in web_app."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


def _safe_parse_pos_like_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts)
    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None) + timedelta(hours=7)
        return parsed
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _to_yyyy_mm_dd(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d")


def _extract_status_changed_to_sent_at(order: Dict[str, Any]) -> Optional[datetime]:
    histories = order.get("status_history")
    if not isinstance(histories, list):
        return None
    sent_times: List[datetime] = []
    for item in histories:
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "")).strip() != "2":
            continue
        parsed = _safe_parse_pos_like_datetime(item.get("updated_at"))
        if parsed is not None:
            sent_times.append(parsed)
    if not sent_times:
        return None
    return sorted(sent_times)[-1]


def order_counts_for_carrier_pickup_day(
    order: Dict[str, Any],
    target_date: str,
    *,
    trust_carrier_window_when_pickup_unknown: bool = True,
    trust_carrier_pickup_es_scope: bool = False,
) -> bool:
    """
    Đếm đơn đã gửi (status=2) thuộc ngày ĐVVC lấy target_date.

    Pancake get_orders(updateStatus=carrier_picked_up_at, timeRange=ngày X) đã lọc theo ES;
    một số đơn thiếu field parse được → shipped_date None, web trước đây loại hết → ít hơn POS 1–2 đơn.
    Khi trust_carrier_window_when_pickup_unknown=True: status=2 + không suy ra được ngày lấy → vẫn tính vào ngày X
    (chỉ dùng cho list đã lấy từ cửa sổ carrier_picked_up_at hoặc DB sync tương đương).

    Khi trust_carrier_pickup_es_scope=True: mọi đơn status=2 trong response API đó đều tính (không lọc lại theo parse
    ngày) — khớp tab POS khi payload parse lệch ngày 1 ngày hoặc thiếu mốc.
    """
    try:
        st = int(order.get("status", -1))
    except Exception:
        st = -1
    if st != 2:
        return False
    if trust_carrier_pickup_es_scope:
        return True
    pickup_dt, _ = _extract_shipping_pickup_time(order)
    day = _to_yyyy_mm_dd(pickup_dt)
    if day == target_date:
        return True
    if trust_carrier_window_when_pickup_unknown and day is None:
        return True
    return False


def _extract_shipping_pickup_time(order: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[str]]:
    direct_candidates = (
        "carrier_picked_up_at",
        "carrier_pickup_at",
        "picked_up_at",
        "pickup_time",
        "shipping_picked_at",
        "partner_picked_up_at",
        "delivery_picked_up_at",
        "warehouse_exported_at",
    )
    for key in direct_candidates:
        parsed = _safe_parse_pos_like_datetime(order.get(key))
        if parsed is not None:
            return parsed, key

    shipments = order.get("shipments")
    if isinstance(shipments, list):
        shipment_candidates = (
            "carrier_picked_up_at",
            "picked_up_at",
            "pickup_time",
            "updated_at",
        )
        for shipment in shipments:
            if not isinstance(shipment, dict):
                continue
            for key in shipment_candidates:
                parsed = _safe_parse_pos_like_datetime(shipment.get(key))
                if parsed is not None:
                    return parsed, f"shipments.{key}"

    sent_changed_at = _extract_status_changed_to_sent_at(order)
    if sent_changed_at is not None:
        return sent_changed_at, "status_history.status=2.updated_at"

    parsed_last_status = _safe_parse_pos_like_datetime(order.get("last_update_status_at"))
    if parsed_last_status is not None:
        return parsed_last_status, "last_update_status_at"

    return None, None
