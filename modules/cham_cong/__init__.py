"""
Module Chấm công & Phân công việc — Tiểu Hiềm
Routes: /cham-cong/*
DB: PostgreSQL (prefix cc_)
Auth: dùng session của Posbot
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, date, timedelta

from flask import (Blueprint, jsonify, redirect, render_template,
                   request, session, url_for, flash)

_BASE = os.path.join(os.path.dirname(__file__), '..', '..')
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

try:
    from tz_utils import now_hcm, today_hcm as _today_hcm_str, to_hcm
except ImportError:
    def now_hcm():
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
    def _today_hcm_str():
        return now_hcm().strftime('%Y-%m-%d')
    def to_hcm(dt):
        if dt is None:
            return None
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        return dt.astimezone(timezone(timedelta(hours=7)))

def today_hcm() -> date:
    """Trả về date object hôm nay theo giờ HCM."""
    return date.fromisoformat(_today_hcm_str())

from .cc_db import (
    init_cc_tables, seed_employees_from_users, CC_ROLES, cc_role_label, cc_role_icon, cc_role_color,
    get_employee, get_all_employees, upsert_employee, create_standalone_employee, delete_employee,
    get_attendance, check_in as db_check_in, check_out as db_check_out,
    get_month_attendance, get_month_sessions_grouped, get_admin_monthly_stats,
    get_team_attendance_today, get_all_employees_status_today,
    get_monthly_report,
    get_tasks, get_all_tasks, get_task, create_task, update_task_status, delete_task, get_task_report,
    count_task_progress, get_task_progress, add_task_progress, delete_task_progress,
    get_kpi, upsert_kpi, get_kpi_history,
    get_announcements, create_announcement, delete_announcement, update_announcement,
    get_cc_settings, upsert_cc_setting, haversine_m, calc_cong_value,
    get_all_offices, get_office, upsert_office, delete_office,
    get_offices_for_team, office_teams, office_teams_map,
    assign_office_to_employees, assign_office_to_department,
    get_all_teams_from_db, assign_office_to_team_code,
    get_open_session, get_any_open_session, get_today_sessions, create_checkin_session,
    close_open_session, get_session_total_hours, backfill_checkout as db_backfill_checkout,
    seed_default_holidays_vn, get_holidays_range, list_holidays,
    upsert_holiday, delete_holiday, get_month_calendar,
    get_team_overtime_totals, split_regular_overtime,
    create_attendance_request, list_attendance_requests, count_pending_requests,
    get_attendance_request, cancel_attendance_request,
    reject_attendance_request, approve_attendance_request,
    resign_employee, restore_employee,
    admin_set_attendance_sessions,
)

bp = Blueprint('cham_cong', __name__,
               template_folder='templates',
               url_prefix='/cham-cong')

_PRIORITY_ORDER = {'urgent': 0, 'high': 1, 'normal': 2, 'low': 3}


@bp.app_template_filter('hcm_time')
def hcm_time_filter(dt, fmt='%H:%M'):
    """Jinja filter: convert UTC/naive datetime → giờ VN rồi format."""
    if dt is None:
        return '—'
    converted = to_hcm(dt)
    if converted is None:
        return '—'
    return converted.strftime(fmt)

# ─── Helpers ──────────────────────────────────────

def _current_user() -> dict | None:
    """Trả về user dict dựa theo session — đọc từ DB."""
    uid = session.get('user_id') or session.get('username')
    if not uid:
        return None
    try:
        import sys
        sys.path.insert(0, _BASE)
        from user_helpers import get_user_by_id, get_user_by_username
        # thử theo id trước, sau đó username
        u = get_user_by_id(uid) or get_user_by_username(str(uid))
        return u
    except Exception:
        pass
    return None


def _require_login():
    if not session.get('logged_in'):
        return redirect('/login?next=/cham-cong/')
    return None


def _team_code_from_id(team_id) -> str | None:
    """Resolve team_code (string) từ team_id input.

    Input có thể là:
    - int (vd 4): users.team_id raw từ DB → lookup teams.team_code
    - string số ("4"): tương tự, convert + lookup
    - string team_code ("team-ken"): user_helpers.load_all_users đã JOIN sẵn → return luôn
    - None / "" → None

    `cc_employees.department` lưu team_code → cần resolve trước khi filter.
    """
    if not team_id:
        return None
    s = str(team_id).strip()
    if not s:
        return None
    # Case 1: đã là team_code (chứa chữ hoặc dấu '-') → dùng luôn
    if not s.isdigit():
        return s
    # Case 2: là số → lookup teams table (cache 5 phút)
    cache_key = f"cc:team_code:{s}"
    cache_set = None
    try:
        from redis_cache import cache_get, cache_set as _cset
        cache_set = _cset
        cached = cache_get(cache_key)
        if cached:
            return cached
    except Exception:
        pass
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team_code FROM teams WHERE id=%s", (int(s),))
                row = cur.fetchone()
        code = row[0] if row else None
        if code and cache_set:
            try: cache_set(cache_key, code, ttl=300)
            except Exception: pass
        return code
    except Exception:
        return None


def _ctx_base(user: dict, emp: dict | None) -> dict:
    """Context chung cho mọi template."""
    cc_role = (emp or {}).get('cc_role', 'sale') if emp else 'sale'
    pos_role = (user or {}).get('role', 'staff')
    is_admin = pos_role in ('admin', 'superadmin', 'manager', 'accountant', 'it') or cc_role == 'admin'
    is_manager = is_admin or pos_role in ('leader', 'sale_leader')
    # leader_dept: None = thấy tất cả NV (admin); team_code (string) = chỉ thấy team mình
    if is_admin:
        leader_dept = None
    elif pos_role in ('leader', 'sale_leader'):
        # Phải map team_id (int) → team_code (string) vì query lọc theo team_code
        # và cc_employees.department. Bug cũ: truyền thẳng "4" → query không match.
        leader_dept = _team_code_from_id((user or {}).get('team_id'))
    else:
        leader_dept = '__self__'  # sentinel: staff chỉ thấy bản thân
    today = today_hcm()
    # Badge "Bổ sung công" — số đề nghị đang chờ duyệt (theo phạm vi quyền)
    try:
        if is_admin:
            pending_requests = count_pending_requests()
        elif is_manager:
            pending_requests = count_pending_requests(department=leader_dept)
        else:
            pending_requests = 0
    except Exception:
        pending_requests = 0
    return {
        'user': user,
        'emp': emp,
        'pending_requests': pending_requests,
        'cc_role': cc_role,
        'cc_role_label': cc_role_label(cc_role),
        'cc_role_icon': cc_role_icon(cc_role),
        'cc_role_color': cc_role_color(cc_role),
        'is_admin': is_admin,
        'is_manager': is_manager,
        # Quyền multi-select ngày để cộng dồn (kê toán lương): chỉ admin/IT/kế toán
        'can_select_days': pos_role in ('admin', 'superadmin', 'it', 'accountant', 'ketoan'),
        'leader_dept': leader_dept,
        'today': today,
        'CC_ROLES': CC_ROLES,
        'cc_role_label_fn': cc_role_label,
        'cc_role_icon_fn': cc_role_icon,
        'cc_role_color_fn': cc_role_color,
    }


def _fmt_duration(check_in, check_out) -> str:
    if not check_in or not check_out:
        return '—'
    ci = to_hcm(check_in)
    co = to_hcm(check_out)
    if not ci or not co:
        return '—'
    delta = co - ci
    # Làm tròn về phút gần nhất (round, không floor) — khớp với giờ hiển thị HH:MM
    # VD: 07:46:39 → 17:22:13 = 9h35min34s → round = 9h36p (≈ 9.60h trên AppSheet)
    total_min = round(delta.total_seconds() / 60)
    h, m = divmod(total_min, 60)
    return f"{h}g{m:02d}p"


# ─── Routes ──────────────────────────────────────

@bp.route('/')
def dashboard():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')

    uid = str(user['id'])
    emp = get_employee(uid)
    ctx = _ctx_base(user, emp)

    today = ctx['today']
    today_str = today.isoformat()

    # Chấm công hôm nay
    att = get_attendance(uid, today_str)
    ctx['att'] = att
    if att:
        ctx['att_duration'] = _fmt_duration(att.get('check_in'), att.get('check_out'))

    # Sessions hôm nay (multi check-in)
    today_sessions = get_today_sessions(uid, today_str)
    open_sess = get_open_session(uid, today_str)
    total_h = get_session_total_hours(uid, today_str)
    ctx['today_sessions'] = today_sessions
    ctx['open_session'] = open_sess
    ctx['total_hours_today'] = f'{int(total_h)}g{int((total_h % 1)*60):02d}p' if total_h > 0 else None

    # Phiên cũ chưa checkout từ ngày trước (chặn check-in mới)
    any_open = get_any_open_session(uid)
    if any_open and str(any_open.get('date', '')) != today_str:
        ctx['pending_old_session'] = any_open
    else:
        ctx['pending_old_session'] = None

    # Tasks hôm nay (todo + in-progress)
    my_tasks = [t for t in get_tasks(user_id=uid) if t['status'] in ('todo', 'in-progress')]
    my_tasks.sort(key=lambda t: _PRIORITY_ORDER.get(t['priority'], 2))
    ctx['my_tasks'] = my_tasks[:8]
    ctx['task_count'] = len(my_tasks)

    # Thông báo
    cc_role = ctx['cc_role']
    ctx['announcements'] = get_announcements(target_role=cc_role, limit=5)

    # Admin: xem team (leader chỉ thấy NV trong bộ phận của họ)
    if ctx['is_manager']:
        dept = ctx['leader_dept']
        ctx['team_today'] = get_team_attendance_today(today_str, department=dept)
        ctx['all_employees'] = get_all_employees(department=dept)

    return render_template('cham_cong/dashboard.html', **ctx)


@bp.route('/diem-danh')
def attendance():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')

    uid = str(user['id'])
    emp = get_employee(uid)
    ctx = _ctx_base(user, emp)

    today = ctx['today']

    # Tháng được xem
    try:
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
    except ValueError:
        year, month = today.year, today.month

    # Xem của nhân viên nào
    view_uid = request.args.get('uid', uid)
    if view_uid != uid:
        if not ctx['is_manager']:
            view_uid = uid  # staff chỉ xem mình
        elif not ctx['is_admin']:
            # leader: chỉ xem NV trong team của mình
            dept = ctx['leader_dept']
            if dept:
                team_uids = {str(e['user_id']) for e in get_all_employees(department=dept)}
                if str(view_uid) not in team_uids:
                    view_uid = uid

    ctx['view_uid'] = view_uid
    ctx['year'] = year
    ctx['month'] = month

    # Lịch sử tháng
    records = get_month_attendance(view_uid, year, month)
    records_map = {r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date']): r
                   for r in records}
    ctx['records'] = records
    ctx['records_map'] = records_map
    # Sessions từng ca chi tiết grouped by date (multi check-in/out)
    ctx['sessions_by_date'] = get_month_sessions_grouped(view_uid, year, month)

    # Thống kê tháng — tính từ sessions (từng ca riêng) thay vì records (checkin đầu→checkout cuối)
    all_sessions_flat = [s for lst in ctx['sessions_by_date'].values() for s in lst]
    present = len({
        (s['date'].isoformat() if hasattr(s['date'], 'isoformat') else str(s['date']))
        for s in all_sessions_flat if s.get('check_in')
    })
    full_day = sum(1 for r in records if r['check_in'] and r['check_out'])
    total_hours = sum(
        (s['check_out'] - s['check_in']).total_seconds() / 3600
        for s in all_sessions_flat if s.get('check_in') and s.get('check_out')
    )
    ctx['stat_present'] = present
    ctx['stat_full_day'] = full_day
    ctx['stat_total_hours'] = round(total_hours, 1)

    # Marketing (adser): thêm thống kê số công
    cc_settings = get_cc_settings()
    ctx['cc_settings'] = cc_settings
    is_adser = ctx.get('cc_role') == 'adser'
    ctx['is_adser'] = is_adser
    ctx['is_sale'] = ctx.get('cc_role') == 'sale'
    if is_adser:
        work_start = cc_settings.get('work_start', '08:00')
        work_end   = cc_settings.get('work_end',   '17:30')
        try:
            min_h = float(cc_settings.get('min_hours_cong', 9))
        except Exception:
            min_h = 9.0
        try:
            half_h = float(cc_settings.get('half_hours_cong', 0) or 0)
        except Exception:
            half_h = 0.0
        total_cong = sum(
            calc_cong_value(r['check_in'], r['check_out'], work_start, work_end, min_h, half_h)
            for r in records if r['check_in'] and r['check_out']
        )
        ctx['stat_total_cong'] = round(total_cong, 1)
    else:
        ctx['stat_total_cong'] = None

    # Calendar days
    import calendar
    cal = calendar.monthcalendar(year, month)
    ctx['calendar'] = cal

    # Tăng ca + Ngày lễ
    try:
        cal_data = get_month_calendar(view_uid, year, month)
        ctx['cal_days']   = cal_data['days']    # {YYYY-MM-DD: {reg_min, ot_min, multiplier, holiday_name, ...}}
        ctx['cal_totals'] = cal_data['totals']  # {reg_min, ot_min, pay_hours, total_hours}
        ctx['view_cc_role']   = cal_data.get('cc_role') or ''
        ctx['view_ot_eligible'] = bool(cal_data.get('ot_eligible'))
        ctx['stat_ot_hours']  = round(cal_data['totals']['ot_min']  / 60.0, 1)
        ctx['stat_reg_hours'] = round(cal_data['totals']['reg_min'] / 60.0, 1)
        ctx['stat_pay_hours'] = cal_data['totals']['pay_hours']
        # Tách riêng ngày thường vs ngày lễ — không cộng chung
        _hol_reg = sum(d['reg_min'] for d in cal_data['days'].values() if d.get('holiday_name'))
        _hol_ot  = sum(d['ot_min']  for d in cal_data['days'].values() if d.get('holiday_name'))
        _wd_reg  = sum(d['reg_min'] for d in cal_data['days'].values() if not d.get('holiday_name'))
        _wd_ot   = sum(d['ot_min']  for d in cal_data['days'].values() if not d.get('holiday_name'))
        ctx['stat_weekday_reg_h'] = round(_wd_reg / 60.0, 1)
        ctx['stat_weekday_ot_h']  = round(_wd_ot  / 60.0, 1)
        ctx['stat_holiday_reg_h'] = round(_hol_reg / 60.0, 1)
        ctx['stat_holiday_ot_h']  = round(_hol_ot  / 60.0, 1)
        # Backward-compat: stat_holiday_hours = tổng giờ ngày lễ (reg+ot) cho summary cũ
        ctx['stat_holiday_hours'] = round((_hol_reg + _hol_ot) / 60.0, 1)
        # Đếm ngày đủ ca / chưa đủ ca theo TỔNG GIỜ thực tế trong ngày
        # Đủ ca    = >= 9h (mặc định, đọc từ cc_settings.min_hours_cong)
        # Chưa đủ ca = có làm (> 0) nhưng < 9h (tất cả ngày dưới ngưỡng đủ ca)
        try:
            _min_full_min = int(float((cc_settings or {}).get('min_hours_cong', 9)) * 60)
        except Exception:
            _min_full_min = 540
        _full_cnt = 0
        _short_cnt = 0
        for d in cal_data['days'].values():
            _t = (d.get('reg_min') or 0) + (d.get('ot_min') or 0)
            if _t <= 0:
                continue
            if _t >= _min_full_min:
                _full_cnt += 1
            else:
                _short_cnt += 1
        ctx['stat_full_day']  = _full_cnt   # override count cũ (chỉ check_in+check_out, không xét giờ)
        ctx['stat_short_day'] = _short_cnt
    except Exception as _e:
        ctx['cal_days']   = {}
        ctx['cal_totals'] = {'reg_min': 0, 'ot_min': 0, 'pay_hours': 0, 'total_hours': 0}
        ctx['view_cc_role']   = ''
        ctx['view_ot_eligible'] = False
        ctx['stat_ot_hours']  = 0
        ctx['stat_reg_hours'] = 0
        ctx['stat_pay_hours'] = 0
        ctx['stat_holiday_hours'] = 0
        ctx['stat_weekday_reg_h'] = 0
        ctx['stat_weekday_ot_h']  = 0
        ctx['stat_holiday_reg_h'] = 0
        ctx['stat_holiday_ot_h']  = 0
        ctx['stat_short_day']     = 0
    ctx['month_name'] = ['', 'Tháng 1','Tháng 2','Tháng 3','Tháng 4',
                         'Tháng 5','Tháng 6','Tháng 7','Tháng 8',
                         'Tháng 9','Tháng 10','Tháng 11','Tháng 12'][month]

    # Manager: chọn nhân viên (leader chỉ thấy team mình)
    if ctx['is_manager']:
        dept = ctx['leader_dept'] if not ctx['is_admin'] else None
        # Admin/IT/Kế toán: có thể lọc theo team qua ?team=<team_code>
        selected_team = (request.args.get('team') or '').strip()
        if ctx['is_admin'] and selected_team:
            dept = selected_team
        ctx['all_employees'] = get_all_employees(department=dept)
        # Dropdown danh sách team — chỉ admin/IT/kế toán mới được lọc
        if ctx['is_admin']:
            ctx['all_teams'] = get_all_teams_from_db()
            ctx['selected_team'] = selected_team
        else:
            ctx['all_teams'] = []
            ctx['selected_team'] = ''

    # Chấm công hôm nay (cả trang diem-danh cũng cần)
    att = get_attendance(uid, today.isoformat())
    ctx['att'] = att

    # Phiên cũ chưa checkout từ ngày trước (chặn check-in mới)
    any_open = get_any_open_session(uid)
    if any_open and str(any_open.get('date', '')) != today.isoformat():
        ctx['pending_old_session'] = any_open
    else:
        ctx['pending_old_session'] = None

    return render_template('cham_cong/attendance.html', **ctx)


@bp.route('/giam-sat')
def giam_sat():
    """Trang giám sát chấm công thời gian thực — chỉ admin và kế toán."""
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')
    emp = get_employee(user['id'])
    ctx = _ctx_base(user, emp)

    if not ctx['is_manager']:
        return redirect('/cham-cong/')

    today = ctx['today']
    today_str = today.isoformat()

    employees = get_all_employees_status_today(today_str, department=ctx['leader_dept'])

    cnt_working  = sum(1 for e in employees if e['check_in'] and not e['check_out'])
    cnt_done     = sum(1 for e in employees if e['check_in'] and e['check_out'])
    cnt_absent   = sum(1 for e in employees if not e['check_in'])
    cnt_total    = len(employees)

    ctx.update({
        'employees': employees,
        'cnt_working': cnt_working,
        'cnt_done': cnt_done,
        'cnt_absent': cnt_absent,
        'cnt_total': cnt_total,
        'today_str': today_str,
    })
    return render_template('cham_cong/giam_sat.html', **ctx)


@bp.route('/api/giam-sat')
def api_giam_sat():
    """API trả JSON trạng thái chấm công hôm nay (cho auto-refresh)."""
    if not session.get('user_id') and not session.get('username'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 401
    emp = get_employee(user['id'])
    ctx = _ctx_base(user, emp)
    if not ctx['is_manager']:
        return jsonify({'ok': False}), 403

    today_str = today_hcm().isoformat()
    employees = get_all_employees_status_today(today_str, department=ctx['leader_dept'])

    def fmt(dt):
        if dt is None:
            return None
        converted = to_hcm(dt)
        return converted.strftime('%H:%M') if converted else None

    result = []
    for e in employees:
        ci = fmt(e['check_in'])
        co = fmt(e['check_out'])
        if ci and co:
            status = 'done'
        elif ci:
            status = 'working'
        else:
            status = 'absent'
        result.append({
            'user_id':   e['user_id'],
            'full_name': e['full_name'] or e['user_id'],
            'cc_role':   e['cc_role'],
            'check_in':  ci,
            'check_out': co,
            'status':    status,
            'check_in_lat':      e.get('check_in_lat'),
            'check_in_lng':      e.get('check_in_lng'),
            'check_in_distance_m': e.get('check_in_distance_m'),
        })

    cnt_working = sum(1 for r in result if r['status'] == 'working')
    cnt_done    = sum(1 for r in result if r['status'] == 'done')
    cnt_absent  = sum(1 for r in result if r['status'] == 'absent')
    return jsonify({
        'ok': True,
        'employees': result,
        'cnt_working': cnt_working,
        'cnt_done':    cnt_done,
        'cnt_absent':  cnt_absent,
        'cnt_total':   len(result),
    })


@bp.route('/cong-viec')
def tasks():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')

    uid = str(user['id'])
    emp = get_employee(uid)
    ctx = _ctx_base(user, emp)

    filter_status = request.args.get('status', '')
    filter_role = request.args.get('role', '')

    if ctx['is_manager']:
        dept = ctx['leader_dept']
        team_emps = get_all_employees(department=dept)
        ctx['all_employees'] = team_emps
        team_uids = {e['user_id'] for e in team_emps}
        all_tasks = get_all_tasks(limit=300)
        # Leader chỉ thấy task giao cho NV trong team của họ
        if dept:
            all_tasks = [t for t in all_tasks if not t.get('assigned_to') or t.get('assigned_to') in team_uids]
        if filter_status:
            all_tasks = [t for t in all_tasks if t['status'] == filter_status]
        if filter_role:
            all_tasks = [t for t in all_tasks if t.get('role_type') == filter_role]
        ctx['tasks'] = all_tasks
    else:
        my_tasks = get_tasks(user_id=uid)
        if filter_status:
            my_tasks = [t for t in my_tasks if t['status'] == filter_status]
        ctx['tasks'] = my_tasks

    ctx['filter_status'] = filter_status
    ctx['filter_role'] = filter_role

    # Group tasks by status
    todo = [t for t in ctx['tasks'] if t['status'] == 'todo']
    in_progress = [t for t in ctx['tasks'] if t['status'] == 'in-progress']
    done = [t for t in ctx['tasks'] if t['status'] == 'done']
    ctx['tasks_todo'] = sorted(todo, key=lambda t: _PRIORITY_ORDER.get(t['priority'], 2))
    ctx['tasks_in_progress'] = sorted(in_progress, key=lambda t: _PRIORITY_ORDER.get(t['priority'], 2))
    ctx['tasks_done'] = done[:20]  # giới hạn done

    # Đếm số progress mỗi task để hiện badge "Cập nhật (N)"
    try:
        task_ids = [t['id'] for t in ctx['tasks']]
        ctx['progress_counts'] = count_task_progress(task_ids) if task_ids else {}
    except Exception:
        ctx['progress_counts'] = {}

    return render_template('cham_cong/tasks.html', **ctx)


@bp.route('/kpi')
def kpi():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')

    uid = str(user['id'])
    emp = get_employee(uid)
    ctx = _ctx_base(user, emp)

    today = ctx['today']
    try:
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
    except ValueError:
        year, month = today.year, today.month

    view_uid = request.args.get('uid', uid)
    if view_uid != uid and not ctx['is_manager']:
        view_uid = uid
    ctx['view_uid'] = view_uid
    ctx['year'] = year
    ctx['month'] = month

    # KPI hôm nay
    today_kpi = get_kpi(view_uid, today.isoformat())
    ctx['today_kpi'] = today_kpi

    # Lịch sử tháng
    history = get_kpi_history(view_uid, year, month)
    ctx['kpi_history'] = history

    if ctx['is_manager']:
        ctx['all_employees'] = get_all_employees(department=ctx['leader_dept'])

    return render_template('cham_cong/kpi.html', **ctx)


@bp.route('/admin')
def admin():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')
    if user.get('role') not in ('admin', 'superadmin', 'manager', 'accountant', 'leader', 'sale_leader', 'it'):
        return redirect(url_for('cham_cong.dashboard'))

    emp = get_employee(str(user['id']))
    ctx = _ctx_base(user, emp)

    today = ctx['today']
    try:
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
    except ValueError:
        year, month = today.year, today.month
    ctx['year'] = year
    ctx['month'] = month

    # Load users từ DB
    try:
        import sys; sys.path.insert(0, _BASE)
        from user_helpers import load_all_users
        all_users = load_all_users()
    except Exception:
        all_users = []

    _dept_filter = ctx['leader_dept']   # None = xem tất cả (admin/kế toán)
    # Toggle "Hiện NV đã nghỉ" từ query string
    show_resigned = (request.args.get('show_resigned') or '').strip() in ('1', 'true', 'on', 'yes')
    ctx['show_resigned'] = show_resigned

    # Load toàn bộ cc_employees BAO GỒM cả NV đã nghỉ (để emp_map có đầy đủ
    # thông tin resignation cho template hiển thị badge "Đã nghỉ").
    all_emps = get_all_employees(department=None, include_resigned=True)
    # Normalize: emp_map key luôn là str (user_id từ DB là VARCHAR)
    emp_map = {str(e['user_id']): e for e in all_emps}

    # Merge users + employees
    # Rule nghỉ việc (yêu cầu 2026-05-30):
    # - NV nghỉ ngày X → VẪN HIỆN đến X + 1 tháng (đủ thời gian kế toán tính công +
    #   chốt sổ). Vd nghỉ 13/5 → còn hiện đến 12/6, từ 13/6 mới ẩn.
    # - Tài khoản users.status='inactive' nhưng KHÔNG có resigned_at → ẩn luôn.
    # - Toggle show_resigned (admin): bật → hiện hết kể cả nghỉ đã lâu.
    from datetime import date as _date_today
    from calendar import monthrange as _mr
    _today = _date_today.today()
    def _add_one_month(d):
        """d + 1 tháng (giữ nguyên ngày, nếu ngày không tồn tại → ngày cuối tháng kế)."""
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        last = _mr(y, m)[1]
        return d.replace(year=y, month=m, day=min(d.day, last))
    pos_ids = set()
    merged = []
    for u in all_users:
        uid_str = str(u['id'])
        e = emp_map.get(uid_str, {})
        resigned_at = e.get('resigned_at') if e else None
        if resigned_at:
            # Đã nghỉ → chỉ ẩn nếu hôm nay đã qua mốc (resigned_at + 1 tháng).
            if _today >= _add_one_month(resigned_at) and not show_resigned:
                continue
        elif u.get('status') == 'inactive':
            # Không phải nghỉ — tài khoản bị disable → ẩn luôn.
            continue
        user_team = str(u.get('team_id', '') or '').strip()
        emp_dept = (e.get('department') or '').strip()
        # Leader: chỉ giữ user thuộc team của leader (qua users.team_id hoặc cc_employees.department)
        if _dept_filter and _dept_filter != '__self__':
            if user_team != _dept_filter and emp_dept != _dept_filter:
                continue
        elif _dept_filter == '__self__':
            if uid_str != str((user or {}).get('id', '')):
                continue
        pos_ids.add(uid_str)
        # Ưu tiên department trong cc_employees; fallback team_id từ users
        dept = emp_dept or user_team
        merged.append({
            'user_id': uid_str,
            'username': u.get('username', ''),
            'pos_role': u.get('role', 'staff'),
            'full_name': e.get('full_name') or u.get('username', ''),
            'cc_role': e.get('cc_role', ''),
            'department': dept,
            'phone': e.get('phone', ''),
            'position': e.get('position', ''),
            'office_id': e.get('office_id'),
            'resigned_at': resigned_at,
            'resigned_note': e.get('resigned_note'),
            'resigned_team': e.get('resigned_team'),
            'resigned_leader': e.get('resigned_leader'),
        })
    # Thêm nhân viên standalone (emp-*) không có trong users.json
    for e in all_emps:
        if str(e['user_id']) not in pos_ids:
            resigned_at = e.get('resigned_at')
            # Cùng rule: ẩn nếu hôm nay >= resigned_at + 1 tháng.
            if resigned_at and _today >= _add_one_month(resigned_at) and not show_resigned:
                continue
            emp_dept = (e.get('department') or '').strip()
            # Leader chỉ thấy standalone thuộc team mình; staff không thấy
            if _dept_filter and _dept_filter != '__self__' and emp_dept != _dept_filter:
                continue
            if _dept_filter == '__self__':
                continue
            merged.append({
                'user_id': str(e['user_id']),
                'username': e.get('full_name') or str(e['user_id']),
                'pos_role': '',
                'full_name': e.get('full_name', ''),
                'cc_role': e.get('cc_role', ''),
                'department': e.get('department', ''),
                'phone': e.get('phone', ''),
                'position': e.get('position', ''),
                'office_id': e.get('office_id'),
                'resigned_at': resigned_at,
                'resigned_note': e.get('resigned_note'),
                'resigned_team': e.get('resigned_team'),
                'resigned_leader': e.get('resigned_leader'),
            })
    # Sắp xếp: theo team (department) rồi theo tên để template có thể group
    def _sort_key(u):
        dept = (u.get('department') or '').strip()
        # Người không có team đẩy xuống cuối
        dept_key = (1, '') if not dept else (0, dept.lower())
        return (dept_key, (u.get('full_name') or u.get('username') or '').lower())
    merged.sort(key=_sort_key)
    ctx['merged_users'] = merged

    # Danh sách tất cả user active trong POS (để modal "Chọn từ phần mềm")
    ctx['all_pos_users'] = [
        {
            'id':       str(u['id']),
            'username': u.get('username', ''),
            'role':     u.get('role', 'staff'),
            'cc_role':  emp_map.get(str(u['id']), {}).get('cc_role', ''),
        }
        for u in all_users
        if u.get('status') != 'inactive'
    ]

    # Cài đặt văn phòng
    cc_settings = get_cc_settings()
    ctx['cc_settings'] = cc_settings
    # Leader chỉ xem/sửa office gán cho team của họ; admin xem tất cả
    if _dept_filter and user.get('role') in ('leader', 'sale_leader'):
        ctx['cc_offices'] = get_offices_for_team(_dept_filter)
    else:
        ctx['cc_offices'] = get_all_offices()
    # Gắn danh sách team đã gán cho từng văn phòng (để hiển thị)
    _teams_by_office = office_teams_map()
    for _o in ctx['cc_offices']:
        _o['teams'] = _teams_by_office.get(int(_o['id']), [])
    # Lấy team từ bảng teams (web app settings)
    ctx['all_teams'] = get_all_teams_from_db()

    # Báo cáo chấm công tháng (có số công cho adser)
    report = get_monthly_report(year, month, settings=cc_settings,
                                department=_dept_filter if _dept_filter != '__self__' else None)
    ctx['monthly_report'] = report
    # Stats chi tiết tháng: giờ làm, ca, OT ngày thường/lễ
    ctx['emp_month_stats'] = get_admin_monthly_stats(
        year, month,
        department=_dept_filter if _dept_filter != '__self__' else None
    )
    ctx['month_name'] = ['', 'Tháng 1','Tháng 2','Tháng 3','Tháng 4',
                         'Tháng 5','Tháng 6','Tháng 7','Tháng 8',
                         'Tháng 9','Tháng 10','Tháng 11','Tháng 12'][month]

    # Thông báo
    ctx['announcements'] = get_announcements(limit=20)

    # Báo cáo công việc
    ctx['task_report'] = get_task_report(department=_dept_filter)

    return render_template('cham_cong/admin.html', **ctx)


# ─── API Endpoints ──────────────────────────────────────────

@bp.route('/api/checkin', methods=['POST'])
def api_checkin():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'msg': 'Chưa đăng nhập'}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy user'}), 400

    uid = str(user['id'])
    emp = get_employee(uid)
    cc_role = (emp or {}).get('cc_role', 'sale')
    now = now_hcm()
    today_str = now.date().isoformat()

    # Chặn check-in mới khi còn phiên cũ chưa check-out — KỂ CẢ phiên từ ngày trước
    open_sess = get_any_open_session(uid)
    if open_sess:
        sess_date = open_sess.get('date')
        sess_ci = open_sess.get('check_in')
        try:
            ci_str = to_hcm(sess_ci).strftime('%H:%M %d/%m') if sess_ci else ''
        except Exception:
            ci_str = ''
        if sess_date and str(sess_date) != today_str:
            msg = f'Bạn còn phiên chưa check-out từ {ci_str or sess_date}. Hãy check-out phiên cũ trước!'
        else:
            msg = 'Bạn đang trong ca làm việc, hãy check-out trước!'
        return jsonify({'ok': False, 'msg': msg, 'open_session_date': str(sess_date) if sess_date else None})

    lat = lng = distance_m = None

    # Sale: GPS tùy chọn — ghi nhận nếu có, không block nếu không có (giống leader)
    if cc_role == 'sale':
        data = request.get_json(silent=True) or {}
        try:
            _lat = float(data.get('lat', 0) or 0)
            _lng = float(data.get('lng', 0) or 0)
        except (TypeError, ValueError):
            _lat = _lng = 0.0
        if _lat and _lng:
            lat, lng = _lat, _lng

    # Marketing (adser) bắt buộc check-in bằng GPS và phải trong bán kính văn phòng
    elif cc_role == 'adser':
        data = request.get_json(silent=True) or {}
        try:
            lat = float(data.get('lat', 0) or 0)
            lng = float(data.get('lng', 0) or 0)
        except (TypeError, ValueError):
            lat = lng = 0.0

        if not lat or not lng:
            return jsonify({'ok': False, 'msg': 'Marketing phải check-in bằng GPS. Vui lòng bật định vị!'})

        # Ưu tiên: office được gán cho nhân viên → fallback global settings
        off_lat = off_lng = radius = 0
        office_name = 'văn phòng'
        emp_office_id = (emp or {}).get('office_id')
        if emp_office_id:
            office = get_office(int(emp_office_id))
            if office:
                off_lat = float(office['lat'])
                off_lng = float(office['lng'])
                radius  = float(office['radius_m'])
                office_name = office['name']
        if not off_lat or not off_lng:
            settings = get_cc_settings()
            try:
                off_lat = float(settings.get('office_lat', 0))
                off_lng = float(settings.get('office_lng', 0))
                radius  = float(settings.get('office_radius_m', 300))
            except Exception:
                off_lat = off_lng = radius = 0

        if off_lat and off_lng:
            distance_m = int(haversine_m(lat, lng, off_lat, off_lng))
            if distance_m > radius:
                return jsonify({
                    'ok': False,
                    'msg': f'Bạn cách {office_name} {distance_m}m (giới hạn {int(radius)}m). Check-in thất bại!'
                })

    create_checkin_session(uid, today_str, now, lat=lat, lng=lng, distance_m=distance_m)
    sessions_today = get_today_sessions(uid, today_str)
    session_num = len(sessions_today)
    extra = f' (cách VP {distance_m}m)' if distance_m is not None else ''
    label = f'Ca {session_num}' if session_num > 1 else ''
    msg = f'Check-in {label} thành công!{extra}'.strip()
    return jsonify({'ok': True, 'time': now.strftime('%H:%M'),
                    'msg': msg, 'session_num': session_num})


@bp.route('/api/checkout', methods=['POST'])
def api_checkout():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'msg': 'Chưa đăng nhập'}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy user'}), 400

    uid = str(user['id'])
    now = now_hcm()
    today_str = now.date().isoformat()

    closed = close_open_session(uid, today_str, now)
    if not closed:
        return jsonify({'ok': False, 'msg': 'Không có ca làm việc nào đang mở. Hãy check-in trước!'})

    dur = _fmt_duration(closed['check_in'], now)
    total_h = get_session_total_hours(uid, today_str)
    total_str = f'{int(total_h)}g{int((total_h % 1)*60):02d}p'
    sessions_today = get_today_sessions(uid, today_str)
    return jsonify({
        'ok': True,
        'time': now.strftime('%H:%M'),
        'duration': dur,
        'total_hours': total_str,
        'session_count': len(sessions_today),
        'msg': f'Check-out thành công! Ca này: {dur} — Tổng hôm nay: {total_str}'
    })


@bp.route('/api/backfill-checkout', methods=['POST'])
def api_backfill_checkout():
    """Đặt lại giờ check-out cho một ca đã check-in nhưng "Chưa ra".

    Body JSON: {"date": "YYYY-MM-DD", "time": "HH:MM", "user_id"?: "..."}
    - Nhân viên: chỉ được set cho chính mình.
    - Manager/leader: được set cho NV trong team (leader: đúng dept; admin: tất cả).
    """
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'msg': 'Chưa đăng nhập'}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy user'}), 400

    data = request.get_json(silent=True) or request.form
    date_str = (data.get('date') or '').strip()
    time_str = (data.get('time') or '').strip()
    target_uid = str(data.get('user_id') or user['id']).strip()
    if not date_str or not time_str:
        return jsonify({'ok': False, 'msg': 'Thiếu ngày hoặc giờ.'})

    # Validate HH:MM (accept HH:MM hoặc HH:MM:SS)
    try:
        from datetime import datetime as _dt
        if len(time_str) == 5:
            parsed_t = _dt.strptime(time_str, '%H:%M')
        else:
            parsed_t = _dt.strptime(time_str, '%H:%M:%S')
        parsed_d = _dt.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'ok': False, 'msg': 'Định dạng ngày/giờ không hợp lệ.'})

    # Build HCM-aware timestamp
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    _VN = ZoneInfo('Asia/Ho_Chi_Minh')
    ts = _dt(
        parsed_d.year, parsed_d.month, parsed_d.day,
        parsed_t.hour, parsed_t.minute, 0, tzinfo=_VN,
    )

    # Permission: self OR manager (admin any, leader only own dept)
    uid = str(user['id'])
    if target_uid != uid:
        emp = get_employee(uid)
        ctx = _ctx_base(user, emp)
        if not ctx.get('is_manager'):
            return jsonify({'ok': False, 'msg': 'Không có quyền.'}), 403
        if not ctx.get('is_admin'):
            dept = ctx.get('leader_dept')
            if dept:
                team_uids = {str(e['user_id']) for e in get_all_employees(department=dept)}
                if target_uid not in team_uids:
                    return jsonify({'ok': False, 'msg': 'Không phải NV trong team.'}), 403

    # Không cho set giờ ở tương lai
    now = now_hcm()
    if ts > now:
        return jsonify({'ok': False, 'msg': 'Không thể đặt giờ ra ở tương lai.'})

    result = db_backfill_checkout(target_uid, date_str, ts)
    if not result.get('ok'):
        return jsonify(result)
    return jsonify({
        'ok': True,
        'msg': result.get('msg') or 'Đã đóng ca.',
        'check_out': ts.strftime('%H:%M %d/%m/%Y'),
    })


# ─── Bổ sung công ───────────────────────────────────────────

@bp.route('/bo-sung-cong')
def bo_sung_cong():
    redir = _require_login()
    if redir:
        return redir
    user = _current_user()
    if not user:
        return redirect('/login')

    uid = str(user['id'])
    emp = get_employee(uid)
    ctx = _ctx_base(user, emp)
    ctx['active_page'] = 'bo_sung_cong'

    # Đề nghị của chính mình (mọi trạng thái)
    ctx['my_requests'] = list_attendance_requests(user_id=uid, limit=100)

    # Quản lý: danh sách cần duyệt (theo phạm vi quyền)
    if ctx['is_manager']:
        dept = ctx['leader_dept'] if not ctx['is_admin'] else None
        ctx['pending_list'] = list_attendance_requests(status='pending', department=dept, limit=200)
        ctx['reviewed_list'] = list_attendance_requests(department=dept, limit=80)
        # bỏ pending khỏi reviewed_list để khỏi trùng
        ctx['reviewed_list'] = [r for r in ctx['reviewed_list'] if r['status'] != 'pending']

    return render_template('cham_cong/bo_sung_cong.html', **ctx)


def _can_review_request(ctx, req) -> bool:
    """Quản lý có quyền duyệt đề nghị này không (admin: tất cả; leader: team mình)."""
    if not ctx.get('is_manager'):
        return False
    if ctx.get('is_admin'):
        return True
    dept = ctx.get('leader_dept')
    if not dept or dept == '__self__':
        return False
    team_uids = {str(e['user_id']) for e in get_all_employees(department=dept)}
    return str(req['user_id']) in team_uids


@bp.route('/api/bo-sung-cong', methods=['POST'])
def api_create_attendance_request():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'msg': 'Chưa đăng nhập.'}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False, 'msg': 'Lỗi tài khoản.'}), 400

    data = request.get_json(silent=True) or request.form
    work_date = (data.get('work_date') or '').strip()
    reason = (data.get('reason') or '').strip()
    sessions = data.get('sessions')
    if isinstance(sessions, str):
        try:
            sessions = json.loads(sessions)
        except Exception:
            sessions = []

    if not work_date:
        return jsonify({'ok': False, 'msg': 'Vui lòng chọn ngày.'})
    try:
        datetime.strptime(work_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'ok': False, 'msg': 'Ngày không hợp lệ.'})

    # Validate từng ca: bắt buộc có giờ vào; giờ định dạng HH:MM
    clean = []
    for ca in (sessions or []):
        ci = (ca.get('check_in') or '').strip()
        co = (ca.get('check_out') or '').strip()
        if not ci:
            continue
        try:
            datetime.strptime(ci, '%H:%M')
            if co:
                datetime.strptime(co, '%H:%M')
        except ValueError:
            return jsonify({'ok': False, 'msg': 'Giờ không hợp lệ (định dạng HH:MM).'})
        clean.append({'check_in': ci, 'check_out': co})
    if not clean:
        return jsonify({'ok': False, 'msg': 'Cần nhập ít nhất 1 ca có giờ vào.'})

    uid = str(user['id'])
    req_id = create_attendance_request(uid, work_date, clean, reason)
    return jsonify({'ok': True, 'msg': 'Đã gửi đề nghị bổ sung công.', 'id': req_id})


@bp.route('/api/bo-sung-cong/<int:req_id>/cancel', methods=['POST'])
def api_cancel_attendance_request(req_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400
    result = cancel_attendance_request(req_id, str(user['id']))
    return jsonify(result)


@bp.route('/api/bo-sung-cong/<int:req_id>/approve', methods=['POST'])
def api_approve_attendance_request(req_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400
    emp = get_employee(str(user['id']))
    ctx = _ctx_base(user, emp)
    req = get_attendance_request(req_id)
    if not req:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy đề nghị.'})
    if not _can_review_request(ctx, req):
        return jsonify({'ok': False, 'msg': 'Không có quyền duyệt đề nghị này.'}), 403
    data = request.get_json(silent=True) or request.form
    note = (data.get('note') or '').strip()
    result = approve_attendance_request(req_id, str(user['id']), note)
    return jsonify(result)


@bp.route('/api/bo-sung-cong/<int:req_id>/reject', methods=['POST'])
def api_reject_attendance_request(req_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400
    emp = get_employee(str(user['id']))
    ctx = _ctx_base(user, emp)
    req = get_attendance_request(req_id)
    if not req:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy đề nghị.'})
    if not _can_review_request(ctx, req):
        return jsonify({'ok': False, 'msg': 'Không có quyền xử lý đề nghị này.'}), 403
    data = request.get_json(silent=True) or request.form
    note = (data.get('note') or '').strip()
    result = reject_attendance_request(req_id, str(user['id']), note)
    return jsonify(result)


# ─── Nghỉ việc / Mở lại ─────────────────────────────────────

def _require_resign_permission(user, emp):
    """Chỉ leader/admin/manager/kế toán/IT được đánh dấu nghỉ việc."""
    ctx = _ctx_base(user, emp)
    if not ctx.get('is_manager'):
        return None
    return ctx


@bp.route('/api/employee/<uid>/resign', methods=['POST'])
def api_resign_employee(uid: str):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400
    emp = get_employee(str(user['id']))
    ctx = _require_resign_permission(user, emp)
    if not ctx:
        return jsonify({'ok': False, 'msg': 'Không có quyền.'}), 403
    # leader chỉ được nghỉ NV trong team mình; admin/IT/kế toán/manager mọi NV
    target_emp = get_employee(uid)
    if not target_emp:
        return jsonify({'ok': False, 'msg': 'Không tìm thấy NV.'})
    if not ctx.get('is_admin'):
        dept = ctx.get('leader_dept')
        team_uids = {str(e['user_id']) for e in get_all_employees(department=dept, include_resigned=True)}
        if str(uid) not in team_uids:
            return jsonify({'ok': False, 'msg': 'Không phải NV trong team.'}), 403
    data = request.get_json(silent=True) or request.form
    note = (data.get('note') or '').strip()
    # Ngày nghỉ tùy chọn (leader tích muộn vẫn đúng ngày) — không cho ngày tương lai
    resigned_at = None
    raw_date = (data.get('date') or '').strip()
    if raw_date:
        try:
            from datetime import date as _d
            parsed = _d.fromisoformat(raw_date)
            if parsed <= _d.today():
                resigned_at = parsed
        except ValueError:
            pass
    result = resign_employee(str(uid), note, str(user['id']), resigned_at=resigned_at)
    return jsonify(result)


@bp.route('/api/employee/<uid>/restore', methods=['POST'])
def api_restore_employee(uid: str):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400
    emp = get_employee(str(user['id']))
    ctx = _require_resign_permission(user, emp)
    if not ctx:
        return jsonify({'ok': False, 'msg': 'Không có quyền.'}), 403
    if not ctx.get('is_admin'):
        # leader chỉ mở lại NV trong team mình
        dept = ctx.get('leader_dept')
        team_uids = {str(e['user_id']) for e in get_all_employees(department=dept, include_resigned=True)}
        if str(uid) not in team_uids:
            return jsonify({'ok': False, 'msg': 'Không phải NV trong team.'}), 403
    result = restore_employee(str(uid))
    return jsonify(result)


@bp.route('/api/tasks', methods=['POST'])
def api_create_task():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400

    data = request.get_json() or request.form
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'ok': False, 'msg': 'Thiếu tiêu đề'})

    assigned_to = data.get('assigned_to', '')
    task_id = create_task(
        title=title,
        description=data.get('description', ''),
        task_type=data.get('task_type', 'one-time'),
        role_type=data.get('role_type', ''),
        assigned_to=assigned_to,
        assigned_by=user['id'],
        priority=data.get('priority', 'normal'),
        due_date=data.get('due_date') or None,
    )

    # Gửi push notification cho người được giao việc (best-effort, không chặn response)
    if assigned_to:
        try:
            from push_notifications import send_push_to_user
            priority = data.get('priority', 'normal')
            prio_icon = {'urgent': '🔴', 'high': '🟠', 'normal': '📌', 'low': '🔵'}.get(priority, '📌')
            assigner_name = user.get("full_name") or user.get("username") or "Admin"
            desc = (data.get('description') or '').strip()
            # Body: "👤 <tên người giao>\n<nội dung>" — luôn hiện tên người giao ở dòng đầu
            if desc:
                if len(desc) > 120:
                    desc = desc[:117] + '...'
                body = f'👤 {assigner_name} giao\n{desc}'
            else:
                body = f'👤 {assigner_name} vừa giao bạn 1 công việc'
            send_push_to_user(
                user_id=str(assigned_to),
                title=f'{prio_icon} Công việc mới: {title}',
                body=body,
                url='/cham-cong/cong-viec',
                tag=f'task-{task_id}',
                extra={'task_id': task_id, 'assigner': assigner_name},
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('send push for task %s failed', task_id)

    return jsonify({'ok': True, 'task_id': task_id, 'msg': 'Đã tạo công việc!'})


@bp.route('/api/tasks/<int:task_id>/status', methods=['POST'])
def api_task_status(task_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    data = request.get_json() or request.form
    status = data.get('status', '')
    if status not in ('todo', 'in-progress', 'done', 'cancelled'):
        return jsonify({'ok': False, 'msg': 'Trạng thái không hợp lệ'})
    # Lấy task trước khi update để biết assigned_by + status cũ
    task = None
    try:
        task = get_task(task_id)
    except Exception:
        pass
    old_status = (task or {}).get('status')
    update_task_status(task_id, status)

    # Push cho người giao việc khi status đổi sang in-progress / done
    if task and old_status != status and status in ('in-progress', 'done'):
        assigner = task.get('assigned_by')
        user = _current_user() or {}
        # Đừng gửi push cho chính người giao nếu họ tự đổi
        if assigner and str(assigner) != str(user.get('id') or ''):
            try:
                from push_notifications import send_push_to_user
                who = user.get('full_name') or user.get('username') or 'Nhân viên'
                if status == 'in-progress':
                    title = f'▶️ Bắt đầu: {task.get("title") or ""}'
                    body = f'{who} đã bắt đầu xử lý công việc bạn giao'
                else:
                    title = f'✅ Đã xong: {task.get("title") or ""}'
                    body = f'{who} đã hoàn thành công việc bạn giao'
                send_push_to_user(
                    user_id=str(assigner),
                    title=title,
                    body=body,
                    url='/cham-cong/cong-viec',
                    tag=f'task-status-{task_id}',
                    extra={'task_id': task_id, 'status': status},
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception('push task status %s failed', task_id)
    return jsonify({'ok': True, 'msg': 'Đã cập nhật!'})


@bp.route('/api/tasks/<int:task_id>/progress', methods=['GET'])
def api_task_progress_list(task_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    try:
        items = get_task_progress(task_id)
        # DB lưu TIMESTAMP naive ở UTC (PG timezone=UTC). Convert sang giờ VN để hiển thị.
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo
        _VN = ZoneInfo('Asia/Ho_Chi_Minh')
        for it in items:
            ca = it.get('created_at')
            if ca and hasattr(ca, 'isoformat'):
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=_tz.utc)
                ca_vn = ca.astimezone(_VN)
                # ISO cho JS tính "x phút trước" (đã có TZ +07:00)
                it['created_at'] = ca_vn.isoformat()
                # Chuỗi đã format sẵn theo giờ VN — client hiển thị trực tiếp
                it['created_at_str'] = ca_vn.strftime('%H:%M %d/%m/%Y')
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception('get task progress %s failed', task_id)
        return jsonify({'ok': False, 'msg': str(e)}), 500


@bp.route('/api/tasks/<int:task_id>/progress', methods=['POST'])
def api_task_progress_add(task_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False, 'msg': 'Chưa đăng nhập'}), 401
    data = request.get_json() or request.form
    content = (data.get('content') or '').strip()
    progress_type = data.get('progress_type') or 'note'
    if progress_type not in ('done', 'pending', 'note'):
        progress_type = 'note'
    if not content:
        return jsonify({'ok': False, 'msg': 'Nội dung không được để trống'}), 400
    try:
        new_id = add_task_progress(task_id, str(user['id']), content, progress_type)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception('add task progress %s failed', task_id)
        return jsonify({'ok': False, 'msg': str(e)}), 500

    # Push cho người giao việc
    try:
        task = get_task(task_id)
        assigner = (task or {}).get('assigned_by')
        if assigner and str(assigner) != str(user.get('id') or ''):
            from push_notifications import send_push_to_user
            who = user.get('full_name') or user.get('username') or 'Nhân viên'
            prefix = {'done': '✅', 'pending': '⚠️', 'note': '📝'}.get(progress_type, '📝')
            body_txt = content if len(content) <= 140 else content[:137] + '...'
            send_push_to_user(
                user_id=str(assigner),
                title=f'{prefix} {who} cập nhật: {task.get("title") or ""}',
                body=body_txt,
                url='/cham-cong/cong-viec',
                tag=f'task-progress-{task_id}',
                extra={'task_id': task_id, 'progress_id': new_id},
            )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('push task progress %s failed', task_id)

    return jsonify({'ok': True, 'id': new_id, 'msg': 'Đã lưu cập nhật!'})


@bp.route('/api/tasks/progress/<int:progress_id>', methods=['DELETE'])
def api_task_progress_delete(progress_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 401
    is_manager = user.get('role') in ('admin', 'leader')
    try:
        ok = delete_task_progress(progress_id, str(user['id']), is_manager)
        if not ok:
            return jsonify({'ok': False, 'msg': 'Không tìm thấy hoặc không có quyền'}), 403
        return jsonify({'ok': True})
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception('delete task progress %s failed', progress_id)
        return jsonify({'ok': False, 'msg': str(e)}), 500


@bp.route('/api/tasks/<int:task_id>', methods=['DELETE'])
def api_delete_task(task_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'leader', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    delete_task(task_id)
    return jsonify({'ok': True})


@bp.route('/api/push/send-test-to-user', methods=['POST'])
def api_push_send_test_to_user():
    """Admin/leader gửi push test đến user bất kỳ — để kiểm tra user đã bật thông báo chưa."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'leader', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or request.form
    target_user_id = (data.get('user_id') or '').strip()
    if not target_user_id:
        return jsonify({'ok': False, 'msg': 'Thiếu user_id'}), 400
    try:
        from push_notifications import send_push_to_user
        r = send_push_to_user(
            user_id=target_user_id,
            title='🔔 Test từ Admin',
            body=f'{user.get("full_name") or user.get("username") or "Admin"} gửi thông báo test. Nếu bạn thấy dòng này là OK.',
            url='/cham-cong/',
            tag=f'admin-test-{target_user_id}',
        )
        return jsonify({'ok': True, **r})
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception('admin push test failed')
        return jsonify({'ok': False, 'msg': str(e)}), 500


