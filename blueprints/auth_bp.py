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

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard.dashboard")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = find_user(username)
        if user and str(user.get("status", "active")) != "active":
            error = "Tài khoản đang bị khóa."
        elif user and _check_user_password(password, user):
            session.permanent = True
            session["logged_in"] = True
            session["username"] = username
            session["role"] = str(user.get("role", "staff"))
            session["user_id"] = user.get("id")
            session["full_name"] = user.get("full_name", "") or username
            return redirect(next_url)
        elif username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
            # backward-compatible fallback for first-time setup
            session.permanent = True
            session["logged_in"] = True
            session["username"] = username
            session["role"] = "admin"
            return redirect(next_url)
        else:
            error = "Sai tài khoản hoặc mật khẩu."
    return render_template_string(LOGIN_TEMPLATE, error=error, next_url=next_url)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))



@auth_bp.route("/profile", methods=["GET", "POST"])
def profile():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login", next="/profile"))

    users = load_users()
    uid = session.get("user_id", "")
    me = next((u for u in users if u.get("id") == uid), None)
    if not me:
        return redirect(url_for("auth.login"))

    messages = []

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "update_name":
            new_name = request.form.get("full_name", "").strip()
            if not new_name:
                messages.append(("error", "Tên không được để trống."))
            else:
                me["full_name"] = new_name
                save_users(users)
                session["full_name"] = new_name
                # Đồng bộ cc_employees
                try:
                    from modules.cham_cong.cc_db import upsert_employee, get_employee
                    emp = get_employee(uid)
                    if emp:
                        upsert_employee(uid, new_name, emp.get("cc_role","sale"),
                                        emp.get("department",""), emp.get("phone",""),
                                        emp.get("position",""))
                except Exception:
                    pass
                messages.append(("success", f"Đã cập nhật tên thành: {new_name}"))

        elif action == "change_password":
            old_pw  = request.form.get("old_password", "").strip()
            new_pw  = request.form.get("new_password", "").strip()
            new_pw2 = request.form.get("new_password2", "").strip()
            if not old_pw or not new_pw or not new_pw2:
                messages.append(("error", "Vui lòng nhập đầy đủ thông tin."))
            elif not _check_user_password(old_pw, me):
                messages.append(("error", "Mật khẩu hiện tại không đúng."))
            elif new_pw != new_pw2:
                messages.append(("error", "Mật khẩu mới không khớp."))
            elif len(new_pw) < 4:
                messages.append(("error", "Mật khẩu mới phải có ít nhất 4 ký tự."))
            else:
                me["password"] = new_pw
                save_users(users)
                messages.append(("success", "Đã đổi mật khẩu thành công!"))

    current_name = me.get("full_name") or me.get("username", "")
    username = me.get("username", "")
    role_label = {"admin":"Quản trị","leader":"Trưởng nhóm","staff":"Nhân viên",
                  "accountant":"Kế toán","kho":"Kho","manager":"Manager"}.get(me.get("role",""), me.get("role",""))

    alerts_html = ""
    for lvl, txt in messages:
        color = "#d1fae5" if lvl == "success" else "#fee2e2"
        tc    = "#065f46" if lvl == "success" else "#991b1b"
        alerts_html += f'<div style="padding:10px 14px;border-radius:8px;background:{color};color:{tc};margin-bottom:10px;font-size:14px;">{txt}</div>'

    body = f"""
<div style="max-width:520px;margin:32px auto;padding:0 16px;">
  <div style="background:#fff;border-radius:16px;box-shadow:0 2px 16px rgba(0,0,0,.08);overflow:hidden;">
    <!-- Header -->
    <div style="background:linear-gradient(135deg,#f59e0b,#d97706);padding:24px;text-align:center;color:#fff;">
      <div style="width:64px;height:64px;border-radius:50%;background:rgba(255,255,255,.25);margin:0 auto 12px;display:flex;align-items:center;justify-content:center;font-size:28px;">
        <i class="bi bi-person-fill"></i>
      </div>
      <div style="font-size:20px;font-weight:700;">{current_name}</div>
      <div style="font-size:13px;opacity:.85;">@{username} &nbsp;·&nbsp; {role_label}</div>
    </div>

    <div style="padding:24px;">
      {alerts_html}

      <!-- Form 1: Cập nhật tên -->
      <div style="margin-bottom:24px;">
        <div style="font-size:15px;font-weight:700;margin-bottom:12px;color:#374151;">
          <i class="bi bi-person-badge"></i> Tên hiển thị
        </div>
        <form method="post">
          <input type="hidden" name="action" value="update_name">
          <div style="display:flex;gap:8px;">
            <input type="text" name="full_name" value="{current_name}"
                   placeholder="Nguyễn Văn Nam"
                   style="flex:1;padding:10px 12px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:14px;outline:none;"
                   onfocus="this.style.borderColor='#f59e0b'" onblur="this.style.borderColor='#e5e7eb'">
            <button type="submit"
                    style="padding:10px 18px;background:#f59e0b;color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:14px;">
              Lưu
            </button>
          </div>
        </form>
      </div>

      <hr style="border:none;border-top:1px solid #f3f4f6;margin:0 0 24px;">

      <!-- Form 2: Đổi mật khẩu -->
      <div>
        <div style="font-size:15px;font-weight:700;margin-bottom:12px;color:#374151;">
          <i class="bi bi-shield-lock"></i> Đổi mật khẩu
        </div>
        <form method="post">
          <input type="hidden" name="action" value="change_password">
          <div style="display:flex;flex-direction:column;gap:10px;">
            <input type="password" name="old_password" placeholder="Mật khẩu hiện tại"
                   style="padding:10px 12px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:14px;"
                   onfocus="this.style.borderColor='#f59e0b'" onblur="this.style.borderColor='#e5e7eb'">
            <input type="password" name="new_password" placeholder="Mật khẩu mới"
                   style="padding:10px 12px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:14px;"
                   onfocus="this.style.borderColor='#f59e0b'" onblur="this.style.borderColor='#e5e7eb'">
            <input type="password" name="new_password2" placeholder="Nhập lại mật khẩu mới"
                   style="padding:10px 12px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:14px;"
                   onfocus="this.style.borderColor='#f59e0b'" onblur="this.style.borderColor='#e5e7eb'">
            <button type="submit"
                    style="padding:10px;background:#1e40af;color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:14px;">
              <i class="bi bi-shield-check"></i> Đổi mật khẩu
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
  <div style="text-align:center;margin-top:16px;">
    <a href="/" style="color:#6b7280;font-size:13px;text-decoration:none;">
      <i class="bi bi-arrow-left"></i> Quay về trang chủ
    </a>
  </div>
</div>
"""
    return render_template_string(PAGE_TEMPLATE, title="Hồ sơ cá nhân", body=body)


