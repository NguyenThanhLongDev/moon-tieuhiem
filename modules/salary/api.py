from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import jsonify, redirect, render_template_string, request, session, url_for

from . import service
from .validators import (
    parse_cpqc_tier_form,
    parse_employee_form,
    parse_employee_setting_form,
    parse_product_form,
    parse_salary_config_form,
)


def _actor() -> str:
    return str(session.get("username") or "system")


def _wants_json() -> bool:
    if request.args.get("format") == "json":
        return True
    mt = (request.content_type or "").lower()
    if "application/json" in mt:
        return True
    acc = (request.headers.get("Accept") or "").lower()
    return "application/json" in acc


def _wrap_page(page_template: str, title: str, inner: str) -> str:
    return render_template_string(page_template, title=title, body=inner)


_SALARY_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ACCOUNTING_DOC_PATH = _SALARY_REPO_ROOT / "docs" / "salary_business_for_accounting.md"
_ACCOUNTING_FEEDBACK_LOG = _SALARY_REPO_ROOT / "logs" / "salary_accounting_feedback.jsonl"


def _append_accounting_feedback_row(row: dict) -> None:
    """Lưu một dòng phản hồi kế toán (file JSON Lines trong thư mục logs)."""
    _ACCOUNTING_FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with open(_ACCOUNTING_FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def _load_accounting_feedback_rows_newest_first(limit: int = 200) -> List[Dict[str, Any]]:
    """Đọc file phản hồi; mới nhất trước."""
    if not _ACCOUNTING_FEEDBACK_LOG.is_file():
        return []
    parsed: List[Dict[str, Any]] = []
    try:
        with open(_ACCOUNTING_FEEDBACK_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    parsed.reverse()
    return parsed[:limit]


def _yn(b: Any) -> str:
    return "Có" if bool(b) else "Không"


def _salary_form_money(name: str, default: float = 0.0) -> float:
    raw = (request.form.get(name) or "").strip().replace(",", "").replace(" ", "")
    if raw == "":
        return float(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Số không hợp lệ ({name}).") from exc


def _salary_form_int(name: str, default: int = 0) -> int:
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        return int(default)
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ValueError(f"Số nguyên không hợp lệ ({name}).") from exc


def _prepare_feedback_rows_for_template(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "submitted_at": str(r.get("submitted_at") or ""),
                "submitted_by_login": str(r.get("submitted_by_login") or ""),
                "_yn_read": _yn(r.get("cb_read_principles")),
                "_yn_cpqc": _yn(r.get("cb_agree_cpqc_table")),
                "_yn_proc": _yn(r.get("cb_agree_wait_returns_process")),
                "comment": str(r.get("comment") or ""),
                "signer_name": str(r.get("signer_name") or ""),
                "signer_title": str(r.get("signer_title") or ""),
                "sign_date": str(r.get("sign_date") or ""),
            }
        )
    return out


def _accounting_doc_body_html() -> Tuple[str, Optional[str]]:
    """Đọc tài liệu kế toán (Markdown) → HTML an toàn (bảng, tiêu đề)."""
    if not _ACCOUNTING_DOC_PATH.is_file():
        return (
            "<p>Không tìm thấy tài liệu. Vui lòng kiểm tra file "
            "<code>docs/salary_business_for_accounting.md</code> trên máy chủ.</p>",
            None,
        )
    raw = _ACCOUNTING_DOC_PATH.read_text(encoding="utf-8")
    try:
        import markdown as md_lib  # type: ignore

        body = md_lib.markdown(
            raw,
            extensions=["tables", "nl2br"],
        )
        return body, None
    except Exception:
        pass
    # Fallback không cần thư viện: đoạn văn + thoát HTML
    esc = html.escape(raw)
    esc = re.sub(r"^### (.+)$", r"<h3>\1</h3>", esc, flags=re.MULTILINE)
    esc = re.sub(r"^## (.+)$", r"<h2>\1</h2>", esc, flags=re.MULTILINE)
    esc = re.sub(r"^# (.+)$", r"<h1>\1</h1>", esc, flags=re.MULTILINE)
    esc = "<pre class=\"salary-doc-fallback\" style=\"white-space:pre-wrap;font-family:inherit;\">" + esc + "</pre>"
    return (
        esc,
        "Thư viện <code>markdown</code> chưa cài — hiển thị dạng văn bản. Chạy: pip install markdown",
    )


SHELL_START = """
<div class="topbar dashboard-topbar">
  <div class="dashboard-shell">
    <div class="dashboard-title-row">
      <div class="title"><h1>{{ title }}</h1></div>
    </div>
    <div class="controls">
      <div class="dashboard-menu-grid">
        <a class="button-link" href="/">← Dashboard</a>
        <a class="button-link" href="{{ url_for('salary_page') }}">Lương / KPI</a>
        <a class="button-link" href="{{ url_for('salary_periods_list') }}">Kỳ lương</a>
        <a class="button-link" href="{{ url_for('salary_config_list') }}">Cấu hình lương</a>
        <a class="button-link" href="{{ url_for('salary_products') }}">Sản phẩm KPI</a>
        <a class="button-link" href="{{ url_for('salary_employees') }}">Nhân sự</a>
        <a class="button-link" href="{{ url_for('salary_employee_settings') }}">Lương nhân viên</a>
        <a class="button-link" href="{{ url_for('salary_doc_accounting') }}">Tài liệu kế toán</a>
        <a class="button-link" href="{{ url_for('salary_doc_accounting_feedback_list') }}">Phản hồi kế toán</a>
      </div>
    </div>
  </div>
</div>
<div class="dashboard-shell">
"""

SHELL_END = "</div>"


def register_salary_routes(app, login_required: Callable, page_template: str):
    service.init_salary_module()

    # ── Gate vai trò cho TOÀN BỘ module lương (2026-06-11, Q5 chốt) ───────
    # - admin/manager/kế toán/IT: toàn quyền (sửa tier, chốt kỳ...)
    # - leader/sale_leader: CHỈ XEM (GET) — không sửa/chốt
    # - NV khác: chặn, trừ trang riêng /salary/my (xem bảng lương CỦA MÌNH,
    #   đăng ký bằng _inner_login_required nên không qua gate này)
    from functools import wraps as _wraps
    _SALARY_FULL_ROLES = {"admin", "superadmin", "manager", "ketoan", "accountant", "it"}
    _SALARY_VIEW_ROLES = {"leader", "sale_leader"}
    _inner_login_required = login_required

    def login_required(f):  # che tên — mọi @login_required phía dưới đều qua gate này
        @_inner_login_required
        @_wraps(f)
        def _gated(*args, **kwargs):
            role = str(session.get("role") or "").strip().lower()
            if role in _SALARY_FULL_ROLES:
                return f(*args, **kwargs)
            if role in _SALARY_VIEW_ROLES and request.method == "GET":
                return f(*args, **kwargs)
            if role in _SALARY_VIEW_ROLES:
                return "Leader chỉ được xem — thao tác sửa/chốt lương dành cho kế toán/quản lý.", 403
            return "Bạn không có quyền truy cập module lương. Xem bảng lương của bạn tại /salary/my", 403
        return _gated

    @app.route("/salary/chuan-cpqc", methods=["GET", "POST"])
    @login_required
    def salary_chuan_cpqc():
        """Nhập CHUẨN CPQC hàng loạt — màn hình 1 trang cho kế toán/leader.

        Chuẩn CPQC = số tiền quảng cáo TỐI ĐA chấp nhận để có 1 đơn của SP đó.
        NV chạy thực tế thấp hơn chuẩn → hoa hồng cao; vượt 150% chuẩn → 0 + dừng chạy.
        Cột "CPQC thực tế" = số đang chạy 30 ngày qua để tham khảo khi định chuẩn.
        """
        from db import get_conn as _gc
        msg = err = None
        if request.method == "POST":
            saved = 0
            errors = []
            i = 0
            while True:
                sku = request.form.get(f"sku_{i}")
                if sku is None:
                    break
                std_raw = (request.form.get(f"std_{i}") or "").strip().replace(".", "").replace(",", "")
                i += 1
                if not std_raw:
                    continue
                try:
                    std = float(std_raw)
                    if std < 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"{sku}: '{request.form.get(f'std_{i-1}')}' không phải số")
                    continue
                name = (request.form.get(f"name_{i-1}") or sku).strip()
                gia_nhap = float(request.form.get(f"gn_{i-1}") or 0)
                existing = service.get_product_by_code(sku) if hasattr(service, "get_product_by_code") else None
                if existing is None:
                    from . import repositories as _repo
                    existing = _repo.get_product_salary_config_by_code(sku)
                data = {
                    "product_code": sku,
                    "product_name": name,
                    "import_price": gia_nhap if gia_nhap > 0 else (existing or {}).get("import_price") or 0,
                    "cpqc_standard_per_order": std,
                    "vat_rate": (existing or {}).get("vat_rate", 0.08),
                    "is_commission_enabled": True,
                    "notes": "nhập hàng loạt /salary/chuan-cpqc",
                }
                from . import repositories as _repo
                try:
                    if existing:
                        _repo.update_product_salary_config(int(existing["id"]), data, _actor())
                    else:
                        _repo.create_product_salary_config(data, _actor())
                    saved += 1
                except Exception as exc:
                    errors.append(f"{sku}: {exc}")
            msg = f"Đã lưu chuẩn CPQC cho {saved} sản phẩm."
            if errors:
                err = " · ".join(errors[:5])

        # Tham khảo 30 ngày gần nhất: CP ads phân bổ per SKU + đơn (không tính hoàn) + giá nhập/bán
        with _gc() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH ad_sku AS (
                        SELECT a.ad_id, p.sku, MAX(p.name) AS name,
                               MAX(p.gia_nhap) AS gia_nhap, MAX(p.gia_ban) AS gia_ban,
                               SUM(oi.line_total) AS rev,
                               COUNT(DISTINCT a.order_id) FILTER (
                                   WHERE o.order_status::text NOT IN ('cancelled','returned','returning')
                               ) AS orders_ok
                        FROM mb_order_attribution a
                        JOIN orders o ON o.id = a.order_id
                        JOIN order_items oi ON oi.order_id = a.order_id
                        JOIN wh_variation_map vm ON vm.pos_variation_id = oi.external_variant_id
                        JOIN wh_products p ON p.id = vm.product_id
                        WHERE a.ad_id <> '' AND a.order_date >= CURRENT_DATE - 30
                        GROUP BY a.ad_id, p.sku
                    ),
                    ad_tot AS (SELECT ad_id, SUM(rev) AS tot FROM ad_sku GROUP BY ad_id),
                    sp AS (
                        SELECT ad_id, SUM(spend) * 1.113 AS spend_vat
                        FROM mb_fb_entity_daily
                        WHERE metric_date >= CURRENT_DATE - 30
                        GROUP BY ad_id
                    )
                    SELECT s.sku, MAX(s.name) AS name,
                           MAX(s.gia_nhap)::bigint AS gia_nhap,
                           MAX(s.gia_ban)::bigint AS gia_ban,
                           SUM(s.orders_ok) AS don,
                           ROUND(SUM(COALESCE(sp.spend_vat,0) * s.rev / NULLIF(t.tot,0)))::bigint AS ads_alloc,
                           CASE WHEN SUM(s.orders_ok) > 0
                                THEN ROUND(SUM(COALESCE(sp.spend_vat,0) * s.rev / NULLIF(t.tot,0)) / SUM(s.orders_ok))::bigint
                           END AS cpqc_thuc_te
                    FROM ad_sku s
                    JOIN ad_tot t ON t.ad_id = s.ad_id
                    LEFT JOIN sp ON sp.ad_id = s.ad_id
                    GROUP BY s.sku
                    HAVING SUM(s.orders_ok) > 0
                    ORDER BY ads_alloc DESC NULLS LAST
                """)
                cols = [d[0] for d in cur.description]
                skus = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("SELECT UPPER(product_code), cpqc_standard_per_order FROM product_salary_configs")
                current_std = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        def _vnd0(v):
            try:
                return f"{float(v):,.0f}".replace(",", ".")
            except (TypeError, ValueError):
                return ""

        rows_html = []
        for i, r in enumerate(skus):
            std_now = current_std.get(str(r["sku"]).upper())
            std_val = _vnd0(std_now) if std_now else ""
            actual = r.get("cpqc_thuc_te")
            rows_html.append(
                f"<tr><td><strong>{html.escape(str(r['sku']))}</strong>"
                f"<input type='hidden' name='sku_{i}' value=\"{html.escape(str(r['sku']))}\">"
                f"<input type='hidden' name='name_{i}' value=\"{html.escape(str(r['name'] or '')[:120])}\">"
                f"<input type='hidden' name='gn_{i}' value='{float(r['gia_nhap'] or 0)}'></td>"
                f"<td>{html.escape(str(r['name'] or '')[:38])}</td>"
                f"<td style='text-align:right'>{_vnd0(r['gia_nhap'])}</td>"
                f"<td style='text-align:right'>{_vnd0(r['gia_ban'])}</td>"
                f"<td style='text-align:right'>{r['don']}</td>"
                f"<td style='text-align:right;font-weight:600'>{_vnd0(actual)}</td>"
                f"<td><input name='std_{i}' value='{std_val}' data-actual='{int(actual or 0)}' "
                f"placeholder='nhập chuẩn...' style='width:110px;text-align:right;padding:4px 8px;"
                f"border:1px solid {'#ddd' if std_val else '#f59e0b'};border-radius:6px'></td></tr>"
            )

        inner = (
            '<div class="card">'
            "<h2>🎯 Chuẩn CPQC theo sản phẩm — nhập hàng loạt</h2>"
            "<div style='background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:10px 14px;margin:10px 0;line-height:1.6'>"
            "<b>Chuẩn CPQC là gì?</b> Là số tiền quảng cáo TỐI ĐA công ty chấp nhận bỏ ra để có 1 đơn của sản phẩm đó.<br>"
            "Ví dụ: chuẩn 70.000đ — NV chạy hết 49.000đ/đơn (= 70% chuẩn) → hoa hồng <b>3,5%</b> doanh thu. "
            "Chạy hết 84.000đ/đơn (= 120%) → chỉ còn <b>1,5%</b>. Vượt 105.000đ (150%) → <b>0% + báo dừng chạy</b>.<br>"
            "💡 Cột <b>CPQC thực tế</b> = trung bình NV đang chạy 30 ngày qua — nhìn nó để định chuẩn cho hợp lý. "
            "Ô viền <span style='color:#b45309'>cam</span> = chưa có chuẩn (hoa hồng SP đó đang bị bỏ qua)."
            "</div>"
            + (f"<div class='alert' style='background:#d1fae5;padding:8px 12px;border-radius:8px'>{html.escape(msg)}</div>" if msg else "")
            + (f"<div class='alert' style='background:#fee2e2;padding:8px 12px;border-radius:8px'>{html.escape(err)}</div>" if err else "")
            + "<form method='post'>"
            "<p><button class='button' type='submit' style='background:#F59E0B;border:0;color:#fff;"
            "padding:8px 18px;border-radius:8px;font-weight:700;cursor:pointer'>💾 Lưu tất cả</button> "
            "<button type='button' onclick='goiY()' style='padding:8px 14px;border-radius:8px;cursor:pointer'>"
            "✨ Điền gợi ý = CPQC thực tế (chỉ ô trống)</button> "
            f"<span style='color:#888'>({len(skus)} SP có đơn từ ads 30 ngày qua)</span></p>"
            "<div style='overflow-x:auto'><table class='cc-table' style='width:100%;font-size:13px'>"
            "<tr><th>SKU</th><th>Tên SP</th><th style='text-align:right'>Giá nhập</th>"
            "<th style='text-align:right'>Giá bán</th><th style='text-align:right'>Đơn 30n</th>"
            "<th style='text-align:right'>CPQC thực tế/đơn</th><th>CHUẨN CPQC/đơn</th></tr>"
            + "".join(rows_html)
            + "</table></div>"
            "<p><button class='button' type='submit' style='background:#F59E0B;border:0;color:#fff;"
            "padding:8px 18px;border-radius:8px;font-weight:700;cursor:pointer'>💾 Lưu tất cả</button></p>"
            "</form>"
            "<script>function goiY(){document.querySelectorAll(\"input[name^='std_']\").forEach(function(el){"
            "if(!el.value && el.dataset.actual && el.dataset.actual!=='0'){el.value=el.dataset.actual;}});}</script>"
            "</div>"
        )
        return _wrap_page(page_template, "Chuẩn CPQC sản phẩm", inner)

    @app.route("/salary/my")
    @_inner_login_required
    def salary_my():
        """Bảng lương CỦA TÔI — mọi NV đăng nhập xem được, chỉ thấy dữ liệu của mình.

        employee_code = username (theo sync_employees_from_dashboard_users).
        Chỉ hiện kỳ ĐÃ CHỐT (finalized) — kỳ nháp kế toán còn đang tính, chưa phải số cuối.
        """
        username = str(session.get("username") or "").strip()
        if not username:
            return redirect("/login?next=/salary/my")
        from db import get_conn as _gc
        periods = []
        lines_by_period: Dict[str, list] = {}
        with _gc() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT period_key, employee_name_snapshot, total_revenue_net,
                           total_commission_amount, base_salary, allowance, penalty,
                           advance_amount, final_salary, finalized_at
                    FROM salary_period_employee_results
                    WHERE LOWER(employee_code) = LOWER(%s) AND status = 'finalized'
                    ORDER BY period_key DESC LIMIT 24
                    """,
                    (username,),
                )
                cols = [d[0] for d in cur.description]
                periods = [dict(zip(cols, r)) for r in cur.fetchall()]
                if periods:
                    cur.execute(
                        """
                        SELECT period_key, product_code, product_name_snapshot,
                               orders_count, revenue_net, cpqc_percent,
                               commission_percent, commission_amount, stop_run
                        FROM salary_period_product_lines
                        WHERE LOWER(employee_code) = LOWER(%s)
                          AND period_key = ANY(%s)
                        ORDER BY period_key DESC, commission_amount DESC
                        """,
                        (username, [p["period_key"] for p in periods]),
                    )
                    lcols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        line = dict(zip(lcols, row))
                        lines_by_period.setdefault(line["period_key"], []).append(line)

        def _vnd(v):
            try:
                return f"{float(v):,.0f}".replace(",", ".") + "đ"
            except (TypeError, ValueError):
                return "0đ"

        if not periods:
            body = (
                '<div class="card"><h2>💰 Bảng lương của tôi</h2>'
                "<p>Chưa có kỳ lương nào được chốt trên hệ thống. "
                "Khi kế toán chốt kỳ, bảng lương của bạn (lương cơ bản theo công + hoa hồng từng sản phẩm) sẽ hiện ở đây.</p></div>"
            )
        else:
            parts = ['<div class="card"><h2>💰 Bảng lương của tôi</h2>']
            for p in periods:
                parts.append(
                    f"<h3 style='margin-top:14px'>Kỳ {html.escape(str(p['period_key']))}</h3>"
                    "<table class='cc-table' style='width:100%;max-width:560px'>"
                    f"<tr><td>Lương cơ bản (theo công)</td><td style='text-align:right'>{_vnd(p['base_salary'])}</td></tr>"
                    f"<tr><td>Trợ cấp</td><td style='text-align:right'>{_vnd(p['allowance'])}</td></tr>"
                    f"<tr><td>Hoa hồng KPI</td><td style='text-align:right'>{_vnd(p['total_commission_amount'])}</td></tr>"
                    f"<tr><td>Phạt</td><td style='text-align:right'>-{_vnd(p['penalty'])}</td></tr>"
                    f"<tr><td>Ứng lương</td><td style='text-align:right'>-{_vnd(p['advance_amount'])}</td></tr>"
                    f"<tr><td><strong>THỰC NHẬN</strong></td><td style='text-align:right'><strong>{_vnd(p['final_salary'])}</strong></td></tr>"
                    "</table>"
                )
                lines = lines_by_period.get(p["period_key"]) or []
                if lines:
                    parts.append(
                        "<details style='margin:8px 0'><summary>Chi tiết hoa hồng theo sản phẩm"
                        f" ({len(lines)} SP)</summary><table class='cc-table' style='width:100%'>"
                        "<tr><th>SP</th><th>Đơn</th><th>DT net</th><th>%CPQC</th><th>Tier</th><th>Hoa hồng</th></tr>"
                    )
                    for ln in lines:
                        stop = " ⛔" if ln.get("stop_run") else ""
                        parts.append(
                            f"<tr><td>{html.escape(str(ln['product_code']))}{stop}</td>"
                            f"<td>{ln['orders_count']}</td>"
                            f"<td style='text-align:right'>{_vnd(ln['revenue_net'])}</td>"
                            f"<td style='text-align:right'>{ln['cpqc_percent']}%</td>"
                            f"<td style='text-align:right'>{float(ln['commission_percent'] or 0) * 100:.2f}%</td>"
                            f"<td style='text-align:right'>{_vnd(ln['commission_amount'])}</td></tr>"
                        )
                    parts.append("</table></details>")
            parts.append("</div>")
            body = "".join(parts)
        return _wrap_page(page_template, "Bảng lương của tôi", body)

    @app.route("/salary", strict_slashes=False)
    @login_required
    def salary_page():
        inner = render_template_string(
            SHELL_START + """
  <div class="card">
    <div class="section-title"><h2>Lương / KPI theo kỳ</h2></div>
    <p>Lương <strong>không</strong> tính realtime: chờ hàng hoàn, huỷ đơn, phí vận chuyển thực tế. Chọn kỳ (tháng), tính lại bản nháp, rồi <strong>chốt lương</strong> khi đủ số liệu.</p>
    <ul style="line-height:1.8;">
      <li><a class="button-link" href="{{ url_for('salary_periods_list') }}">Kỳ lương — chọn kỳ / review nháp / chốt</a></li>
      <li><a class="button-link" href="{{ url_for('salary_chuan_cpqc') }}"><strong>🎯 Chuẩn CPQC sản phẩm (nhập hàng loạt)</strong></a></li>
      <li><a class="button-link" href="{{ url_for('salary_config_list') }}">Cấu hình lương</a></li>
      <li><a class="button-link" href="{{ url_for('salary_products') }}">Sản phẩm KPI</a></li>
      <li><a class="button-link" href="{{ url_for('salary_employees') }}">Nhân sự</a></li>
      <li><a class="button-link" href="{{ url_for('salary_employee_settings') }}">Lương theo nhân viên</a></li>
      <li><a class="button-link" href="{{ url_for('salary_doc_accounting') }}"><strong>Tài liệu nghiệp vụ (kế toán xác nhận)</strong></a></li>
      <li><a class="button-link" href="{{ url_for('salary_doc_accounting_feedback_list') }}">Xem phản hồi kế toán đã gửi</a> <span style="color:#666;">(ban quản trị)</span></li>
    </ul>
  </div>
"""
            + SHELL_END,
            title="Lương / KPI",
        )
        return _wrap_page(page_template, "Lương / KPI", inner)

    @app.route("/salary/docs/ke-toan")
    @login_required
    def salary_doc_accounting():
        doc_html, warn = _accounting_doc_body_html()
        warn_html = f'<div class="alert" style="margin-bottom:1rem;">{warn}</div>' if warn else ""
        msg = (request.args.get("msg") or "").strip()
        err_q = (request.args.get("err") or "").strip()
        msg_html = (
            f'<div class="alert" style="margin-bottom:1rem;background:#e8f5e9;border-color:#4caf50;">{html.escape(msg)}</div>'
            if msg
            else ""
        )
        if err_q:
            msg_html = (
                f'<div class="alert" style="margin-bottom:1rem;background:#ffebee;border-color:#f44336;">{html.escape(err_q)}</div>'
                + msg_html
            )
        inner = render_template_string(
            SHELL_START
            + warn_html
            + msg_html
            + """
  <div class="card">
    <div class="section-title"><h2>Tài liệu nghiệp vụ tính lương — Kế toán</h2></div>
    <p style="margin-bottom:1rem;color:#555;">Cuối trang có <strong>mẫu xác nhận &amp; góp ý trực tiếp</strong>. In trang (Ctrl+P) nếu cần bản giấy.
      <br><a class="button-link" href="{{ url_for('salary_doc_accounting_feedback_list') }}">Xem các phản hồi đã gửi</a> (mới nhất trước)</p>
    <style>
      .salary-doc-prose { max-width: 48rem; margin: 0 auto; line-height: 1.7; font-size: 0.95rem; }
      .salary-doc-prose h1 { font-size: 1.35rem; margin-top: 1.25rem; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.35rem; }
      .salary-doc-prose h2 { font-size: 1.15rem; margin-top: 1.1rem; }
      .salary-doc-prose h3 { font-size: 1.05rem; margin-top: 0.9rem; }
      .salary-doc-prose table { border-collapse: collapse; width: 100%; margin: 0.85rem 0; }
      .salary-doc-prose th, .salary-doc-prose td { border: 1px solid #ccc; padding: 0.45rem 0.65rem; text-align: left; vertical-align: top; }
      .salary-doc-prose th { background: #f5f5f5; }
      .salary-doc-prose ul { padding-left: 1.25rem; }
      .salary-doc-prose blockquote { margin: 0.75rem 0; padding-left: 1rem; border-left: 4px solid #1976d2; color: #333; }
      .salary-doc-prose hr { border: none; border-top: 1px solid #ddd; margin: 1.25rem 0; }
      .salary-feedback-form label { display: block; margin: 0.65rem 0 0.2rem; font-weight: 600; }
      .salary-feedback-form input[type="text"], .salary-feedback-form input[type="date"], .salary-feedback-form textarea { width: 100%; max-width: 36rem; padding: 0.45rem 0.5rem; box-sizing: border-box; }
      .salary-feedback-form textarea { min-height: 6rem; }
      .salary-feedback-form .cb-row { margin: 0.5rem 0; font-weight: normal; }
      .salary-feedback-form .cb-row input { margin-right: 0.5rem; vertical-align: middle; }
      .salary-doc-footer { margin-top: 2rem; padding-top: 1.25rem; border-top: 2px solid #ddd; font-size: 0.85rem; color: #444; line-height: 1.6; text-align: center; }
    </style>
    <div class="salary-doc-prose">{{ doc_html|safe }}</div>
  </div>
  <div class="card" style="margin-top:1.25rem;">
    <div class="section-title"><h2>Phần dành cho Kế toán — xác nhận &amp; góp ý trực tiếp</h2></div>
    <p style="color:#555;margin-bottom:1rem;">Điền và bấm <strong>Gửi phản hồi</strong>. Nội dung được lưu để đối chiếu (không đổi số lương tự động). <strong>Ban quản trị xem tại:</strong> <a class="button-link" href="{{ url_for('salary_doc_accounting_feedback_list') }}">Phản hồi kế toán đã gửi</a>.</p>
    <form class="salary-feedback-form" method="post" action="{{ url_for('salary_doc_accounting_feedback') }}">
      <div class="cb-row"><label><input type="checkbox" name="cb_read" value="1"> Tôi đã đọc và hiểu các nguyên tắc trên.</label></div>
      <div class="cb-row"><label><input type="checkbox" name="cb_cpqc" value="1"> Tôi đồng ý với cách tính CPQC và bảng hoa hồng <strong>đang áp dụng thực tế</strong> (đối chiếu bảng cấu hình chính thức).</label></div>
      <div class="cb-row"><label><input type="checkbox" name="cb_process" value="1"> Tôi đồng ý với quy trình chờ hoàn / phí / huỷ trước khi chốt kỳ.</label></div>
      <label for="acc_comment">Tôi có góp ý cần chỉnh (ghi rõ mục và đề xuất):</label>
      <textarea id="acc_comment" name="comment" placeholder="Để trống nếu không có góp ý."></textarea>
      <label for="acc_name">Họ tên</label>
      <input type="text" id="acc_name" name="signer_name" required autocomplete="name" placeholder="Bắt buộc">
      <label for="acc_title">Chức danh</label>
      <input type="text" id="acc_title" name="signer_title" placeholder="Ví dụ: Kế toán trưởng">
      <label for="acc_date">Ngày</label>
      <input type="date" id="acc_date" name="sign_date" value="{{ default_date }}">
      <p style="margin-top:1.25rem;">
        <button type="submit" class="button-link" style="cursor:pointer;border:0;padding:0.5rem 1rem;background:#1976d2;color:#fff;border-radius:4px;">Gửi phản hồi</button>
      </p>
    </form>
    <div class="salary-doc-footer">
      <div><strong>Phiên bản dashboard — 1.1</strong></div>
      <div>Bản quyền thuộc về CTY Tiểu Hiểm</div>
      <div>Người viết: Phùng Danh Tường.</div>
      <div><em>Chỉ lưu hành nội bộ</em></div>
      <div>Liên hệ: <strong>0902708391</strong></div>
    </div>
    <p style="margin-top:1rem;font-size:0.85rem;color:#666;"><em>Tài liệu mô tả logic nghiệp vụ phase 1. Mọi thay đổi sau này cần cập nhật lại tài liệu và ký xác nhận lại.</em></p>
  </div>
"""
            + SHELL_END,
            title="Tài liệu kế toán — Lương",
            doc_html=doc_html,
            default_date=datetime.now().date().isoformat(),
        )
        return _wrap_page(page_template, "Tài liệu kế toán — Lương", inner)

    @app.route("/salary/docs/ke-toan/feedback", methods=["POST"])
    @login_required
    def salary_doc_accounting_feedback():
        cb_read = request.form.get("cb_read") == "1"
        cb_cpqc = request.form.get("cb_cpqc") == "1"
        cb_process = request.form.get("cb_process") == "1"
        comment = (request.form.get("comment") or "").strip()[:8000]
        signer_name = (request.form.get("signer_name") or "").strip()[:200]
        signer_title = (request.form.get("signer_title") or "").strip()[:200]
        sign_date = (request.form.get("sign_date") or "").strip()[:32]
        if not signer_name:
            return redirect(url_for("salary_doc_accounting", err="Vui lòng nhập họ tên."))
        row = {
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "submitted_by_login": _actor(),
            "cb_read_principles": cb_read,
            "cb_agree_cpqc_table": cb_cpqc,
            "cb_agree_wait_returns_process": cb_process,
            "comment": comment,
            "signer_name": signer_name,
            "signer_title": signer_title,
            "sign_date": sign_date,
        }
        try:
            _append_accounting_feedback_row(row)
        except OSError:
            return redirect(url_for("salary_doc_accounting", err="Không ghi được file phản hồi. Kiểm tra quyền thư mục logs."))
        return redirect(url_for("salary_doc_accounting", msg="Đã lưu phản hồi. Cảm ơn anh/chị."))

    @app.route("/salary/docs/ke-toan/da-phan-hoi")
    @login_required
    def salary_doc_accounting_feedback_list():
        rows = _load_accounting_feedback_rows_newest_first(250)
        inner = render_template_string(
            SHELL_START
            + """
  <div class="card">
    <div class="section-title"><h2>Phản hồi kế toán đã gửi</h2></div>
    <p style="margin-bottom:1rem;">
      <a class="button-link" href="{{ url_for('salary_doc_accounting') }}">← Tài liệu &amp; form gửi phản hồi</a>
      &nbsp;|&nbsp;
      <a class="button-link" href="{{ url_for('salary_page') }}">Lương / KPI</a>
    </p>
    <p style="color:#555;font-size:0.9rem;margin-bottom:1rem;">Dữ liệu lưu trên máy chủ (file log). Ai đăng nhập được trang Lương đều xem được — hãy hạn chế tài khoản truy cập.</p>
    {% if not rows %}
    <p><em>Chưa có phản hồi nào.</em></p>
    {% else %}
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>Thời gian gửi (UTC)</th>
            <th>Tài khoản đăng nhập</th>
            <th>Đã đọc hiểu</th>
            <th>Đồng ý CPQC/HH</th>
            <th>Đồng ý quy trình</th>
            <th>Góp ý</th>
            <th>Họ tên ký</th>
            <th>Chức danh</th>
            <th>Ngày ký</th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.submitted_at | e }}</td>
            <td>{{ r.submitted_by_login | e }}</td>
            <td>{{ r._yn_read }}</td>
            <td>{{ r._yn_cpqc }}</td>
            <td>{{ r._yn_proc }}</td>
            <td style="max-width:22rem;white-space:pre-wrap;">{{ r.comment | e }}</td>
            <td>{{ r.signer_name | e }}</td>
            <td>{{ r.signer_title | e }}</td>
            <td>{{ r.sign_date | e }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p style="margin-top:1rem;font-size:0.85rem;color:#666;">Tối đa 250 bản ghi mới nhất.</p>
    {% endif %}
  </div>
"""
            + SHELL_END,
            title="Phản hồi kế toán",
            rows=_prepare_feedback_rows_for_template(rows),
        )
        return _wrap_page(page_template, "Phản hồi kế toán", inner)

    @app.route("/salary/configs")
    @login_required
    def salary_config_list():
        rows = service.list_configs()
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <div class="section-title"><h2>Cấu hình lương</h2>
      <a class="button-link" href="{{ url_for('salary_config_new') }}">+ Thêm</a>
    </div>
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>Mã</th><th>Tên</th><th>Chi nhánh</th><th>Trừ VAT</th><th>Trừ ship</th><th>Active</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.config_code }}</td>
            <td>{{ r.config_name }}</td>
            <td>{{ r.branch_code or '—' }}</td>
            <td>{{ 'Có' if r.deduct_vat else 'Không' }}</td>
            <td>{{ 'Có' if r.deduct_shipping_fee else 'Không' }}</td>
            <td>{{ 'Có' if r.is_active else 'Không' }}</td>
            <td><a class="button-link" href="{{ url_for('salary_config_detail', config_id=r.id) }}">Xem / sửa</a></td>
          </tr>
          {% endfor %}
          {% if not rows %}<tr><td colspan="7">Chưa có cấu hình.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>
