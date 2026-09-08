"""
Scheduler tự động — thay thế cron jobs cũ trên thichthich.vn.
Dùng APScheduler chạy nền trong cùng tiến trình Flask.
Múi giờ: Asia/Ho_Chi_Minh (UTC+7).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

BASE_DIR = Path(__file__).resolve().parent
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
log = logging.getLogger("scheduler")


def _python_for_subjobs() -> str:
    """Chọn Python interpreter phù hợp cho subprocess jobs.
    Ưu tiên: SCHEDULER_PYTHON env → .venv/bin/python3 → sys.executable
    """
    env_python = os.environ.get("SCHEDULER_PYTHON", "").strip()
    if env_python and Path(env_python).is_file():
        return env_python
    venv_python = BASE_DIR / ".venv" / "bin" / "python3"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _reset_outbound_cursors(shop_ids: list[str]) -> None:
    """Xóa Redis cursor outbound cho danh sách shop — gọi khi subprocess timeout."""
    try:
        from redis_cache import cache_delete_pattern
        for sid in shop_ids:
            cache_delete_pattern(f"sync_cursor_ob:{sid}:*")
        log.info("[scheduler] cursor reset %d shops: %s", len(shop_ids), shop_ids[:5])
    except Exception as exc:
        log.warning("[scheduler] cursor reset failed: %s", exc)


def _run(label: str, cmd: list[str], shell_script: bool = False,
         env_extra: dict | None = None, timeout: int = 600,
         reset_cursor_shop_ids: list[str] | None = None) -> None:
    """Chạy script con, log kết quả kèm stderr khi lỗi.

    FIX MEMORY LEAK: Không dùng capture_output=True (buffers toàn bộ stdout vào RAM worker).
    - stdout → DEVNULL  (output lớn, không cần khi thành công)
    - stderr → PIPE     (chỉ capture stderr để log lỗi — nhỏ hơn nhiều)
    - env=os.environ    (không copy dict, tiết kiệm RAM)

    env_extra: dict vars override (vd {"WH_SYNC_USE_CURSOR": "0"} cho nightly full).
    timeout: cap subprocess (giây). Default 600s; job nặng (auto_refresh) override 1200s.
    reset_cursor_shop_ids: nếu truyền, khi timeout sẽ xóa Redis cursor outbound của các shop
        này → lần chạy kế tiếp fetch full thay vì resume cursor lệch/thiếu.
    """
    python = _python_for_subjobs()
    log.info("[scheduler] START %s (python=%s, timeout=%ss)", label, python, timeout)
    if env_extra:
        _env = dict(os.environ)
        _env.update(env_extra)
    else:
        _env = os.environ
    try:
        if shell_script:
            result = subprocess.run(
                ["bash"] + cmd,
                cwd=str(BASE_DIR),
                env=_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                [python] + cmd,
                cwd=str(BASE_DIR),
                env=_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        if result.returncode == 0:
            log.info("[scheduler] OK %s", label)
        else:
            log.warning(
                "[scheduler] WARN %s exit=%s python=%s\nSTDERR: %s",
                label,
                result.returncode,
                python,
                (result.stderr or "")[-2000:],
            )
    except subprocess.TimeoutExpired:
        log.error("[scheduler] TIMEOUT %s", label)
        if reset_cursor_shop_ids:
            _reset_outbound_cursors(reset_cursor_shop_ids)
    except Exception as exc:
        log.error("[scheduler] ERROR %s: %s", label, exc)


def job_auto_refresh() -> None:
    today = __import__("datetime").date.today().strftime("%Y-%m-%d")
    # Workload nặng: 8 bước (sync POS 150d, orders, inventory, FB ads, verify…)
    # Bình thường ~8-10 phút. Cap 1200s (20p) để tránh false-timeout, vẫn đủ ngắn để fail-fast nếu hang.
    _run("auto_refresh", ["run_auto_refresh_all_data.sh", today], shell_script=True, timeout=1200)


def job_resync_recent_30days() -> None:
    """Backfill 30 ngày gần nhất từ POS analytics → DB.

    Mục đích: POS data thay đổi liên tục (đơn hoàn/huỷ ngày cũ). auto_refresh chính
    sync rolling 150 ngày mỗi 15p nhưng dashboard nhiều khi đọc cache stale.
    Job này gọi explicit per-date để chắc chắn 30 ngày gần nhất đều fresh.
    Bug 2026-05-21: NV báo dashboard thanhleader lệch POS từ 13/5 — job này phòng tái phát.
    """
    import datetime as _dt
    today = _dt.date.today()
    # Sync rolling 30 days vào JSON
    _run(
        "resync_30d_pos",
        ["sync_pos.py", "--days", "30"],
        timeout=900,
    )
    # Bootstrap toàn bộ JSON → daily_shop_metrics (ALL rows trong JSON đều upsert)
    today_str = today.strftime("%Y-%m-%d")
    _run(
        "resync_30d_bootstrap",
        ["scripts/bootstrap_daily_shop_metrics_from_json.py", "--require-date", today_str],
        timeout=300,
    )


def job_resync_old_orders() -> None:
    """Quét lại TỪNG ĐƠN 3-8 tuần tuổi vào bảng `orders` (trạng thái + giá trị tiền).

    Mục đích: bắt đơn HOÀN MUỘN — sync thường chỉ kéo đơn theo ngày tạo nên đơn cũ
    đổi trạng thái sang returned không được cập nhật (T4 chỉ bắt 38% đơn hoàn).
    Phục vụ lương 2B: khi count hoàn khớp daily_shop_metrics thì engine lương
    chuyển từ hoàn ƯỚC (returned_count × AOV) sang hoàn THẬT từng đồng.

    Phủ sóng cùng các vòng quét có sẵn: cron 03:30 (D-1..10) + cron_refresh_order_status
    05:00/23:45 (D-1..14). Job này lo D-15..D-56 → đơn được refresh hằng ngày 2 tuần đầu,
    rồi các mốc 15/18/21/28/35/42/49/56 — đủ chín cho kỳ lương lệch 1 tháng.
    """
    import datetime as _dt
    today = _dt.date.today()
    for tuoi in (15, 18, 21, 28, 35, 42, 49, 56):
        target = (today - _dt.timedelta(days=tuoi)).isoformat()
        _run(
            f"resync_orders_{target}",
            ["scripts/sync_orders_order_items_to_db.py", "--date", target],
            timeout=1800,
        )


def job_doi_soat_orders() -> None:
    """ĐỐI SOÁT ĐẦU VÀO đơn hàng: đếm đơn từng ngày trong bảng `orders` vs
    POS analytics `total_order_count` (= tổng đơn tạo trong ngày — verify 05/06
    khớp tuyệt đối 1.773=1.773). Ngày nào lệch >1% (và >10 đơn) → tự re-sync
    ngày đó để vớt đơn sót/thừa. Tự chữa, không cần người soi.

    Đây là tầng đảm bảo KHÔNG SÓT ĐƠN (đầu vào) — bổ sung cho resync_old_orders
    (đảm bảo trạng thái chín). Cả 2 phục vụ lương 2B: đơn đúng → lương đúng.
    """
    import datetime as _dt
    from db import get_conn
    today = _dt.date.today()
    d_from, d_to = today - _dt.timedelta(days=60), today - _dt.timedelta(days=1)
    lech_dates: list[tuple[float, str]] = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT d.ngay, d.so_orders, a.so_analytics
                    FROM (SELECT created_at_pos::date AS ngay, COUNT(*) AS so_orders
                          FROM orders
                          WHERE created_at_pos::date BETWEEN %s AND %s
                          GROUP BY 1) d
                    JOIN (SELECT metric_date::date AS ngay, SUM(total_order_count) AS so_analytics
                          FROM daily_shop_metrics
                          WHERE metric_date::date BETWEEN %s AND %s
                          GROUP BY 1) a ON a.ngay = d.ngay
                    WHERE a.so_analytics > 0
                """, (d_from, d_to, d_from, d_to))
                for ngay, so_o, so_a in cur.fetchall():
                    lech = abs(int(so_o) - int(so_a))
                    if lech > 10 and lech / float(so_a) > 0.01:
                        lech_dates.append((lech / float(so_a), ngay.isoformat()))
    except Exception:
        log.exception("[doi_soat_orders] lỗi query")
        return
    if not lech_dates:
        log.info("[doi_soat_orders] OK — 60 ngày khớp POS trong ±1%%")
        return
    lech_dates.sort(reverse=True)
    log.warning("[doi_soat_orders] %d ngày lệch >1%%: %s", len(lech_dates),
                [(d, f"{p:.1%}") for p, d in lech_dates[:10]])
    for _, target in lech_dates[:6]:  # tự chữa tối đa 6 ngày/đêm
        _run(f"doisoat_resync_{target}",
             ["scripts/sync_orders_order_items_to_db.py", "--date", target],
             timeout=1800)


def job_daily_summary() -> None:
    _run("daily_summary", ["daily_summary.py"])


def job_order_report() -> None:
    _run("order_report", ["send_order_report.py"])


def job_slow_sales() -> None:
    _run("slow_sales", ["send_slow_sales_report.py"])


def job_stock_alert() -> None:
    _run("stock_alert", ["send_stock_alert.py"])


def job_top_report() -> None:
    _run("top_report", ["daily_top_report.py"])


