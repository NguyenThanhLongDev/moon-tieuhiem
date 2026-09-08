# -*- coding: utf-8 -*-
"""Lan báo cáo Ads (Lãi/Lỗ) qua Zalo — theo yêu cầu sếp Phong 11/08.

Dùng chung cho: gửi tay (test), cron sáng/tối, gửi nhóm (lan_ads_report_threads)
và gửi riêng từng NV (users.zalo_uid).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("lan_ads_report")


def _m(v) -> str:
    """1234567 → '1,23tr' kiểu gọn; giữ dấu cho số âm."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    neg = v < 0
    a = abs(v)
    if a >= 1_000_000:
        s = f"{a/1_000_000:,.1f}".rstrip("0").rstrip(".") + "tr"
    elif a >= 1_000:
        s = f"{a/1_000:,.0f}k"
    else:
        s = f"{a:,.0f}đ"
    return ("-" if neg else "") + s


def _page_owner(date_from: str, date_to: str, lookback_days: int = 14) -> dict:
    """{page_id: {"nv": tên NV, "team": tên team}} — theo TK QC chi nhiều nhất cho page.

    Nới cửa sổ về trước `lookback_days` ngày: page hôm nay KHÔNG chạy ads nhưng
    vẫn ra đơn (đơn về sau) thì vẫn biết ai từng chạy — tránh ghi "chưa gán NV".
    """
    out = {}
    try:
        import datetime as _dt
        _from = (_dt.datetime.strptime(date_from, "%Y-%m-%d")
                 - _dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH sp AS (
                    SELECT page_id, REPLACE(fb_ad_account_id,'act_','') AS acct, SUM(s) AS spend
                      FROM (SELECT page_id, fb_ad_account_id, metric_date, MAX(spend) AS s
                              FROM fb_ads_page_daily_spend
                             WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_id,'') <> ''
                             GROUP BY page_id, fb_ad_account_id, metric_date) t
                     GROUP BY page_id, REPLACE(fb_ad_account_id,'act_','')
                ),
                rk AS (
                    SELECT page_id, acct,
                           ROW_NUMBER() OVER (PARTITION BY page_id ORDER BY spend DESC) AS rn
                      FROM sp
                )
                SELECT rk.page_id, u.id, COALESCE(NULLIF(u.full_name,''), u.username),
                       COALESCE(t.team_name,'')
                  FROM rk
                  JOIN user_ad_account_assignments m
                    ON REPLACE(m.ad_account_id,'act_','') = rk.acct AND m.assigned_to IS NULL
                  JOIN users u ON u.id = m.user_id
                  LEFT JOIN teams t ON t.id = u.team_id
                 WHERE rk.rn = 1
            """, (_from, date_to))
            for pid, uid, nv, team in cur.fetchall():
                out[str(pid)] = {"nv_id": str(uid), "nv": nv, "team": team or ""}
            # Override thủ công (ưu tiên CAO NHẤT) — cho page ads chưa map được
            cur.execute("""
                SELECT o.page_id, u.id, COALESCE(NULLIF(u.full_name,''), u.username),
                       COALESCE(t.team_name,'')
                  FROM page_owner_override o
                  JOIN users u ON u.id = o.user_id
                  LEFT JOIN teams t ON t.id = u.team_id
            """)
            for pid, uid, nv, team in cur.fetchall():
                out[str(pid)] = {"nv_id": str(uid), "nv": nv, "team": team or ""}
    except Exception as exc:
        logger.warning("_page_owner error: %s", exc)
    return out


def _test_stats(date_from: str, date_to: str) -> dict:
    """Thống kê page TEST (đang chạy ads nhưng CHƯA có doanh thu POS) trong kỳ.

    Đơn của page test lấy từ Meta (đăng ký form ladipage / lượt mua) — giống
    cách trang Chi phí QC đếm, vì page test chưa lên POS nên không có đơn chốt.
    """
    out = {"so_page": 0, "tien": 0.0, "don": 0, "co_don": [], "chua_don": []}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH spend AS (
                    SELECT page_id, SUM(sp) AS spend FROM (
                        SELECT page_id, metric_date, fb_ad_account_id, MAX(spend) AS sp
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_id,'') <> ''
                         GROUP BY page_id, metric_date, fb_ad_account_id
                    ) t GROUP BY page_id
                ),
                pos AS (
                    SELECT page_id, SUM(revenue) AS rev, SUM(success_order_count) AS chot
                      FROM pos_page_daily_metrics
                     WHERE metric_date BETWEEN %s AND %s GROUP BY page_id
                ),
                meta AS (
                    SELECT page_id,
                           GREATEST(COALESCE(SUM(registrations),0), COALESCE(SUM(purchases),0)) AS don
                      FROM mb_fb_entity_daily
                     WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_id,'') <> ''
                     GROUP BY page_id
                )
                SELECT s.page_id, s.spend, COALESCE(m.don, 0),
                       COALESCE(NULLIF(n.name,''), NULLIF(sn.nm,''), s.page_id)
                  FROM spend s
                  LEFT JOIN pos p  ON p.page_id = s.page_id
                  LEFT JOIN meta m ON m.page_id = s.page_id
                  LEFT JOIN fb_page_names n ON n.page_id = s.page_id
                  LEFT JOIN (SELECT page_id, MAX(page_name) AS nm
                               FROM fb_ads_page_daily_spend
                              WHERE metric_date BETWEEN %s AND %s
                                AND COALESCE(page_name,'') <> ''
                              GROUP BY page_id) sn ON sn.page_id = s.page_id
                 WHERE s.spend > 0 AND COALESCE(p.rev, 0) <= 0
                 ORDER BY s.spend DESC
            """, (date_from, date_to) * 4)
            for pid, spend, don, name in cur.fetchall():
                spend = float(spend or 0)
                don = int(don or 0)
                out["so_page"] += 1
                out["tien"] += spend
                out["don"] += don
                row = {"name": (name or pid), "spend": spend, "don": don, "page_id": str(pid)}
                (out["co_don"] if don > 0 else out["chua_don"]).append(row)
        out["co_don"].sort(key=lambda x: -x["don"])
        out["chua_don"].sort(key=lambda x: -x["spend"])
    except Exception as exc:
        logger.warning("_test_stats error: %s", exc)
    return out


