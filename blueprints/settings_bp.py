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

from flask import Blueprint, abort, render_template_string, request, url_for, redirect, session, g, send_file, jsonify, flash
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
    SETTINGS_BODY,
)
try:
    from db import get_conn as get_db_conn
except Exception:
    get_db_conn = None
try:
    import shop_types
except Exception:
    shop_types = None
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

# Nhãn tiếng Việt cho tính năng tự dò được (không có thì lấy luôn mã)
_PERM_NHAN = {
    "ads": "Quảng cáo (ads)", "budget_chat": "Chat ngân sách",
    "expense_chat": "Chat chi phí", "fraud_detect": "Kiểm soát gian lận",
    "hr": "Nhân sự (HR)", "ladipage": "Đơn LadiPage",
    "leader_brain": "Trợ lý Leader", "marketing_brain": "Trợ lý Marketing",
    "page_account": "Page & Tài khoản QC", "quan_ly_via": "Quản lý VIA",
    "salary_2b": "Lương 2B", "salary_b1": "Lương B1",
    "shop": "Chi tiết Shop", "teams_admin": "Quản lý Team",
    "kho_vat_ly": "Kho vật lý",
}

def _perm_missing_modules() -> list:
    """Module ĐANG CHẠY nhưng chưa có trong permissions.json.

    Không tự thêm — chỉ liệt kê để admin bấm Thêm, vì thêm vào là menu sẽ ẩn
    với các vai trò chưa được tick (dễ làm NV mất trang đang dùng).
    """
    try:
        from flask import current_app
        import perm_utils as _pu
        co = {m.get("key") for m in (_pu.load_perms().get("modules") or [])}
        bo_qua = {"static", "auth", "misc", "settings"}
        out = []
        for b in sorted(current_app.blueprints):
            if b in co or b in bo_qua:
                continue
            out.append({"key": b, "label": _PERM_NHAN.get(b, b)})
        return out
    except Exception:
        return []


settings_bp = Blueprint("settings", __name__)

logger = logging.getLogger(__name__)


def _sync_team_to_shops(team_id, shop_keys) -> int:
    """Đồng bộ team của user XUỐNG các shop user đó sở hữu → khớp tab Nhân sự ↔ Shop & Web.

    `users.team_id` và `shops.team_id` cùng là teams.id (số). Chỉ áp khi shop_keys là
    DANH SÁCH CỤ THỂ (bỏ qua "*" = xem-tất-cả, không phải sở hữu) và user có team.
    last-writer-wins: shop bị 2 user khác team gán thì lần sửa sau thắng.
    Trả số shop được đổi team. Không raise — lỗi DB chỉ log + trả 0.
    """
    if not team_id or not shop_keys or "*" in shop_keys or get_db_conn is None:
        return 0
    try:
        tid = int(str(team_id).strip())
    except (TypeError, ValueError):
        return 0
    keys = [k for k in shop_keys if k and k != "*"]
    if not keys:
        return 0
    try:
        with get_db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute(
                "UPDATE shops SET team_id=%s WHERE shop_key = ANY(%s) AND team_id IS DISTINCT FROM %s",
                (tid, keys, tid),
            )
            n = _cur.rowcount
            _conn.commit()
        return n or 0
    except Exception as exc:
        logger.warning("_sync_team_to_shops lỗi: %s", exc)
        return 0


def _sync_team_to_vias(user_id, team_code) -> int:
    """Đẩy team của user XUỐNG mọi VIA user đó đang giữ (module quan_ly_via).

    Wrapper lazy-import để tránh circular import. Lỗi chỉ log + trả 0.
    """
    if not user_id or not team_code:
        return 0
    try:
#        from modules.quan_ly_via import sync_user_team_to_vias
        return sync_user_team_to_vias(user_id, team_code)
    except Exception as exc:
        logger.warning("_sync_team_to_vias lỗi: %s", exc)
        return 0


def _sync_team_to_vat_options(user_id, team_code) -> int:
    """Đẩy team của user XUỐNG mọi TK QC user đang phụ trách (module chi_phi_qc).

    Wrapper lazy-import để tránh circular import. Lỗi chỉ log + trả 0.
    """
    if not user_id or not team_code:
        return 0
    try:
        from modules.chi_phi_qc import sync_user_team_to_vat_options
        return sync_user_team_to_vat_options(user_id, team_code)
    except Exception as exc:
        logger.warning("_sync_team_to_vat_options lỗi: %s", exc)
        return 0


def _apply_team_office(user_ident, team_code, full_name="", by_username=False) -> bool:
    """Gán văn phòng chấm công mặc định của team cho 1 NV (module cham_cong).

    by_username=True → user_ident là username, tự resolve sang users.id (dùng cho
    luồng tạo user mới vì object trong RAM mang id tạm 'u-...'). Lỗi log + trả False.
    """
    if not team_code:
        return False
    try:
        uid = user_ident
        if by_username:
            uid = None
            if get_db_conn:
                with get_db_conn() as _c:
                    _cur = _c.cursor()
                    _cur.execute("SELECT id FROM users WHERE username=%s", (user_ident,))
                    _r = _cur.fetchone()
                    uid = _r[0] if _r else None
            if not uid:
                return False
        from modules.cham_cong.cc_db import apply_team_office_to_user
        return apply_team_office_to_user(uid, team_code, full_name)
    except Exception as exc:
        logger.warning("_apply_team_office lỗi: %s", exc)
        return False