def job_weekly_kpi_alert() -> None:
    """Báo cáo tuần TOP 10 NV doanh thu thấp (so sánh tuần trước nữa).
    Chạy mỗi T2 lúc 19:00 VN, kỳ Mon→Sun của tuần TRƯỚC vừa kết thúc."""
    _run("weekly_kpi_alert", ["send_kpi_alert.py", "--period", "week"], timeout=300)


def job_monthly_kpi_alert() -> None:
    """Báo cáo tháng TOP 10 NV doanh thu thấp (so sánh tháng trước nữa).
    Chạy ngày 1 hàng tháng lúc 19:00 VN, kỳ là tháng TRƯỚC vừa kết thúc."""
    _run("monthly_kpi_alert", ["send_kpi_alert.py", "--period", "month"], timeout=300)


def job_ads_alert() -> None:
    _run("ads_alert", ["send_ads_alert.py"])


def job_budget_chat_digest() -> None:
    """21:00 — Telegram digest tổng NS NGÀY MAI + Lan nhắc NV chưa báo vào team chat."""
    try:
        from modules.budget_chat.telegram_bridge import (
            cron_daily_digest, cron_lan_nudge_unreported,
        )
        cron_daily_digest()
        cron_lan_nudge_unreported()
    except Exception as exc:
        logger.warning("job_budget_chat_digest error: %s", exc)


def job_zalo_team_digest() -> None:
    """21:05 — Lan gửi tổng hợp NS ngày mai vào từng Zalo group team."""
    try:
        from modules.budget_chat.zalo_digest import send_zalo_team_digest
        send_zalo_team_digest()
    except Exception as exc:
        logger.warning("job_zalo_team_digest error: %s", exc)


def job_zalo_expense_reminder() -> None:
    """20:00 — Lan nhắc NV báo chi phí trong ngày."""
    try:
        from modules.expense_chat.reminder import send_expense_reminder
        send_expense_reminder()
    except Exception as exc:
        logger.warning("job_zalo_expense_reminder error: %s", exc)


def job_4day_summary() -> None:
    _run("4day_summary", ["send_4day_summary.py"])


def job_sweep_dup_pending() -> None:
    """Dọn dup_pending_* stale trong app_config (>1 giờ).

    expense_chat lưu state hỏi 'là 1 hay 2 khoản' qua key
    `dup_pending_<uid>` ở app_config (TTL logic 5 phút). NV không trả lời
    thì row nằm lại → cron sweep mỗi 30 phút để app_config không phình.
    Import lazy bên trong job để tránh ImportError lúc scheduler boot.
    """
    try:
        from scripts.sweep_dup_pending import sweep
        scanned, deleted = sweep(max_age_sec=3600, dry_run=False)
        log.info("[sweep_dup_pending] scanned=%d deleted=%d", scanned, deleted)
    except Exception as exc:
        log.warning("job_sweep_dup_pending error: %s", exc)


def job_cleanup_waiting_orders() -> None:
    """P1-4: Dọn đơn ảo waiting hàng ngày lúc 03:30.
    fast_cleanup_waiting.py fetch status=9 từ tất cả shop, bulk-update
    đơn không nằm trong list → received; sau đó mark fake orders → auto_cleaned.
    Timeout 600s (10p) — thường chạy 5-10s với 90 shop.
    """
    _run("cleanup_waiting", ["scripts/fast_cleanup_waiting.py"], timeout=600)


def job_sync_fb_ads_today() -> None:
    """Auto-sync chi phí QC 7 ngày qua — đảm bảo data đầy đủ cho:
    1. Account mới mapping giữa tuần → tự backfill historical.
    2. FB API trễ data → re-sync để bắt kịp.
    3. Idempotent UPSERT → chạy lại không gây duplicate.

    Sync CẢ 2 script:
    - `sync_fb_ads_by_page.py`: page-level spend (báo cáo theo page)
    - `sync_facebook_ads_to_db.py`: account-level metrics (báo cáo theo account/shop)
    """
    import datetime as _dt
    today = _dt.date.today()
    date_from = today - _dt.timedelta(days=7)
    df = date_from.strftime("%Y-%m-%d")
    dt = today.strftime("%Y-%m-%d")
    _run("sync_fb_ads_by_page_7d", [
        "scripts/sync_fb_ads_by_page.py",
        "--date-from", df, "--date-to", dt,
    ], timeout=1800)
    _run("sync_fb_ads_account_7d", [
        "scripts/sync_facebook_ads_to_db.py",
        "--date-from", df, "--date-to", dt,
    ], timeout=1800)


def job_sync_fb_page_info() -> None:
    """Enrich fb_pages (tên + ảnh avatar) cho page mới có spend nhưng chưa có info.
    Để tab Chi phí QC hiện tên page thật + avatar thay vì 'Page <id>'.

    2 bước:
      1. sync_fb_page_info.py — Graph API qua token TK QC (chỉ lấy được page admin)
      2. scrape_fb_page_names.py — fallback scrape m.facebook.com (lấy được ~90% còn lại,
         không cần token, dùng cho page không thuộc TK QC nào nhưng vẫn public)
    """
    import datetime as _dt
    since = (_dt.date.today() - _dt.timedelta(days=14)).strftime("%Y-%m-%d")
    _run("sync_fb_page_info", ["scripts/sync_fb_page_info.py", "--since", since], timeout=900)
    # Fallback HTML scrape cho các page Graph API không lấy được tên
    _run("scrape_fb_page_names", ["scripts/scrape_fb_page_names.py"], timeout=600)


def job_sync_fb_budget() -> None:
    """Quét ngân sách (spend_cap/amount_spent) các TK QC đuôi TH → snapshot/ngày.
    Để IT/admin xem tối (trước 19h30) TK nào sắp cạn giới hạn mà nạp/nâng limit."""
    _run("sync_fb_budget", ["scripts/sync_fb_budget.py"], timeout=900)


def job_sync_pos_page_metrics() -> None:
    """Kéo chi phí QC / doanh thu / lợi nhuận / đơn POS THEO TỪNG PAGE (Pancake)
    cho MỌI shop active — để đối chiếu với CP Ads FB ở tab Chi phí QC.
    Kéo lại 10 ngày gần nhất mỗi lần (bắt ad cost cập nhật trễ + đơn hoàn)."""
    _run("sync_pos_page_metrics_10d", [
        "scripts/sync_pos_page_metrics.py", "--all", "--days-back", "10",
    ], timeout=1800)


def job_lan_ads_report() -> None:
    """20h00: Lan gửi báo cáo TỔNG (số TRONG NGÀY) cho nhóm + sếp — sếp Phong 11/08.

    Bước 1: sync POS hôm nay cho số tươi. Bước 2: gửi nhóm đã BẬT + cá nhân ở ô ②.
    Báo cáo RIÊNG của từng mar KHÔNG nằm ở đây — xem job_lan_personal_report (08h00).
    """
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    _run("sync_pos_before_report", [
        "scripts/sync_pos_page_metrics.py", "--all",
        "--date-from", today, "--date-to", today,
    ], timeout=900)
    try:
        from modules.chi_phi_qc.lan_ads_report import build_parts, send_parts
        from app_ctx import load_config
        cfg = load_config() or {}
        parts = build_parts(today, today)
        n = 0
        for tid in [t for t in str(cfg.get("lan_ads_report_threads") or "").split(",") if t.strip()]:
            if send_parts(tid, parts, "group"):
                n += 1
        for uid in [t for t in str(cfg.get("lan_ads_report_users") or "").split(",") if t.strip()]:
            if send_parts(uid, parts, "user"):
                n += 1
        log.info("[lan_ads_report] bản TỔNG đã gửi %s nơi", n)
    except Exception as exc:
        log.warning("[lan_ads_report] lỗi: %s", exc)


def job_ladipage_match() -> None:
    """Mỗi 15 phút: đối soát đơn LadiPage với đơn POS (sếp Phong 12/08).

    Không đẩy đơn nữa — chỉ soi đơn khách đã điền form mà CHƯA thấy trên POS,
    để sale kiểm tra lại (sót đơn / đơn bị mang ra ngoài).
    """
    # ── BƯỚC 0 (sếp Phong 07/09): kéo đơn POS hôm nay + hôm qua vào bảng `orders`
    #    TRƯỚC khi dò. Trước đây bảng `orders` KHÔNG có job nào cập nhật định kỳ cho
    #    ngày hiện tại (chỉ tự sync ngầm khi tình cờ có người mở dashboard) → sale
    #    lên POS rồi 1-2 ngày mà phần mềm vẫn báo "chưa lên". Script chạy ~2s/ngày.
    #    Cửa sổ 7 NGÀY (không chỉ hôm qua+nay): sale hay lên POS trễ 3-7 ngày, đơn
    #    cũ trong bảng `orders` cũng phải tươi mới khớp được (Long 07/09 chiều).
    import datetime as _dt
    _hom_nay = _dt.date.today()
    for _i in range(6, -1, -1):
        _d = _hom_nay - _dt.timedelta(days=_i)
        _run(f"ladi_sync_orders_{_d.isoformat()}",
             ["scripts/sync_orders_order_items_to_db.py", "--date", _d.isoformat()],
             timeout=300)
    try:
        from modules.ladipage.matcher import run_match, tim_chu_ads, verify_pos_direct
        r = run_match(limit=1000)
        log.info("[ladipage_match] dò %s đơn — khớp %s, chưa thấy %s, khách cũ %s",
                 r["kiem_tra"], r["co_pos"], r["chua_co"], r.get("khach_cu", 0))
        # DỨT ĐIỂM (Long 07/09): đơn còn "chưa lên" → hỏi THẲNG Pancake POS (nguồn gốc),
        # không tin bản sao `orders` nữa. ~2-3 phút / 500 đơn, trong chu kỳ 15 phút.
        # Tối ưu (Long 07/09): đơn 3-14 ngày hầu như không đổi trong 15 phút → chỉ hỏi
        # POS mỗi giờ (phút :00); còn mỗi 15 phút chỉ hỏi đơn ≤2 ngày (~100 đơn, ~30s).
        # Giảm ~50.000 → ~12.000 lượt gọi Pancake/ngày, tránh bị chặn rate-limit.
        _ngay = 14 if _dt.datetime.now().minute < 15 else 2
        v = verify_pos_direct(days=_ngay)
        log.info("[ladipage_match] xác nhận thẳng POS (≤%s ngày): quét %s, thêm ĐÃ lên %s, lỗi API %s",
                 _ngay, v["quet"], v["co_pos"], v["loi"])
        # Truy chủ đơn từ ID quảng cáo trong link landing (utm_id/campaign/content)
        c = tim_chu_ads(limit=500)
        log.info("[ladipage_match] chủ ads: xét %s, ra chủ %s", c["xet"], c["ra_chu"])
    except Exception as exc:
        log.warning("[ladipage_match] lỗi: %s", exc)


