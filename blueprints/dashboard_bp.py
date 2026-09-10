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
    DASHBOARD_BODY,
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

dashboard_bp = Blueprint("dashboard", __name__)

@dashboard_bp.route("/")
@login_required
def dashboard():
    raw_date = request.args.get("date", "").strip()
    selected_date = raw_date or datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")

    filter_staff = request.args.get("staff", "").strip()
    filter_date_from = request.args.get("date_from", "").strip()
    filter_date_to = request.args.get("date_to", "").strip()
    # Mặc định = HÔM NAY khi chưa chọn → picker hiện sẵn ngày hôm nay
    if not filter_date_from:
        filter_date_from = selected_date
    if not filter_date_to:
        filter_date_to = selected_date
    # Chọn 1 ngày (from==to) → xem đúng ngày đó (đồng bộ phần POS theo ngày picker)
    if filter_date_from and filter_date_to and filter_date_from == filter_date_to:
        selected_date = filter_date_from

    user = current_user()
    user_role = str((user or {}).get("role", "staff")).strip()
    user_team_id = str((user or {}).get("team_id", "")).strip()
    can_choose_team = user_role in ("admin", "accountant")

    if can_choose_team:
        filter_team = request.args.get("team", "").strip()
    elif user_role == "leader":
        filter_team = user_team_id
    else:
        filter_team = ""

    # Cùng 1 ngày (from==to) → coi như xem theo NGÀY (không phải khoảng) → POS theo ngày, không cảnh báo real-time
    is_date_range = bool(filter_date_from and filter_date_to and filter_date_from != filter_date_to)
    try:
        if filter_date_from:
            datetime.strptime(filter_date_from, "%Y-%m-%d")
        if filter_date_to:
            datetime.strptime(filter_date_to, "%Y-%m-%d")
    except ValueError:
        is_date_range = False
        filter_date_from = ""
        filter_date_to = ""

    sync_status = request.args.get("sync_status", "").strip()
    sync_message = request.args.get("sync_message", "").strip()
    sync_status_label = "Đồng bộ"
    if sync_status == "ok":
        sync_status_label = "Đồng bộ thành công"
    elif sync_status == "error":
        sync_status_label = "Đồng bộ thất bại"
    allowed_shop_keys = get_allowed_shop_keys_for_current_user()

    if not is_date_range:
        if selected_date and is_db_order_status_enabled() and should_auto_sync_missing_order_status_date(
            selected_date, allowed_shop_keys=allowed_shop_keys
        ):
            auto_sync_missing_order_status_for_date(selected_date)  # im lặng, không show thông báo

    # Versioned (2026-06-11): lọc team/NV theo NGƯỜI GIỮ SHOP TRONG KỲ đang xem —
    # shop chuyển chủ (vd Ken → Minh Đạt hiệu lực 7/6) lọc tháng 5 vẫn nằm
    # team-ken với đầy đủ doanh thu cũ, khớp bảng % ads/DT từng NV phía trên.
    shop_team_map_range: Dict[str, set] = {}
    staff_shop_map: Dict[str, set] = {}
    if filter_team or filter_staff:
        _rng_from = filter_date_from if is_date_range else selected_date
        _rng_to = filter_date_to if is_date_range else selected_date
        try:
            from app_ctx import load_shop_user_team_map_for_range
            shop_team_map_range, staff_shop_map = load_shop_user_team_map_for_range(
                _rng_from, _rng_to)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("dashboard: lỗi map shop-team theo kỳ — fallback hiện tại")
            shop_team_map_range = {
                sk: {tc} for sk, tc in load_shop_team_map_from_users().items()}
            for u in load_users():
                uname = str(u.get("username", "")).strip()
                if uname == filter_staff:
                    for sk in u.get("assigned_shops", []):
                        sk = str(sk).strip()
                        if sk and sk != "*":
                            staff_shop_map.setdefault(uname, set()).add(sk)
                    break

    filtered_shop_keys = allowed_shop_keys
    if filter_team:
        team_shops = {sk for sk, tcs in shop_team_map_range.items() if filter_team in tcs}
        if filtered_shop_keys is None:
            filtered_shop_keys = team_shops
        else:
            filtered_shop_keys = filtered_shop_keys & team_shops
    if filter_staff and staff_shop_map.get(filter_staff):
        staff_shops = staff_shop_map[filter_staff]
        if filtered_shop_keys is None:
            filtered_shop_keys = staff_shops
        else:
            filtered_shop_keys = filtered_shop_keys & staff_shops

    if is_date_range and is_db_daily_dashboard_enabled():
        db_range_map = load_daily_revenue_map_date_range_from_db(
            filter_date_from, filter_date_to, allowed_shop_keys=filtered_shop_keys
        )
        # Bảng orders trống — dùng shop_order_status_cache (SUM increments + MAX cumulative)
        db_shop_status_range = load_order_status_from_status_cache_range(
            filter_date_from, filter_date_to, allowed_shop_keys=filtered_shop_keys
        ) or {}
        meta_map = get_visible_shop_meta_map()
        name_map = load_shop_name_map()
        shops: List[Dict[str, Any]] = []
        for shop_key, data in db_range_map.items():
            shop_meta = meta_map.get(shop_key, {})
            if shop_meta.get("status") != "active":
                continue
            shop_name = str(shop_meta.get("shop_name", "")).strip() or name_map.get(shop_key, shop_key)
            revenue = float(data.get("revenue", 0))
            orders = float(data.get("orders", 0))
            profit = float(data.get("profit", 0))
            ads_cost = float(data.get("ads_cost", 0))
            avg_profit = float(data.get("avg_profit", 0))
            if profit < 0:
                status = "Lỗ"
            elif revenue <= 0 or orders <= 0:
                status = "Cảnh báo"
            elif revenue < 1_000_000:
                status = "Cần xem"
            elif revenue >= 10_000_000:
                status = "Tốt"
            else:
                status = "Ổn định"
            shop_st = db_shop_status_range.get(shop_key, {})
            shops.append({
                "shop_key": shop_key,
                "shop_name": shop_name,
                "business_type": (shop_meta.get("business_type") or "cty").lower(),
                "revenue": revenue,
                "orders": orders,
                "profit": profit,
                "ads_cost": ads_cost,
                "avg_profit": avg_profit,
                "new_orders": int(shop_st.get("new_orders", 0)),
                "confirmed_orders": int(shop_st.get("confirmed_orders", 0)),
                "sent_orders": int(shop_st.get("sent_orders", 0)),
                "received_orders": int(shop_st.get("received_orders", 0)),
                "returning_orders": int(shop_st.get("returning_orders", 0)),
                "returned_orders": int(shop_st.get("returned_orders", 0)),
                "cancelled_orders": int(shop_st.get("cancelled_orders", 0)),
                "total_created_today": int(orders),
                "new_status_orders": int(shop_st.get("new_orders", 0)),
                "carrier_pickup_orders": 0,
                "fb_ads_cost": 0.0, "fb_ads_vat_amount": 0.0, "fb_ads_cost_after_vat": 0.0,
                "revenue_fmt": format_money(revenue),
                "orders_fmt": format_int(orders),
                "profit_fmt": format_money(profit),
                "ads_cost_fmt": format_money(ads_cost),
                "avg_profit_fmt": format_money(avg_profit),
                "fb_ads_cost_fmt": "0", "fb_ads_vat_amount_fmt": "0", "fb_ads_cost_after_vat_fmt": "0",
                "revenue_raw": f"{revenue} (range {filter_date_from}~{filter_date_to})",
                "profit_class": money_class(profit),
                "avg_profit_class": money_class(avg_profit),
                "status": status,
                "status_class": status_class(status),
                "date_key": f"{filter_date_from}~{filter_date_to}",
            })
        shops.sort(key=lambda x: x.get("revenue", 0), reverse=True)
        # Bổ sung total_active_returning / total_returned_all từ db_shop_status_range (đã dùng cache)
        for _s in shops:
            _cr = db_shop_status_range.get(_s.get("shop_key", ""), {})
            _s["total_active_returning"] = int(_cr.get("total_active_returning", 0) or 0)
            _s["total_returned_all"] = int(_cr.get("total_returned_all", 0) or 0)
        # Tổng kết từ shops (không query orders table vì bảng trống)
        _sum_new = sum(int(_s.get("new_orders", 0)) for _s in shops)
        order_status_summary = {
            "new": _sum_new,
            "new_status": _sum_new,
            "confirmed": sum(int(_s.get("confirmed_orders", 0)) for _s in shops),
            "sent": sum(int(_s.get("sent_orders", 0)) for _s in shops),
            "received": sum(int(_s.get("received_orders", 0)) for _s in shops),
            "returning": sum(int(_s.get("returning_orders", 0)) for _s in shops),
            "returned": sum(int(_s.get("returned_orders", 0)) for _s in shops),
            "cancelled": sum(int(_s.get("cancelled_orders", 0)) for _s in shops),
            "total_active_returning": sum(int(_s.get("total_active_returning", 0)) for _s in shops),
            "total_returned_all": sum(int(_s.get("total_returned_all", 0)) for _s in shops),
        }
    else:
        # [CPQC] Bảng Kinh Doanh/Đơn hàng LUÔN theo ngày đang xem (mặc định = hôm nay),
        # không dùng số tích lũy toàn thời gian. Top "Đơn hàng — Pancake POS" vẫn tích lũy (biến live_* riêng).
        shops = load_all_shops(selected_date, allowed_shop_keys=filtered_shop_keys, cumulative_status=False)
        if selected_date:
            # Tổng kết từ shops list đã load (không gọi thêm 34 API calls nữa)
            order_status_summary = {
                "new": sum(int(s.get("new_status_orders", 0) or 0) for s in shops),
                "new_status": sum(int(s.get("new_status_orders", 0) or 0) for s in shops),
                "confirmed": sum(int(s.get("confirmed_orders", 0) or 0) for s in shops),
                "sent": sum(int(s.get("sent_orders", 0) or 0) for s in shops),
                "received": sum(int(s.get("received_orders", 0) or 0) for s in shops),
                "returning": sum(int(s.get("returning_orders", 0) or 0) for s in shops),
                "returned": sum(int(s.get("returned_orders", 0) or 0) for s in shops),
                "cancelled": sum(int(s.get("cancelled_orders", 0) or 0) for s in shops),
                "total_active_returning": sum(int(s.get("total_active_returning", 0) or 0) for s in shops),
                "total_returned_all": sum(int(s.get("total_returned_all", 0) or 0) for s in shops),
            }
        else:
            order_status_summary = {"new": 0, "new_status": 0, "confirmed": 0, "sent": 0, "received": 0, "returning": 0, "returned": 0, "cancelled": 0}

    selected_month = parse_month_value(selected_date[:7] if selected_date else None)
    if is_dashboard_lightweight_home_enabled():
        lightweight_counts = get_home_alert_counts_cached(selected_month, allowed_shop_keys=allowed_shop_keys)
        ads_delay_count = int(lightweight_counts.get("ads_delay_count", 0) or 0)
        monthly_loss_count = int(lightweight_counts.get("monthly_loss_count", 0) or 0)
        monthly_total_loss_fmt = str(lightweight_counts.get("monthly_total_loss_fmt", "--"))
    else:
        ads_delay_data = build_ads_delay_data(allowed_shop_keys=allowed_shop_keys)
        monthly_loss_data = build_monthly_loss_data(selected_month, allowed_shop_keys=allowed_shop_keys)
        ads_delay_count = len(ads_delay_data["affected_shops"])
        monthly_loss_count = len(monthly_loss_data["losing_shops"])
        monthly_total_loss_fmt = monthly_loss_data["monthly_total_loss_fmt"]

    carrier_pickup_total = (
        sum_carrier_pickup_orders_dashboard(selected_date, allowed_shop_keys) if (selected_date and not is_date_range) else None
    )
    if carrier_pickup_total is not None:
        carrier_pickup_orders = format_int(carrier_pickup_total)
        carrier_pickup_orders_hint = "Từ DB kho vật lý (carrier_picked_up_at)"
    else:
        carrier_pickup_orders = "—"
        carrier_pickup_orders_hint = "Chọn 1 ngày cụ thể để xem" if is_date_range else "DB kho vật lý chưa có dữ liệu ngày này"

    # Tỷ lệ hoàn per shop = TÍCH LŨY từ snapshot Pancake live (status 5 / (status 2 + 3)).
    # Số hoàn không có dữ liệu theo từng ngày (returned_orders increment luôn 0) → tỷ lệ hoàn
    # chỉ có nghĩa ở dạng tích lũy. Dùng cùng nguồn với tỷ lệ hoàn tổng (return_rate_fmt) →
    # nhất quán, và hiển thị được cả khi lọc theo khoảng ngày (trước đây bị ép "—").
    _live_pos_early = load_live_pos_status()
    _live_shop_counts = {
        str(_sr.get("shop_key") or ""): (_sr.get("counts") or {})
        for _sr in (_live_pos_early.get("shop_results") or [])
        if _sr.get("ok")
    }
    for _s in shops:
        _c          = _live_shop_counts.get(_s.get("shop_key", ""), {})
        _sent_s     = int(_c.get("2", 0) or 0)  # đang gửi
        _recv_s     = int(_c.get("3", 0) or 0)  # đã nhận
        _ret_s      = int(_c.get("5", 0) or 0)  # đã hoàn xong
        _xuat_kho_s = _sent_s + _recv_s
        if _xuat_kho_s > 0:
            _r = _ret_s / _xuat_kho_s
            _s["shop_return_rate"] = f"{_r * 100:.1f}%"
            _s["shop_return_rate_color"] = "#dc3545" if _r > 0.20 else "#fd7e14" if _r > 0.12 else "#198754"
            _d = _recv_s / _xuat_kho_s
            _s["shop_delivery_rate"] = f"{_d * 100:.1f}%"
            _s["shop_delivery_rate_color"] = "#dc3545" if _d < 0.80 else "#fd7e14" if _d < 0.88 else "#198754"
        else:
            _s["shop_return_rate"] = "—"
            _s["shop_return_rate_color"] = "#6c757d"
            _s["shop_delivery_rate"] = "—"
            _s["shop_delivery_rate_color"] = "#6c757d"

        # Tỷ lệ phát = đơn đã đẩy đi / tổng đơn tạo
        _total_created_s = int(_s.get("total_created_today", 0) or 0)
        _da_day_di_s = _sent_s + _recv_s + _ret_s
        if _total_created_s > 0:
            _dp = _da_day_di_s / _total_created_s
            _s["shop_dispatch_rate"] = f"{_dp * 100:.1f}%"
            _s["shop_dispatch_rate_color"] = "#198754" if _dp >= 0.50 else "#fd7e14" if _dp >= 0.30 else "#dc3545"
        else:
            _s["shop_dispatch_rate"] = "—"
            _s["shop_dispatch_rate_color"] = "#6c757d"

    kho_shipping = get_kho_shipping_stats(filtered_shop_keys)
    kho_waiting_fmt            = format_int(kho_shipping["waiting"])
    kho_shipped_fmt            = format_int(kho_shipping["shipped"])
    kho_confirmed_fmt          = format_int(kho_shipping["confirmed"])
    kho_outbound_confirmed_fmt = format_int(kho_shipping.get("outbound_confirmed", 0))
    kho_returns_received_fmt   = format_int(kho_shipping.get("returns_received", 0))
    # Helper: lọc live_pos status_counts theo allowed_shop_keys từ per-shop breakdown
    def _filtered_live_counts(lp_data: dict, shop_keys) -> dict:
        if shop_keys is None:
            return lp_data.get("status_counts", {})
        shop_results = lp_data.get("shop_results", [])
        if not shop_results:
            # Chưa có per-shop data → không hiển thị số của shop khác cho user bị giới hạn
            return {}
        filtered: dict = {}
        for _sr in shop_results:
            if _sr.get("ok") and _sr.get("shop_key") in shop_keys:
                for _k, _v in _sr.get("counts", {}).items():
                    filtered[_k] = filtered.get(_k, 0) + int(_v or 0)
        return filtered

    # Tỷ lệ hoàn = đơn hoàn / đơn đã xuất kho (từ live Pancake cache — tích lũy toàn thời gian)
    # Đã xuất kho = đang gửi (status 2) + đã nhận (status 3)
    # _live_pos_early đã load ở trên (phần tỷ lệ hoàn per shop) — tái dùng, không load lại.
    _live_counts_early = _filtered_live_counts(_live_pos_early, filtered_shop_keys)
    _sent_raw  = int(_live_counts_early.get("2", 0) or 0)  # status=2: đang gửi
    _nhan_raw  = int(_live_counts_early.get("3", 0) or 0)  # status=3: đã nhận
    _hoan_raw  = int(_live_counts_early.get("5", 0) or 0)  # status=5: đã hoàn xong
    _xuat_kho_raw = _sent_raw + _nhan_raw                  # đã xuất kho = đang gửi + đã nhận
    if _xuat_kho_raw > 0:
        _rate_val = _hoan_raw / _xuat_kho_raw
        return_rate_fmt = f"{_rate_val * 100:.1f}%"
        return_rate_color = "#dc3545" if _rate_val > 0.20 else "#fd7e14" if _rate_val > 0.12 else "#198754"
    else:
        return_rate_fmt = "—"
        return_rate_color = "#6c757d"
    # Tỷ lệ thực tế = đã nhận / đã xuất kho (tích lũy toàn thời gian)
    if _xuat_kho_raw > 0:
        _del_rate_val = _nhan_raw / _xuat_kho_raw
        delivery_rate_fmt = f"{_del_rate_val * 100:.1f}%"
        delivery_rate_color = "#dc3545" if _del_rate_val < 0.80 else "#fd7e14" if _del_rate_val < 0.88 else "#198754"
    else:
        delivery_rate_fmt = "—"
        delivery_rate_color = "#6c757d"
    # Tỷ lệ phát = đơn đã đẩy đi / tổng đơn
    _da_day_di_g = _sent_raw + _nhan_raw + _hoan_raw
    _tong_g = sum(int(v or 0) for v in _live_counts_early.values())
    if _tong_g > 0:
        _dp_g = _da_day_di_g / _tong_g
        dispatch_rate_fmt = f"{_dp_g * 100:.1f}%"
        dispatch_rate_color = "#198754" if _dp_g >= 0.50 else "#fd7e14" if _dp_g >= 0.30 else "#dc3545"
    else:
        dispatch_rate_fmt = "—"
        dispatch_rate_color = "#6c757d"

    # Tỷ lệ hoàn THEO KỲ (khối xanh) — cùng công thức: đã hoàn / (đang gửi + đã nhận)
    # nhưng dùng order_status_summary in-period (sent/received/returned theo kỳ).
    _ky_sent = int(order_status_summary.get("sent", 0) or 0)
    _ky_received = int(order_status_summary.get("received", 0) or 0)
    _ky_returned = int(order_status_summary.get("returned", 0) or 0)
    _ky_xuat = _ky_sent + _ky_received
    if _ky_xuat > 0:
        _ky_rate = _ky_returned / _ky_xuat
        ky_return_rate_fmt = f"{_ky_rate * 100:.1f}%"
        ky_return_rate_color = "#dc3545" if _ky_rate > 0.20 else "#fd7e14" if _ky_rate > 0.12 else "#198754"
    else:
        ky_return_rate_fmt = "—"
        ky_return_rate_color = "#6c757d"
    # Tỷ lệ thực tế THEO KỲ
    if _ky_xuat > 0:
        _ky_del_rate = _ky_received / _ky_xuat
        ky_delivery_rate_fmt = f"{_ky_del_rate * 100:.1f}%"
        ky_delivery_rate_color = "#dc3545" if _ky_del_rate < 0.80 else "#fd7e14" if _ky_del_rate < 0.88 else "#198754"
    else:
        ky_delivery_rate_fmt = "—"
        ky_delivery_rate_color = "#6c757d"
    # Tỷ lệ phát THEO KỲ
    _ky_new = int(order_status_summary.get("new", 0) or 0) + int(order_status_summary.get("confirmed", 0) or 0)
    _ky_tong = _ky_sent + _ky_received + _ky_returned + _ky_new
    if _ky_tong > 0:
        _ky_dp = (_ky_sent + _ky_received + _ky_returned) / _ky_tong
        ky_dispatch_rate_fmt = f"{_ky_dp * 100:.1f}%"
        ky_dispatch_rate_color = "#198754" if _ky_dp >= 0.50 else "#fd7e14" if _ky_dp >= 0.30 else "#dc3545"
    else:
        ky_dispatch_rate_fmt = "—"
        ky_dispatch_rate_color = "#6c757d"

    live_pos = _live_pos_early  # tái dùng, không load lại
    live_synced_at = str(live_pos.get("synced_at", "") or "")
    # live_shop_count: nếu user bị giới hạn shop → đếm số shop được phép có dữ liệu
    if filtered_shop_keys is not None:
        live_shop_count = sum(
            1 for _sr in live_pos.get("shop_results", [])
            if _sr.get("ok") and _sr.get("shop_key") in filtered_shop_keys
        )
    else:
        live_shop_count = int(live_pos.get("shop_count", 0) or 0)
    live_error_count = int(live_pos.get("error_count", 0) or 0)
    # Dùng DB cache theo ngày cho Live POS (mặc định = hôm nay nếu URL không có ?date=).
    # _sum_cache đã cộng tổng tất cả shop → nhiều shop vẫn đúng.
    # Khi chọn KHOẢNG ngày: SUM theo ngày cho kết quả sai (1 đơn đang giao
    # 10 ngày bị đếm 10 lần) → dùng số tích lũy thực từ live_pos_status.json.
    if selected_date and not is_date_range:
        dashboard_status_cache_map = load_order_status_from_status_cache(
            selected_date, allowed_shop_keys=filtered_shop_keys
        )
    else:
        dashboard_status_cache_map = None

    if dashboard_status_cache_map:
        live_date_mode = True
        def _sum_cache(field):
            return sum(int(v.get(field, 0) or 0) for v in dashboard_status_cache_map.values())
        # cache confirmed_orders = Pancake status 9 = "Chờ chuyển hàng" (KHÔNG phải "Đã xác nhận").
        # → đưa vào counts["9"] (Chờ chuyển hàng); live_confirmed (Xác nhận, status 1/7) cache không tách → 0.
        live_confirmed = 0
        live_counts = {
            "0": _sum_cache("new_orders"),
            "1": 0,
            "2": _sum_cache("sent_orders"),
            "3": _sum_cache("received_orders"),
            "4": _sum_cache("returning_orders"),
            "5": _sum_cache("returned_orders"),
            "6": _sum_cache("cancelled_orders"),
            "7": 0,
            "9": _sum_cache("confirmed_orders"),
        }
    else:
        live_date_mode = False
        # Lọc theo allowed_shop_keys từ per-shop breakdown nếu user bị giới hạn shop
        live_counts = _filtered_live_counts(live_pos, filtered_shop_keys)
        live_confirmed = int(live_counts.get("7", 0) or 0)  # POS key 7 = Đã xác nhận

    total_revenue_value = sum(s["revenue"] for s in shops)
    total_orders_value = sum(s["orders"] for s in shops)
    total_created_today_value = sum(int(s.get("total_created_today", 0) or 0) for s in shops)
    total_profit_value = sum(s["profit"] for s in shops)
    total_ads_cost_value = sum(s["ads_cost"] for s in shops)
    avg_profit_weighted_num = sum(
        float(s.get("avg_profit", 0) or 0) * float(s.get("orders", 0) or 0) for s in shops
    )
    avg_profit_per_order_value = (
        avg_profit_weighted_num / total_orders_value if total_orders_value > 0 else 0.0
    )

    # ── Đồng bộ Lãi/Lỗ dashboard KHỚP báo cáo Lãi/Lỗ (Quảng cáo › Lãi/Lỗ) — sếp 05/08:
    #    Chi phí QC lấy TRỰC TIẾP từ FB ads theo page (không phải POS ads_cost = 0);
    #    Lãi/Lỗ = Lợi nhuận POS − CP ads (Win+Test) − CP khác (75k/đơn).
    #    Chỉ áp khi xem TỔNG (không lọc team/NV) vì báo cáo tính toàn công ty.
    try:
        from modules.chi_phi_qc import _lai_lo_report
        from db import query_one as _qo_ll
        _ll_from = filter_date_from if is_date_range else (selected_date or "")
        _ll_to = filter_date_to if is_date_range else (selected_date or "")
        if _ll_from and _ll_to:
            _rep = _lai_lo_report(_ll_from, _ll_to)
            if filter_staff:
                _u = _qo_ll("SELECT id FROM users WHERE username=%s", (filter_staff,))
                _nvid = str(_u[0]) if _u else None
                _ll_rows = [r for r in _rep["by_nv"] if r["key"] == _nvid]
            elif filter_team:
                _t = _qo_ll("SELECT COALESCE(NULLIF(team_name,''),team_code) FROM teams WHERE team_code=%s",
                            (filter_team,))
                _tname = _t[0] if _t else filter_team
                _ll_rows = [r for r in _rep["by_team"] if r["key"] == _tname]
            else:
                _ll_rows = _rep["by_page"]
            total_ads_cost_value = sum(r["cpqc"] + r["cp_test"] for r in _ll_rows)
            total_profit_value = sum(r["lai_lo"] for r in _ll_rows)
            # Khi lọc team/NV → Doanh thu cũng lấy theo report (shop có thể trống)
            if filter_team or filter_staff:
                total_revenue_value = sum(r["rev"] for r in _ll_rows)
    except Exception:
        pass

    # % ads/DT + bậc hoa hồng 2B — NV lọc theo mình là thấy ngay đang ở bậc lương nào
    ads_dt_pct = (total_ads_cost_value / total_revenue_value * 100) if total_revenue_value > 0 else 0.0
    bac_2b_pct = None
    if total_revenue_value > 0 and total_ads_cost_value > 0:
        try:
#            from modules.salary_2b import get_config as _s2b_cfg, _tier_pick as _s2b_tier
            bac_2b_pct = _s2b_tier(ads_dt_pct, _s2b_cfg()["tiers"])
        except Exception:
            bac_2b_pct = None

    # Bảng % ads/DT TỪNG NV khi lọc theo team — leader nhìn phát biết ai đốt ads kéo team
    nv_ads_breakdown = []
    if filter_team and total_ads_cost_value > 0:
        try:
#            from modules.salary_2b import get_config as _s2b_cfg2, _tier_pick as _s2b_tier2
            from db import query_all as _qa
            if is_date_range:
                _d1, _d2 = filter_date_from, filter_date_to
            else:
                _d1 = _d2 = selected_date or None
            if _d1 and _d2:
                _tiers = _s2b_cfg2()["tiers"]
                for un, fn, dt, ads in _qa("""
                    SELECT u.username, u.full_name,
                           SUM(m.gross_revenue), SUM(m.ads_cost)
                    FROM daily_shop_metrics m
                    JOIN user_shop_assignments usa ON usa.shop_id = m.shop_id
                         AND m.metric_date::date >= usa.assigned_from
                         AND (usa.assigned_to IS NULL OR m.metric_date::date <= usa.assigned_to)
                    JOIN users u ON u.id = usa.user_id
                    JOIN teams t ON t.id = u.team_id
                    WHERE t.team_code = %s
                      AND m.metric_date::date >= %s::date AND m.metric_date::date <= %s::date
                    GROUP BY 1, 2
                    HAVING SUM(m.gross_revenue) > 0""", (filter_team, _d1, _d2)):
                    dt, ads = float(dt or 0), float(ads or 0)
                    pct = ads / dt * 100 if dt else 0.0
                    nv_ads_breakdown.append({
                        "username": un, "full_name": fn or un,
                        "dt": format_money(dt), "ads": format_money(ads),
                        "pct": pct, "bac": _s2b_tier2(pct, _tiers) if ads > 0 else None,
                    })
                nv_ads_breakdown.sort(key=lambda x: -x["pct"])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("dashboard: lỗi build nv_ads_breakdown")
            nv_ads_breakdown = []

    active_shops = sum(1 for s in shops if s["orders"] > 0 or s["revenue"] > 0)
    if is_date_range:
        selected_date_label = f"{filter_date_from} → {filter_date_to}"
    else:
        selected_date_label = selected_date or "Ngày mới nhất trong từng shop"
    revenue_scope_text = f"Của {selected_date_label}"

    # Hiện ĐẦY ĐỦ team + nhân viên (sếp 05/08) — không chỉ team/NV có shop.
    try:
        from db import query_all as _qa_ls
        team_list = [{"code": r[0], "name": r[1] or r[0]}
                     for r in _qa_ls("SELECT team_code, COALESCE(NULLIF(team_name,''),team_code) "
                                     "FROM teams WHERE status='active' ORDER BY team_name")]
        staff_list = [{"username": r[0], "team_id": (str(r[1]) if r[1] is not None else "")}
                      for r in _qa_ls("SELECT username, team_id FROM users "
                                      "WHERE status='active' ORDER BY username")]
    except Exception:
        team_list = load_team_list_for_filter()
        staff_list = load_staff_list_for_filter()

    _is_admin = is_admin_user()
    try:
        import shop_types as _shop_types
        _biz_types = _shop_types.get_shop_business_types(active_only=False)
        _biz_type_map = {t["code"]: t for t in _biz_types}
    except Exception:
        _biz_types, _biz_type_map = [], {}
    body = render_template_string(
        DASHBOARD_BODY,
        shop_business_types=_biz_types,
        shop_type_map=_biz_type_map,
        is_admin_dashboard=_is_admin,
        shop_errors=get_shop_errors() if _is_admin else [],
        dashboard_data_source=(
            "DB (daily+status)"
            if is_db_daily_dashboard_enabled() and is_db_order_status_enabled()
            else ("DB (daily) + fallback" if is_db_daily_dashboard_enabled() else "JSON/API fallback")
        ),
        total_shops=len(shops),
        active_shops=active_shops,
        updated_at=now_hcm().strftime("%d/%m/%Y %H:%M"),
        ads_delay_count=ads_delay_count,
        monthly_loss_count=monthly_loss_count,
        monthly_total_loss_fmt=monthly_total_loss_fmt,
        selected_month=selected_month,
        total_revenue=format_money(total_revenue_value),
        total_orders=format_int(total_orders_value),
        new_orders=total_created_today_value,
        confirmed_orders=order_status_summary["confirmed"],
        sent_orders=order_status_summary["sent"],
        carrier_pickup_orders=carrier_pickup_orders,
        carrier_pickup_orders_hint=carrier_pickup_orders_hint,
        kho_waiting=kho_waiting_fmt,
        kho_shipped=kho_shipped_fmt,
        kho_confirmed=kho_confirmed_fmt,
        kho_outbound_confirmed=kho_outbound_confirmed_fmt,
        kho_returns_received=kho_returns_received_fmt,
        return_rate_fmt=return_rate_fmt,
        return_rate_color=return_rate_color,
        received_orders=order_status_summary["received"],
        returned_orders=order_status_summary["returned"],
        returning_orders=order_status_summary.get("returning", 0),
        cancelled_orders=order_status_summary.get("cancelled", 0),
        # Chọn ngày → "Đang hoàn về"/"Đã hoàn xong" tính THEO NGÀY (status 4/5 của ngày,
        # giống các cột khác); không chọn ngày → giữ số tích lũy toàn thời gian.
        total_active_returning=(int(live_counts.get("4", 0) or 0) if live_date_mode
                                else order_status_summary.get("total_active_returning", 0)),
        total_returned_all=(int(live_counts.get("5", 0) or 0) if live_date_mode
                            else order_status_summary.get("total_returned_all", 0)),
        new_status_orders=order_status_summary.get("new_status", 0),
        total_ads_cost=format_money(total_ads_cost_value),
        ads_dt_pct=ads_dt_pct,
        bac_2b_pct=bac_2b_pct,
        nv_ads_breakdown=nv_ads_breakdown,
        total_profit=format_money(total_profit_value),
        total_profit_class=money_class(total_profit_value),
        avg_profit_per_order=format_money(avg_profit_per_order_value),
        avg_profit_class=money_class(avg_profit_per_order_value),
        shops=shops,
        alerts=build_alerts(shops),
        selected_date=selected_date or "",
        selected_date_label=selected_date_label,
        revenue_scope_text=revenue_scope_text,
        sync_message=sync_message,
        sync_status_label=sync_status_label,
        filter_team=filter_team,
        filter_staff=filter_staff,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        team_list=team_list,
        staff_list=staff_list,
        is_date_range=is_date_range,
        ky_return_rate_fmt=ky_return_rate_fmt,
        ky_return_rate_color=ky_return_rate_color,
        delivery_rate_fmt=delivery_rate_fmt,
        delivery_rate_color=delivery_rate_color,
        dispatch_rate_fmt=dispatch_rate_fmt,
        dispatch_rate_color=dispatch_rate_color,
        ky_delivery_rate_fmt=ky_delivery_rate_fmt,
        ky_delivery_rate_color=ky_delivery_rate_color,
        ky_dispatch_rate_fmt=ky_dispatch_rate_fmt,
        ky_dispatch_rate_color=ky_dispatch_rate_color,
        can_choose_team=can_choose_team,
        user_role=user_role,
        live_s0=format_int(live_counts.get("0", 0)),
        live_s1=format_int(live_counts.get("1", 0)),
        live_s2=format_int(live_counts.get("2", 0)),
        live_s3=format_int(live_counts.get("3", 0)),
        live_s4=format_int(live_counts.get("4", 0)),
        live_s5=format_int(live_counts.get("5", 0)),
        live_s6=format_int(live_counts.get("6", 0)),
        live_s79=format_int(int(live_counts.get("9", 0) or 0)),
        live_total=format_int(sum(int(live_counts.get(str(k), 0) or 0) for k in [0,1,2,3,4,5,6,7,9])),
        live_synced_at=live_synced_at,
        live_shop_count=live_shop_count,
        live_error_count=live_error_count,
        live_has_data=bool(live_counts),
        live_date_mode=live_date_mode,
        live_confirmed=format_int(live_confirmed),
        can_view_profit_kpi=(str(user_role).lower() != "staff"),
    )
    return render_template_string(PAGE_TEMPLATE, title="Tiểu Hiềm Software", body=body)


