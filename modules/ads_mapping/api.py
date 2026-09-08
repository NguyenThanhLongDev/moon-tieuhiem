from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from flask import redirect, render_template_string, request, session, url_for

from . import repository as repo
from . import service
from .constants import ADS_VAT_RATE


def _wrap(page_template: str, title: str, inner: str) -> str:
    ver = str(os.environ.get("POS_DASHBOARD_WEB_VERSION", "1.1")).strip() or "1.1"
    return render_template_string(
        page_template,
        title=title,
        body=inner,
        dashboard_web_version=ver,
    )


def _actor() -> str:
    return str(session.get("username") or "system")


def _parse_ymd(s: Optional[str], default: str) -> str:
    raw = (s or "").strip() or default
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def _money(n: Any) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:,.0f}"


SHELL_TOP = """
<div class="topbar dashboard-topbar">
  <div class="dashboard-shell">
    <div class="dashboard-title-row">
      <div class="title"><h1>{{ title }}</h1></div>
    </div>
    <div class="controls">
      <div class="dashboard-menu-grid">
        <a class="button-link" href="{{ url_for('ads_kpi_dashboard') }}">← KPI quảng cáo (/ads-kpi)</a>
        <a class="button-link" href="{{ url_for('ads_allocation_index') }}">Phân bổ Ads theo SP (chính)</a>
        <a class="button-link" href="{{ url_for('ads_mapping_map') }}">Map tự động (deprecated)</a>
        <a class="button-link" href="{{ url_for('ads_mapping_unmapped') }}">Chưa map (deprecated)</a>
        <a class="button-link" href="{{ url_for('ads_mapping_rules') }}">Rule (deprecated)</a>
        <a class="button-link" href="{{ url_for('ads_mapping_product_cost') }}">Chi phí theo SP (deprecated)</a>
      </div>
    </div>
  </div>
</div>
<div class="dashboard-shell">
"""

SHELL_END = "</div>"


def _parse_rule_form(form) -> Dict[str, Any]:
    def d(name: str) -> Any:
        v = form.get(name, "")
        return str(v).strip() if v is not None else ""

    ef, et = d("effective_from"), d("effective_to")
    return {
        "fb_ad_account_id": d("fb_ad_account_id") or None,
        "match_level": d("match_level") or "name_pattern",
        "campaign_id": d("campaign_id") or None,
        "campaign_name": d("campaign_name") or None,
        "adset_id": d("adset_id") or None,
        "adset_name": d("adset_name") or None,
        "ad_id": d("ad_id") or None,
        "ad_name": d("ad_name") or None,
        "name_pattern": d("name_pattern") or None,
        "product_id": int(d("product_id") or "0"),
        "priority": int(d("priority") or "100"),
        "effective_from": ef or None,
        "effective_to": et or None,
        "is_active": bool(form.get("is_active")),
        "note": d("note") or None,
    }


def _normalize_rule_row(row: Dict[str, Any]) -> Dict[str, Any]:
    def ymd(v: Any) -> str:
        if v is None:
            return ""
        return str(v)[:10]

    return {
        "fb_ad_account_id": row.get("fb_ad_account_id") or "",
        "match_level": row.get("match_level") or "name_pattern",
        "campaign_id": row.get("campaign_id") or "",
        "campaign_name": row.get("campaign_name") or "",
        "adset_id": row.get("adset_id") or "",
        "adset_name": row.get("adset_name") or "",
        "ad_id": row.get("ad_id") or "",
        "ad_name": row.get("ad_name") or "",
        "name_pattern": row.get("name_pattern") or "",
        "product_id": int(row.get("product_id") or 0),
        "priority": int(row.get("priority") or 100),
        "effective_from": ymd(row.get("effective_from")),
        "effective_to": ymd(row.get("effective_to")),
        "is_active": bool(row.get("is_active", True)),
        "note": row.get("note") or "",
    }


