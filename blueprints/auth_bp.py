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

from flask import Blueprint, abort, render_template_string, render_template, request, url_for, redirect, session, g, send_file, jsonify
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



@auth_bp.route("/profile", methods=["GET"])
def profile():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login", next="/profile"))

    users = load_users()
    uid = session.get("user_id", "")
    me = next((u for u in users if u.get("id") == uid), None)
    if not me:
        return redirect(url_for("auth.login"))

    current_name = me.get("full_name") or me.get("username", "")
    username = me.get("username", "")
    role_label = {"admin":"Quản trị","leader":"Trưởng nhóm","staff":"Nhân viên",
                  "accountant":"Kế toán","kho":"Kho","manager":"Manager"}.get(me.get("role",""), me.get("role",""))

    return render_template("auth/profile.html",
                          title="Hồ sơ cá nhân",
                          current_name=current_name,
                          username=username,
                          role_label=role_label,
                          PAGE_TEMPLATE=PAGE_TEMPLATE)


@auth_bp.route("/profile/update-name", methods=["POST"])
def update_name():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login", next="/profile"))

    users = load_users()
    uid = session.get("user_id", "")
    me = next((u for u in users if u.get("id") == uid), None)
    if not me:
        return redirect(url_for("auth.login"))

    new_name = request.form.get("full_name", "").strip()
    if not new_name:
        return redirect(url_for("auth.profile"))

    me["full_name"] = new_name
    save_users(users)
    session["full_name"] = new_name
    return redirect(url_for("auth.profile"))


@auth_bp.route("/profile/change-password", methods=["POST"])
def change_password():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login", next="/profile"))

    users = load_users()
    uid = session.get("user_id", "")
    me = next((u for u in users if u.get("id") == uid), None)
    if not me:
        return redirect(url_for("auth.login"))

    old_pw  = request.form.get("old_password", "").strip()
    new_pw  = request.form.get("new_password", "").strip()
    new_pw2 = request.form.get("new_password2", "").strip()

    if not old_pw or not new_pw or not new_pw2:
        return redirect(url_for("auth.profile"))
    if not _check_user_password(old_pw, me):
        return redirect(url_for("auth.profile"))
    if new_pw != new_pw2:
        return redirect(url_for("auth.profile"))
    if len(new_pw) < 4:
        return redirect(url_for("auth.profile"))

    me["password"] = new_pw
    save_users(users)
    return redirect(url_for("auth.profile"))