def _meta_orders(date_from: str, date_to: str) -> dict:
    """{page_id: số đơn Meta} — dùng cho page TEST (chưa lên POS nên POS đếm 0 đơn).

    Không có cái này thì mọi page test đều bị coi là "đốt ads 0 đơn" — sai, vì
    đơn của chúng nằm ở form đăng ký / lượt mua bên Meta.
    """
    out = {}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT page_id,
                       GREATEST(COALESCE(SUM(registrations),0), COALESCE(SUM(purchases),0))
                  FROM mb_fb_entity_daily
                 WHERE metric_date BETWEEN %s AND %s AND COALESCE(page_id,'') <> ''
                 GROUP BY page_id""", (date_from, date_to))
            for pid, don in cur.fetchall():
                out[str(pid)] = int(don or 0)
    except Exception as exc:
        logger.warning("_meta_orders error: %s", exc)
    return out


def _who(owner: dict, page_id) -> str:
    """' — hieu · Team Hiếu'. Page không ai chạy ads → '(bán tự nhiên)'."""
    o = owner.get(str(page_id or "")) or {}
    nv = o.get("nv") or ""
    if not nv:
        return " — (bán tự nhiên, không chạy ads)"
    return f" — {nv}" + (f" · {o['team']}" if o.get("team") else "")


def build_report(date_from: str, date_to: str, nv_filter: str = "") -> str:
    """Soạn text báo cáo Lãi/Lỗ cho khoảng ngày. nv_filter=user_id → chỉ phần NV đó."""
    from modules.chi_phi_qc import _lai_lo_report
    data = _lai_lo_report(date_from, date_to, nv_f=nv_filter)
    pages = data.get("by_page") or []
    nvs = data.get("by_nv") or []

    tong_rev = sum(r["rev"] for r in pages)
    tong_ll = sum(r["lai_lo"] for r in pages)
    tong_cpqc = sum(r["cpqc"] for r in pages)
    tong_test = sum(r["cp_test"] for r in pages)
    tong_don = sum(r["don"] for r in pages)
    tong_gv = sum(r.get("gia_von", 0) for r in pages)
    tong_khac = sum(r.get("cp_khac", 0) for r in pages)
    tong_ads = tong_cpqc + tong_test          # ngân sách ads đã tiêu trong ngày
    tong_hoan = sum(r.get("hoan", 0) for r in pages)

    ky = date_from if date_from == date_to else f"{date_from} → {date_to}"
    lines = [
        f"🌸 LAN BÁO CÁO ADS — {ky}",
        "",
        "①  TỔNG QUAN NGÀY",
        f"💰 Doanh thu: {_m(tong_rev)}  ·  🧾 {int(tong_don)} đơn"
        + (f" (hoàn {int(tong_hoan)})" if tong_hoan else ""),
        f"📦 Giá vốn hàng: {_m(tong_gv)}",
        f"💸 NGÂN SÁCH ADS ĐÃ TIÊU: {_m(tong_ads)}",
        f"    ├ Page đang bán: {_m(tong_cpqc)}",
        f"    └ Test SP mới: {_m(tong_test)}",
        f"🏆 LÃI/LỖ THỰC: {_m(tong_ll)}",
        "─" * 22,
        "②  LÃI/LỖ TỪNG PAGE (đã bán trên POS)",
    ]

    # PAGE BÁN THẬT (đã có trên POS, có doanh thu) — sếp Phong: ưu tiên báo trước.
    # Page test (chưa lên POS/chưa có DT) tách riêng ở khối 🧪 bên dưới.
    owner = _page_owner(date_from, date_to) if not nv_filter else {}
    ban_that = [r for r in pages if r["rev"] > 0]

    lo = [r for r in ban_that if r["lai_lo"] < 0]
    lo.sort(key=lambda x: x["lai_lo"])
    if lo:
        lines.append(f"🔴 PAGE LỖ — đã bán trên POS ({len(lo)} page):")
        for r in lo[:5]:
            lines.append(f"  • {r.get('name') or r.get('key')}: {_m(r['lai_lo'])}"
                         f" (DT {_m(r['rev'])} · {int(r['don'])} đơn · ads {_m(r['cpqc'])})"
                         f"{_who(owner, r.get('key'))}")
        if len(lo) > 5:
            lines.append(f"  … +{len(lo)-5} page lỗ khác (xem web)")
    else:
        lines.append("🟢 Không page bán nào bị lỗ 🎉")

    lai = [r for r in ban_that if r["lai_lo"] > 0]
    lai.sort(key=lambda x: -x["lai_lo"])
    if lai:
        lines.append("")
        lines.append(f"🏆 PAGE LÃI — đã bán trên POS ({len(lai)} page):")
        for r in lai[:10]:
            lines.append(f"  • {r.get('name') or r.get('key')}: +{_m(r['lai_lo'])}"
                         f" (DT {_m(r['rev'])} · {int(r['don'])} đơn)"
                         f"{_who(owner, r.get('key'))}")
        if len(lai) > 10:
            lines.append(f"  … +{len(lai)-10} page lãi khác")

    # ── Page TEST trong ngày (sếp Phong 11/08) ──
    if not nv_filter:
        ts = _test_stats(date_from, date_to)
        if ts["so_page"]:
            gia_don = (ts["tien"] / ts["don"]) if ts["don"] else 0
            lines.append("")
            lines.append("─" * 22)
            lines.append("③  TEST SẢN PHẨM MỚI")
            lines.append(f"🧪 {ts['so_page']} page/SP · đốt {_m(ts['tien'])} · ra {ts['don']} đơn"
                         + (f" (~{_m(gia_don)}/đơn)" if ts["don"] else ""))
            if ts["co_don"]:
                lines.append(f"  ✅ Test CÓ đơn ({len(ts['co_don'])}):")
                for r in ts["co_don"][:5]:
                    lines.append(f"    • {r['name']}: {r['don']} đơn · {_m(r['spend'])}"
                                 f"{_who(owner, r.get('page_id'))}")
            if ts["chua_don"]:
                lines.append(f"  ❌ Test CHƯA ra đơn ({len(ts['chua_don'])}) — đốt nhiều nhất:")
                for r in ts["chua_don"][:5]:
                    lines.append(f"    • {r['name']}: {_m(r['spend'])}"
                                 f"{_who(owner, r.get('page_id'))}")
            # Test theo NHÂN VIÊN: ai test mấy SP, ra mấy đơn
            per_nv = {}
            for r in ts["co_don"] + ts["chua_don"]:
                _o = owner.get(str(r.get("page_id") or "")) or {}
                nv = _o.get("nv") or "(không rõ NV)"
                if _o.get("team"):
                    nv = f"{nv} · {_o['team']}"
                d = per_nv.setdefault(nv, {"sp": 0, "don": 0, "tien": 0.0})
                d["sp"] += 1
                d["don"] += r["don"]
                d["tien"] += r["spend"]
            if per_nv:
                lines.append("  👥 Test theo NV (SP · đơn · tiền):")
                for nv, d in sorted(per_nv.items(), key=lambda x: -x[1]["sp"])[:8]:
                    hieu = " ⚠️ chưa ra đơn" if d["don"] == 0 else ""
                    lines.append(f"    • {nv}: {d['sp']} SP · {d['don']} đơn · {_m(d['tien'])}{hieu}")

    if not nv_filter and nvs:
        lines.append("")
        lines.append("─" * 22)
        lines.append("④  THEO NHÂN VIÊN")
        nvs2 = sorted(nvs, key=lambda x: -x["lai_lo"])
        top = [r for r in nvs2 if r["lai_lo"] > 0 and "(không chạy ads)" not in str(r.get("name") or "")]
        if top:
            lines.append("🥇 LÃI CAO NHẤT:")
            for i, r in enumerate(top[:3], 1):
                medal = ["🥇", "🥈", "🥉"][i - 1]
                lines.append(f"  {medal} {r.get('name') or r.get('key')}"
                             f"{(' · ' + r['team']) if r.get('team') else ''}: +{_m(r['lai_lo'])}"
                             f" (DT {_m(r['rev'])} · {int(r['don'])} đơn)")
        am = [r for r in nvs2 if r["lai_lo"] < 0]
        if am:
            lines.append(f"⚠️ NV ĐANG LỖ ({len(am)}) — page lỗ của từng bạn:")
            for r in am:
                nv_name = r.get("name") or r.get("key")
                lines.append(f"  ⚠️ {nv_name}{(' · ' + r['team']) if r.get('team') else ''}:"
                             f" {_m(r['lai_lo'])}")
                pl = [p for p in pages
                      if p["lai_lo"] < 0
                      and (owner.get(str(p.get("key"))) or {}).get("nv") == nv_name]
                pl.sort(key=lambda x: x["lai_lo"])
                for p in pl[:3]:
                    tag = "bán POS" if p["rev"] > 0 else "test chưa ra đơn"
                    lines.append(f"      ↳ {p.get('name') or p.get('key')}: {_m(p['lai_lo'])} ({tag})")
        con = [r for r in nvs2 if r["lai_lo"] >= 0][3:]
        if con:
            lines.append("✅ NV khác (lãi): "
                         + " · ".join(f"{r.get('name') or r.get('key')} +{_m(r['lai_lo'])}"
                                      for r in con[:6]))

    lines.append("")
    lines.append("💡 Page test >2-3 ngày chưa ra đơn → cân nhắc tắt/đổi mẫu.")
    lines.append("📊 Chi tiết: moon.tieuhiem.com/chi-phi-qc/lai-lo")
    return "\n".join(lines)


def _smart_provider() -> dict:
    """Provider AI cho phần phân tích — đọc Cài đặt → AI Models (app_config).

    ai_chat_base_url + ai_chat_model + ai_chat_api_key (proxy GPT của sếp).
    Thiếu ô nào → trả {} (bỏ qua khối phân tích, KHÔNG làm vỡ báo cáo).
    """
    try:
        from app_ctx import load_config
        cfg = load_config() or {}
        base = str(cfg.get("ai_chat_base_url") or "").strip().rstrip("/")
        model = str(cfg.get("ai_chat_model") or "").strip()
        key = str(cfg.get("ai_chat_api_key") or "").strip()
        if base and model and key:
            return {"url": base + "/chat/completions", "model": model, "key": key}
    except Exception as exc:
        logger.warning("_smart_provider error: %s", exc)
    return {}


def build_ai_analysis(report_text: str, timeout: int = 90) -> str:
    """Khối '🧠 PHÂN TÍCH & LỜI KHUYÊN' — đưa số cho GPT tự nhận xét.

    Trả "" nếu chưa cấu hình AI hoặc gọi lỗi → báo cáo vẫn gửi bình thường.
    """
    prov = _smart_provider()
    if not prov:
        return ""
    import requests
    system = (
        "Bạn là chuyên gia quảng cáo Facebook cho shop thời trang Việt Nam, đang "
        "làm trợ lý cho sếp và đội marketing (nhiều bạn MỚI, chưa có kinh nghiệm). "
        "Đọc báo cáo lãi/lỗ ads trong ngày rồi đưa nhận xét NGẮN, CỤ THỂ, DỄ HIỂU."
    )
    user = (
        f"{report_text}\n\n"
        "Hãy viết phần phân tích cho sếp, tiếng Việt, KHÔNG markdown, không dùng ** hay #.\n"
        "Bố cục đúng 3 mục, mỗi mục 2-4 gạch đầu dòng ngắn:\n"
        "1) BẤT THƯỜNG HÔM NAY — page/nhân viên nào đốt tiền mà không ra đơn, "
        "tỷ lệ ads/doanh thu quá cao (ngưỡng cảnh báo 33%), test nhiều mà không hiệu quả.\n"
        "2) NÊN LÀM NGAY — tắt/giảm ngân sách page nào, nhân bản page nào đang ngon, "
        "bạn nào cần kèm cặp.\n"
        "3) ĐIỂM TỐT — khen ngắn page/nhân viên hiệu quả nhất.\n"
        "Nêu ĐÍCH DANH tên page và tên nhân viên kèm số liệu. Tối đa 12 dòng."
    )
    try:
        r = requests.post(
            prov["url"],
            headers={"Authorization": "Bearer " + prov["key"],
                     "Content-Type": "application/json"},
            json={"model": prov["model"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": 0.3},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("AI analysis HTTP %s: %s", r.status_code, r.text[:200])
            return ""
        txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        txt = txt.strip()
        if not txt:
            return ""
        return f"🧠 PHÂN TÍCH & LỜI KHUYÊN ({prov['model']})\n\n{txt}"
    except Exception as exc:
        logger.warning("build_ai_analysis error: %s", exc)
        return ""


def _nv_work_stats(user_id: int, day: str) -> dict:
    """Chỉ số làm việc của 1 NV trong NGÀY — theo yêu cầu sếp Phong 12/08.

    Đếm trên các TK QC mà NV đang phụ trách (uaa active):
      camp_moi / camp_chay · so_page · page_test · mau_test
      ngan_sach · ngan_sach_test · don · don_test
    """
    out = {"camp_chay": 0, "camp_moi": 0, "so_page": 0, "page_test": 0,
           "ngan_sach": 0.0, "ngan_sach_test": 0.0, "don_test": 0}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT REPLACE(ad_account_id,'act_','') FROM user_ad_account_assignments
                 WHERE user_id=%s AND assigned_to IS NULL""", (user_id,))
            accts = [r[0] for r in cur.fetchall()]
            if not accts:
                return out
            # campaign chạy hôm đó + campaign LÊN MỚI (lần đầu có chi tiêu)
            cur.execute("""
                SELECT COUNT(DISTINCT campaign_id) FROM mb_fb_entity_daily
                 WHERE metric_date=%s AND REPLACE(account_id,'act_','') = ANY(%s)
                   AND COALESCE(campaign_id,'')<>''""", (day, accts))
            out["camp_chay"] = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT campaign_id, MIN(metric_date) AS mn
                      FROM mb_fb_entity_daily
                     WHERE REPLACE(account_id,'act_','') = ANY(%s) AND COALESCE(campaign_id,'')<>''
                     GROUP BY campaign_id) x
                 WHERE x.mn=%s""", (accts, day))
            out["camp_moi"] = int((cur.fetchone() or [0])[0] or 0)
            # page chạy + page test (chưa có doanh thu POS ngày đó) + ngân sách
            cur.execute("""
                WITH sp AS (
                    SELECT page_id, SUM(s) AS spend FROM (
                        SELECT page_id, metric_date, fb_ad_account_id, MAX(spend) AS s
                          FROM fb_ads_page_daily_spend
                         WHERE metric_date=%s AND REPLACE(fb_ad_account_id,'act_','') = ANY(%s)
                           AND COALESCE(page_id,'')<>''
                         GROUP BY page_id, metric_date, fb_ad_account_id) t
                     GROUP BY page_id),
                pos AS (SELECT page_id, SUM(revenue) AS rev FROM pos_page_daily_metrics
                         WHERE metric_date=%s GROUP BY page_id),
                mt AS (SELECT page_id,
                              GREATEST(COALESCE(SUM(registrations),0),COALESCE(SUM(purchases),0)) AS don
                         FROM mb_fb_entity_daily WHERE metric_date=%s GROUP BY page_id)
                SELECT COUNT(*) FILTER (WHERE sp.spend>0),
                       COUNT(*) FILTER (WHERE sp.spend>0 AND COALESCE(pos.rev,0)<=0),
                       COALESCE(SUM(sp.spend),0),
                       COALESCE(SUM(sp.spend) FILTER (WHERE COALESCE(pos.rev,0)<=0),0),
                       COALESCE(SUM(mt.don) FILTER (WHERE COALESCE(pos.rev,0)<=0),0)
                  FROM sp LEFT JOIN pos ON pos.page_id=sp.page_id
                          LEFT JOIN mt  ON mt.page_id=sp.page_id
            """, (day, accts, day, day))
            r = cur.fetchone() or (0, 0, 0, 0, 0)
            out["so_page"] = int(r[0] or 0)
            out["page_test"] = int(r[1] or 0)
            out["ngan_sach"] = float(r[2] or 0)
            out["ngan_sach_test"] = float(r[3] or 0)
            out["don_test"] = int(r[4] or 0)
    except Exception as exc:
        logger.warning("_nv_work_stats(%s) error: %s", user_id, exc)
    return out


def _nv_page_detail(user_id: int, day: str) -> dict:
    """Bóc CHI TIẾT từng page của 1 NV trong ngày → {"lo": [...], "dot": [...], "lai": [...]}.

    - lo  : page lãi/lỗ < 0 (sắp xếp lỗ nặng trước)
    - dot : page có tiêu ads nhưng KHÔNG ra đơn nào (đốt tiền)
    - lai : page có lãi (sắp xếp lãi cao trước)
    """
    out = {"lo": [], "dot": [], "lai": []}
    try:
        from db import get_conn
        from modules.chi_phi_qc import _lai_lo_report
        with get_conn() as conn, conn.cursor() as cur:
            # PHẢI lấy đúng field _page_owner dùng làm "nv" (full_name, fallback username),
            # không phải username — VD user 'Chienchien' hiển thị là 'chien' → so username là trượt.
            cur.execute("""SELECT COALESCE(NULLIF(full_name,''), username)
                             FROM users WHERE id=%s""", (user_id,))
            row = cur.fetchone()
        uname = (row[0] if row else "") or ""
        if not uname:
            return out
        owner = _page_owner(day, day)
        meta = _meta_orders(day, day)
        for r in (_lai_lo_report(day, day).get("by_page") or []):
            if (owner.get(str(r.get("key"))) or {}).get("nv") != uname:
                continue
            ads = float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0)
            # Page test chưa lên POS → lấy đơn Meta, không thì bị kết oan "0 đơn"
            don = int(float(r.get("don") or 0)) or meta.get(str(r.get("key")), 0)
            item = {"name": r.get("name") or str(r.get("key")), "ads": ads, "don": don,
                    "rev": float(r.get("rev") or 0), "lai_lo": float(r.get("lai_lo") or 0)}
            if ads > 0 and don == 0:
                out["dot"].append(item)
            elif item["lai_lo"] < 0:
                out["lo"].append(item)
            elif item["lai_lo"] > 0:
                out["lai"].append(item)
        out["lo"].sort(key=lambda x: x["lai_lo"])
        out["dot"].sort(key=lambda x: -x["ads"])
        out["lai"].sort(key=lambda x: -x["lai_lo"])
    except Exception as exc:
        logger.warning("_nv_page_detail(%s) error: %s", user_id, exc)
    return out




def _nv_delivery_stats(user_id: int, day: str) -> dict:
    """Tỷ lệ phát / hoàn của NV theo các shop NV được gán (đơn đã xuất kho)."""
    out = {"sent": 0, "received": 0, "returned": 0, "phat": None, "hoan": None}
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(c.sent_orders),0), COALESCE(SUM(c.received_orders),0),
                       COALESCE(SUM(c.returned_orders),0)
                  FROM shop_order_status_cache c
                  JOIN shops s ON s.id = c.shop_id
                  JOIN user_shop_assignments usa ON usa.shop_id = s.id
                       AND %s::date >= usa.assigned_from
                       AND (usa.assigned_to IS NULL OR %s::date <= usa.assigned_to)
                 WHERE c.metric_date = %s::date AND usa.user_id = %s""",
                (day, day, day, user_id))
            r = cur.fetchone() or (0, 0, 0)
            sent, received, returned = int(r[0] or 0), int(r[1] or 0), int(r[2] or 0)
            xuat = sent + received
            out.update(sent=sent, received=received, returned=returned)
            if xuat > 0:
                out["phat"] = received / xuat * 100
                out["hoan"] = returned / xuat * 100
    except Exception as exc:
        logger.warning("_nv_delivery_stats(%s) error: %s", user_id, exc)
    return out