def _rule_form(ctx: Dict[str, Any], product_rows: List[Dict[str, Any]]) -> str:
    pid = int(ctx.get("product_id") or 0)
    opts = "".join(
        f'<option value="{p["id"]}"{" selected" if pid == int(p["id"]) else ""}>'
        f'{html.escape(p["product_code"])}</option>'
        for p in product_rows
    )
    levels = ["ad", "adset", "campaign", "name_pattern"]
    ml = str(ctx.get("match_level") or "name_pattern")
    lvl_opts = "".join(f'<option value="{lv}"{" selected" if ml == lv else ""}>{lv}</option>' for lv in levels)
    return f"""
    <form method="post" class="settings-form">
      <div class="settings-grid-2">
        <select name="match_level">{lvl_opts}</select>
        <input type="text" name="fb_ad_account_id" value="{html.escape(str(ctx.get('fb_ad_account_id') or ''))}" placeholder="fb_ad_account_id (tuỳ chọn)">
      </div>
      <div class="settings-grid-2">
        <input type="text" name="campaign_id" value="{html.escape(str(ctx.get('campaign_id') or ''))}" placeholder="campaign_id">
        <input type="text" name="campaign_name" value="{html.escape(str(ctx.get('campaign_name') or ''))}" placeholder="campaign_name">
      </div>
      <div class="settings-grid-2">
        <input type="text" name="adset_id" value="{html.escape(str(ctx.get('adset_id') or ''))}" placeholder="adset_id">
        <input type="text" name="adset_name" value="{html.escape(str(ctx.get('adset_name') or ''))}" placeholder="adset_name">
      </div>
      <div class="settings-grid-2">
        <input type="text" name="ad_id" value="{html.escape(str(ctx.get('ad_id') or ''))}" placeholder="ad_id">
        <input type="text" name="ad_name" value="{html.escape(str(ctx.get('ad_name') or ''))}" placeholder="ad_name">
      </div>
      <p><input type="text" name="name_pattern" value="{html.escape(str(ctx.get('name_pattern') or ''))}" placeholder="name_pattern (substring)" style="width:100%;max-width:520px;"></p>
      <div class="settings-grid-2">
        <select name="product_id" required>{opts}</select>
        <input type="number" name="priority" value="{int(ctx.get('priority') or 100)}" placeholder="priority">
      </div>
      <div class="settings-grid-2">
        <input type="date" name="effective_from" value="{html.escape(str(ctx.get('effective_from') or ''))}">
        <input type="date" name="effective_to" value="{html.escape(str(ctx.get('effective_to') or ''))}">
      </div>
      <p><label><input type="checkbox" name="is_active" value="1" {" checked" if ctx.get("is_active", True) else ""}> Active</label></p>
      <p><textarea name="note" rows="2" style="width:100%;max-width:520px;" placeholder="Ghi chú">{html.escape(str(ctx.get("note") or ""))}</textarea></p>
      <button class="button" type="submit">Lưu</button>
      <a class="button-link" href="{url_for("ads_mapping_rules")}">Danh sách</a>
    </form>
    """


