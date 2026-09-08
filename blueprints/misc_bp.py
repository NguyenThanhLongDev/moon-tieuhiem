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

misc_bp = Blueprint("misc", __name__)

@misc_bp.route("/download-source")
def download_source():
    import os as _os
    f = _os.path.join(_os.path.dirname(__file__), "posbot_source_20260413.tar.gz")
    if not _os.path.exists(f):
        abort(404)
    return send_file(f, as_attachment=True, download_name="posbot_source_20260413.tar.gz")

@misc_bp.route("/huong-dan")
@login_required
def huong_dan():
    body = """
<style>
.hd-wrap{max-width:960px;margin:0 auto;padding:0 12px 60px;}
.hd-hero{background:linear-gradient(135deg,#1e40af,#7c3aed);color:#fff;border-radius:16px;padding:32px 28px 24px;margin-bottom:32px;}
.hd-hero h1{font-size:1.8rem;font-weight:800;margin:0 0 6px;}
.hd-hero p{opacity:.85;margin:0;font-size:.97rem;}
.hd-section{margin-bottom:40px;}
.hd-section-title{font-size:1.15rem;font-weight:700;color:#1e40af;border-left:4px solid #1e40af;padding-left:12px;margin-bottom:16px;display:flex;align-items:center;gap:8px;}
.hd-section-title span{font-size:1.3rem;}
.hd-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:12px;overflow:hidden;}
.hd-card-head{background:#f8fafc;padding:12px 16px;cursor:pointer;display:flex;align-items:center;gap:10px;font-weight:600;font-size:.95rem;user-select:none;border-bottom:1px solid #e5e7eb;}
.hd-card-head .hd-url{margin-left:auto;font-size:.78rem;background:#e0e7ff;color:#3730a3;border-radius:6px;padding:2px 8px;font-weight:500;font-family:monospace;}
.hd-card-head .hd-badge{font-size:.73rem;padding:2px 8px;border-radius:20px;font-weight:600;}
.badge-admin{background:#fee2e2;color:#991b1b;}
.badge-leader{background:#fef3c7;color:#92400e;}
.badge-all{background:#d1fae5;color:#065f46;}
.hd-card-body{padding:14px 16px 16px;font-size:.9rem;line-height:1.7;color:#374151;}
.hd-card-body ul{margin:8px 0 0 0;padding-left:20px;}
.hd-card-body li{margin-bottom:4px;}
.hd-card-body .tip{background:#eff6ff;border-left:3px solid #3b82f6;border-radius:0 8px 8px 0;padding:8px 12px;margin-top:10px;font-size:.87rem;color:#1e40af;}
.hd-card-body b{color:#111827;}
.hd-role-table{width:100%;border-collapse:collapse;margin-top:10px;font-size:.87rem;}
.hd-role-table th{background:#f3f4f6;padding:7px 10px;text-align:left;font-weight:600;border-bottom:1px solid #e5e7eb;}
.hd-role-table td{padding:7px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top;}
.hd-role-table tr:last-child td{border-bottom:none;}
.hd-icon{font-size:1.1rem;}
.hd-toc{background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;padding:16px 20px;margin-bottom:32px;}
.hd-toc-title{font-weight:700;color:#111827;margin-bottom:10px;font-size:.97rem;}
.hd-toc ul{margin:0;padding-left:20px;}
.hd-toc li{margin-bottom:4px;}
.hd-toc a{color:#3b82f6;text-decoration:none;font-size:.9rem;}
.hd-toc a:hover{text-decoration:underline;}
</style>

<div class="hd-wrap">

<div class="hd-hero">
  <h1>📖 Hướng dẫn sử dụng — Tiểu Hiềm Software</h1>
  <p>Tài liệu đầy đủ tất cả tính năng của hệ thống POS + Chấm công & Phân công việc</p>
</div>

<!-- MỤC LỤC -->
<div class="hd-toc">
  <div class="hd-toc-title">📋 Mục lục nhanh</div>
  <ul>
    <li><a href="#pos">Phần 1 — Hệ thống POS (Quản lý bán hàng)</a>
      <ul>
        <li><a href="#dashboard">1.1 Dashboard chính</a></li>
        <li><a href="#shop-detail">1.2 Chi tiết từng shop</a></li>
        <li><a href="#stock">1.3 Tồn kho</a></li>
        <li><a href="#slow">1.4 Bán chậm</a></li>
        <li><a href="#sent">1.5 Hàng đã gửi (xuất kho)</a></li>
        <li><a href="#export-v2">1.6 Xuất hàng v2</a></li>
        <li><a href="#ads-kpi">1.7 Ads KPI (Quảng cáo)</a></li>
        <li><a href="#ads-delay">1.8 QC chậm &gt;2 ngày</a></li>
        <li><a href="#loss">1.9 Shop lỗ theo POS</a></li>
        <li><a href="#kho-vat-ly">1.10 Kho vật lý</a></li>
        <li><a href="#chi-phi-qc">1.11 Chi phí QC</a></li>
        <li><a href="#settings">1.12 Cài đặt hệ thống</a></li>
      </ul>
    </li>
    <li><a href="#chamcong">Phần 2 — Chấm công & Phân công việc</a>
      <ul>
        <li><a href="#cc-dashboard">2.1 Dashboard cá nhân</a></li>
        <li><a href="#cc-checkin">2.2 Chấm công (Check-in / Check-out)</a></li>
        <li><a href="#cc-history">2.3 Lịch sử chấm công</a></li>
        <li><a href="#cc-tasks">2.4 Công việc (Task)</a></li>
        <li><a href="#cc-kpi">2.5 KPI hàng ngày</a></li>
        <li><a href="#cc-giam-sat">2.6 Giám sát realtime (Leader)</a></li>
        <li><a href="#cc-admin">2.7 Quản lý (Admin)</a></li>
      </ul>
    </li>
    <li><a href="#roles">Phần 3 — Phân quyền</a></li>
  </ul>
</div>

<!-- ═══════════════════════════════ PHẦN 1 ═══════════════════════════════ -->
<div class="hd-section" id="pos">
  <div class="hd-section-title"><span>🏪</span> Phần 1 — Hệ thống POS (Quản lý bán hàng)</div>

  <!-- 1.1 Dashboard -->
  <div class="hd-card" id="dashboard">
    <div class="hd-card-head">
      <span class="hd-icon">📊</span> 1.1 Dashboard chính
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/</span>
    </div>
    <div class="hd-card-body">
      Trang tổng quan toàn bộ hệ thống 38 shop. Hiển thị số liệu đơn hàng, doanh thu, trạng thái shop theo ngày.
      <ul>
        <li><b>Chọn ngày:</b> Bấm vào ô ngày (mặc định hôm nay) để xem số liệu ngày bất kỳ.</li>
        <li><b>Lọc theo nhân viên:</b> Dropdown "Staff" — chỉ hiện shop của NV đó phụ trách.</li>
        <li><b>Lọc theo team:</b> Admin/Kế toán có thể chọn team để thu hẹp danh sách shop.</li>
        <li><b>Lọc khoảng ngày:</b> Nhập "Từ ngày" + "Đến ngày" để xem tổng hợp nhiều ngày.</li>
        <li><b>Thẻ shop:</b> Mỗi shop hiển thị đơn mới, đơn tạo, doanh thu, trạng thái (xanh/vàng/đỏ).</li>
        <li><b>Đồng bộ:</b> Nút "Đồng bộ ngay" để kéo dữ liệu mới nhất từ Pancake POS.</li>
        <li><b>Xuất Excel:</b> Tải báo cáo tổng hợp ra file .xlsx.</li>
      </ul>
      <div class="tip">💡 Cột "Đơn mới" lấy từ cache live (bao gồm đơn đổi hàng). Cột "Đơn tạo" lấy từ API analytics (không tính exchange). Chênh lệch là bình thường.</div>
    </div>
  </div>

  <!-- 1.2 Chi tiết shop -->
  <div class="hd-card" id="shop-detail">
    <div class="hd-card-head">
      <span class="hd-icon">🏬</span> 1.2 Chi tiết từng shop
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/shop/[tên_shop]</span>
    </div>
    <div class="hd-card-body">
      Bấm vào tên bất kỳ shop trên Dashboard để vào trang chi tiết shop đó.
      <ul>
        <li><b>Thống kê ngày:</b> Tổng đơn, doanh thu, đơn giao thành công, đơn hoàn trả.</li>
        <li><b>Danh sách đơn:</b> Từng đơn hàng theo trạng thái (đang giao, giao xong, hoàn, hủy...).</li>
        <li><b>Cảnh báo:</b> Tự động highlight shop có vấn đề (ít đơn, doanh thu âm, nhiều hoàn...).</li>
        <li><b>Chọn ngày:</b> Xem lại lịch sử ngày bất kỳ qua ô date.</li>
      </ul>
    </div>
  </div>

  <!-- 1.3 Tồn kho -->
  <div class="hd-card" id="stock">
    <div class="hd-card-head">
      <span class="hd-icon">📦</span> 1.3 Tồn kho
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/stock</span>
    </div>
    <div class="hd-card-body">
      Tổng quan hàng tồn kho toàn bộ 38 shop — phát hiện nhanh shop hết hàng, hàng thấp, hàng ế.
      <ul>
        <li><b>Thẻ tổng quan:</b> Tổng số shop hết hàng, tồn thấp, bán chậm, tổng giá trị kho.</li>
        <li><b>Bảng shop:</b> Từng shop với số SKU hết, thấp, chậm — bấm vào shop để xem chi tiết.</li>
        <li><b>Top 10 sản phẩm tồn nhiều nhất:</b> Hiển thị ở cuối trang.</li>
        <li><b>Chi tiết shop:</b> (<code>/stock/[tên_shop]</code>) Xem từng sản phẩm: mã, tên, số lượng, giá nhập, tổng giá trị.</li>
        <li><b>Lọc ngày:</b> Xem tồn kho ngày bất kỳ (nếu có dữ liệu lịch sử).</li>
      </ul>
      <div class="tip">💡 Hàng "tồn thấp" = dưới ngưỡng cảnh báo đã cài. Hàng "bán chậm" = không xuất trong 7 ngày mà vẫn còn tồn.</div>
    </div>
  </div>

  <!-- 1.4 Bán chậm -->
  <div class="hd-card" id="slow">
    <div class="hd-card-head">
      <span class="hd-icon">🐢</span> 1.4 Bán chậm
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/slow</span>
    </div>
    <div class="hd-card-body">
      Danh sách sản phẩm bán chậm (ít xuất trong 7 ngày gần nhất mà vẫn còn tồn).
      <ul>
        <li><b>Tổng quan:</b> Số shop có hàng chậm, tổng SKU chậm, tổng số lượng ứ đọng.</li>
        <li><b>Bảng shop:</b> Xếp theo số lượng hàng chậm giảm dần — bấm vào shop để xem chi tiết.</li>
        <li><b>Chi tiết shop:</b> (<code>/slow/[tên_shop]</code>) Từng sản phẩm: mã, tên, xuất 7 ngày, tồn hiện tại, tồn 7 ngày trước.</li>
      </ul>
      <div class="tip">💡 Dùng trang này để quyết định đẩy sale, giảm giá hoặc điều phối hàng sang shop khác.</div>
    </div>
  </div>

  <!-- 1.5 Hàng đã gửi -->
  <div class="hd-card" id="sent">
    <div class="hd-card-head">
      <span class="hd-icon">🚚</span> 1.5 Hàng đã gửi (Xuất kho)
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/sent-items</span>
    </div>
    <div class="hd-card-body">
      Theo dõi hàng đã xuất kho giao cho đơn vị vận chuyển — tổng hợp theo shop và sản phẩm.
      <ul>
        <li><b>Chọn ngày / shop:</b> Lọc theo ngày cụ thể hoặc từng shop.</li>
        <li><b>Tổng quan:</b> Tổng đơn giao, tổng sản phẩm, tổng SKU riêng biệt, số đơn shipper đã lấy.</li>
        <li><b>Shop xuất nhiều nhất:</b> Hiển thị top shop.</li>
        <li><b>Top sản phẩm:</b> Sản phẩm được xuất nhiều nhất trong ngày.</li>
        <li><b>Chi tiết từng shop:</b> (<code>/sent-items/[tên_shop]</code>) Từng sản phẩm xuất với số lượng và đơn hàng tương ứng.</li>
        <li><b>Xuất Excel:</b> Tải toàn bộ dữ liệu ra file .xlsx.</li>
      </ul>
    </div>
  </div>

  <!-- 1.6 Xuất hàng v2 -->
  <div class="hd-card" id="export-v2">
    <div class="hd-card-head">
      <span class="hd-icon">📋</span> 1.6 Xuất hàng v2 (Báo cáo nâng cao)
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/export-items-v2</span>
    </div>
    <div class="hd-card-body">
      Báo cáo sản phẩm xuất kho nâng cao — chi tiết hơn /sent-items, phù hợp cho kho và kế toán.
      <ul>
        <li><b>Chọn ngày / shop:</b> Lọc linh hoạt theo ngày và từng shop.</li>
        <li><b>Bảng chi tiết:</b> Từng sản phẩm với mã SKU, tên, số lượng xuất, shop nguồn, ngày.</li>
        <li><b>Tổng hợp:</b> Tổng số lượng, tổng giá trị, số SKU khác nhau.</li>
        <li><b>Xuất Excel:</b> File .xlsx có đầy đủ cột để nộp cho kế toán hoặc đối chiếu kho.</li>
      </ul>
    </div>
  </div>

  <!-- 1.7 Ads KPI -->
  <div class="hd-card" id="ads-kpi">
    <div class="hd-card-head">
      <span class="hd-icon">📈</span> 1.7 Ads KPI — Hiệu quả quảng cáo
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/ads-kpi</span>
    </div>
    <div class="hd-card-body">
      Theo dõi hiệu quả chạy Facebook/TikTok Ads theo từng nhân viên và từng shop.
      <ul>
        <li><b>Chọn ngày / khoảng ngày:</b> Xem KPI ngày đơn lẻ hoặc tổng hợp nhiều ngày.</li>
        <li><b>Lọc shop:</b> Chọn shop cụ thể hoặc xem toàn hệ thống.</li>
        <li><b>Các chỉ số chính:</b> Chi phí QC, đơn tạo, đơn thành công, doanh thu, ROAS, CPO (Chi phí/Đơn).</li>
        <li><b>Bảng theo nhân viên:</b> Xếp hạng NV theo ROAS / CPO / doanh thu.</li>
        <li><b>Đồng bộ thủ công:</b> Nút "Đồng bộ" để kéo dữ liệu mới nhất từ Facebook Ads API.</li>
        <li><b>Xuất báo cáo:</b> Chế độ "Chi tiết" hoặc "Tổng hợp" — xuất ra Excel.</li>
        <li><b>Group by:</b> Nhóm kết quả theo nhân viên hoặc theo shop.</li>
      </ul>
      <div class="tip">💡 ROAS &gt; 3 = hiệu quả tốt. CPO càng thấp càng tốt. Trang này là cơ sở để tính lương thưởng Adser.</div>
    </div>
  </div>

  <!-- 1.8 QC chậm -->
  <div class="hd-card" id="ads-delay">
    <div class="hd-card-head">
      <span class="hd-icon">⏰</span> 1.8 QC chậm &gt;2 ngày
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/ads-delay</span>
    </div>
    <div class="hd-card-body">
      Cảnh báo các shop/chiến dịch quảng cáo chưa được cập nhật dữ liệu trong hơn 2 ngày.
      <ul>
        <li><b>Danh sách shop bị chậm:</b> Shop nào chưa có data QC mới — cần kiểm tra lại.</li>
        <li><b>Nguyên nhân thường gặp:</b> Token Facebook hết hạn, tài khoản QC bị khóa, chưa đồng bộ.</li>
        <li><b>Chi tiết shop:</b> Bấm vào shop để xem ngày cuối có data và thông tin chi tiết.</li>
      </ul>
      <div class="tip">💡 Kiểm tra trang này mỗi sáng để đảm bảo data QC không bị gián đoạn.</div>
    </div>
  </div>

  <!-- 1.9 Shop lỗ -->
  <div class="hd-card" id="loss">
    <div class="hd-card-head">
      <span class="hd-icon">📉</span> 1.9 Shop lỗ theo POS
      <span class="hd-badge badge-leader">Leader+</span>
      <span class="hd-url">/loss-day-alert</span>
    </div>
    <div class="hd-card-body">
      Thống kê shop đang lỗ (doanh thu &lt; chi phí) theo từng tháng.
      <ul>
        <li><b>Chọn tháng:</b> Xem tháng hiện tại hoặc các tháng trước.</li>
        <li><b>Tổng lỗ tháng:</b> Tổng thiệt hại ước tính toàn hệ thống.</li>
        <li><b>Danh sách shop lỗ:</b> Xếp theo mức lỗ giảm dần — bấm vào để xem chi tiết lý do.</li>
        <li><b>Chi tiết:</b> Xem từng ngày shop lỗ, nguyên nhân (doanh thu thấp, chi phí QC cao, hoàn nhiều).</li>
      </ul>
    </div>
  </div>

  <!-- 1.10 Kho vật lý -->
  <div class="hd-card" id="kho-vat-ly">
    <div class="hd-card-head">
      <span class="hd-icon">🏭</span> 1.10 Kho vật lý
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/kho-vat-ly</span>
    </div>
    <div class="hd-card-body">
      Quản lý kho hàng vật lý — nhập hàng vào kho, xuất hàng cho đơn, trả hàng, kiểm kê.
      <br><br>
      <b>📥 Nhập kho</b> (<code>/kho-vat-ly/inbound</code>)
      <ul>
        <li>Tạo phiếu nhập hàng từ nhà cung cấp vào kho.</li>
        <li>Quét mã barcode hoặc nhập thủ công tên/mã sản phẩm.</li>
        <li>Xác nhận nhập lô — tự động cộng số lượng vào kho hệ thống.</li>
        <li>Đẩy lên POS: Sau khi xác nhận nhập, có thể đồng bộ số liệu lên Pancake.</li>
      </ul>
      <b>📤 Xuất kho</b> (<code>/kho-vat-ly/outbound</code>)
      <ul>
        <li>Danh sách yêu cầu xuất kho (từ đơn hàng đã duyệt).</li>
        <li>Quét mã đơn hàng hoặc scan barcode sản phẩm để xác nhận xuất.</li>
        <li>Xác nhận từng đơn hoặc xác nhận hàng loạt (Admin bulk-confirm).</li>
        <li>Đồng bộ với Pancake để cập nhật trạng thái "đã xuất kho".</li>
      </ul>
      <b>🔄 Trả hàng</b> (<code>/kho-vat-ly/returns</code>)
      <ul>
        <li>Ghi nhận hàng hoàn trả về kho (đơn hoàn từ khách).</li>
        <li>Scan mã đơn hoặc nhập mã để xác nhận hàng về.</li>
        <li>Tự động cộng lại số lượng tồn kho khi xác nhận hoàn.</li>
      </ul>
      <b>📊 Báo cáo kho</b> (<code>/kho-vat-ly/bao-cao</code>)
      <ul>
        <li>Tổng hợp nhập/xuất/tồn theo từng sản phẩm, từng shop, theo tháng.</li>
        <li>Xem chi tiết theo từng shop hoặc xem toàn hệ thống.</li>
      </ul>
      <b>🔍 Kiểm kho</b> (<code>/kho-vat-ly/kiem-kho</code>)
      <ul>
        <li>Tạo phiếu kiểm kê — đếm thực tế và so với hệ thống.</li>
        <li>Xác nhận chênh lệch để điều chỉnh tồn kho về đúng thực tế.</li>
      </ul>
      <b>💰 Tài chính kho</b> (<code>/kho-vat-ly/tai-chinh</code>)
      <ul>
        <li>Xem giá trị tồn kho theo giá nhập.</li>
        <li>Đồng bộ giá nhập từ Pancake, cập nhật giá thủ công.</li>
      </ul>
      <b>📦 Phân bổ kho</b> (<code>/kho-vat-ly/phan-bo</code>)
      <ul>
        <li>Phân bổ hàng từ kho trung tâm xuống từng shop.</li>
      </ul>
      <div class="tip">💡 Kho vật lý và POS Pancake đồng bộ 2 chiều — sau mỗi thao tác nên bấm "Đồng bộ" để số liệu luôn khớp.</div>
    </div>
  </div>

  <!-- 1.11 Chi phí QC -->
  <div class="hd-card" id="chi-phi-qc">
    <div class="hd-card-head">
      <span class="hd-icon">💰</span> 1.11 Chi phí QC (Quảng cáo)
      <span class="hd-badge badge-leader">Leader+</span>
      <span class="hd-url">/chi-phi-qc</span>
    </div>
    <div class="hd-card-body">
      Quản lý và theo dõi toàn bộ chi phí chạy quảng cáo Facebook / TikTok.
      <ul>
        <li><b>Nhập chi phí:</b> Ghi nhận chi phí QC thủ công hoặc tự động kéo từ API.</li>
        <li><b>Phân loại:</b> Theo shop, theo nhân viên, theo ngày/tháng.</li>
        <li><b>Báo cáo tổng hợp:</b> So sánh chi phí vs doanh thu để tính lãi/lỗ QC.</li>
        <li><b>Xuất Excel:</b> Tải dữ liệu chi phí ra file để gửi kế toán.</li>
      </ul>
    </div>
  </div>

  <!-- 1.12 Cài đặt -->
  <div class="hd-card" id="settings">
    <div class="hd-card-head">
      <span class="hd-icon">⚙️</span> 1.12 Cài đặt hệ thống
      <span class="hd-badge badge-admin">Admin</span>
      <span class="hd-url">/settings</span>
    </div>
    <div class="hd-card-body">
      Khu vực quản trị hệ thống — chỉ Admin được vào đầy đủ.
      <br><br>
      <b>Tab Tổng quan:</b> Thông tin hệ thống, trạng thái kết nối DB, token.
      <br><b>Tab Nhân sự:</b>
      <ul>
        <li>Thêm, sửa, xóa tài khoản nhân viên.</li>
        <li>Đặt lại mật khẩu cho nhân viên.</li>
        <li>Leader có thể nhận NV về team mình và giao shop cho NV.</li>
      </ul>
      <b>Tab Phân quyền:</b>
      <ul>
        <li>Đổi vai trò (Admin / Leader / Kế toán / Staff) cho từng người.</li>
        <li>Gán shop cho từng nhân viên — NV chỉ thấy shop được giao.</li>
      </ul>
      <b>Tab Shop & Web:</b> Thêm/sửa thông tin shop, cấu hình API Pancake.
      <br><b>Tab Facebook Ads:</b> Quản lý mapping tài khoản QC → shop.
      <br><b>Tab FB Token kho:</b> Lưu và kiểm tra token Facebook dùng để kéo data QC.
      <br><b>Tab Facebook Pages:</b> Quản lý danh sách Facebook Page kết nối.
      <br><b>Tab Telegram Bot:</b> Cấu hình bot Telegram để nhận thông báo tự động.
      <br><b>Tab Tài khoản:</b> Đổi mật khẩu tài khoản cá nhân.
    </div>
  </div>

</div><!-- /phần 1 -->


<!-- ═══════════════════════════════ PHẦN 2 ═══════════════════════════════ -->
<div class="hd-section" id="chamcong">
  <div class="hd-section-title"><span>⏱️</span> Phần 2 — Chấm công & Phân công việc</div>
  <p style="font-size:.9rem;color:#6b7280;margin-bottom:16px;">Truy cập qua menu <b>Chấm công</b> hoặc đường dẫn <code>/cham-cong/</code></p>

  <!-- 2.1 Dashboard cá nhân -->
  <div class="hd-card" id="cc-dashboard">
    <div class="hd-card-head">
      <span class="hd-icon">🏠</span> 2.1 Dashboard cá nhân
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/cham-cong/</span>
    </div>
    <div class="hd-card-body">
      Trang chủ module chấm công — hiển thị tổng quan cá nhân trong ngày.
      <ul>
        <li><b>Trạng thái hôm nay:</b> Đang làm việc / Đã về / Chưa chấm công — kèm giờ check-in và check-out.</li>
        <li><b>Nút Check-in / Check-out:</b> Bấm ngay từ dashboard để chấm công nhanh mà không cần vào trang riêng.</li>
        <li><b>Công việc hôm nay:</b> Tối đa 8 task đang làm (Todo + In-progress) của bản thân — xếp theo độ ưu tiên.</li>
        <li><b>Thông báo:</b> Thông báo nội bộ gần nhất từ Admin/Leader.</li>
        <li><b>Team hôm nay (Leader/Admin):</b> Bảng tổng hợp toàn bộ nhân viên — ai đã vào, ai chưa, ai đã về.</li>
      </ul>
    </div>
  </div>

  <!-- 2.2 Chấm công -->
  <div class="hd-card" id="cc-checkin">
    <div class="hd-card-head">
      <span class="hd-icon">✅</span> 2.2 Chấm công (Check-in / Check-out)
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/cham-cong/cham-cong</span>
    </div>
    <div class="hd-card-body">
      Trang chấm công chính thức của từng nhân viên.
      <ul>
        <li><b>Check-in:</b> Bấm nút "Bắt đầu làm việc" — hệ thống ghi nhận giờ vào theo giờ VN (UTC+7).</li>
        <li><b>Check-out:</b> Bấm nút "Kết thúc ca" khi xong việc — ghi nhận giờ ra, tự tính số giờ làm.</li>
        <li><b>Trạng thái hiện tại:</b> Hiển thị rõ đang ở trạng thái nào: Chưa vào / Đang làm / Đã về.</li>
        <li><b>Tóm tắt tháng này:</b> Số ngày đi làm, số ngày vắng, tổng giờ làm tháng hiện tại.</li>
        <li><b>Ghi chú:</b> Có thể thêm ghi chú khi check-in/out (tình trạng đặc biệt, lý do muộn...).</li>
      </ul>
      <div class="tip">💡 Chỉ check-in được 1 lần/ngày. Nếu cần sửa giờ, liên hệ Admin để chấm hộ.</div>
    </div>
  </div>

  <!-- 2.3 Lịch sử -->
  <div class="hd-card" id="cc-history">
    <div class="hd-card-head">
      <span class="hd-icon">📅</span> 2.3 Lịch sử chấm công
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/cham-cong/lich-su</span>
    </div>
    <div class="hd-card-body">
      Xem lịch sử chấm công cá nhân theo tháng.
      <ul>
        <li><b>Chọn tháng:</b> Dropdown chọn tháng/năm muốn xem.</li>
        <li><b>Bảng lịch sử:</b> Từng ngày trong tháng — giờ vào, giờ ra, số giờ làm, trạng thái (đi làm/vắng/nghỉ phép).</li>
        <li><b>Tổng kết tháng:</b> Tổng ngày đi làm, tổng giờ làm, số ngày vắng không phép.</li>
        <li><b>Màu sắc:</b> Xanh = đi đủ | Vàng = vào muộn/về sớm | Đỏ = vắng | Xám = nghỉ phép.</li>
      </ul>
    </div>
  </div>

  <!-- 2.4 Công việc -->
  <div class="hd-card" id="cc-tasks">
    <div class="hd-card-head">
      <span class="hd-icon">📝</span> 2.4 Công việc (Task)
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/cham-cong/cong-viec</span>
    </div>
    <div class="hd-card-body">
      Hệ thống phân công và theo dõi công việc nội bộ.
      <br><br>
      <b>Nhân viên thường xem:</b>
      <ul>
        <li>Danh sách task được giao: Cần làm / Đang làm / Xong.</li>
        <li>Bấm vào task để xem mô tả chi tiết, deadline, độ ưu tiên.</li>
        <li>Cập nhật trạng thái: Kéo task từ "Cần làm" → "Đang làm" → "Xong".</li>
      </ul>
      <b>Leader / Admin tạo và giao việc:</b>
      <ul>
        <li><b>Tạo task mới:</b> Bấm nút "+ Tạo task" — điền tiêu đề, mô tả, deadline, độ ưu tiên, loại việc.</li>
        <li><b>Giao cho NV:</b> Chọn nhân viên nhận việc (toàn bộ NV trong hệ thống: sale, kho, mua hàng, adser...).</li>
        <li><b>Độ ưu tiên:</b> Khẩn cấp 🔴 / Cao 🟠 / Bình thường 🟡 / Thấp 🟢.</li>
        <li><b>Loại việc:</b> Phân loại theo bộ phận (Adser, Sale, Đóng hàng, Mua hàng...).</li>
        <li><b>Lọc task:</b> Lọc theo trạng thái hoặc theo loại việc.</li>
        <li><b>Xóa task:</b> Admin có thể xóa task đã hoàn thành hoặc không cần nữa.</li>
      </ul>
      <div class="tip">💡 Kanban board 3 cột: Cần làm — Đang làm — Xong. Task "Xong" giới hạn hiển thị 20 bản gần nhất.</div>
    </div>
  </div>

  <!-- 2.5 KPI -->
  <div class="hd-card" id="cc-kpi">
    <div class="hd-card-head">
      <span class="hd-icon">🎯</span> 2.5 KPI hàng ngày
      <span class="hd-badge badge-all">Tất cả</span>
      <span class="hd-url">/cham-cong/kpi</span>
    </div>
    <div class="hd-card-body">
      Nhập và theo dõi chỉ số KPI cá nhân theo từng ngày.
      <ul>
        <li><b>Nhập KPI hôm nay:</b> Điền các chỉ số theo vai trò (doanh thu, số đơn, ROAS, đơn chốt...) và bấm Lưu.</li>
        <li><b>KPI hôm nay:</b> Hiển thị nhanh các chỉ số đã nhập trong ngày.</li>
        <li><b>Lịch sử tháng:</b> Bảng KPI theo từng ngày trong tháng — chọn tháng/năm muốn xem.</li>
        <li><b>Leader/Admin — xem KPI của NV khác:</b> Dropdown chọn nhân viên để xem KPI của họ.</li>
        <li><b>Ghi chú:</b> Thêm ghi chú vào từng ngày (lý do đặc biệt, sự kiện ảnh hưởng KPI...).</li>
      </ul>
      <div class="tip">💡 Nên nhập KPI cuối mỗi ngày làm việc để có dữ liệu đầy đủ cho báo cáo tháng.</div>
    </div>
  </div>

  <!-- 2.6 Giám sát -->
  <div class="hd-card" id="cc-giam-sat">
    <div class="hd-card-head">
      <span class="hd-icon">👁️</span> 2.6 Giám sát realtime
      <span class="hd-badge badge-leader">Leader / Admin</span>
      <span class="hd-url">/cham-cong/giam-sat</span>
    </div>
    <div class="hd-card-body">
      Trang giám sát trực tiếp toàn bộ nhân viên — ai đang làm, ai vắng, ai đã về.
      <ul>
        <li><b>4 thẻ tổng quan:</b> Tổng NV | Đang làm việc | Đã về | Chưa đến.</li>
        <li><b>Danh sách NV:</b> Từng người với trạng thái màu sắc, giờ vào, giờ ra, thời gian làm.</li>
        <li><b>Tự động làm mới:</b> Trang tự refresh mỗi 60 giây — không cần F5 thủ công.</li>
        <li><b>Leader xem tất cả NV:</b> Sale, kho, mua hàng, adser — toàn bộ không phân biệt bộ phận.</li>
        <li><b>Sắp xếp:</b> Đang làm → Đã về → Chưa đến.</li>
      </ul>
      <div class="tip">💡 Dùng trang này đầu giờ sáng để biết ai đã vào làm, ai cần nhắc nhở.</div>
    </div>
  </div>

  <!-- 2.7 Quản lý Admin -->
  <div class="hd-card" id="cc-admin">
    <div class="hd-card-head">
      <span class="hd-icon">🛠️</span> 2.7 Quản lý (Admin)
      <span class="hd-badge badge-admin">Admin / Leader</span>
      <span class="hd-url">/cham-cong/admin</span>
    </div>
    <div class="hd-card-body">
      Khu vực quản trị module chấm công — 5 tab chức năng.
      <br><br>
      <b>Tab Nhân viên:</b>
      <ul>
        <li>Xem danh sách toàn bộ NV trong hệ thống (từ POS + NV standalone).</li>
        <li><b>Thêm NV mới:</b> Tạo nhân viên standalone (chỉ dùng chấm công, không có tài khoản POS).</li>
        <li><b>Cập nhật thông tin:</b> Sửa tên, bộ phận, vai trò CC, SĐT, chức vụ.</li>
        <li><b>Seed từ POS:</b> Đồng bộ danh sách NV từ file users.json vào bảng chấm công.</li>
      </ul>
      <b>Tab Chấm công hộ:</b>
      <ul>
        <li>Admin chấm hộ check-in / check-out cho NV (khi NV quên hoặc cần điều chỉnh).</li>
        <li>Chọn NV → chọn ngày → nhập giờ vào / giờ ra → Lưu.</li>
      </ul>
      <b>Tab Báo cáo tháng:</b>
      <ul>
        <li>Tổng hợp chấm công toàn bộ NV trong tháng được chọn.</li>
        <li>Bảng mỗi NV: số ngày đi làm, số ngày vắng, tổng giờ, trung bình giờ/ngày.</li>
        <li>Xuất Excel báo cáo tháng để tính lương, nộp kế toán.</li>
      </ul>
      <b>Tab Báo cáo CV (Công việc):</b>
      <ul>
        <li>Thống kê task của từng NV: Tổng task, hoàn thành, đang làm, cần làm, hủy.</li>
        <li>Thanh tiến độ màu sắc: Xanh ≥80% | Tím ≥50% | Vàng ≥20% | Đỏ &lt;20%.</li>
        <li>Tìm kiếm NV nhanh bằng ô search.</li>
        <li>Xem ngày hoàn thành task cuối cùng của từng người.</li>
      </ul>
      <b>Tab Thông báo:</b>
      <ul>
        <li>Tạo thông báo nội bộ gửi đến tất cả NV hoặc theo vai trò (Adser, Sale, Kho...).</li>
        <li>Thông báo hiển thị trên Dashboard cá nhân của NV được nhắm tới.</li>
        <li>Xóa thông báo cũ không còn dùng.</li>
      </ul>
    </div>
  </div>

</div><!-- /phần 2 -->


<!-- ═══════════════════════════════ PHẦN 3 ═══════════════════════════════ -->
<div class="hd-section" id="roles">
  <div class="hd-section-title"><span>🔐</span> Phần 3 — Phân quyền hệ thống</div>

  <div class="hd-card">
    <div class="hd-card-head">
      <span class="hd-icon">👥</span> Các vai trò và quyền hạn
    </div>
    <div class="hd-card-body">
      <table class="hd-role-table">
        <thead>
          <tr>
            <th>Vai trò</th>
            <th>POS — Dashboard, shop, kho, báo cáo</th>
            <th>Chấm công & Phân công</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><b>Admin</b></td>
            <td>Toàn quyền tất cả shop, cài đặt, quản lý NV, xem mọi số liệu</td>
            <td>Toàn quyền: chấm hộ, quản lý NV, báo cáo, thông báo, xem mọi người</td>
          </tr>
          <tr>
            <td><b>Kế toán</b></td>
            <td>Xem tất cả shop, báo cáo, xuất Excel — không vào cài đặt</td>
            <td>Như Admin trong chấm công: xem toàn bộ NV, báo cáo tháng</td>
          </tr>
          <tr>
            <td><b>Leader</b></td>
            <td>Xem shop được giao, xem báo cáo team, quản lý NV trong team</td>
            <td>Giám sát realtime tất cả NV, giao việc cho mọi NV (sale, kho, mua hàng...), xem KPI NV khác</td>
          </tr>
          <tr>
            <td><b>Adser</b></td>
            <td>Xem shop được giao, Ads KPI của mình</td>
            <td>Chấm công cá nhân, xem task của mình, nhập KPI</td>
          </tr>
          <tr>
            <td><b>Sale (chốt đơn)</b></td>
            <td>Xem shop được giao</td>
            <td>Chấm công cá nhân, xem task được giao, nhập KPI</td>
          </tr>
          <tr>
            <td><b>Đóng hàng / Kho</b></td>
            <td>Kho vật lý (xuất kho, xác nhận đơn)</td>
            <td>Chấm công cá nhân, xem task được giao</td>
          </tr>
          <tr>
            <td><b>Staff</b></td>
            <td>Chỉ xem shop được giao</td>
            <td>Chấm công cá nhân, xem task của mình</td>
          </tr>
        </tbody>
      </table>
      <div class="tip" style="margin-top:14px;">
        💡 <b>Ghi nhớ nhanh:</b> Admin & Kế toán thấy TẤT CẢ. Leader thấy TẤT CẢ trong chấm công nhưng chỉ thấy shop team mình trong POS. Các NV khác chỉ thấy phần việc của mình.
      </div>
    </div>
  </div>

  <div class="hd-card">
    <div class="hd-card-head">
      <span class="hd-icon">❓</span> Các câu hỏi thường gặp (FAQ)
    </div>
    <div class="hd-card-body">
      <b>Q: Quên check-in / check-out, làm sao sửa?</b><br>
      A: Nhắn Admin để vào trang Quản lý → Tab "Chấm công hộ" → Chọn bạn → Sửa giờ cho ngày đó.
      <br><br>
      <b>Q: Số đơn "Mới" và "Đơn tạo" khác nhau là sao?</b><br>
      A: "Mới" = cache live từ Pancake (bao gồm đơn đổi hàng). "Đơn tạo" = API analytics chỉ tính đơn gốc. Chênh lệch là bình thường.
      <br><br>
      <b>Q: Tôi không thấy shop X trên Dashboard?</b><br>
      A: Shop đó chưa được Admin giao cho bạn. Nhờ Admin vào Cài đặt → Phân quyền → Gán shop cho tài khoản bạn.
      <br><br>
      <b>Q: Tôi là Leader, có thể giao việc cho nhân viên kho/sale không?</b><br>
      A: Có — Leader thấy và giao được việc cho TẤT CẢ nhân viên (sale, kho, mua hàng, adser...).
      <br><br>
      <b>Q: Dữ liệu QC bị chậm, cần làm gì?</b><br>
      A: Vào trang "QC chậm &gt;2 ngày" (/ads-delay) để xem shop nào bị ảnh hưởng. Kiểm tra token Facebook trong Cài đặt → FB Token kho.
      <br><br>
      <b>Q: Muốn tải báo cáo chấm công tháng ra Excel?</b><br>
      A: Vào Chấm công → Quản lý → Tab "Báo cáo tháng" → Chọn tháng → Bấm "Xuất Excel".
    </div>
  </div>

</div><!-- /phần 3 -->

</div><!-- /hd-wrap -->
"""
    return render_page("Hướng dẫn sử dụng", body)