def build_personal_report(user_id: int, name: str, day: str) -> str:
    """Báo cáo RIÊNG cho 1 mar về ngày `day` — đúng chỉ số sếp Phong yêu cầu."""
    from modules.chi_phi_qc import _lai_lo_report
    data = _lai_lo_report(day, day, nv_f=str(user_id))
    rows = data.get("by_nv") or []
    r = rows[0] if rows else {"rev": 0, "lai_lo": 0, "don": 0, "cpqc": 0, "cp_test": 0}
    w = _nv_work_stats(user_id, day)

    don = int(r.get("don") or 0)
    ads_tong = float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0)
    ads_don = (ads_tong / don) if don else 0
    ads_don_test = (w["ngan_sach_test"] / w["don_test"]) if w["don_test"] else 0
    lai = float(r.get("lai_lo") or 0)

    L = [
        f"🌸 BÁO CÁO RIÊNG — {name}",
        f"📅 Ngày {day}",
        "",
        "①  KẾT QUẢ",
        f"{'🟢 LÃI' if lai >= 0 else '🔴 LỖ'}: {_m(lai)}",
        f"💸 Ngân sách ads đã chạy: {_m(ads_tong)}",
        f"🎯 Tiền ads/đơn: {_m(ads_don) if don else '—'}"
        + (f"  ({don} đơn · DT {_m(r.get('rev') or 0)})" if don else "  (chưa có đơn)"),
        "",
        "②  QUÁ TRÌNH LÀM VIỆC",
        f"🚀 Camp lên mới: {w['camp_moi']}  (đang chạy {w['camp_chay']} camp)",
        f"🧪 Mẫu test: {w['page_test']}",
        f"📄 Page chạy: {w['so_page']}  (trong đó {w['page_test']} page test)",
        f"💰 Ngân sách test: {_m(w['ngan_sach_test'])}",
        f"🎯 Ads/đơn của SP test: {_m(ads_don_test) if w['don_test'] else '—'}"
        + (f"  ({w['don_test']} đơn test)" if w["don_test"] else "  (test chưa ra đơn)"),
    ]
    # ── Bóc CỤ THỂ từng page: lỗ / đốt ads không ra đơn / lãi
    d = _nv_page_detail(user_id, day)
    if d["dot"]:
        L += ["", f"③  🔥 ĐỐT ADS — KHÔNG RA ĐƠN ({len(d['dot'])} page)"]
        for it in d["dot"][:8]:
            L.append(f"• {it['name']}: {_m(it['ads'])} · 0 đơn")
        if len(d["dot"]) > 8:
            L.append(f"… và {len(d['dot']) - 8} page nữa")
        L.append(f"👉 Tổng tiền đốt: {_m(sum(x['ads'] for x in d['dot']))} — tắt hoặc đổi mẫu ngay.")
    if d["lo"]:
        L += ["", f"④  🔴 PAGE ĐANG LỖ ({len(d['lo'])} page)"]
        for it in d["lo"][:8]:
            L.append(f"• {it['name']}: lỗ {_m(abs(it['lai_lo']))}"
                     f"  (ads {_m(it['ads'])} · {it['don']} đơn · DT {_m(it['rev'])})")
        if len(d["lo"]) > 8:
            L.append(f"… và {len(d['lo']) - 8} page nữa")
    if d["lai"]:
        L += ["", f"⑤  🟢 PAGE ĐANG LÃI ({len(d['lai'])} page) — top 5"]
        for it in d["lai"][:5]:
            L.append(f"• {it['name']}: +{_m(it['lai_lo'])}"
                     f"  (ads {_m(it['ads'])} · {it['don']} đơn)")
    # Tỷ lệ phát / hoàn theo shop NV quản lý
    dstat = _nv_delivery_stats(user_id, day)
    if dstat["phat"] is not None:
        L += ["", f"📦 Tỷ lệ phát: {dstat['phat']:.1f}%"
                  f"  (đã nhận {dstat['received']} / xuất kho {dstat['sent'] + dstat['received']})",
              f"🔄 Tỷ lệ hoàn: {dstat['hoan']:.1f}%"
                  f"  (đã hoàn {dstat['returned']})"]
    else:
        L += ["", f"📦 Tỷ lệ phát: — (chưa có đơn xuất kho)"]

    if not (d["dot"] or d["lo"] or d["lai"]):
        L += ["", "ℹ️ Chưa gán được page nào cho bạn — báo quản lý gán page để xem chi tiết."]

    if w["page_test"] and not w["don_test"]:
        L += ["", "⚠️ Test chưa ra đơn nào — xem lại mẫu/nhắm mục tiêu nhé."]
    return "\n".join(L)