def register_ads_mapping_routes(
    app,
    *,
    login_required: Callable,
    page_template: str,
    get_allowed_shop_keys: Callable[[], Optional[set]],
    can_view_ads_global: Callable[[], bool],
) -> None:
    @app.route("/ads-mapping")
    @login_required
    def ads_mapping_hub():
        # Luồng chính: phân bổ tay theo ngày (/ads-allocation). Mapping rule tự động giữ URL cũ cho tương thích.
        return redirect(url_for("ads_allocation_index"))

    @app.route("/ads-mapping/map", methods=["GET", "POST"])
    @login_required
    def ads_mapping_map():
        allowed = get_allowed_shop_keys()
        can_mutate = can_view_ads_global()
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
        messages: List[Dict[str, str]] = []
        if not repo.mapping_schema_ready():
            inner = (
                SHELL_TOP
                + '<div class="alert"><small>DB</small><div>Thiếu migration 011.</div></div>'
                + SHELL_END
            )
            return _wrap(page_template, "Map Ads → SP", render_template_string(inner, title="Map"))

        if request.method == "POST":
            action = request.form.get("action", "").strip()
            df = _parse_ymd(request.form.get("date_from"), today)
            dt = _parse_ymd(request.form.get("date_to"), today)
            if df > dt:
                df, dt = dt, df
            fb_acc = request.form.get("fb_ad_account_id", "").strip() or None
            try:
                if action == "rebuild_mapping":
                    if not can_mutate:
                        messages.append({"level": "Quyền", "text": "Chỉ admin / kế toán được rebuild mapping."})
                    else:
                        stats = service.rebuild_mapping_for_range(df, dt, allowed, fb_ad_account_id=fb_acc)
                        messages.append({
                            "level": "Rebuild",
                            "text": f"Đã xóa auto {stats['deleted_auto_rows']} dòng, áp lại {stats['auto_applied']} dòng; giữ {stats['manual_preserved']} map tay.",
                        })
                elif action == "rebuild_aggregate":
                    if not can_mutate:
                        messages.append({"level": "Quyền", "text": "Chỉ admin / kế toán được aggregate."})
                    else:
                        st = service.rebuild_product_cost_aggregate(df, dt)
                        messages.append({
                            "level": "Aggregate",
                            "text": f"product_ads_cost_daily: xóa {st['aggregate_deleted']} dòng cũ, thêm {st['aggregate_inserted']} dòng (khoảng ngày).",
                        })
                elif action == "manual_map":
                    ads_raw_id = int(request.form.get("ads_raw_id", "0") or 0)
                    product_id = int(request.form.get("product_id", "0") or 0)
                    if product_id <= 0:
                        messages.append({"level": "Lỗi", "text": "Chọn sản phẩm trước khi map tay."})
                    else:
                        service.manual_map_row(ads_raw_id, product_id, _actor(), allowed)
                        messages.append({"level": "OK", "text": "Đã map tay."})
                elif action == "create_rule_from_row":
                    if not can_mutate:
                        messages.append({"level": "Quyền", "text": "Chỉ admin / kế toán tạo rule."})
                    else:
                        ads_raw_id = int(request.form.get("ads_raw_id", "0") or 0)
                        rule_product_id = int(request.form.get("product_id", "0") or 0)
                        if rule_product_id <= 0:
                            messages.append({"level": "Lỗi", "text": "Chọn sản phẩm khi tạo rule."})
                        else:
                            raw_row = repo.get_raw_row_for_access(ads_raw_id, allowed)
                            if not raw_row:
                                messages.append({"level": "Lỗi", "text": "Không đọc được dòng raw."})
                            else:
                                pname = request.form.get("name_pattern", "").strip()
                                if not pname:
                                    pname = str(raw_row.get("fb_ad_account_id") or "")
                                rid = repo.insert_rule(
                                    {
                                        "fb_ad_account_id": (raw_row.get("fb_ad_account_id") or None),
                                        "match_level": "name_pattern",
                                        "campaign_id": None,
                                        "campaign_name": None,
                                        "adset_id": None,
                                        "adset_name": None,
                                        "ad_id": None,
                                        "ad_name": None,
                                        "name_pattern": pname or None,
                                        "product_id": rule_product_id,
                                        "priority": int(request.form.get("priority", "100") or 100),
                                        "effective_from": None,
                                        "effective_to": None,
                                        "is_active": True,
                                        "note": request.form.get("note", "").strip() or None,
                                    }
                                )
                                messages.append({"level": "OK", "text": f"Đã tạo rule #{rid}."})
            except Exception as exc:
                messages.append({"level": "Lỗi", "text": str(exc)})

        df = _parse_ymd(request.args.get("from") or request.form.get("date_from"), today)
        dt = _parse_ymd(request.args.get("to") or request.form.get("date_to"), today)
        if df > dt:
            df, dt = dt, df
        fb_acc = (request.args.get("fb_ad_account_id") or request.form.get("fb_ad_account_id") or "").strip() or None
        kw = (request.args.get("q") or "").strip()
        st_f = (request.args.get("status") or "").strip()
        prod_f = request.args.get("product_id", "").strip()
        product_id = int(prod_f) if prod_f.isdigit() else None
        rows = repo.list_map_rows_enriched(
            df,
            dt,
            allowed,
            fb_account=fb_acc,
            campaign_kw=kw,
            status_filter=st_f,
            product_id=product_id,
            limit=500,
        )
        products = repo.list_products_for_select()
        msg_html = ""
        for m in messages:
            msg_html += f'<div class="alert"><small>{html.escape(m["level"])}</small><div>{html.escape(m["text"])}</div></div>'

        rows_html = ""
        for r in rows:
            pid = r.get("product_id")
            spend = _money(r.get("spend"))
            try:
                spv = float(r.get("spend") or 0)
            except (TypeError, ValueError):
                spv = 0.0
            vat = _money(spv * ADS_VAT_RATE)
            pname = html.escape(str(r.get("product_name") or "—"))
            mth = html.escape(str(r.get("mapping_method") or ""))
            st = html.escape(str(r.get("status") or ""))
            conf = r.get("confidence")
            acc = html.escape(str(r.get("fb_ad_account_id") or ""))
            an = html.escape(str(r.get("account_name") or ""))
            sk = html.escape(str(r.get("shop_key") or ""))
            options = "".join(
                f'<option value="{p["id"]}"{" selected" if pid and int(p["id"]) == int(pid) else ""}>'
                f'{html.escape(p["product_code"])} — {html.escape(p["product_name"])}</option>'
                for p in products
            )
            manual_form = ""
            if products:
                manual_form = f"""
                <form method="post" style="display:inline;">
                  <input type="hidden" name="action" value="manual_map">
                  <input type="hidden" name="date_from" value="{html.escape(df)}">
                  <input type="hidden" name="date_to" value="{html.escape(dt)}">
                  <input type="hidden" name="ads_raw_id" value="{int(r["ads_raw_id"])}">
                  <select name="product_id" style="max-width:180px;">{options}</select>
                  <button class="button-link" type="submit">Map tay</button>
                </form>
                """
            rule_form = ""
            if can_mutate and products:
                rule_form = f"""
                <form method="post" style="display:inline-block;margin-top:4px;">
                  <input type="hidden" name="action" value="create_rule_from_row">
                  <input type="hidden" name="date_from" value="{html.escape(df)}">
                  <input type="hidden" name="date_to" value="{html.escape(dt)}">
                  <input type="hidden" name="ads_raw_id" value="{int(r["ads_raw_id"])}">
                  <select name="product_id" style="max-width:160px;">{options}</select>
                  <input type="text" name="name_pattern" placeholder="pattern (mặc định act_id)" style="max-width:140px;">
                  <input type="number" name="priority" value="100" style="width:70px;">
                  <button class="button-link" type="submit">Tạo rule</button>
                </form>
                """
            rows_html += f"""
            <tr>
              <td>{r["stat_date"]}</td>
              <td>{sk}</td>
              <td>—</td><td>—</td><td>—</td>
              <td>{acc}<br><small>{an}</small></td>
              <td>{spend}</td>
              <td>{vat}</td>
              <td>{pname}</td>
              <td>{mth}</td>
              <td>{conf}</td>
              <td>{st}</td>
              <td>{manual_form}{rule_form}</td>
            </tr>
            """

        inner = render_template_string(
            SHELL_TOP
            + """
  {{ msg_html|safe }}
  <div class="card" style="margin-bottom:14px;">
    <div class="section-title"><h2>Map Campaign / tài khoản → Sản phẩm</h2></div>
    <p style="color:#64748b;font-size:14px;">Cột Campaign / Adset / Ad hiển thị "—" vì sync hiện chỉ lưu mức <strong>tài khoản + ngày</strong>. Khi sync mở rộng, có thể bơm thêm cột vào raw và engine.</p>
    <form method="get" class="settings-form">
      <div class="settings-grid-2">
        <label>Từ <input type="date" name="from" value="{{ df }}"></label>
        <label>Đến <input type="date" name="to" value="{{ dt }}"></label>
      </div>
      <div class="settings-grid-2">
        <input type="text" name="fb_ad_account_id" value="{{ fb_acc or '' }}" placeholder="Lọc fb_ad_account_id (tuỳ chọn)">
        <input type="text" name="q" value="{{ kw_e }}" placeholder="Tìm trong account name / id">
      </div>
      <div class="settings-grid-2">
        <select name="status">
          <option value="" {% if not st_f %}selected{% endif %}>Mọi trạng thái</option>
          <option value="mapped" {% if st_f == 'mapped' %}selected{% endif %}>Đã map</option>
          <option value="unmapped" {% if st_f == 'unmapped' %}selected{% endif %}>Chưa map</option>
          <option value="low_confidence" {% if st_f == 'low_confidence' %}selected{% endif %}>Low confidence</option>
        </select>
        <select name="product_id">
          <option value="">Mọi SP</option>
          {% for p in products %}
          <option value="{{ p.id }}" {% if prod_sel == p.id|string %}selected{% endif %}>{{ p.product_code }}</option>
          {% endfor %}
        </select>
      </div>
      <button class="button" type="submit">Lọc</button>
    </form>
    {% if can_mutate %}
    <form method="post" class="settings-form" style="margin-top:12px;">
      <input type="hidden" name="date_from" value="{{ df }}">
      <input type="hidden" name="date_to" value="{{ dt }}">
      <input type="hidden" name="fb_ad_account_id" value="{{ fb_acc or '' }}">
      <button class="button" type="submit" name="action" value="rebuild_mapping">Rebuild mapping (theo filter ngày + shop scope)</button>
      <button class="button" type="submit" name="action" value="rebuild_aggregate" style="margin-left:8px;">Rebuild tổng SP (product_ads_cost_daily)</button>
    </form>
    {% endif %}
  </div>
  <div class="card">
    <div class="section-title"><h2>Dữ liệu</h2><span>Tối đa 500 dòng</span></div>
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Ngày</th><th>Shop</th><th>Campaign</th><th>Adset</th><th>Ad</th>
          <th>Account</th><th>Spend</th><th>VAT*</th><th>SP</th><th>Method</th><th>Conf</th><th>Status</th><th>Thao tác</th>
        </tr>
      </thead>
      <tbody>
        {{ rows_html|safe }}
      </tbody>
    </table>
    </div>
    <p style="font-size:13px;color:#64748b;">VAT ước tính theo hệ số {{ vat_rate }} (giống KPI quảng cáo).</p>
  </div>
"""
            + SHELL_END,
            title="Map Ads → SP",
            msg_html=msg_html,
            df=df,
            dt=dt,
            fb_acc=fb_acc or "",
            kw_e=html.escape(kw),
            st_f=st_f,
            prod_sel=prod_f,
            products=products,
            rows_html=rows_html,
            can_mutate=can_mutate,
            vat_rate=ADS_VAT_RATE,
        )
        return _wrap(page_template, "Map Ads → SP", inner)

    @app.route("/ads-mapping/unmapped")
    @login_required
    def ads_mapping_unmapped():
        allowed = get_allowed_shop_keys()
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
        df = _parse_ymd(request.args.get("from"), today)
        dt = _parse_ymd(request.args.get("to"), today)
        if not repo.mapping_schema_ready():
            inner = SHELL_TOP + '<div class="alert">Thiếu migration 011.</div>' + SHELL_END
            return _wrap(page_template, "Chưa map", render_template_string(inner, title="Chưa map"))
        rows = repo.list_unmapped_operational(df, dt, allowed, limit=400)
        body_rows = ""
        for r in rows:
            body_rows += f"""
            <tr>
              <td>{r["stat_date"]}</td>
              <td>{html.escape(str(r.get("shop_key") or ""))}</td>
              <td>{html.escape(str(r.get("fb_ad_account_id") or ""))}</td>
              <td>{_money(r.get("spend"))}</td>
              <td>{html.escape(str(r.get("result_status") or ""))}</td>
              <td>{html.escape(str(r.get("queue_reason") or ""))}</td>
              <td>{html.escape(str(r.get("queue_status") or ""))}</td>
            </tr>
            """
        inner = render_template_string(
            SHELL_TOP
            + """
  <div class="card">
    <div class="section-title"><h2>Campaign / tài khoản chưa map hoặc low confidence</h2></div>
    <form method="get" class="filter-form">
      <input type="date" name="from" value="{{ df }}">
      <input type="date" name="to" value="{{ dt }}">
      <button class="button" type="submit">Xem</button>
    </form>
    <div class="table-scroll" style="margin-top:12px;">
    <table>
      <thead>
        <tr><th>Ngày</th><th>Shop</th><th>Account</th><th>Spend</th><th>Result</th><th>Queue reason</th><th>Queue</th></tr>
      </thead>
      <tbody>{{ body_rows|safe }}</tbody>
    </table>
    </div>
    <p style="margin-top:10px;"><a class="button-link" href="{{ url_for('ads_mapping_map', from=df, to=dt) }}">Mở màn Map để xử lý</a></p>
  </div>
"""
            + SHELL_END,
            title="Chưa map",
            df=df,
            dt=dt,
            body_rows=body_rows,
        )
        return _wrap(page_template, "Chưa map", inner)

    @app.route("/ads-mapping/product-cost", methods=["GET", "POST"])
    @login_required
    def ads_mapping_product_cost():
        allowed = get_allowed_shop_keys()
        can_mutate = can_view_ads_global()
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
        messages: List[str] = []
        if not repo.mapping_schema_ready():
            inner = SHELL_TOP + '<div class="alert">Thiếu migration 011.</div>' + SHELL_END
            return _wrap(page_template, "Chi phí SP", render_template_string(inner, title="Chi phí SP"))
        if request.method == "POST" and request.form.get("action") == "rebuild_aggregate":
            if not can_mutate:
                messages.append("Chỉ admin / kế toán rebuild aggregate.")
            else:
                df = _parse_ymd(request.form.get("date_from"), today)
                dt = _parse_ymd(request.form.get("date_to"), today)
                if df > dt:
                    df, dt = dt, df
                try:
                    st = service.rebuild_product_cost_aggregate(df, dt)
                    messages.append(
                        f"Đã rebuild product_ads_cost_daily: xóa {st['aggregate_deleted']} dòng, thêm {st['aggregate_inserted']} dòng."
                    )
                except Exception as exc:
                    messages.append(str(exc))
        df = _parse_ymd(request.args.get("from") or request.form.get("date_from"), today)
        dt = _parse_ymd(request.args.get("to") or request.form.get("date_to"), today)
        if df > dt:
            df, dt = dt, df
        prod_f = (request.args.get("product_id") or "").strip()
        product_id = int(prod_f) if prod_f.isdigit() else None
        cost_rows = repo.list_product_ads_cost_daily(df, dt, allowed, product_id=product_id, limit=500)
        msg_html = "".join(f'<div class="alert">{html.escape(m)}</div>' for m in messages)
        tbl = ""
        for cr in cost_rows:
            tbl += f"""
            <tr>
              <td>{cr["stat_date"]}</td>
              <td>{html.escape(str(cr.get("product_code") or ""))}</td>
              <td>{html.escape(str(cr.get("product_name") or ""))}</td>
              <td>{_money(cr.get("total_spend"))}</td>
              <td>{_money(cr.get("total_vat"))}</td>
              <td>{_money(cr.get("total_cost"))}</td>
              <td>{cr.get("source_rows_count")}</td>
            </tr>
            """
        inner = render_template_string(
            SHELL_TOP
            + """
  {{ msg_html|safe }}
  <div class="card" style="margin-bottom:14px;">
    <div class="section-title"><h2>Chi phí Ads theo sản phẩm</h2><span>product_ads_cost_daily</span></div>
    <form method="get" class="settings-form">
      <div class="settings-grid-2">
        <label>Từ <input type="date" name="from" value="{{ df }}"></label>
        <label>Đến <input type="date" name="to" value="{{ dt }}"></label>
      </div>
      <button class="button" type="submit">Lọc</button>
    </form>
    {% if can_mutate %}
    <form method="post" class="settings-form" style="margin-top:12px;">
      <input type="hidden" name="date_from" value="{{ df }}">
      <input type="hidden" name="date_to" value="{{ dt }}">
      <input type="hidden" name="action" value="rebuild_aggregate">
      <button class="button" type="submit">Rebuild tổng hợp (toàn cục theo khoảng ngày)</button>
    </form>
    {% endif %}
  </div>
  <div class="card">
    <div class="table-scroll">
    <table>
      <thead>
        <tr><th>Ngày</th><th>Mã SP</th><th>Tên SP</th><th>Spend</th><th>VAT</th><th>Total</th><th>Số dòng nguồn</th></tr>
      </thead>
      <tbody>{{ tbl|safe }}</tbody>
    </table>
    </div>
  </div>
"""
            + SHELL_END,
            title="Chi phí SP",
            msg_html=msg_html,
            df=df,
            dt=dt,
            tbl=tbl,
            can_mutate=can_mutate,
        )
        return _wrap(page_template, "Chi phí Ads theo SP", inner)

    @app.route("/ads-mapping/rules", methods=["GET", "POST"])
    @login_required
    def ads_mapping_rules():
        if not can_view_ads_global():
            inner = SHELL_TOP + '<div class="alert">Chỉ admin / kế toán.</div>' + SHELL_END
            return _wrap(page_template, "Rules", render_template_string(inner, title="Rules"))
        if not repo.mapping_schema_ready():
            inner = SHELL_TOP + '<div class="alert">Thiếu migration 011.</div>' + SHELL_END
            return _wrap(page_template, "Rules", render_template_string(inner, title="Rules"))
        messages: List[str] = []
        if request.method == "POST":
            action = request.form.get("action", "").strip()
            rid = request.form.get("rule_id", "").strip()
            if action == "delete" and rid.isdigit():
                repo.delete_rule(int(rid))
                messages.append("Đã xóa rule.")
        rules = repo.list_mapping_rules()
        rows = ""
        for r in rules:
            ef = r.get("effective_from") or "—"
            et = r.get("effective_to") or "—"
            active = "yes" if r.get("is_active") else "no"
            rows += f"""
            <tr>
              <td>{r["id"]}</td>
              <td>{html.escape(str(r.get("match_level") or ""))}</td>
              <td>{html.escape(str(r.get("fb_ad_account_id") or "—"))}</td>
              <td>{html.escape(str(r.get("name_pattern") or "—"))}</td>
              <td>{html.escape(str(r.get("product_code") or ""))}</td>
              <td>{r.get("priority")}</td>
              <td>{active}</td>
              <td>{ef} → {et}</td>
              <td>
                <a class="button-link" href="{url_for("ads_mapping_rule_edit", rule_id=r["id"])}">Sửa</a>
                <form method="post" style="display:inline;" onsubmit="return confirm('Xóa rule?');">
                  <input type="hidden" name="action" value="delete">
                  <input type="hidden" name="rule_id" value="{r["id"]}">
                  <button class="button-link" type="submit">Xóa</button>
                </form>
              </td>
            </tr>
            """
        msg = "".join(f'<div class="alert">{html.escape(m)}</div>' for m in messages)
        inner = render_template_string(
            SHELL_TOP
            + """
  {{ msg|safe }}
  <div class="card">
    <div class="section-title"><h2>Rule mapping</h2>
      <a class="button-link" href="{{ url_for('ads_mapping_rule_new') }}">+ Thêm rule</a>
    </div>
    <div class="table-scroll">
    <table>
      <thead>
        <tr><th>ID</th><th>Level</th><th>FB account</th><th>Pattern / ids</th><th>SP</th><th>Prio</th><th>Active</th><th>Hiệu lực</th><th></th></tr>
      </thead>
      <tbody>{{ rows|safe }}</tbody>
    </table>
    </div>
  </div>
"""
            + SHELL_END,
            title="Rules",
            msg=msg,
            rows=rows,
        )
        return _wrap(page_template, "Rules", inner)

    @app.route("/ads-mapping/rules/new", methods=["GET", "POST"])
    @login_required
    def ads_mapping_rule_new():
        if not can_view_ads_global():
            return redirect(url_for("ads_mapping_hub"))
        if not repo.mapping_schema_ready():
            return redirect(url_for("ads_mapping_hub"))
        products = repo.list_products_for_select()
        ctx: Dict[str, Any] = {
            "match_level": "name_pattern",
            "priority": 100,
            "is_active": True,
        }
        if request.method == "POST":
            ctx = _parse_rule_form(request.form)
            if ctx["product_id"] <= 0:
                ctx["_err"] = "Chọn sản phẩm."
            else:
                try:
                    repo.insert_rule(ctx)
                    return redirect(url_for("ads_mapping_rules"))
                except Exception as exc:
                    ctx["_err"] = str(exc)
        err = html.escape(str(ctx.get("_err", "")))
        inner = (
            SHELL_TOP
            + f'<div class="card"><h2>Rule mới</h2>{err and f"<div class=\"alert\">{err}</div>"}'
            + _rule_form(ctx, products)
            + "</div>"
            + SHELL_END
        )
        return _wrap(page_template, "Rule mới", render_template_string(inner, title="Rule mới"))

    @app.route("/ads-mapping/rules/<int:rule_id>/edit", methods=["GET", "POST"])
    @login_required
    def ads_mapping_rule_edit(rule_id: int):
        if not can_view_ads_global():
            return redirect(url_for("ads_mapping_hub"))
        if not repo.mapping_schema_ready():
            return redirect(url_for("ads_mapping_hub"))
        row = repo.get_rule(rule_id)
        if not row:
            return redirect(url_for("ads_mapping_rules"))
        products = repo.list_products_for_select()
        ctx: Dict[str, Any] = _normalize_rule_row(row)
        if request.method == "POST":
            ctx = _parse_rule_form(request.form)
            if ctx["product_id"] <= 0:
                ctx["_err"] = "Chọn sản phẩm."
            else:
                ctx["id"] = rule_id
                try:
                    repo.update_rule(rule_id, ctx)
                    return redirect(url_for("ads_mapping_rules"))
                except Exception as exc:
                    ctx["_err"] = str(exc)
        err = html.escape(str(ctx.get("_err", "")))
        inner = (
            SHELL_TOP
            + f'<div class="card"><h2>Sửa rule #{rule_id}</h2>{err and f"<div class=\"alert\">{err}</div>"}'
            + _rule_form(ctx, products)
            + "</div>"
            + SHELL_END
        )
        return _wrap(page_template, "Sửa rule", render_template_string(inner, title="Sửa rule"))
