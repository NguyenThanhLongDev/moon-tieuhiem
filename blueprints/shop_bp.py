from __future__ import annotations

import os
import sys
import json
import threading
import subprocess
import glob
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Blueprint, abort, render_template_string, request, url_for, redirect, session, g, send_file, jsonify
from openpyxl import Workbook

# Import everything from app_ctx (helpers, login_required, render_page, etc.)
from app_ctx import *
from app_ctx import (
    login_required, render_page,
    _check_user_password, _hash_password,
    _enforce_admin_only_sections,
    _fb_ads_tokens_store_path,
    _trigger_page_spend_sync_bg,
    _get_request_cache, _set_request_cache,
    _build_sent_items_from_db,
    _build_shop_employee_mapping_for_ads,
)
from app_constants import (
    BASE_DIR, DASHBOARD_WEB_VERSION, PAGE_TEMPLATE, LOGIN_TEMPLATE,
    DASHBOARD_USERNAME, DASHBOARD_PASSWORD,
    LOW_STOCK_THRESHOLD, SLOW_DAYS, MIN_OLD_QTY, MAX_SOLD_IN_7D,
    MONTH_LOSS_WARN, MONTH_LOSS_SEVERE,
    SHOP_BODY, STOCK_DASHBOARD_BODY, STOCK_SHOP_BODY,
    SLOW_DASHBOARD_BODY, SLOW_SHOP_BODY,
    SENT_ITEMS_DASHBOARD_BODY, SENT_ITEMS_SHOP_BODY,
    EXPORT_ITEMS_V2_BODY,
)
try:
    from db import get_conn as get_db_conn
except Exception:
    get_db_conn = None
try:
    from repositories.admin_repo import (
        list_fb_ad_account_mappings as repo_list_fb_ad_account_mappings,
        upsert_fb_ad_account_mapping as repo_upsert_fb_ad_account_mapping,
        delete_fb_ad_account_mapping as repo_delete_fb_ad_account_mapping,
        toggle_fb_ad_account_mapping_status as repo_toggle_fb_ad_account_mapping_status,
        get_shop_id_by_key as repo_get_shop_id_by_key,
        get_fb_ad_account_mappings as repo_get_fb_ad_account_mappings,
    )
except Exception:
    repo_list_fb_ad_account_mappings = None
    repo_upsert_fb_ad_account_mapping = None
    repo_delete_fb_ad_account_mapping = None
    repo_toggle_fb_ad_account_mapping_status = None
    repo_get_shop_id_by_key = None
    repo_get_fb_ad_account_mappings = None

from modules.export_items_v2.service import build_export_items_v2_report, save_export_items_v2_excel

shop_bp = Blueprint("shop", __name__)


