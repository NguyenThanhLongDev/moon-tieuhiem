"""Route trang tổng quan Chi phí QC  (GET /chi-phi-qc/).

Tách từ __init__.py — chỉ điều phối: gọi db_queries lấy dữ liệu,
gọi logic dựng dòng page, cộng tổng rồi render template.
"""
from __future__ import annotations

import logging
from datetime import date

from flask import render_template, request, session

from tz_utils import fmt_hcm

from modules.chi_phi_qc.blueprint import chi_phi_qc_bp, login_required
from modules.chi_phi_qc.db_queries import (
    _DEFAULT_VAT_RATE,
    _account_owner_segments,
    _active_pages_for_user,
    _auto_pages_for_date,
    _daily_spend_by_page,
    _get_last_sync,
    _has_page_spend_table,
    _kpi_spend_by_page,
    _load_account_meta,
    _load_auto_phan_loai_map,
    _load_vat_rate_map,
    _manual_declared_pages_for_date,
    _query_pos_known_pages,
    _query_pos_page_metrics,
    _shop_keys_to_account_ids,
    _user_direct_account_ids,
)
from modules.chi_phi_qc.helpers import (
    _NON_ADS_ROLES,
    _get_allowed_shop_keys_for_user,
    _get_team_members,
    _get_user_by_id,
    _is_full_view,
    _load_users_json,
    _normalize_shops,
)
from modules.chi_phi_qc.logic import _build_page_entry, _split_page_entry_by_owner

logger = logging.getLogger(__name__)