def job_ladipage_verify_full() -> None:
    """Đêm: hỏi thẳng Pancake POS cho MỌI đơn Ladi còn 'chưa lên' — không giới hạn ngày."""
    try:
        from modules.ladipage.matcher import verify_pos_direct
        v = verify_pos_direct(days=3650, limit=5000)
        log.info("[ladipage_verify_full] quét %s đơn cũ — thêm ĐÃ lên %s, lỗi API %s",
                 v["quet"], v["co_pos"], v["loi"])
    except Exception as exc:
        log.warning("[ladipage_verify_full] lỗi: %s", exc)


def job_lan_realtime_report() -> None:
    """Báo cáo LÃI/LỖ NGAY TRONG NGÀY — sếp Phong 14/08:
    "Xem lỗ lãi real time cái này mới quan trọng".

    Chạy nhiều lần/ngày trong giờ làm. Bắt buộc sync POS + FB Ads hôm nay TRƯỚC
    khi gửi, không thì doanh thu trống → nhìn như đang lỗ nặng (số ảo).
    """
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    _run("rt_sync_pos", ["scripts/sync_pos_page_metrics.py", "--all",
                         "--date-from", today, "--date-to", today], timeout=600)
    _run("rt_sync_ads", ["scripts/sync_fb_ads_by_page.py",
                         "--date-from", today, "--date-to", today], timeout=900)
    try:
        from modules.chi_phi_qc.lan_ads_report import send_realtime_report
        log.info("[lan_realtime] %s: đã gửi %s nơi", today, send_realtime_report(today))
    except Exception as exc:
        log.warning("[lan_realtime] lỗi: %s", exc)


def job_lan_week_report() -> None:
    """Thứ Hai 08h30: báo cáo TUẦN TRƯỚC, so với tuần trước nữa (sếp Phong 14/08)."""
    import datetime as _dt
    hom_nay = _dt.date.today()
    t1 = hom_nay - _dt.timedelta(days=hom_nay.weekday() + 1)   # Chủ nhật tuần trước
    f1 = t1 - _dt.timedelta(days=6)                            # Thứ Hai tuần trước
    t0 = f1 - _dt.timedelta(days=1)
    f0 = t0 - _dt.timedelta(days=6)
    try:
        from modules.chi_phi_qc.lan_ads_report import send_period_report
        n = send_period_report("TUẦN", f1.isoformat(), t1.isoformat(),
                               f0.isoformat(), t0.isoformat())
        log.info("[lan_week] %s→%s: đã gửi %s nơi", f1, t1, n)
    except Exception as exc:
        log.warning("[lan_week] lỗi: %s", exc)


def job_lan_month_report() -> None:
    """Mồng 1 lúc 08h45: báo cáo THÁNG TRƯỚC, so với tháng trước nữa."""
    import datetime as _dt
    hom_nay = _dt.date.today()
    t1 = hom_nay.replace(day=1) - _dt.timedelta(days=1)   # ngày cuối tháng trước
    f1 = t1.replace(day=1)
    t0 = f1 - _dt.timedelta(days=1)
    f0 = t0.replace(day=1)
    try:
        from modules.chi_phi_qc.lan_ads_report import send_period_report
        n = send_period_report("THÁNG", f1.isoformat(), t1.isoformat(),
                               f0.isoformat(), t0.isoformat())
        log.info("[lan_month] %s→%s: đã gửi %s nơi", f1, t1, n)
    except Exception as exc:
        log.warning("[lan_month] lỗi: %s", exc)


def job_lan_personal_report() -> None:
    """08h00: Lan gửi báo cáo RIÊNG cho từng mar về NGÀY HÔM QUA (sếp Phong 12/08).

    Sếp chốt: bản TỔNG vẫn 20h (job_lan_ads_report), bản RIÊNG chuyển sang sáng
    hôm sau — sáng ra mar đọc số hôm qua rồi biết đường chỉnh ads trong ngày.
    Sync POS ngày hôm qua trước cho số chốt (POS còn về đơn muộn buổi tối).
    """
    import datetime as _dt
    yday = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    _run("sync_pos_before_personal", [
        "scripts/sync_pos_page_metrics.py", "--all",
        "--date-from", yday, "--date-to", yday,
    ], timeout=900)
    try:
        from modules.chi_phi_qc.lan_ads_report import (
            send_personal_reports, send_team_reports)
        log.info("[lan_personal_report] %s: đã gửi %s NV", yday, send_personal_reports(yday))
        # Báo cáo NHÓM TEAM — chỉ số của team đó, gửi vào nhóm Zalo riêng từng team
        log.info("[lan_team_report] %s: đã gửi %s nhóm team", yday, send_team_reports(yday))
    except Exception as exc:
        log.warning("[lan_personal_report] lỗi: %s", exc)


def job_sync_page_top_product() -> None:
    """Map page → sản phẩm bán nhiều nhất (Pancake) cho MỌI shop active — 1 lần/ngày.
    Dùng cho cột 'Sản phẩm' ở trang chi tiết shop."""
    _run("sync_page_top_product", [
        "scripts/sync_page_top_product.py", "--all", "--max-pages", "50",
    ], timeout=1800)


def job_check_landing_links() -> None:
    """Check link landing SỐNG/CHẾT (Page & Ads › Landing). Có guard chống wipe khi
    mạng server nghẽn (0/N sống → bỏ qua, giữ data cũ). 2 lần/ngày."""
    _run("check_landing_links", ["scripts/check_landing_links.py"], timeout=900)


def job_sync_fb_campaign_objective() -> None:
    """Sync objective mỗi campaign FB → phân loại chi tiêu ladipage (SALES/LEADS/...)
    vs tương tác. Dùng cho card 'Chi tiêu ladipage' ở trang Landing. 1 lần/ngày."""
    _run("sync_fb_campaign_objective", ["scripts/sync_fb_campaign_objective.py"], timeout=1800)


def job_auto_record_company_pages() -> None:
    """Tự ghi nhận page công ty (chạy TK công ty >=2 ngày) vào whitelist → hết gắn 'page
    lạ' oan ở trang Kiểm soát. Page mới <2 ngày vẫn hiện để soi. 1 lần/ngày."""
    _run("auto_record_company_pages", ["scripts/auto_record_company_pages.py"], timeout=300)


def job_auto_map_ads_shops() -> None:
    """Tự nối TK QC → shop POS cho NV có đúng 1 shop active (chống "Chưa gán shop"
    tái diễn với TK mới). Chạy in-process, nhẹ. NV nhiều/không shop → bỏ qua (map tay)."""
    try:
        from db import get_conn
        from repositories.admin_repo import auto_map_single_shop_accounts
        with get_conn() as conn:
            with conn.cursor() as cur:
                n = auto_map_single_shop_accounts(cur)
            conn.commit()
        if n:
            log.info("[auto_map_ads_shops] Đã tự map %d TK → shop NV (single-shop).", n)
    except Exception as exc:
        log.error("[auto_map_ads_shops] error: %s", exc)


def job_sync_fb_ads_backfill_month() -> None:
    """Backfill toàn bộ tháng trước vào đầu tháng mới — đảm bảo dữ liệu đầy đủ cho kế toán."""
    import datetime as _dt
    today = _dt.date.today()
    first_of_month = today.replace(day=1)
    last_of_prev = first_of_month - _dt.timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    log.info("[scheduler] Backfill tháng %s/%s: %s → %s",
             last_of_prev.month, last_of_prev.year,
             first_of_prev, last_of_prev)
    _run("sync_fb_ads_prev_month", [
        "scripts/sync_fb_ads_by_page.py",
        "--date-from", first_of_prev.strftime("%Y-%m-%d"),
        "--date-to",   last_of_prev.strftime("%Y-%m-%d"),
    ], timeout=1800)


def job_sync_pa_account_insights() -> None:
    """Auto-sync spend từ FB API cho tất cả pa_ad_accounts (dùng token Trần Huy)."""
    _run("sync_pa_insights", ["scripts/sync_pa_account_insights.py", "--days", "7"])