"""
            + SHELL_END,
            title="Cấu hình lương",
            rows=rows,
            error=request.args.get("error"),
        )
        return _wrap_page(page_template, "Cấu hình lương", inner)

    @app.route("/salary/configs/new", methods=["GET", "POST"])
    @login_required
    def salary_config_new():
        error = None
        fv = {
            "config_code": "",
            "config_name": "",
            "branch_code": "",
            "notes": "",
            "is_active": True,
            "deduct_vat": True,
            "deduct_shipping_fee": True,
        }
        if request.method == "POST":
            fv = {
                "config_code": request.form.get("config_code", ""),
                "config_name": request.form.get("config_name", ""),
                "branch_code": request.form.get("branch_code", ""),
                "notes": request.form.get("notes", ""),
                "is_active": request.form.get("is_active") in ("1", "on", "true", True, "yes"),
                "deduct_vat": request.form.get("deduct_vat") in ("1", "on", "true", True, "yes"),
                "deduct_shipping_fee": request.form.get("deduct_shipping_fee") in ("1", "on", "true", True, "yes"),
            }
            try:
                data = parse_salary_config_form(request.form)
                cid = service.create_config(data, _actor())
                return redirect(url_for("salary_config_detail", config_id=cid))
            except Exception as exc:
                error = str(exc)
        form_html = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Cấu hình mới</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <p><label>Mã cấu hình</label><input name="config_code" required value="{{ fv.config_code }}"></p>
      <p><label>Tên</label><input name="config_name" required value="{{ fv.config_name }}"></p>
      <p><label>Chi nhánh (để trống = toàn hệ thống)</label><input name="branch_code" value="{{ fv.branch_code }}"></p>
      <p><label><input type="checkbox" name="is_active" value="1" {% if fv.is_active %}checked{% endif %}> Đang áp dụng</label></p>
      <p><label><input type="checkbox" name="deduct_vat" value="1" {% if fv.deduct_vat %}checked{% endif %}> Trừ VAT khi tính (sau này)</label></p>
      <p><label><input type="checkbox" name="deduct_shipping_fee" value="1" {% if fv.deduct_shipping_fee %}checked{% endif %}> Trừ phí ship khi tính (sau này)</label></p>
      <p><label>Ghi chú</label><textarea name="notes" rows="3">{{ fv.notes }}</textarea></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{{ url_for('salary_config_list') }}">Hủy</a>
    </form>
  </div>
"""
            + SHELL_END,
            title="Thêm cấu hình",
            error=error,
            fv=SimpleNamespace(**fv),
        )
        return _wrap_page(page_template, "Thêm cấu hình", form_html)

    @app.route("/salary/configs/<int:config_id>", methods=["GET", "POST"])
    @login_required
    def salary_config_detail(config_id: int):
        cfg = service.get_config(config_id)
        if not cfg:
            return redirect(url_for("salary_config_list", error="Không tìm thấy cấu hình"))
        error = request.args.get("error")
        if request.method == "POST":
            try:
                data = parse_salary_config_form(request.form)
                service.update_config(config_id, data, _actor())
                cfg = service.get_config(config_id)
                error = None
            except Exception as exc:
                error = str(exc)
        tiers = service.list_tiers(config_id)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Sửa cấu hình #{{ cfg.id }}</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <p><label>Mã cấu hình</label><input name="config_code" required value="{{ cfg.config_code }}"></p>
      <p><label>Tên</label><input name="config_name" required value="{{ cfg.config_name }}"></p>
      <p><label>Chi nhánh</label><input name="branch_code" value="{{ cfg.branch_code or '' }}"></p>
      <p><label><input type="checkbox" name="is_active" value="1" {% if cfg.is_active %}checked{% endif %}> Active</label></p>
      <p><label><input type="checkbox" name="deduct_vat" value="1" {% if cfg.deduct_vat %}checked{% endif %}> Trừ VAT</label></p>
      <p><label><input type="checkbox" name="deduct_shipping_fee" value="1" {% if cfg.deduct_shipping_fee %}checked{% endif %}> Trừ phí ship</label></p>
      <p><label>Ghi chú</label><textarea name="notes" rows="3">{{ cfg.notes or '' }}</textarea></p>
      <button class="button" type="submit">Lưu cấu hình</button>
    </form>
  </div>
  <div class="card">
    <div class="section-title"><h2>Mốc %CPQC → hoa hồng</h2></div>
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>min %</th><th>max %</th><th>%HH (decimal)</th><th>Giá nhập ≤</th><th>stop</th><th>sort</th><th>active</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for t in tiers %}
          <tr>
            <td>{{ t.min_cpqc_percent }}</td>
            <td>{{ t.max_cpqc_percent if t.max_cpqc_percent is not none else '∞' }}</td>
            <td>{{ t.commission_percent }}</td>
            <td>{{ t.require_import_price_lte or '—' }}</td>
            <td>{{ 'Có' if t.stop_run else '—' }}</td>
            <td>{{ t.sort_order }}</td>
            <td>{{ 'Có' if t.is_active else 'Không' }}</td>
            <td>
              <a class="button-link" href="{{ url_for('salary_tier_edit', config_id=cfg.id, tier_id=t.id) }}">Sửa</a>
              <form method="post" action="{{ url_for('salary_tier_delete', config_id=cfg.id, tier_id=t.id) }}" style="display:inline;" onsubmit="return confirm('Xóa mốc này?');">
                <button class="button" type="submit" style="padding:6px 10px;font-size:13px;">Xóa</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <h3>Thêm mốc</h3>
    <form method="post" action="{{ url_for('salary_tier_add', config_id=cfg.id) }}" class="dashboard-date-form" style="max-width:640px;">
      <p><label>min_cpqc_percent</label><input name="min_cpqc_percent" value="0"></p>
      <p><label>max_cpqc_percent (để trống = không giới hạn trên)</label><input name="max_cpqc_percent"></p>
      <p><label>commission_percent (vd 0.035 = 3.5%%)</label><input name="commission_percent" value="0"></p>
      <p><label>require_import_price_lte</label><input name="require_import_price_lte"></p>
      <p><label><input type="checkbox" name="stop_run" value="1"> stop_run</label></p>
      <p><label>sort_order</label><input name="sort_order" value="0"></p>
      <p><label><input type="checkbox" name="is_active" value="1" checked> is_active</label></p>
      <button class="button" type="submit">Thêm mốc</button>
    </form>
  </div>