@bp.route('/api/kpi', methods=['POST'])
def api_save_kpi():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 400

    data = request.get_json() or {}
    uid = data.get('user_id', user['id'])
    date_str = data.get('date', today_hcm().isoformat())
    emp = get_employee(uid)
    cc_role = (emp or {}).get('cc_role', 'sale')
    upsert_kpi(uid, date_str, cc_role, data)
    return jsonify({'ok': True, 'msg': 'Đã lưu KPI!'})


@bp.route('/api/employee/save', methods=['POST'])
def api_save_employee():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    role = (user or {}).get('role', '')
    admin_roles = {'admin', 'superadmin', 'manager', 'accountant', 'it'}
    is_admin_like = role in admin_roles
    is_leader = role in ('leader', 'sale_leader')
    if not user or (not is_admin_like and not is_leader):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403

    data = request.get_json() or request.form
    uid = data.get('user_id', '').strip()
    if not uid:
        return jsonify({'ok': False, 'msg': 'Thiếu user_id'})

    # Leader: chỉ được sửa NV trong team của mình
    if is_leader:
        leader_team = str(user.get('team_id') or '').strip()
        if not leader_team:
            return jsonify({'ok': False, 'msg': 'Leader chưa được gán team'}), 403
        try:
            from db import get_conn as _gc
            with _gc() as _c:
                with _c.cursor() as _cur:
                    _cur.execute("""
                        SELECT t.team_code FROM users u
                        LEFT JOIN teams t ON t.id = u.team_id
                        WHERE u.id::text = %s
                    """, (uid,))
                    row = _cur.fetchone()
            target_team = (row[0] if row else '') or ''
        except Exception:
            target_team = ''
        if target_team != leader_team:
            return jsonify({'ok': False, 'msg': 'Chỉ sửa được NV trong team của bạn'}), 403

    # Văn phòng chấm công: tick WFH → office "Làm việc tại nhà" (bán kính không giới hạn);
    # else lấy office_id được chọn. None → giữ nguyên office hiện tại (COALESCE trong upsert).
    office_id = None
    if str(data.get('wfh', '')).strip().lower() in ('1', 'true', 'on', 'yes'):
        try:
            from db import get_conn as _gc
            with _gc() as _c:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT id FROM cc_offices WHERE name ILIKE %s ORDER BY id LIMIT 1", ('%WFH%',))
                    _row = _cur.fetchone()
            office_id = int(_row[0]) if _row else None
        except Exception:
            office_id = None
    else:
        _oid = str(data.get('office_id') or '').strip()
        if _oid.isdigit():
            office_id = int(_oid)

    upsert_employee(
        user_id=uid,
        full_name=data.get('full_name', '').strip(),
        cc_role=data.get('cc_role', 'sale').strip(),
        department=data.get('department', '').strip(),
        phone=data.get('phone', '').strip(),
        position=data.get('position', '').strip(),
        office_id=office_id,
    )

    # Cập nhật web role nếu được gửi — leader chỉ set được staff/leader;
    # admin-like set được mọi role.
    new_web_role = (data.get('web_role') or '').strip().lower()
    if new_web_role:
        if is_leader:
            allowed = {'staff', 'leader'}
        else:
            allowed = {'admin', 'superadmin', 'manager', 'leader',
                       'staff', 'accountant', 'kho', 'it'}
        if new_web_role in allowed:
            try:
                from db import get_conn as _gc
                with _gc() as _c:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "UPDATE users SET role=%s::user_role WHERE id::text=%s",
                            (new_web_role, uid),
                        )
                    _c.commit()
                # Đồng bộ cc_role theo web role mới
                try:
                    from modules.cham_cong.cc_db import sync_cc_role_from_web_role
                    sync_cc_role_from_web_role(uid, new_web_role)
                except Exception:
                    pass
            except Exception as e:
                return jsonify({'ok': True, 'msg': f'Đã lưu NV (cảnh báo: không đổi được role: {e})'})
    return jsonify({'ok': True, 'msg': 'Đã lưu!'})