@chi_phi_qc_bp.route("/")
@login_required
def index():
    user_id   = session.get("user_id")
    role      = session.get("role", "staff")
    today     = date.today().isoformat()
    # ── Date range (Từ ngày → Đến ngày). Tương thích link cũ ?date= ──
    legacy    = (request.args.get("date") or "").strip()
    date_from = (request.args.get("date_from") or legacy or today).strip() or today
    date_to   = (request.args.get("date_to")   or legacy or today).strip() or today
    # Đảo nếu user chọn ngược (từ > đến)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    # `selected` = ngày Đến: dùng cho các thao tác theo-ngày (khai báo / Win-Test / sync)
    selected  = date_to
    view_tab  = request.args.get("tab", "all")   # "all" or "team"

    has_auto_table = _has_page_spend_table()
    account_meta   = _load_account_meta(date_from, date_to)
    spend_map      = _daily_spend_by_page(date_from, date_to, user_id, role)

    # ── Determine which ad accounts this user is allowed to see ──
    allowed_shop_keys  = _get_allowed_shop_keys_for_user(user_id, role)  # None = all
    if allowed_shop_keys is None:
        allowed_account_ids = None  # admin/ketoan: xem tất cả
    else:
        # Lấy account từ shop được gán (date-aware: mapping theo cửa sổ giao với kỳ lọc)
        shop_based_ids = _shop_keys_to_account_ids(allowed_shop_keys, date_from, date_to)
        # Lấy account được gán trực tiếp cho user này (date-aware theo khoảng lọc)
        direct_ids = set(_user_direct_account_ids(user_id, date_from, date_to))
        # LEADER: gộp thêm TK gán trực tiếp của TẤT CẢ thành viên trong team —
        # nếu không, NV có TK gán-trực-tiếp (không qua shop) sẽ bị thiếu khỏi
        # tab "Phân theo nhân viên" khi leader xem (bug: leader chỉ thấy 1 phần team).
        if role == "leader":
            _cu = _get_user_by_id(user_id)
            _tid = str(_cu.get("team_id", "")).strip()
            for _mb in (_get_team_members(_tid) if _tid else []):
                if _mb.get("id"):
                    direct_ids |= set(_user_direct_account_ids(_mb.get("id"), date_from, date_to))
        # Gộp cả hai nguồn
        allowed_account_ids = list(set(shop_based_ids) | direct_ids)

    # ── Auto-discovered pages (filtered by role) ──
    auto_pages = _auto_pages_for_date(date_from, date_to, allowed_account_ids) if has_auto_table else []

    # ── Manual pages (legacy, for pages not in auto) ──
    # Pass allowed_account_ids so leader sees only team pages (staff uses fb_page_assignments)
    manual_pages = _active_pages_for_user(user_id, role, allowed_account_ids=allowed_account_ids)
    kpi_map_old  = _kpi_spend_by_page(date_from, date_to)

    # ── Manually declared pages (user-typed, no auto sync) ──
    manual_declared = _manual_declared_pages_for_date(date_from, date_to, user_id, role)

    # ── Load Win/Test choices for auto-detected pages ──
    auto_phan_loai_map = _load_auto_phan_loai_map(date_from, date_to)

    # ── Mức VAT mỗi page (6.1%/11.3%, NV tự chọn; mặc định 11.3%) ──
    vat_rate_map = _load_vat_rate_map(date_from, date_to)

    # ── Build unified page list ──
    all_page_ids: set = set()
    pages = []
    for ap in auto_pages:
        meta = account_meta.get(ap.get("fb_ad_account_id") or "", {})
        entry = _build_page_entry(ap, meta, spend_map)
        # Inject Win/Test choice (default: ma_win)
        entry["auto_phan_loai"] = auto_phan_loai_map.get(ap["page_id"], "ma_win")
        pages.append(entry)
        all_page_ids.add(ap["page_id"])

    for mp in manual_pages:
        pid = mp["page_id"]
        if pid in all_page_ids:
            continue
        spend = spend_map.get(pid, {
            "win_chua": 0, "win_co": 0,
            "test_chua": 0, "test_co": 0,
            "tong": 0, "count": 0,
        })
        kpi = kpi_map_old.get(pid, {
            "cp_ads_fb": 0, "cp_ads_fb_vat": 0,
            "impressions": 0, "clicks": 0,
            "fb_account_names": "",
        })
        enriched_accs = []
        for acc in mp.get("ad_accounts", []):
            aid  = acc.get("ad_account_id", "")
            meta = account_meta.get(aid, {})
            enriched_accs.append({
                **acc,
                "ad_account_name": meta.get("name") or acc.get("ad_account_name") or aid,
                "owner_id":        meta.get("owner_id"),
                "owner_name":      meta.get("owner_name"),
                "owner_username":  meta.get("owner_username"),
            })
        pages.append({
            "page_id":       pid,
            "page_name":     mp.get("page_name", pid),
            "picture_url":   mp.get("picture_url"),
            "page_category": mp.get("page_category"),
            "ad_accounts":   enriched_accs,
            "source":        "manual",
            "spend":         spend,
            "kpi":           kpi,
        })

    # ── Manually declared pages (user typed page name, no auto sync) ──
    for md in manual_declared:
        pid = md["page_id"]
        if pid in all_page_ids:
            continue
        spend = spend_map.get(pid, {
            "win_chua": 0, "win_co": 0,
            "test_chua": 0, "test_co": 0,
            "tong": 0, "count": 0,
        })
        aid  = md.get("ad_account_id") or ""
        meta = account_meta.get(aid, {})
        pages.append({
            "page_id":           pid,
            "page_name":         md.get("page_name") or pid,
            "picture_url":       None,
            "page_category":     None,
            "is_manual_decl":    True,
            "declared_user_id":  str(md.get("declared_user_id") or ""),
            "decl_id":           md.get("decl_id"),
            "phan_loai":         md.get("phan_loai") or "ma_win",
            "tien_chua_thue":    float(md.get("tien_chua_thue") or 0),
            "thanh_tien":        float(md.get("thanh_tien") or 0),
            "decl_note":         md.get("note") or "",
            "ad_accounts":       [{
                "ad_account_id":   aid,
                "ad_account_name": meta.get("name") or md.get("ad_account_name") or aid,
                "owner_id":        meta.get("owner_id"),
                "owner_name":      meta.get("owner_name"),
                "owner_username":  meta.get("owner_username"),
            }] if aid else [],
            "source":            "manual_decl",
            "spend":             spend,
            "kpi": {
                "cp_ads_fb":     0,
                "cp_ads_fb_vat": 0,
                "impressions":   0,
                "clicks":        0,
                "fb_account_names": meta.get("name") or md.get("ad_account_name") or "",
            },
        })
        all_page_ids.add(pid)

    # ── Tách dòng theo NV phụ trách: TK đổi tay GIỮA kỳ → 1 dòng tách thành N dòng ──
    # VD: TK X giai đoạn 13/5-31/5 là Danh, 1/6- là Hân → khi lọc 31/5→1/6
    # sẽ tách thành 2 dòng (Danh cho 31/5, Hân cho 1/6) với chi phí cộng dồn riêng.
    try:
        _owner_segs = _account_owner_segments(date_from, date_to)
        _multi_aids = {aid for aid, segs in _owner_segs.items() if len(segs) > 1}
        if _multi_aids:
            _new_pages = []
            for _entry in pages:
                _eaids = [(a.get("ad_account_id") or "") for a in (_entry.get("ad_accounts") or [])]
                if any(aid in _multi_aids for aid in _eaids):
                    _new_pages.extend(_split_page_entry_by_owner(_entry, _owner_segs, date_from, date_to))
                else:
                    _new_pages.append(_entry)
            pages = _new_pages
    except Exception as _split_err:
        logger.error("Split-by-owner error: %s", _split_err)

    # ── Áp mức VAT mỗi page (6.1%/11.3%) → tính lại cp_ads_fb_vat ──
    # Page chưa chọn → mặc định 11.3%. Lưu rate (%) lên page để template hiện nút + tổng.
    # Map {ad_account_id: default_vat_rate} — VAT mặc định mỗi TK. Page sẽ tự áp rate
    # của TK đầu (primary). TK không có entry → mặc định 11.3% (_DEFAULT_VAT_RATE).
    _tk_default_vat: dict = {}
    try:
        from db import get_conn as _gc_v
        with _gc_v() as _c, _c.cursor() as _cur:
            _cur.execute("SELECT fb_ad_account_id, default_vat_rate FROM fb_ad_account_vat_options")
            _tk_default_vat = {str(r[0]): float(r[1] or _DEFAULT_VAT_RATE) for r in _cur.fetchall() if r[0]}
    except Exception as _e:
        logger.warning("Load vat_options error: %s", _e)
    # ── Số POS theo page (đối chiếu CP QC POS vs CP Ads FB) ──
    pos_page_map = _query_pos_page_metrics(date_from, date_to)
    # Page TỪNG có trên POS (bất kỳ ngày) → để phân biệt "chưa có số kỳ này" vs "không phải page POS"
    _all_pids = [p.get("page_id") for p in pages if p.get("page_id")]
    pos_known_map = _query_pos_known_pages(_all_pids) if _all_pids else {}
    # ── Đơn Meta theo page (cả kỳ) — số đơn cho page TEST (giống modal Chi tiết theo ngày) ──
    # Ưu tiên registrations (điền form ladipage) > purchases (lượt mua). Ladipage đa số đếm
    # bằng complete_registration, không bắn purchase.
    _meta_orders: dict = {}
    if _all_pids:
        try:
            from db import get_conn as _gc_m
            with _gc_m() as _cm, _cm.cursor() as _curm:
                _curm.execute("""
                    SELECT page_id,
                           GREATEST(COALESCE(SUM(registrations),0), COALESCE(SUM(purchases),0))
                      FROM mb_fb_entity_daily
                     WHERE page_id = ANY(%s) AND metric_date BETWEEN %s AND %s
                     GROUP BY page_id
                """, (_all_pids, date_from, date_to))
                for _pid, _mo in _curm.fetchall():
                    _meta_orders[str(_pid)] = int(_mo or 0)
        except Exception as _e_mo:
            logger.warning("Load meta orders error: %s", _e_mo)
    for p in pages:
        # Ưu tiên VAT:  #1 admin override per page (vat_rate_map)
        #              #2 TK default rate (theo TK chính của page, từ fb_ad_account_vat_options)
        #              #3 _DEFAULT_VAT_RATE (11.3%) cho TK chưa khai báo
        _page_aids = [(a.get("ad_account_id") or "") for a in (p.get("ad_accounts") or [])]
        _has_declared_tk = any(aid in _tk_default_vat for aid in _page_aids)
        _tk_rate = None
        for _aid in _page_aids:
            if _aid in _tk_default_vat:
                _tk_rate = _tk_default_vat[_aid]
                break
        if _tk_rate is None:
            _tk_rate = _DEFAULT_VAT_RATE
        rate = vat_rate_map.get(p.get("page_id"), _tk_rate)
        p["vat_rate"] = rate
        p["vat_pct"]  = round(rate * 100, 1)
        _fb = float(p.get("kpi", {}).get("cp_ads_fb", 0) or 0)
        if _fb > 0:
            p["kpi"]["cp_ads_fb_vat"] = round(_fb * (1.0 + rate), 0)
        # allow_6_1 = TK có trong vat_options table (admin mới được click radio).
        # TK chưa khai báo → ẩn radio, ẩn ô "Tổng X TK", chỉ hiện số 11.3 tính sẵn.
        p["allow_6_1"] = _has_declared_tk
        # POS metrics (None nếu page chưa có số trong kỳ đang xem)
        p["pos"] = pos_page_map.get(str(p.get("page_id")))
        # Tên page POS đã biết (dù kỳ này chưa có số) → để không ghi nhầm "Chưa có trên POS"
        p["pos_known_name"] = pos_known_map.get(str(p.get("page_id")))
        # Phân loại Win/Test (chốt 2026-08-04 — boss Phong):
        #   • NV đánh tay → theo mark đó
        #   • auto: WIN chỉ khi page CÓ trên POS **VÀ có phát sinh doanh thu**;
        #     page chưa lên POS, HOẶC có trên POS mà doanh thu = 0 → TEST
        _pl_explicit = auto_phan_loai_map.get(p.get("page_id"))
        _pos_rev = float((p["pos"] or {}).get("revenue") or 0) if p.get("pos") else 0.0
        if _pl_explicit == "ma_test":
            p["auto_phan_loai"] = "ma_test"
        elif _pl_explicit == "ma_win":
            p["auto_phan_loai"] = "ma_win"
        elif p["pos_known_name"] and _pos_rev > 0:
            p["auto_phan_loai"] = "ma_win"
        else:
            p["auto_phan_loai"] = "ma_test"
        # Page LÀ page POS (đã từng có số) nhưng kỳ này chưa phát sinh → coi như số 0,
        # hiển thị đầy đủ y như page POS có số (0₫ + lệch + DT/LN/chốt/hoàn = 0).
        if p["pos"] is None and p["pos_known_name"]:
            p["pos"] = {
                "ads": 0.0, "revenue": 0.0, "profit": 0.0, "sales": 0.0,
                "chot": 0, "hoan": 0, "name": p["pos_known_name"], "cp_per_order": 0,
            }
        # Số đơn + nguồn (giống modal): WIN→đơn chốt POS; TEST→đơn Meta (form ladipage)
        if p["auto_phan_loai"] == "ma_win" and p.get("pos"):
            p["so_don"] = int((p["pos"] or {}).get("chot") or 0)
            p["don_src"] = "POS"
        else:
            p["so_don"] = int(_meta_orders.get(str(p.get("page_id")), 0))
            p["don_src"] = "Meta"

    # ── Đánh dấu DÒNG ĐẦU mỗi page + tổng +VAT theo page (GLOBAL list, cho tab "Tất cả page") ──
    # Page chạy nhiều TK → nhiều entry cùng page_id. Dòng có spend cao nhất = primary →
    # hiện đầy đủ POS/lệch + ô "Tổng X TK"; các dòng sau hiện "↑ cùng page".
    from collections import defaultdict as _dd_g
    _g_sum_vat = _dd_g(float); _g_sum_fb = _dd_g(float); _g_row_count = _dd_g(int)
    _g_top: dict = {}
    for _i, _pg in enumerate(pages):
        _pid = _pg.get("page_id")
        _sp = float((_pg.get("kpi") or {}).get("cp_ads_fb", 0) or 0)
        _vt = float((_pg.get("kpi") or {}).get("cp_ads_fb_vat", 0) or 0)
        _g_sum_vat[_pid] += _vt; _g_sum_fb[_pid] += _sp; _g_row_count[_pid] += 1
        if _pid not in _g_top or _sp > _g_top[_pid][1]:
            _g_top[_pid] = (_i, _sp)
    for _i, _pg in enumerate(pages):
        _pid = _pg.get("page_id")
        _pg["_is_primary_row"] = (_g_top[_pid][0] == _i)
        _pg["_page_total_vat"] = round(_g_sum_vat[_pid], 0)
        _pg["_page_total_fb"]  = round(_g_sum_fb[_pid], 0)
        _pg["_page_row_count"] = _g_row_count[_pid]
    # Sort global pages: gom dòng cùng page liền nhau (theo tổng page desc, trong page theo TK spend desc)
    pages.sort(key=lambda p: (
        -float(_g_sum_vat.get(p.get("page_id"), 0) or p["kpi"]["cp_ads_fb"]),
        p.get("page_id") or "",
        -float(p["kpi"]["cp_ads_fb"] or 0),
    ))

    # ── Team groups (for admin/ketoan/leader: group pages by employee) ──
    team_groups = []
    if _is_full_view(role) or role == "leader":
        current_user_data = _get_user_by_id(user_id)
        if role == "leader":
            team_id = str(current_user_data.get("team_id", "")).strip()
            raw_members = _get_team_members(team_id) if team_id else [current_user_data]
        else:
            # admin/ketoan: show all teams grouped
            raw_members = [u for u in _load_users_json()
                           if u.get("status", "active") == "active"
                           and u.get("role") not in ("admin", "superadmin", "ketoan", "accountant")]
        # NV đã nghỉ: vẫn hiện nếu ngày xem (date_from) TRƯỚC ngày nghỉ →
        # lịch sử trước lúc nghỉ vẫn thấy. Nghỉ rồi xem ngày sau ngày nghỉ → ẩn.
        def _is_member_visible(m):
            if str(m.get("status", "active")).strip().lower() != "active":
                return False
            r = (m.get("resigned_at") or "").strip()
            if not r:
                return True
            # date_from ≤ resigned_at-1 (ngày cuối còn active) → vẫn hiện
            return date_from < r
        raw_members = [m for m in raw_members if _is_member_visible(m)]

        # Build lookups: account_id → pages AND declared_user_id → manual pages
        account_id_to_pages: dict = {}
        user_id_to_manual_pages: dict = {}
        for pg in pages:
            for acc in pg.get("ad_accounts", []):
                aid = acc.get("ad_account_id") or ""
                if aid:
                    account_id_to_pages.setdefault(aid, []).append(pg)
            # Track manual pages by the user who declared them
            if pg.get("is_manual_decl") and pg.get("declared_user_id"):
                uid = pg["declared_user_id"]
                user_id_to_manual_pages.setdefault(uid, []).append(pg)

        # ── Page → NV qua PAGE-BINDING shop (chia TK chung cho nhiều NV) ──
        # Nếu 1 page được bind vào shop của NV nào → page đó thuộc NV đó (ưu tiên
        # hơn việc gom theo TK). Cho phép 1 TK chạy nhiều NV: mỗi page về đúng người.
        # Shop→NV phải theo NGƯỜI GIỮ SHOP TRONG KỲ LỌC (usa versioned, migration 058)
        # — không phải chủ hiện tại. Vd shop Ken→Minh Đạt hiệu lực 1/6: lọc tháng 5
        # page/chi phí vẫn thuộc Ken. Cùng bài với fix dashboard 7426275d.
        _period_staff_shops: dict = {}
        if date_from and date_to:
            try:
                import app_ctx as _app_ctx
                _, _period_staff_shops = _app_ctx.load_shop_user_team_map_for_range(date_from, date_to)
            except Exception as _e:
                logger.warning("load_shop_user_team_map_for_range error: %s", _e)
                _period_staff_shops = {}

        def _member_shops_in_period(_mb) -> list:
            if _period_staff_shops:
                return _normalize_shops(sorted(_period_staff_shops.get(str(_mb.get("username") or ""), set())))
            return _normalize_shops(_mb.get("assigned_shops", []))

        shopkey_to_member: dict = {}
        for _mb in raw_members:
            for _sk in _member_shops_in_period(_mb):
                shopkey_to_member.setdefault(_sk, str(_mb.get("id") or ""))
        bound_page_member: dict = {}   # page_id -> member_id (theo binding mới nhất trong khoảng)
        try:
            from db import get_conn as _gc_pb
            _all_pids = [pg["page_id"] for pg in pages if pg.get("page_id")]
            if _all_pids and shopkey_to_member:
                with _gc_pb() as _cn:
                    with _cn.cursor() as _cur:
                        _cur.execute(
                            """
                            SELECT DISTINCT ON (b.page_id) b.page_id, s.shop_key
                              FROM fb_page_shop_binding b
                              JOIN shops s ON s.id = b.pos_shop_id
                             WHERE b.page_id = ANY(%s)
                               AND b.assigned_from <= %s::date
                               AND (b.assigned_to IS NULL OR b.assigned_to >= %s::date)
                             ORDER BY b.page_id, b.assigned_from DESC
                            """,
                            (_all_pids, date_to, date_from),
                        )
                        for _pid, _sk in _cur.fetchall():
                            _mid = shopkey_to_member.get(_sk)
                            if _mid:
                                bound_page_member[str(_pid)] = _mid
        except Exception as _e:
            logger.warning("team page-binding map error: %s", _e)

        # ── Tiền theo (NV, TK, page) CẮT THEO CỬA SỔ SỞ HỮU TK (uaa versioned) ──
        # TK đổi chủ giữa kỳ lọc: tiền NGÀY NÀO thuộc người giữ TK ngày đó — không
        # phải "TK overlap kỳ lọc thì ôm trọn". Vd AnhMinh9.6: yennhith 5-8/6,
        # anhminhth từ 9/6 → lọc 1-12/6 spend ngày 6/6 thuộc Yến Nhi (bug 12/6).
        _uaa_win_spend: dict = {}   # (user_id, ad_account_id, page_id) -> spend trong cửa sổ
        if date_from and date_to:
            try:
                from db import get_conn as _gc_ws
                with _gc_ws() as _cn_ws:
                    with _cn_ws.cursor() as _cur_ws:
                        _cur_ws.execute("""
                            SELECT a.user_id::text, s.fb_ad_account_id, s.page_id, SUM(s.spend)
                              FROM fb_ads_page_daily_spend s
                              JOIN user_ad_account_assignments a
                                ON a.ad_account_id = s.fb_ad_account_id
                               AND s.metric_date >= a.assigned_from
                               AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                             WHERE s.metric_date BETWEEN %s AND %s
                             GROUP BY 1, 2, 3
                        """, (date_from, date_to))
                        for _uid_w, _aid_w, _pid_w, _sp_w in _cur_ws.fetchall():
                            _uaa_win_spend[(str(_uid_w), str(_aid_w), str(_pid_w))] = float(_sp_w or 0)
            except Exception as _e:
                logger.warning("uaa window spend map error: %s", _e)

        # ── POS theo (NV, page) CHỈ TÍNH NGÀY NV GIỮ TK chạy page (đối xứng _uaa_win_spend) ──
        # FB đã cắt theo cửa sổ TK; POS cũng phải vậy, nếu không page chạy test 1 ngày của
        # NV này ôm trọn POS cả kỳ của chủ TK khác (bug 19/6: phuong test page 654 ngày 16
        # mà hiện POS 963k của duytungth ngày 4-9). Ngày NV không có POS → không có key → POS rỗng.
        _uaa_win_pos: dict = {}   # (user_id, page_id) -> pos dict
        if date_from and date_to:
            try:
                from db import get_conn as _gc_wp
                with _gc_wp() as _cn_wp:
                    with _cn_wp.cursor() as _cur_wp:
                        _cur_wp.execute("""
                            SELECT od.user_id, od.page_id,
                                   COALESCE(SUM(pp.ads_amount),0), COALESCE(SUM(pp.revenue),0),
                                   COALESCE(SUM(pp.profit),0), COALESCE(SUM(pp.success_order_count),0),
                                   COALESCE(SUM(pp.returned_order_count),0), MAX(pp.page_name),
                                   MAX(pp.pos_shop_id) AS pos_shop_id
                              FROM (
                                  SELECT DISTINCT a.user_id::text AS user_id, s.page_id, s.metric_date
                                    FROM fb_ads_page_daily_spend s
                                    JOIN user_ad_account_assignments a
                                      ON a.ad_account_id = s.fb_ad_account_id
                                     AND s.metric_date >= a.assigned_from
                                     AND s.metric_date <= COALESCE(a.assigned_to, DATE '9999-12-31')
                                   WHERE s.metric_date BETWEEN %s AND %s
                              ) od
                              JOIN pos_page_daily_metrics pp
                                ON pp.page_id = od.page_id AND pp.metric_date = od.metric_date
                             GROUP BY 1, 2
                        """, (date_from, date_to))
                        for _u, _p, _ads, _rev, _pf, _sc, _rc, _nm, _psid in _cur_wp.fetchall():
                            _ads = float(_ads or 0); _sc = int(_sc or 0)
                            _uaa_win_pos[(str(_u), str(_p))] = {
                                "ads": _ads, "revenue": float(_rev or 0), "profit": float(_pf or 0),
                                "chot": _sc, "hoan": int(_rc or 0), "name": (_nm or "").strip(),
                                "pos_shop_id": (str(_psid) if _psid else None),  # → link Pancake
                                "cp_per_order": round(_ads / _sc, 0) if _sc > 0 else 0,
                            }
            except Exception as _e:
                logger.warning("uaa window POS map error: %s", _e)

        # ── [FIX nhiều NV chung shop] TK gán ĐÍCH DANH cho NV qua uaa → KHÔNG hiện ở NV khác ──
        # Trước: nhiều NV chung shop1 → acc_ids_via_shop = MỌI TK của shop → ai cũng thấy hết TK.
        # Giờ: TK đã gán trực tiếp (user_ad_account_assignments) cho 1 NV cụ thể thì NV khác
        # (dù chung shop) bỏ TK đó. TK chung chưa gán ai vẫn hiện cho mọi NV của shop.
        _acc_direct_owner: dict = {}   # account_id -> set(member_id) đã gán trực tiếp
        for _mb in raw_members:
            if not _mb.get("id"):
                continue
            for _a in (_user_direct_account_ids(_mb.get("id"), date_from, date_to) or []):
                _acc_direct_owner.setdefault(str(_a), set()).add(str(_mb.get("id") or ""))

        for member in raw_members:
            shops     = _member_shops_in_period(member)
            acc_ids_via_shop = _shop_keys_to_account_ids(shops, date_from, date_to) if shops else []
            member_id = str(member.get("id") or "")
            # Bỏ TK đã gán đích danh cho NV KHÁC (giữ TK chung chưa gán ai + TK của chính mình)
            acc_ids_via_shop = [
                _a for _a in acc_ids_via_shop
                if (str(_a) not in _acc_direct_owner) or (member_id in _acc_direct_owner[str(_a)])
            ]
            # Direct assignment: TK QC ↔ NV qua user_ad_account_assignments (date-aware)
            # (TK đã được gán cho NV này, kể cả khi TK chưa có shop mapping).
            # FIX: date-aware → page lịch sử về đúng chủ tại thời điểm đó (không phải chủ hiện tại).
            acc_ids_direct = _user_direct_account_ids(member.get("id"), date_from, date_to) if member.get("id") else []
            # Hợp 2 nguồn → đầy đủ TK của NV
            acc_ids = list(dict.fromkeys(list(acc_ids_via_shop) + list(acc_ids_direct)))
            member_pages = []
            # Dedup theo (page_id, ad_account_id) → page chạy 2 TK = 2 dòng (mỗi dòng 1 TK).
            seen_keys: set = set()

            def _pg_aid(_pg):
                _acs = _pg.get("ad_accounts") or []
                return (_acs[0].get("ad_account_id") if _acs else "") or ""

            # Pages from ad account mapping — BỎ QUA page đã bind cho NV KHÁC
            # (page-binding ưu tiên: TK chung chia page theo shop của từng NV).
            # Dùng shallow copy để team-local flags không corrupt global flags.
            # TK gán TRỰC TIẾP (uaa, không qua shop): cắt tiền theo cửa sổ NV giữ TK
            # — page chỉ có tiền NGOÀI cửa sổ → bỏ hẳn khỏi NV này (về đúng chủ cũ).
            _direct_only = set(acc_ids_direct) - set(acc_ids_via_shop)
            for aid in acc_ids:
                for pg in account_id_to_pages.get(aid, []):
                    pid = pg["page_id"]
                    key = (pid, _pg_aid(pg) or aid)
                    if key in seen_keys:
                        continue
                    _bm = bound_page_member.get(pid)
                    if _bm is not None and _bm != member_id:
                        continue  # page này thuộc NV khác (theo binding) → không gom vào đây
                    _copy = dict(pg)
                    if aid in _direct_only and _uaa_win_spend:
                        _win = _uaa_win_spend.get((member_id, aid, pid), 0.0)
                        _k = dict(_copy.get("kpi") or {})
                        _full = float(_k.get("cp_ads_fb") or 0)
                        if _full > 0:
                            if _win <= 0:
                                continue   # toàn bộ tiền page này nằm ngoài kỳ NV giữ TK
                            if _win < _full:
                                _ratio = _win / _full
                                _k["cp_ads_fb"]     = round(_win, 0)
                                _k["cp_ads_fb_vat"] = round(float(_k.get("cp_ads_fb_vat") or 0) * _ratio, 0)
                                _copy["kpi"] = _k
                    # POS scope theo ngày NV GIỮ TK (uaa) chạy page — áp cho MỌI TK NV giữ
                    # qua uaa (kể cả TK đồng thời map qua shop, nên KHÔNG nằm trong _direct_only).
                    # None = ngày NV không có POS → cột POS trống, không ôm POS cả kỳ của chủ
                    # TK khác (bug 19/6: phuong test page 654 ngày 16 → POS phải trống, không
                    # phải 963k của duytungth ngày 4-9). Page thuần-shop (aid không thuộc uaa
                    # của NV) giữ POS global như cũ.
                    if aid in acc_ids_direct:
                        _copy["pos"] = _uaa_win_pos.get((member_id, pid))
                    member_pages.append(_copy)
                    seen_keys.add(key)

            # Pages BIND vào shop của NV này (kể cả khi TK không thuộc acc_ids của họ)
            for pg in pages:
                pid = pg["page_id"]
                key = (pid, _pg_aid(pg))
                if key in seen_keys:
                    continue
                if bound_page_member.get(pid) == member_id:
                    member_pages.append(dict(pg))
                    seen_keys.add(key)

            # Manual pages declared for/by this member (giữ dedup chỉ theo page_id
            # cho phiếu khai báo tay, vì không gắn TK).
            _seen_manual_pids = {p["page_id"] for p in member_pages}
            for pg in user_id_to_manual_pages.get(member_id, []):
                if pg["page_id"] not in _seen_manual_pids:
                    member_pages.append(dict(pg))
                    _seen_manual_pids.add(pg["page_id"])

            # ── Đánh dấu DÒNG ĐẦU mỗi page + tổng +VAT/FB theo page ──
            from collections import defaultdict as _dd
            _page_sum_vat = _dd(float); _page_sum_fb = _dd(float); _page_row_count = _dd(int)
            _page_top: dict = {}
            for _i, _pg in enumerate(member_pages):
                _pid = _pg.get("page_id")
                _sp = float((_pg.get("kpi") or {}).get("cp_ads_fb", 0) or 0)
                _vt = float((_pg.get("kpi") or {}).get("cp_ads_fb_vat", 0) or 0)
                _page_sum_vat[_pid] += _vt
                _page_sum_fb[_pid]  += _sp
                _page_row_count[_pid] += 1
                if _pid not in _page_top or _sp > _page_top[_pid][1]:
                    _page_top[_pid] = (_i, _sp)
            for _i, _pg in enumerate(member_pages):
                _pid = _pg.get("page_id")
                _pg["_is_primary_row"] = (_page_top[_pid][0] == _i)
                _pg["_page_total_vat"] = round(_page_sum_vat[_pid], 0)
                _pg["_page_total_fb"]  = round(_page_sum_fb[_pid], 0)
                _pg["_page_row_count"] = _page_row_count[_pid]

            # CHỈ giữ page ĐANG CHẠY: có chi phí FB (cp_ads_fb>0) hoặc có khai báo
            # (spend.tong>0). Bỏ page legacy map vào TK nhưng 0đ (gây rối báo cáo).
            member_pages = [
                p for p in member_pages
                if float((p.get("kpi") or {}).get("cp_ads_fb", 0) or 0) > 0
                or float((p.get("spend") or {}).get("tong", 0) or 0) > 0
            ]

            # Ẩn NV KHÔNG chạy quảng cáo: không có TK QC nào gán VÀ không có khai
            # báo tay. (Loại kho/sale/it + staff không phụ trách ads như vuit/huynhth.)
            # NV ads có TK nhưng 0đ hôm nay → acc_ids vẫn có → VẪN hiện ("chưa có chi phí").
            _role = str(member.get("role", "")).strip().lower()
            if (not acc_ids and not member_pages) or _role in _NON_ADS_ROLES:
                continue

            team_groups.append({
                "user_id":   member.get("id"),
                "username":  member.get("username", ""),
                "full_name": member.get("full_name", "") or member.get("username", ""),
                "role":      member.get("role", "staff"),
                "team_id":   member.get("team_id", ""),
                "shop_keys": shops,
                "acc_ids":   acc_ids,
                "pages":     sorted(member_pages, key=lambda p: (
                    -float(_page_sum_vat.get(p.get("page_id"), 0) or p["kpi"]["cp_ads_fb"]),
                    p.get("page_id") or "",
                    -float(p["kpi"]["cp_ads_fb"] or 0),
                )),
                "totals": {
                    "cp_ads_fb":     sum(p["kpi"]["cp_ads_fb"]     for p in member_pages),
                    "cp_ads_fb_vat": sum(p["kpi"]["cp_ads_fb_vat"] for p in member_pages),
                    # effective total: use FB VAT when auto-detected, else manual declaration
                    "tong_khai":     sum(
                        p["kpi"]["cp_ads_fb_vat"] if p["kpi"]["cp_ads_fb_vat"] > 0
                        else p["spend"]["tong"]
                        for p in member_pages
                    ),
                },
            })
        # Sort by cp_ads_fb desc
        team_groups.sort(key=lambda g: g["totals"]["cp_ads_fb"], reverse=True)

    # Effective win/test: auto pages counted by their Win/Test classification
    eff_win = eff_test = 0.0
    for p in pages:
        vat = p["kpi"]["cp_ads_fb_vat"]
        if vat > 0:
            # Auto page — count based on Win/Test choice
            if p.get("auto_phan_loai", "ma_win") == "ma_test":
                eff_test += vat
            else:
                eff_win  += vat
        else:
            # Manual declaration — use declared amounts
            eff_win  += p["spend"]["win_co"]
            eff_test += p["spend"]["test_co"]

    totals = {
        "win_chua":      sum(p["spend"]["win_chua"]      for p in pages),
        "win_co":        sum(p["spend"]["win_co"]        for p in pages),
        "test_chua":     sum(p["spend"]["test_chua"]     for p in pages),
        "test_co":       sum(p["spend"]["test_co"]       for p in pages),
        # effective win/test includes auto page amounts
        "effective_win":  eff_win,
        "effective_test": eff_test,
        # effective total: FB VAT when auto-detected, else manual declaration
        "tong":          sum(
            p["kpi"]["cp_ads_fb_vat"] if p["kpi"]["cp_ads_fb_vat"] > 0
            else p["spend"]["tong"]
            for p in pages
        ),
        "count":         sum(p["spend"]["count"]         for p in pages),
        "cp_ads_fb":     sum(p["kpi"]["cp_ads_fb"]       for p in pages),
        "cp_ads_fb_vat": sum(p["kpi"]["cp_ads_fb_vat"]  for p in pages),
        # Tổng CP QC POS (chỉ page có trên POS) — để đối chiếu ở tfoot
        "pos_ads":       sum((p.get("pos") or {}).get("ads", 0) for p in pages),
        "pos_chot":      sum((p.get("pos") or {}).get("chot", 0) for p in pages),
        "pos_count":     sum(1 for p in pages if p.get("pos")),
        # Tổng Doanh thu / Lợi nhuận / Hoàn từ POS — cho dòng tổng bảng
        "pos_sales":     sum((p.get("pos") or {}).get("sales", 0) for p in pages),
        "pos_revenue":   sum((p.get("pos") or {}).get("revenue", 0) for p in pages),
        "pos_profit":    sum((p.get("pos") or {}).get("profit", 0) for p in pages),
        "pos_hoan":      sum((p.get("pos") or {}).get("hoan", 0) for p in pages),
    }

    # Last sync time for the selected date
    last_sync = _get_last_sync(selected)
    last_sync_at = ""
    if last_sync and last_sync.get("synced_at"):
        last_sync_at = fmt_hcm(last_sync["synced_at"], "%H:%M %d/%m/%Y")

    # Tổng NV hiển thị (giờ hiện cả NV chưa có chi phí trong khoảng ngày)
    team_active_count = len(team_groups)
    team_pages_total  = sum(len(g.get("pages", [])) for g in team_groups)

    # ── Load page→shop binding (2 tier: explicit + auto-detected) + shops của user ──
    page_bindings: dict = {}
    user_shops: list = []
    try:
        from db import get_conn as _gc
        from repositories.admin_repo import list_page_bindings_active
        with _gc() as _conn:
            with _conn.cursor() as _cur:
                # Tier 1: explicit page bindings
                page_bindings = list_page_bindings_active(_cur)
                # Tier 2: auto-detect — pages của TK đơn shop (fb_ad_account_mappings)
                # Lấy danh sách TK chỉ map 1 shop active
                _cur.execute(
                    """
                    SELECT m.fb_ad_account_id, MIN(m.shop_id), MIN(s.shop_name), MIN(s.shop_key)
                      FROM fb_ad_account_mappings m
                      JOIN shops s ON s.id = m.shop_id
                     WHERE m.assigned_to IS NULL
                     GROUP BY m.fb_ad_account_id
                    HAVING COUNT(*) = 1
                    """
                )
                _single_shop_tk = {
                    str(r[0]): {"pos_shop_id": int(r[1]), "shop_name": r[2] or "", "shop_key": r[3] or ""}
                    for r in _cur.fetchall()
                }
                # Map page_id → TK đơn shop (qua fb_ads_page_daily_spend) — trong khoảng ngày đang xem
                _cur.execute(
                    """
                    SELECT DISTINCT s.page_id, s.fb_ad_account_id
                      FROM fb_ads_page_daily_spend s
                     WHERE s.metric_date BETWEEN %s AND %s
                       AND s.fb_ad_account_id = ANY(%s)
                    """,
                    (date_from, date_to, list(_single_shop_tk.keys()) or [""]),
                )
                for r in _cur.fetchall():
                    pid = str(r[0])
                    aid = str(r[1])
                    if pid in page_bindings:
                        continue  # Tier 1 (explicit) đã có → giữ
                    tk_shop = _single_shop_tk.get(aid)
                    if tk_shop:
                        page_bindings[pid] = {
                            **tk_shop,
                            "shop_source": "auto",
                        }
                # Mark tier 1 explicit ones với shop_source='manual' để UI phân biệt
                for pid, b in page_bindings.items():
                    if "shop_source" not in b:
                        b["shop_source"] = "manual"

                # Shops của user hiện tại
                _priv = (role or "").lower() in {"admin", "superadmin", "manager", "accountant", "ketoan", "it", "leader", "sale_leader"}
                if _priv:
                    _cur.execute("SELECT id, shop_name, shop_key FROM shops WHERE status='active' ORDER BY shop_name")
                else:
                    _cur.execute(
                        "SELECT s.id, s.shop_name, s.shop_key FROM user_shop_assignments usa "
                        "JOIN shops s ON s.id=usa.shop_id WHERE usa.user_id=%s AND usa.assigned_to IS NULL AND s.status='active' "
                        "ORDER BY s.shop_name",
                        (int(user_id) if user_id else 0,),
                    )
                user_shops = [
                    {"id": int(r[0]), "shop_name": r[1] or "", "shop_key": r[2] or ""}
                    for r in _cur.fetchall()
                ]
    except Exception as _exc:
        logger.warning("Load page_bindings/user_shops error: %s", _exc)

    return render_template("chi_phi_qc/index.html",
                           pages=pages,
                           totals=totals,
                           team_groups=team_groups,
                           team_active_count=team_active_count,
                           team_pages_total=team_pages_total,
                           view_tab=view_tab,
                           selected_date=selected,
                           date_from=date_from,
                           date_to=date_to,
                           is_range=(date_from != date_to),
                           today=today,
                           current_user_id=user_id,
                           current_role=role,
                           has_auto_table=has_auto_table,
                           auto_page_count=len(auto_pages),
                           last_sync_at=last_sync_at,
                           page_bindings=page_bindings,
                           user_shops=user_shops)