def send_personal_reports(day: str) -> int:
    """Gửi báo cáo riêng cho mọi NV đã map Zalo (users.zalo_uid). Trả số người đã gửi."""
    sent = 0
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT u.id, COALESCE(NULLIF(u.full_name,''), u.username), u.zalo_uid
                  FROM users u
                  JOIN user_ad_account_assignments m ON m.user_id=u.id AND m.assigned_to IS NULL
                 WHERE COALESCE(u.zalo_uid,'') <> ''""")
            people = cur.fetchall()
        for uid, name, zuid in people:
            txt = build_personal_report(uid, name, day)
            if send_long(str(zuid), txt, "user"):
                sent += 1
    except Exception as exc:
        logger.warning("send_personal_reports error: %s", exc)
    return sent


def send_to_thread(thread_id: str, text: str, thread_type: str = "group") -> bool:
    """Gửi thẳng qua bridge và KIỂM TRA kết quả thật (không báo OK giả).

    Trước đây dùng _push_to_zalo — hàm này thoát im lặng khi thiếu env
    (ZALO_BRIDGE_SECRET / OUTBOUND_URL) nên báo 'gửi OK' mà tin không tới.
    """
    import os
    import requests
    secret = (os.environ.get("ZALO_BRIDGE_SECRET") or "").strip()
    url = os.environ.get("ZALO_BRIDGE_OUTBOUND_URL", "http://127.0.0.1:5071/send")
    if not secret:
        logger.error("send_to_thread: THIẾU ZALO_BRIDGE_SECRET — không gửi được")
        return False
    payload = {"thread_id": str(thread_id), "text": text, "thread_type": thread_type}
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload,
                              headers={"X-Bridge-Secret": secret}, timeout=20)
            if r.status_code == 200 and (r.json() or {}).get("ok"):
                return True
            logger.warning("send_to_thread %s: bridge trả %s %s",
                           thread_id, r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("send_to_thread %s lần %s lỗi: %s", thread_id, attempt + 1, exc)
    return False


def build_parts(date_from: str, date_to: str, nv_filter: str = "") -> list:
    """Chia báo cáo thành TỪNG KHỐI (①tổng quan ②page ③test ④NV) để gửi rời.

    Sếp Phong 11/08: gửi tách ra, vài giây 1 tin — dễ đọc, không dồn 1 cục.
    """
    text = build_report(date_from, date_to, nv_filter)
    sep = "─" * 22
    parts = [p.strip() for p in text.split(sep) if p.strip()]
    ai = build_ai_analysis(text)      # khối 🧠 do GPT viết (bỏ qua nếu chưa cấu hình)
    if ai:
        parts.append(ai)
    return parts


def send_parts(thread_id: str, parts: list, thread_type: str = "group",
               delay: float = 4.0) -> bool:
    """Gửi lần lượt từng khối, nghỉ `delay` giây giữa các tin."""
    import time
    ok_all = True
    for i, part in enumerate(parts):
        if not send_long(thread_id, part, thread_type):
            ok_all = False
        if i < len(parts) - 1:
            time.sleep(delay)
    return ok_all


def send_long(thread_id: str, text: str, thread_type: str = "group",
              limit: int = 2200) -> bool:
    """Zalo chặn tin quá dài → cắt theo DÒNG thành nhiều tin gửi nối tiếp.

    Cắt ưu tiên tại ranh giới khối (dòng '─────') để mỗi tin là 1 phần trọn vẹn.
    """
    import time
    if len(text) <= limit:
        return send_to_thread(thread_id, text, thread_type)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    ok_all = True
    n = len(chunks)
    for i, ch in enumerate(chunks, 1):
        body = ch if n == 1 else f"{ch}\n\n〔{i}/{n}〕"
        if not send_to_thread(thread_id, body, thread_type):
            ok_all = False
        time.sleep(1.2)   # tránh Zalo chặn gửi dồn
    return ok_all


def send_daily_report(date_from: str, date_to: str) -> int:
    """Gửi báo cáo vào mọi nhóm đã BẬT (lan_ads_report_threads). Trả số nhóm gửi."""
    from app_ctx import load_config
    cfg = load_config() or {}
    tids = [t for t in str(cfg.get("lan_ads_report_threads") or "").split(",") if t.strip()]
    if not tids:
        return 0
    text = build_report(date_from, date_to)
    n = 0
    for tid in tids:
        if send_to_thread(tid, text, "group"):
            n += 1
    return n


# ============================================================
#  BÁO CÁO NHÓM TEAM — gửi vào nhóm Zalo của từng team (sếp Phong 12/08)
#  Khác bản TỔNG: CHỈ số của team đó, xếp hạng nội bộ team, không thấy team khác.
# ============================================================
def build_team_report(team_name: str, day: str) -> str:
    """Báo cáo cho 1 TEAM về ngày `day`."""
    from modules.chi_phi_qc import _lai_lo_report
    data = _lai_lo_report(day, day, team_f=team_name)
    nvs = [r for r in (data.get("by_nv") or []) if str(r.get("key") or "") != "__org"]
    pages = data.get("by_page") or []

    rev = sum(float(r.get("rev") or 0) for r in nvs)
    don = sum(int(float(r.get("don") or 0)) for r in nvs)
    ads = sum(float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0) for r in nvs)
    lai = sum(float(r.get("lai_lo") or 0) for r in nvs)

    L = [
        f"🌸 BÁO CÁO TEAM — {team_name}",
        f"📅 Ngày {day}",
        "",
        "①  KẾT QUẢ TEAM",
        f"{'🟢 LÃI' if lai >= 0 else '🔴 LỖ'}: {_m(lai)}",
        f"💰 Doanh thu: {_m(rev)}  ·  🧾 {don} đơn",
        f"💸 Ngân sách ads: {_m(ads)}",
        f"🎯 Tiền ads/đơn: {_m(ads / don) if don else '—'}",
    ]

    # ② Từng người trong team — xếp lãi cao trước
    nvs.sort(key=lambda r: -float(r.get("lai_lo") or 0))
    if nvs:
        L += ["", f"②  TỪNG NGƯỜI TRONG TEAM ({len(nvs)})"]
        huy = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(nvs):
            a = float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0)
            d_ = int(float(r.get("don") or 0))
            ll = float(r.get("lai_lo") or 0)
            ico = huy[i] if i < 3 and ll > 0 else ("⚠️" if ll < 0 else "  ")
            chi = f"ads {_m(a)} · {d_} đơn" + (f" · {_m(a / d_)}/đơn" if d_ else "")
            L.append(f"{ico} {r.get('name')}: {'+' if ll >= 0 else ''}{_m(ll)}  ({chi})")

    # ③④ Page đốt ads / page lỗ — kèm tên NV phụ trách
    owner = _page_owner(day, day)
    meta = _meta_orders(day, day)
    dot, lo = [], []
    for r in pages:
        a = float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0)
        d_ = int(float(r.get("don") or 0)) or meta.get(str(r.get("key")), 0)
        # Chỉ ghi tên NV nếu người đó THUỘC team này — _page_owner nhìn 14 ngày
        # nên có thể trả về người team khác, ghi vào báo cáo team là gây hiểu nhầm.
        _o = owner.get(str(r.get("key"))) or {}
        it = {"name": r.get("name") or str(r.get("key")), "ads": a, "don": d_,
              "rev": float(r.get("rev") or 0), "lai_lo": float(r.get("lai_lo") or 0),
              "nv": (_o.get("nv") or "") if (_o.get("team") or "") == team_name else ""}
        if a > 0 and d_ == 0:
            dot.append(it)
        elif it["lai_lo"] < 0:
            lo.append(it)
    dot.sort(key=lambda x: -x["ads"])
    lo.sort(key=lambda x: x["lai_lo"])
    if dot:
        L += ["", f"③  🔥 PAGE ĐỐT ADS — 0 ĐƠN ({len(dot)} page)"]
        for it in dot[:10]:
            L.append(f"• {it['name']}: {_m(it['ads'])}" + (f" — {it['nv']}" if it["nv"] else ""))
        if len(dot) > 10:
            L.append(f"… và {len(dot) - 10} page nữa")
        L.append(f"👉 Tổng tiền đốt: {_m(sum(x['ads'] for x in dot))}")
    if lo:
        L += ["", f"④  🔴 PAGE LỖ CÓ ĐƠN ({len(lo)} page)"]
        for it in lo[:10]:
            L.append(f"• {it['name']}: {_m(it['lai_lo'])} (ads {_m(it['ads'])} · {it['don']} đơn)"
                     + (f" — {it['nv']}" if it["nv"] else ""))
        if len(lo) > 10:
            L.append(f"… và {len(lo) - 10} page nữa")

    # ⑤ Test SP mới của team
    t_sp = [r for r in pages if (float(r.get("cp_test") or 0) > 0)]
    if t_sp:
        for r in t_sp:   # đơn của page test nằm ở Meta (chưa lên POS)
            r["_don"] = int(float(r.get("don") or 0)) or meta.get(str(r.get("key")), 0)
        t_ads = sum(float(r.get("cp_test") or 0) for r in t_sp)
        t_don = sum(r["_don"] for r in t_sp)
        chua = [r for r in t_sp if r["_don"] == 0]
        L += ["", "⑤  🧪 TEST SP MỚI",
              f"{len(t_sp)} SP · đốt {_m(t_ads)} · ra {t_don} đơn"
              + (f" (~{_m(t_ads / t_don)}/đơn)" if t_don else ""),
              f"❌ {len(chua)} SP chưa ra đơn nào"]
        tot = sorted([r for r in t_sp if r["_don"] > 0], key=lambda r: -r["_don"])[:3]
        for r in tot:
            _o2 = owner.get(str(r.get("key"))) or {}
            nv = (_o2.get("nv") or "") if (_o2.get("team") or "") == team_name else ""
            L.append(f"✅ {r.get('name')}: {r['_don']} đơn · "
                     f"{_m(r.get('cp_test') or 0)}" + (f" — {nv}" if nv else ""))

    if not nvs:
        L += ["", "ℹ️ Team chưa có ai chạy ads ngày này."]
    L += ["", "📊 Chi tiết: moon.tieuhiem.com/chi-phi-qc/lai-lo"]
    return "\n".join(L)


def _team_report_map() -> dict:
    """{thread_id: team_name} — cấu hình ở app_config `lan_team_report_threads`
    dạng "tid=Team A,tid2=Team B"."""
    out = {}
    try:
        from app_ctx import load_config
        raw = str((load_config() or {}).get("lan_team_report_threads") or "")
        for pair in raw.split(","):
            if "=" in pair:
                tid, team = pair.split("=", 1)
                if tid.strip() and team.strip():
                    out[tid.strip()] = team.strip()
    except Exception as exc:
        logger.warning("_team_report_map error: %s", exc)
    return out


def send_team_reports(day: str) -> int:
    """Gửi báo cáo TEAM vào từng nhóm đã map. Trả số nhóm gửi thành công."""
    n = 0
    for tid, team in _team_report_map().items():
        try:
            if send_long(tid, build_team_report(team, day), "group"):
                n += 1
        except Exception as exc:
            logger.warning("send_team_reports(%s/%s) error: %s", tid, team, exc)
    return n


# ============================================================
#  BÁO CÁO NHANH TRONG NGÀY (real-time) — sếp Phong 14/08
#  "Xem lỗ lãi real time cái này mới quan trọng"
#  Gửi nhiều lần/ngày, NGẮN GỌN, chỉ số sống tới thời điểm gửi.
# ============================================================
def build_realtime_report(day: str) -> str:
    """Bản tin ngắn: lãi/lỗ tới lúc này + page đang lỗ + page đốt tiền chưa ra đơn."""
    import datetime as _dt
    from modules.chi_phi_qc import _lai_lo_report

    data = _lai_lo_report(day, day)
    pages = data.get("by_page") or []
    nvs = [r for r in (data.get("by_nv") or []) if str(r.get("key") or "") != "__org"]

    rev = sum(float(r.get("rev") or 0) for r in pages)
    don = sum(int(float(r.get("don") or 0)) for r in pages)
    ads = sum(float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0) for r in pages)
    lai = sum(float(r.get("lai_lo") or 0) for r in pages)
    gio = _dt.datetime.now().strftime("%H:%M")

    ty_le = (ads / rev * 100) if rev > 0 else 0
    canh_bao = ""
    if rev > 0 and ty_le >= 40:
        canh_bao = f"  ⚠️ ads/DT {ty_le:.0f}% — cao"
    elif rev > 0:
        canh_bao = f"  ({ty_le:.0f}% DT)"

    hoan = sum(int(float(r.get("hoan") or 0)) for r in pages)
    tong_don = don + hoan
    L = [f"⚡ LÃI/LỖ NGAY LÚC NÀY — {gio}  ({day})",
         f"💰 Doanh thu: {_m(rev)}  ·  🧾 {don} đơn",
         f"💸 Ads đã tiêu: {_m(ads)}{canh_bao}",
         f"{'🟢 ĐANG LÃI' if lai >= 0 else '🔴 ĐANG LỖ'}: {_m(lai)}"]
    if tong_don > 0:
        _r = hoan / tong_don * 100
        L.append(f"{'🔴' if _r >= 25 else '🟠' if _r >= 15 else '🟢'} Tỷ lệ hoàn: {_r:.1f}%"
                 f"  ({hoan}/{tong_don} đơn) · phát thành công {100-_r:.1f}%")

    owner = _page_owner(day, day)
    meta = _meta_orders(day, day)
    dot, lo = [], []
    for r in pages:
        a = float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0)
        d_ = int(float(r.get("don") or 0)) or meta.get(str(r.get("key")), 0)
        it = {"name": r.get("name") or str(r.get("key")), "ads": a, "don": d_,
              "rev": float(r.get("rev") or 0), "gv": float(r.get("gia_von") or 0),
              "ll": float(r.get("lai_lo") or 0), "nv": (owner.get(str(r.get("key"))) or {}).get("nv", "")}
        if a > 0 and d_ == 0:
            dot.append(it)
        elif it["ll"] < 0:
            lo.append(it)
    dot.sort(key=lambda x: -x["ads"])
    lo.sort(key=lambda x: x["ll"])

    # Sếp Phong 14/08: liệt kê ĐỦ mọi page, không cắt top 5 — tin dài thì
    # send_long tự cắt thành nhiều tin, quan trọng là không giấu page nào.
    if dot:
        L += ["", f"🔥 ĐỐT ADS CHƯA RA ĐƠN ({len(dot)} page):"]
        for it in dot:
            L.append(f"  • {it['name']}: {_m(it['ads'])}" + (f" — {it['nv']}" if it["nv"] else ""))
        L.append(f"  👉 tổng {_m(sum(x['ads'] for x in dot))} — cân nhắc tắt")
    if lo:
        L += ["", f"🔴 PAGE ĐANG LỖ ({len(lo)} page):"]
        for it in lo:
            adsdon = f" · {_m(it['ads']/it['don'])}/đơn" if it["don"] else ""
            # DT 0 mà vẫn có đơn = page TEST (đơn đếm từ Meta, chưa lên POS) —
            # khác hẳn page đang bán mà lỗ thật, phải tách để sếp khỏi hiểu nhầm.
            nhan = " 🧪TEST" if it["rev"] <= 0 else ""
            L.append(f"  • {it['name']}:{nhan} LỖ {_m(abs(it['ll']))}"
                     + (f" — {it['nv']}" if it["nv"] else ""))
            L.append(f"     DT {_m(it['rev'])} · {it['don']} đơn · ads {_m(it['ads'])}{adsdon}")
        _lo_ban = [x for x in lo if x["rev"] > 0]
        _lo_test = [x for x in lo if x["rev"] <= 0]
        L.append(f"  👉 TỔNG LỖ: {_m(abs(sum(x['ll'] for x in lo)))}"
                 f"  (page đang bán {_m(abs(sum(x['ll'] for x in _lo_ban)))}"
                 f" · page test {_m(abs(sum(x['ll'] for x in _lo_test)))})")

    nvs.sort(key=lambda r: float(r.get("lai_lo") or 0))
    xau = [r for r in nvs if float(r.get("lai_lo") or 0) < 0]
    tot = [r for r in nvs if float(r.get("lai_lo") or 0) > 0]
    if xau:
        L += ["", f"⚠️ NV ĐANG LỖ ({len(xau)}):"]
        for r in xau:
            L.append(f"  • {r.get('name')}: {_m(r.get('lai_lo'))}"
                     f" (DT {_m(r.get('rev'))} · {int(float(r.get('don') or 0))} đơn)")
    if tot:
        tot.sort(key=lambda r: -float(r.get("lai_lo") or 0))
        L += ["", f"✅ NV ĐANG LÃI ({len(tot)}):"]
        for r in tot:
            L.append(f"  • {r.get('name')}: +{_m(r.get('lai_lo'))}"
                     f" (DT {_m(r.get('rev'))} · {int(float(r.get('don') or 0))} đơn)")

    L += ["", "📊 moon.tieuhiem.com/chi-phi-qc/lai-lo"]
    return "\n".join(L)


def send_realtime_report(day: str) -> int:
    """Gửi bản tin nhanh — CHỈ cho người trong app_config `lan_realtime_users`.

    Sếp Phong 14/08: bản trong ngày gửi RIÊNG cho sếp, KHÔNG gửi nhóm công ty
    (nhóm chỉ nhận bản tổng 20h) — tránh mar thấy hết số liệu toàn công ty và
    tránh spam nhóm 3 tin × 6 lần/ngày.
    """
    from app_ctx import load_config
    cfg = load_config() or {}
    uids = [t.strip() for t in str(cfg.get("lan_realtime_users") or "").split(",") if t.strip()]
    if not uids:
        logger.warning("send_realtime_report: chưa cấu hình lan_realtime_users — bỏ qua")
        return 0
    txt = build_realtime_report(day)
    n = 0
    for uid in uids:
        if send_long(uid, txt, "user"):
            n += 1
    return n


# ============================================================
#  BÁO CÁO TUẦN / THÁNG — sếp Phong 14/08
#  So kỳ này với kỳ trước: lãi/lỗ · NV lợi nhuận cao nhất · NV âm
#  · NV test ra nhiều mã nhất.
# ============================================================
def _tong_ky(date_from: str, date_to: str) -> dict:
    """Gom số của 1 kỳ: tổng + theo NV + test theo NV."""
    from modules.chi_phi_qc import _lai_lo_report
    data = _lai_lo_report(date_from, date_to)
    pages = data.get("by_page") or []
    nvs = [r for r in (data.get("by_nv") or []) if str(r.get("key") or "") != "__org"]

    out = {
        "rev": sum(float(r.get("rev") or 0) for r in pages),
        "don": sum(int(float(r.get("don") or 0)) for r in pages),
        "ads": sum(float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0) for r in pages),
        "test": sum(float(r.get("cp_test") or 0) for r in pages),
        "lai": sum(float(r.get("lai_lo") or 0) for r in pages),
        "hoan": sum(int(float(r.get("hoan") or 0)) for r in pages),
        "team": {},    # tên team → {lai, rev, don, ads}
        "nv": {},      # tên NV → {lai, rev, don, ads}
        "nv_test": {}, # tên NV → {so_sp, don, tien}
        "pages": pages,
    }
    for r in (data.get("by_team") or []):
        ten = r.get("name") or "(chưa gán team)"
        out["team"][ten] = {
            "lai": float(r.get("lai_lo") or 0), "rev": float(r.get("rev") or 0),
            "don": int(float(r.get("don") or 0)),
            "ads": float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0),
        }
    for r in nvs:
        out["nv"][r.get("name") or "?"] = {
            "lai": float(r.get("lai_lo") or 0), "rev": float(r.get("rev") or 0),
            "don": int(float(r.get("don") or 0)),
            "ads": float(r.get("cpqc") or 0) + float(r.get("cp_test") or 0),
        }
    # Test: page có tiêu tiền test → quy về chủ page
    owner = _page_owner(date_from, date_to)
    meta = _meta_orders(date_from, date_to)
    for r in pages:
        t = float(r.get("cp_test") or 0)
        if t <= 0:
            continue
        nv = (owner.get(str(r.get("key"))) or {}).get("nv") or "(chưa gán NV)"
        d = out["nv_test"].setdefault(nv, {"so_sp": 0, "don": 0, "tien": 0.0})
        d["so_sp"] += 1
        d["tien"] += t
        d["don"] += int(float(r.get("don") or 0)) or meta.get(str(r.get("key")), 0)
    return out


def _chenh(nay: float, truoc: float) -> str:
    """'+12.3tr (+18%)' — so với kỳ trước."""
    d = nay - truoc
    if truoc:
        pct = d / abs(truoc) * 100
        return f"{'+' if d >= 0 else ''}{_m(d)} ({'+' if pct >= 0 else ''}{pct:.0f}%)"
    return f"{'+' if d >= 0 else ''}{_m(d)}"


def build_period_report(ten_ky: str, f1: str, t1: str, f0: str, t0: str) -> str:
    """ten_ky = 'TUẦN' | 'THÁNG'. f1..t1 = kỳ này, f0..t0 = kỳ trước."""
    a = _tong_ky(f1, t1)
    b = _tong_ky(f0, t0)

    L = [f"📅 BÁO CÁO {ten_ky}  {f1} → {t1}",
         f"(so với {ten_ky.lower()} trước: {f0} → {t0})",
         "",
         "①  TỔNG QUAN",
         f"💰 Doanh thu: {_m(a['rev'])}   {_chenh(a['rev'], b['rev'])}",
         f"🧾 Số đơn: {a['don']}   ({a['don']-b['don']:+d})",
         f"💸 Ads: {_m(a['ads'])}   {_chenh(a['ads'], b['ads'])}",
         f"🧪 Trong đó test: {_m(a['test'])}   {_chenh(a['test'], b['test'])}",
         f"{'🟢 LÃI' if a['lai'] >= 0 else '🔴 LỖ'}: {_m(a['lai'])}   {_chenh(a['lai'], b['lai'])}"]
    if a["rev"] > 0:
        L.append(f"📊 Ads/Doanh thu: {a['ads']/a['rev']*100:.0f}%"
                 + (f"  (kỳ trước {b['ads']/b['rev']*100:.0f}%)" if b["rev"] > 0 else ""))
    # Tỷ lệ hoàn = đơn hoàn ÷ (đơn chốt + đơn hoàn) — cùng công thức trang Doanh thu
    _tong_a = a["don"] + a["hoan"]
    _tong_b = b["don"] + b["hoan"]
    if _tong_a > 0:
        _ra = a["hoan"] / _tong_a * 100
        _rb = (b["hoan"] / _tong_b * 100) if _tong_b > 0 else 0
        _dau = "🔴" if _ra >= 25 else "🟠" if _ra >= 15 else "🟢"
        L.append(f"{_dau} TỶ LỆ HOÀN: {_ra:.1f}%  ({a['hoan']} đơn hoàn / {_tong_a} đơn)"
                 + (f"  · kỳ trước {_rb:.1f}% ({_ra-_rb:+.1f}đ)" if _tong_b > 0 else ""))
        L.append(f"   → phát thành công {100-_ra:.1f}%")

    # ② Theo TEAM
    teams = sorted(a["team"].items(), key=lambda x: -x[1]["lai"])
    if teams:
        L += ["", f"②  👥 THEO TEAM ({len(teams)})"]
        for ten, d in teams:
            truoc = (b["team"].get(ten) or {}).get("lai", 0)
            dau = "🟢" if d["lai"] >= 0 else "🔴"
            L.append(f"{dau} {ten}: {'+' if d['lai'] >= 0 else ''}{_m(d['lai'])}   {_chenh(d['lai'], truoc)}")
            L.append(f"      DT {_m(d['rev'])} · {d['don']} đơn · ads {_m(d['ads'])}"
                     + (f" · {d['ads']/d['rev']*100:.0f}% DT" if d["rev"] > 0 else ""))

    # ③ NV lợi nhuận cao nhất
    lai_nv = sorted(a["nv"].items(), key=lambda x: -x[1]["lai"])
    tot = [x for x in lai_nv if x[1]["lai"] > 0]
    am = [x for x in lai_nv if x[1]["lai"] < 0]
    if tot:
        L += ["", f"③  🏆 NV MANG VỀ LỢI NHUẬN CAO NHẤT ({len(tot)})"]
        huy = ["🥇", "🥈", "🥉"]
        for i, (ten, d) in enumerate(tot):
            truoc = (b["nv"].get(ten) or {}).get("lai", 0)
            L.append(f"{huy[i] if i < 3 else '  •'} {ten}: +{_m(d['lai'])}   {_chenh(d['lai'], truoc)}")
            L.append(f"      DT {_m(d['rev'])} · {d['don']} đơn · ads {_m(d['ads'])}")
    if am:
        L += ["", f"④  ⚠️ NV ÂM ({len(am)})"]
        for ten, d in sorted(am, key=lambda x: x[1]["lai"]):
            truoc = (b["nv"].get(ten) or {}).get("lai", 0)
            L.append(f"  • {ten}: {_m(d['lai'])}   {_chenh(d['lai'], truoc)}")
            L.append(f"      DT {_m(d['rev'])} · {d['don']} đơn · ads {_m(d['ads'])}")

    # ④ Test hàng
    test_nv = sorted(a["nv_test"].items(), key=lambda x: -x[1]["so_sp"])
    if test_nv:
        L += ["", f"⑤  🧪 TEST HÀNG — ai ra nhiều mã nhất"]
        for ten, d in test_nv:
            truoc = (b["nv_test"].get(ten) or {}).get("so_sp", 0)
            adsdon = f" · {_m(d['tien']/d['don'])}/đơn" if d["don"] else " · CHƯA RA ĐƠN"
            L.append(f"  • {ten}: {d['so_sp']} mã  (kỳ trước {truoc})")
            L.append(f"      {d['don']} đơn · tiền test {_m(d['tien'])}{adsdon}")

    # ⑥ Sản phẩm/page hoàn về nhiều nhất
    owner = _page_owner(f1, t1)
    hoan_rows = []
    for r in a["pages"]:
        h = int(float(r.get("hoan") or 0))
        if h <= 0:
            continue
        d = int(float(r.get("don") or 0))
        hoan_rows.append({"name": r.get("name") or str(r.get("key")), "hoan": h, "don": d,
                          "ty": h / (d + h) * 100 if (d + h) else 0,
                          "nv": (owner.get(str(r.get("key"))) or {}).get("nv", "")})
    if hoan_rows:
        hoan_rows.sort(key=lambda x: (-x["hoan"], -x["ty"]))
        L += ["", f"⑥  📦 SẢN PHẨM HOÀN VỀ NHIỀU NHẤT ({len(hoan_rows)} page có hoàn)"]
        for it in hoan_rows[:10]:
            canh = " ⚠️" if it["ty"] >= 30 else ""
            L.append(f"  • {it['name']}: {it['hoan']} đơn hoàn / {it['don']+it['hoan']} "
                     f"= {it['ty']:.0f}%{canh}" + (f" — {it['nv']}" if it["nv"] else ""))

    # ⑦⑧ Top page lãi / lỗ trong kỳ
    tot_page = sorted([r for r in a["pages"] if float(r.get("lai_lo") or 0) > 0],
                      key=lambda r: -float(r.get("lai_lo") or 0))[:10]
    if tot_page:
        L += ["", "⑦  🏅 10 PAGE LÃI CAO NHẤT KỲ"]
        for r in tot_page:
            nv = (owner.get(str(r.get("key"))) or {}).get("nv") or ""
            L.append(f"  • {r.get('name')}: +{_m(r.get('lai_lo'))}"
                     f" (DT {_m(r.get('rev'))} · {int(float(r.get('don') or 0))} đơn)"
                     + (f" — {nv}" if nv else ""))

    lo = sorted([r for r in a["pages"] if float(r.get("lai_lo") or 0) < 0],
                key=lambda r: float(r.get("lai_lo") or 0))[:10]
    if lo:
        L += ["", "⑧  🔴 10 PAGE LỖ NẶNG NHẤT KỲ"]
        for r in lo:
            nv = (owner.get(str(r.get("key"))) or {}).get("nv") or ""
            L.append(f"  • {r.get('name')}: {_m(r.get('lai_lo'))}"
                     f" (DT {_m(r.get('rev'))} · {int(float(r.get('don') or 0))} đơn)"
                     + (f" — {nv}" if nv else ""))

    L += ["", "📊 moon.tieuhiem.com/chi-phi-qc/lai-lo"]
    return "\n".join(L)


def send_period_report(ten_ky: str, f1: str, t1: str, f0: str, t0: str) -> int:
    """Gửi báo cáo tuần/tháng cho danh sách `lan_period_users` (mặc định = sếp)."""
    from app_ctx import load_config
    cfg = load_config() or {}
    uids = [t.strip() for t in str(cfg.get("lan_period_users")
                                   or cfg.get("lan_realtime_users") or "").split(",") if t.strip()]
    if not uids:
        logger.warning("send_period_report: chưa cấu hình người nhận")
        return 0
    txt = build_period_report(ten_ky, f1, t1, f0, t0)
    n = 0
    for uid in uids:
        if send_long(uid, txt, "user"):
            n += 1
    return n
