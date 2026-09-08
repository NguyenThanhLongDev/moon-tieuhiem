# -*- coding: utf-8 -*-
"""Nạp đơn từ GOOGLE SHEET của sale rồi đối soát với POS — sếp Phong 12/08.

Sale không làm việc trên LadiPage mà trên 1 file Google Sheet: đơn khách điền
form chảy về sheet, sale nhìn sheet rồi lên đơn POS. Vậy nguồn SỰ THẬT để kiểm
"đơn khách đặt đã lên POS chưa" chính là sheet này, không phải webhook (webhook
chỉ bắt được các form đã cấu hình — hiện mới 3/40 trang).

Đọc sheet qua link xuất CSV công khai, KHÔNG cần API key Google:
  https://docs.google.com/spreadsheets/d/<ID>/gviz/tq?tqx=out:csv&gid=<GID>
Điều kiện: sheet để chế độ "Bất kỳ ai có đường liên kết → Người xem".

Cột được tự nhận diện theo tên tiêu đề (SĐT, họ tên, địa chỉ, thời gian…) nên
không bắt sale phải sửa lại sheet.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from datetime import datetime

import requests

logger = logging.getLogger("ladipage.sheet")


def _nod(s) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(c) != "Mn").lower().strip()


def csv_url(url: str) -> str:
    """Link sheet bất kỳ → link xuất CSV. Giữ đúng tab (gid) nếu có."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    if not m:
        raise ValueError("Không phải link Google Sheet")
    sid = m.group(1)
    g = re.search(r"[#&?]gid=(\d+)", url or "")
    gid = g.group(1) if g else "0"
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"


def fetch_rows(url: str, timeout: int = 45) -> list:
    """Tải sheet → list[dict] theo tiêu đề cột."""
    r = requests.get(csv_url(url), timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Không tải được sheet (HTTP {r.status_code}). "
                           "Kiểm tra sheet đã chia sẻ 'Bất kỳ ai có link → Người xem' chưa.")
    text = r.content.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise RuntimeError("Sheet chưa công khai — Google trả về trang đăng nhập. "
                           "Vào Chia sẻ → 'Bất kỳ ai có đường liên kết' → Người xem.")
    return list(csv.DictReader(io.StringIO(text)))


_PHONE_KEYS = ("so dien thoai", "sdt", "phone", "dien thoai", "sđt", "tel", "mobile", "so dt")
_NAME_KEYS = ("ho ten", "ten", "name", "fullname", "khach", "khach hang", "ho va ten")
_ADDR_KEYS = ("dia chi", "address", "diachi", "noi nhan", "dia chi nhan hang")
_TIME_KEYS = ("thoi gian", "time", "ngay", "created", "dau thoi gian", "timestamp", "luc")
_MONEY_KEYS = ("cod", "tien", "gia", "thanh tien", "tong tien", "so tien", "amount")


def _pick(row: dict, keys) -> str:
    for k, v in row.items():
        if _nod(k) in keys:
            return str(v or "").strip()
    # khớp lỏng: tiêu đề CHỨA từ khoá
    for k, v in row.items():
        nk = _nod(k)
        if any(x in nk for x in keys):
            return str(v or "").strip()
    return ""


def _to_money(s) -> int:
    d = re.sub(r"[^\d]", "", str(s or ""))
    return int(d) if d else 0


def _to_time(s):
    s = str(s or "").strip()
    for f in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
              "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def import_sheet(url: str, pos_shop_id: str = "1720128057") -> dict:
    """Nạp sheet vào ladipage_inbound_orders (nguon='sheet'), bỏ qua dòng đã có.

    Trả {'doc': n, 'them': n, 'trung': n, 'bo': n}.
    """
    from db import get_conn
    from modules.ladipage.matcher import norm_phone
    rows = fetch_rows(url)
    out = {"doc": len(rows), "them": 0, "trung": 0, "bo": 0}
    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url).group(1)

    with get_conn() as conn:
        for i, row in enumerate(rows, start=2):    # dòng 1 là tiêu đề
            phone = norm_phone(_pick(row, _PHONE_KEYS))
            if not phone or len(phone) < 9:
                out["bo"] += 1
                continue
            ho_ten = _pick(row, _NAME_KEYS)
            dia_chi = _pick(row, _ADDR_KEYS)
            tien = _to_money(_pick(row, _MONEY_KEYS))
            luc = _to_time(_pick(row, _TIME_KEYS))
            # Ghi chú = mọi cột còn lại (size, màu, combo, nguồn…) cho sale dễ tra
            bo_qua_val = {ho_ten, dia_chi}
            extra = [f"{k}: {v}" for k, v in row.items()
                     if str(v or "").strip() and str(v).strip() not in bo_qua_val
                     and _nod(k) not in _PHONE_KEYS + _NAME_KEYS + _ADDR_KEYS + _TIME_KEYS]
            note = " | ".join(extra)[:1500]
            key = f"{sid}#{i}#{phone}"

            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM ladipage_inbound_orders WHERE sheet_row_key=%s", (key,))
                if cur.fetchone():
                    out["trung"] += 1
                    continue
                cur.execute("""
                    INSERT INTO ladipage_inbound_orders
                        (received_at, shop_key, pos_shop_id, ho_ten, so_dien_thoai,
                         dia_chi, tien, note, source_name, phone_norm, nguon,
                         sheet_row_key, match_status)
                    VALUES (COALESCE(%s, NOW()), NULL, %s, %s, %s, %s, %s, %s,
                            'Google Sheet', %s, 'sheet', %s, 'cho_kiem_tra')
                """, (luc, str(pos_shop_id), ho_ten or None, _pick(row, _PHONE_KEYS),
                      dia_chi or None, tien or None, note or None, phone, key))
                out["them"] += 1
            conn.commit()
    logger.info("import_sheet %s: %s", sid, out)
    return out
