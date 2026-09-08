"""
Xuất hàng 2 — đơn đã gửi (order_status=shipping) từ PostgreSQL.
Mốc ngày xuất: xuất kho thực tế → ĐVVC → đã gửi → inserted_at JSON → orders.created_at_pos.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from order_pickup_utils import _extract_status_changed_to_sent_at, _safe_parse_pos_like_datetime

_LOW_STOCK = 5
_LOOSE_PRE_DAYS = 75
_LOOSE_POST_DAYS = 21

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _try_parse_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _norm_sku(s: Optional[str]) -> str:
    return str(s or "").strip()


def _parse_ymd(s: str) -> Optional[date]:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _month_bounds(ym: str) -> Optional[Tuple[date, date]]:
    m = re.match(r"^(\d{4})-(\d{2})$", str(ym or "").strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if mo < 1 or mo > 12:
        return None
    first = date(y, mo, 1)
    if mo == 12:
        last = date(y, 12, 31)
    else:
        last = date(y, mo + 1, 1) - timedelta(days=1)
    return first, last


def extract_export_date_for_report(order: Dict[str, Any]) -> Tuple[Optional[date], str]:
    if not isinstance(order, dict):
        return None, "invalid_order"

    for key in (
        "warehouse_exported_at",
        "exported_at",
        "warehouse_out_at",
        "actual_exported_at",
    ):
        dt = _safe_parse_pos_like_datetime(order.get(key))
        if dt is not None:
            return dt.date(), key

    for key in (
        "carrier_picked_up_at",
        "carrier_pickup_at",
        "picked_up_at",
        "pickup_time",
        "shipping_picked_at",
        "partner_picked_up_at",
        "delivery_picked_up_at",
    ):
        dt = _safe_parse_pos_like_datetime(order.get(key))
        if dt is not None:
            return dt.date(), key

    shipments = order.get("shipments")
    if isinstance(shipments, list):
        for shipment in shipments:
            if not isinstance(shipment, dict):
                continue
            for key in ("carrier_picked_up_at", "picked_up_at", "pickup_time", "updated_at"):
                dt = _safe_parse_pos_like_datetime(shipment.get(key))
                if dt is not None:
                    return dt.date(), f"shipments.{key}"

    sent_changed = _extract_status_changed_to_sent_at(order)
    if sent_changed is not None:
        return sent_changed.date(), "status_history.status=2.updated_at"

    dt = _safe_parse_pos_like_datetime(order.get("last_update_status_at"))
    if dt is not None:
        return dt.date(), "last_update_status_at"

    dt = _safe_parse_pos_like_datetime(order.get("inserted_at") or order.get("created_at"))
    if dt is not None:
        return dt.date(), "inserted_at|created_at"

    return None, "none"


def _json_order_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _stock_totals_by_shop_sku(
    shop_key_to_meta: Dict[str, Dict[str, str]],
    shop_keys_scope: Set[str],
) -> Dict[Tuple[str, str], float]:
    totals: Dict[Tuple[str, str], float] = {}

    for sk in shop_keys_scope:
        meta = shop_key_to_meta.get(sk) or {}
        pancake_id = str(meta.get("shop_id", "") or "").strip()
        if not pancake_id:
            continue
        path_latest = os.path.join(_REPO_ROOT, f"stock_{pancake_id}.json")
        data = _try_parse_json_file(path_latest) if os.path.exists(path_latest) else None
        if not isinstance(data, dict):
            continue
        for product in data.get("data", []):
            if not isinstance(product, dict):
                continue
            pcode = str(product.get("custom_id", "") or "")
            for variation in product.get("variations", []) or []:
                if not isinstance(variation, dict):
                    continue
                vcode = _norm_sku(variation.get("custom_id") or pcode)
                for wh in variation.get("variations_warehouses", []) or []:
                    if not isinstance(wh, dict):
                        continue
                    qty = wh.get("available_quantity")
                    if qty is None:
                        continue
                    try:
                        qf = float(qty)
                    except Exception:
                        continue
                    key = (sk, vcode)
                    totals[key] = totals.get(key, 0.0) + qf
    return totals


def _stock_status_label(qty: float) -> str:
    if qty <= 0:
        return "Hết hàng"
    if qty <= _LOW_STOCK:
        return "Sắp hết"
    return "Đủ hàng"


def build_export_items_v2_report(
    *,
    view_mode: str,
    anchor_date: str,
    month_value: str,
    date_from: str,
    date_to: str,
    shop_key_filter: Optional[str],
    search: str,
    allowed_shop_keys: Optional[Set[str]],
    get_db_conn: Any,
    shop_key_to_meta: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    mode = (view_mode or "day").strip().lower()
    d_end: Optional[date] = None
    d_start: Optional[date] = None

    if mode == "day":
        d = _parse_ymd(anchor_date) or date.today()
        d_start = d_end = d
    elif mode == "month":
        bounds = _month_bounds(month_value)
        if not bounds:
            t = date.today()
            d_start = t.replace(day=1)
            last = calendar.monthrange(t.year, t.month)[1]
            d_end = t.replace(day=last)
        else:
            d_start, d_end = bounds
    else:
        d_start = _parse_ymd(date_from) or date.today()
        d_end = _parse_ymd(date_to) or d_start
        if d_end < d_start:
            d_start, d_end = d_end, d_start

    assert d_start is not None and d_end is not None
    period_label = f"{d_start.isoformat()} → {d_end.isoformat()}"

    if get_db_conn is None:
        return {
            "error": "Chưa có kết nối PostgreSQL (DATABASE_URL / driver). Xuất hàng 2 cần DB orders/order_items.",
            "rows": [],
            "kpis": {},
            "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
            "meta_note": "",
        }

    active_shop_keys = [
        sk
        for sk, m in shop_key_to_meta.items()
        if str(m.get("status", "")).strip() == "active"
    ]
    if allowed_shop_keys is not None:
        active_shop_keys = [sk for sk in active_shop_keys if sk in allowed_shop_keys]

    if shop_key_filter:
        if shop_key_filter not in active_shop_keys:
            return {
                "error": "Shop không hợp lệ hoặc không thuộc phạm vi tài khoản.",
                "rows": [],
                "kpis": {},
                "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
                "meta_note": "",
            }
        scope_keys = {shop_key_filter}
    else:
        scope_keys = set(active_shop_keys)

    if not scope_keys:
        return {
            "error": "Không có shop active trong phạm vi xem.",
            "rows": [],
            "kpis": {},
            "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
            "meta_note": "",
        }

    loose_lo = d_start - timedelta(days=_LOOSE_PRE_DAYS)
    loose_hi = d_end + timedelta(days=_LOOSE_POST_DAYS)
    shop_key_list = sorted(scope_keys)

    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT o.id, s.shop_key, s.shop_name, o.raw_payload_json, o.external_order_id,
                           o.created_at_pos
                    FROM orders o
                    INNER JOIN shops s ON s.id = o.shop_id
                    WHERE o.order_status = 'shipping'
                      AND s.shop_key = ANY(%s)
                      AND o.created_at_pos::date >= %s
                      AND o.created_at_pos::date <= %s
                    """,
                    (shop_key_list, loose_lo, loose_hi),
                )
                order_rows = cur.fetchall()
    except Exception as exc:
        return {
            "error": f"Lỗi truy vấn DB: {exc}",
            "rows": [],
            "kpis": {},
            "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
            "meta_note": "",
        }

    qualifying_ids: List[int] = []
    id_to_meta: Dict[int, Tuple[str, str, str, str]] = {}
    for row in order_rows:
        oid, sk, sname, raw_j, ext_oid = row[0], row[1], row[2], row[3], row[4]
        cap = row[5] if len(row) > 5 else None
        payload = _json_order_payload(raw_j)
        exp_d, _ = extract_export_date_for_report(payload)
        if exp_d is None and cap is not None:
            try:
                if isinstance(cap, datetime):
                    exp_d = cap.date()
                elif isinstance(cap, date):
                    exp_d = cap
                else:
                    parsed_cap = _safe_parse_pos_like_datetime(cap)
                    if parsed_cap is not None:
                        exp_d = parsed_cap.date()
            except Exception:
                exp_d = None
        if exp_d is None or exp_d < d_start or exp_d > d_end:
            continue
        qualifying_ids.append(int(oid))
        id_to_meta[int(oid)] = (str(sk), str(sname or sk), str(ext_oid or ""), exp_d.isoformat())

    if not qualifying_ids:
        stock_map = _stock_totals_by_shop_sku(shop_key_to_meta, scope_keys)
        return {
            "error": "",
            "rows": [],
            "kpis": {
                "total_export_orders": 0,
                "total_qty": 0.0,
                "distinct_skus": 0,
                "total_current_stock": 0.0,
                "top_product_label": "—",
            },
            "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
            "meta_note": (
                "Đơn lọc theo ngày xuất (ưu tiên warehouse_exported_at → ĐVVC → đã gửi → inserted_at). "
                "prefilter SQL: created_at_pos trong cửa sổ lỏng quanh kỳ. "
                "Nếu JSON thiếu mốc: dùng orders.created_at_pos."
            ),
            "_stock_map": stock_map,
        }

    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT oi.order_id, oi.sku, oi.product_name, oi.external_variant_id, oi.quantity::float
                    FROM order_items oi
                    WHERE oi.order_id = ANY(%s)
                    """,
                    (qualifying_ids,),
                )
                item_rows = cur.fetchall()
    except Exception as exc:
        return {
            "error": f"Lỗi đọc order_items: {exc}",
            "rows": [],
            "kpis": {},
            "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
            "meta_note": "",
        }

    agg: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order_ids_per_group: Dict[Tuple[str, str, str], Set[int]] = {}
    last_date_per_group: Dict[Tuple[str, str, str], date] = {}

    for oid, sku, pname, ext_var, qty in item_rows:
        oid = int(oid)
        if oid not in id_to_meta:
            continue
        sk, sname, _, exp_day_str = id_to_meta[oid]
        code = _norm_sku(sku)
        if not code and ext_var:
            code = f"#{ext_var}"
        name = str(pname or "").strip() or "Không rõ tên"
        gkey = (sk, code, name)
        if gkey not in agg:
            agg[gkey] = {"shop_key": sk, "shop_name": sname, "product_code": code, "product_name": name, "qty": 0.0}
            order_ids_per_group[gkey] = set()
            last_date_per_group[gkey] = datetime.strptime(exp_day_str, "%Y-%m-%d").date()
        agg[gkey]["qty"] += float(qty or 0)
        order_ids_per_group[gkey].add(oid)
        od = datetime.strptime(exp_day_str, "%Y-%m-%d").date()
        if od > last_date_per_group[gkey]:
            last_date_per_group[gkey] = od

    stock_map = _stock_totals_by_shop_sku(shop_key_to_meta, scope_keys)

    search_t = (search or "").strip().lower()
    list_rows: List[Dict[str, Any]] = []
    all_order_ids: Set[int] = set()
    total_qty = 0.0

    for gkey, row in agg.items():
        code_l = row["product_code"].lower()
        name_l = row["product_name"].lower()
        if search_t and search_t not in code_l and search_t not in name_l:
            continue
        sk = row["shop_key"]
        code = row["product_code"]
        oids = order_ids_per_group[gkey]
        all_order_ids.update(oids)
        q = row["qty"]
        total_qty += q
        if code.startswith("#"):
            current = 0.0
        else:
            current = float(stock_map.get((sk, code), 0.0))
        last_d = last_date_per_group[gkey]
        list_rows.append({
            "shop_key": sk,
            "shop_name": row["shop_name"],
            "product_code": code,
            "product_name": row["product_name"],
            "qty": q,
            "order_count": len(oids),
            "last_export_date": last_d.isoformat(),
            "current_stock": current,
            "stock_status": _stock_status_label(current),
        })

    list_rows.sort(key=lambda x: (-x["qty"], x["shop_name"], x["product_name"]))

    distinct_skus = len(list_rows)
    total_current_stock = sum(float(r["current_stock"]) for r in list_rows)
    top_label = "—"
    if list_rows:
        top = list_rows[0]
        top_label = f"{top['product_name']} ({top['product_code']}) — {top['shop_name']}"

    kpis = {
        "total_export_orders": len(all_order_ids),
        "total_qty": total_qty,
        "distinct_skus": distinct_skus,
        "total_current_stock": total_current_stock,
        "top_product_label": top_label,
    }

    return {
        "error": "",
        "rows": list_rows,
        "kpis": kpis,
        "period": {"start": d_start.isoformat(), "end": d_end.isoformat(), "label": period_label},
        "meta_note": (
            "Ngày xuất trong kỳ: parse từ raw_payload_json đơn (shipping), ưu tiên "
            "warehouse_exported_at → mốc ĐVVC → status đã gửi → last_update_status_at → inserted_at; "
            "nếu vẫn không có mốc thì dùng orders.created_at_pos. "
            "prefilter: order_status=shipping và created_at_pos trong cửa sổ lỏng quanh kỳ. "
            "Tồn hiện tại: tổng available_quantity theo mã (file stock_<Pancake shop_id>.json mới nhất)."
        ),
        "_stock_map": stock_map,
    }


def save_export_items_v2_excel(report: Dict[str, Any], filepath: str) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Xuat hang 2"
    ws.append(
        [
            "Shop",
            "Ma SP",
            "Ten SP",
            "SL xuat ky",
            "Ton hien tai",
            "So don",
            "Ngay xuat gan nhat",
            "Trang thai ton",
        ]
    )
    for r in report.get("rows") or []:
        ws.append(
            [
                r.get("shop_name"),
                r.get("product_code"),
                r.get("product_name"),
                int(round(float(r.get("qty") or 0))),
                int(round(float(r.get("current_stock") or 0))),
                r.get("order_count"),
                r.get("last_export_date"),
                r.get("stock_status"),
            ]
        )
    wb.save(filepath)