@settings_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if is_accountant_user():
        abort(403)
    messages: List[Dict[str, str]] = []
    if request.args.get("synced") == "1":
        messages.append({"level": "Đồng bộ POS", "text": "⏳ Đang kéo đơn hàng / doanh thu / CP quảng cáo POS trong nền — chờ 1-2 phút rồi tải lại trang."})
    tab = request.args.get("tab", "overview").strip() or "overview"
    tabs = [
        {"key": "overview", "label": "Tổng quan"},
        {"key": "staff", "label": "Nhân sự"},
        {"key": "phan_quyen", "label": "Phân quyền"},
        {"key": "shop_web", "label": "Shop & Web"},
        {"key": "ad_accounts", "label": "Tài khoản quảng cáo", "url": "/page-account/ad-accounts"},
        {"key": "facebook_pages", "label": "Facebook Pages"},
        {"key": "telegram", "label": "Telegram Bot"},
        {"key": "accounts", "label": "Tài khoản"},
        {"key": "backup_db", "label": "🗄️ Backup DB"},
        # 🤖 AI Models: ẩn khỏi menu — khách hàng (dùng chung tài khoản admin) KHÔNG
        # được thấy API key của các nhà cung cấp AI. Kỹ thuật mở bằng:
        #   /settings?tab=ai_models&dev=1
        *([{"key": "ai_models", "label": "🤖 AI Models"}]
          if request.args.get("dev") == "1" or session.get("dev_panel") else []),
        {"key": "lan_qr", "label": "🌸 Lan Zalo (QR)", "url": "/lan-qr"},
        {"key": "lan_bao_cao", "label": "📢 Lan gửi báo cáo cho ai?", "url": "/chi-phi-qc/lan-bao-cao"},
    ]
    tab_keys = {t["key"] for t in tabs}
    # Mở tab kỹ thuật bằng ?dev=1 rồi NHỚ trong phiên, để bấm Lưu (POST) không văng ra
    if request.args.get("dev") == "1":
        session["dev_panel"] = True
    if tab == "ai_models" and not session.get("dev_panel"):
        tab = "overview"
    if tab not in tab_keys:
        tab = "overview"

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        if is_leader_user() and action in {
            "leader_claim_staff",
            "leader_update_user_shops",
            "leader_change_staff_password",
        }:
            me = current_user() or {}
            my_team = str(me.get("team_id", "")).strip()
            # sale_leader quản lý nhân viên role='sale'; leader thường quản lý role='staff'
            my_managed_role = "sale" if str(me.get("role", "")).strip() == "sale_leader" else "staff"
            users = load_users()
            shop_meta = load_shop_meta_map()
            if not my_team:
                messages.append({"level": "Lỗi", "text": "Leader chưa có team_id, không thể quản lý nhân sự."})
            elif action == "leader_claim_staff":
                user_id = request.form.get("user_id", "").strip()
                target = next((u for u in users if str(u.get("id", "")) == user_id), None)
                if not target:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy nhân viên."})
                elif str(target.get("role", "")).strip() != my_managed_role:
                    messages.append({"level": "Lỗi", "text": f"Chỉ có thể gom nhân viên ({my_managed_role}) chưa thuộc team nào."})
                elif str(target.get("team_id", "")).strip():
                    messages.append({"level": "Lỗi", "text": "Nhân viên đã thuộc một team; chỉ quản trị viên mới chuyển team."})
                elif my_managed_role == "staff" and "*" in normalize_assigned_shops(target.get("assigned_shops", [])):
                    messages.append({
                        "level": "Lỗi",
                        "text": "Nhân viên chưa có shop mặc định cụ thể. Nhờ admin gán shop trước rồi mới gom vào team.",
                    })
                else:
                    target["team_id"] = my_team
                    save_users(users)
                    messages.append({
                        "level": "Thành công",
                        "text": f"Đã thêm '{target.get('username')}' vào team {my_team}.",
                    })
            else:
                user_id = request.form.get("user_id", "").strip()
                target = next((u for u in users if str(u.get("id", "")) == user_id), None)
                is_self_target = bool(target) and str(target.get("id", "")) == str(me.get("id", ""))
                if not target:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy nhân viên."})
                elif action == "leader_change_staff_password":
                    new_password = request.form.get("new_password", "").strip()
                    if not is_self_target and str(target.get("role", "")).strip() != my_managed_role:
                        messages.append({"level": "Lỗi", "text": f"Leader chỉ đổi mật khẩu cho chính mình hoặc {my_managed_role} trong team."})
                    elif not is_self_target and str(target.get("team_id", "")).strip() != my_team:
                        messages.append({"level": "Lỗi", "text": "Chỉ đổi mật khẩu cho nhân viên trong team của bạn."})
                    elif not new_password:
                        messages.append({"level": "Lỗi", "text": "Mật khẩu mới không được để trống."})
                    else:
                        target["password"] = new_password
                        save_users(users)
                        if is_self_target:
                            messages.append({"level": "Thành công", "text": "Đã đổi mật khẩu của bạn."})
                        else:
                            messages.append({"level": "Thành công", "text": "Đã đổi mật khẩu nhân viên."})
                elif str(target.get("role", "")).strip() != my_managed_role:
                    messages.append({"level": "Lỗi", "text": f"Chỉ thao tác trên nhân viên ({my_managed_role}) trong team."})
                elif str(target.get("team_id", "")).strip() != my_team:
                    messages.append({"level": "Lỗi", "text": "Chỉ thao tác trên nhân viên trong team của bạn."})
                else:
                    assigned_selected = [x.strip() for x in request.form.getlist("assigned_shops") if x.strip()]
                    assigned_shops = normalize_assigned_shops(assigned_selected or [])
                    leader_manageable = get_leader_manageable_shop_keys(me, shop_meta, users)
                    if "*" in assigned_shops:
                        messages.append({"level": "Lỗi", "text": "Leader không được gán '*' — chọn từng shop trong team."})
                    else:
                        bad = [k for k in assigned_shops if k not in leader_manageable]
                        if bad:
                            messages.append({"level": "Lỗi", "text": f"Shop vượt phạm vi quản lý của leader: {', '.join(bad)}"})
                        else:
                            target["assigned_shops"] = assigned_shops
                            save_users(users)
                            messages.append({"level": "Thành công", "text": "Đã cập nhật shop cho nhân viên."})
            tab = "staff"
        elif not is_admin_user():
            messages.append({"level": "Quyền truy cập", "text": "Chỉ admin mới có thể chỉnh sửa mục này."})
        else:
            # ── Bảo vệ tài khoản chủ (admin/superadmin) khỏi leo thang quyền ──
            # is_admin_user() gồm cả manager/it → nếu không chặn, manager/it sửa được
            # cả tài khoản admin (đổi mật khẩu/đổi vai trò = chiếm quyền). Quy tắc:
            # CHỈ owner (admin·superadmin) mới được thao tác trên tài khoản admin·superadmin,
            # và mới được cấp vai trò admin·superadmin cho bất kỳ ai.
            _OWNER_ROLES = {"admin", "superadmin"}
            _me_is_owner = str(session.get("role", "")).strip() in _OWNER_ROLES
            _blocked = False
            _USER_MUT = {"update_user_all", "change_user_password", "toggle_user_status",
                         "change_user_role", "update_user_fullname", "update_username"}
            if not _me_is_owner and action in _USER_MUT:
                _tid = (request.form.get("user_id") or "").strip()
                _tgt = next((u for u in load_users()
                             if str(u.get("id", "")).strip() == _tid), None)
                if _tgt and str(_tgt.get("role", "")).strip() in _OWNER_ROLES:
                    messages.append({"level": "Quyền truy cập",
                                     "text": "Chỉ admin/superadmin mới được thao tác trên tài khoản admin."})
                    _blocked = True
            # [MOON] KHÔNG cho tạo/nâng vai trò admin·superadmin qua giao diện (kể cả owner) —
            # bảo vệ billing: tài khoản chủ chỉ tạo trực tiếp ở DB, không lộ ra web khách.
            if (not _blocked
                    and action in {"create_user", "update_user_all", "change_user_role"}):
                _want = (request.form.get("role") or request.form.get("new_role") or "").strip()
                if _want in _OWNER_ROLES:
                    messages.append({"level": "Quyền truy cập",
                                     "text": "Không thể tạo/cấp vai trò admin ở đây. Tài khoản chủ do nhà cung cấp quản lý riêng."})
                    _blocked = True
            if _blocked:
                tab = "staff"
            elif action == "toggle_maintenance":
                cur_val = str((load_config() or {}).get("maintenance_mode", "0"))
                new_val = "0" if cur_val == "1" else "1"
                save_config_key("maintenance_mode", new_val)
                if new_val == "1":
                    save_config_key("maintenance_started_at", now_hcm().strftime("%H:%M %d/%m/%Y"))
                else:
                    save_config_key("maintenance_started_at", "")
                messages.append({
                    "level": "Thành công",
                    "text": ("Đã BẬT chế độ bảo trì — chỉ admin/superadmin/manager/it dùng được."
                             if new_val == "1" else "Đã TẮT chế độ bảo trì."),
                })
                tab = "overview"
            elif action == "save_maintenance_message":
                msg_txt = request.form.get("maintenance_message", "").strip()
                save_config_key("maintenance_message", msg_txt)
                messages.append({"level": "Thành công", "text": "Đã lưu thông báo bảo trì."})
                tab = "overview"
            elif action == "create_user":
                username = request.form.get("username", "").strip()
                password = request.form.get("password", "").strip()
                role = request.form.get("role", "staff").strip()
                team_id = request.form.get("team_id", "").strip()
                assigned_selected = [x.strip() for x in request.form.getlist("assigned_shops") if x.strip()]
                # Staff KHÔNG bắt buộc có shop — NV mới chưa có shop là bình thường,
                # để trống = chưa gán shop (KHÔNG mặc định "*" = xem tất cả).
                # Các role khác để trống → "*" như cũ.
                if role == "staff":
                    assigned_shops = [s for s in assigned_selected if s != "*"]
                else:
                    assigned_shops = normalize_assigned_shops(assigned_selected or ["*"])
                users = load_users()
                if not username or not password:
                    messages.append({"level": "Lỗi", "text": "Thiếu username hoặc password."})
                elif any(str(u.get("username", "")).strip() == username for u in users):
                    messages.append({"level": "Lỗi", "text": f"Username '{username}' đã tồn tại."})
                else:
                    wh_id_new = request.form.get("warehouse_id", "").strip() or None
                    wh_name_new = request.form.get("warehouse_name", "").strip() or ""
                    full_name_new = request.form.get("full_name", "").strip()
                    users.append({
                        "id": f"u-{uuid.uuid4().hex[:8]}",
                        "username": username,
                        "full_name": full_name_new,
                        "password": password,
                        "role": role if role in {"admin", "leader", "sale_leader", "sale", "staff", "accountant", "kho", "manager"} else "staff",
                        "team_id": team_id,
                        # staff để trống = [] (chưa có shop); role khác giữ ["*"] khi rỗng
                        "assigned_shops": assigned_shops if (role == "staff") else (assigned_shops or ["*"]),
                        # Ngày hiệu lực gán shop (date picker). Rỗng → CURRENT_DATE.
                        "assign_from": request.form.get("assigned_from", "").strip(),
                        "status": "active",
                        "warehouse_id": int(wh_id_new) if wh_id_new and wh_id_new.isdigit() else None,
                        "warehouse_name": wh_name_new,
                    })
                    save_users(users)
                    messages.append({"level": "Thành công", "text": f"Đã tạo user '{username}'."})
                    # [THỐNG NHẤT] Tự thêm NV mới vào hệ thống chấm công (cc_employees)
                    # → tạo user 1 chỗ = NV đầy đủ (account + team + shop + chấm công).
                    try:
                        from modules.cham_cong.cc_db import upsert_employee as _cc_upsert
                        _role_to_cc = {"admin": "admin", "superadmin": "admin", "manager": "admin",
                                       "it": "it", "leader": "leader", "sale_leader": "leader",
                                       "sale": "sale", "kho": "packing", "accountant": "accounting"}
                        _cc_role = _role_to_cc.get(role, "sale")
                        _new_uid = None
                        if get_db_conn:
                            with get_db_conn() as _c:
                                _cur = _c.cursor()
                                _cur.execute("SELECT id FROM users WHERE username=%s", (username,))
                                _r = _cur.fetchone()
                                _new_uid = str(_r[0]) if _r else None
                        if _new_uid:
                            _cc_upsert(_new_uid, full_name_new or username, _cc_role, department=team_id or "")
                            messages.append({"level": "Thành công", "text": "Đã thêm NV vào hệ thống chấm công."})
                    except Exception as _e:
                        messages.append({"level": "Cảnh báo", "text": f"Tạo user OK nhưng thêm chấm công lỗi: {_e}"})
                    # Chấm công: gán văn phòng MẶC ĐỊNH của team cho NV mới (nếu team đã cấu hình)
                    _no = _apply_team_office(username, team_id, full_name_new or username, by_username=True)
                    if _no:
                        messages.append({"level": "Thành công",
                                         "text": "Đã gán văn phòng chấm công theo team cho user mới."})
                    if role == "staff":
                        if not team_id:
                            messages.append({"level": "Cảnh báo", "text": "Staff chưa có team_id. Nên gán team để đảm bảo phân quyền nhất quán."})
                        elif "*" not in assigned_shops:
                            shop_meta = load_shop_meta_map()
                            cross_team = []
                            for shop_key in assigned_shops:
                                shop_team = str(shop_meta.get(shop_key, {}).get("team_id", "")).strip()
                                if shop_team and shop_team != team_id:
                                    cross_team.append(shop_key)
                            if cross_team:
                                messages.append({
                                    "level": "Cảnh báo",
                                    "text": f"Assigned shops khác team_id '{team_id}': {', '.join(cross_team)}",
                                })
                tab = "staff"
            elif action == "update_user_all":
                # Modal "Sửa user" — cập nhật tất cả field cùng lúc
                user_id = request.form.get("user_id", "").strip()
                new_username = request.form.get("username", "").strip()
                new_password = request.form.get("password", "").strip()  # có thể trống = giữ nguyên
                new_fullname = request.form.get("full_name", "").strip()
                new_role     = request.form.get("role", "staff").strip()
                new_team_id  = request.form.get("team_id", "").strip()
                new_status   = request.form.get("status", "active").strip()
                assigned_selected = [x.strip() for x in request.form.getlist("assigned_shops") if x.strip()]
                new_shops = normalize_assigned_shops(assigned_selected or ["*"])

                users = load_users()
                target = None
                for u in users:
                    if str(u.get("id", "")) == user_id:
                        target = u
                        break
                if not target:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                elif not new_username:
                    messages.append({"level": "Lỗi", "text": "Thiếu username."})
                else:
                    # Check username trùng (trừ chính mình)
                    if new_username != target.get("username", "") and any(
                        str(u.get("username", "")).strip() == new_username for u in users
                        if str(u.get("id", "")) != user_id
                    ):
                        messages.append({"level": "Lỗi", "text": f"Username '{new_username}' đã tồn tại."})
                    else:
                        target["username"] = new_username
                        target["full_name"] = new_fullname
                        if new_password:
                            target["password"] = new_password
                        if new_role in {"admin", "leader", "sale_leader", "sale", "staff", "accountant", "kho", "manager", "it", "superadmin"}:
                            target["role"] = new_role
                        target["team_id"] = new_team_id
                        if new_status in {"active", "inactive"}:
                            target["status"] = new_status
                        target["assigned_shops"] = new_shops or ["*"]
                        # Ngày hiệu lực gán shop (date picker). Rỗng → CURRENT_DATE.
                        target["assign_from"] = request.form.get("assigned_from", "").strip()
                        # [DÙNG CHUNG SHOP] KHÔNG gỡ shop khỏi NV khác — nhiều NV được phép
                        # cùng giữ 1 shop (cùng xem/truy cập). save_users cũng không đóng holder khác.
                        save_users(users)
                        messages.append({"level": "Thành công",
                                         "text": f"Đã cập nhật user '{new_username}'."})
                        # Đồng bộ team XUỐNG shop user này sở hữu (chỉ list cụ thể, không "*")
                        # → tránh lệch giữa tab Nhân sự và tab Shop & Web. last-writer-wins.
                        _n = _sync_team_to_shops(new_team_id, new_shops)
                        if _n:
                            messages.append({"level": "Thành công",
                                             "text": f"Đã đồng bộ team cho {_n} shop của user."})
                        # Đồng bộ team XUỐNG VIA user này đang giữ (assigned_user_id)
                        _nv = _sync_team_to_vias(user_id, new_team_id)
                        if _nv:
                            messages.append({"level": "Thành công",
                                             "text": f"Đã đồng bộ team cho {_nv} VIA của user."})
                        # Đồng bộ team XUỐNG TK QC user đang phụ trách (vat-options)
                        _nt = _sync_team_to_vat_options(user_id, new_team_id)
                        if _nt:
                            messages.append({"level": "Thành công",
                                             "text": f"Đã đồng bộ team cho {_nt} TK QC của user."})
                        # Chấm công: gán văn phòng mặc định của team mới cho user
                        if _apply_team_office(user_id, new_team_id, new_fullname or new_username):
                            messages.append({"level": "Thành công",
                                             "text": "Đã cập nhật văn phòng chấm công theo team mới."})
                tab = "staff"
            elif action == "update_user_shops":
                user_id = request.form.get("user_id", "").strip()
                assigned_selected = [x.strip() for x in request.form.getlist("assigned_shops") if x.strip()]
                assigned_shops = normalize_assigned_shops(assigned_selected or ["*"])
                users = load_users()
                updated = False
                target_user = None
                for user in users:
                    if str(user.get("id", "")) == user_id:
                        user["assigned_shops"] = assigned_shops
                        target_user = user
                        updated = True
                        break
                if updated:
                    save_users(users)
                    messages.append({"level": "Thành công", "text": "Đã cập nhật danh sách shop cho user."})
                    user_role = str((target_user or {}).get("role", "staff")).strip()
                    user_team = str((target_user or {}).get("team_id", "")).strip()
                    if not user_team:
                        if user_role == "staff":
                            messages.append({"level": "Cảnh báo", "text": "Staff chưa có team_id. Nên gán team để đảm bảo phân quyền nhất quán."})
                    else:
                        # Đồng bộ team xuống shop vừa gán cho user (shop mới nhận team của user).
                        _n = _sync_team_to_shops(user_team, assigned_shops)
                        if _n:
                            messages.append({"level": "Thành công",
                                             "text": f"Đã đồng bộ team cho {_n} shop được gán."})
                else:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy user để cập nhật shop."})
                tab = "staff"
            elif action == "change_user_password":
                user_id = request.form.get("user_id", "").strip()
                new_password = request.form.get("new_password", "").strip()
                users = load_users()
                updated = False
                if not new_password:
                    messages.append({"level": "Lỗi", "text": "Mật khẩu mới không được để trống."})
                else:
                    for user in users:
                        if str(user.get("id", "")) == user_id:
                            user["password"] = new_password
                            updated = True
                            break
                    if updated:
                        save_users(users)
                        messages.append({"level": "Thành công", "text": "Đã đổi mật khẩu user."})
                    else:
                        messages.append({"level": "Lỗi", "text": "Không tìm thấy user để đổi mật khẩu."})
                tab = "staff"
            elif action == "toggle_user_status":
                user_id = request.form.get("user_id", "").strip()
                users = load_users()
                for user in users:
                    if str(user.get("id", "")) == user_id:
                        old_status = str(user.get("status", "active"))
                        user["status"] = "inactive" if old_status == "active" else "active"
                        break
                save_users(users)
                messages.append({"level": "Thành công", "text": "Đã cập nhật trạng thái user."})
                tab = "staff"
            elif action == "change_user_team":
                user_id = request.form.get("user_id", "").strip()
                new_team_code = request.form.get("new_team_id", "").strip()  # "" = bỏ team
                users = load_users()
                target = next((u for u in users if str(u.get("id", "")) == user_id), None)
                if not target:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                else:
                    # Validate team_code phải tồn tại trong bảng teams (hoặc rỗng để bỏ team)
                    if new_team_code:
                        try:
                            from db import get_conn as _gc
                            with _gc() as _c:
                                _cu = _c.cursor()
                                _cu.execute("SELECT 1 FROM teams WHERE team_code=%s", (new_team_code,))
                                if not _cu.fetchone():
                                    messages.append({"level": "Lỗi", "text": f"Team code '{new_team_code}' không tồn tại."})
                                    new_team_code = None
                        except Exception:
                            new_team_code = None
                    if new_team_code is not None:
                        old = target.get("team_id", "")
                        target["team_id"] = new_team_code
                        save_users(users)
                        if new_team_code:
                            messages.append({"level": "Thành công", "text": f"Đã chuyển '{target.get('username')}' từ team '{old or '(không có)'}' → '{new_team_code}'."})
                        else:
                            messages.append({"level": "Thành công", "text": f"Đã bỏ team cho '{target.get('username')}' (trước đây: '{old}')."})
                tab = "staff"
            elif action == "create_team":
                if not is_admin_user():
                    messages.append({"level": "Lỗi", "text": "Chỉ admin được tạo team."})
                else:
                    import re as _re
                    import unicodedata as _ud
                    team_code = request.form.get("team_code", "").strip().lower()
                    team_name = request.form.get("team_name", "").strip()
                    leader_user_id = request.form.get("leader_user_id", "").strip()
                    team_type = request.form.get("team_type", "kinh_doanh").strip()
                    if team_type not in ("kinh_doanh", "sale", "khac"):
                        team_type = "kinh_doanh"

                    def _slugify_team(_name: str) -> str:
                        _s = (_name or "").strip().lower().replace("đ", "d")
                        _s = _ud.normalize("NFD", _s)
                        _s = "".join(_ch for _ch in _s if _ud.category(_ch) != "Mn")
                        _s = _re.sub(r"[^a-z0-9]+", "-", _s).strip("-")
                        return _s[:50] or "team"

                    if not team_name:
                        messages.append({"level": "Lỗi", "text": "Phải nhập tên team."})
                    elif team_code and not _re.match(r"^[a-z0-9][a-z0-9-]{0,49}$", team_code):
                        messages.append({"level": "Lỗi", "text": "team_code chỉ chứa a-z, 0-9, dấu '-', tối đa 50 ký tự."})
                    else:
                        try:
                            from db import get_conn as _gc
                            with _gc() as _c:
                                _cu = _c.cursor()
                                # Tự sinh team_code từ tên nếu không nhập — đảm bảo duy nhất.
                                if not team_code:
                                    _base = _slugify_team(team_name)
                                    team_code = _base
                                    _n = 2
                                    while True:
                                        _cu.execute("SELECT 1 FROM teams WHERE team_code=%s", (team_code,))
                                        if not _cu.fetchone():
                                            break
                                        team_code = f"{_base}-{_n}"
                                        _n += 1
                                _cu.execute("SELECT 1 FROM teams WHERE team_code=%s", (team_code,))
                                if _cu.fetchone():
                                    messages.append({"level": "Lỗi", "text": f"Team code '{team_code}' đã tồn tại."})
                                else:
                                    leader_db_id = None
                                    if leader_user_id:
                                        _cu.execute("SELECT id FROM users WHERE id=%s", (leader_user_id,))
                                        row = _cu.fetchone()
                                        if row:
                                            leader_db_id = row[0]
                                    _cu.execute(
                                        "INSERT INTO teams (team_code, team_name, status, leader_user_id, team_type) VALUES (%s, %s, 'active', %s, %s) RETURNING id",
                                        (team_code, team_name, leader_db_id, team_type),
                                    )
                                    new_team_db_id = _cu.fetchone()[0]
                                    # Tự động gán leader vào team đó (cập nhật users.team_id)
                                    leader_joined = False
                                    if leader_db_id:
                                        _cu.execute(
                                            "UPDATE users SET team_id = %s WHERE id = %s",
                                            (new_team_db_id, leader_db_id),
                                        )
                                        leader_joined = True
                                    _c.commit()
                                    msg = f"Đã tạo team '{team_code}' ({team_name})."
                                    if leader_joined:
                                        msg += " Đã gán leader vào team."
                                    messages.append({"level": "Thành công", "text": msg})
                        except Exception as exc:
                            messages.append({"level": "Lỗi", "text": f"Lỗi tạo team: {exc}"})
                tab = "staff"
            elif action == "change_user_role":
                user_id = request.form.get("user_id", "").strip()
                new_role = request.form.get("new_role", "").strip()
                valid_roles = {"staff", "leader", "sale_leader", "sale", "kho", "accountant", "it", "manager", "admin"}
                if new_role not in valid_roles:
                    messages.append({"level": "Lỗi", "text": f"Vai trò '{new_role}' không hợp lệ."})
                else:
                    users = load_users()
                    changed = False
                    for user in users:
                        if str(user.get("id", "")) == user_id:
                            user["role"] = new_role
                            changed = True
                            break
                    if changed:
                        save_users(users)
                        messages.append({"level": "Thành công", "text": "Đã đổi vai trò user."})
                    else:
                        messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                tab = "staff"
            elif action == "update_user_fullname":
                user_id = request.form.get("user_id", "").strip()
                full_name = request.form.get("full_name", "").strip()
                users = load_users()
                changed = False
                target_username = ""
                for u in users:
                    if u.get("id") == user_id:
                        u["full_name"] = full_name
                        target_username = u.get("username", "")
                        changed = True
                        break
                if changed:
                    save_users(users)
                    # Đồng bộ cc_employees
                    try:
                        from modules.cham_cong.cc_db import upsert_employee, get_employee
                        emp = get_employee(user_id)
                        if emp:
                            upsert_employee(
                                user_id=user_id,
                                full_name=full_name,
                                cc_role=emp.get("cc_role", "sale"),
                                department=emp.get("department", ""),
                                phone=emp.get("phone", ""),
                                position=emp.get("position", ""),
                            )
                    except Exception:
                        pass
                    # Cập nhật session nếu đang sửa chính mình
                    if user_id == session.get("user_id"):
                        session["full_name"] = full_name
                    messages.append({"level": "Thành công", "text": f"Đã cập nhật tên đầy đủ cho '{target_username}': {full_name}"})
                else:
                    messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                tab = "staff"
            elif action == "update_username":
                import re as _re_uname
                user_id = request.form.get("user_id", "").strip()
                new_username = request.form.get("new_username", "").strip()
                old_username = ""
                if not user_id or not new_username:
                    messages.append({"level": "Lỗi", "text": "Thiếu user_id hoặc tên đăng nhập mới."})
                elif not _re_uname.fullmatch(r"[A-Za-z0-9_.-]{2,40}", new_username):
                    messages.append({"level": "Lỗi", "text": "Tên đăng nhập chỉ chứa chữ/số/._- (2-40 ký tự)."})
                else:
                    users = load_users()
                    # Kiểm tra trùng username (case-insensitive so sánh, lưu đúng case)
                    new_lower = new_username.lower()
                    clash = False
                    target_user = None
                    for u in users:
                        if str(u.get("id", "")) == user_id:
                            target_user = u
                            old_username = u.get("username", "")
                        else:
                            if str(u.get("username", "")).lower() == new_lower:
                                clash = True
                    if not target_user:
                        messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                    elif clash:
                        messages.append({"level": "Lỗi", "text": f"Tên đăng nhập '{new_username}' đã tồn tại."})
                    elif old_username == new_username:
                        messages.append({"level": "Thông tin", "text": "Tên đăng nhập không thay đổi."})
                    else:
                        # Update DB trước (tránh trường hợp JSON và DB lệch)
                        db_ok = True
                        if get_db_conn:
                            try:
                                with get_db_conn() as _conn:
                                    _cur = _conn.cursor()
                                    _cur.execute("SELECT 1 FROM users WHERE LOWER(username)=%s AND id::text<>%s", (new_lower, user_id))
                                    if _cur.fetchone():
                                        db_ok = False
                                        messages.append({"level": "Lỗi", "text": f"DB: Tên đăng nhập '{new_username}' đã tồn tại."})
                                    else:
                                        _cur.execute("UPDATE users SET username=%s WHERE id::text=%s", (new_username, user_id))
                                        _conn.commit()
                            except Exception as _e:
                                db_ok = False
                                messages.append({"level": "Lỗi", "text": f"DB update thất bại: {_e}"})
                        if db_ok:
                            target_user["username"] = new_username
                            save_json_file(USERS_FILE, users)
                            # Cập nhật session nếu đang sửa chính mình
                            if user_id == session.get("user_id") or old_username == session.get("username"):
                                session["username"] = new_username
                            messages.append({"level": "Thành công", "text": f"Đã đổi tên đăng nhập: '{old_username}' → '{new_username}'."})
                tab = "staff"
            elif action == "update_user_all":
                # Cập nhật nhiều trường của user trong 1 lần: username, password,
                # full_name, role, team_id, status, assigned_shops.
                # Chỉ cập nhật field nào client GỬI ĐẾN (form có chứa key đó).
                # password rỗng = giữ nguyên.
                import re as _re_uname
                user_id = request.form.get("user_id", "").strip()
                if not user_id:
                    messages.append({"level": "Lỗi", "text": "Thiếu user_id."})
                else:
                    users = load_users()
                    target = next((u for u in users if str(u.get("id", "")) == user_id), None)
                    if not target:
                        messages.append({"level": "Lỗi", "text": "Không tìm thấy user."})
                    else:
                        old_username = target.get("username", "")
                        changes = []
                        errs = []

                        # 1) username (validate + check trùng)
                        new_username = request.form.get("new_username", "").strip()
                        if new_username and new_username != old_username:
                            if not _re_uname.fullmatch(r"[A-Za-z0-9_.-]{2,40}", new_username):
                                errs.append("Tên đăng nhập chỉ chứa chữ/số/._- (2-40 ký tự).")
                            else:
                                new_lower = new_username.lower()
                                clash = any(str(u.get("username", "")).lower() == new_lower
                                            for u in users if str(u.get("id", "")) != user_id)
                                if clash:
                                    errs.append(f"Tên đăng nhập '{new_username}' đã tồn tại.")
                                else:
                                    # Sync DB nếu có
                                    if get_db_conn:
                                        try:
                                            with get_db_conn() as _conn:
                                                _cur = _conn.cursor()
                                                _cur.execute(
                                                    "UPDATE users SET username=%s WHERE id::text=%s",
                                                    (new_username, user_id))
                                                _conn.commit()
                                        except Exception as _e:
                                            errs.append(f"DB update tên đăng nhập thất bại: {_e}")
                                    if not errs:
                                        target["username"] = new_username
                                        if user_id == session.get("user_id") or old_username == session.get("username"):
                                            session["username"] = new_username
                                        changes.append(f"username '{old_username}'→'{new_username}'")

                        # 2) password (rỗng = giữ)
                        new_password = request.form.get("new_password", "").strip()
                        if new_password:
                            target["password"] = new_password
                            changes.append("đổi mật khẩu")

                        # 3) full_name
                        if "full_name" in request.form:
                            new_full = request.form.get("full_name", "").strip()
                            if new_full != (target.get("full_name") or ""):
                                target["full_name"] = new_full
                                changes.append("tên đầy đủ")

                        # 4) role
                        if "role" in request.form:
                            new_role = request.form.get("role", "").strip()
                            valid_roles = {"admin", "leader", "sale_leader", "sale", "staff",
                                           "accountant", "kho", "manager", "it"}
                            if new_role in valid_roles and new_role != target.get("role"):
                                target["role"] = new_role
                                changes.append(f"vai trò → {new_role}")

                        # 5) team_id (cho phép rỗng = bỏ team)
                        team_changed_to = None
                        if "team_id" in request.form:
                            new_team = request.form.get("team_id", "").strip()
                            if new_team != str(target.get("team_id") or ""):
                                target["team_id"] = new_team
                                team_changed_to = new_team
                                changes.append(f"team → '{new_team or '(không)'}'")

                        # 6) status
                        if "status" in request.form:
                            new_status = request.form.get("status", "").strip()
                            if new_status in ("active", "inactive") and new_status != target.get("status"):
                                target["status"] = new_status
                                changes.append(f"trạng thái → {new_status}")

                        # 7) assigned_shops (nếu client gửi field này)
                        if "assigned_shops" in request.form:
                            assigned_selected = [x.strip() for x in request.form.getlist("assigned_shops") if x.strip()]
                            target["assigned_shops"] = normalize_assigned_shops(assigned_selected or ["*"])
                            changes.append("danh sách shop")

                        if errs:
                            for e in errs:
                                messages.append({"level": "Lỗi", "text": e})
                        elif changes:
                            save_users(users)
                            # Đổi team → đẩy team XUỐNG shop user sở hữu + VIA user đang giữ
                            if team_changed_to:
                                _sync_team_to_shops(team_changed_to,
                                                    normalize_assigned_shops(target.get("assigned_shops", [])))
                                _nv = _sync_team_to_vias(user_id, team_changed_to)
                                if _nv:
                                    changes.append(f"đồng bộ {_nv} VIA")
                                _nt = _sync_team_to_vat_options(user_id, team_changed_to)
                                if _nt:
                                    changes.append(f"đồng bộ {_nt} TK QC")
                                if _apply_team_office(user_id, team_changed_to,
                                                      target.get("full_name") or target.get("username") or ""):
                                    changes.append("văn phòng chấm công")
                            messages.append({"level": "Thành công",
                                             "text": f"Đã cập nhật user '{target.get('username')}': "
                                                     + ", ".join(changes) + "."})
                        else:
                            messages.append({"level": "Thông tin", "text": "Không có gì thay đổi."})
                tab = "staff"
            elif action == "add_shop":
                shop_key = request.form.get("shop_key", "").strip()
                shop_name = request.form.get("shop_name", "").strip()
                shop_id = request.form.get("shop_id", "").strip()
                team_code = request.form.get("shop_team_id", "").strip()
                status = request.form.get("shop_status", "active").strip()
                pos_api_key = request.form.get("pos_api_key", "").strip()
                business_type = request.form.get("business_type", "cty").strip().lower()
                _valid_bt = shop_types.get_valid_type_codes() if shop_types else {"cty", "hkd", "pos3"}
                if business_type not in _valid_bt:
                    business_type = "cty"
                norm_status = "active" if status == "active" else "inactive"
                if not shop_key or not shop_name or not shop_id:
                    messages.append({"level": "Lỗi", "text": "Thiếu shop_key, shop_name hoặc shop_id."})
                else:
                    db_ok = False
                    if get_db_conn:
                        try:
                            with get_db_conn() as _conn:
                                _cur = _conn.cursor()
                                # Kiểm tra shop_key đã tồn tại chưa
                                _cur.execute("SELECT id FROM shops WHERE shop_key=%s", (shop_key,))
                                if _cur.fetchone():
                                    messages.append({"level": "Lỗi", "text": f"Shop key '{shop_key}' đã tồn tại."})
                                    db_ok = None  # sentinel: đã có lỗi, skip
                                else:
                                    # Map team_code → team_id
                                    team_db_id = None
                                    if team_code:
                                        _cur.execute("SELECT id FROM teams WHERE team_code=%s", (team_code,))
                                        _row = _cur.fetchone()
                                        team_db_id = _row[0] if _row else None
                                    # Ghi vào bảng shops chính
                                    _cur.execute("""
                                        INSERT INTO shops (shop_key, shop_name, pancake_shop_id, team_id, status, business_type)
                                        VALUES (%s, %s, %s, %s, %s::record_status, %s)
                                    """, (shop_key, shop_name, shop_id, team_db_id, norm_status, business_type))
                                    _conn.commit()
                                    db_ok = True
                        except Exception as _e:
                            messages.append({"level": "Cảnh báo", "text": f"Lưu shops DB thất bại: {_e}"})
                    if db_ok is True:
                        # Ghi wh_shops để có api_key
                        try:
#                            from modules.kho_vat_ly.wh_db import wh_db as _wh_db
                            with _wh_db() as _conn:
                                _conn.execute("""
                                    INSERT INTO wh_shops (shop_key, shop_name, pos_shop_id, pos_api_key, status)
                                    VALUES (%s,%s,%s,%s,%s)
                                    ON CONFLICT (shop_key) DO UPDATE
                                      SET shop_name=EXCLUDED.shop_name, pos_shop_id=EXCLUDED.pos_shop_id,
                                          pos_api_key=EXCLUDED.pos_api_key, status=EXCLUDED.status
                                """, (shop_key, shop_name, shop_id, pos_api_key or None, norm_status))
                            try:
                                import pancake_auth as _pa; _pa.clear_cache()
                            except Exception:
                                pass
                        except Exception as _e:
                            messages.append({"level": "Cảnh báo", "text": f"Lưu wh_shops thất bại: {_e}"})
                        # Backup JSON
                        try:
                            shops_raw = try_parse_json(SHOPS_FILE) if os.path.exists(SHOPS_FILE) else []
                            if not isinstance(shops_raw, list):
                                shops_raw = []
                            shops_raw.append({"shop_key": shop_key, "shop_name": shop_name,
                                              "shop_id": shop_id, "team_id": team_code, "status": norm_status,
                                              "business_type": business_type})
                            save_json_file(SHOPS_FILE, shops_raw)
                        except Exception:
                            pass
                        messages.append({"level": "Thành công", "text": f"Đã thêm shop '{shop_name}'" + (" (có API key)" if pos_api_key else "") + "."})
                        invalidate_shop_meta_cache()
                tab = "shop_web"
            elif action == "update_shop_business_type":
                shop_key = request.form.get("shop_key", "").strip()
                business_type = request.form.get("business_type", "cty").strip().lower()
                _valid_bt = shop_types.get_valid_type_codes() if shop_types else {"cty", "hkd", "pos3"}
                if business_type not in _valid_bt:
                    business_type = "cty"
                if not shop_key:
                    messages.append({"level": "Lỗi", "text": "Thiếu shop_key."})
                else:
                    db_ok = False
                    if get_db_conn:
                        try:
                            with get_db_conn() as _conn:
                                _cur = _conn.cursor()
                                _cur.execute("UPDATE shops SET business_type=%s WHERE shop_key=%s",
                                             (business_type, shop_key))
                                if _cur.rowcount > 0:
                                    _conn.commit()
                                    db_ok = True
                                else:
                                    messages.append({"level": "Lỗi", "text": f"Không tìm thấy shop '{shop_key}'."})
                        except Exception as _e:
                            messages.append({"level": "Lỗi", "text": f"Cập nhật loại shop thất bại: {_e}"})
                    if db_ok:
                        # Đồng bộ JSON
                        try:
                            shops_raw = try_parse_json(SHOPS_FILE) if os.path.exists(SHOPS_FILE) else []
                            if isinstance(shops_raw, list):
                                for _r in shops_raw:
                                    if isinstance(_r, dict) and str(_r.get("shop_key", "")) == shop_key:
                                        _r["business_type"] = business_type
                                save_json_file(SHOPS_FILE, shops_raw)
                        except Exception:
                            pass
                        _tm = shop_types.get_type_map() if shop_types else {}
                        bt_label = (_tm.get(business_type) or {}).get("label") or business_type.upper()
                        messages.append({"level": "Thành công", "text": f"Đã đặt loại shop '{shop_key}' = {bt_label}."})
                        invalidate_shop_meta_cache()
                tab = "shop_web"
            elif action in ("add_shop_type", "update_shop_type"):
                tab = "shop_web"
                if not shop_types:
                    messages.append({"level": "Lỗi", "text": "Module loại shop chưa sẵn sàng."})
                else:
                    code = request.form.get("type_code", "").strip().lower()
                    label = request.form.get("type_label", "").strip()
                    icon = request.form.get("type_icon", "").strip()
                    color = request.form.get("type_color", "").strip() or "#1d4ed8"
                    bg_color = request.form.get("type_bg_color", "").strip() or "#dbeafe"
                    active = request.form.get("type_active", "1").strip() != "0"
                    try:
                        sort_order = int(request.form.get("type_sort", "100").strip() or 100)
                    except ValueError:
                        sort_order = 100
                    # Mã chỉ cho chữ/số/gạch dưới — tránh ký tự lạ làm lệch filter
                    import re as _re
                    if not code or not _re.match(r"^[a-z0-9_]+$", code):
                        messages.append({"level": "Lỗi", "text": "Mã loại chỉ gồm chữ thường/số/gạch dưới (vd: aff, ctv)."})
                    elif not label:
                        messages.append({"level": "Lỗi", "text": "Thiếu tên loại shop."})
                    else:
                        try:
                            shop_types.upsert_shop_type(code, label, icon, color, bg_color, sort_order, active)
                            invalidate_shop_meta_cache()
                            _verb = "Đã cập nhật" if action == "update_shop_type" else "Đã thêm"
                            messages.append({"level": "Thành công", "text": f"{_verb} loại shop '{label}' ({code})."})
                        except Exception as _e:
                            messages.append({"level": "Lỗi", "text": f"Lưu loại shop thất bại: {_e}"})
            elif action == "toggle_shop_type":
                tab = "shop_web"
                code = request.form.get("type_code", "").strip().lower()
                active = request.form.get("type_active", "1").strip() == "1"
                if shop_types and code:
                    try:
                        shop_types.set_active(code, active)
                        invalidate_shop_meta_cache()
                        messages.append({"level": "Thành công", "text": f"Đã {'hiện' if active else 'ẩn'} loại '{code}'."})
                    except Exception as _e:
                        messages.append({"level": "Lỗi", "text": f"Đổi trạng thái loại thất bại: {_e}"})
            elif action == "delete_shop_type":
                tab = "shop_web"
                code = request.form.get("type_code", "").strip().lower()
                if shop_types and code:
                    err = shop_types.delete_shop_type(code)
                    if err:
                        messages.append({"level": "Lỗi", "text": err})
                    else:
                        invalidate_shop_meta_cache()
                        messages.append({"level": "Thành công", "text": f"Đã xoá loại '{code}'."})
            elif action == "toggle_shop_status":
                shop_key = request.form.get("shop_key", "").strip()
                if not shop_key:
                    messages.append({"level": "Lỗi", "text": "Thiếu shop_key."})
                else:
                    toggled = False
                    if get_db_conn:
                        try:
                            with get_db_conn() as _conn:
                                _cur = _conn.cursor()
                                _cur.execute("SELECT status::text FROM shops WHERE shop_key=%s", (shop_key,))
                                _row = _cur.fetchone()
                                if _row:
                                    cur_status = str(_row[0] or "").strip().lower()
                                    new_status = "inactive" if cur_status == "active" else "active"
                                    _cur.execute(
                                        "UPDATE shops SET status=%s::record_status WHERE shop_key=%s",
                                        (new_status, shop_key)
                                    )
                                    _conn.commit()
                                    toggled = True
                        except Exception as _e:
                            messages.append({"level": "Cảnh báo", "text": f"Cập nhật DB thất bại: {_e}"})
                    # Đồng bộ wh_shops
                    if toggled:
                        try:
#                            from modules.kho_vat_ly.wh_db import wh_db as _wh_db
                            with _wh_db() as _conn:
                                _conn.execute(
                                    "UPDATE wh_shops SET status=%s WHERE shop_key=%s",
                                    (new_status, shop_key)
                                )
                        except Exception:
                            pass
                        # Backup JSON
                        try:
                            shops_raw = try_parse_json(SHOPS_FILE) if os.path.exists(SHOPS_FILE) else []
                            if isinstance(shops_raw, list):
                                for _r in shops_raw:
                                    if str(_r.get("shop_key", "")).strip() == shop_key:
                                        _r["status"] = new_status
                                        break
                                save_json_file(SHOPS_FILE, shops_raw)
                        except Exception:
                            pass
                        messages.append({"level": "Thành công", "text": f"Đã {'kích hoạt' if new_status == 'active' else 'tắt'} shop '{shop_key}'."})
                        invalidate_shop_meta_cache()
                    elif not toggled and not any(m["level"] == "Cảnh báo" for m in messages[-1:]):
                        messages.append({"level": "Lỗi", "text": f"Không tìm thấy shop '{shop_key}' trong DB."})
                tab = "shop_web"
            elif action == "update_shop_api_key":
                shop_key = request.form.get("shop_key", "").strip()
                pos_api_key = request.form.get("pos_api_key", "").strip()
                if not shop_key:
                    messages.append({"level": "Lỗi", "text": "Thiếu shop_key."})
                else:
                    try:
#                        from modules.kho_vat_ly.wh_db import wh_db as _wh_db
                        with _wh_db() as _conn:
                            _conn.execute(
                                "UPDATE wh_shops SET pos_api_key=%s WHERE shop_key=%s",
                                (pos_api_key or None, shop_key)
                            )
                        try:
                            import pancake_auth as _pa; _pa.clear_cache()
                        except Exception:
                            pass
                        if pos_api_key:
                            messages.append({"level": "Thành công", "text": f"Đã lưu API key cho {shop_key}."})
                        else:
                            messages.append({"level": "Thành công", "text": f"Đã xoá API key của {shop_key} (sẽ chỉ hoạt động local)."})
                        invalidate_shop_meta_cache()
                    except Exception as _e:
                        messages.append({"level": "Lỗi", "text": f"Lưu thất bại: {_e}"})
                tab = "shop_web"
            elif action == "update_shop_info":
                shop_key = request.form.get("shop_key", "").strip()
                new_shop_name = request.form.get("shop_name", "").strip()
                new_team_code = request.form.get("shop_team_id", "").strip()
                new_pancake_id = request.form.get("shop_id", "").strip()
                # Các field bổ sung từ modal "Sửa shop" (đầy đủ trong 1 lần Lưu).
                # Chỉ áp khi field được gửi lên (modal có), để không phá form cũ.
                new_business_type = request.form.get("business_type", "").strip().lower()
                _valid_bt = shop_types.get_valid_type_codes() if shop_types else {"cty", "hkd", "pos3"}
                if new_business_type and new_business_type not in _valid_bt:
                    new_business_type = ""
                new_status_raw = request.form.get("shop_status", "").strip().lower()
                new_status = new_status_raw if new_status_raw in ("active", "inactive") else ""
                # API key: để trống = GIỮ NGUYÊN (không đụng), nhập mới = thay
                new_api_key = request.form.get("pos_api_key", "").strip()
                # Validate pos_shop_id: chỉ số, 8-12 ký tự (phòng admin paste/gõ nhầm
                # gây corruption như "1021311494333", "1328994")
                if new_pancake_id:
                    if not new_pancake_id.isdigit() or not (8 <= len(new_pancake_id) <= 12):
                        messages.append({"level": "Lỗi", "text": f"pos_shop_id không hợp lệ: '{new_pancake_id}' (phải là chuỗi số 8-12 ký tự)."})
                        new_pancake_id = ""  # bỏ qua, không UPDATE
                if not shop_key:
                    messages.append({"level": "Lỗi", "text": "Thiếu shop_key."})
                elif not new_shop_name:
                    messages.append({"level": "Lỗi", "text": "Tên shop không được trống."})
                else:
                    if get_db_conn:
                        try:
                            with get_db_conn() as _conn:
                                _cur = _conn.cursor()
                                team_db_id = None
                                if new_team_code:
                                    _cur.execute("SELECT id FROM teams WHERE team_code=%s", (new_team_code,))
                                    _r = _cur.fetchone()
                                    team_db_id = _r[0] if _r else None
                                update_fields = ["shop_name=%s", "team_id=%s"]
                                update_vals = [new_shop_name, team_db_id]
                                if new_pancake_id:
                                    update_fields.append("pancake_shop_id=%s")
                                    update_vals.append(new_pancake_id)
                                if new_business_type:
                                    update_fields.append("business_type=%s")
                                    update_vals.append(new_business_type)
                                if new_status:
                                    update_fields.append("status=%s::record_status")
                                    update_vals.append(new_status)
                                update_vals.append(shop_key)
                                _cur.execute(
                                    f"UPDATE shops SET {', '.join(update_fields)} WHERE shop_key=%s",
                                    update_vals
                                )
                                _conn.commit()
                        except Exception as _e:
                            messages.append({"level": "Cảnh báo", "text": f"Cập nhật DB thất bại: {_e}"})
                    # Đồng bộ wh_shops (tên, pos_shop_id, status, api_key)
                    try:
#                        from modules.kho_vat_ly.wh_db import wh_db as _wh_db
                        with _wh_db() as _conn:
                            _wh_fields = ["shop_name=?"]
                            _wh_vals = [new_shop_name]
                            if new_pancake_id:
                                _wh_fields.append("pos_shop_id=?")
                                _wh_vals.append(new_pancake_id)
                            if new_status:
                                _wh_fields.append("status=?")
                                _wh_vals.append(new_status)
                            if new_api_key:
                                _wh_fields.append("pos_api_key=?")
                                _wh_vals.append(new_api_key)
                            _wh_vals.append(shop_key)
                            _conn.execute(
                                f"UPDATE wh_shops SET {', '.join(_wh_fields)} WHERE shop_key=?",
                                _wh_vals
                            )
                    except Exception:
                        pass
                    # Backup JSON
                    try:
                        shops_raw = try_parse_json(SHOPS_FILE) if os.path.exists(SHOPS_FILE) else []
                        if isinstance(shops_raw, list):
                            for _r in shops_raw:
                                if str(_r.get("shop_key", "")).strip() == shop_key:
                                    _r["shop_name"] = new_shop_name
                                    if new_team_code:
                                        _r["team_id"] = new_team_code
                                    if new_pancake_id:
                                        _r["shop_id"] = new_pancake_id
                                    if new_business_type:
                                        _r["business_type"] = new_business_type
                                    if new_status:
                                        _r["status"] = new_status
                                    break
                            save_json_file(SHOPS_FILE, shops_raw)
                    except Exception:
                        pass
                    messages.append({"level": "Thành công", "text": f"Đã cập nhật thông tin shop '{shop_key}'."})
                    invalidate_shop_meta_cache()
                tab = "shop_web"
            elif action == "add_website":
                website_name = request.form.get("website_name", "").strip()
                website_url = request.form.get("website_url", "").strip()
                website_shop_key = request.form.get("website_shop_key", "").strip()
                website_status = request.form.get("website_status", "active").strip()
                websites = load_websites()
                if not website_name or not website_url:
                    messages.append({"level": "Lỗi", "text": "Thiếu tên website hoặc URL."})
                else:
                    websites.append({
                        "id": f"w-{uuid.uuid4().hex[:8]}",
                        "name": website_name,
                        "url": website_url,
                        "shop_key": website_shop_key,
                        "status": "active" if website_status == "active" else "inactive",
                    })
                    save_websites(websites)
                    messages.append({"level": "Thành công", "text": f"Đã thêm website '{website_name}'."})
                tab = "shop_web"
            elif action == "toggle_website_status":
                website_id = request.form.get("website_id", "").strip()
                websites = load_websites()
                for website in websites:
                    if str(website.get("id", "")).strip() == website_id:
                        old_status = str(website.get("status", "active"))
                        website["status"] = "inactive" if old_status == "active" else "active"
                        break
                save_websites(websites)
                messages.append({"level": "Thành công", "text": "Đã cập nhật trạng thái website."})
                tab = "shop_web"
            elif action == "save_permissions":
                perms = perm_utils.load_perms()
                modules = perms.get("modules", [])
                cu = perms.get("role_permissions", {}) or {}
                # BẮT ĐẦU TỪ QUYỀN CŨ, chỉ ghi đè role có trên form.
                # (Trước đây dựng dict RỖNG rồi chỉ điền 5 role → role nào không nằm
                #  trong danh sách, vd sale / sale_leader, bị XOÁ SẠCH quyền sau mỗi
                #  lần bấm Lưu → nhân viên sale mất quyền, đăng nhập bị văng.)
                new_role_perms = dict(cu)
                all_roles = ["accountant", "kho", "leader", "staff", "it"]
                for role_key in all_roles:
                    new_role_perms[role_key] = request.form.getlist(f"perm_{role_key}")
                # Super role luôn toàn quyền
                for r in ("admin", "superadmin", "manager"):
                    new_role_perms[r] = cu.get(r, ["*"])
                perms["role_permissions"] = new_role_perms
                perm_utils.save_perms(perms)
                messages.append({"level": "Thành công", "text": "Đã lưu phân quyền."})
                tab = "phan_quyen"
            elif action == "add_perm_detected":
                # Thêm nhanh các tính năng ĐANG CHẠY mà chưa có trong permissions.json
                from flask import current_app as _app
                perms = perm_utils.load_perms()
                mods = perms.get("modules", [])
                co = {m["key"] for m in mods}
                nhan = {b: (getattr(_app.blueprints[b], "name", b) or b) for b in _app.blueprints}
                them = 0
                for k in request.form.getlist("detected"):
                    if k and k not in co:
                        mods.append({"key": k, "label": _PERM_NHAN.get(k, k), "group": "Khác"})
                        co.add(k); them += 1
                perms["modules"] = mods
                perm_utils.save_perms(perms)
                messages.append({"level": "Thành công",
                                 "text": f"Đã thêm {them} tính năng vào phân quyền. "
                                         "Mặc định chỉ Admin/Manager có quyền — hãy tick cho vai trò khác."})
                tab = "phan_quyen"
            elif action == "add_perm_module":
                tab = "phan_quyen"
                import re as _re, unicodedata as _ud
                p_key = request.form.get("perm_key", "").strip().lower()
                p_label = request.form.get("perm_label", "").strip()
                p_group = request.form.get("perm_group", "").strip() or "Khác"
                p_path = request.form.get("perm_path", "").strip()
                # Chỉ cần nhập TÊN → tự sinh mã quyền (slug từ tên, bỏ dấu tiếng Việt).
                if not p_key and p_label:
                    _s = _ud.normalize("NFKD", p_label).encode("ascii", "ignore").decode().lower()
                    _s = _re.sub(r"[^a-z0-9]+", "_", _s).strip("_")
                    p_key = _s[:40] or ("perm_" + str(abs(hash(p_label)) % 10000))
                if not p_key or not _re.match(r"^[a-z0-9_]+$", p_key):
                    messages.append({"level": "Lỗi", "text": "Mã quyền chỉ gồm chữ thường/số/gạch dưới (vd: bao_cao)."})
                elif not p_label:
                    messages.append({"level": "Lỗi", "text": "Thiếu tên tính năng."})
                else:
                    perms = perm_utils.load_perms()
                    modules = perms.setdefault("modules", [])
                    if any(m.get("key") == p_key for m in modules):
                        messages.append({"level": "Lỗi", "text": f"Quyền '{p_key}' đã tồn tại."})
                    else:
                        modules.append({"group": p_group, "key": p_key, "label": p_label})
                        if p_path:
                            if not p_path.startswith("/"):
                                p_path = "/" + p_path
                            perms.setdefault("path_map", {})[p_path] = p_key
                        perm_utils.save_perms(perms)
                        messages.append({"level": "Thành công", "text": f"Đã thêm quyền '{p_label}' ({p_key})."})
            elif action == "delete_perm_module":
                tab = "phan_quyen"
                p_key = request.form.get("perm_key", "").strip().lower()
                if not p_key:
                    messages.append({"level": "Lỗi", "text": "Thiếu mã quyền."})
                else:
                    perms = perm_utils.load_perms()
                    perms["modules"] = [m for m in perms.get("modules", []) if m.get("key") != p_key]
                    perms["path_map"] = {p: k for p, k in perms.get("path_map", {}).items() if k != p_key}
                    for _rk, _lst in (perms.get("role_permissions", {}) or {}).items():
                        if isinstance(_lst, list) and p_key in _lst:
                            perms["role_permissions"][_rk] = [x for x in _lst if x != p_key]
                    perm_utils.save_perms(perms)
                    messages.append({"level": "Thành công", "text": f"Đã xoá quyền '{p_key}'."})

    can_manage = validate_settings_tab_access(tab)
    active_label = next((t["label"] for t in tabs if t["key"] == tab), "Tổng quan")
    users = load_users()
    _shops_base = sorted(load_shop_meta_map().values(), key=lambda x: x.get("shop_name", ""))
    try:
#        from modules.kho_vat_ly.wh_db import wh_db as _wh_db
        with _wh_db() as _wconn:
            _wh_rows = _wconn.execute(
                "SELECT shop_key, pos_api_key, pos_shop_id, last_webhook_at FROM wh_shops"
            ).fetchall()
        _api_key_map = {str(r["shop_key"]): str(r["pos_api_key"] or "") for r in _wh_rows}
        _webhook_map = {str(r["shop_key"]): r["last_webhook_at"] for r in _wh_rows}
        _pos_shop_id_map = {str(r["shop_key"]): str(r["pos_shop_id"] or "") for r in _wh_rows}
    except Exception:
        _api_key_map = {}
        _webhook_map = {}
        _pos_shop_id_map = {}
    shops = []
    for _s in _shops_base:
        _s2 = dict(_s)
        _key = str(_s.get("shop_key", ""))
        _s2["pos_api_key"] = _api_key_map.get(_key, "")
        _s2["last_webhook_at"] = _webhook_map.get(_key)
        _s2["wh_pos_shop_id"] = _pos_shop_id_map.get(_key, "")
        shops.append(_s2)
    websites = load_websites()
    fb_ad_mappings = list_fb_ad_mappings_for_settings()
    edit_mapping_id = request.args.get("edit_mapping_id", "").strip()
    fb_edit_mapping = {"id": "", "shop_key": "", "fb_ad_account_id": "", "account_name": "", "status": "active"}
    if edit_mapping_id:
        selected = next((x for x in fb_ad_mappings if str(x.get("id", "")) == edit_mapping_id), None)
        if selected:
            fb_edit_mapping = {
                "id": str(selected.get("id", "")),
                "shop_key": str(selected.get("shop_key", "")),
                "fb_ad_account_id": str(selected.get("fb_ad_account_id", "")),
                "account_name": str(selected.get("account_name", "")),
                "status": str(selected.get("status", "active")),
            }
    all_shop_keys = [s.get("shop_key", "") for s in shops if s.get("shop_key", "")]
    # (shop_key, business_type) — dùng cho dropdown có nhãn CTY/HKD
    all_shops_with_type = [
        {"shop_key": s.get("shop_key", ""), "business_type": (s.get("business_type") or "cty").lower()}
        for s in shops if s.get("shop_key", "")
    ]
    missing_team_shops = get_shops_missing_team_id()
    missing_team_preview_items = [f"{x['shop_key']} ({x['shop_name']})" for x in missing_team_shops[:8]]
    missing_team_preview = ", ".join(missing_team_preview_items)
    missing_team_more_count = max(0, len(missing_team_shops) - len(missing_team_preview_items))

    # Lấy danh sách kho vật lý cho phân quyền nhân viên
    all_warehouses = []
    try:
        with get_db_conn() as _wh_conn:
            _wh_cur = _wh_conn.cursor()
            _wh_cur.execute("SELECT id, code, name FROM wh_warehouses WHERE status='active' ORDER BY id")
            all_warehouses = [{"id": r[0], "code": r[1], "name": r[2]} for r in _wh_cur.fetchall()]
    except Exception:
        all_warehouses = []

    # Danh sách teams (từ bảng teams DB) — dùng cho dropdown đổi team của NV
    all_teams = []
    try:
        with get_db_conn() as _tc:
            _tcur = _tc.cursor()
            _tcur.execute("SELECT team_code, team_name FROM teams WHERE status='active' AND team_code IS NOT NULL ORDER BY team_code")
            all_teams = [{"code": r[0], "name": r[1]} for r in _tcur.fetchall() if r[0]]
    except Exception:
        all_teams = []

    display_users = []
    for user in users:
        # [MOON] Ẩn tài khoản chủ phần mềm (admin/superadmin) khỏi danh sách để bảo vệ billing:
        # chủ đăng nhập trực tiếp, khách (kể cả manager) không thấy/không sửa được.
        if str(user.get("role", "")).strip() in ("admin", "superadmin"):
            continue
        assigned = user.get("assigned_shops", ["*"])
        if isinstance(assigned, list):
            assigned_text = ", ".join(assigned) if assigned else "*"
        else:
            assigned_text = str(assigned or "*")
        display_users.append({
            "id": str(user.get("id", "")),
            "username": str(user.get("username", "")),
            "full_name": str(user.get("full_name", "") or ""),
            "role": str(user.get("role", "staff")),
            "team_id": str(user.get("team_id", "")),
            "assigned_shops": assigned_text,
            "assigned_from": user.get("assigned_from") or "",
            "status": str(user.get("status", "active")),
            "warehouse_id": user.get("warehouse_id") or "",
            "warehouse_name": str(user.get("warehouse_name") or ""),
        })

    leader_team_id = ""
    leader_has_team = False
    leader_team_label = ""
    orphan_staff_users: List[Dict[str, str]] = []
    leader_team_staff_users: List[Dict[str, str]] = []
    leader_team_scope_users: List[Dict[str, str]] = []
    leader_manageable_shop_keys: List[str] = []
    leader_self_user: Dict[str, str] = {"id": "", "username": ""}
    if is_leader_user():
        me = current_user() or {}
        leader_self_user = {
            "id": str(me.get("id", "")),
            "username": str(me.get("username", "")),
        }
        leader_team_id = str(me.get("team_id", "")).strip()
        leader_has_team = bool(leader_team_id)
        leader_team_label = leader_team_id or "(chưa có team_id — liên hệ admin)"
        # sale_leader quản lý nhân viên role='sale'; leader thường quản lý role='staff'
        _managed_role = "sale" if str(me.get("role", "")).strip() == "sale_leader" else "staff"
        shop_m = load_shop_meta_map()
        if leader_team_id:
            leader_manageable_shop_keys = sorted(get_leader_manageable_shop_keys(me, shop_m, users))
        for user in users:
            uid = str(user.get("id", ""))
            uname = str(user.get("username", ""))
            role = str(user.get("role", "staff")).strip()
            tid = str(user.get("team_id", "")).strip()
            st = str(user.get("status", "active")).strip()
            assigned = user.get("assigned_shops", ["*"])
            if isinstance(assigned, list):
                assigned_text = ", ".join(assigned) if assigned else "*"
            else:
                assigned_text = str(assigned or "*")
            if tid == leader_team_id and role not in {"admin", "accountant"}:
                leader_team_scope_users.append({
                    "id": uid,
                    "username": uname,
                    "role": role,
                    "team_id": tid,
                    "assigned_shops": assigned_text,
                    "status": st,
                })
            if role == _managed_role and not tid and st == "active":
                orphan_staff_users.append({"id": uid, "username": uname})
            if role == _managed_role and tid == leader_team_id and st == "active":
                leader_team_staff_users.append({
                    "id": uid,
                    "username": uname,
                    "assigned_shops": assigned_text,
                })

    fb_token_rows: List[Dict[str, Any]] = []
    fb_token_store_error: Optional[str] = None
    if is_admin_user():
        fb_token_rows, fb_token_store_error = load_fb_token_rows_for_settings()
    fb_token_test_date_default = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")

    # Maintenance mode (chỉ admin thấy box điều khiển trong tab overview)
    _cfg_now = load_config() or {}
    maintenance_mode_on = str(_cfg_now.get("maintenance_mode", "0")) == "1"
    maintenance_message = str(_cfg_now.get("maintenance_message", "") or "")
    maintenance_started_at = str(_cfg_now.get("maintenance_started_at", "") or "")

    # Shop có API key invalid (≠ 32 hex) → sync silent skip
    invalid_apikey_shops = []
    try:
#        from modules.kho_vat_ly.wh_db import wh_db as _wh_db_check
        with _wh_db_check() as _ck:
            invalid_apikey_shops = [dict(r) for r in _ck.execute("""
                SELECT shop_name, pos_shop_id, pos_api_key
                FROM wh_shops
                WHERE COALESCE(status,'active') = 'active'
                  AND pos_api_key IS NOT NULL AND pos_api_key != ''
                  AND (LENGTH(pos_api_key) != 32
                       OR pos_api_key !~ '^[0-9a-fA-F]+$')
                ORDER BY shop_name
            """).fetchall()]
    except Exception:
        pass

    # Tìm tên shop + lọc theo team (tab Shop & Web) — áp TRƯỚC phân trang
    shop_q = request.args.get("shop_q", "").strip()
    shop_team_filter = request.args.get("shop_team", "").strip()
    shop_team_options = sorted({
        str(s.get("team_id") or "").strip()
        for s in shops if str(s.get("team_id") or "").strip()
    })
    shops_filtered = shops
    if shop_q:
        _qlow = shop_q.lower()
        shops_filtered = [
            s for s in shops_filtered
            if _qlow in str(s.get("shop_name", "")).lower()
            or _qlow in str(s.get("shop_key", "")).lower()
            or _qlow in str(s.get("shop_id", "")).lower()
        ]
    if shop_team_filter:
        shops_filtered = [
            s for s in shops_filtered
            if str(s.get("team_id") or "").strip() == shop_team_filter
        ]

    # Phân trang bảng shop trong tab Shop & Web (30 shop/trang). Giữ list full
    # cho các derived (all_shop_keys, all_shops_with_type...) — chỉ slice danh
    # sách hiển thị xuống template.
    SHOP_PAGE_SIZE = 30
    shop_total = len(shops_filtered)
    shop_total_pages = max(1, (shop_total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    try:
        shop_page = int(request.args.get("shop_page", 1) or 1)
    except (TypeError, ValueError):
        shop_page = 1
    if shop_page < 1:
        shop_page = 1
    if shop_page > shop_total_pages:
        shop_page = shop_total_pages
    _shop_start = (shop_page - 1) * SHOP_PAGE_SIZE
    shops_display = shops_filtered[_shop_start:_shop_start + SHOP_PAGE_SIZE]

    try:
        from modules.fb_pages import _fb_app_id as _faid, _fb_app_secret as _fasec, _redirect_uri as _fru
        _fb_id, _fb_sec, _fb_ru = _faid(), _fasec(), _fru()
    except Exception:
        _fb_id, _fb_sec, _fb_ru = "", "", ""

    body = render_template_string(
        SETTINGS_BODY,
        tabs=tabs,
        selected_tab=tab,
        fb_app_id=_fb_id,
        fb_app_secret_set=bool(_fb_sec),
        fb_redirect_uri=_fb_ru,
        active_label=active_label,
        invalid_apikey_shops=invalid_apikey_shops,
        system_info="Tiểu Hiềm Software settings module",
        shops_count=len(load_shop_meta_map()),
        users_count=len(users),
        updated_at=now_hcm().strftime("%d/%m/%Y %H:%M"),
        current_username=session.get("username", ""),
        current_role=session.get("role", "staff"),
        is_admin_settings=is_admin_user(),
        leader_has_team=leader_has_team,
        leader_team_label=leader_team_label,
        leader_team_id=leader_team_id or "-",
        leader_self_user=leader_self_user,
        orphan_staff_users=orphan_staff_users,
        leader_team_staff_users=leader_team_staff_users,
        leader_team_scope_users=leader_team_scope_users,
        leader_manageable_shop_keys=leader_manageable_shop_keys,
        can_manage=can_manage,
        messages=messages,
        users=display_users,
        shops=shops_display,
        shop_total=shop_total,
        shop_page=shop_page,
        shop_total_pages=shop_total_pages,
        shop_page_size=SHOP_PAGE_SIZE,
        shop_q=shop_q,
        shop_team=shop_team_filter,
        shop_team_options=shop_team_options,
        websites=websites,
        fb_ad_mappings=fb_ad_mappings,
        fb_edit_mapping=fb_edit_mapping,
        fb_db_ready=bool(get_db_conn and repo_list_fb_ad_account_mappings),
        fb_sync_default_date=datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d"),
        facebook_access_token_masked=mask_secret(get_facebook_access_token()),
        facebook_access_token_placeholder="Nhập token mới để cập nhật",
        all_shop_keys=all_shop_keys,
        all_shops_with_type=all_shops_with_type,
        shop_business_types=(shop_types.get_shop_business_types(active_only=False) if shop_types else []),
        all_warehouses=all_warehouses,
        all_teams=all_teams,
        missing_team_count=len(missing_team_shops),
        missing_team_preview=missing_team_preview or "-",
        missing_team_more_count=missing_team_more_count,
        fb_token_rows=fb_token_rows,
        fb_token_store_error=fb_token_store_error,
        fb_token_test_date_default=fb_token_test_date_default,
        perm_data=perm_utils.load_perms(),
        perm_missing=_perm_missing_modules(),
        perm_config_roles=[
            {"key": "accountant", "label": "Kế toán"},
            {"key": "kho",        "label": "Kho"},
            {"key": "leader",     "label": "Trưởng nhóm"},
            {"key": "staff",      "label": "Nhân viên"},
            {"key": "it",         "label": "IT"},
        ],
        shop_errors=get_shop_errors() if is_admin_user() else [],
        maintenance_mode_on=maintenance_mode_on,
        maintenance_message=maintenance_message,
        maintenance_started_at=maintenance_started_at,
        zalo_sync_secret=(os.environ.get("ZALO_COOKIE_SYNC_SECRET") or "").strip(),
        ads_payers_value=_cfg_now.get("expense_ads_payers") or "",
        ai_models_data={
            "deepseek_api_key":  bool(_cfg_now.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY")),
            "gemini_api_key":    bool(_cfg_now.get("gemini_api_key")),
            "gemini_model":      _cfg_now.get("gemini_model") or "gemini-2.0-pro-exp",
            "openai_api_key":    bool(_cfg_now.get("openai_api_key")),
            "anthropic_api_key": bool(_cfg_now.get("anthropic_api_key")),
        },
    )
    return render_template_string(PAGE_TEMPLATE, title="Cài đặt", body=body)


@settings_bp.route("/settings/api/update-shop-key", methods=["POST"])
@login_required
def settings_update_shop_key():
    """AJAX: cập nhật pos_api_key cho một shop."""
    if not is_admin_user():
        return jsonify({"ok": False, "error": "Chỉ admin mới có quyền."}), 403
    data = request.get_json(silent=True) or {}
    shop_key = (data.get("shop_key") or "").strip()
    pos_api_key = (data.get("pos_api_key") or "").strip() or None
    if not shop_key:
        return jsonify({"ok": False, "error": "Thiếu shop_key."}), 400
    try:
        # Dùng connection direct (không qua wh_db._adapt — _adapt rewrite shops→wh_shops làm hỏng JOIN sau)
        from db import get_conn as _get_conn
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wh_shops SET pos_api_key=%s WHERE shop_key=%s",
                    (pos_api_key, shop_key)
                )
                if cur.rowcount == 0:
                    # wh_shops chưa có row → INSERT từ shops (shop inactive/mới chưa import sang module kho)
                    cur.execute("""
                        INSERT INTO wh_shops (shop_key, shop_name, pos_shop_id, pos_api_key, status)
                        SELECT s.shop_key, s.shop_name, COALESCE(s.pancake_shop_id, ''), %s, s.status::text
                        FROM shops s WHERE s.shop_key = %s
                        ON CONFLICT (shop_key) DO UPDATE SET pos_api_key = EXCLUDED.pos_api_key
                    """, (pos_api_key, shop_key))
                    if cur.rowcount == 0:
                        return jsonify({
                            "ok": False,
                            "error": f"Không tìm thấy shop_key='{shop_key}' trong bảng shops. Thêm shop trước ở Settings."
                        }), 404
            conn.commit()
        try:
            import pancake_auth as _pa; _pa.clear_cache()
        except Exception:
            pass
        try:
            from app_ctx import invalidate_shop_meta_cache
            invalidate_shop_meta_cache()
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as exc:
        logging.exception("[update-shop-key] exception")
        return jsonify({"ok": False, "error": str(exc)}), 500


@settings_bp.route("/settings/api/delete-shop", methods=["POST"])
@login_required
def settings_delete_shop():
    """AJAX: xoá HẲN 1 shop khỏi DB.
    Body: {shop_key, confirm: 'preview' | 'execute'}
    - 'preview' → trả về số row sẽ bị xoá ở từng bảng (không xoá thật)
    - 'execute' → xoá thật trong transaction
    """
    if not is_admin_user():
        return jsonify({"ok": False, "error": "Chỉ admin mới có quyền."}), 403
    data = request.get_json(silent=True) or {}
    shop_key = (data.get("shop_key") or "").strip()
    mode = (data.get("confirm") or "preview").strip().lower()
    if not shop_key:
        return jsonify({"ok": False, "error": "Thiếu shop_key."}), 400
    if mode not in ("preview", "execute"):
        return jsonify({"ok": False, "error": "confirm phải là 'preview' hoặc 'execute'."}), 400

    try:
        from db import get_conn as _get_conn
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # Lấy id từ cả 2 bảng
                cur.execute("SELECT id, shop_name, pancake_shop_id FROM shops WHERE shop_key=%s", (shop_key,))
                row_shops = cur.fetchone()
                cur.execute("SELECT id FROM wh_shops WHERE shop_key=%s", (shop_key,))
                row_wh = cur.fetchone()
                shops_id = int(row_shops[0]) if row_shops else None
                wh_id    = int(row_wh[0])    if row_wh    else None
                shop_name = (row_shops[1] if row_shops else "") or ""
                pos_id    = (row_shops[2] if row_shops else "") or ""

                if shops_id is None and wh_id is None:
                    # Idempotent: shop có thể đã xoá nhưng UI còn cache → bust cache + báo ok
                    try:
                        from app_ctx import invalidate_shop_meta_cache
                        invalidate_shop_meta_cache()
                    except Exception:
                        pass
                    if mode == "execute":
                        return jsonify({"ok": True, "executed": True,
                                        "summary": {"shop_key": shop_key, "shop_name": "(đã không tồn tại)",
                                                    "pos_shop_id": "", "shops_id": None, "wh_shops_id": None,
                                                    "counts": {}},
                                        "deleted": {"already_gone": 1}})
                    return jsonify({"ok": True,
                                    "preview": {"shop_key": shop_key, "shop_name": "(đã không tồn tại trong DB)",
                                                "pos_shop_id": "", "shops_id": None, "wh_shops_id": None,
                                                "counts": {}}})

                # Đếm row sẽ bị ảnh hưởng
                counts = {}
                def _c(label, sql, params):
                    try:
                        cur.execute(sql, params)
                        counts[label] = int(cur.fetchone()[0] or 0)
                    except Exception:
                        counts[label] = -1

                if wh_id is not None:
                    _c("wh_outbound_requests", "SELECT COUNT(*) FROM wh_outbound_requests WHERE shop_id=%s", (wh_id,))
                    _c("wh_stock_movements",   "SELECT COUNT(*) FROM wh_stock_movements   WHERE shop_id=%s", (wh_id,))
                    _c("wh_shop_inventory",    "SELECT COUNT(*) FROM wh_shop_inventory    WHERE shop_id=%s", (wh_id,))
                    _c("wh_phanbo",            "SELECT COUNT(*) FROM wh_phanbo            WHERE shop_id=%s", (wh_id,))
                if shops_id is not None:
                    _c("daily_shop_metrics",   "SELECT COUNT(*) FROM daily_shop_metrics   WHERE shop_id=%s", (shops_id,))
                    _c("fb_ad_account_mappings", "SELECT COUNT(*) FROM fb_ad_account_mappings WHERE shop_id=%s", (shops_id,))

                # Đếm NV gán shop này qua bảng user_shop_assignments
                if shops_id is not None:
                    cur.execute("SELECT COUNT(*) FROM user_shop_assignments WHERE shop_id=%s", (shops_id,))
                    counts["user_shop_assignments"] = int(cur.fetchone()[0] or 0)
                else:
                    counts["user_shop_assignments"] = 0

                summary = {
                    "shop_key": shop_key,
                    "shop_name": shop_name,
                    "pos_shop_id": pos_id,
                    "shops_id": shops_id,
                    "wh_shops_id": wh_id,
                    "counts": counts,
                }

                if mode == "preview":
                    return jsonify({"ok": True, "preview": summary})

                # EXECUTE — xoá tuần tự trong transaction
                deleted = {}
                def _d(label, sql, params):
                    cur.execute(sql, params)
                    deleted[label] = cur.rowcount

                # 1. Module kho (FK wh_shops.id)
                if wh_id is not None:
                    _d("wh_outbound_requests", "DELETE FROM wh_outbound_requests WHERE shop_id=%s", (wh_id,))
                    _d("wh_stock_movements",   "DELETE FROM wh_stock_movements   WHERE shop_id=%s", (wh_id,))
                    _d("wh_shop_inventory",    "DELETE FROM wh_shop_inventory    WHERE shop_id=%s", (wh_id,))
                    try:
                        _d("wh_phanbo", "DELETE FROM wh_phanbo WHERE shop_id=%s", (wh_id,))
                    except Exception:
                        pass

                # 2. Core (FK shops.id)
                if shops_id is not None:
                    _d("daily_shop_metrics",   "DELETE FROM daily_shop_metrics   WHERE shop_id=%s", (shops_id,))
                    _d("fb_ad_account_mappings", "DELETE FROM fb_ad_account_mappings WHERE shop_id=%s", (shops_id,))

                # 3. Gỡ liên kết NV ↔ shop trong user_shop_assignments
                if shops_id is not None:
                    _d("user_shop_assignments", "DELETE FROM user_shop_assignments WHERE shop_id=%s", (shops_id,))

                # 4. Row chính
                if wh_id is not None:
                    _d("wh_shops", "DELETE FROM wh_shops WHERE id=%s", (wh_id,))
                if shops_id is not None:
                    _d("shops", "DELETE FROM shops WHERE id=%s", (shops_id,))

                conn.commit()

                # Clear caches
                try:
                    import pancake_auth as _pa; _pa.clear_cache()
                except Exception:
                    pass
                try:
                    from app_ctx import invalidate_shop_meta_cache
                    invalidate_shop_meta_cache()
                except Exception:
                    pass

                return jsonify({"ok": True, "executed": True, "summary": summary, "deleted": deleted})

    except Exception as exc:
        logging.exception("[delete-shop] exception")
        return jsonify({"ok": False, "error": str(exc)}), 500


@settings_bp.route("/settings/db-backup")
@login_required
def settings_db_backup():
    """Xuất toàn bộ database ra file .dump (pg_dump custom format) để download."""
    import subprocess, shutil, tempfile
    if not is_admin_user():
        abort(403)
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        abort(500, "DATABASE_URL không được cấu hình.")
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        abort(500, "pg_dump không tìm thấy trên server.")
    ts = now_hcm().strftime("%Y%m%d_%H%M")
    filename = f"tieuhiem_backup_{ts}.dump"
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".dump", delete=False)
        tmp.close()
        result = subprocess.run(
            [pg_dump, db_url, "-F", "c", "-f", tmp.name],
            capture_output=True, timeout=300
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[:500]
            logging.error("[DB-BACKUP] pg_dump failed: %s", err)
            abort(500, f"pg_dump thất bại: {err}")
        return send_file(
            tmp.name,
            as_attachment=True,
            download_name=filename,
            mimetype="application/octet-stream"
        )
    except subprocess.TimeoutExpired:
        abort(500, "pg_dump timeout (>5 phút). Database quá lớn?")
    except Exception as exc:
        logging.exception("[DB-BACKUP] exception")
        abort(500, str(exc))


@settings_bp.route("/settings/fb-app-config", methods=["POST"])
@login_required
def save_fb_app_config():
    """Lưu App ID + App Secret Facebook vào app_config (cho luồng Đăng nhập Facebook
    lấy tên + ảnh page). Mỗi khách nhập app FB riêng trên giao diện, khỏi sửa env."""
    role = str(session.get("role", "")).strip().lower()
    if role not in ("admin", "superadmin", "it"):
        return redirect(url_for("settings.settings_page", tab="facebook_pages"))
    app_id = request.form.get("facebook_app_id", "").strip()
    app_secret = request.form.get("facebook_app_secret", "").strip()
    try:
        save_config_key("facebook_app_id", app_id)
        if app_secret:  # để trống = giữ nguyên secret cũ
            from modules.fb_pages import enc_secret
            save_config_key("facebook_app_secret", enc_secret(app_secret))  # mã hóa trước khi lưu
        flash("Đã lưu cấu hình App Facebook. Vào Page & Tài khoản → Đăng nhập Facebook để lấy page.", "success")
    except Exception as exc:
        flash(f"Lỗi lưu cấu hình: {exc}", "danger")
    return redirect(url_for("settings.settings_page", tab="facebook_pages"))


@settings_bp.route("/settings/ai-keys", methods=["POST"])
@login_required
def settings_ai_keys():
    """Lưu API keys của AI providers vào app_config.

    Mask khi đã set: nếu user gửi empty hoặc placeholder → giữ key cũ.
    """
    from app_ctx import save_config_key, load_config
    if not is_admin_user():
        abort(403)
    cfg = load_config() or {}
    KEYS = [
        ("deepseek_api_key",  "DeepSeek (text — chat parse)"),
        ("gemini_api_key",    "Gemini (vision — ảnh chi phí)"),
        ("gemini_model",      "Gemini model (vd gemini-2.0-pro-exp / gemini-1.5-pro)"),
        ("openai_api_key",    "OpenAI (dự phòng)"),
        ("anthropic_api_key", "Anthropic Claude (dự phòng)"),
    ]
    PLACEHOLDER = "••••••••"
    saved = []
    for key, label in KEYS:
        val = (request.form.get(key) or "").strip()
        if not val or val == PLACEHOLDER:
            continue  # giữ nguyên giá trị cũ
        save_config_key(key, val)
        saved.append(label)

    # Tên chủ chuyển ngân sách QC (mỗi lần submit thay luôn, kể cả rỗng = xoá)
    ads_payers = request.form.get("ads_payers")
    if ads_payers is not None:
        save_config_key("expense_ads_payers", ads_payers.strip())
        saved.append("Tên chủ chuyển NS QC")

    if saved:
        flash(f"Đã lưu: {', '.join(saved)}", "success")
    else:
        flash("Không có thay đổi.", "info")
    return redirect(url_for("settings.settings_page", tab="ai_models"))


@settings_bp.route("/settings/db-restore", methods=["POST"])
@login_required
def settings_db_restore():
    """Nhận file .dump upload từ trình duyệt và restore vào database hiện tại."""
    import subprocess, shutil, tempfile
    if not is_admin_user():
        return jsonify({"ok": False, "error": "Chỉ admin mới có quyền restore."}), 403
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        return jsonify({"ok": False, "error": "DATABASE_URL chưa được cấu hình."}), 500
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Không tìm thấy file upload."}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        file.save(tmp.name)
        tmp.close()
        if ext in (".dump", ".backup"):
            pg_restore = shutil.which("pg_restore")
            if not pg_restore:
                return jsonify({"ok": False, "error": "pg_restore không tìm thấy trên server."}), 500
            result = subprocess.run(
                [pg_restore, "--no-owner", "--clean", "--if-exists", "-d", db_url, tmp.name],
                capture_output=True, timeout=600
            )
        elif ext == ".sql":
            psql = shutil.which("psql")
            if not psql:
                return jsonify({"ok": False, "error": "psql không tìm thấy trên server."}), 500
            result = subprocess.run(
                [psql, db_url, "-f", tmp.name],
                capture_output=True, timeout=600
            )
        else:
            return jsonify({"ok": False, "error": "Định dạng file không hỗ trợ. Dùng .dump hoặc .sql"}), 400

        os.unlink(tmp.name)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[:800]
            # pg_restore với --clean thường có warning nhưng vẫn OK nếu returncode != 1
            if result.returncode == 1 and b"error" not in result.stderr.lower():
                return jsonify({"ok": True, "message": "Restore hoàn tất (có một số warning không nghiêm trọng)."})
            logging.error("[DB-RESTORE] failed rc=%s: %s", result.returncode, err)
            return jsonify({"ok": False, "error": f"Restore thất bại (rc={result.returncode}): {err}"}), 500
        return jsonify({"ok": True, "message": "Restore database thành công."})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Restore timeout (>10 phút). File quá lớn?"}), 500
    except Exception as exc:
        logging.exception("[DB-RESTORE] exception")
        return jsonify({"ok": False, "error": str(exc)}), 500





# ── Đồng bộ POS thủ công (admin/kế toán) — kéo đơn hàng/doanh thu/CP QC ngay ──
_SYNC_POS_LOCK = threading.Lock()


@settings_bp.route("/sync-pos", methods=["POST"])
@login_required
def sync_pos_data():
    """Bấm 'Đồng bộ POS' ở tab Shop & Web → chạy nền run_auto_refresh_all_data.sh
    (kéo đơn hàng + doanh thu + CP quảng cáo POS, KHÔNG kéo sản phẩm). Chỉ admin/kế toán."""
    role = str(session.get("role", "")).strip().lower()
    if role not in ("admin", "superadmin"):
        return redirect(url_for("settings.settings_page", tab="shop_web"))

    # ── Khoảng ngày cần kéo (Từ ngày → Đến ngày) ──
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    today = datetime.now(tz).date()

    def _parse(s, default):
        try:
            return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
        except Exception:
            return default

    d_to = _parse(request.form.get("date_to"), today)
    d_from = _parse(request.form.get("date_from"), d_to)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if d_to > today:
        d_to = today
    MAX_DAYS = 31  # giới hạn để không kéo quá nặng 1 lần
    if (d_to - d_from).days > MAX_DAYS - 1:
        d_from = d_to - timedelta(days=MAX_DAYS - 1)

    if not _SYNC_POS_LOCK.acquire(blocking=False):
        return redirect(url_for("settings.settings_page", tab="shop_web", synced="1"))

    script = os.path.join(BASE_DIR, "run_auto_refresh_all_data.sh")
    # Cửa sổ analytics phải đủ rộng để bao ngày cũ nhất
    span = (today - d_from).days + 2
    days = []
    cur = d_from
    while cur <= d_to:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    env = os.environ.copy()
    env["SKIP_SYNC_PRODUCTS"] = "1"   # không kéo sản phẩm/tồn kho
    env["SYNC_DAYS"] = str(span)

    def _worker(env_dict, day_list):
        try:
            for ds in day_list:
                subprocess.run(["bash", script, ds], cwd=BASE_DIR,
                               check=False, timeout=1800, env=env_dict)
        except Exception:
            logging.exception("[SYNC-POS] lỗi chạy auto refresh")
        finally:
            try:
                _SYNC_POS_LOCK.release()
            except Exception:
                pass

    threading.Thread(target=_worker, args=(env, days), daemon=True).start()
    return redirect(url_for("settings.settings_page", tab="shop_web", synced="1"))