@dashboard_bp.route("/sync-pos-now", methods=["POST"])
@login_required
def sync_pos_now():
    selected_date = request.form.get("date", "").strip()
    if not selected_date:
        selected_date = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except Exception:
        return redirect(url_for("dashboard.dashboard", date=selected_date, sync_status="error", sync_message="Ngày không hợp lệ."))

    auto_enabled_db_mode = False
    if not is_db_daily_dashboard_enabled():
        os.environ["WEB_DAILY_DASHBOARD_DB_ENABLED"] = "1"
        auto_enabled_db_mode = True

    if not os.getenv("DATABASE_URL", "").strip():
        return redirect(
            url_for(
                "dashboard",
                date=selected_date,
                sync_status="error",
                sync_message="Thiếu DATABASE_URL để đồng bộ vào DB.",
            )
        )

    force = request.form.get("force", "0") == "1"
    with SYNC_IN_PROGRESS_LOCK:
        if selected_date in SYNC_IN_PROGRESS_DATES:
            if not force:
                return redirect(
                    url_for(
                        "dashboard",
                        date=selected_date,
                        sync_status="error",
                        sync_message="Đang sync, vui lòng chờ...",
                    )
                )
            SYNC_IN_PROGRESS_DATES.discard(selected_date)
        SYNC_IN_PROGRESS_DATES.add(selected_date)

    script_path = os.path.join(BASE_DIR, "run_auto_refresh_all_data.sh")

    def _run_in_background(date_str: str, script: str, env: dict) -> None:
        try:
            subprocess.run(
                ["bash", script, date_str],
                cwd=BASE_DIR,
                check=False,
                timeout=1800,
                env=env,
            )
        except Exception:
            pass
        finally:
            clear_home_alert_counts_cache()
            with SYNC_IN_PROGRESS_LOCK:
                SYNC_IN_PROGRESS_DATES.discard(date_str)

    bg = threading.Thread(
        target=_run_in_background,
        args=(selected_date, script_path, os.environ.copy()),
        daemon=True,
        name=f"sync-pos-{selected_date}",
    )
    bg.start()

    return redirect(
        url_for(
            "dashboard",
            date=selected_date,
            sync_status="ok",
            sync_message=(
                f"Đang đồng bộ dữ liệu ngày {selected_date} trong nền — dashboard sẽ cập nhật sau vài phút."
                + (" (Đã tự bật DB mode)" if auto_enabled_db_mode else "")
            ),
        )
    )


