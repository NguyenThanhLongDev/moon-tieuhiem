"""Module salary_2b — MÔ PHỎNG lương theo phương án 2B (chưa phải bảng lương chính thức).

Công thức (chốt với sếp 11/06/2026):
    LƯƠNG THÁNG M = LƯƠNG CƠ BẢN THÁNG M + KPI THÁNG M-1   (KPI trả trễ 1 tháng)

    LƯƠNG CƠ BẢN = (lương vùng + phụ cấp vùng) × số công đạt chuẩn / 26
        - Công đạt chuẩn: ngày chấm công ≥ 8.5h (tính từ check_in/out, cột regular_minutes chết)
        - Vùng lấy từ HR (hr_profiles.region / teams.region — migration 057, sếp chỉ định
          Nam/Thành/Công Minh = ĐN, Nhật/Minh/Ken = HCM), fallback GPS chấm công (lat<13 → HCM)
        - HCM: 5.200.000 + 800.000 · ĐN: 4.000.000 + 700.000 (sếp chốt 11/06)
        - Thử việc (tháng dương chứa hire_date): 85% lương cứng, chưa phụ cấp
        - Phụ cấp chỉ áp khi đủ 3 tháng kể từ hire_date (không có hire_date → coi như lâu năm)

    KPI = bậc(% ads/DT) × DOANH THU SAU THUẾ SAU HOÀN tháng M-1
        - DT/ads lấy theo SHOP GÁN CHO NV (user_shop_assignments × daily_shop_metrics)
          — đúng "thước đo của công ty" (khớp dashboard từng đồng)
        - Hoàn = returned_count × AOV (ước lượng — POS không trả giá trị hoàn per shop/ngày)
        - Sau thuế: chia (1 + VAT 8%) — cùng định nghĩa engine modules/salary
        - Bậc calibrate để gần khớp bảng kế toán đang trả
          (verify: Chí Năm T5 = 12.567.461 vs Excel thật 12.524.944, lệch 0,34%)

Module này READ-ONLY — chỉ SELECT, không ghi gì vào DB.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session

logger = logging.getLogger(__name__)

salary_2b_bp = Blueprint(
    "salary_2b", __name__,
    template_folder="templates",
    url_prefix="/salary-2b",
)

_CONFIG_KEY = "salary_2b_config"

# ── Cấu hình MẶC ĐỊNH — bản chạy thật chỉnh tại /salary-2b/config (lưu DB) ──
CONFIG = {
    "vat_rate": 0.08,            # DT sau thuế = DT / (1 + vat_rate)
    "gio_chuan": 8.5,            # ngưỡng 1 công (Q2 đã chốt)
    "gio_nua_cong": 4.25,        # ≥ nửa ngày chuẩn = 0,5 công (sếp chốt 11/06)
    "cong_chuan": 26,            # số công chuẩn / tháng
    "ty_le_thu_viec": 0.85,      # tháng đầu (theo hire_date HR) hưởng 85% lương cứng
    # vùng → (lương cơ bản, phụ cấp) — nhân theo công/26
    "luong_vung": {
        "HCM": (5_200_000, 800_000),
        "DN":  (4_000_000, 700_000),
    },
    # bậc hoa hồng: (% ads/DT từ, đến, % hưởng trên DT net)
    # calibrate theo dòng Chí Năm trong Excel kế toán T5
    "tiers": [
        (0.0,  26.0, 3.2),
        (26.0, 35.0, 2.5),
        (35.0, 45.0, 1.5),
        (45.0, None, 0.0),   # >45% ads/DT → 0đ + cảnh báo dừng chạy
    ],
    # Lương leader (giải mã từ Excel "Lương Lead MN", verify 6/6 dòng):
    # 15tr cứng + pct% × LN RÒNG team = (LN Pancake − CP test − CP chung) × (1 − thuế TNDN).
    # CP chung kế toán phân bổ 8.242đ/đơn (suy ra từ Excel — 4/4 team khớp).
    # LN âm → TRỪ lương theo đúng % (như Excel: Đỗ Đình Hùng −570K).
    # Tách 3 nguồn như Excel: CTY (TNDN 20%) · HKD (GTGT 1% doanh thu + TNDN 20%) · POS3 (miễn thuế)
    "leader": {
        "luong_cung": 15_000_000,
        "pct": 3.0,
        "cp_chung_per_don": 8_242,
        "thue_tndn": 20.0,
        "thue_gtgt_hkd": 1.0,
    },
    # Lương IT cố định / 26 công (sếp chốt 11/06): Vũ 12tr, Long 11tr
    "it": {"vuit": 12_000_000, "longit": 11_000_000},
    # Lương sale = LƯƠNG GIỜ (giải mã Excel "Lương sale MN", verify từng dòng):
    # giờ thường × đơn giá + giờ tăng ca (vượt 8,5h/ngày thường) × 35K
    # + giờ CN × đơn giá × 1,5 + giờ lễ × hệ số bảng cc_holidays.
    # Trách nhiệm quản lý 3tr · phụ cấp 800K/30 × công (sau 3 tháng, prorate từ ngày đủ)
    # · chuyên cần ≥30 công 500K, ≥26 công 200K.
    "sale": {
        "rate_default": 27_000,
        "rate_per_user": {"trongnam": 30_000, "thanhthaosale": 30_000, "thusale": 30_000},
        "ot_rate": 35_000,
        "he_so_cn": 1.5,
        "trach_nhiem": {"trongnam": 3_000_000},  # đích danh NV → tiền trách nhiệm
        "phu_cap_thang": 800_000,
        "chuyen_can": [[30, 500_000], [26, 200_000]],
    },
    # Lương KHO (Excel "Lương hành chính+ kho", verify: Khánh Huyền
    # 244,59h×30K + 44,8h×45K = 9.353.700 ✓): lương giờ 30K, ≤8h/ngày thường,
    # vượt + CN + lễ × 1,5. Phụ cấp 30K/công. Chuyên cần như sale.
    # quangkho (quản lý) LƯƠNG THÁNG 15tr chia 30 ngày (kho làm cả CN) —
    # giải mã: kế toán trả 425K/ngày = 15tr × 85% thử việc ÷ 30 (vào làm 17/04).
    # trangkho (quản lý) + trách nhiệm 3tr.
    "kho": {
        "rate_default": 30_000,
        "cap_gio": 8.0,
        "he_so_ot": 1.5,
        "phu_cap_ngay": 30_000,
        "ngay_chia_luong_thang": 30,
        "trach_nhiem": {"trangkho": 3_000_000},
        "luong_thang_rieng": {"quangkho": 15_000_000},
        "chuyen_can": [[30, 500_000], [26, 200_000]],
    },
    # HÀNH CHÍNH (mua bán / kế toán): lương tháng × công/26 (thử việc ×85% tự áp
    # theo hire_date) + phụ cấp 30K/công (sau 3 tháng) − bảo hiểm.
    # Verify Excel: Diệu 12tr×25/26 + 750K − 472,5K = 11.815.961 ✓ ·
    # Thái Huyền = 5tr×85%×21/26 = 3.432.692 ✓ (chính là luật thử việc 85%).
    "hanh_chinh": {
        "nu":      {"luong_thang": 8_000_000, "bao_hiem": 472_500},
        "huyenkt": {"luong_thang": 5_000_000, "bao_hiem": 0},
    },
    "hc_phu_cap_ngay": 30_000,
}

_FULL_VIEW_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}
_CONFIG_EDIT_ROLES = {"admin", "superadmin", "manager"}


def get_config() -> dict:
    """Config 2B: bản lưu DB (app_config) đè lên mặc định. Sếp chỉnh tại /salary-2b/config."""
    cfg = json.loads(json.dumps(CONFIG))  # deep copy
    try:
        from db import query_all
        rows = query_all("SELECT value FROM app_config WHERE key = %s", (_CONFIG_KEY,))
        if rows and rows[0][0]:
            saved = json.loads(rows[0][0])
            for k in ("vat_rate", "gio_chuan", "gio_nua_cong", "cong_chuan", "ty_le_thu_viec"):
                if k in saved:
                    cfg[k] = float(saved[k])
            if isinstance(saved.get("luong_vung"), dict):
                for reg, pair in saved["luong_vung"].items():
                    if reg in cfg["luong_vung"] and isinstance(pair, (list, tuple)) and len(pair) == 2:
                        cfg["luong_vung"][reg] = (float(pair[0]), float(pair[1]))
            if isinstance(saved.get("leader"), dict):
                for k in ("luong_cung", "pct", "cp_chung_per_don", "thue_tndn"):
                    if k in saved["leader"]:
                        cfg["leader"][k] = float(saved["leader"][k])
            if isinstance(saved.get("it"), dict):
                cfg["it"] = {str(u): float(v) for u, v in saved["it"].items()}
            if isinstance(saved.get("kho"), dict):
                for k, v in saved["kho"].items():
                    if k in ("trach_nhiem", "luong_thang_rieng") and isinstance(v, dict):
                        cfg["kho"][k] = {str(u): float(x) for u, x in v.items()}
                    elif k == "chuyen_can" and isinstance(v, list):
                        cfg["kho"][k] = [[float(a), float(b)] for a, b in v]
                    elif k in cfg["kho"]:
                        cfg["kho"][k] = float(v)
            if isinstance(saved.get("hanh_chinh"), dict):
                cfg["hanh_chinh"] = {
                    str(u): {"luong_thang": float(d.get("luong_thang", 0)),
                             "bao_hiem": float(d.get("bao_hiem", 0))}
                    for u, d in saved["hanh_chinh"].items() if isinstance(d, dict)
                }
            if isinstance(saved.get("sale"), dict):
                for k, v in saved["sale"].items():
                    if k in ("rate_per_user", "trach_nhiem") and isinstance(v, dict):
                        cfg["sale"][k] = {str(u): float(x) for u, x in v.items()}
                    elif k == "chuyen_can" and isinstance(v, list):
                        cfg["sale"][k] = [[float(a), float(b)] for a, b in v]
                    elif k in cfg["sale"]:
                        cfg["sale"][k] = float(v)
            if isinstance(saved.get("tiers"), list) and saved["tiers"]:
                tiers = []
                for t in saved["tiers"]:
                    if isinstance(t, (list, tuple)) and len(t) == 3:
                        mx = None if t[1] in (None, "", "null") else float(t[1])
                        tiers.append((float(t[0]), mx, float(t[2])))
                if tiers:
                    cfg["tiers"] = tiers
    except Exception:
        logger.exception("salary_2b: lỗi đọc config DB — dùng mặc định")
    return cfg


def save_config(cfg: dict) -> None:
    from db import get_conn
    payload = json.dumps(cfg, ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (_CONFIG_KEY, payload))
        conn.commit()


def _require_full_view(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") and not session.get("username"):
            return redirect("/login?next=/salary-2b/")
        if session.get("role") not in _FULL_VIEW_ROLES:
            return "Không có quyền xem trang này", 403
        return f(*args, **kwargs)
    return decorated


def _month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _la_thu_viec(hire_date, m_start) -> bool:
    """Thử việc = tháng lương bắt đầu khi chưa đủ 30 ngày kể từ ngày vào làm.
    (vd vào 17/04 → tháng 4 VÀ tháng 5 đều 85% — khớp cách kế toán áp với Đăng Quang.)"""
    from datetime import timedelta
    return bool(hire_date) and m_start < hire_date + timedelta(days=30)


def _tier_pick(pct: float, tiers):
    for mn, mx, com in tiers:
        if pct >= mn and (mx is None or pct < mx):
            return com
    return 0.0


def compute_salary_2b(year: int, month: int, cfg: dict | None = None):
    """Tính lương tháng (year, month): LCB tháng đó + KPI tháng trước. Chỉ SELECT."""
    from db import query_all

    cfg = cfg or get_config()
    m_start, m_end = _month_bounds(year, month)
    if month == 1:
        k_start, k_end = _month_bounds(year - 1, 12)
    else:
        k_start, k_end = _month_bounds(year, month - 1)

    # 1. Công tháng M (tính từ check_in/out): ≥ giờ chuẩn = 1 công,
    #    ≥ nửa giờ chuẩn = 0,5 công (kế toán có nửa công — sếp chốt 11/06)
    cong = {r[0]: float(r[1] or 0) for r in query_all("""
        SELECT u.username,
               SUM(CASE
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 1.0
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 0.5
                   ELSE 0 END)
        FROM cc_attendance a JOIN users u ON u.id = a.user_id::bigint
        WHERE a.date >= %s AND a.date < %s
          AND a.check_in IS NOT NULL AND a.check_out IS NOT NULL
        GROUP BY 1""", (cfg["gio_chuan"], cfg["gio_nua_cong"], m_start, m_end))}

    # 2. Vùng: HR (hr_profiles.region, backfill theo teams.region — migration 057)
    #    → fallback GPS chấm công nếu HR chưa set
    region, hire = {}, {}
    for u, reg_hr, reg_team, hd in query_all("""
        SELECT u.username, p.region, t.region, p.hire_date
        FROM users u
        LEFT JOIN hr_profiles p ON p.user_id = u.id
        LEFT JOIN teams t ON t.id = u.team_id"""):
        r = reg_hr or reg_team
        if r:
            region[u] = r
        if hd:
            hire[u] = hd
    for u, lat in query_all("""
        SELECT u.username, AVG(s.check_in_lat)
        FROM cc_attendance_sessions s JOIN users u ON u.id = s.user_id::bigint
        WHERE s.date >= %s - INTERVAL '60 days' AND s.date < %s
          AND s.check_in_lat IS NOT NULL
        GROUP BY 1""", (m_start, m_end)):
        region.setdefault(u, "HCM" if float(lat) < 13 else "DN")

    # 2B. HOÀN THẬT từng đồng từ bảng orders (cron resync_old_orders nuôi) —
    #     chỉ dùng khi tháng KPI đã phủ ≥80% số đơn hoàn so với POS analytics,
    #     chưa đủ (tháng cũ chưa backfill) thì rớt về hoàn ƯỚC = count × AOV.
    hoan_that, dung_hoan_that = {}, False
    try:
        cov = query_all("""
            SELECT (SELECT COUNT(*) FROM orders
                    WHERE created_at_pos >= %s AND created_at_pos < %s
                      AND order_status IN ('returned', 'returning')),
                   (SELECT COALESCE(SUM(returned_count), 0) FROM daily_shop_metrics
                    WHERE metric_date::date >= %s AND metric_date::date < %s)""",
                        (k_start, k_end, k_start, k_end))[0]
        if cov[1] and float(cov[0]) / float(cov[1]) >= 0.8:
            dung_hoan_that = True
            hoan_that = {r[0]: float(r[1] or 0) for r in query_all("""
                -- net_revenue = TIỀN THỰC THU của đơn (sau giảm giá). total_amount là
                -- giá niêm yết → trừ hoàn DƯ ~10%, NV bị thiệt (INCIDENTS 11/06, sếp duyệt đổi).
                SELECT u.username, SUM(o.net_revenue)
                FROM orders o
                JOIN user_shop_assignments usa ON usa.shop_id = o.shop_id
                     AND o.created_at_pos::date >= usa.assigned_from
                     AND (usa.assigned_to IS NULL OR o.created_at_pos::date <= usa.assigned_to)
                JOIN users u ON u.id = usa.user_id
                LEFT JOIN cc_employees e ON e.user_id = u.id::text
                WHERE o.created_at_pos >= %s AND o.created_at_pos < %s
                  AND o.order_status IN ('returned', 'returning')
                  AND o.created_at_pos::date <= COALESCE(u.resigned_at::date, e.resigned_at, %s)
                GROUP BY 1""", (k_start, k_end, k_end))}
    except Exception:
        logger.exception("salary_2b: lỗi đọc hoàn thật — dùng hoàn ước")

    # 3. KPI tháng M-1: DT/ads/hoàn theo shop gán cho NV.
    #    NV nghỉ việc (resigned_at từ nút tích của leader ở /cham-cong/admin):
    #    chỉ tính DT đến hết ngày nghỉ — phần sau quy về công ty, không tính cho ai.
    rows = query_all("""
        SELECT u.username, u.full_name, u.role,
               SUM(m.gross_revenue), SUM(m.ads_cost),
               SUM(m.order_count), SUM(m.returned_count),
               COALESCE(u.resigned_at::date, e.resigned_at) AS resigned,
               SUM(m.pos_profit_loss) AS lai
        FROM daily_shop_metrics m
        JOIN shops s ON s.id = m.shop_id
        JOIN user_shop_assignments usa ON usa.shop_id = s.id
             AND m.metric_date::date >= usa.assigned_from
             AND (usa.assigned_to IS NULL OR m.metric_date::date <= usa.assigned_to)
        JOIN users u ON u.id = usa.user_id
        LEFT JOIN cc_employees e ON e.user_id = u.id::text
        WHERE m.metric_date::date >= %s AND m.metric_date::date < %s
          AND m.metric_date::date <= COALESCE(u.resigned_at::date, e.resigned_at, %s)
        GROUP BY 1, 2, 3, 8
        HAVING SUM(m.gross_revenue) > 0
        ORDER BY SUM(m.gross_revenue) DESC""", (k_start, k_end, k_end))

    # AUDIT 12/06 P0#2: roster KHÔNG được "kéo" từ DT tháng M−1 — NV mới (chưa có
    # shop/DT kỳ trước) vẫn phải có LƯƠNG CƠ BẢN theo công tháng M. Bổ sung NV
    # marketing có công trong tháng nhưng vắng mặt trong rows (KPI = 0).
    rows = list(rows)
    seen_users = {r[0] for r in rows}
    missing = [u for u in cong.keys() if u not in seen_users and cong.get(u, 0) > 0]
    if missing:
        extra = query_all("""
            SELECT u.username, u.full_name, u.role::text,
                   COALESCE(u.resigned_at::date, e.resigned_at)
            FROM users u
            LEFT JOIN cc_employees e ON e.user_id = u.id::text
            WHERE u.username = ANY(%s)
              AND u.role::text = 'staff'
              AND (EXISTS (SELECT 1 FROM user_ad_account_assignments ua
                           WHERE ua.user_id = u.id AND ua.assigned_to IS NULL)
                   OR EXISTS (SELECT 1 FROM user_shop_assignments usa
                              WHERE usa.user_id = u.id AND usa.assigned_to IS NULL))
        """, (missing,))
        for username, full_name, role, resigned in extra:
            rows.append((username, full_name, role, 0, 0, 0, 0, resigned, 0))

    out, warn_no_cong = [], []
    for username, full_name, role, dt, ads, orders, returned, resigned, lai in rows:
        if (role or "") == "leader":
            continue  # leader có công thức riêng (15tr + %LN ròng team) — bảng dưới
        dt = float(dt or 0)
        ads = float(ads or 0)
        if dung_hoan_that:
            hoan = hoan_that.get(username, 0.0)
        else:
            aov = dt / float(orders) if orders else 0.0
            hoan = float(returned or 0) * aov
        dt_net = max(dt - hoan, 0.0) / (1.0 + cfg["vat_rate"])
        ads_pct = ads / dt * 100 if dt else 0.0
        tier = _tier_pick(ads_pct, cfg["tiers"])
        kpi = dt_net * tier / 100.0

        c = cong.get(username, 0)
        reg = region.get(username, "DN")
        base, pc = cfg["luong_vung"][reg]
        # Thâm niên theo hire_date (HR): tháng đầu = thử việc 85% lương cứng,
        # phụ cấp chỉ áp khi đã làm đủ 3 tháng. Không có hire_date → coi như NV lâu năm.
        hd = hire.get(username)
        thu_viec = _la_thu_viec(hd, m_start)
        du_3_thang = (not hd) or ((year * 12 + month) - (hd.year * 12 + hd.month) >= 3)
        if thu_viec:
            base = base * cfg["ty_le_thu_viec"]
        if not du_3_thang:
            pc = 0
        lcb = (base + pc) * c / cfg["cong_chuan"]
        if c == 0:
            warn_no_cong.append(username)

        out.append({
            "username": username, "full_name": full_name or username,
            "role": role or "", "is_leader": "leader" in (role or ""),
            "region": reg, "cong": c, "lcb": lcb,
            "thu_viec": thu_viec, "co_phu_cap": du_3_thang and not thu_viec,
            "resigned": resigned, "lai": float(lai or 0),
            "dt": dt, "ads": ads, "hoan": hoan, "dt_net": dt_net,
            "ads_pct": ads_pct, "tier": tier, "kpi": kpi,
            "tong": lcb + kpi,
            "stop_run": tier == 0.0 and ads_pct > 0,
        })

    totals = {
        "dt": sum(r["dt"] for r in out),
        "ads": sum(r["ads"] for r in out),
        "lai": sum(r["lai"] for r in out),
        "lcb": sum(r["lcb"] for r in out),
        "kpi": sum(r["kpi"] for r in out),
        "tong": sum(r["tong"] for r in out),
        "so_nv": len(out),
        "so_bac_0": sum(1 for r in out if r["stop_run"]),
        "so_lo": sum(1 for r in out if r["lai"] < 0),
        "hoan_nguon": "THẬT (từng đơn từ POS)" if dung_hoan_that else "ước lượng (số đơn hoàn × đơn TB)",
    }
    return out, totals, warn_no_cong, (k_start, m_start)


def _cptest_key(year: int, month: int) -> str:
    return f"salary_2b_cptest_{year}_{month:02d}"


def get_cp_test(year: int, month: int) -> dict:
    """CP test hàng per team cho kỳ KPI (year, month) — kế toán nhập tay, lưu app_config."""
    from db import query_all
    try:
        rows = query_all("SELECT value FROM app_config WHERE key = %s",
                         (_cptest_key(year, month),))
        if rows and rows[0][0]:
            return {int(k): float(v) for k, v in json.loads(rows[0][0]).items()}
    except Exception:
        logger.exception("salary_2b: lỗi đọc cp_test")
    return {}


def compute_leaders(year: int, month: int, cfg: dict):
    """Lương leader tháng (year, month) = lương cứng + pct% × LN ròng team tháng M-1.
    LN ròng = (Σ lãi POS shop của thành viên team − CP test − CP chung/đơn × Σ đơn) × (1 − thuế).
    Công thức giải mã từ Excel "Lương Lead MN" (đã verify 6/6 dòng tháng 4)."""
    from db import query_all

    if month == 1:
        k_start, k_end = _month_bounds(year - 1, 12)
    else:
        k_start, k_end = _month_bounds(year, month - 1)
    lc = cfg["leader"]
    cp_test = get_cp_test(k_start.year, k_start.month)

    # Nguồn chia shop→team CHÍNH TẮC: shops.team_id (đã backfill 11/06 từ NV gán shop;
    # verify HKD khớp Excel 99%). KHÔNG suy qua user_shop_assignments nữa — NV chuyển
    # team sẽ làm trôi lịch sử. Tách 3 nguồn như Excel: CTY / HKD / POS3.
    rows = query_all("""
        SELECT t.id, t.team_name, lu.username, lu.full_name,
               CASE WHEN s.business_type = 'cty' THEN 'CTY'
                    WHEN s.business_type = 'pos3'
                         OR s.shop_name ILIKE '%%pos%%3%%' OR s.shop_key ILIKE '%%pos3%%' THEN 'POS3'
                    ELSE 'HKD' END AS nguon,
               SUM(m.order_count), SUM(m.pos_profit_loss), SUM(m.gross_revenue)
        FROM teams t
        JOIN users lu ON lu.id = t.leader_user_id
        JOIN shops s ON s.team_id = t.id
        JOIN daily_shop_metrics m ON m.shop_id = s.id
        WHERE m.metric_date::date >= %s AND m.metric_date::date < %s
        GROUP BY 1, 2, 3, 4, 5""", (k_start, k_end))

    teams: dict[int, dict] = {}
    for team_id, team_name, username, full_name, nguon, orders, lai, dt in rows:
        e = teams.setdefault(team_id, {
            "team_id": team_id, "team_name": team_name,
            "username": username, "full_name": full_name or username,
            "orders": 0, "lai": 0.0, "cp_chung": 0.0, "gtgt": 0.0,
            "ln_rong": 0.0, "cp_test": float(cp_test.get(team_id, 0)),
        })
        orders = int(orders or 0)
        lai = float(lai or 0)
        chung = orders * lc["cp_chung_per_don"]
        gtgt = float(dt or 0) * lc["thue_gtgt_hkd"] / 100.0 if nguon == "HKD" else 0.0
        ln_truoc = lai - chung - gtgt
        # CP test kế toán nhập 1 số cho cả team → trừ vào nguồn CTY (như Excel)
        if nguon == "CTY":
            ln_truoc -= e["cp_test"]
        ln_rong = ln_truoc if nguon == "POS3" else ln_truoc * (1 - lc["thue_tndn"] / 100.0)
        e["orders"] += orders
        e["lai"] += lai
        e["cp_chung"] += chung
        e["gtgt"] += gtgt
        e["ln_rong"] += ln_rong

    out = []
    for e in teams.values():
        e["luong_ln"] = e["ln_rong"] * lc["pct"] / 100.0
        e["luong_cung"] = lc["luong_cung"]
        e["tong"] = lc["luong_cung"] + e["luong_ln"]
        out.append(e)
    out.sort(key=lambda x: -x["tong"])
    return out, (k_start.year, k_start.month)


def compute_it(year: int, month: int, cfg: dict, cong: dict | None = None):
    """Lương IT cố định × công/26."""
    from db import query_all
    if not cfg.get("it"):
        return []
    m_start, m_end = _month_bounds(year, month)
    unames = tuple(cfg["it"].keys())
    cong_rows = {r[0]: float(r[1] or 0) for r in query_all("""
        SELECT u.username,
               SUM(CASE
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 1.0
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 0.5
                   ELSE 0 END)
        FROM cc_attendance a JOIN users u ON u.id = a.user_id::bigint
        WHERE a.date >= %s AND a.date < %s AND u.username IN %s
          AND a.check_in IS NOT NULL AND a.check_out IS NOT NULL
        GROUP BY 1""", (cfg["gio_chuan"], cfg["gio_nua_cong"], m_start, m_end, unames))}
    names = {r[0]: r[1] for r in query_all(
        "SELECT username, full_name FROM users WHERE username IN %s", (unames,))}
    out = []
    for un, base in cfg["it"].items():
        c = cong_rows.get(un, 0)
        out.append({"username": un, "full_name": names.get(un, un) or un,
                    "base": base, "cong": c,
                    "tong": base * c / cfg["cong_chuan"]})
    return out


def do_chin_don(year: int, month: int) -> dict:
    """Độ chín dữ liệu đơn của kỳ KPI: đơn còn kẹt trạng thái dở dang (shipping/new/
    confirmed) dù đã quá 30 ngày = trạng thái chưa được quét lại → DT/hoàn chưa chín.
    Hiện trên bảng lương để kế toán biết data đủ tin chưa trước khi chốt."""
    from db import query_all
    if month == 1:
        k_start, k_end = _month_bounds(year - 1, 12)
    else:
        k_start, k_end = _month_bounds(year, month - 1)
    try:
        r = query_all("""
            SELECT COUNT(*) FILTER (WHERE order_status IN ('shipping', 'new', 'confirmed')
                                      AND created_at_pos < NOW() - INTERVAL '30 days'),
                   COUNT(*)
            FROM orders
            WHERE created_at_pos >= %s AND created_at_pos < %s""", (k_start, k_end))[0]
        return {"ket": int(r[0] or 0), "tong": int(r[1] or 0)}
    except Exception:
        logger.exception("salary_2b: lỗi check độ chín đơn")
        return {"ket": 0, "tong": 0}


def find_orphan_shops(year: int, month: int):
    """Shop CÓ ĐƠN trong kỳ KPI nhưng chưa gán team hoặc chưa gán NV nào
    → doanh thu rơi ngoài bảng lương (bài học shop longth 11/06). Cảnh báo trên trang."""
    from db import query_all
    if month == 1:
        k_start, k_end = _month_bounds(year - 1, 12)
    else:
        k_start, k_end = _month_bounds(year, month - 1)
    return [
        {"shop_id": r[0], "shop_name": r[1], "orders": int(r[2] or 0),
         "dt": float(r[3] or 0), "thieu_team": r[4], "thieu_nv": r[5]}
        for r in query_all("""
            SELECT s.id, s.shop_name, SUM(m.order_count), SUM(m.gross_revenue),
                   (s.team_id IS NULL) AS thieu_team,
                   NOT EXISTS (SELECT 1 FROM user_shop_assignments usa
                               WHERE usa.shop_id = s.id
                                 AND usa.assigned_from < %s
                                 AND (usa.assigned_to IS NULL OR usa.assigned_to >= %s)) AS thieu_nv
            FROM shops s
            JOIN daily_shop_metrics m ON m.shop_id = s.id
            WHERE m.metric_date::date >= %s AND m.metric_date::date < %s
              AND (s.team_id IS NULL
                   OR NOT EXISTS (SELECT 1 FROM user_shop_assignments usa
                                  WHERE usa.shop_id = s.id
                                    AND usa.assigned_from < %s
                                    AND (usa.assigned_to IS NULL OR usa.assigned_to >= %s)))
            GROUP BY s.id, s.shop_name, s.team_id
            HAVING SUM(m.order_count) > 0
            ORDER BY SUM(m.gross_revenue) DESC""", (k_end, k_start, k_start, k_end, k_end, k_start))
    ]


def compute_sale(year: int, month: int, cfg: dict):
    """Lương sale tháng (year, month) — lương GIỜ từ chấm công (cc_attendance_sessions):
    ngày thường: ≤8,5h = giờ thường × đơn giá, vượt = tăng ca × 35K;
    Chủ nhật × 1,5; ngày lễ × hệ số bảng cc_holidays."""
    from db import query_all

    sc = cfg["sale"]
    m_start, m_end = _month_bounds(year, month)
    holidays = {r[0]: float(r[1] or 2) for r in query_all(
        "SELECT holiday_date, multiplier FROM cc_holidays WHERE holiday_date >= %s AND holiday_date < %s",
        (m_start, m_end))}

    rows = query_all("""
        SELECT u.username, u.full_name, u.role, p.hire_date, s.date,
               SUM(EXTRACT(EPOCH FROM (s.check_out - s.check_in))/3600.0) AS h
        FROM cc_attendance_sessions s
        JOIN users u ON u.id = s.user_id::bigint
        LEFT JOIN hr_profiles p ON p.user_id = u.id
        WHERE u.role IN ('sale', 'sale_leader')
          AND s.date >= %s AND s.date < %s
          AND s.check_in IS NOT NULL AND s.check_out IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5""", (m_start, m_end))

    nv: dict[str, dict] = {}
    for username, full_name, role, hd, d, h in rows:
        e = nv.setdefault(username, {
            "username": username, "full_name": full_name or username,
            "is_leader": role == "sale_leader", "hire_date": hd,
            "gio_thuong": 0.0, "gio_ot": 0.0, "gio_x": 0.0, "tien_x": 0.0,
            "cong": 0.0, "cong_sau_moc_pc": 0.0,
        })
        h = float(h or 0)
        rate = sc["rate_per_user"].get(username, sc["rate_default"])
        cong_ngay = 1.0 if h >= cfg["gio_chuan"] else (0.5 if h >= cfg["gio_nua_cong"] else 0.0)
        e["cong"] += cong_ngay
        if d in holidays:
            e["gio_x"] += h
            e["tien_x"] += h * rate * holidays[d]
        elif d.weekday() == 6:  # Chủ nhật
            e["gio_x"] += h
            e["tien_x"] += h * rate * sc["he_so_cn"]
        else:
            e["gio_thuong"] += min(h, cfg["gio_chuan"])
            e["gio_ot"] += max(h - cfg["gio_chuan"], 0)
        # phụ cấp: chỉ tính công từ ngày đủ 3 tháng (prorate như kế toán)
        if hd:
            moc = date(hd.year + (1 if hd.month > 9 else 0), (hd.month + 3 - 1) % 12 + 1, min(hd.day, 28))
            if d >= moc:
                e["cong_sau_moc_pc"] += cong_ngay
        else:
            e["cong_sau_moc_pc"] += cong_ngay

    out = []
    for e in nv.values():
        rate = sc["rate_per_user"].get(e["username"], sc["rate_default"])
        e["rate"] = rate
        e["luong_gio"] = e["gio_thuong"] * rate + e["gio_ot"] * sc["ot_rate"] + e["tien_x"]
        e["trach_nhiem"] = sc["trach_nhiem"].get(e["username"], 0)
        e["phu_cap"] = sc["phu_cap_thang"] / 30.0 * e["cong_sau_moc_pc"]
        e["chuyen_can"] = 0
        for nguong, tien in sorted(sc["chuyen_can"], key=lambda x: -x[0]):
            if e["cong"] >= nguong:
                e["chuyen_can"] = tien
                break
        e["tong"] = e["luong_gio"] + e["trach_nhiem"] + e["phu_cap"] + e["chuyen_can"]
        out.append(e)
    out.sort(key=lambda x: -x["tong"])
    return out


def _gio_theo_ngay(year: int, month: int, roles: tuple):
    """Giờ làm per (username, ngày) từ cc_attendance_sessions cho các role chỉ định."""
    from db import query_all
    m_start, m_end = _month_bounds(year, month)
    return query_all("""
        SELECT u.username, u.full_name, u.role, p.hire_date, s.date,
               SUM(EXTRACT(EPOCH FROM (s.check_out - s.check_in))/3600.0) AS h
        FROM cc_attendance_sessions s
        JOIN users u ON u.id = s.user_id::bigint
        LEFT JOIN hr_profiles p ON p.user_id = u.id
        WHERE u.role IN %s
          AND s.date >= %s AND s.date < %s
          AND s.check_in IS NOT NULL AND s.check_out IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5""", (roles, m_start, m_end))


def compute_kho(year: int, month: int, cfg: dict):
    """Lương kho: giờ thường (≤8h/ngày) × 30K + (vượt 8h + CN + lễ) × 30K×1,5.
    quangkho: lương ngày 425K × công. Phụ cấp 30K/công. Chuyên cần như sale."""
    from db import query_all
    kc = cfg["kho"]
    m_start, m_end = _month_bounds(year, month)
    holidays = {r[0] for r in query_all(
        "SELECT holiday_date FROM cc_holidays WHERE holiday_date >= %s AND holiday_date < %s",
        (m_start, m_end))}

    hc_users = set((cfg.get("hanh_chinh") or {}).keys())  # NV hành chính tính bảng riêng
    m_start, _ = _month_bounds(year, month)
    nv: dict[str, dict] = {}
    for username, full_name, role, hd, d, h in _gio_theo_ngay(year, month, ("kho",)):
        if username in hc_users:
            continue
        e = nv.setdefault(username, {
            "username": username, "full_name": full_name or username,
            "gio_thuong": 0.0, "gio_ot": 0.0, "cong": 0.0,
            "thu_viec": _la_thu_viec(hd, m_start),
        })
        h = float(h or 0)
        e["cong"] += 1.0 if h >= cfg["gio_chuan"] else (0.5 if h >= cfg["gio_nua_cong"] else 0.0)
        if d in holidays or d.weekday() == 6:
            e["gio_ot"] += h
        else:
            e["gio_thuong"] += min(h, kc["cap_gio"])
            e["gio_ot"] += max(h - kc["cap_gio"], 0)

    out = []
    for e in nv.values():
        luong_thang = kc["luong_thang_rieng"].get(e["username"])
        if luong_thang:
            # quản lý lương tháng: chia 30 ngày (kho làm cả CN), thử việc ×85%
            base = luong_thang * (cfg["ty_le_thu_viec"] if e["thu_viec"] else 1.0)
            e["luong_gio"] = base / kc["ngay_chia_luong_thang"] * e["cong"]
            e["ghi_chu"] = f"lương tháng {luong_thang/1e6:,.0f}tr/{kc['ngay_chia_luong_thang']:.0f} ngày"
        else:
            e["luong_gio"] = (e["gio_thuong"] * kc["rate_default"]
                              + e["gio_ot"] * kc["rate_default"] * kc["he_so_ot"])
            e["ghi_chu"] = ""
        e["trach_nhiem"] = kc["trach_nhiem"].get(e["username"], 0)
        e["phu_cap"] = kc["phu_cap_ngay"] * e["cong"]
        e["chuyen_can"] = 0
        for nguong, tien in sorted(kc["chuyen_can"], key=lambda x: -x[0]):
            if e["cong"] >= nguong:
                e["chuyen_can"] = tien
                break
        e["tong"] = e["luong_gio"] + e["trach_nhiem"] + e["phu_cap"] + e["chuyen_can"]
        out.append(e)
    out.sort(key=lambda x: -x["tong"])
    return out


def compute_hanh_chinh(year: int, month: int, cfg: dict):
    """Hành chính (mua bán/kế toán): lương tháng × công/26 (thử việc ×85%)
    + phụ cấp 30K/công (sau 3 tháng) − bảo hiểm."""
    from db import query_all
    hc = cfg.get("hanh_chinh") or {}
    if not hc:
        return []
    m_start, m_end = _month_bounds(year, month)
    unames = tuple(hc.keys())
    rows = query_all("""
        SELECT u.username, u.full_name, p.hire_date,
               SUM(CASE
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 1.0
                   WHEN EXTRACT(EPOCH FROM (a.check_out - a.check_in))/3600.0 >= %s THEN 0.5
                   ELSE 0 END)
        FROM cc_attendance a
        JOIN users u ON u.id = a.user_id::bigint
        LEFT JOIN hr_profiles p ON p.user_id = u.id
        WHERE u.username IN %s AND a.date >= %s AND a.date < %s
          AND a.check_in IS NOT NULL AND a.check_out IS NOT NULL
        GROUP BY 1, 2, 3""", (cfg["gio_chuan"], cfg["gio_nua_cong"], unames, m_start, m_end))
    info = {r[0]: r for r in rows}
    out = []
    for un, c in hc.items():
        r = info.get(un)
        full_name, hd, cong = (r[1], r[2], float(r[3] or 0)) if r else (un, None, 0.0)
        thu_viec = _la_thu_viec(hd, m_start)
        du_3_thang = (not hd) or ((year * 12 + month) - (hd.year * 12 + hd.month) >= 3)
        luong = c["luong_thang"] * (cfg["ty_le_thu_viec"] if thu_viec else 1.0) * cong / cfg["cong_chuan"]
        phu_cap = cfg["hc_phu_cap_ngay"] * cong if (du_3_thang and not thu_viec) else 0
        out.append({
            "username": un, "full_name": full_name or un,
            "luong_thang": c["luong_thang"], "cong": cong,
            "thu_viec": thu_viec, "luong": luong, "phu_cap": phu_cap,
            "bao_hiem": c["bao_hiem"],
            "tong": luong + phu_cap - c["bao_hiem"],
        })
    out.sort(key=lambda x: -x["tong"])
    return out


@salary_2b_bp.route("/")
@_require_full_view
def index():
    try:
        ym = request.args.get("month", "")
        year, month = (int(x) for x in ym.split("-")) if ym else (2026, 5)
    except Exception:
        year, month = 2026, 5
    cfg = get_config()
    rows, totals, warn_no_cong, (k_start, m_start) = compute_salary_2b(year, month, cfg)
    leaders, _kpi_ym = compute_leaders(year, month, cfg)
    it_rows = compute_it(year, month, cfg)
    sale_rows = compute_sale(year, month, cfg)
    kho_rows = compute_kho(year, month, cfg)
    hc_rows = compute_hanh_chinh(year, month, cfg)
    totals["leader_tong"] = sum(r["tong"] for r in leaders)
    totals["it_tong"] = sum(r["tong"] for r in it_rows)
    totals["sale_tong"] = sum(r["tong"] for r in sale_rows)
    totals["kho_tong"] = sum(r["tong"] for r in kho_rows)
    totals["hc_tong"] = sum(r["tong"] for r in hc_rows)
    totals["tat_ca"] = (totals["tong"] + totals["leader_tong"] + totals["it_tong"]
                        + totals["sale_tong"] + totals["kho_tong"] + totals["hc_tong"])
    # Thưởng / phạt / ứng (kế toán nhập) → thực lĩnh per NV
    adj = get_adjustments(year, month)
    all_nv = []
    for nhom, lst in (("Marketing", rows), ("Leader", leaders), ("Sale", sale_rows),
                      ("Kho", kho_rows), ("Hành chính", hc_rows), ("IT", it_rows)):
        for r in lst:
            a = adj.get(r["username"], {})
            thuong, phat, ung = a.get("thuong", 0), a.get("phat", 0), a.get("ung", 0)
            all_nv.append({
                "username": r["username"], "full_name": r["full_name"], "nhom": nhom,
                "tong": r["tong"], "thuong": thuong, "phat": phat, "ung": ung,
                "thuc_linh": r["tong"] + thuong - phat - ung,
            })
    totals["thuong"] = sum(x["thuong"] for x in all_nv)
    totals["phat"] = sum(x["phat"] for x in all_nv)
    totals["ung"] = sum(x["ung"] for x in all_nv)
    totals["thuc_linh"] = sum(x["thuc_linh"] for x in all_nv)
    return render_template(
        "salary_2b/index.html",
        rows=rows, totals=totals, warn_no_cong=warn_no_cong,
        leaders=leaders, it_rows=it_rows, sale_rows=sale_rows,
        kho_rows=kho_rows, hc_rows=hc_rows, all_nv=all_nv,
        orphan_shops=find_orphan_shops(year, month),
        do_chin=do_chin_don(year, month),
        year=year, month=month, kpi_month=k_start.month, kpi_year=k_start.year,
        config=cfg, can_edit=session.get("role") in _CONFIG_EDIT_ROLES,
    )


def _adj_key(year: int, month: int) -> str:
    return f"salary_2b_adj_{year}_{month:02d}"


def get_adjustments(year: int, month: int) -> dict:
    """Thưởng / phạt / ứng per NV per kỳ — kế toán nhập tay, lưu app_config."""
    from db import query_all
    try:
        rows = query_all("SELECT value FROM app_config WHERE key = %s", (_adj_key(year, month),))
        if rows and rows[0][0]:
            return {str(u): {"thuong": float(d.get("thuong", 0)), "phat": float(d.get("phat", 0)),
                             "ung": float(d.get("ung", 0))}
                    for u, d in json.loads(rows[0][0]).items() if isinstance(d, dict)}
    except Exception:
        logger.exception("salary_2b: lỗi đọc adjustments")
    return {}


@salary_2b_bp.route("/adj", methods=["POST"])
@_require_full_view
def save_adjustments():
    """Kế toán lưu thưởng/phạt/ứng. Form field: adj_<loại>_<username>."""
    if session.get("role") not in (_CONFIG_EDIT_ROLES | {"ketoan", "accountant"}):
        return "Không có quyền", 403
    try:
        ky, km = (int(x) for x in request.form["adj_ym"].split("-"))
        values: dict = {}
        for k, v in request.form.items():
            if not k.startswith("adj_") or k == "adj_ym":
                continue
            _, loai, username = k.split("_", 2)
            if loai not in ("thuong", "phat", "ung"):
                continue
            so = "".join(ch for ch in (v or "") if ch.isdigit())
            if so and float(so) > 0:
                values.setdefault(username, {})[loai] = float(so)
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (_adj_key(ky, km), json.dumps(values)))
            conn.commit()
        flash("✅ Đã lưu thưởng / phạt / ứng")
    except (ValueError, KeyError) as exc:
        flash(f"❌ Dữ liệu không hợp lệ: {exc}")
    return redirect(request.referrer or "/salary-2b/")


@salary_2b_bp.route("/huong-dan")
@_require_full_view
def huong_dan():
    """Tài liệu vận hành + bàn giao cho IT/kế toán — render từ LUONG_2B_HUONG_DAN.md."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "LUONG_2B_HUONG_DAN.md")
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read()
    except OSError:
        md = "# Không tìm thấy LUONG_2B_HUONG_DAN.md"
    return render_template("salary_2b/huong_dan.html", md_content=md)