@shop_bp.route("/shop/<shop_key>/daily-json")
@login_required
def shop_daily_json(shop_key: str):
    """JSON chi tiết TỪNG NGÀY của 1 shop (cho modal ở dashboard): ngày · doanh thu ·
    lợi nhuận · đơn tạo. Lấy từ daily_shop_metrics."""
    import datetime as _dt
    if not is_shop_allowed_for_current_user(shop_key):
        abort(403)
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    if not date_from or not date_to:
        return jsonify({"error": "thiếu from/to"}), 400
    try:
        d0 = _dt.datetime.strptime(date_from, "%Y-%m-%d").date()
        d1 = _dt.datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "ngày sai định dạng"}), 400
    if d0 > d1:
        d0, d1 = d1, d0

    by_date: dict = {}
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.metric_date,
                           COALESCE(m.net_revenue, 0),
                           COALESCE(m.pos_profit_loss, 0),
                           COALESCE(m.order_count, 0),
                           COALESCE(m.confirmed_count, 0),
                           COALESCE(m.returned_count, 0)
                      FROM daily_shop_metrics m
                      JOIN shops s ON s.id = m.shop_id
                     WHERE s.shop_key = %s
                       AND m.metric_date BETWEEN %s AND %s
                    """,
                    (shop_key, d0.isoformat(), d1.isoformat()),
                )
                for d, rev, profit, oc, cf, rt in cur.fetchall():
                    by_date[str(d)] = {
                        "revenue": float(rev or 0),
                        "profit": float(profit or 0),
                        "orders": int(oc or 0),
                        "confirmed": int(cf or 0),
                        "returned": int(rt or 0),
                    }
    except Exception as exc:
        logging.getLogger(__name__).error("shop_daily_json error: %s", exc)
        return jsonify({"error": "lỗi truy vấn"}), 500

    days = []
    cur_d = d0
    while cur_d <= d1:
        k = cur_d.isoformat()
        e = by_date.get(k, {})
        days.append({
            "date": k,
            "revenue": round(float(e.get("revenue", 0) or 0), 0),
            "profit": round(float(e.get("profit", 0) or 0), 0),
            "orders": int(e.get("orders", 0) or 0),
            "confirmed": int(e.get("confirmed", 0) or 0),
            "returned": int(e.get("returned", 0) or 0),
        })
        cur_d += _dt.timedelta(days=1)
    totals = {
        "revenue": sum(x["revenue"] for x in days),
        "profit": sum(x["profit"] for x in days),
        "orders": sum(x["orders"] for x in days),
        "confirmed": sum(x["confirmed"] for x in days),
        "returned": sum(x["returned"] for x in days),
    }
    return jsonify({"days": days, "totals": totals, "from": d0.isoformat(), "to": d1.isoformat()})


def _shop_page_stats(shop_id: int, month_from: str, month_to: str) -> List[Dict[str, Any]]:
    """Thống kê theo page cho 1 shop trong [month_from, month_to] (cả tháng).
    Mỗi dòng 1 page (cả Win + Test): số đơn chốt, tiền ads FB, ads/đơn, đơn hoàn, tỷ lệ hoàn.
    1 page ≈ 1 sản phẩm → tên page dùng làm tên sản phẩm. Sắp theo tỷ lệ hoàn giảm dần."""
    if get_db_conn is None:
        return []
    try:
        from db import query_all
        pos_rows = query_all("""
            SELECT page_id, MAX(page_name),
                   COALESCE(SUM(success_order_count),0),
                   COALESCE(SUM(returned_order_count),0),
                   COALESCE(SUM(revenue),0)
              FROM pos_page_daily_metrics
             WHERE shop_id = %s AND metric_date BETWEEN %s AND %s
             GROUP BY page_id
        """, (shop_id, month_from, month_to))
        if not pos_rows:
            return []
        page_ids = [str(r[0]) for r in pos_rows if r[0]]
        ads_map: Dict[str, float] = {}
        if page_ids:
            ads_rows = query_all("""
                SELECT page_id, SUM(spend) FROM (
                    SELECT page_id, metric_date, fb_ad_account_id, MAX(spend) AS spend
                      FROM fb_ads_page_daily_spend
                     WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                     GROUP BY page_id, metric_date, fb_ad_account_id
                ) t GROUP BY page_id
            """, (page_ids, month_from, month_to))
            ads_map = {str(r[0]): float(r[1] or 0) for r in ads_rows}
        # Avatar page (ảnh page/sản phẩm) — ưu tiên fb_page_names.picture_url, fallback graph.facebook
        pic_map: Dict[str, str] = {}
        if page_ids:
            try:
                pic_rows = query_all(
                    "SELECT page_id, picture_url FROM fb_page_names "
                    "WHERE page_id = ANY(%s) AND picture_url IS NOT NULL AND picture_url <> ''",
                    (page_ids,),
                )
                pic_map = {str(r[0]): str(r[1]) for r in pic_rows}
            except Exception:
                pic_map = {}
        # Sản phẩm bán nhiều nhất theo page (pos_page_top_product) — tên SKU + ảnh
        prod_map: Dict[str, Dict[str, str]] = {}
        try:
            prod_rows = query_all(
                "SELECT page_id, product_name, product_image FROM pos_page_top_product WHERE shop_id=%s",
                (shop_id,),
            )
            prod_map = {
                str(r[0]): {"name": str(r[1] or ""), "image": str(r[2] or "")}
                for r in prod_rows
            }
        except Exception:
            prod_map = {}
        rows: List[Dict[str, Any]] = []
        for pid, pname, chot, hoan, rev in pos_rows:
            chot = int(chot or 0); hoan = int(hoan or 0); rev = float(rev or 0)
            ads = float(ads_map.get(str(pid), 0))
            tong_don = chot + hoan
            ty_hoan = (hoan / tong_don * 100.0) if tong_don > 0 else 0.0
            ads_per = (ads / chot) if chot > 0 else None
            _avatar = pic_map.get(str(pid)) or (
                "https://graph.facebook.com/%s/picture?type=square&width=64&height=64" % str(pid)
            )
            _prod = prod_map.get(str(pid)) or {}
            rows.append({
                "page_id": str(pid),
                "page_name": pname or str(pid),
                "avatar": _avatar,
                "product_name": _prod.get("name") or "",
                "product_image": _prod.get("image") or "",
                "loai": "win" if rev > 0 else "test",
                "so_don": chot,
                "hoan": hoan,
                "ty_hoan": ty_hoan,
                "ads_raw": round(ads),
                "so_don_fmt":  "{:,.0f}".format(chot),
                "hoan_fmt":    "{:,.0f}".format(hoan),
                "ty_hoan_fmt": "{:.1f}%".format(ty_hoan),
                "ads_fmt":     "{:,.0f}".format(ads),
                "ads_per_don_fmt": ("{:,.0f}".format(ads_per) if ads_per is not None else "—"),
            })
        # Sắp: nhiều đơn hoàn nhất trước (sản phẩm "về hoàn nhiều"), rồi tỷ lệ hoàn, rồi số đơn
        rows.sort(key=lambda x: (-x["hoan"], -x["ty_hoan"], -x["so_don"]))
        return rows
    except Exception as exc:
        logging.getLogger(__name__).warning("_shop_page_stats error: %s", exc)
        return []


@shop_bp.route("/shop/<shop_key>")
@login_required
def shop_detail(shop_key: str):
    if not is_shop_allowed_for_current_user(shop_key):
        abort(403)
    selected_date = request.args.get("date", "").strip() or None
    shops = load_all_shops(selected_date, allowed_shop_keys=get_allowed_shop_keys_for_current_user())
    shop = next((s for s in shops if s["shop_key"] == shop_key), None)
    if not shop:
        abort(404)

    # ── Thống kê theo page (theo KHOẢNG NGÀY — mặc định cả tháng của ngày đang chọn) ──
    _df = (request.args.get("date_from") or "").strip()
    _dt = (request.args.get("date_to") or "").strip()
    page_stats: List[Dict[str, Any]] = []
    range_label = ""
    date_from = _df
    date_to = _dt
    try:
        from db import query_one
        _sid_row = query_one("SELECT id FROM shops WHERE shop_key=%s LIMIT 1", (shop_key,))
        if _sid_row:
            _sid = _sid_row[0]
            if _df and _dt:
                _rf, _rt = _df, _dt
            else:
                # Mặc định: cả tháng của ngày đang chọn (hoặc ngày mới nhất có dữ liệu)
                _d = selected_date
                if not _d:
                    _lr = query_one(
                        "SELECT MAX(metric_date) FROM pos_page_daily_metrics WHERE shop_id=%s",
                        (_sid,),
                    )
                    _d = _lr[0].isoformat() if _lr and _lr[0] else None
                if _d:
                    import calendar as _cal
                    _m = _d[:7]
                    _y, _mo = int(_m[:4]), int(_m[5:7])
                    _rf = _m + "-01"
                    _rt = "%s-%02d" % (_m, _cal.monthrange(_y, _mo)[1])
                else:
                    _rf = _rt = None
            if _rf and _rt:
                date_from, date_to = _rf, _rt
                range_label = "%s → %s" % (_rf, _rt)
                page_stats = _shop_page_stats(_sid, _rf, _rt)
    except Exception as _e:
        logging.getLogger(__name__).warning("shop page_stats error: %s", _e)

    body = render_template_string(
        SHOP_BODY,
        shop=shop,
        shop_alerts=build_shop_alerts(shop),
        selected_date=selected_date or "",
        selected_date_label=selected_date or "Ngày mới nhất trong shop",
        page_stats=page_stats,
        range_label=range_label,
        date_from=date_from,
        date_to=date_to,
        shop_key=shop_key,
    )
    return render_template_string(PAGE_TEMPLATE, title=shop["shop_name"], body=body)

@shop_bp.route("/stock")
@login_required
def stock_dashboard():
    selected_date = request.args.get("date", "").strip() or None
    stock_shops = load_all_stock_shops(selected_date)

    total_shops = len(stock_shops)
    total_out = sum(s["out_count"] for s in stock_shops)
    total_low = sum(s["low_count"] for s in stock_shops)
    total_slow = sum(s["slow_count"] for s in stock_shops)
    total_problem_shops = sum(1 for s in stock_shops if s["out_count"] > 0 or s["low_count"] > 0 or s["slow_count"] > 0)
    total_quantity_all = 0
    total_value_all = 0
    total_stock_items = build_total_stock_items(stock_shops)
    top_stock_items = total_stock_items[:10]

    for shop in stock_shops:
        for raw_item in shop.get("data", []):
            variations = raw_item.get("variations", []) or []
            # Loop qua TẤT CẢ variations × TẤT CẢ warehouses
            # Bug fix 2026-05-21: trước chỉ tính variations[0] + warehouses[0]
            # → SP multi-color/multi-warehouse bị under-count nặng.
            for variation in variations:
                warehouses = variation.get("variations_warehouses", []) or []
                qty_v = sum(int(w.get("actual_remain_quantity", 0) or 0) for w in warehouses)
                try:
                    qty_v = float(qty_v)
                except Exception:
                    qty_v = 0
                try:
                    price_v = float(variation.get("average_imported_price", 0) or 0)
                except Exception:
                    price_v = 0
                total_quantity_all += qty_v
                total_value_all += qty_v * price_v
    selected_date_label = selected_date or "Ngày mới nhất"

    body = render_template_string(
        STOCK_DASHBOARD_BODY,
        total_shops=format_int(total_shops),
        total_out=format_int(total_out),
        total_low=format_int(total_low),
        total_slow=format_int(total_slow),
        total_problem_shops=format_int(total_problem_shops),
        updated_at=now_hcm().strftime("%d/%m/%Y %H:%M"),
        stock_shops=stock_shops,
        stock_alerts=build_stock_dashboard_alerts(stock_shops),
        low_threshold=LOW_STOCK_THRESHOLD,
        selected_date=selected_date or "",
        selected_date_label=selected_date_label,
        total_quantity_all=format_int(total_quantity_all),
        total_value_all=format_int(total_value_all),
        top_stock_items=top_stock_items,
        total_stock_items=total_stock_items[:100],
    )
    return render_template_string(PAGE_TEMPLATE, title="Tồn kho", body=body)
@shop_bp.route("/stock/<shop_key>")
@login_required
def stock_shop_detail(shop_key: str):
    if not is_shop_allowed_for_current_user(shop_key):
        abort(403)
    selected_date = request.args.get("date", "").strip() or None
    stock_shops = load_all_stock_shops(selected_date)
    stock_shop = next((s for s in stock_shops if s["shop_key"] == shop_key), None)
    if not stock_shop:
        abort(404)

    selected_date_label = selected_date or "Ngày mới nhất"
    full_stock_items = []

    for raw_item in stock_shop.get("data", []):
        variations = raw_item.get("variations", []) or []
        product_name = raw_item.get("name", "")
        # 1 row per variation (multi-color/size hiện đầy đủ thay vì chỉ [0]).
        # Bug fix 2026-05-21: trước chỉ tính variations[0] + warehouses[0].
        for variation in variations:
            warehouses = variation.get("variations_warehouses", []) or []
            first_warehouse = warehouses[0] if warehouses else {}
            product_code = variation.get("custom_id") or raw_item.get("display_id") or ""
            warehouse_id = first_warehouse.get("warehouse_id", "")
            try:
                quantity = float(sum(int(w.get("actual_remain_quantity", 0) or 0) for w in warehouses))
            except Exception:
                quantity = 0
            try:
                import_price = float(variation.get("average_imported_price", 0) or 0)
            except Exception:
                import_price = 0
            full_stock_items.append({
                "product_name": product_name,
                "product_code": product_code,
                "warehouse_id": warehouse_id,
                "quantity": quantity,
                "import_price": import_price,
            })

    total_quantity = sum(item.get("quantity", 0) for item in full_stock_items)
    total_value = sum(
        item.get("quantity", 0) * item.get("import_price", 0)
        for item in full_stock_items
    )

    body = render_template_string(
        STOCK_SHOP_BODY,
        stock_shop=stock_shop,
        stock_shop_alerts=build_stock_shop_alerts(stock_shop["shop_id"], selected_date),
        selected_date=selected_date or "",
        stock_items=full_stock_items,
        selected_date_label=selected_date_label,
        total_quantity=format_int(total_quantity),
        total_value=format_int(total_value),
    )
    return render_template_string(PAGE_TEMPLATE, title=f"Tồn kho - {stock_shop['shop_name']}", body=body)

@shop_bp.route("/slow")
@login_required
def slow_dashboard():
    selected_date = request.args.get("date", "").strip() or None
    slow_shops = build_slow_dashboard_data(selected_date)

    total_shops = len(slow_shops)
    total_slow_items = sum(s["count"] for s in slow_shops)
    total_stuck_qty = sum(s["stuck"] for s in slow_shops)
    total_problem_shops = sum(1 for s in slow_shops if s["count"] > 0)

    selected_date_label = selected_date or "Ngày mới nhất"

    body = render_template_string(
        SLOW_DASHBOARD_BODY,
        total_shops=format_int(total_shops),
        total_slow_items=format_int(total_slow_items),
        total_stuck_qty=format_int(total_stuck_qty),
        total_problem_shops=format_int(total_problem_shops),
        updated_at=now_hcm().strftime("%d/%m/%Y %H:%M"),
        slow_shops=slow_shops,
        slow_alerts=build_slow_dashboard_alerts(slow_shops),
        slow_days=SLOW_DAYS,
        selected_date=selected_date or "",
        selected_date_label=selected_date_label,
    )
    return render_template_string(PAGE_TEMPLATE, title="Bán chậm", body=body)

@shop_bp.route("/slow/<shop_key>")
@login_required
def slow_shop_detail(shop_key: str):
    if not is_shop_allowed_for_current_user(shop_key):
        abort(403)
    selected_date = request.args.get("date", "").strip() or None
    slow_shops = build_slow_dashboard_data(selected_date)
    slow_shop = next((s for s in slow_shops if s["shop_key"] == shop_key), None)
    if not slow_shop:
        abort(404)

    slow_items = []
    for item in slow_shop["items"][:30]:
        slow_items.append({
            "message": f"{item['product_name']} | Mã: {item['product_code']} | Xuất 7 ngày: {item['sold_7d']} | Tồn hiện tại: {item['today_qty']} | Tồn 7 ngày trước: {item['old_qty']}"
        })

    selected_date_label = selected_date or "Ngày mới nhất"

    body = render_template_string(
        SLOW_SHOP_BODY,
        slow_shop=slow_shop,
        slow_items=slow_items,
        selected_date=selected_date or "",
        selected_date_label=selected_date_label,
    )
    return render_template_string(PAGE_TEMPLATE, title=f"Bán chậm - {slow_shop['shop_name']}", body=body)

@shop_bp.route("/sent-items")
@login_required
def sent_items_dashboard():
    selected_date = request.args.get("date", "").strip() or today_hcm()
    selected_shop_key = request.args.get("shop_key", "").strip() or None
    if selected_shop_key and not is_shop_allowed_for_current_user(selected_shop_key):
        abort(403)

    try:
        sent_data = build_sent_items_data(selected_date, selected_shop_key)
    except Exception as e:
        sent_data = {
            "rows": [],
            "top_items": [],
            "total_orders": 0,
            "total_quantity": 0,
            "total_distinct": 0,
            "total_carrier_pickup_orders": 0,
            "error": f"Lỗi build_sent_items_data: {e}",
            "target_date": selected_date,
        }

    sent_shops = sent_data["rows"]
    top_items = sent_data["top_items"]

    top_shop_name = "-"
    if sent_shops:
        top_shop_name = sent_shops[0]["shop_name"]

    selected_scope_label = "Toàn hệ thống"
    if selected_shop_key:
        meta = get_visible_shop_meta_map().get(selected_shop_key)
        if meta:
            selected_scope_label = meta.get("shop_name", selected_shop_key)

    if sent_data["error"]:
        top_items = [{
            "shop_name": "Lỗi",
            "product_name": sent_data["error"],
            "product_code": "-",
            "quantity_fmt": "0",
            "order_count_fmt": "0",
        }]
        top_shop_name = "Lỗi"

    body = render_template_string(
        SENT_ITEMS_DASHBOARD_BODY,
        selected_date=selected_date,
        selected_date_label=selected_date,
        selected_shop_key=selected_shop_key or "",
        selected_scope_label=selected_scope_label,
        updated_at=now_hcm().strftime("%d/%m/%Y %H:%M"),
        shop_options=load_active_shop_options(),
        total_shops=format_int(len(sent_shops)),
        total_orders=format_int(sent_data["total_orders"]),
        total_quantity=format_int(sent_data["total_quantity"]),
        total_distinct=format_int(sent_data["total_distinct"]),
        total_carrier_pickup_orders=format_int(int(sent_data.get("total_carrier_pickup_orders", 0) or 0)),
        top_shop_name=top_shop_name,
        sent_shops=sent_shops,
        top_items=top_items,
    )
    return render_template_string(PAGE_TEMPLATE, title="Xuất hàng", body=body)

@shop_bp.route("/sent-items/<shop_key>")
@login_required
def sent_items_shop_detail(shop_key: str):
    if not is_shop_allowed_for_current_user(shop_key):
        abort(403)
    selected_date = request.args.get("date", "").strip() or datetime.now().strftime("%Y-%m-%d")
    sent_data = build_sent_items_data(selected_date, shop_key)
    sent_shop = next((s for s in sent_data["rows"] if s["shop_key"] == shop_key), None)
    if not sent_shop:
        abort(404)

    sent_items = []
    for item in sent_shop["items"][:50]:
        sent_items.append({
            "product_name": item["product_name"],
            "product_code": item["product_code"],
            "quantity_fmt": format_int(item["quantity"]),
            "order_count_fmt": format_int(item["order_count"]),
        })

    body = render_template_string(
        SENT_ITEMS_SHOP_BODY,
        sent_shop=sent_shop,
        sent_items=sent_items,
        selected_date=selected_date,
        selected_date_label=selected_date,
    )
    return render_template_string(PAGE_TEMPLATE, title=f"Xuất hàng - {sent_shop['shop_name']}", body=body)
@shop_bp.route("/sent-items/save-excel")
@login_required
def save_sent_items_excel_route():
    selected_date = request.args.get("date", "").strip() or datetime.now().strftime("%Y-%m-%d")
    selected_shop_key = request.args.get("shop_key", "").strip() or None

    filepath, filename = save_sent_items_excel(selected_date, selected_shop_key)

    import subprocess

    try:
        subprocess.run(
            ["rclone", "copy", filepath, "drive:pos_exports"],
            check=True
        )
        upload_status = "☁️ Đã upload Google Drive"
    except subprocess.CalledProcessError:
        upload_status = "❌ Lỗi upload Google Drive"

    body = render_template_string(
        """
        <div class="topbar">
          <div class="title">
            <h1>Đã lưu file Excel</h1>
            <p>File đã được lưu trên VPS.</p>
            <div style="margin-top:10px;color:#16a34a;font-weight:600;">
              {{ upload_status }}
            </div>
          </div>
          <div class="controls">
            <a class="button-link" href="{{ url_for('shop.sent_items_dashboard', date=selected_date, shop_key=selected_shop_key) }}">← Quay lại Xuất hàng</a>
          </div>
        </div>

        <div class="card">
          <div class="section-title">
            <h2>Thông tin file</h2>
            <span>Nội bộ</span>
          </div>
          <table>
            <tbody>
              <tr><th>Tên file</th><td>{{ filename }}</td></tr>
              <tr><th>Đường dẫn lưu</th><td>{{ filepath }}</td></tr>
              <tr><th>Ngày dữ liệu</th><td>{{ selected_date }}</td></tr>
              <tr><th>Phạm vi</th><td>{{ selected_scope }}</td></tr>
            </tbody>
          </table>
        </div>
        """,
        filepath=filepath,
        filename=filename,
        selected_date=selected_date,
        selected_shop_key=selected_shop_key or "",
        selected_scope=selected_shop_key or "toan_he_thong",
        upload_status=upload_status,
    )
    return render_template_string(PAGE_TEMPLATE, title="Da luu Excel", body=body)


def _export_v2_fmt_vn_date(ymd: str) -> str:
    try:
        return datetime.strptime(str(ymd).strip()[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return str(ymd or "")


def _export_v2_fmt_month_label(ym: str) -> str:
    s = str(ym or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if not m:
        return s or "—"
    return f"Tháng {m.group(2)}/{m.group(1)}"


@shop_bp.route("/export-items-v2")
@login_required
def export_items_v2_dashboard():
    z_now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    default_date = z_now.strftime("%Y-%m-%d")
    default_month = z_now.strftime("%Y-%m")

    view_mode = (request.args.get("view_mode") or "day").strip().lower()
    if view_mode not in ("day", "month", "range"):
        view_mode = "day"
    anchor_date = (request.args.get("date") or "").strip() or default_date
    month_value = (request.args.get("month") or "").strip() or default_month
    date_from = (request.args.get("from") or "").strip() or default_date
    date_to = (request.args.get("to") or "").strip() or default_date
    selected_shop_key = (request.args.get("shop_key") or "").strip() or None
    search_q = (request.args.get("q") or "").strip()

    if selected_shop_key and not is_shop_allowed_for_current_user(selected_shop_key):
        abort(403)

    allowed = get_allowed_shop_keys_for_current_user()
    meta_map = get_visible_shop_meta_map()

    if view_mode == "day":
        filter_mode_label = "Theo ngày"
        filter_time_summary = _export_v2_fmt_vn_date(anchor_date)
    elif view_mode == "month":
        filter_mode_label = "Theo tháng"
        filter_time_summary = _export_v2_fmt_month_label(month_value)
    else:
        filter_mode_label = "Khoảng ngày"
        filter_time_summary = f"{_export_v2_fmt_vn_date(date_from)} – {_export_v2_fmt_vn_date(date_to)}"

    if selected_shop_key:
        sm = meta_map.get(selected_shop_key) or {}
        filter_shop_summary = str(sm.get("shop_name") or selected_shop_key)
    else:
        filter_shop_summary = "Tất cả"

    try:
        raw_report = build_export_items_v2_report(
            view_mode=view_mode,
            anchor_date=anchor_date,
            month_value=month_value,
            date_from=date_from,
            date_to=date_to,
            shop_key_filter=selected_shop_key,
            search=search_q,
            allowed_shop_keys=allowed,
            get_db_conn=get_db_conn,
            shop_key_to_meta=meta_map,
        )
    except Exception as e:
        raw_report = {
            "error": str(e),
            "rows": [],
            "kpis": {},
            "period": {"label": "", "start": default_date, "end": default_date},
            "meta_note": "",
        }

    report_error = str(raw_report.get("error") or "").strip()
    period = raw_report.get("period") or {}
    period_label = str(period.get("label") or "").strip() or f"{date_from} → {date_to}"
    meta_note = str(raw_report.get("meta_note") or "").strip()
    kpis = raw_report.get("kpis") or {}

    detail_rows = []
    for r in raw_report.get("rows") or []:
        detail_rows.append({
            **r,
            "qty_fmt": format_int(round(float(r.get("qty") or 0))),
            "stock_fmt": format_int(round(float(r.get("current_stock") or 0))),
            "order_count_fmt": format_int(int(r.get("order_count") or 0)),
        })

    from urllib.parse import urlencode

    qs = urlencode(
        {
            "view_mode": view_mode,
            "date": anchor_date,
            "month": month_value,
            "from": date_from,
            "to": date_to,
            "shop_key": selected_shop_key or "",
            "q": search_q,
        }
    )
    excel_url = url_for("shop.export_items_v2_save_excel") + ("?" + qs if qs else "")

    body = render_template_string(
        EXPORT_ITEMS_V2_BODY,
        view_mode=view_mode,
        anchor_date=anchor_date,
        month_value=month_value,
        date_from=date_from,
        date_to=date_to,
        selected_shop_key=selected_shop_key or "",
        search_q=search_q,
        shop_options=load_active_shop_options(),
        updated_at=datetime.now().strftime("%d/%m/%Y %H:%M"),
        report_error=report_error,
        period_label=period_label,
        meta_note=meta_note,
        kpi_orders=format_int(int(kpis.get("total_export_orders") or 0)),
        kpi_qty=format_int(round(float(kpis.get("total_qty") or 0))),
        kpi_distinct=format_int(int(kpis.get("distinct_skus") or 0)),
        kpi_stock=format_int(round(float(kpis.get("total_current_stock") or 0))),
        kpi_top=str(kpis.get("top_product_label") or "—"),
        detail_rows=detail_rows,
        excel_url=excel_url,
        filter_mode_label=filter_mode_label,
        filter_time_summary=filter_time_summary,
        filter_shop_summary=filter_shop_summary,
    )
    return render_template_string(PAGE_TEMPLATE, title="Xuất hàng 2", body=body)


@shop_bp.route("/export-items-v2/save-excel")
@login_required
def export_items_v2_save_excel():
    z_now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    default_date = z_now.strftime("%Y-%m-%d")
    default_month = z_now.strftime("%Y-%m")

    view_mode = (request.args.get("view_mode") or "day").strip().lower()
    if view_mode not in ("day", "month", "range"):
        view_mode = "day"
    anchor_date = (request.args.get("date") or "").strip() or default_date
    month_value = (request.args.get("month") or "").strip() or default_month
    date_from = (request.args.get("from") or "").strip() or default_date
    date_to = (request.args.get("to") or "").strip() or default_date
    selected_shop_key = (request.args.get("shop_key") or "").strip() or None
    search_q = (request.args.get("q") or "").strip()

    if selected_shop_key and not is_shop_allowed_for_current_user(selected_shop_key):
        abort(403)

    allowed = get_allowed_shop_keys_for_current_user()
    meta_map = get_visible_shop_meta_map()

    raw_report = build_export_items_v2_report(
        view_mode=view_mode,
        anchor_date=anchor_date,
        month_value=month_value,
        date_from=date_from,
        date_to=date_to,
        shop_key_filter=selected_shop_key,
        search=search_q,
        allowed_shop_keys=allowed,
        get_db_conn=get_db_conn,
        shop_key_to_meta=meta_map,
    )
    err = str(raw_report.get("error") or "").strip()
    if err:
        abort(400, description=err)

    period = raw_report.get("period") or {}
    p_start = str(period.get("start") or "").replace("-", "")
    p_end = str(period.get("end") or "").replace("-", "")
    sk_part = selected_shop_key or "all"
    fname = f"xuat_hang_2_{sk_part}_{p_start}_{p_end}.xlsx"

    exports_dir = os.path.join(BASE_DIR, "exports")
    os.makedirs(exports_dir, exist_ok=True)
    filepath = os.path.join(exports_dir, fname)
    save_export_items_v2_excel(raw_report, filepath)
    return send_file(filepath, as_attachment=True, download_name=fname)