def job_warm_dashboard_cache() -> None:
    """Pre-compute home_alert + carrier_pickup cache cho scope "admin" (allowed=None).

    Web worker khi nhận request → đọc từ Redis (cache đã warm) → phản hồi
    nhanh, không phải compute build_ads_delay_data + build_monthly_loss_data
    (~100MB/request). Áp dụng cho ~21 users (admin/manager/accountant/kho/sale)
    xem toàn bộ, ~70% traffic dashboard.

    Chạy in-process (không subprocess) vì compute trên scheduler service riêng,
    không tranh RAM với web workers.

    LEAK FIX 2026-04-30: build_* dùng tạm ~150MB. glibc giữ pages cho lần sau
    → scheduler RAM tích lũy lên 1GB+ sau vài giờ. Phải `gc.collect()` +
    `malloc_trim` sau mỗi lần để trả pages về OS.
    """
    import datetime as _dt
    import gc as _gc
    try:
        # build_ads_delay_data / build_monthly_loss_data có dùng Flask `g`
        # → cần push app_context khi chạy trong scheduler standalone.
        from web_app import app as _flask_app
        from app_ctx import get_home_alert_counts_cached
        current_month = _dt.date.today().strftime("%Y-%m")
        # test_request_context cung cấp fake request → tránh "Working outside
        # of request context" error khi build_* truy cập request/g.
        with _flask_app.test_request_context("/", method="GET"):
            result = get_home_alert_counts_cached(current_month, allowed_shop_keys=None)
        log.info(
            "[warm_dashboard] month=%s ads_delay=%d monthly_loss=%d total_loss=%s",
            current_month,
            result.get("ads_delay_count", 0),
            result.get("monthly_loss_count", 0),
            result.get("monthly_total_loss_fmt", "--"),
        )
    except Exception as exc:
        log.warning("[warm_dashboard] error: %s", exc, exc_info=True)
    finally:
        # Free tạm objects + ép glibc trả pages về OS (giống malloc_trim ở web).
        _gc.collect()
        try:
            import ctypes as _ctypes
            _ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass


def job_sync_fb_ads_backfill_week() -> None:
    """Backfill 7 ngày qua mỗi Chủ nhật — vá các ngày bị miss trong tuần."""
    import datetime as _dt
    today = _dt.date.today()
    date_from = today - _dt.timedelta(days=7)
    _run("sync_fb_ads_week", [
        "scripts/sync_fb_ads_by_page.py",
        "--date-from", date_from.strftime("%Y-%m-%d"),
        "--date-to",   today.strftime("%Y-%m-%d"),
    ], timeout=1800)


def job_db_healthcheck() -> None:
    _run("db_healthcheck", ["run_daily_db_healthcheck.sh"], shell_script=True)


def job_sync_orders_db() -> None:
    today = __import__("datetime").date.today().strftime("%Y-%m-%d")
    _run("sync_orders_db", ["run_sync_orders_to_db.sh", today], shell_script=True)


def job_mb_alert_burning_ads() -> None:
    """Marketing Brain: cảnh báo ad spend ≥300k/2 ngày (D-3..D-2) mà 0 đơn POS → Telegram."""
    _run("mb_alert_burning_ads", ["scripts/mb_alert_burning_ads.py"], timeout=300)


def _get_active_pos_shop_ids() -> list[str]:
    """Đọc list pos_shop_id có api_key, sort để batch ổn định giữa các lần."""
    from modules.kho_vat_ly.wh_db import wh_db as _db
    with _db() as conn:
        rows = conn.execute(
            "SELECT pos_shop_id FROM wh_shops "
            "WHERE status='active' AND pos_api_key IS NOT NULL AND pos_api_key != '' "
            "ORDER BY id"
        ).fetchall()
    return [str(r["pos_shop_id"]) for r in rows]


def _chunk_list(lst: list, n_chunks: int) -> list[list]:
    """Chia list thành n chunks gần đều nhau."""
    if not lst or n_chunks <= 0:
        return []
    sz = (len(lst) + n_chunks - 1) // n_chunks
    return [lst[i:i + sz] for i in range(0, len(lst), sz)]


def job_sync_wh_returns() -> None:
    """Sync đơn hoàn — chia 4 batch subprocess sequential để tránh balloon RAM.

    • Lần đầu: full 90 ngày.
    • Những lần tiếp: 21 ngày active_only.
    • Mỗi subprocess xử lý ~22 shop × 3 status = ~66 task → peak ~50-200MB.
    • Subprocess exit → kernel reclaim RAM → start batch kế tiếp.
    """
    shop_ids = _get_active_pos_shop_ids()
    if not shop_ids:
        log.warning("[sync_returns_smart] không có shop active")
        return
    batches = _chunk_list(shop_ids, 4)
    log.info("[sync_returns_smart] %d shop chia %d batch (~%d shop/batch)",
             len(shop_ids), len(batches), len(batches[0]) if batches else 0)
    for idx, batch in enumerate(batches, start=1):
        _run(f"sync_returns_smart.b{idx}",
             ["scripts/sync_orders_smart.py", "--source", "returns",
              "--shop-ids", ",".join(batch)],
             timeout=1800)


_SYNC_RETURNS_1645_MARKER = "/tmp/last_sync_returns_16h45.txt"


def job_sync_returns_evening_1645() -> None:
    """Force sync hàng hoàn (hôm qua + hôm nay) lúc 16:45 mỗi ngày.

    Mục đích: NV bắt đầu quét hàng hoàn lúc 18:00 → cần data đầy đủ trước đó.
    Sync 30 phút (job_sync_wh_returns) đôi khi bị lag → thêm 1 mốc force trước
    khung quét tối, đảm bảo backlog Pancake đã hoàn dồn lên DB.

    KHÔNG sửa logic gốc — chỉ gọi sync_returns_for_date_range của
    wh_sync_returns.py (cùng API mà thread fast-returns + button "Sync từ POS"
    đang dùng). RETURNS_SYNC_LOCK trong hàm gốc tự skip nếu đang chạy.

    Sau khi chạy xong → ghi marker file để catch-up khi service restart sau 16:45
    biết hôm đó đã chạy rồi, KHÔNG chạy lại lần 2.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range
        now_vn = _dt.now(_tz.utc) + _td(hours=7)
        date_today = now_vn.strftime("%Y-%m-%d")
        date_yesterday = (now_vn - _td(days=1)).strftime("%Y-%m-%d")
        log.info("[sync_returns_16h45] BẮT ĐẦU — range %s → %s",
                 date_yesterday, date_today)
        t0 = _dt.now()
        result = sync_returns_for_date_range(date_yesterday, date_today)
        elapsed = (_dt.now() - t0).total_seconds()
        errs = result.get("errors", []) or []
        log.info("[sync_returns_16h45] XONG sau %.1fs — inserted=%s skipped=%s errors=%s",
                 elapsed,
                 result.get("inserted", 0),
                 result.get("skipped", 0),
                 len(errs))
        if errs:
            log.warning("[sync_returns_16h45] lỗi đầu tiên: %s", errs[0])
        # Marker: ghi ngày VN sau khi sync xong (catchup tránh trùng)
        try:
            from pathlib import Path as _Path
            _Path(_SYNC_RETURNS_1645_MARKER).write_text(date_today)
        except Exception as _e:
            log.warning("[sync_returns_16h45] không ghi được marker: %s", _e)
    except Exception as e:
        log.error("[sync_returns_16h45] CRASH: %s", e, exc_info=True)


def _catchup_sync_returns_if_missed() -> None:
    """Catch-up khi pos-scheduler boot SAU 16:45 mà hôm đó chưa chạy.

    Khi service restart đúng giờ trigger (vd 16:45:45 hôm 2026-06-04), APScheduler
    tính next_run=ngày mai → slot hôm nay bị skip vĩnh viễn. Catch-up này
    fix bằng cách: đọc marker file → nếu hôm nay đã > 16:45 + marker khác
    ngày hôm nay → chạy bù.

    An toàn: chạy 1 lần khi boot, idempotent qua marker file.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path as _Path
    try:
        now_vn = _dt.now(_tz.utc) + _td(hours=7)
        date_today = now_vn.strftime("%Y-%m-%d")
        cutoff = now_vn.replace(hour=16, minute=45, second=0, microsecond=0)
        if now_vn < cutoff:
            log.info("[catchup_returns] Hiện %s chưa qua 16:45 — bỏ qua catchup",
                     now_vn.strftime("%H:%M"))
            return
        marker = _Path(_SYNC_RETURNS_1645_MARKER)
        last = ""
        if marker.exists():
            try:
                last = marker.read_text().strip()
            except Exception:
                last = ""
        if last == date_today:
            log.info("[catchup_returns] Hôm nay %s đã chạy rồi (marker=%s) — bỏ qua",
                     date_today, last)
            return
        log.info("[catchup_returns] Boot lúc %s, đã qua 16:45, marker=%r ≠ today %s "
                 "→ CHẠY BÙ ngay",
                 now_vn.strftime("%H:%M:%S"), last, date_today)
        job_sync_returns_evening_1645()
    except Exception as e:
        log.error("[catchup_returns] CRASH: %s", e, exc_info=True)