"""
            + SHELL_END,
            title="Chi tiết cấu hình",
            cfg=cfg,
            tiers=tiers,
            error=error,
        )
        return _wrap_page(page_template, f"Cấu hình {cfg['config_code']}", inner)

    @app.route("/salary/configs/<int:config_id>/tiers/add", methods=["POST"])
    @login_required
    def salary_tier_add(config_id: int):
        try:
            data = parse_cpqc_tier_form(request.form)
            service.create_tier(config_id, data, _actor())
        except Exception as exc:
            return redirect(url_for("salary_config_detail", config_id=config_id, error=str(exc)))
        return redirect(url_for("salary_config_detail", config_id=config_id))

    @app.route("/salary/configs/<int:config_id>/tiers/<int:tier_id>/edit", methods=["GET", "POST"])
    @login_required
    def salary_tier_edit(config_id: int, tier_id: int):
        tier = service.get_tier(tier_id)
        if not tier or int(tier["salary_config_id"]) != int(config_id):
            return redirect(url_for("salary_config_list", error="Mốc KPI không hợp lệ"))
        error = None
        if request.method == "POST":
            try:
                data = parse_cpqc_tier_form(request.form)
                service.update_tier(tier_id, data, _actor())
                return redirect(url_for("salary_config_detail", config_id=config_id))
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Sửa mốc CPQC #{{ tier.id }}</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <p><label>min_cpqc_percent</label><input name="min_cpqc_percent" value="{{ tier.min_cpqc_percent }}"></p>
      <p><label>max_cpqc_percent</label><input name="max_cpqc_percent" value="{{ tier.max_cpqc_percent if tier.max_cpqc_percent is not none else '' }}"></p>
      <p><label>commission_percent</label><input name="commission_percent" value="{{ tier.commission_percent }}"></p>
      <p><label>require_import_price_lte</label><input name="require_import_price_lte" value="{{ tier.require_import_price_lte or '' }}"></p>
      <p><label><input type="checkbox" name="stop_run" value="1" {% if tier.stop_run %}checked{% endif %}> stop_run</label></p>
      <p><label>sort_order</label><input name="sort_order" value="{{ tier.sort_order }}"></p>
      <p><label><input type="checkbox" name="is_active" value="1" {% if tier.is_active %}checked{% endif %}> is_active</label></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{{ url_for('salary_config_detail', config_id=config_id) }}">Quay lại</a>
    </form>
  </div>
"""
            + SHELL_END,
            tier=tier,
            error=error,
            config_id=config_id,
        )
        return _wrap_page(page_template, "Sửa mốc CPQC", inner)

    @app.route("/salary/configs/<int:config_id>/tiers/<int:tier_id>/delete", methods=["POST"])
    @login_required
    def salary_tier_delete(config_id: int, tier_id: int):
        tier = service.get_tier(tier_id)
        if not tier or int(tier["salary_config_id"]) != int(config_id):
            return redirect(url_for("salary_config_list", error="Không xóa được"))
        try:
            service.delete_tier(tier_id, _actor())
        except Exception:
            pass
        return redirect(url_for("salary_config_detail", config_id=config_id))

    @app.route("/salary/products")
    @login_required
    def salary_products():
        rows = service.list_products()
        inner = render_template_string(
            SHELL_START
            + """
  <div class="card">
    <div class="section-title"><h2>Sản phẩm KPI</h2>
      <a class="button-link" href="{{ url_for('salary_product_new') }}">+ Thêm</a>
    </div>
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>Mã</th><th>Tên</th><th>Giá nhập</th><th>CPQC chuẩn</th><th>VAT</th><th>HH</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.product_code }}</td>
            <td>{{ r.product_name }}</td>
            <td>{{ r.import_price }}</td>
            <td>{{ r.cpqc_standard_per_order }}</td>
            <td>{{ r.vat_rate if r.vat_rate is not none else '—' }}</td>
            <td>{{ 'Có' if r.is_commission_enabled else 'Không' }}</td>
            <td><a class="button-link" href="{{ url_for('salary_product_edit', product_id=r.id) }}">Sửa</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
"""
            + SHELL_END,
            rows=rows,
        )
        return _wrap_page(page_template, "Sản phẩm KPI", inner)

    @app.route("/salary/products/new", methods=["GET", "POST"])
    @login_required
    def salary_product_new():
        error = None
        if request.method == "POST":
            try:
                data = parse_product_form(request.form)
                pid = service.create_product(data, _actor())
                return redirect(url_for("salary_product_edit", product_id=pid))
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Sản phẩm mới</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <p><label>Mã SP</label><input name="product_code" required></p>
      <p><label>Tên</label><input name="product_name" required></p>
      <p><label>Giá nhập</label><input name="import_price" value="0"></p>
      <p><label>CPQC chuẩn / đơn</label><input name="cpqc_standard_per_order" required></p>
      <p><label>VAT (0.08 / 0.10, để trống = theo POS sau này)</label><input name="vat_rate"></p>
      <p><label><input type="checkbox" name="is_commission_enabled" value="1" checked> Áp dụng hoa hồng</label></p>
      <p><label>Ghi chú</label><textarea name="notes" rows="2"></textarea></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{{ url_for('salary_products') }}">Hủy</a>
    </form>
  </div>
