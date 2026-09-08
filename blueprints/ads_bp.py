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
    ADS_KPI_BODY, ADS_DELAY_BODY, MONTHLY_LOSS_BODY,
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

ads_bp = Blueprint("ads", __name__)

@ads_bp.route("/ads-kpi", methods=["GET", "POST"])
@login_required
def ads_kpi_dashboard():
    messages: List[Dict[str, str]] = []
    default_today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")

    def _parse_ymd(s: Optional[str]) -> Optional[str]:
        if not s or not str(s).strip():
            return None
        raw = str(s).strip()
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            return None

    def _fmt_vn(ymd: str) -> str:
        return datetime.strptime(ymd, "%Y-%m-%d").strftime("%d/%m/%Y")

    allowed_shop_keys = None if can_view_ads_global() else get_allowed_shop_keys_for_current_user()
    selected_shop_key = request.args.get("shop_key", "").strip()
    export_shop_key = request.args.get("shop", "").strip()
    export_employee = request.args.get("employee", "").strip()
    export_mode = request.args.get("mode", "").strip() or "detail"
    summary_group_by = request.args.get("group_by", "").strip() or "employee"
    export_warning_note = ""

    view_from = _parse_ymd(request.args.get("from", ""))
    view_to = _parse_ymd(request.args.get("to", ""))
    date_legacy = _parse_ymd(request.args.get("date", ""))

    if request.method == "POST":
        selected_shop_key = request.form.get("shop_key", "").strip() or selected_shop_key
        vf = _parse_ymd(request.form.get("view_from", ""))
        vt = _parse_ymd(request.form.get("view_to", ""))
        if vf and vt:
            view_from, view_to = vf, vt

    if not view_from and not view_to:
        view_from = view_to = date_legacy or default_today
    elif view_from and not view_to:
        view_to = view_from
    elif view_to and not view_from:
        view_from = view_to

    if view_from > view_to:
        view_from, view_to = view_to, view_from

    ads_kpi_is_range = view_from != view_to
    export_date_from = view_from
    export_date_to = view_to

    selected_date = _parse_ymd(request.args.get("date", "").strip()) or view_to
    if request.method == "POST":
        sync_sd = _parse_ymd(request.form.get("sync_date", ""))
        if sync_sd:
            selected_date = sync_sd

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        if action == "sync_fb_ads":
            if not is_admin_user():
                messages.append({"level": "Quyền truy cập", "text": "Chỉ admin mới có thể sync Facebook Ads."})
            else:
                sync_shop_key = request.form.get("sync_shop_key", "").strip()
                ok, text = run_facebook_ads_sync(selected_date, sync_shop_key)
                messages.append({"level": "Thành công" if ok else "Lỗi", "text": text})
                if ok and sync_shop_key:
                    latest_spend = get_fb_ads_spend_for_shop_date(sync_shop_key, selected_date)
                    if latest_spend is not None:
                        messages.append({
                            "level": "DB",
                            "text": f"DB updated: {sync_shop_key} | {selected_date} | CP Ads FB = {format_money(latest_spend)}",
                        })
        elif action == "export_ads_excel":
            export_date_from = request.form.get("export_date_from", "").strip() or selected_date
            export_date_to = request.form.get("export_date_to", "").strip() or selected_date
            export_shop_key = request.form.get("export_shop_key", "").strip()
            export_employee = request.form.get("export_employee", "").strip()
            export_mode = request.form.get("export_mode", "").strip() or "detail"
            summary_group_by = request.form.get("summary_group_by", "").strip() or "employee"
            if not can_view_ads_global():
                messages.append({"level": "Quyền truy cập", "text": "Chỉ admin/accountant mới có thể xuất Excel quảng cáo."})
            else:
                try:
                    datetime.strptime(export_date_from, "%Y-%m-%d")
                    datetime.strptime(export_date_to, "%Y-%m-%d")
                    filepath, filename, row_count, warnings = save_ads_export_excel(
                        date_from=export_date_from,
                        date_to=export_date_to,
                        mode=export_mode,
                        group_by=summary_group_by,
                        shop_filter_key=export_shop_key,
                        employee_filter=export_employee,
                        allowed_shop_keys=allowed_shop_keys,
                    )
                    export_warning_note = (
                        "Một số shop được gán UNMAPPED_OR_MULTI để tránh double count theo rule kế toán."
                        if warnings
                        else ""
                    )
                    messages.append({"level": "Xuất Excel", "text": f"Đã tạo file {filename} ({row_count} dòng)."})
                    return send_file(filepath, as_attachment=True, download_name=filename)
                except Exception as exc:
                    messages.append({"level": "Lỗi", "text": f"Xuất Excel thất bại: {exc}"})
    if ads_kpi_is_range:
        if not get_db_conn:
            shops = []
            messages.append({
                "level": "Dữ liệu",
                "text": "Xem tổng theo khoảng ngày cần DATABASE_URL và bảng daily_shop_metrics / fb_ads_daily_metrics.",
            })
        else:
            shops = load_shops_for_ads_kpi_range(view_from, view_to, allowed_shop_keys=allowed_shop_keys)
    else:
        shops = load_all_shops(view_from, allowed_shop_keys=allowed_shop_keys)
    if not selected_shop_key:
        if allowed_shop_keys is not None and len(allowed_shop_keys) == 1:
            selected_shop_key = next(iter(allowed_shop_keys))
        elif allowed_shop_keys is None and len(shops) == 1:
            only = str(shops[0].get("shop_key", "") or "").strip()
            if only:
                selected_shop_key = only
    ads_sync_shop_options = [
        {"shop_key": s.get("shop_key", ""), "shop_name": s.get("shop_name", s.get("shop_key", ""))}
        for s in shops
        if s.get("shop_key")
    ]
    export_employee_options = sorted({
        str(u.get("username", "")).strip()
        for u in load_users()
        if str(u.get("status", "active")).strip() == "active"
        and str(u.get("role", "staff")).strip() in {"staff", "leader"}
        and str(u.get("username", "")).strip()
    })
    export_employee_options.append("UNMAPPED_OR_MULTI")
    if ads_kpi_is_range:
        sync_meta = get_fb_ads_sync_meta_for_range(view_from, view_to, allowed_shop_keys=allowed_shop_keys)
    else:
        sync_meta = get_fb_ads_sync_meta_for_date(selected_date, allowed_shop_keys=allowed_shop_keys)
    if selected_shop_key:
        if ads_kpi_is_range:
            selected_shop_detail = get_fb_ads_account_breakdown_for_shop_range(
                selected_shop_key,
                view_from,
                view_to,
                allowed_shop_keys=allowed_shop_keys,
            )
        else:
            selected_shop_detail = get_fb_ads_account_breakdown_for_shop_date(
                selected_shop_key,
                selected_date,
                allowed_shop_keys=allowed_shop_keys,
            )
    else:
        selected_shop_detail = None
    ads_scope_note = ""
    if not can_view_ads_global():
        r = str(session.get("role", "")).strip()
        if r == "staff":
            ads_scope_note = (
                "Bạn là nhân viên (staff): chỉ được xem chi phí quảng cáo của các shop được gán trực tiếp cho tài khoản của bạn. "
                "Không xem được dữ liệu shop của người khác."
            )
        elif r == "leader":
            ads_scope_note = (
                "Bạn là leader: xem chi phí quảng cáo trong phạm vi cả team (shop gắn team_id và shop các thành viên trong team được phép truy cập)."
            )
    if ads_kpi_is_range:
        selected_date_label = f"{_fmt_vn(view_from)} – {_fmt_vn(view_to)}"
    else:
        selected_date_label = view_from
    try:
        import shop_types as _shop_types
        _ads_type_map = {t["code"]: t for t in _shop_types.get_shop_business_types(active_only=False)}
    except Exception:
        _ads_type_map = {}
    body = render_template_string(
        ADS_KPI_BODY,
        shop_type_map=_ads_type_map,
        is_admin_dashboard=is_admin_user(),
        shops=shops,
        ads_scope_note=ads_scope_note,
        messages=messages,
        selected_shop_key=selected_shop_key,
        selected_shop_detail=selected_shop_detail,
        ads_sync_shop_options=ads_sync_shop_options,
        export_employee_options=export_employee_options,
        export_date_from=export_date_from,
        export_date_to=export_date_to,
        export_shop_key=export_shop_key,
        export_employee=export_employee,
        export_mode=export_mode,
        summary_group_by=summary_group_by,
        export_warning_note=export_warning_note,
        can_export_ads_excel=can_view_ads_global(),
        ads_last_sync_text=sync_meta["last_sync_text"],
        ads_sync_summary_text=sync_meta["summary_text"],
        selected_date=selected_date,
        selected_date_label=selected_date_label,
        ads_kpi_is_range=ads_kpi_is_range,
        view_date_from=view_from,
        view_date_to=view_to,
    )
    return render_template_string(PAGE_TEMPLATE, title="KPI quảng cáo", body=body)