@bp.route('/api/employee/create', methods=['POST'])
def api_create_employee():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'accountant', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or request.form
    full_name = (data.get('full_name') or '').strip()
    if not full_name:
        return jsonify({'ok': False, 'msg': 'Thiếu tên nhân viên'})
    uid = create_standalone_employee(
        full_name=full_name,
        cc_role=data.get('cc_role', 'packing').strip(),
        department=data.get('department', '').strip(),
        phone=data.get('phone', '').strip(),
        position=data.get('position', '').strip(),
    )
    return jsonify({'ok': True, 'user_id': uid, 'msg': f'Đã thêm "{full_name}"!'})


@bp.route('/api/employee/delete/<uid>', methods=['DELETE'])
def api_delete_employee(uid: str):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') != 'admin':
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    if not uid.startswith('emp-'):
        return jsonify({'ok': False, 'msg': 'Chỉ xoá được nhân viên tạo thủ công'})
    delete_employee(uid)
    return jsonify({'ok': True, 'msg': 'Đã xoá!'})


def _admin_can_mark(user) -> bool:
    return bool(user) and user.get('role') in (
        'admin', 'superadmin', 'manager', 'accountant',
        'leader', 'sale_leader', 'it'
    )


@bp.route('/api/attendance/sessions')
def api_admin_get_sessions():
    """Trả về danh sách ca của (user_id, date) để form chấm công hộ auto-fill."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not _admin_can_mark(user):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    uid = (request.args.get('user_id') or '').strip()
    date_str = (request.args.get('date') or '').strip()
    if not uid or not date_str:
        return jsonify({'ok': False, 'msg': 'Thiếu user_id/date'}), 400
    sessions = get_today_sessions(uid, date_str)
    out = []
    for s in sessions:
        ci = to_hcm(s.get('check_in')) if s.get('check_in') else None
        co = to_hcm(s.get('check_out')) if s.get('check_out') else None
        out.append({
            'check_in':  ci.strftime('%H:%M') if ci else '',
            'check_out': co.strftime('%H:%M') if co else '',
        })
    return jsonify({'ok': True, 'sessions': out, 'count': len(out)})


@bp.route('/api/attendance/admin-mark', methods=['POST'])
def api_admin_mark_attendance():
    """Admin chấm công hộ — ghi ĐÈ toàn bộ ca của NV trong 1 ngày.
    Body: {user_id, date, sessions: [{check_in,check_out}, ...]}
    Tương thích cũ: nếu có check_in/check_out đơn → đóng gói thành 1 ca.
    """
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not _admin_can_mark(user):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or request.form
    uid = (data.get('user_id') or '').strip()
    date_str = (data.get('date') or today_hcm().isoformat()).strip()
    sessions = data.get('sessions')
    if isinstance(sessions, str):
        try:
            sessions = json.loads(sessions)
        except Exception:
            sessions = None
    # Back-compat: form cũ gửi check_in/check_out đơn
    if not sessions:
        ci = (data.get('check_in') or '').strip()
        co = (data.get('check_out') or '').strip()
        sessions = [{'check_in': ci, 'check_out': co}] if ci else []
    if not uid:
        return jsonify({'ok': False, 'msg': 'Thiếu nhân viên'})
    # Validate format HH:MM (chuẩn hoá)
    clean = []
    for ca in sessions:
        ci = (ca.get('check_in') or '').strip()
        co = (ca.get('check_out') or '').strip()
        if not ci:
            continue
        try:
            datetime.strptime(ci, '%H:%M')
            if co:
                datetime.strptime(co, '%H:%M')
        except ValueError:
            return jsonify({'ok': False, 'msg': f'Giờ không hợp lệ ({ci}/{co})'})
        clean.append({'check_in': ci, 'check_out': co})
    result = admin_set_attendance_sessions(uid, date_str, clean)
    return jsonify(result)


@bp.route('/api/announcements', methods=['POST'])
def api_create_announcement():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'accountant', 'leader', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403

    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'ok': False, 'msg': 'Thiếu tiêu đề'})

    target_role = data.get('target_role') or None
    content = data.get('content', '')
    create_announcement(
        title=title,
        content=content,
        author_id=user['id'],
        target_role=target_role,
        is_pinned=bool(data.get('is_pinned', False)),
    )

    # Push thông báo đến tất cả NV phù hợp (chạy background để không chặn response)
    try:
        import threading
        emps = get_all_employees()
        if target_role:
            emps = [e for e in emps if (e.get('cc_role') or '') == target_role]
        target_ids = [str(e['user_id']) for e in emps if e.get('user_id') and str(e['user_id']) != str(user['id'])]

        body_txt = (content or '').strip()
        if len(body_txt) > 140:
            body_txt = body_txt[:137] + '...'
        if not body_txt:
            body_txt = f'{user.get("full_name") or user.get("username") or "Admin"} vừa đăng thông báo mới'

        def _blast():
            from push_notifications import send_push_to_user
            for uid in target_ids:
                try:
                    send_push_to_user(
                        user_id=uid,
                        title=f'📢 {title}',
                        body=body_txt,
                        url='/cham-cong/',
                        tag=f'announce-{uid}',
                    )
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('push announce to %s failed', uid)

        threading.Thread(target=_blast, daemon=True).start()
    except Exception:
        import logging
        logging.getLogger(__name__).exception('announcement push dispatch failed')

    return jsonify({'ok': True, 'msg': 'Đã đăng thông báo!'})


@bp.route('/api/announcements/<int:ann_id>', methods=['DELETE'])
def api_delete_announcement(ann_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'accountant', 'leader', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    delete_announcement(ann_id)
    return jsonify({'ok': True})


@bp.route('/api/announcements/<int:ann_id>', methods=['PUT', 'PATCH'])
def api_update_announcement(ann_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'accountant', 'leader', 'it'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'ok': False, 'msg': 'Thiếu tiêu đề'})
    update_announcement(
        ann_id=ann_id,
        title=title,
        content=data.get('content', ''),
        target_role=data.get('target_role') or None,
        is_pinned=bool(data.get('is_pinned', False)),
    )
    return jsonify({'ok': True, 'msg': 'Đã cập nhật thông báo!'})


@bp.route('/api/offices', methods=['GET'])
def api_list_offices():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'offices': get_all_offices()})


def _leader_team(user) -> str | None:
    """Trả về team_code của leader; None nếu không phải leader hoặc không có team."""
    if not user or user.get('role') != 'leader':
        return None
    return str(user.get('team_id', '') or '').strip() or None


@bp.route('/api/offices', methods=['POST'])
def api_save_office():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    role = (user or {}).get('role')
    if not user or role not in ('admin', 'superadmin', 'manager', 'it', 'leader', 'sale_leader'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or {}
    try:
        lat = float(data['lat'])
        lng = float(data['lng'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'ok': False, 'msg': 'Thiếu toạ độ lat/lng'})
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'msg': 'Thiếu tên văn phòng'})
    radius_m = int(data.get('radius_m', 300))
    note = data.get('note', '')
    office_id = data.get('id') or None
    if office_id:
        office_id = int(office_id)
    # Leader chỉ được sửa office đã gán cho team của họ
    if role in ('leader', 'sale_leader') and office_id:
        tc = _leader_team(user)
        teams = office_teams(office_id)
        if not tc or (teams and tc not in teams):
            return jsonify({'ok': False, 'msg': 'Leader chỉ sửa được văn phòng của team mình'}), 403
    new_id = upsert_office(name, lat, lng, radius_m, note, office_id)
    # Nếu leader tạo mới: auto-gán cho team của họ
    if role in ('leader', 'sale_leader') and not office_id:
        tc = _leader_team(user)
        if tc:
            assign_office_to_team_code(new_id, tc)
    return jsonify({'ok': True, 'id': new_id, 'msg': f'Đã lưu văn phòng "{name}"!'})


@bp.route('/api/offices/<int:office_id>', methods=['DELETE'])
def api_delete_office(office_id: int):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    role = (user or {}).get('role')
    if not user or role not in ('admin', 'superadmin', 'manager', 'it', 'leader', 'sale_leader'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    if role in ('leader', 'sale_leader'):
        tc = _leader_team(user)
        teams = office_teams(office_id)
        # Cho phép xoá nếu office chỉ đang dùng bởi team của leader (hoặc chưa gán ai)
        if teams and (not tc or teams - {tc}):
            return jsonify({'ok': False, 'msg': 'Văn phòng này đang dùng bởi team khác'}), 403
    delete_office(office_id)
    return jsonify({'ok': True, 'msg': 'Đã xoá!'})


@bp.route('/api/offices/assign', methods=['POST'])
def api_assign_office():
    """Gán văn phòng cho nhân viên hoặc cả team."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    role = (user or {}).get('role')
    if not user or role not in ('admin', 'superadmin', 'manager', 'it', 'leader', 'sale_leader'):
        return jsonify({'ok': False, 'msg': 'Không có quyền'}), 403
    data = request.get_json() or {}
    office_id = data.get('office_id')  # None để bỏ gán
    if office_id is not None:
        office_id = int(office_id)
    # Gán theo team_code (từ bảng teams)
    team_code = (data.get('department') or data.get('team_code') or '').strip()
    # Leader: ép team_code = team của mình
    if role in ('leader', 'sale_leader'):
        my_tc = _leader_team(user)
        if not my_tc:
            return jsonify({'ok': False, 'msg': 'Leader chưa có team'}), 403
        if team_code and team_code != my_tc:
            return jsonify({'ok': False, 'msg': 'Leader chỉ gán được cho team của mình'}), 403
        team_code = my_tc
    if team_code:
        n = assign_office_to_team_code(office_id, team_code)
        return jsonify({'ok': True, 'msg': f'Đã gán {n} nhân viên có team "{team_code}"!'})
    # Gán theo danh sách user_id
    user_ids = data.get('user_ids') or []
    if not user_ids:
        return jsonify({'ok': False, 'msg': 'Thiếu danh sách nhân viên'})
    assign_office_to_employees(office_id, user_ids)
    return jsonify({'ok': True, 'msg': f'Đã gán {len(user_ids)} nhân viên!'})