def job_fast_new_orders() -> None:
    """Sync nhanh status 9 (chờ chuyển hàng) — chỉ lấy hôm nay + hôm qua, cực nhẹ.

    Mục đích duy nhất: đơn mới tạo trên POS → vào DB ngay để NV quét được.
    Không auto-confirm, không xử lý status 2 — chỉ INSERT đơn status 9.
    Mỗi vòng ~3-5s (83 shop song song, status 9 only, 2 ngày).
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range as _sync
        now_vn = _dt.now(_tz.utc) + _td(hours=7)
        date_today = now_vn.strftime("%Y-%m-%d")
        date_yesterday = (now_vn - _td(days=1)).strftime("%Y-%m-%d")
        result = _sync(date_yesterday, date_today,
                       active_only=False, shipped_only=True, waiting_only=True)
        ins = result.get("inserted", 0)
        if ins:
            log.info("[fast-new-orders] +%s đơn mới status 9", ins)
    except Exception as e:
        log.error("[fast-new-orders] lỗi: %s", e)


def job_fast_shipped_outbound() -> None:
    """Fast-shipped sync — fetch status 9+2 trong 7 ngày, auto-confirm pre_confirmed+shipped.

    THAY THẾ background thread `wh-fast-sync` trước đây trong gunicorn worker.
    Chỉ fetch status 9 (chờ chuyển) + 2 (shipped) trong 7 ngày → bắt đơn ĐVVC vừa lấy.
    Skip fast nếu không có đơn pending/pre_confirmed cần xử lý (đỡ tải Pancake API).

    Chạy in-process trong scheduler service (không subprocess) → latency thấp.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        from modules.kho_vat_ly.wh_db import wh_db as _db
        from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range as _sync_ob

        # Smart skip: nếu không có đơn pending/pre_confirmed thì bỏ vòng này
        with _db() as _chk:
            row = _chk.execute("""
                SELECT COUNT(*) AS c FROM wh_outbound_requests
                WHERE status IN ('pending','pre_confirmed')
                  AND pancake_status IN ('waiting','confirmed')
                LIMIT 1
            """).fetchone()
        if not row or (row["c"] or 0) <= 0:
            log.info("[fast-shipped] skip — không có đơn pending/pre_confirmed")
            return

        now_vn = _dt.now(_tz.utc) + _td(hours=7)
        date_to = now_vn.strftime("%Y-%m-%d")
        date_from = (now_vn - _td(days=7)).strftime("%Y-%m-%d")

        result = _sync_ob(date_from, date_to, active_only=False, shipped_only=True)
        ins = result.get("inserted", 0)
        upd = result.get("updated", 0)
        errs = result.get("errors", []) or []
        log.info("[fast-shipped] +%s mới, %s cập nhật, %s lỗi shop", ins, upd, len(errs))
    except Exception as e:
        log.error("[fast-shipped] lỗi: %s", e)