"""
            + SHELL_END,
            error=error,
        )
        return _wrap_page(page_template, "Thêm sản phẩm KPI", inner)

    @app.route("/salary/products/<int:product_id>/edit", methods=["GET", "POST"])
    @login_required
    def salary_product_edit(product_id: int):
        row = service.get_product(product_id)
        if not row:
            return redirect(url_for("salary_products"))
        error = None
        if request.method == "POST":
            try:
                data = parse_product_form(request.form)
                service.update_product(product_id, data, _actor())
                row = service.get_product(product_id)
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Sửa sản phẩm</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <p><label>Mã SP</label><input name="product_code" required value="{{ row.product_code }}"></p>
      <p><label>Tên</label><input name="product_name" required value="{{ row.product_name }}"></p>
      <p><label>Giá nhập</label><input name="import_price" value="{{ row.import_price }}"></p>
      <p><label>CPQC chuẩn / đơn</label><input name="cpqc_standard_per_order" value="{{ row.cpqc_standard_per_order }}"></p>
      <p><label>VAT</label><input name="vat_rate" value="{{ row.vat_rate if row.vat_rate is not none else '' }}"></p>
      <p><label><input type="checkbox" name="is_commission_enabled" value="1" {% if row.is_commission_enabled %}checked{% endif %}> Áp dụng HH</label></p>
      <p><label>Ghi chú</label><textarea name="notes" rows="2">{{ row.notes or '' }}</textarea></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{{ url_for('salary_products') }}">Danh sách</a>
    </form>
  </div>
"""
            + SHELL_END,
            row=row,
            error=error,
        )
        return _wrap_page(page_template, "Sửa sản phẩm KPI", inner)

    @app.route("/salary/employees")
    @login_required
    def salary_employees():
        rows = service.list_emps()
        inner = render_template_string(
            SHELL_START
            + """
  {% if msg %}<div class="alert">{{ msg }}</div>{% endif %}
  {% if err %}<div class="alert" style="background:#fef2f2;">{{ err }}</div>{% endif %}
  <div class="card">
    <div class="section-title"><h2>Nhân sự</h2>
      <span style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <a class="button-link" href="{{ url_for('salary_employee_new') }}">+ Thêm</a>
      <form method="post" action="{{ url_for('salary_employees_sync_users') }}" style="display:inline;"
            onsubmit="return confirm('Đồng bộ từ users.json?\\n\\n- Chỉ tài khoản active, role staff/leader.\\n- Mã NV = salary_employee_code (nếu có) hoặc username.\\n- Tạo cài đặt lương mặc định nếu chưa có.');">
        <button class="button" type="submit" style="font-size:14px;">Đồng bộ từ tài khoản web</button>
      </form>
      </span>
    </div>
    <p style="font-size:14px;color:#64748b;line-height:1.5;margin:0 0 12px;">
      Nút <strong>Đồng bộ từ tài khoản web</strong> đọc file <code>users.json</code> (Cài đặt dashboard), thêm/cập nhật <code>employees</code> và bổ sung <code>employee_salary_settings</code> (lương cứng 0, cấu hình default_main) khi thiếu.
      Có thể gán mã lương riêng trong JSON: <code>salary_employee_code</code> hoặc <code>employee_code</code>; tên hiển thị: <code>display_name</code> hoặc <code>employee_name</code>.
    </p>
    <div class="dashboard-table-scroll">
      <table>
        <thead><tr><th>Mã</th><th>Tên</th><th>Chi nhánh</th><th>Active</th><th></th></tr></thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.employee_code }}</td>
            <td>{{ r.employee_name }}</td>
            <td>{{ r.branch_code or '—' }}</td>
            <td>{{ 'Có' if r.is_active else 'Không' }}</td>
            <td><a class="button-link" href="{{ url_for('salary_employee_edit', employee_code=r.employee_code) }}">Sửa</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
"""
            + SHELL_END,
            rows=rows,
            msg=request.args.get("msg"),
            err=request.args.get("err"),
        )
        return _wrap_page(page_template, "Nhân sự", inner)

    @app.route("/salary/employees/sync-from-users", methods=["POST"])
    @login_required
    def salary_employees_sync_users():
        try:
            out = service.sync_employees_from_dashboard_users(_actor())
        except ValueError as exc:
            return redirect(url_for("salary_employees", err=str(exc)))
        summary = (
            f"Đồng bộ xong: +{out['employees_created']} NV mới, "
            f"cập nhật {out['employees_updated']}, "
            f"+{out['salary_settings_created']} cài đặt lương; "
            f"bỏ qua {out['skipped_users']} dòng (không active hoặc không phải staff/leader)."
        )
        return redirect(url_for("salary_employees", msg=summary))

    @app.route("/salary/employees/new", methods=["GET", "POST"])
    @login_required
    def salary_employee_new():
        error = None
        if request.method == "POST":
            try:
                data = parse_employee_form(request.form)
                service.create_emp(data, _actor())
                return redirect(url_for("salary_employees"))
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <form method="post" class="dashboard-date-form" style="max-width:480px;">
      <p><label>Mã NV</label><input name="employee_code" required></p>
      <p><label>Tên</label><input name="employee_name" required></p>
      <p><label>Chi nhánh</label><input name="branch_code"></p>
      <p><label>Team</label><input name="team_code"></p>
      <p><label>Leader</label><input name="leader_code"></p>
      <p><label><input type="checkbox" name="is_active" value="1" checked> Active</label></p>
      <button class="button" type="submit">Lưu</button>
    </form>
  </div>