@bp.route('/api/cai-dat/van-phong', methods=['GET'])
def api_get_settings():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'settings': get_cc_settings()})


@bp.route('/api/cai-dat/van-phong', methods=['POST'])
def api_save_settings():
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'it'):
        return jsonify({'ok': False, 'msg': 'Chỉ Admin mới được cấu hình'}), 403
    data = request.get_json() or {}
    allowed = ('office_lat', 'office_lng', 'office_radius_m',
                'work_start', 'work_end', 'min_hours_cong', 'half_hours_cong')
    for key in allowed:
        if key in data and data[key] is not None:
            upsert_cc_setting(key, str(data[key]))
    return jsonify({'ok': True, 'msg': 'Đã lưu cài đặt!'})


# ─── Holidays & Overtime (tăng ca) ──────────────────────────────

@bp.route('/api/holidays', methods=['GET'])
def api_list_holidays():
    """Danh sách ngày lễ. Query ?year=YYYY để lọc theo năm."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    y = request.args.get('year', type=int)
    return jsonify({'ok': True, 'items': list_holidays(y)})


@bp.route('/api/holidays', methods=['POST'])
def api_upsert_holiday():
    """Admin/Manager thêm/sửa 1 ngày lễ."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'it'):
        return jsonify({'ok': False, 'msg': 'Chỉ quản lý mới được sửa ngày lễ'}), 403
    data = request.get_json() or {}
    hd = (data.get('holiday_date') or '').strip()
    name = (data.get('name') or '').strip()
    if not hd or not name:
        return jsonify({'ok': False, 'msg': 'Thiếu ngày hoặc tên'})
    try:
        mult = float(data.get('multiplier', 2.0))
    except Exception:
        mult = 2.0
    is_lunar = bool(data.get('is_lunar', False))
    note = (data.get('note') or '').strip()
    from datetime import date as _date
    try:
        d = _date.fromisoformat(hd)
    except Exception:
        return jsonify({'ok': False, 'msg': 'Sai định dạng ngày (YYYY-MM-DD)'})
    upsert_holiday(d, name, mult, is_lunar, note)
    return jsonify({'ok': True, 'msg': 'Đã lưu ngày lễ!'})