def job_fast_returns() -> None:
    """Fast-returns sync mỗi 5 phút — bắt đơn hoàn về kho nhanh.

    THAY THẾ background thread `wh-fast-returns` trước đây.
    Cửa sổ 30 ngày (đơn hoàn có thể đến muộn 1 tháng), fast_only=True.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range as _sync_ret
        now_vn = _dt.now(_tz.utc) + _td(hours=7)
        date_to = now_vn.strftime("%Y-%m-%d")
        date_from = (now_vn - _td(days=30)).strftime("%Y-%m-%d")
        result = _sync_ret(date_from, date_to, fast_only=True)
        ins = result.get("inserted", 0)
        errs = result.get("errors", []) or []
        log.info("[fast-returns] +%s mới, %s lỗi shop", ins, len(errs))
    except Exception as e:
        log.error("[fast-returns] lỗi: %s", e)


def job_sync_products() -> None:
    """Sync sản phẩm + tồn POS từ Pancake mỗi 3h — thay thế `wh-products-sync`.

    Cập nhật wh_products (SKU, tên, giá) + wh_shop_inventory (tồn POS) cho từng shop.
    """
    try:
        from modules.kho_vat_ly.wh_db import wh_db as _db
        from modules.kho_vat_ly.wh_sync_pos import sync_shop_inventory_to_db
        with _db() as conn:
            shops = conn.execute(
                "SELECT * FROM wh_shops WHERE status='active' ORDER BY shop_name"
            ).fetchall()
        total_synced = 0
        total_removed = 0
        total_errors = []
        for shop in shops:
            api_key = str(shop.get("pos_api_key") or "").strip()
            if not api_key:
                continue
            try:
                with _db() as c2:
                    result = sync_shop_inventory_to_db(
                        c2,
                        shop_db_id=shop["id"],
                        pos_shop_id=shop["pos_shop_id"],
                        api_key=api_key,
                    )
                total_synced += result.get("synced", 0)
                total_removed += int(result.get("removed") or 0)
                if result.get("errors"):
                    total_errors.append(f"{shop['shop_name']}: {result['errors'][0]}")
            except Exception as e_shop:
                total_errors.append(f"{shop['shop_name']}: {e_shop}")
        log.info("[products-sync] +%s SP, %s dòng tồn xóa, %s shop lỗi",
                 total_synced, total_removed, len(total_errors))
    except Exception as e:
        log.error("[products-sync] lỗi: %s", e)


def job_variant_backfill() -> None:
    """Điền variant_name còn thiếu trong wh_variation_map — thay thế startup backfill.

    Chạy 4h sáng hằng ngày (sau nightly outbound recheck 02:30).
    """
    try:
        from modules.kho_vat_ly import _startup_variation_backfill
        _startup_variation_backfill()
        log.info("[variant-backfill] xong")
    except Exception as e:
        log.error("[variant-backfill] lỗi: %s", e)


def job_full_snapshot() -> None:
    """Weekly full snapshot — code + secrets + system configs → Drive."""
    _run("full_snapshot", ["scripts/full_snapshot.sh"], shell_script=True, timeout=1800)


def job_backup_db() -> None:
    """Backup PostgreSQL hàng ngày 02:00 sáng (TRƯỚC nightly sync 02:30).

    Rotate 7 daily / 4 weekly / 3 monthly trong /mnt/nvme/backup/postgres/.
    Verify integrity sau dump bằng pg_restore --list.
    """
    _run("backup_db", ["scripts/backup_db.sh"], shell_script=True)


def _auto_confirm_shipped_orders() -> dict:
    """Tự động confirm đơn pre_confirmed mà ĐVVC đã lấy (pancake_status shipped/received).
    CHỈ pre_confirmed — đơn pending (NV chưa quét) giữ nguyên là "Lệch POS" để điều tra.
    Bắt buộc NV phải quét qua bước "Đã chuẩn bị" trước khi trừ tồn.
    """
    import datetime as _dt
    from modules.kho_vat_ly.wh_db import wh_db as _db
    from modules.kho_vat_ly import (
        _pick_warehouse_for_outbound,
        _get_variant_inv_outbound,
        _upsert_inventory,
        now_vn,
    )

    confirmed_by = "auto_scheduler"
    done = 0
    skipped_low_stock = 0

    with _db() as conn:
        pending_items = conn.execute("""
            SELECT * FROM wh_outbound_requests
            WHERE pancake_status IN ('shipped','received')
              AND status = 'pre_confirmed'
            ORDER BY order_code, id
        """).fetchall()

        if not pending_items:
            return {"done": 0, "total": 0}

        total = len(pending_items)
        for item in pending_items:
            product_id = item["product_id"]
            # Dùng carrier_picked_up_at làm confirmed_at (đúng thời điểm ĐVVC lấy hàng)
            confirmed_at = item.get("carrier_picked_up_at") or now_vn()

            # Không map sản phẩm → xác nhận luôn, không trừ tồn
            if not product_id:
                conn.execute(
                    "UPDATE wh_outbound_requests SET status='confirmed', qty_confirmed=%s,"
                    " confirmed_by=%s, confirmed_at=%s WHERE id=%s",
                    (item["qty_ordered"] or 1, confirmed_by, confirmed_at, item["id"])
                )
                done += 1
                continue

            qty_needed = item["qty_ordered"] or 0
            if qty_needed <= 0:
                continue

            pos_var_id = (item.get("pos_variation_id") or "").strip() or None
            wh_id, qty_before = _pick_warehouse_for_outbound(conn, product_id, pos_var_id, qty_needed)

            # Lấy row_id để UPDATE trực tiếp — tránh tạo row mới sau khi tách SP
            inv_xp, _ = _get_variant_inv_outbound(conn, product_id, wh_id, pos_var_id)
            row_id = inv_xp["id"] if inv_xp else None

            # Thiếu tồn → KHÔNG bù, KHÔNG confirm — giữ pre_confirmed để NV xử lý thủ công
            if qty_before < qty_needed:
                log.warning(
                    "[AUTO-CONFIRM SKIP] đơn %s sp_id=%s: tồn %s < cần %s — bỏ qua, chờ NV xử lý",
                    item["order_code"], product_id, qty_before, qty_needed
                )
                skipped_low_stock += 1
                continue

            qty_after = qty_before - qty_needed
            _upsert_inventory(conn, product_id, wh_id, qty_after, pos_var_id,
                              inventory_row_id=row_id)
            conn.execute("""
                INSERT INTO wh_stock_movements
                  (type, product_id, warehouse_id, qty, qty_before, qty_after,
                   ref_order_id, note, created_at)
                VALUES ('outbound_confirmed',%s,%s,%s,%s,%s,%s,%s,%s)
            """, (product_id, wh_id, qty_needed, qty_before, qty_after,
                  item["order_code"], "Scheduler auto-confirm khi ĐVVC lấy hàng", confirmed_at))
            conn.execute(
                "UPDATE wh_outbound_requests SET status='confirmed', qty_confirmed=%s,"
                " confirmed_by=%s, confirmed_at=%s, warehouse_id=%s WHERE id=%s",
                (qty_needed, confirmed_by, confirmed_at, wh_id, item["id"])
            )
            done += 1

    return {"done": done, "total": total, "skipped_low_stock": skipped_low_stock}


def job_backfill_order_warehouse() -> None:
    """Backfill warehouse_id=NULL trên đơn pending/pre_confirmed từ wh_shops.

    Safety net: nếu sync tạo đơn thiếu warehouse_id (bug cũ, shop mới chưa cấu hình,
    webhook edge case...) → job này sửa tự động mỗi giờ trước khi NV quét.
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE wh_outbound_requests o
                    SET warehouse_id = ws.warehouse_id
                    FROM wh_shops ws
                    WHERE o.shop_id = ws.id
                      AND o.warehouse_id IS NULL
                      AND ws.warehouse_id IS NOT NULL
                      AND o.status IN ('pending', 'pre_confirmed')
                """)
                n = cur.rowcount
        if n:
            log.warning("[backfill_warehouse] fixed %d orders với warehouse_id=NULL", n)
    except Exception as exc:
        log.error("[backfill_warehouse] ERROR: %s", exc, exc_info=True)


def job_sync_kho_outbound() -> None:
    """Sync trạng thái ĐVVC — chia 4 batch subprocess sequential.

    Lý do batched (thay vì 1 subprocess sync 87 shop):
    • 87 shop × 5 status = 435 fetch tasks → _fetched_results accumulate balloon 17GB
    • Chia 4 batch × ~22 shop × 5 status = ~110 task/batch → peak ~100MB/batch
    • Subprocess exit giữa batch → kernel reclaim RAM → batch kế tiếp start sạch
    • Cursor incremental vẫn dùng (Redis shared) → mỗi batch chỉ fetch đơn mới
    """
    shop_ids = _get_active_pos_shop_ids()
    if not shop_ids:
        log.warning("[sync_outbound_smart] không có shop active")
        return
    batches = _chunk_list(shop_ids, 4)
    log.info("[sync_outbound_smart] %d shop chia %d batch (~%d shop/batch)",
             len(shop_ids), len(batches), len(batches[0]) if batches else 0)
    for idx, batch in enumerate(batches, start=1):
        _run(f"sync_outbound_smart.b{idx}",
             ["scripts/sync_orders_smart.py", "--source", "outbound",
              "--shop-ids", ",".join(batch)],
             timeout=1800,
             reset_cursor_shop_ids=batch)

    # Sau khi sync pancake_status → tự động confirm đơn ĐVVC đã lấy
    try:
        r = _auto_confirm_shipped_orders()
        if r["total"] > 0:
            log.info(
                "[scheduler] auto_confirm_shipped: done=%s/%s skipped_low_stock=%s",
                r["done"], r["total"], r.get("skipped_low_stock", 0),
            )
    except Exception as exc:
        log.error("[scheduler] ERROR auto_confirm_shipped: %s", exc, exc_info=True)


def job_sync_kho_nightly() -> None:
    """Recheck toàn bộ 90 ngày — chạy 02:30 AM mỗi ngày.

    Safety net: bắt các đơn bị sót trạng thái (hoàn/huỷ/đổi COD)
    mà regular 14-ngày có thể bỏ qua.
    Subprocess exit → OS thu hồi RAM hoàn toàn sau khi xong.

    OVERRIDE WH_SYNC_USE_CURSOR=0 → ignore cursor, fetch full 90 ngày để bảo
    đảm reconciliation đầy đủ (Stripe pattern: backfill + events polling tách biệt).
    """
    # Nightly cũng chia batch để safe (full 90 ngày × 8 status × 87 shop sẽ rất nặng)
    shop_ids = _get_active_pos_shop_ids()
    batches = _chunk_list(shop_ids, 4)
    log.info("[nightly] %d shop chia %d batch full 90d", len(shop_ids), len(batches))
    for idx, batch in enumerate(batches, start=1):
        _run(f"sync_outbound_nightly.b{idx}",
             ["scripts/sync_orders_smart.py", "--mode", "full", "--source", "outbound",
              "--shop-ids", ",".join(batch)],
             env_extra={"WH_SYNC_USE_CURSOR": "0"},
             timeout=3600,
             reset_cursor_shop_ids=batch)


def job_refresh_live_pos_status() -> None:
    """Refresh live_pos_status.json — đếm đơn theo status từ Pancake API tất cả shops.

    Cập nhật số liệu "Chờ chuyển hàng / Đã gửi / Đã nhận..." trên dashboard.
    Chạy mỗi 10 phút để dashboard luôn phản ánh thực tế Pancake POS.

    Lưu ý: fetch tuần tự 90 shop × 1 POST request — mất ~30-60 giây.
    max_instances=1 đảm bảo không overlap.
    """
    try:
        # Gọi trực tiếp không cần Flask request context.
        # fetch_live_pos_status() tự fallback load_shop_meta_map() khi RuntimeError.
        from app_ctx import fetch_live_pos_status as _fetch, save_live_pos_status as _save
        data = _fetch()
        _save(data)
        s9 = data.get("status_counts", {}).get("9", 0)
        log.info(
            "[live-pos-status] refresh OK — %d shops, s9=%d, synced_at=%s",
            data.get("shop_count", 0), s9, data.get("synced_at", "?"),
        )
    except Exception as e:
        log.error("[live-pos-status] lỗi refresh: %s", e)


def start_scheduler() -> BackgroundScheduler:
    """Khởi động scheduler. Gọi 1 lần khi app start."""
    scheduler = BackgroundScheduler(timezone=VN_TZ)

    # ── Đồng bộ dữ liệu POS (mỗi 15 phút) ───────────────────────────────────
    scheduler.add_job(
        job_auto_refresh,
        CronTrigger(minute="*/15", timezone=VN_TZ),
        id="auto_refresh",
        name="Sync POS data (every 15 min)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # ── Resync 30 ngày gần nhất (3 lần/ngày: 04:00, 12:00, 20:00) ───────────
    # Bắt đơn hoàn/huỷ ngày cũ. auto_refresh chính chạy nhưng nhiều khi POS API
    # rolling không đủ rộng hoặc dashboard cache stale. Job này force explicit.
    scheduler.add_job(
        job_resync_recent_30days,
        CronTrigger(hour="4,12,20", minute=5, timezone=VN_TZ),
        id="resync_recent_30days",
        name="Resync 30 ngày gần nhất từ POS (04:05/12:05/20:05)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Resync TỪNG ĐƠN 3-8 tuần tuổi vào bảng orders (03:20 đêm) ──────────
    # Bắt đơn hoàn muộn cho lương 2B (hoàn thật thay hoàn ước). Đêm vắng tải.
    scheduler.add_job(
        job_resync_old_orders,
        CronTrigger(hour=3, minute=20, timezone=VN_TZ),
        id="resync_old_orders",
        name="Resync đơn 3-8 tuần tuổi vào orders (03:20)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Đối soát ĐẦU VÀO đơn vs POS analytics + tự chữa (04:40 đêm) ────────
    scheduler.add_job(
        job_doi_soat_orders,
        CronTrigger(hour=4, minute=40, timezone=VN_TZ),
        id="doi_soat_orders",
        name="Đối soát số đơn orders vs POS 60 ngày + tự re-sync ngày lệch (04:40)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Lan nhắc NV báo chi phí trong ngày (20:00 VN) ───────────────────────
    scheduler.add_job(
        job_zalo_expense_reminder,
        CronTrigger(hour=20, minute=0, timezone=VN_TZ),
        id="zalo_expense_reminder",
        name="Zalo expense reminder NV (20:00 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Lan gửi tổng hợp NS vào Zalo group team (21:05 VN) ──────────────────
    scheduler.add_job(
        job_zalo_team_digest,
        CronTrigger(hour=21, minute=5, timezone=VN_TZ),
        id="zalo_team_digest",
        name="Zalo team digest NS ngày mai (21:05 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Đồng bộ đơn vào DB (21:25 VN) ───────────────────────────────────────
    scheduler.add_job(
        job_sync_orders_db,
        CronTrigger(hour=21, minute=25, timezone=VN_TZ),
        id="sync_orders_db",
        name="Sync orders → DB (21:25 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Marketing Brain: cảnh báo ad đốt tiền 0 đơn (08:30 VN) ──────────────
    scheduler.add_job(
        job_mb_alert_burning_ads,
        CronTrigger(hour=8, minute=30, timezone=VN_TZ),
        id="mb_alert_burning_ads",
        name="MB cảnh báo ad đốt tiền 0 đơn (08:30 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── ĐÃ TẮT 07/09 (Long): job kho vật lý clone từ tieuhiemsoft — moon không dùng
    #    kho, DB thiếu bảng `sync_state` → crash mỗi lần chạy, spam log vô ích.
    # scheduler.add_job(
    #     job_sync_wh_returns,
    #     CronTrigger(minute="*/30", timezone=VN_TZ),
    #     id="sync_wh_returns",
    #     name="Sync wh_return_receipts từ Pancake (mỗi 30 phút, subprocess smart)",
    #     replace_existing=True, max_instances=1, misfire_grace_time=600,
    # )

    # ── Force sync hàng hoàn 16:45 VN — buffer trước NV quét 18:00 ──────────
    # Tách riêng khỏi cron 30 phút để chắc chắn data đủ trước khung quét tối.
    # Gọi cùng API sync_returns_for_date_range — KHÔNG động logic gốc.
    scheduler.add_job(
        job_sync_returns_evening_1645,
        CronTrigger(hour=16, minute=45, timezone=VN_TZ),
        id="sync_returns_evening_1645",
        name="Force sync hàng hoàn buffer trước NV quét tối (16:45 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Sync wh_outbound_requests trạng thái ĐVVC (mỗi 30 phút, subprocess smart) ─
    # Cập nhật shipped→received/returned nhanh hơn, KPI "Đang giao" luôn chính xác.
    # ĐÃ TẮT 07/09 (Long) — moon không dùng kho, thiếu bảng `sync_state` → crash.
    # scheduler.add_job(
    #     job_sync_kho_outbound,
    #     CronTrigger(minute="*/30", timezone=VN_TZ),
    #     id="sync_kho_outbound",
    #     name="Sync wh_outbound_requests ĐVVC status (mỗi 30 phút, subprocess smart)",
    #     replace_existing=True, max_instances=1, misfire_grace_time=600,
    # )

    # ── Backfill warehouse_id=NULL (mỗi giờ) — safety net ───────────────────
    scheduler.add_job(
        job_backfill_order_warehouse,
        CronTrigger(minute=5, timezone=VN_TZ),
        id="backfill_order_warehouse",
        name="Backfill warehouse_id=NULL trên đơn pending (mỗi giờ)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # ── Nightly full recheck 90 ngày — bắt đơn bị sót trạng thái (02:30 AM) ─
    scheduler.add_job(
        job_sync_kho_nightly,
        CronTrigger(hour=2, minute=30, timezone=VN_TZ),
        id="sync_kho_nightly",
        name="Nightly full recheck 90 ngày outbound (02:30 AM)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Fast-new-orders 10 giây — chỉ status 9 hôm nay, cực nhẹ ────────────
    # Mục đích: đơn mới trên POS → DB ngay để NV quét được (mỗi vòng ~3-5s)
    # ĐÃ TẮT 07/09 (Long) — moon không dùng kho, thiếu bảng `wh_products` → crash
    # 8.600 lần/ngày (10s/lần). Đơn Ladi KHÔNG dùng job này (dùng ladi_sync_orders).
    # scheduler.add_job(
    #     job_fast_new_orders,
    #     IntervalTrigger(seconds=10, timezone=VN_TZ),
    #     id="fast_new_orders",
    #     name="Fast-new-orders (10s) — sync status 9 hôm nay để NV quét",
    #     replace_existing=True, max_instances=1, misfire_grace_time=30,
    # )

    # ── Fast-shipped 10 giây — chờ chuyển hàng → đã giao ĐVVC ───────────────
    # Khớp default FAST_SYNC_INTERVAL trong modules/kho_vat_ly/__init__.py.
    scheduler.add_job(
        job_fast_shipped_outbound,
        IntervalTrigger(seconds=10, timezone=VN_TZ),
        id="fast_shipped_outbound",
        name="Fast-shipped sync (10s) — chờ chuyển hàng → đã giao",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    # ── Fast-returns 10 giây — khớp luồng fast-shipped ───────────────────────
    # ĐÃ TẮT 07/09 (Long) — moon không dùng kho, crash 1.200+ lần/ngày.
    # scheduler.add_job(
    #     job_fast_returns,
    #     IntervalTrigger(seconds=10, timezone=VN_TZ),
    #     id="fast_returns",
    #     name="Fast-returns sync (10s) — bắt đơn hoàn về kho",
    #     replace_existing=True, max_instances=1, misfire_grace_time=60,
    # )

    # ── Products + tồn POS mỗi 3h — thay thế wh-products-sync ──────────────
    scheduler.add_job(
        job_sync_products,
        CronTrigger(minute=15, hour="*/3", timezone=VN_TZ),
        id="sync_products",
        name="Sync sản phẩm + tồn POS (mỗi 3h)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Variant backfill 4h sáng — thay thế _startup_variation_backfill ────
    scheduler.add_job(
        job_variant_backfill,
        CronTrigger(hour=4, minute=0, timezone=VN_TZ),
        id="variant_backfill",
        name="Backfill variant_name (04:00 hằng ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Budget chat digest 21:00 — tổng ngân sách ngày mai ───────────────
    scheduler.add_job(
        job_budget_chat_digest,
        CronTrigger(hour=21, minute=0, timezone=VN_TZ),
        id="budget_chat_digest",
        name="Telegram digest ngân sách ngày mai (21:00 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=900,
    )

    # ── Backup PostgreSQL 02:00 sáng (trước nightly sync 02:30) ────────────
    # pg_dump → /mnt/nvme/backup/postgres/ rotate 7/4/3 (daily/weekly/monthly)
    scheduler.add_job(
        job_backup_db,
        CronTrigger(hour=2, minute=0, timezone=VN_TZ),
        id="backup_db",
        name="Backup PostgreSQL (02:00 hằng ngày, rotate 7/4/3)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )

    # ── Full snapshot weekly (Chủ nhật 03:00) — code + secrets + system → Drive ──
    scheduler.add_job(
        job_full_snapshot,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=VN_TZ),
        id="full_snapshot",
        name="Full website snapshot (Chủ nhật 03:00, upload Drive)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Cleanup đơn ảo waiting hàng đêm (03:30 VN) ─── P1-4 fix 2026-05-10 ──
    # fast_cleanup_waiting.py: fetch live status=9 từ POS, bulk-update stale waiting → received,
    # rồi mark tracking='' + carrier='' → auto_cleaned. Ngăn tích lũy ~150 đơn ảo/ngày.
    scheduler.add_job(
        job_cleanup_waiting_orders,
        CronTrigger(hour=3, minute=30, timezone=VN_TZ),
        id="cleanup_waiting",
        name="Cleanup đơn ảo waiting (03:30 hằng ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )

    # ── DB Healthcheck (06:30 VN) ─────────────────────────────────────────────
    scheduler.add_job(
        job_db_healthcheck,
        CronTrigger(hour=6, minute=30, timezone=VN_TZ),
        id="db_healthcheck",
        name="DB healthcheck (06:30 VN)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Telegram jobs — đã bật lại (2026-04-30) ─────────────────────────────
    # Trước đây tắt vì _run() dùng capture_output=True → buffer RAM. Đã fix
    # sang stdout=DEVNULL, stderr=PIPE → an toàn.
    scheduler.add_job(job_daily_summary,  CronTrigger(hour=7,  minute=30, timezone=VN_TZ), id="daily_summary",  name="Telegram: Daily summary",   replace_existing=True, max_instances=1, misfire_grace_time=600)
    scheduler.add_job(job_4day_summary,   CronTrigger(hour=9,  minute=0,  timezone=VN_TZ), id="4day_summary",   name="Telegram: 4-day summary (T-4)", replace_existing=True, max_instances=1, misfire_grace_time=600)
    scheduler.add_job(job_order_report,   CronTrigger(hour=20, minute=15, timezone=VN_TZ), id="order_report",   name="Telegram: Order report",     replace_existing=True, max_instances=1, misfire_grace_time=600)
    # TẠM TẮT (2026-05-14): slow_sales đang silent fail (PROJECT_DIR sai path),
    # stock_alert đọc JSON legacy không tin được. User sẽ nghiên cứu lại trước
    # khi bật lại — uncomment khi đã rewrite theo chuẩn DB-only.
    # scheduler.add_job(job_slow_sales,     CronTrigger(hour=8,  minute=0,  timezone=VN_TZ), id="slow_sales",     name="Telegram: Slow sales",       replace_existing=True, max_instances=1, misfire_grace_time=600)
    # scheduler.add_job(job_stock_alert,    CronTrigger(hour=9,  minute=0,  timezone=VN_TZ), id="stock_alert",    name="Telegram: Stock alert",      replace_existing=True, max_instances=1, misfire_grace_time=600)
    scheduler.add_job(job_ads_alert,      CronTrigger(hour=20, minute=30, timezone=VN_TZ), id="ads_alert",      name="Telegram: Ads alert",        replace_existing=True, max_instances=1, misfire_grace_time=600)
    scheduler.add_job(job_top_report,     CronTrigger(hour=20, minute=45, timezone=VN_TZ), id="top_report",     name="Telegram: Top report",       replace_existing=True, max_instances=1, misfire_grace_time=600)

    # ── KPI Alert: NV doanh thu thấp ──────────────────────────────────
    # Tuần: T2 hàng tuần 19:00 VN, báo cáo kỳ Mon→Sun TUẦN TRƯỚC vừa kết thúc.
    #   Vd: ngày 8/6 (T2) báo cáo kỳ 1/6→7/6, so với 25/5→31/5.
    # Tháng: ngày 1 hàng tháng 19:00 VN, báo cáo tháng trước vừa kết thúc.
    #   Vd: ngày 1/7 báo cáo kỳ 1/6→30/6, so với 1/5→31/5.
    scheduler.add_job(
        job_weekly_kpi_alert,
        CronTrigger(day_of_week='mon', hour=19, minute=0, timezone=VN_TZ),
        id="weekly_kpi_alert",
        name="Telegram: Báo cáo TUẦN NV doanh thu thấp",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_monthly_kpi_alert,
        CronTrigger(day=1, hour=19, minute=0, timezone=VN_TZ),
        id="monthly_kpi_alert",
        name="Telegram: Báo cáo THÁNG NV doanh thu thấp",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Auto-sync chi phí QC Facebook Ads — hôm nay + hôm qua (6 lần/ngày) ───
    scheduler.add_job(
        job_sync_fb_ads_today,
        CronTrigger(hour="7,10,13,16,19,22", minute=10, timezone=VN_TZ),
        id="sync_fb_ads_today",
        name="Auto-sync FB Ads hôm nay+hôm qua (6 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Auto-map TK QC → shop POS cho NV single-shop (chống "Chưa gán shop") ──
    #    Chạy mỗi giờ ở phút 5 (trước lượt sync FB ở phút 10).
    scheduler.add_job(
        job_auto_map_ads_shops,
        CronTrigger(minute=5, timezone=VN_TZ),
        id="auto_map_ads_shops",
        name="Auto-map TK QC → shop NV (single-shop, mỗi giờ)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # ── Enrich fb_pages (tên + ảnh page) cho page mới — 4 lần/ngày ───────────
    scheduler.add_job(
        job_sync_fb_page_info,
        CronTrigger(hour="6,12,18,22", minute=20, timezone=VN_TZ),
        id="sync_fb_page_info",
        name="Enrich tên + ảnh page (4 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Auto-sync spend TK pa_ad_accounts (theo dõi) — 3 lần/ngày ──────────────
    scheduler.add_job(
        job_sync_pa_account_insights,
        CronTrigger(hour="8,13,20", minute=30, timezone=VN_TZ),
        id="sync_pa_account_insights",
        name="Auto-sync spend TK BM (theo dõi, 3 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Kéo POS theo page (đối chiếu CP Ads FB) — 4 lần/ngày, 3 ngày gần nhất ──
    scheduler.add_job(
        job_sync_pos_page_metrics,
        CronTrigger(hour="7,11,15,19,22", minute=40, timezone=VN_TZ),
        id="sync_pos_page_metrics",
        name="Kéo POS theo page (đối chiếu FB, 5 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Map page → sản phẩm bán nhiều nhất (cột Sản phẩm trang shop) — 1 lần/ngày 6:20 ──
    scheduler.add_job(
        job_sync_page_top_product,
        CronTrigger(hour=6, minute=20, timezone=VN_TZ),
        id="sync_page_top_product",
        name="Map page → sản phẩm (Pancake, 1 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Check link landing SỐNG/CHẾT (Page & Ads › Landing) — 2 lần/ngày 8:10 & 20:10 ──
    scheduler.add_job(
        job_check_landing_links,
        CronTrigger(hour="8,20", minute=10, timezone=VN_TZ),
        id="check_landing_links",
        name="Check link landing sống/chết (2 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── Sync objective campaign (phân loại ladipage vs tương tác) — 1 lần/ngày 6:40 ──
    scheduler.add_job(
        job_sync_fb_campaign_objective,
        CronTrigger(hour=6, minute=40, timezone=VN_TZ),
        id="sync_fb_campaign_objective",
        name="Sync objective campaign FB (1 lần/ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # ── ĐÃ TẮT 28/08 (sếp Phong): job auto-whitelist "chạy >=2 ngày" biến MỌI page
    #    (kể cả page ladi lạ) thành "page công ty" → tab Page lạ trống trơn.
    #    Page công ty giờ = quản trị được: fb_pages / pa_pages / gán shop / whitelist TAY.

    # ── Snapshot ngân sách TK QC (spend_cap còn lại) — 1 lần/ngày 19:30 ──
    scheduler.add_job(
        job_sync_fb_budget,
        CronTrigger(hour=19, minute=30, timezone=VN_TZ),
        id="sync_fb_budget",
        name="Snapshot ngân sách TK QC (19:30 mỗi ngày)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=900,
    )

    # ── Pre-compute dashboard cache (warm cache cho scope admin) — mỗi 5 phút ─
    # Web worker đọc từ Redis cache thay vì compute build_ads_delay_data +
    # build_monthly_loss_data (~100MB/request) → giảm RAM peak workers.
    # Chạy ngay sau khi scheduler start để có cache ngay lần đầu.
    scheduler.add_job(
        job_warm_dashboard_cache,
        IntervalTrigger(minutes=5),
        id="warm_dashboard_cache",
        name="Pre-compute dashboard alert cache (5 phút)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
        next_run_time=__import__("datetime").datetime.now(),  # chạy ngay
    )

    # ── Refresh live_pos_status.json — số đơn theo status từ Pancake API ────────
    # Dashboard đọc file này để hiển thị "Chờ chuyển hàng / Đã gửi / Đã nhận...".
    # Chạy mỗi 10 phút để số liệu luôn khớp Pancake POS thực tế.
    scheduler.add_job(
        job_refresh_live_pos_status,
        IntervalTrigger(minutes=10),
        id="refresh_live_pos_status",
        name="Refresh live_pos_status.json (10 phút)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
        next_run_time=__import__("datetime").datetime.now(),  # chạy ngay khi khởi động
    )

    # ── Backfill toàn bộ tháng trước — chạy ngày mồng 1 lúc 02:00 ────────────
    scheduler.add_job(
        job_sync_fb_ads_backfill_month,
        CronTrigger(day=1, hour=2, minute=0, timezone=VN_TZ),
        id="sync_fb_ads_prev_month",
        name="Backfill FB Ads tháng trước (mồng 1 hàng tháng 02:00)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Backfill 7 ngày qua — mỗi Chủ nhật 03:00 ─────────────────────────────
    scheduler.add_job(
        job_sync_fb_ads_backfill_week,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=VN_TZ),
        id="sync_fb_ads_week",
        name="Backfill FB Ads 7 ngày qua (Chủ nhật 03:00)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # ── Sweep dup_pending_* stale ở app_config — mỗi 30 phút ───────────────
    # expense_chat ghi state hỏi 'là 1 hay 2 khoản' theo key dup_pending_<uid>.
    # NV không trả lời → row đọng. Sweep để app_config không phình.
    scheduler.add_job(
        job_sweep_dup_pending,
        IntervalTrigger(minutes=30, timezone=VN_TZ),
        id="sweep_dup_pending",
        name="Sweep dup_pending_* (mỗi 30 phút, age > 1h)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        job_lan_ads_report,
        CronTrigger(hour=20, minute=0, timezone=VN_TZ),
        id="lan_ads_report_20h",
        name="Lan báo cáo TỔNG (nhóm + sếp) qua Zalo — 20h00",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        job_ladipage_match,
        CronTrigger(minute="*/15", timezone=VN_TZ),
        id="ladipage_match_15m",
        name="Đối soát đơn LadiPage ↔ POS (mỗi 15 phút)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # ── Đêm 03:15: hỏi thẳng POS cho TOÀN BỘ đơn "chưa lên" (mọi ngày, không giới
    #    hạn 14 ngày) — Long 07/09 "mỗi lần quét kiểm tra đơn cũ không?" → bắt cả
    #    đơn sale lên trễ hàng tháng. ~600-1000 đơn ≈ 3-5 phút, giờ vắng.
    scheduler.add_job(
        job_ladipage_verify_full,
        CronTrigger(hour=3, minute=15, timezone=VN_TZ),
        id="ladipage_verify_full",
        name="Đối soát LadiPage ↔ POS TOÀN BỘ lịch sử (03:15 hằng đêm)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        job_lan_realtime_report,
        CronTrigger(hour="9,11,13,15,17,19", minute=30, timezone=VN_TZ),
        id="lan_realtime_report",
        name="Lan báo LÃI/LỖ trong ngày (9h30–19h30, mỗi 2 tiếng)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=900,
    )

    scheduler.add_job(
        job_lan_week_report,
        CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=VN_TZ),
        id="lan_week_report",
        name="Lan báo cáo TUẦN (thứ Hai 08h30, so tuần trước)",
        replace_existing=True, max_instances=1, misfire_grace_time=3600,
    )

    scheduler.add_job(
        job_lan_month_report,
        CronTrigger(day=1, hour=8, minute=45, timezone=VN_TZ),
        id="lan_month_report",
        name="Lan báo cáo THÁNG (mồng 1 lúc 08h45, so tháng trước)",
        replace_existing=True, max_instances=1, misfire_grace_time=7200,
    )

    scheduler.add_job(
        job_lan_personal_report,
        CronTrigger(hour=8, minute=0, timezone=VN_TZ),
        id="lan_personal_report_8h",
        name="Lan báo cáo RIÊNG từng mar (số hôm qua) — 08h00",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )

    scheduler.start()

    # ── Startup backfill: sync 7 ngày gần nhất khi app khởi động ────────────
    # Chạy sau 60 giây để tránh tranh chấp khi server vừa start
    import threading as _th, datetime as _dt
    def _startup_backfill():
        import time as _time
        _time.sleep(60)
        today = _dt.date.today()
        date_from = today - _dt.timedelta(days=7)
        log.info("[scheduler] Startup backfill FB Ads %s → %s", date_from, today)
        _run("sync_fb_ads_startup", [
            "scripts/sync_fb_ads_by_page.py",
            "--date-from", date_from.strftime("%Y-%m-%d"),
            "--date-to",   today.strftime("%Y-%m-%d"),
        ], timeout=1800)
    _th.Thread(target=_startup_backfill, daemon=True, name="startup-fb-backfill").start()

    # ── Catch-up cron sync_returns_16h45 nếu boot SAU 16:45 mà chưa chạy ────
    # Khi service restart đúng giờ trigger → APScheduler tính next_run=ngày mai,
    # bỏ qua slot hôm nay. Thread này check marker file → chạy bù.
    # Sleep 30s để fast-returns thread + auto_refresh khởi động xong, tránh đụng IO.
    def _startup_catchup_returns():
        import time as _time
        _time.sleep(30)
        _catchup_sync_returns_if_missed()
    _th.Thread(target=_startup_catchup_returns, daemon=True,
               name="startup-catchup-returns-1645").start()

    log.info("[scheduler] Started. Jobs: %s", [j.name for j in scheduler.get_jobs()])
    return scheduler