"""
            + SHELL_END,
            error=error,
        )
        return _wrap_page(page_template, "Thêm nhân sự", inner)

    @app.route("/salary/employees/<employee_code>/edit", methods=["GET", "POST"])
    @login_required
    def salary_employee_edit(employee_code: str):
        row = service.get_emp(employee_code)
        if not row:
            return redirect(url_for("salary_employees"))
        error = None
        if request.method == "POST":
            try:
                data = parse_employee_form({**dict(request.form), "employee_code": employee_code})
                service.update_emp(employee_code, data, _actor())
                row = service.get_emp(employee_code)
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <form method="post" class="dashboard-date-form" style="max-width:480px;">
      <p><label>Mã NV</label><input readonly value="{{ row.employee_code }}"></p>
      <p><label>Tên</label><input name="employee_name" required value="{{ row.employee_name }}"></p>
      <p><label>Chi nhánh</label><input name="branch_code" value="{{ row.branch_code or '' }}"></p>
      <p><label>Team</label><input name="team_code" value="{{ row.team_code or '' }}"></p>
      <p><label>Leader</label><input name="leader_code" value="{{ row.leader_code or '' }}"></p>
      <p><label><input type="checkbox" name="is_active" value="1" {% if row.is_active %}checked{% endif %}> Active</label></p>
      <button class="button" type="submit">Lưu</button>
    </form>
  </div>
"""
            + SHELL_END,
            row=row,
            error=error,
        )
        return _wrap_page(page_template, "Sửa nhân sự", inner)

    @app.route("/salary/employees/settings")
    @login_required
    def salary_employee_settings():
        rows = service.list_emp_settings()
        opts = service.config_options()
        inner = render_template_string(
            SHELL_START
            + """
  <div class="card">
    <div class="section-title"><h2>Lương nhân viên</h2></div>
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>Mã</th><th>Tên</th><th>Lương cứng</th><th>Phụ cấp</th><th>Phạt</th><th>Tạm ứng</th><th>Config</th><th>Active</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.employee_code }}</td>
            <td>{{ r.employee_name }}</td>
            <td>{{ r.base_salary }}</td>
            <td>{{ r.allowance }}</td>
            <td>{{ r.default_penalty }}</td>
            <td>{{ r.default_advance }}</td>
            <td>{{ r.salary_config_id or '—' }}</td>
            <td>{{ 'Có' if r.is_active else 'Không' }}</td>
            <td><a class="button-link" href="{{ url_for('salary_employee_setting_edit', employee_code=r.employee_code) }}">Sửa</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p><small>Thêm nhân sự tại mục Nhân sự trước khi gán lương.</small></p>
  </div>
"""
            + SHELL_END,
            rows=rows,
            opts=opts,
        )
        return _wrap_page(page_template, "Lương nhân viên", inner)

    @app.route("/salary/employees/settings/<employee_code>/edit", methods=["GET", "POST"])
    @login_required
    def salary_employee_setting_edit(employee_code: str):
        row = service.get_emp_setting(employee_code)
        opts = service.config_options()
        emp = service.get_emp(employee_code)
        if not emp:
            return redirect(url_for("salary_employees", error="Chưa có nhân sự này"))
        error = None
        if request.method == "POST":
            try:
                data = parse_employee_setting_form({**dict(request.form), "employee_code": employee_code})
                service.save_emp_setting(data, _actor())
                return redirect(url_for("salary_employee_settings"))
            except Exception as exc:
                error = str(exc)
        inner = render_template_string(
            SHELL_START
            + """
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Cài đặt lương — {{ emp.employee_name }} ({{ emp.employee_code }})</h2>
    <form method="post" class="dashboard-date-form" style="max-width:520px;">
      <input type="hidden" name="employee_code" value="{{ emp.employee_code }}">
      <p><label>Lương cứng</label><input name="base_salary" value="{{ row.base_salary if row else 0 }}"></p>
      <p><label>Phụ cấp</label><input name="allowance" value="{{ row.allowance if row else 0 }}"></p>
      <p><label>Phạt mặc định</label><input name="default_penalty" value="{{ row.default_penalty if row else 0 }}"></p>
      <p><label>Tạm ứng mặc định</label><input name="default_advance" value="{{ row.default_advance if row else 0 }}"></p>
      <p><label>Cấu hình lương</label>
        <select name="salary_config_id">
          <option value="">—</option>
          {% for o in opts %}
          <option value="{{ o.id }}" {% if row and row.salary_config_id == o.id %}selected{% endif %}>{{ o.label }}</option>
          {% endfor %}
        </select>
      </p>
      <p><label><input type="checkbox" name="is_active" value="1" {% if not row or row.is_active %}checked{% endif %}> Active</label></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{{ url_for('salary_employee_settings') }}">Quay lại</a>
    </form>
  </div>
"""
            + SHELL_END,
            emp=emp,
            row=row,
            opts=opts,
            error=error,
        )
        return _wrap_page(page_template, "Cài đặt lương NV", inner)

    @app.route("/salary/periods/open")
    @login_required
    def salary_period_open():
        raw = (request.args.get("period") or "").strip()
        try:
            pk = service.normalize_period_key(raw)
        except ValueError:
            return redirect(url_for("salary_periods_list", err="Kỳ không hợp lệ (YYYY-MM)."))
        return redirect(url_for("salary_period_view", period_key=pk))

    @app.route("/salary/periods")
    @login_required
    def salary_periods_list():
        rows = service.list_period_summaries()
        if _wants_json():
            return jsonify({"ok": True, "data": rows})
        inner = render_template_string(
            SHELL_START
            + """
  {% if err %}<div class="alert" style="background:#fef2f2;">{{ err }}</div>{% endif %}
  <div class="card">
    <div class="section-title"><h2>Kỳ lương</h2></div>
    <p>Chọn tháng (YYYY-MM) để xem. Trước chốt: vào <strong>Review dữ liệu nháp</strong> để kế toán chỉnh các trường chưa chắc, rồi <strong>Tính lại kỳ</strong> → chốt. Sau khi chốt, kỳ khóa.</p>
    <form method="get" action="{{ url_for('salary_period_open') }}" class="dashboard-date-form" style="margin-bottom:16px;">
      <label>Kỳ</label>
      <input type="month" name="period" required value="{{ default_period }}">
      <button class="button" type="submit">Mở kỳ</button>
    </form>
    <p><small>API: <code>?format=json</code> — trả JSON danh sách kỳ.</small></p>
    <div class="dashboard-table-scroll">
      <table>
        <thead>
          <tr>
            <th>Kỳ</th><th>Trạng thái</th><th>Chốt lúc</th><th>NV</th><th>Khóa</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.period_key }}</td>
            <td>{{ r.registry_status }}</td>
            <td>{{ r.finalized_at or '—' }}</td>
            <td>{{ r.employee_rows }}</td>
            <td>{{ 'Có' if r.locked else 'Không' }}</td>
            <td><a class="button-link" href="{{ url_for('salary_period_view', period_key=r.period_key) }}">Mở</a></td>
          </tr>
          {% endfor %}
          {% if not rows %}
          <tr><td colspan="6">Chưa có kỳ nào. Chọn tháng ở trên hoặc bấm Tính lại trong màn kỳ.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
"""
            + SHELL_END,
            rows=rows,
            default_period=request.args.get("default_period", "") or "",
            err=request.args.get("err"),
        )
        return _wrap_page(page_template, "Kỳ lương", inner)

    @app.route("/salary/periods/<period_key>")
    @login_required
    def salary_period_view(period_key: str):
        try:
            pk = service.normalize_period_key(period_key)
        except ValueError as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 400
            return redirect(url_for("salary_periods_list"))
        reg = service.get_period_registry(pk)
        emp_rows = service.list_employee_results_for_period(pk)
        locked = service.is_period_locked(pk)
        if _wants_json():
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "period_key": pk,
                        "registry": reg,
                        "locked": locked,
                        "employee_results": emp_rows,
                    },
                }
            )
        inner = render_template_string(
            SHELL_START
            + """
  {% if msg %}<div class="alert">{{ msg }}</div>{% endif %}
  {% if err %}<div class="alert" style="background:#fef2f2;">{{ err }}</div>{% endif %}
  <div class="card">
    <div class="section-title"><h2>Kỳ {{ pk }}</h2>
      <span>{% if locked %}Đã chốt (chỉ xem){% else %}Bản nháp{% endif %}</span>
    </div>
    {% if reg and reg.finalized_at %}
    <p>Chốt: {{ reg.finalized_at }} {% if reg.finalized_by %}bởi {{ reg.finalized_by }}{% endif %}</p>
    {% endif %}
    <div class="dashboard-menu-grid" style="margin-bottom:12px;">
      <a class="button-link" href="{{ url_for('salary_periods_list') }}">← Danh sách kỳ</a>
      <a class="button-link" href="{{ url_for('salary_period_draft_review', period_key=pk) }}">Review dữ liệu nháp (input kỳ)</a>
    </div>
    <p style="color:#64748b;font-size:14px;margin:8px 0 12px;">Quy trình đề xuất: chỉnh / xác nhận dữ liệu trên trang <strong>Review nháp</strong> → <strong>Tính lại kỳ</strong> → kiểm tra bảng NV → <strong>Chốt lương</strong>.</p>
    {% if not locked %}
    <form method="post" action="{{ url_for('salary_period_recalculate', period_key=pk) }}" style="display:inline;"
          onsubmit="return confirm('Tính lại kỳ {{ pk }}? Dữ liệu nháp hiện tại sẽ bị xóa và tạo lại.');">
      <button class="button" type="submit">Tính lại kỳ</button>
    </form>
    {% endif %}
    <div class="dashboard-table-scroll" style="margin-top:16px;">
      <table>
        <thead>
          <tr>
            <th>NV</th><th>Chi nhánh</th><th>Lương cứng</th><th>Phụ cấp</th><th>HH</th><th>Lương cuối</th><th>Trạng thái</th>
          </tr>
        </thead>
        <tbody>
          {% for r in emp_rows %}
          <tr>
            <td>{{ r.employee_name_snapshot }} ({{ r.employee_code }})</td>
            <td>{{ r.branch_code or '—' }}</td>
            <td>{{ "{:,.0f}".format(r.base_salary) }}</td>
            <td>{{ "{:,.0f}".format(r.allowance) }}</td>
            <td>{{ "{:,.0f}".format(r.total_commission_amount) }}</td>
            <td><strong>{{ "{:,.0f}".format(r.final_salary) }}</strong></td>
            <td>{{ r.status }}{% if r.finalized_at %} · {{ r.finalized_at }}{% endif %}</td>
          </tr>
          {% endfor %}
          {% if not emp_rows %}
          <tr><td colspan="7">Chưa có dòng lương. Bấm <strong>Tính lại kỳ</strong> (nếu kỳ chưa chốt).</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
    {% if not locked and emp_rows %}
    <div class="card" style="margin-top:20px;border:1px solid #f59e0b;padding:16px;border-radius:12px;">
      <h3>Chốt lương kỳ {{ pk }}</h3>
      <p>Chỉ chốt sau khi đã review/chỉnh <strong>dữ liệu nháp (input)</strong> và bấm <strong>Tính lại kỳ</strong> để kiểm tra HH / lương cuối. Sau khi chốt, không tính lại và không sửa dữ liệu kỳ.</p>
      <form method="post" action="{{ url_for('salary_period_finalize', period_key=pk) }}">
        <p><label><input type="checkbox" name="confirm_finalize" value="1" required> Tôi xác nhận chốt lương kỳ {{ pk }}</label></p>
        <button class="button" type="submit" style="background:#b45309;">Chốt lương</button>
      </form>
    </div>
    {% endif %}
  </div>
"""
            + SHELL_END,
            pk=pk,
            reg=reg,
            emp_rows=emp_rows,
            locked=locked,
            msg=request.args.get("msg"),
            err=request.args.get("err"),
        )
        return _wrap_page(page_template, f"Kỳ lương {pk}", inner)

    @app.route("/salary/periods/<period_key>/recalculate", methods=["POST"])
    @login_required
    def salary_period_recalculate(period_key: str):
        try:
            pk = service.normalize_period_key(period_key)
        except ValueError as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 400
            return redirect(url_for("salary_periods_list", err=str(exc)))
        try:
            out = service.recalculate_period(pk)
            if _wants_json():
                return jsonify({"ok": True, **out})
            return redirect(url_for("salary_period_view", period_key=pk, msg="Đã tính lại kỳ."))
        except ValueError as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 400
            return redirect(url_for("salary_period_view", period_key=pk, err=str(exc)))

    @app.route("/salary/periods/<period_key>/finalize", methods=["POST"])
    @login_required
    def salary_period_finalize(period_key: str):
        try:
            pk = service.normalize_period_key(period_key)
        except ValueError as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 400
            return redirect(url_for("salary_periods_list", err=str(exc)))
        confirmed = False
        if request.is_json:
            body = request.get_json(silent=True) or {}
            confirmed = bool(body.get("confirm"))
        else:
            confirmed = request.form.get("confirm_finalize") == "1"
        try:
            out = service.finalize_period(pk, _actor(), confirmed)
            if _wants_json():
                return jsonify({"ok": True, **out})
            return redirect(url_for("salary_period_view", period_key=pk, msg="Đã chốt lương kỳ."))
        except ValueError as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 400
            return redirect(url_for("salary_period_view", period_key=pk, err=str(exc)))

    @app.route("/salary/periods/<period_key>/draft-review", methods=["GET", "POST"])
    @login_required
    def salary_period_draft_review(period_key: str):
        """Lớp review kế toán: sửa salary_period_input_lines trước recalculate/finalize (không đổi engine)."""
        try:
            pk = service.normalize_period_key(period_key)
        except ValueError as exc:
            return redirect(url_for("salary_periods_list", err=str(exc)))
        locked = service.is_period_locked(pk)

        if request.method == "POST":
            if locked:
                return redirect(
                    url_for("salary_period_draft_review", period_key=pk, err="Kỳ đã chốt — không sửa dữ liệu nháp.")
                )
            action = (request.form.get("action") or "").strip()
            try:
                if action == "delete":
                    ec = (request.form.get("employee_code") or "").strip()
                    pc = (request.form.get("product_code") or "").strip()
                    if not ec or not pc:
                        raise ValueError("Thiếu mã NV hoặc mã SP để xóa.")
                    service.delete_period_input_line(pk, ec, pc)
                    return redirect(url_for("salary_period_draft_review", period_key=pk, msg="Đã xóa dòng input."))
                if action in ("save_row", "add_row"):
                    ec = (request.form.get("employee_code") or "").strip()
                    pc = (request.form.get("product_code") or "").strip()
                    if not ec or not pc:
                        raise ValueError("Mã NV và mã SP là bắt buộc.")
                    service.save_period_input_line(
                        pk,
                        ec,
                        pc,
                        _salary_form_money("revenue_gross"),
                        _salary_form_money("quantity"),
                        _salary_form_int("orders_count", 1),
                        _salary_form_money("ads_cost"),
                        _salary_form_money("shipping_fee"),
                        returned_revenue_gross=_salary_form_money("returned_revenue_gross"),
                        returned_quantity=_salary_form_money("returned_quantity"),
                        returned_orders_count=_salary_form_int("returned_orders_count", 0),
                        shipping_fee_return_delta=_salary_form_money("shipping_fee_return_delta"),
                    )
                    return redirect(
                        url_for("salary_period_draft_review", period_key=pk, msg="Đã lưu dòng nháp (input).")
                    )
            except ValueError as exc:
                return redirect(url_for("salary_period_draft_review", period_key=pk, err=str(exc)))

        input_lines = service.list_period_input_lines(pk)
        employees = [e for e in service.list_emps() if e.get("is_active", True)]
        products = service.list_products()
        pmap = {str(p.get("product_code") or ""): str(p.get("product_name") or "") for p in products}

        inner = render_template_string(
            SHELL_START
            + """
  {% if msg %}<div class="alert">{{ msg }}</div>{% endif %}
  {% if err %}<div class="alert" style="background:#fef2f2;">{{ err }}</div>{% endif %}
  <div class="card">
    <div class="section-title"><h2>Review nháp kỳ {{ pk }}</h2>
      <span>{% if locked %}Chỉ xem{% else %}Kế toán chỉnh tay{% endif %}</span>
    </div>
    <p style="line-height:1.6;color:#475569;">
      Đây là <strong>salary_period_input_lines</strong> — đầu vào trước khi engine tính HH/CPQC.
      Các số <em>có thể</em> lấy tự động từ POS/Ads trong tương lai; hiện tại kế toán xác nhận/chỉnh các trường chưa chắc trước khi bấm <strong>Tính lại kỳ</strong>.
    </p>
    <div class="dashboard-menu-grid" style="margin-bottom:12px;">
      <a class="button-link" href="{{ url_for('salary_period_view', period_key=pk) }}">← Tổng quan kỳ</a>
      <a class="button-link" href="{{ url_for('salary_periods_list') }}">Danh sách kỳ</a>
    </div>
    <div class="dashboard-table-scroll" style="margin-top:12px;">
      <table>
        <thead>
          <tr>
            <th>NV</th><th>Mã SP</th><th>Tên SP (config)</th>
            <th>DT gross</th><th>SL</th><th>Số đơn</th><th>Ads</th><th>Ship</th>
            <th>Hoàn DT</th><th>Hoàn SL</th><th>Hoàn đơn</th><th>Δ ship hoàn</th>
            {% if not locked %}<th></th>{% endif %}
          </tr>
        </thead>
        <tbody>
          {% for row in input_lines %}
          <tr>
            <form method="post" action="{{ url_for('salary_period_draft_review', period_key=pk) }}" style="display:contents;">
              <input type="hidden" name="action" value="save_row">
              <input type="hidden" name="employee_code" value="{{ row.employee_code }}">
              <input type="hidden" name="product_code" value="{{ row.product_code }}">
            <td>{{ row.employee_code }}</td>
            <td>{{ row.product_code }}</td>
            <td style="max-width:140px;white-space:normal;">{{ pmap.get(row.product_code, '—') }}</td>
            <td><input name="revenue_gross" value="{{ row.revenue_gross }}" style="width:88px" {% if locked %}readonly{% endif %}></td>
            <td><input name="quantity" value="{{ row.quantity }}" style="width:72px" {% if locked %}readonly{% endif %}></td>
            <td><input name="orders_count" value="{{ row.orders_count }}" style="width:56px" {% if locked %}readonly{% endif %}></td>
            <td><input name="ads_cost" value="{{ row.ads_cost }}" style="width:88px" {% if locked %}readonly{% endif %}></td>
            <td><input name="shipping_fee" value="{{ row.shipping_fee }}" style="width:88px" {% if locked %}readonly{% endif %}></td>
            <td><input name="returned_revenue_gross" value="{{ row.returned_revenue_gross }}" style="width:88px" {% if locked %}readonly{% endif %}></td>
            <td><input name="returned_quantity" value="{{ row.returned_quantity }}" style="width:72px" {% if locked %}readonly{% endif %}></td>
            <td><input name="returned_orders_count" value="{{ row.returned_orders_count }}" style="width:56px" {% if locked %}readonly{% endif %}></td>
            <td><input name="shipping_fee_return_delta" value="{{ row.shipping_fee_return_delta }}" style="width:88px" {% if locked %}readonly{% endif %}></td>
            {% if not locked %}
            <td><button class="button" type="submit" style="padding:6px 10px;font-size:13px;">Lưu</button></td>
            {% endif %}
            </form>
          </tr>
            {% if not locked %}
          <tr>
            <td colspan="13" style="border-top:none;padding-top:0;">
              <form method="post" action="{{ url_for('salary_period_draft_review', period_key=pk) }}" style="display:inline;"
                    onsubmit="return confirm('Xóa dòng {{ row.employee_code }} / {{ row.product_code }}?');">
                <input type="hidden" name="action" value="delete">
                <input type="hidden" name="employee_code" value="{{ row.employee_code }}">
                <input type="hidden" name="product_code" value="{{ row.product_code }}">
                <button class="button" type="submit" style="padding:4px 10px;font-size:12px;background:#fef2f2;">Xóa dòng</button>
              </form>
            </td>
          </tr>
            {% endif %}
          {% else %}
          <tr><td colspan="13">Chưa có dòng input. Thêm dòng mới bên dưới (hoặc đồng bộ tự động khi có pipeline).</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% if not locked %}
    <div class="card" style="margin-top:20px;padding:16px;">
      <h3>Thêm dòng nháp</h3>
      {% if not employees %}
      <p class="alert">Chưa có nhân viên active — thêm ở mục <strong>Nhân sự</strong> trước.</p>
      {% else %}
      <p style="font-size:14px;color:#64748b;">Mã SP phải khớp <code>product_salary_configs.product_code</code>.</p>
      <form method="post" action="{{ url_for('salary_period_draft_review', period_key=pk) }}" class="dashboard-date-form" style="max-width:720px;">
        <input type="hidden" name="action" value="add_row">
        <p><label>Mã NV</label>
          <select name="employee_code" required style="width:100%;max-width:28rem;">
            {% for e in employees %}
            <option value="{{ e.employee_code }}">{{ e.employee_code }} — {{ e.employee_name }}</option>
            {% endfor %}
          </select>
        </p>
        <p><label>Mã SP</label>
          <input name="product_code" required list="product_codes_list" placeholder="VD: KIM_A" style="width:100%;max-width:28rem;">
          <datalist id="product_codes_list">
            {% for p in products %}
            <option value="{{ p.product_code }}">{{ p.product_name }}</option>
            {% endfor %}
          </datalist>
        </p>
        <p><label>Doanh thu gross</label><input name="revenue_gross" value="0"></p>
        <p><label>Số lượng</label><input name="quantity" value="0"></p>
        <p><label>Số đơn</label><input name="orders_count" value="1"></p>
        <p><label>Chi phí ads (phân bổ tay)</label><input name="ads_cost" value="0"></p>
        <p><label>Phí ship (net trước hoàn)</label><input name="shipping_fee" value="0"></p>
        <p><label>Hoàn — doanh thu gross</label><input name="returned_revenue_gross" value="0"></p>
        <p><label>Hoàn — số lượng</label><input name="returned_quantity" value="0"></p>
        <p><label>Hoàn — số đơn</label><input name="returned_orders_count" value="0"></p>
        <p><label>Hoàn — điều chỉnh phí ship (delta)</label><input name="shipping_fee_return_delta" value="0"></p>
        <button class="button" type="submit">Thêm / cập nhật dòng</button>
      </form>
      {% endif %}
    </div>
    {% endif %}
  </div>
"""
            + SHELL_END,
            pk=pk,
            input_lines=input_lines,
            employees=employees,
            products=products,
            pmap=pmap,
            locked=locked,
            msg=request.args.get("msg"),
            err=request.args.get("err"),
        )
        return _wrap_page(page_template, f"Review nháp {pk}", inner)