@bp.route('/api/holidays/<hd>', methods=['DELETE'])
def api_delete_holiday(hd: str):
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'it'):
        return jsonify({'ok': False, 'msg': 'Chỉ quản lý mới được xoá'}), 403
    from datetime import date as _date
    try:
        d = _date.fromisoformat(hd)
    except Exception:
        return jsonify({'ok': False, 'msg': 'Sai định dạng ngày'})
    ok = delete_holiday(d)
    return jsonify({'ok': ok})


@bp.route('/api/holidays/seed', methods=['POST'])
def api_seed_holidays():
    """Seed ngày lễ VN cho 1 năm (POST {year:2026})."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user or user.get('role') not in ('admin', 'superadmin', 'manager', 'it'):
        return jsonify({'ok': False, 'msg': 'Chỉ quản lý'}), 403
    data = request.get_json() or {}
    try:
        y = int(data.get('year') or 0)
    except Exception:
        y = 0
    if not y:
        from datetime import datetime as _dt
        y = _dt.now().year
    n = seed_default_holidays_vn(y)
    return jsonify({'ok': True, 'inserted': n, 'year': y})


@bp.route('/api/team-ot', methods=['GET'])
def api_team_overtime():
    """Tổng hợp giờ tăng ca theo team.
    Query: ?team=sale|packing|<team_code>&year=YYYY&month=MM"""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    from datetime import datetime as _dt
    now = _dt.now()
    team = (request.args.get('team') or '').strip() or None
    try:
        y = int(request.args.get('year') or now.year)
        m = int(request.args.get('month') or now.month)
    except Exception:
        y, m = now.year, now.month
    data = get_team_overtime_totals(y, m, department=team)
    return jsonify({'ok': True, **data})


@bp.route('/api/my-calendar', methods=['GET'])
def api_my_calendar():
    """Chi tiết calendar của NV (có OT breakdown). Admin có thể ?uid=..."""
    if not session.get('logged_in'):
        return jsonify({'ok': False}), 401
    user = _current_user()
    if not user:
        return jsonify({'ok': False}), 401
    uid = request.args.get('uid') or str(user['id'])
    # Quyền: staff chỉ xem mình; manager xem ai cũng được
    if uid != str(user['id']) and user.get('role') not in ('admin', 'superadmin', 'manager', 'it', 'leader'):
        uid = str(user['id'])
    from datetime import datetime as _dt
    now = _dt.now()
    try:
        y = int(request.args.get('year') or now.year)
        m = int(request.args.get('month') or now.month)
    except Exception:
        y, m = now.year, now.month
    data = get_month_calendar(uid, y, m)
    return jsonify({'ok': True, 'user_id': uid, 'year': y, 'month': m, **data})


# ─── Register function ──────────────────────────────────────────

def register_cham_cong_module(app) -> None:
    """Gọi từ web_app.py để đăng ký module."""
    import logging
    try:
        init_cc_tables()
    except Exception as e:
        logging.warning(f"[ChamCong] init_cc_tables failed: {e}")
        return
    try:
        n = seed_employees_from_users()
        if n:
            logging.info(f"[ChamCong] Seeded {n} employees from users.json")
    except Exception as e:
        logging.warning(f"[ChamCong] seed_employees failed: {e}")

    # Seed ngày lễ VN cho năm hiện tại + năm sau (idempotent ON CONFLICT DO NOTHING)
    try:
        from datetime import datetime as _dt
        y = _dt.now().year
        seed_default_holidays_vn(y)
        seed_default_holidays_vn(y + 1)
    except Exception as e:
        logging.warning(f"[ChamCong] seed_holidays failed: {e}")

    app.register_blueprint(bp)