@dashboard_bp.route("/api/sync-live-status", methods=["POST"])
@login_required
def api_sync_live_status():
    """Fetch cumulative Pancake POS status counts cho tất cả shops và lưu cache."""
    try:
        data = fetch_live_pos_status()
        save_live_pos_status(data)
        return jsonify({
            "ok": True,
            "synced_at": data.get("synced_at", ""),
            "shop_count": data.get("shop_count", 0),
            "error_count": data.get("error_count", 0),
            "status_counts": data.get("status_counts", {}),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@dashboard_bp.route("/api/admin/clear-all", methods=["POST"])
@login_required
def api_admin_clear_all():
    """Admin only: xoá toàn bộ dữ liệu kể cả SKU/sản phẩm kho."""
    if not is_admin_user():
        return jsonify({"ok": False, "error": "Không có quyền"}), 403
    import glob as _glob
    try:
        from db import get_conn as _get_conn
        tables = [
            "order_items",
            "orders",
            "daily_shop_metrics",
            "shop_order_status_cache",
            "wh_return_receipt_items",
            "wh_return_receipts",
            "wh_reconciliation_items",
            "wh_reconciliation_runs",
            "wh_stocktake_items",
            "wh_stocktakes",
            "wh_inventory_ledger",
            "wh_inventory_balance",
            "wh_stock_movements",
            "wh_inventory",
            "wh_shop_inventory",
            "wh_outbound_requests",
            "wh_products",
        ]
        summary = []
        with _get_conn() as conn:
            with conn.cursor() as cur:
                for t in tables:
                    cur.execute(
                        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=%s)", (t,)
                    )
                    if cur.fetchone()[0]:
                        cur.execute(f"DELETE FROM {t}")
                        summary.append(f"{t}: {cur.rowcount}")
                    else:
                        summary.append(f"{t}: [skip-no-table]")
        # Xoá file JSON cache
        for f in _glob.glob(os.path.join(BASE_DIR, "data_shop*.json")):
            try:
                os.remove(f)
            except Exception:
                pass
        live_cache = os.path.join(BASE_DIR, "live_pos_status.json")
        if os.path.exists(live_cache):
            with open(live_cache, "w") as fh:
                import json as _json
                _json.dump({}, fh)
        return jsonify({"ok": True, "message": "\n".join(summary)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@dashboard_bp.route("/api/admin/sync-all", methods=["POST"])
@login_required
def api_admin_sync_all():
    """Admin only: sync toàn bộ — doanh thu, đơn hàng, kho POS, kho vật lý."""
    if not is_admin_user():
        return jsonify({"ok": False, "error": "Không có quyền"}), 403
    import subprocess as _sub
    results = []
    errors = []
    py = sys.executable
    scripts_dir = os.path.join(BASE_DIR, "scripts")

    def _run(label, cmd, timeout=120):
        try:
            r = _sub.run(cmd, capture_output=True, text=True, timeout=timeout)
            last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            results.append(f"{label}: {last}")
            if r.returncode != 0:
                errors.append(f"{label} exit={r.returncode}: {r.stderr.strip()[-200:]}")
        except _sub.TimeoutExpired:
            errors.append(f"{label}: timeout")
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    # 1. Sync doanh thu daily từ Pancake → JSON
    _run("sync_pos", [py, os.path.join(BASE_DIR, "sync_pos.py")], timeout=180)
    # 2. Import JSON → DB daily_shop_metrics
    _run("bootstrap_daily", [py, os.path.join(scripts_dir, "bootstrap_daily_shop_metrics_from_json.py")], timeout=60)
    # 3. Sync cache trạng thái đơn POS (7 ngày gần nhất)
    _run("sync_status_cache", [py, os.path.join(scripts_dir, "sync_order_status_cache.py"), "--days", "7"], timeout=120)
    # 4. Sync đơn hàng + order_items → DB
    _run("sync_orders", [py, os.path.join(scripts_dir, "sync_orders_order_items_to_db.py")], timeout=180)
    # 5. Sync kho outbound (xuất kho vật lý)
    _run("sync_kho_outbound", [py, os.path.join(scripts_dir, "sync_kho_outbound.py")], timeout=180)
    # 6. Sync hoàn kho vật lý
    _run("sync_wh_returns", [py, os.path.join(scripts_dir, "sync_wh_returns_pg.py")], timeout=120)
    # 7. Live POS status
    try:
        data = fetch_live_pos_status()
        save_live_pos_status(data)
        results.append(f"live_pos: ok ({data.get('shop_count', 0)} shops)")
    except Exception as exc:
        errors.append(f"live_pos: {exc}")

    ok = len(errors) == 0
    msg = "\n".join(results)
    if errors:
        msg += "\n⚠️ Lỗi:\n" + "\n".join(errors)
    return jsonify({"ok": ok, "message": msg})