@ads_bp.route("/ads-delay")
@login_required
def ads_delay_dashboard():
    ads_delay_data = build_ads_delay_data(allowed_shop_keys=get_allowed_shop_keys_for_current_user())
    selected_shop_key = request.args.get("shop_key", "").strip()
    selected_detail = build_ads_delay_detail(selected_shop_key, ads_delay_data["affected_shops"]) if selected_shop_key else None
    body = render_template_string(
        ADS_DELAY_BODY,
        affected_shops=ads_delay_data["affected_shops"],
        affected_count=len(ads_delay_data["affected_shops"]),
        today_str=ads_delay_data["today_str"],
        cutoff_date_str=ads_delay_data["cutoff_date_str"],
        selected_detail=selected_detail,
    )
    return render_template_string(PAGE_TEMPLATE, title="QC chậm > 2 ngày", body=body)


@ads_bp.route("/loss-day-alert")
@ads_bp.route("/monthly-loss")
@login_required
def loss_day_alert_dashboard():
    selected_month = parse_month_value(request.args.get("month", "").strip() or None)
    monthly_loss_data = build_monthly_loss_data(selected_month, allowed_shop_keys=get_allowed_shop_keys_for_current_user())
    selected_shop_key = request.args.get("shop_key", "").strip()
    selected_detail = build_monthly_loss_detail(selected_shop_key, monthly_loss_data["losing_shops"]) if selected_shop_key else None
    body = render_template_string(
        MONTHLY_LOSS_BODY,
        selected_month=selected_month,
        selected_month_label=selected_month,
        affected_count=len(monthly_loss_data["losing_shops"]),
        monthly_total_loss_fmt=monthly_loss_data["monthly_total_loss_fmt"],
        losing_shops=monthly_loss_data["losing_shops"],
        selected_detail=selected_detail,
    )
    return render_template_string(PAGE_TEMPLATE, title="Shop lỗ theo POS", body=body)