@salary_2b_bp.route("/cp-test", methods=["POST"])
@_require_full_view
def save_cp_test():
    """Kế toán nhập CP test hàng per team cho kỳ KPI — lưu app_config."""
    if session.get("role") not in (_CONFIG_EDIT_ROLES | {"ketoan", "accountant"}):
        return "Không có quyền", 403
    try:
        ky, km = (int(x) for x in request.form["kpi_ym"].split("-"))
        values = {}
        for k, v in request.form.items():
            if k.startswith("cptest_") and (v or "").strip():
                # input có dấu chấm phân tách nghìn (10.000.000) → bỏ hết ký tự không phải số
                so = "".join(ch for ch in v if ch.isdigit())
                if so:
                    values[int(k[7:])] = float(so)
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (_cptest_key(ky, km), json.dumps(values)))
            conn.commit()
        flash("✅ Đã lưu CP test hàng")
    except (ValueError, KeyError) as exc:
        flash(f"❌ Dữ liệu không hợp lệ: {exc}")
    return redirect(request.referrer or "/salary-2b/")


@salary_2b_bp.route("/config", methods=["GET", "POST"])
@_require_full_view
def config_page():
    can_edit = session.get("role") in _CONFIG_EDIT_ROLES
    if request.method == "POST":
        if not can_edit:
            return "Chỉ admin/manager được sửa cấu hình lương", 403
        try:
            f = request.form
            tiers = []
            for mn, mx, com in zip(f.getlist("tier_min"), f.getlist("tier_max"),
                                   f.getlist("tier_pct")):
                mn = (mn or "").strip()
                if mn == "":
                    continue
                mx = (mx or "").strip()
                tiers.append((float(mn), None if mx == "" else float(mx), float(com or 0)))
            tiers.sort(key=lambda t: t[0])
            if not tiers:
                raise ValueError("Phải có ít nhất 1 bậc")
            def _so(name, default=0):
                raw = "".join(ch for ch in str(f.get(name, "")) if ch.isdigit() or ch == ".")
                try:
                    return float(raw) if raw else float(default)
                except ValueError:
                    return float(default)

            def _user_dict(prefix):
                d = {}
                for un, val in zip(f.getlist(prefix + "_user"), f.getlist(prefix + "_val")):
                    un = (un or "").strip()
                    so = "".join(ch for ch in str(val) if ch.isdigit())
                    if un and so:
                        d[un] = float(so)
                return d

            def _chuyen_can(prefix):
                cc = []
                for cong, tien in zip(f.getlist(prefix + "_cong"), f.getlist(prefix + "_tien")):
                    so = "".join(ch for ch in str(tien) if ch.isdigit())
                    if (cong or "").strip() and so:
                        cc.append([float(cong), float(so)])
                return sorted(cc, key=lambda x: -x[0]) or [[30, 500000], [26, 200000]]

            hanh_chinh = {}
            for un, lt, bh in zip(f.getlist("hc_user"), f.getlist("hc_luong"), f.getlist("hc_bh")):
                un = (un or "").strip()
                lt_so = "".join(ch for ch in str(lt) if ch.isdigit())
                bh_so = "".join(ch for ch in str(bh) if ch.isdigit())
                if un and lt_so:
                    hanh_chinh[un] = {"luong_thang": float(lt_so), "bao_hiem": float(bh_so or 0)}

            cfg = {
                "vat_rate": _so("vat_rate", 8) / 100.0,
                "gio_chuan": _so("gio_chuan", 8.5),
                "gio_nua_cong": _so("gio_nua_cong", 4.25),
                "cong_chuan": _so("cong_chuan", 26),
                "ty_le_thu_viec": _so("ty_le_thu_viec", 85) / 100.0,
                "luong_vung": {
                    "HCM": (_so("hcm_base", 5200000), _so("hcm_pc", 800000)),
                    "DN": (_so("dn_base", 4000000), _so("dn_pc", 700000)),
                },
                "tiers": tiers,
                "leader": {
                    "luong_cung": _so("ld_luong_cung", 15000000),
                    "pct": _so("ld_pct", 3),
                    "cp_chung_per_don": _so("ld_cp_chung", 8242),
                    "thue_tndn": _so("ld_thue_tndn", 20),
                    "thue_gtgt_hkd": _so("ld_gtgt", 1),
                },
                "sale": {
                    "rate_default": _so("sale_rate", 27000),
                    "rate_per_user": _user_dict("sale_rpu"),
                    "ot_rate": _so("sale_ot", 35000),
                    "he_so_cn": _so("sale_cn", 1.5),
                    "trach_nhiem": _user_dict("sale_tn"),
                    "phu_cap_thang": _so("sale_pc", 800000),
                    "chuyen_can": _chuyen_can("sale_cc"),
                },
                "kho": {
                    "rate_default": _so("kho_rate", 30000),
                    "cap_gio": _so("kho_cap", 8),
                    "he_so_ot": _so("kho_ot", 1.5),
                    "phu_cap_ngay": _so("kho_pc", 30000),
                    "ngay_chia_luong_thang": _so("kho_chia", 30),
                    "trach_nhiem": _user_dict("kho_tn"),
                    "luong_thang_rieng": _user_dict("kho_lt"),
                    "chuyen_can": _chuyen_can("kho_cc"),
                },
                "hanh_chinh": hanh_chinh,
                "hc_phu_cap_ngay": _so("hc_pc", 30000),
                "it": _user_dict("it"),
            }
            save_config(cfg)
            flash("✅ Đã lưu cấu hình lương 2B")
            return redirect("/salary-2b/config")
        except (ValueError, TypeError) as exc:
            flash(f"❌ Dữ liệu không hợp lệ: {exc}")
            return redirect("/salary-2b/config")
    from db import query_all
    users = [{"username": r[0], "full_name": r[1] or r[0]} for r in query_all(
        "SELECT username, full_name FROM users WHERE status = 'active' ORDER BY full_name")]
    return render_template("salary_2b/config.html", config=get_config(),
                           can_edit=can_edit, users=users)


def register_salary_2b_module(app):
    app.register_blueprint(salary_2b_bp)
    logger.info("salary_2b module registered at /salary-2b/")
