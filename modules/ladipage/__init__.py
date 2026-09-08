"""LadiPage → Pancake POS: nhận đơn từ form ladipage, LƯU DB ngay (không mất đơn),
rồi đẩy sang POS. Nếu POS lỗi → đơn nằm ở trang Kiểm tra để đẩy lại / nhập tay.

Luồng:
  Form ladipage --webhook--> ghi ladipage_inbound_orders (pending)
                        └--> thread đẩy POS: pushed(pos_order_id) | failed(pos_error)

Route:
  POST /api/webhooks/ladipage/<pos_shop_id>   (công khai, không login — cho ladipage gọi)
  GET  /ladipage/                              (trang kiểm tra, cần login)
  POST /ladipage/<id>/repush                   (đẩy lại 1 đơn lỗi)
  POST /ladipage/<id>/done                     (đánh dấu đã xử lý tay)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta

import requests
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from db import get_conn

logger = logging.getLogger("ladipage")

ladipage_bp = Blueprint(
    "ladipage", __name__, url_prefix="/ladipage", template_folder="templates",
)

POS_BASE = "https://pos.pages.fm/api/v1"
_VIEW_ROLES = {"admin", "superadmin", "manager", "it", "accountant", "ketoan",
               "leader", "sale"}

# Đơn của page ĐANG BÁN (WIN) mà chưa thấy trên POS — nhóm đáng ngờ nhất
_WIN_THIEU_SQL = """EXISTS (SELECT 1 FROM pos_page_daily_metrics m
                             WHERE m.page_id = ladipage_inbound_orders.ads_page_id
                               AND m.revenue > 0
                               AND m.metric_date <= (received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date)
                    AND COALESCE(match_status,'cho_kiem_tra') = 'chua_co_pos'"""
_WH_CACHE: dict = {}

# Rút TÊN MIỀN từ link landing (bỏ https:// và www.) — dùng cho bộ lọc Nguồn đơn
_DOM_SQL = ("lower(replace(split_part(regexp_replace(COALESCE(source_url,''),"
            "'^https?://',''),'/',1),'www.',''))")

# Rút ĐƯỜNG DẪN đầy đủ: tên miền + path, BỎ tham số ?utm...
# Sếp Phong 20/08: "cùng 1 tên miền sẽ có nhiều sản phẩm" → phải lọc tới từng
# link chi tiết (vd thoitrangluxury.online/somihan) mới đếm được đơn từng mẫu.
_LP_SQL = ("rtrim(lower(replace(split_part(split_part(regexp_replace("
           "COALESCE(source_url,''),'^https?://',''),'?',1),'#',1),'www.','')),'/')")


# ── helpers ────────────────────────────────────────────────────────────────
_SP_DICT = {
    "ao":"áo","quan":"quần","vay":"váy","dam":"đầm","chan":"chân","giay":"giày",
    "so":"sơ","mi":"mi","somi":"sơ mi","thun":"thun","polo":"polo","cardigan":"cardigan",
    "sweater":"sweater","hoodie":"hoodie","khoac":"khoác","len":"len","lot":"lót",
    "nam":"nam","nu":"nữ","tay":"tay","hoa":"hoa","xinh":"xinh","tron":"trơn",
    "cao":"cao","cap":"cấp","nhap":"nhập","khau":"khẩu","dai":"dài","ngan":"ngắn",
    "den":"đen","trang":"trắng","hong":"hồng","xanh":"xanh","do":"đỏ","tim":"tím",
    "kem":"kem","be":"bé","mau":"màu","moi":"mới","hang":"hàng","hieu":"hiệu",
    "gia":"giá","mua":"mua","giam":"giảm","con":"còn","ship":"ship","test":"test",
    "hot":"hot","trend":"trend","nuhoa":"nữ hoa","tt":"trơn","mp":"mp","han":"hàn",
    "caocap":"cao cấp","nhapkhau":"nhập khẩu","daitay":"dài tay","ngantay":"ngắn tay",
    "sominam":"sơ mi nam","somihu":"sơ mi nữ","somihan":"sơ mi hàn","aothun":"áo thun","aopolo":"áo polo",
    "quanjean":"quần jean","chanvay":"chân váy","aokhoac":"áo khoác","aolen":"áo len",
}

def _sp_ten(lp: str) -> str:
    """Chuyển slug landing (vd aosominuhoaxinhcaocap) → tên sản phẩm có dấu (vd Áo sơ mi nữ hoa xinh cao cấp)."""
    if not lp:
        return ""
    path = lp.split("/", 1)[1] if "/" in lp else lp
    s = (path or "").replace("_", " ").replace("-", " ").lower().strip()
    # Thay các cụm 2 từ trước (để không tách sai giữa chừng)
    for k in sorted([k for k in _SP_DICT if len(k) > 3], key=len, reverse=True):
        s = s.replace(k, " " + k + " ")
    # Tách từng từ ASCII -> có dấu
    words = [w for w in s.split() if w]
    out = [_SP_DICT.get(w, w) for w in words]
    if not out:
        return path
    out[0] = out[0][0].upper() + out[0][1:] if out[0] else out[0]
    return " ".join(out)



def _nod(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").lower().strip()


def _can_view() -> bool:
    if not session.get("logged_in"):
        return False
    return str(session.get("role", "")).strip().lower() in _VIEW_ROLES


def _shop_by_pos_id(cur, pos_shop_id):
    # Ưu tiên bảng ladipage_shops (shop chuyên nhận đơn ladipage)
    cur.execute(
        "SELECT pos_shop_id, shop_name, api_key, warehouse_id "
        "FROM ladipage_shops WHERE pos_shop_id = %s AND active", (str(pos_shop_id),))
    r = cur.fetchone()
    if r:
        return {"id": None, "shop_key": str(r[0]), "shop_name": r[1],
                "pos_shop_id": str(r[0]), "pos_api_key": r[2],
                "warehouse_id": r[3]}
    # Fallback: shop trong wh_shops
    cur.execute(
        "SELECT id, shop_key, shop_name, pos_shop_id, pos_api_key "
        "FROM wh_shops WHERE pos_shop_id = %s LIMIT 1", (str(pos_shop_id),))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "shop_key": r[1], "shop_name": r[2],
            "pos_shop_id": str(r[3]), "pos_api_key": r[4], "warehouse_id": None}


def _default_warehouse(pos_shop_id: str, api_key: str):
    if pos_shop_id in _WH_CACHE:
        return _WH_CACHE[pos_shop_id]
    wid = None
    try:
        r = requests.get(f"{POS_BASE}/shops/{pos_shop_id}/warehouses",
                         params={"api_key": api_key}, timeout=20)
        data = r.json()
        arr = data.get("data") if isinstance(data, dict) else data
        for w in (arr or []):
            if w.get("allow_create_order"):
                wid = w.get("id")
                break
        if not wid and arr:
            wid = (arr[0] or {}).get("id")
    except Exception as e:
        logger.warning("warehouse fetch fail %s: %s", pos_shop_id, e)
    _WH_CACHE[pos_shop_id] = wid
    return wid


_KNOWN_COLORS = {"trang", "xanh", "hong", "tim", "do", "den", "nau", "xam",
                 "vang", "be", "kem", "cam", "xanh la", "xanh than", "xanh nhat"}


def _parse_price(text: str) -> int:
    """Tổng các số kèm 'k' trong chuỗi → đồng. VD '299k + 30k' → 329000."""
    total = 0
    for m in re.finditer(r"(\d[\d\.]*)\s*k", _nod(text)):
        try:
            total += int(float(m.group(1).replace(".", ""))) * 1000
        except Exception:
            pass
    return total


def _parse_qty(text: str) -> int:
    m = re.search(r"mua\s*(\d+)", _nod(text))
    if m:
        return max(1, int(m.group(1)))
    return 1


def _flatten(payload) -> dict:
    """Gom mọi cặp key->value (kể cả lồng 1 cấp) thành dict phẳng chuỗi."""
    flat: dict = {}

    def add(k, v):
        if v is None:
            return
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v if x not in (None, ""))
        elif isinstance(v, dict):
            return
        v = str(v).strip()
        if v:
            flat.setdefault(str(k), v)

    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    add(k2, v2)
            else:
                add(k, v)
    return flat


def _extract(payload, form_dict, referer: str) -> dict:
    """Chuẩn hoá payload ladipage → các trường của đơn."""
    flat = _flatten(payload)
    for k, v in (form_dict or {}).items():
        if v not in (None, ""):
            flat.setdefault(str(k), str(v).strip())

    def pick(*names):
        want = {_nod(n) for n in names}
        for k, v in flat.items():
            if _nod(k) in want:
                return v
        return ""

    ho_ten = pick("ho_ten", "name", "fullname", "full_name", "ho ten", "ten",
                  "hoten", "họ tên", "khach")
    sdt = pick("so_dien_thoai", "sdt", "phone", "phone_number", "dien_thoai",
               "số điện thoại", "so dien thoai", "tel", "mobile")
    dia_chi = pick("dia_chi", "address", "diachi", "địa chỉ", "dia chi", "addr")
    sku = pick("sku", "ma_sp", "masp", "ma_san_pham", "product_sku", "ma")
    source_url = (pick("url", "link", "landing_page_url", "landing_url", "landing_page",
                       "link_landing", "page_url", "page_link", "url_page", "current_url",
                       "href", "variant_url", "url_landing", "origin", "ref", "referer",
                       "referrer", "landing") or referer or "").strip()
    source_name = pick("page_name", "landing_name", "ten_landing", "landing_title",
                       "form_title", "page_title", "title", "ten_trang")

    # Các trường khách đã dùng → không đưa vào "lựa chọn"
    used_vals = {v for v in (ho_ten, sdt, dia_chi, sku, source_url, source_name) if v}
    cust_keys = set()
    for k, v in flat.items():
        if v in used_vals:
            cust_keys.add(k)

    size = mau = combo = ""
    selections = []
    for k, v in flat.items():
        if k in cust_keys:
            continue
        nk, nv = _nod(k), _nod(v)
        # bỏ các field kỹ thuật của ladipage
        if nk in ("url", "ref", "origin", "utm_source", "utm_medium",
                  "utm_campaign", "utm_content", "utm_term", "ip", "time",
                  "created_at", "token", "form_id", "tracking_id",
                  "user_agent", "useragent", "data_fbp", "data_fbc", "fbp", "fbc",
                  "fbclid", "event_id", "message_id", "msg_id", "session_id",
                  "uuid", "device", "browser", "platform", "url_page", "api_key",
                  "ladi_source", "form_name", "form"):
            continue
        # bỏ GIÁ TRỊ rác: user-agent, token fb, uuid, chuỗi số dài (timestamp), quá dài
        if (len(v) > 90 or v.startswith("Mozilla") or "AppleWebKit" in v
                or re.match(r"^fb\.\d\.", v)
                or re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", v, re.I)
                or re.match(r"^\d{10,}$", v)
                or re.match(r"^(FORM|SECTION|BUTTON)\d*$", v, re.I)
                or re.match(r"^ladi[.\d]+", v, re.I)):
            continue
        selections.append(v)
        if not size and ("size" in nk or "size" in nv or
                         re.match(r"^(s|m|l|xl|xxl|\dxl)\b", nv)):
            size = v
        elif not mau and ("mau" in nk or "màu" in v.lower() or
                          any(c in nv for c in _KNOWN_COLORS)):
            mau = v
        elif not combo and ("mua" in nv and ("ao" in nv or "gia" in nv or "k" in nv)):
            combo = v

    # dedup giữ thứ tự
    seen = set()
    selections = [x for x in selections if not (x in seen or seen.add(x))]

    price_src = combo or " ".join(selections)
    tien = _parse_price(price_src)
    so_luong = _parse_qty(combo or price_src)

    # Link nguồn: GIỮ https:// để NV bấm vào xem trang bán gì; chỉ cắt đuôi
    # tracking (?fbclid=…&utm_…) cho gọn.
    _src_short = source_url.split("?")[0].split("#")[0].rstrip("/")
    if _src_short and not _src_short.startswith("http"):
        _src_short = "https://" + _src_short
    # Chuẩn hoá: nếu source_url chỉ là tên miền (thiếu protocol), thêm https://
    if source_url and not re.match(r"^[a-z][a-z0-9+.-]*://", source_url.strip(), re.I):
        source_url = "https://" + source_url.strip()
    # LadiPage publish trang ở www.<domain>; link ads lại thiếu "www." → mở ra
    # ERR_NAME_NOT_RESOLVED. Thêm www cho domain 2 nhãn (vd abc.online).
    try:
        _m2 = re.match(r"^(https?://)([^/]+)(/.*)?$", _src_short)
        if _m2:
            _scheme, _host, _path = _m2.group(1), _m2.group(2), _m2.group(3) or ""
            if _host.count(".") == 1 and not _host.startswith("www."):
                _src_short = f"{_scheme}www.{_host}{_path}"
    except Exception:
        pass
    note_parts = [x for x in [ho_ten, sdt, dia_chi] + selections if x]
    if _src_short:
        note_parts.append("Nguồn: " + _src_short)
    note = " | ".join(note_parts)

    return {
        "ho_ten": ho_ten, "so_dien_thoai": sdt, "dia_chi": dia_chi,
        "size": size, "mau": mau, "combo": combo, "so_luong": so_luong,
        "tien": tien, "sku": sku, "selections": " · ".join(selections),
        "source_url": source_url, "source_name": source_name, "note": note,
    }


# ── đẩy POS ────────────────────────────────────────────────────────────────
def _resolve_item(pos_shop_id, api_key, sku, size, mau, qty):
    """Nếu có SKU → tìm product + biến thể khớp size/màu. Trả [item] hoặc []."""
    if not sku:
        return []
    try:
        for pg in range(1, 30):
            r = requests.get(f"{POS_BASE}/shops/{pos_shop_id}/products",
                             params={"api_key": api_key, "page_size": 50,
                                     "page_number": pg}, timeout=25)
            d = r.json()
            for p in (d.get("data") or []):
                if _nod(p.get("custom_id")) != _nod(sku):
                    continue
                vs = p.get("variations") or []
                want = {t for t in [_nod(size), _nod(mau)] if t}
                # rút gọn size (M/L/XL/2XL/3XL) + màu về token
                size_tok = ""
                m = re.search(r"\b(\dxl|xxl|xl|[sml])\b", _nod(size))
                if m:
                    size_tok = m.group(1)
                best = None
                for v in vs:
                    vn = _nod(v.get("display_id") or v.get("name") or "")
                    ok_size = (size_tok and size_tok in re.split(r"[-\s]", vn))
                    ok_mau = any(c and c in vn for c in [_nod(mau)] if c) or \
                             any(c in vn for c in _KNOWN_COLORS if c in _nod(mau))
                    if size_tok and _nod(mau):
                        if ok_size and ok_mau:
                            best = v
                            break
                    elif ok_size or ok_mau:
                        best = best or v
                v = best or (vs[0] if vs else None)
                if not v:
                    return []
                price = v.get("retail_price") or p.get("retail_price") or 0
                return [{
                    "product_id": p.get("id"),
                    "variation_id": v.get("id"),
                    "quantity": max(1, int(qty or 1)),
                    "retail_price": price,
                }]
            if pg >= (d.get("total_pages") or 1):
                break
    except Exception as e:
        logger.warning("resolve_item fail sku=%s: %s", sku, e)
    return []


def _push_to_pos(shop: dict, rec: dict):
    """Tạo đơn POS. Trả (ok, pos_order_id, error)."""
    pos_shop_id, api_key = shop["pos_shop_id"], shop["pos_api_key"]
    wid = shop.get("warehouse_id") or _default_warehouse(pos_shop_id, api_key)
    items = _resolve_item(pos_shop_id, api_key, rec.get("sku"),
                          rec.get("size"), rec.get("mau"), rec.get("so_luong"))
    body = {
        "bill_full_name": rec.get("ho_ten") or "Khách Ladipage",
        "bill_phone_number": rec.get("so_dien_thoai") or "",
        "shipping_address": {
            "full_name": rec.get("ho_ten") or "",
            "phone_number": rec.get("so_dien_thoai") or "",
            "address": rec.get("dia_chi") or "",
            "full_address": rec.get("dia_chi") or "",
        },
        "note": rec.get("note") or "",
        "status": 0,
        "warehouse_id": wid,
        "order_sources_name": "Ladipage",
        "cod": rec.get("tien") or 0,
        "money_to_collect": rec.get("tien") or 0,
        "items": items,
    }
    try:
        r = requests.post(f"{POS_BASE}/shops/{pos_shop_id}/orders",
                          params={"api_key": api_key}, json=body, timeout=30)
    except Exception as e:
        return False, "", f"Kết nối POS lỗi: {e}"
    if 200 <= r.status_code < 300:
        try:
            j = r.json()
        except Exception:
            j = {}
        node = j.get("data") if isinstance(j, dict) else None
        oid = ""
        if isinstance(node, dict):
            oid = node.get("id") or node.get("system_id") or ""
        oid = oid or (j.get("id") if isinstance(j, dict) else "") or ""
        if oid:
            return True, str(oid), ""
        # 2xx nhưng không rõ id — coi như lỗi mềm để kiểm tra tay
        return False, "", f"POS trả 2xx nhưng thiếu order id: {str(j)[:200]}"
    return False, "", f"POS HTTP {r.status_code}: {r.text[:300]}"


def _push_async(row_id: int, pos_shop_id: str):
    """Chạy nền: đọc đơn, đẩy POS, cập nhật trạng thái."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ho_ten, so_dien_thoai, dia_chi, size, mau, combo, "
                    "so_luong, tien, sku, note FROM ladipage_inbound_orders "
                    "WHERE id=%s", (row_id,))
                r = cur.fetchone()
                if not r:
                    return
                rec = dict(zip(["ho_ten", "so_dien_thoai", "dia_chi", "size",
                                "mau", "combo", "so_luong", "tien", "sku",
                                "note"], r))
                shop = _shop_by_pos_id(cur, pos_shop_id)
            if not shop or not shop.get("pos_api_key"):
                _mark(conn, row_id, False, "", "Shop chưa có API key POS")
                return
        ok, oid, err = _push_to_pos(shop, rec)
        with get_conn() as conn:
            _mark(conn, row_id, ok, oid, err)
    except Exception as e:
        logger.exception("push_async fail id=%s", row_id)
        try:
            with get_conn() as conn:
                _mark(conn, row_id, False, "", f"Lỗi hệ thống: {e}")
        except Exception:
            pass


def _mark(conn, row_id, ok, oid, err):
    with conn.cursor() as cur:
        if ok and oid:
            cur.execute(
                "UPDATE ladipage_inbound_orders SET "
                "pos_status=%s, pos_order_id=%s, pos_error=%s, "
                "push_attempts=push_attempts+1, pushed_at=NOW(), "
                "match_status='co_pos', matched_order_code=%s, "
                "matched_order_id=%s, matched_at=NOW(), checked_at=NOW() "
                "WHERE id=%s",
                ("pushed", oid, err or None, oid, oid, row_id))
        else:
            cur.execute(
                "UPDATE ladipage_inbound_orders SET "
                "pos_status=%s, pos_order_id=%s, pos_error=%s, "
                "push_attempts=push_attempts+1, "
                "pushed_at=CASE WHEN %s THEN NOW() ELSE pushed_at END "
                "WHERE id=%s",
                ("pushed" if ok else "failed", oid or None, err or None, ok, row_id))
    conn.commit()


# ── webhook (công khai) ─────────────────────────────────────────────────────
def _webhook(pos_shop_id):
    payload = request.get_json(silent=True, force=True)
    form_dict = request.form.to_dict() if request.form else {}
    if not isinstance(payload, dict):
        payload = {}
    referer = request.headers.get("Referer") or request.headers.get("Origin") or ""

    rec = _extract(payload, form_dict, referer)
    raw = payload or form_dict or {}

    # Cần tối thiểu SĐT để thành đơn có nghĩa; vẫn lưu kể cả thiếu để không mất.
    with get_conn() as conn:
        # Chống trùng: cùng SĐT + note trong 10 phút → bỏ qua (ladipage bắn 2 lần)
        with conn.cursor() as cur:
            if rec["so_dien_thoai"]:
                cur.execute(
                    "SELECT id FROM ladipage_inbound_orders "
                    "WHERE so_dien_thoai=%s AND note=%s "
                    "AND received_at > NOW() - INTERVAL '10 minutes' LIMIT 1",
                    (rec["so_dien_thoai"], rec["note"]))
                dup = cur.fetchone()
                if dup:
                    return jsonify({"ok": True, "duplicate": True,
                                    "id": dup[0]}), 200
            shop = _shop_by_pos_id(cur, pos_shop_id)
            shop_key = shop["shop_key"] if shop else None
            # Đơn TEST (tên 'test' / số ảo 0987654321…) → vào thẳng "Bỏ qua", không
            # đối soát POS, không phình tab "chưa lên" (Long duyệt 07/09).
            from modules.ladipage.matcher import la_don_test
            _test = la_don_test(rec["ho_ten"], rec["so_dien_thoai"])
            cur.execute(
                "INSERT INTO ladipage_inbound_orders "
                "(shop_key, pos_shop_id, ho_ten, so_dien_thoai, dia_chi, size, "
                " mau, combo, so_luong, tien, sku, selections, source_url, "
                " source_name, note, raw_payload, match_status, pos_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (shop_key, str(pos_shop_id), rec["ho_ten"], rec["so_dien_thoai"],
                 rec["dia_chi"], rec["size"], rec["mau"], rec["combo"],
                 rec["so_luong"], rec["tien"], rec["sku"], rec["selections"],
                 rec["source_url"], rec["source_name"], rec["note"],
                 json.dumps(raw, ensure_ascii=False),
                 "bo_qua" if _test else None,
                 "don TEST (ten/so ao) - tu bo qua" if _test else None))
            row_id = cur.fetchone()[0]
        conn.commit()

    # ĐỔI TRỌNG TÂM (sếp Phong 12/08): KHÔNG đẩy đơn lên POS nữa.
    # Chỉ 1 shop POS nên đẩy vào rất khó phân biệt đơn/SP. Giờ phần mềm chỉ GIỮ
    # đơn khách điền, rồi ĐỐI SOÁT xem đơn đó đã được sale lên POS chưa —
    # thiếu thì báo sale kiểm tra lại. Muốn bật đẩy lại: app_config
    # ladipage_auto_push=1 (giữ _push_async để không mất đường lùi).
    try:
        from app_ctx import load_config
        auto_push = str((load_config() or {}).get("ladipage_auto_push") or "") == "1"
    except Exception:
        auto_push = False
    if auto_push:
        threading.Thread(target=_push_async, args=(row_id, str(pos_shop_id)),
                         daemon=True).start()
    else:
        threading.Thread(target=_match_async, args=(row_id,), daemon=True).start()
    return jsonify({"ok": True, "id": row_id}), 200


def _match_async(row_id: int):
    """Dò ngay đơn vừa nhận xem POS đã có chưa (thường CHƯA — sale lên sau).
    Dong thoi tim page/nhan vien cho don moi (khong can cho scheduler 15 phut)."""
    try:
        from modules.ladipage.matcher import run_match, tim_chu_ads
        run_match(limit=50)
        # Tim page/nhan vien cho don moi NGAY — khong can cho scheduler.
        tim_chu_ads(limit=10)
    except Exception as exc:
        logger.warning("ladipage _match_async(%s) error: %s", row_id, exc)


from modules.ladipage.matcher import CANH_BAO_GIO as _CANH_BAO_GIO


def _sheet_url() -> str:
    try:
        from app_ctx import load_config
        return str((load_config() or {}).get("ladipage_sheet_url") or "").strip()
    except Exception:
        return ""


# ── trang kiểm tra ──────────────────────────────────────────────────────────
@ladipage_bp.route("/")
def index():
    if not _can_view():
        abort(403)
    status = (request.args.get("status") or "all").strip()
    q = (request.args.get("q") or "").strip()
    dom = (request.args.get("dom") or "").strip().lower()   # lọc theo tên miền landing
    lp = (request.args.get("lp") or "").strip().lower()     # lọc theo ĐƯỜNG DẪN chi tiết
    nv = (request.args.get("nv") or "").strip()             # lọc theo nhân viên chạy ads
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    # Ngay "chưa lên POS" tuy chinh (status=chua_nngay&n=<so ngay>)
    try:
        n_days = max(1, int(request.args.get("n") or 0))
    except (TypeError, ValueError):
        n_days = None
    per_page = 30
    where, params = [], []
    if date_from:
        where.append("(received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date >= %s")
        params.append(date_from)
    if date_to:
        where.append("(received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date <= %s")
        params.append(date_to)
    # Tab theo KẾT QUẢ ĐỐI SOÁT (không còn theo trạng thái đẩy POS)
    if status in ("co_pos", "chua_co_pos", "cho_kiem_tra", "bo_qua"):
        where.append("COALESCE(match_status,'cho_kiem_tra')=%s")
        params.append(status)
    elif status == "chua_1ngay":
        where.append("COALESCE(match_status,'cho_kiem_tra')='chua_co_pos' "
                     "AND received_at < NOW() - INTERVAL '1 days'")
    elif status == "chua_2ngay":
        where.append("COALESCE(match_status,'cho_kiem_tra')='chua_co_pos' "
                     "AND received_at < NOW() - INTERVAL '2 days'")
    elif status == "chua_3ngay":
        where.append("COALESCE(match_status,'cho_kiem_tra')='chua_co_pos' "
                     "AND received_at < NOW() - INTERVAL '3 days'")
    elif status == "chua_nngay" and n_days:
        where.append("COALESCE(match_status,'cho_kiem_tra')='chua_co_pos' "
                     "AND received_at < NOW() - INTERVAL '%s days'" % n_days)
    elif status == "win_thieu":
        # Sếp Phong 12/08: page TEST chưa lên POS là BÌNH THƯỜNG (mar mới test hàng).
        # Chỉ đơn của page ĐANG BÁN THẬT (WIN) mà không lên POS mới đáng nghi.
        where.append(_WIN_THIEU_SQL)
    if dom:
        where.append("%s = %%s" % _DOM_SQL)
        params.append(dom)
    if lp:
        where.append("%s = %%s" % _LP_SQL)
        params.append(lp)
    if nv:
        try:
            where.append("ads_user_id = %s")
            params.append(int(nv))
        except (TypeError, ValueError):
            pass
    if q:
        where.append("(so_dien_thoai ILIKE %s OR ho_ten ILIKE %s "
                     "OR note ILIKE %s OR pos_order_id ILIKE %s)")
        params += [f"%{q}%"] * 4
    wsql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(match_status,'cho_kiem_tra'), COUNT(*) "
                "FROM ladipage_inbound_orders GROUP BY 1")
            counts = {r[0]: r[1] for r in cur.fetchall()}
            # Preset "N ngày chưa lên POS" gộp về 1 nút lọc (sếp Phong 07/09:
            # "quá nhiều ngày nhiều nó loạn á bạn") — tính sẵn đếm cho từng mốc
            # trong 1 query để đổ vào popover, khỏi bung 10 tab riêng ngoài giao diện.
            _mocs = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 20, 30)
            _filters = ", ".join(
                "COUNT(*) FILTER (WHERE d >= %d) AS d%d" % (m, m) for m in _mocs)
            cur.execute(f"""
                SELECT {_filters} FROM (
                    SELECT EXTRACT(EPOCH FROM (NOW()-received_at))/86400.0 AS d
                      FROM ladipage_inbound_orders
                     WHERE COALESCE(match_status,'cho_kiem_tra')='chua_co_pos'
                ) x
            """)
            _row = cur.fetchone()
            moc_counts = {m: _row[i] for i, m in enumerate(_mocs)}
            for _d, _k in ((1,"chua_1ngay"),(2,"chua_2ngay"),(3,"chua_3ngay")):
                counts[_k] = moc_counts.get(_d, 0)
            if n_days:
                if n_days in moc_counts:
                    counts["chua_nngay"] = moc_counts[n_days]
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM ladipage_inbound_orders "
                        "WHERE COALESCE(match_status,'cho_kiem_tra')='chua_co_pos' "
                        "  AND received_at < NOW() - INTERVAL '%s days'" % n_days)
                    counts["chua_nngay"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ladipage_inbound_orders WHERE " + _WIN_THIEU_SQL)
            counts["win_thieu"] = cur.fetchone()[0]
            # Tổng THẬT — không cộng counts vì 2 tab "canh_bao"/"win_thieu" là lát cắt
            # của các tab kia (đơn bị đếm 2-3 lần → tab Tất cả phình lên 147/97).
            cur.execute("SELECT COUNT(*) FROM ladipage_inbound_orders")
            tong_that = cur.fetchone()[0]
            cur.execute(
                f"SELECT COUNT(*) FROM ladipage_inbound_orders {wsql}", params)
            filtered_total = cur.fetchone()[0]
            # Ghi chú tốc độ 24/08: subquery fb_page_ten kiểu FULL-JOIN-mỗi-dòng
            # ăn 4,7s với 1300 đơn → chuyển sang JOIN gộp 1 lần (0,1s) để trang
            # tải nổi khi hiện TOÀN BỘ đơn cho tìm kiếm client-side.
            _o = "ladipage_inbound_orders."
            cur.execute(
                "SELECT " + _o + "id, received_at, ho_ten, so_dien_thoai, dia_chi, "
                "size, mau, combo, so_luong, tien, " + _o + "note, source_url, "
                "source_name, pos_status, pos_order_id, pos_error, "
                "push_attempts, pushed_at, handled_by, handled_at, "
                + _o + "pos_shop_id, shop_key, "
                "COALESCE(match_status,'cho_kiem_tra') AS match_status, "
                "matched_order_code, matched_at, "
                "EXTRACT(EPOCH FROM (NOW()-received_at))/3600 AS gio_troi, "
                "lower(split_part(regexp_replace(COALESCE(source_url,''),'^https?://',''),'/',1)) AS domain, "
                + _LP_SQL + " AS lp_full, "
                "CASE WHEN COALESCE(source_url,'') <> '' "
                "  THEN rtrim(lower(replace(split_part(split_part(regexp_replace(source_url,'^https?://',''),'?',1),'#',1),'www.','')),'/')"
                "  ELSE '' END AS link_chuan, "
                "raw_payload, selections, ads_account_id, ads_page_id, "
                # Win/Test theo ĐÚNG page FB chạy ra đơn đó (1 landing chạy nhiều page).
                # Win = page đã có doanh thu POS TRƯỚC ngày khách đặt (memo: Win kể từ
                # ngày page lên POS); chưa lên POS = còn đang test sản phẩm.
                "fbp.ten AS fb_page_ten, "
                "ptn.d AS pos_tu_ngay, "
                "COALESCE(NULLIF(u2.full_name,''), u2.username) AS ads_chu, "
                "COALESCE(t2.team_name,'') AS ads_team "
                "FROM ladipage_inbound_orders "
                "LEFT JOIN (SELECT COALESCE(n.page_id, sp.page_id) pid, "
                "                  COALESCE(NULLIF(MAX(n.name),''), MAX(sp.page_name)) ten "
                "             FROM fb_page_names n FULL JOIN fb_ads_page_daily_spend sp "
                "                  ON sp.page_id = n.page_id "
                "            GROUP BY 1) fbp ON fbp.pid = ads_page_id "
                "LEFT JOIN (SELECT m.page_id, MIN(m.metric_date) d "
                "             FROM pos_page_daily_metrics m WHERE m.revenue > 0 "
                "            GROUP BY 1) ptn ON ptn.page_id = ads_page_id "
                "LEFT JOIN users u2 ON u2.id = ads_user_id "
                "LEFT JOIN teams t2 ON t2.id = u2.team_id "
                f"{wsql} "
                "ORDER BY received_at DESC LIMIT %s OFFSET %s",
                params + [per_page, (page - 1) * per_page])
            cols = ["id", "received_at", "ho_ten", "so_dien_thoai", "dia_chi",
                    "size", "mau", "combo", "so_luong", "tien", "note",
                    "source_url", "source_name", "pos_status", "pos_order_id",
                    "pos_error", "push_attempts", "pushed_at", "handled_by",
                    "handled_at", "pos_shop_id", "shop_key",
                    "match_status", "matched_order_code", "matched_at", "gio_troi",
                    "domain", "lp_full", "link_chuan", "raw_payload", "selections", "ads_account_id", "ads_page_id",
                    "fb_page_ten", "pos_tu_ngay", "ads_chu", "ads_team"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Chủ của từng landing (đơn này phát sinh từ trang của ai)
    chu_landing, users_ds = {}, []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT o.domain, COALESCE(NULLIF(u.full_name,''), u.username)
                         FROM ladipage_landing_owner o
                         LEFT JOIN users u ON u.id = o.user_id""")
        chu_landing = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("""SELECT id, COALESCE(NULLIF(full_name,''), username)
                         FROM users WHERE COALESCE(status,'active')='active'
                        ORDER BY 2""")
        users_ds = [{"id": r[0], "ten": r[1]} for r in cur.fetchall()]
    for r in rows:
        # Chủ đơn = NV đang giữ TK QC chạy ra đơn đó (dò từ utm_id trong link).
        # Không dò được mới dùng bảng gán tay theo domain.
        r["chu"] = r.get("ads_chu") or chu_landing.get(r.get("domain") or "", "")
        r["chu_tu_dong"] = bool(r.get("ads_chu"))
        # WIN nếu page đã lên POS trước/đúng ngày khách đặt, chưa thì TEST
        _d1 = r.get("pos_tu_ngay")
        r["win_test"] = ("" if not r.get("ads_page_id")
                         else "WIN" if (_d1 and r["received_at"] and _d1 <= r["received_at"].date())
                         else "TEST")
        # Tên sản phẩm dễ đọc từ link landing (cho modal chi tiết)
        r["sp"] = _sp_ten(r.get("lp_full") or "")

    # Danh sách tên miền landing cho ô lọc — sếp Phong 20/08 xin bộ lọc cột Nguồn đơn
    dom_list = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT %s AS dom, COUNT(*),
                   COUNT(*) FILTER (WHERE COALESCE(match_status,'cho_kiem_tra')='chua_co_pos')
              FROM ladipage_inbound_orders
             WHERE COALESCE(source_url,'') <> ''
             GROUP BY 1 ORDER BY 2 DESC""" % _DOM_SQL)
        dom_list = [{"dom": r[0], "n": int(r[1] or 0), "thieu": int(r[2] or 0)}
                    for r in cur.fetchall() if r[0]]
        # Đường dẫn chi tiết (mỗi link ≈ 1 sản phẩm) — lọc tới từng mẫu đang test
        # Khi đã chọn landing (dom) thì chỉ hiện sản phẩm của landing đó.
        _lp_where = "WHERE COALESCE(source_url,'') <> ''"
        _lp_params = []
        if dom:
            _lp_where += " AND %s = %%s" % _DOM_SQL
            _lp_params.append(dom)
        cur.execute("""
            SELECT %s AS lp, COUNT(*),
                   COUNT(*) FILTER (WHERE COALESCE(match_status,'cho_kiem_tra')='chua_co_pos')
              FROM ladipage_inbound_orders
             %s
             GROUP BY 1 ORDER BY 2 DESC""" % (_LP_SQL, _lp_where), _lp_params)
        lp_list = [{"lp": r[0], "ten": _sp_ten(r[0]), "n": int(r[1] or 0),
                    "thieu": int(r[2] or 0)}
                   for r in cur.fetchall() if r[0]]
        # Danh sách nhân viên chạy ads cho ô lọc
        cur.execute("""
            SELECT o.ads_user_id, COALESCE(NULLIF(u.full_name,''), u.username),
                   COUNT(*),
                   COUNT(*) FILTER (WHERE COALESCE(o.match_status,'cho_kiem_tra')='chua_co_pos')
              FROM ladipage_inbound_orders o
              LEFT JOIN users u ON u.id = o.ads_user_id
             WHERE o.ads_user_id IS NOT NULL
             GROUP BY 1,2 ORDER BY 3 DESC""")
        nv_list = [{"id": r[0], "ten": (r[1] or ("NV " + str(r[0]))),
                    "n": int(r[2] or 0), "thieu": int(r[3] or 0)}
                   for r in cur.fetchall() if r[0]]

    # Nút ngày bấm nhanh cho điện thoại (tính sẵn ở server cho chắc)
    _t = date.today()
    _m1 = _t.replace(day=1)
    presets = [
        {"key": "", "ten": "Tất cả ngày", "f": "", "t": ""},
        {"key": "today", "ten": "Hôm nay", "f": _t.isoformat(), "t": _t.isoformat()},
        {"key": "yest", "ten": "Hôm qua",
         "f": (_t - timedelta(days=1)).isoformat(), "t": (_t - timedelta(days=1)).isoformat()},
        {"key": "7d", "ten": "7 ngày", "f": (_t - timedelta(days=6)).isoformat(), "t": _t.isoformat()},
        {"key": "30d", "ten": "30 ngày", "f": (_t - timedelta(days=29)).isoformat(), "t": _t.isoformat()},
        {"key": "month", "ten": "Tháng này", "f": _m1.isoformat(), "t": _t.isoformat()},
    ]
    for pr in presets:
        pr["on"] = (date_from == pr["f"] and date_to == pr["t"])

    total = tong_that
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    date_days = 0
    if date_from:
        try:
            _df = date.fromisoformat(date_from)
            _dt = date.fromisoformat(date_to) if date_to else date.today()
            date_days = max(1, (_dt - _df).days + 1)
        except ValueError:
            pass
    _today = date.today()
    date_chips = []
    for _d in [3, 7, 14, 30]:
        _f = (_today - timedelta(days=_d)).isoformat()
        _t = _today.isoformat()
        date_chips.append({"days": _d, "f": _f, "t": _t,
                           "on": date_from == _f and date_to == _t})
    return render_template(
        "ladipage/index.html", rows=rows, counts=counts, total=total,
        status=status, q=q, dom=dom, dom_list=dom_list, lp=lp, lp_list=lp_list,
        nv=nv, nv_list=nv_list,
        pos_shop_id="1720128057", canh_bao_gio=_CANH_BAO_GIO,
        n_days=n_days, moc_counts=moc_counts,
        sheet_url=_sheet_url(), users_ds=users_ds, presets=presets,
        page=page, per_page=per_page, total_pages=total_pages,
        filtered_total=filtered_total, date_from=date_from, date_to=date_to,
        date_days=date_days, date_chips=date_chips)


@ladipage_bp.route("/huong-dan")
def huong_dan():
    if not _can_view():
        abort(403)
    return render_template("ladipage/huong_dan.html")


@ladipage_bp.route("/<int:row_id>/repush", methods=["POST"])
def repush(row_id):
    if not _can_view():
        abort(403)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pos_shop_id FROM ladipage_inbound_orders "
                        "WHERE id=%s", (row_id,))
            r = cur.fetchone()
            if not r:
                flash("Không tìm thấy đơn", "error")
                return redirect(url_for("ladipage.index"))
            cur.execute("UPDATE ladipage_inbound_orders SET pos_status='pending' "
                        "WHERE id=%s", (row_id,))
        conn.commit()
        pos_shop_id = r[0]
    _push_async(row_id, str(pos_shop_id))  # đẩy ngay (đồng bộ) để thấy kết quả liền
    flash("Đã đẩy lại đơn #%s" % row_id, "success")
    return redirect(url_for("ladipage.index", **request.args))


@ladipage_bp.route("/<int:row_id>/done", methods=["POST"])
def mark_done(row_id):
    if not _can_view():
        abort(403)
    who = str(session.get("full_name") or session.get("username") or "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ladipage_inbound_orders SET pos_status='manual_done', "
                "handled_by=%s, handled_at=NOW() WHERE id=%s", (who, row_id))
        conn.commit()
    flash("Đã đánh dấu xử lý tay đơn #%s" % row_id, "success")
    return redirect(url_for("ladipage.index", **request.args))


# ── đăng ký ─────────────────────────────────────────────────────────────────
@ladipage_bp.route("/<int:row_id>/do-lai", methods=["POST"])
def do_lai(row_id):
    """Dò lại 1 đơn: xoá kết quả cũ rồi tìm lại trên POS."""
    if not _can_view():
        abort(403)
    from modules.ladipage.matcher import run_match
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ladipage_inbound_orders SET match_status='cho_kiem_tra' "
                    "WHERE id=%s", (row_id,))
        conn.commit()
    r = run_match(limit=200)
    flash(f"Đã dò lại — khớp POS: {r['co_pos']}, chưa thấy: {r['chua_co']}", "info")
    return redirect(url_for("ladipage.index", **{k: v for k, v in request.args.items()}))


@ladipage_bp.route("/<int:row_id>/bo-qua", methods=["POST"])
def bo_qua(row_id):
    """Đánh dấu đơn không cần lên POS (khách huỷ, đơn rác, test…)."""
    if not _can_view():
        abort(403)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ladipage_inbound_orders SET match_status='bo_qua', "
                    "handled_by=%s, handled_at=NOW() WHERE id=%s",
                    (session.get("username", ""), row_id))
        conn.commit()
    flash("Đã bỏ qua đơn này", "info")
    return redirect(url_for("ladipage.index", **{k: v for k, v in request.args.items()}))


@ladipage_bp.route("/do-tat-ca", methods=["POST"])
def do_tat_ca():
    """Dò lại toàn bộ đơn chưa khớp."""
    if not _can_view():
        abort(403)
    from modules.ladipage.matcher import run_match
    r = run_match(limit=2000)
    flash(f"Đã dò {r['kiem_tra']} đơn — khớp POS: {r['co_pos']}, chưa thấy: {r['chua_co']}",
          "success")
    return redirect(url_for("ladipage.index", **{k: v for k, v in request.args.items()}))


@ladipage_bp.route("/nguon-sheet", methods=["POST"])
def nguon_sheet():
    """Lưu link Google Sheet của sale + nạp ngay."""
    if not _can_view():
        abort(403)
    url = (request.form.get("sheet_url") or "").strip()
    from app_ctx import save_config_key
    save_config_key("ladipage_sheet_url", url)
    if not url:
        flash("Đã gỡ link Google Sheet", "info")
        return redirect(url_for("ladipage.index"))
    try:
        from modules.ladipage.sheet_import import import_sheet
        from modules.ladipage.matcher import run_match
        r = import_sheet(url)
        m = run_match(limit=3000)
        flash(f"Đọc {r['doc']} dòng — thêm mới {r['them']}, đã có {r['trung']}, "
              f"bỏ (thiếu SĐT) {r['bo']}. Đối soát: đã lên POS {m['co_pos']}, "
              f"CHƯA lên {m['chua_co']}, khách cũ {m['khach_cu']}.", "success")
    except Exception as exc:
        flash(f"Lỗi đọc sheet: {exc}", "danger")
    return redirect(url_for("ladipage.index"))


@ladipage_bp.route("/nap-sheet", methods=["POST"])
def nap_sheet():
    """Nạp lại sheet đã lưu (nút bấm tay)."""
    if not _can_view():
        abort(403)
    from app_ctx import load_config
    url = str((load_config() or {}).get("ladipage_sheet_url") or "").strip()
    if not url:
        flash("Chưa cấu hình link Google Sheet", "warning")
        return redirect(url_for("ladipage.index"))
    try:
        from modules.ladipage.sheet_import import import_sheet
        from modules.ladipage.matcher import run_match
        r = import_sheet(url)
        m = run_match(limit=3000)
        flash(f"Đã nạp {r['them']} đơn mới từ sheet. Đối soát: lên POS {m['co_pos']}, "
              f"CHƯA lên {m['chua_co']}, khách cũ {m['khach_cu']}.", "success")
    except Exception as exc:
        flash(f"Lỗi nạp sheet: {exc}", "danger")
    return redirect(url_for("ladipage.index"))


def register_ladipage_module(app, login_required=None):
    app.register_blueprint(ladipage_bp)
    # Webhook công khai (không login) — path /api/webhooks/ được whitelist sẵn
    app.add_url_rule("/api/webhooks/ladipage/<pos_shop_id>",
                     endpoint="ladipage_webhook", view_func=_webhook,
                     methods=["POST"])
    logger.info("ladipage module registered")


@ladipage_bp.route("/xuat-excel")
def xuat_excel():
    """Xuất Excel để sale đối chiếu — giữ đúng bộ lọc đang xem trên trang."""
    if not _can_view():
        abort(403)
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    status = (request.args.get("status") or "all").strip()
    q = (request.args.get("q") or "").strip()
    dom = (request.args.get("dom") or "").strip().lower()
    lp = (request.args.get("lp") or "").strip().lower()
    nv = (request.args.get("nv") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    where, params = [], []
    if date_from:
        where.append("(l.received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date >= %s")
        params.append(date_from)
    if date_to:
        where.append("(l.received_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date <= %s")
        params.append(date_to)
    if status in ("co_pos", "chua_co_pos", "cho_kiem_tra", "bo_qua"):
        where.append("COALESCE(l.match_status,'cho_kiem_tra')=%s")
        params.append(status)
    elif status in ("chua_1ngay","chua_2ngay","chua_3ngay"):
        _dd = {"chua_1ngay":1,"chua_2ngay":2,"chua_3ngay":3}[status]
        where.append("COALESCE(l.match_status,'cho_kiem_tra')='chua_co_pos' "
                     "AND l.received_at < NOW() - INTERVAL '%s days'" % _dd)
    elif status == "chua_nngay":
        try:
            _nd = max(1, int(request.args.get("n") or 0))
        except (TypeError, ValueError):
            _nd = None
        if _nd:
            where.append("COALESCE(l.match_status,'cho_kiem_tra')='chua_co_pos' "
                         "AND l.received_at < NOW() - INTERVAL '%s days'" % _nd)
    elif status == "win_thieu":
        where.append(_WIN_THIEU_SQL.replace("ladipage_inbound_orders.", "l.")
                     .replace("COALESCE(match_status", "COALESCE(l.match_status")
                     .replace("(received_at AT", "(l.received_at AT"))
    if dom:
        where.append("%s = %%s" % _DOM_SQL.replace("source_url", "l.source_url"))
        params.append(dom)
    if lp:
        where.append("%s = %%s" % _LP_SQL.replace("source_url", "l.source_url"))
        params.append(lp)
    if q:
        where.append("(l.so_dien_thoai ILIKE %s OR l.ho_ten ILIKE %s OR l.note ILIKE %s)")
        params += [f"%{q}%"] * 3
    if nv:
        try:
            where.append("l.ads_user_id = %s")
            params.append(int(nv))
        except (TypeError, ValueError):
            pass
    wsql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn, conn.cursor() as cur:
        # LƯU Ý 28/08: trước đây JOIN thẳng fb_ads_page_daily_spend (mỗi page cả
        # trăm dòng theo ngày) → mỗi đơn bị NHÂN BẢN hàng trăm lần, file vừa treo
        # >2 phút vừa trùng dòng. Phải JOIN bảng đã GROUP BY page_id.
        cur.execute(f"""
            SELECT l.id, l.received_at, l.ho_ten, l.so_dien_thoai, l.dia_chi, l.size, l.mau,
                   l.combo, l.tien, l.note, l.source_url,
                   COALESCE(l.match_status,'cho_kiem_tra'), l.matched_order_code,
                   l.pos_error, ROUND(EXTRACT(EPOCH FROM (NOW()-l.received_at))/3600),
                   COALESCE(NULLIF(ua.full_name,''), ua.username,
                            NULLIF(u.full_name,''), u.username, ''),
                   COALESCE(n.name, sp.page_name, ''),
                   ptn.d
              FROM ladipage_inbound_orders l
              LEFT JOIN users ua ON ua.id = l.ads_user_id
              LEFT JOIN ladipage_landing_owner o
                ON o.domain = lower(split_part(regexp_replace(COALESCE(l.source_url,''),'^https?://',''),'/',1))
              LEFT JOIN users u ON u.id = o.user_id
              LEFT JOIN fb_page_names n ON n.page_id = l.ads_page_id
              LEFT JOIN (SELECT page_id, MAX(page_name) AS page_name
                           FROM fb_ads_page_daily_spend GROUP BY page_id) sp
                     ON sp.page_id = l.ads_page_id
              LEFT JOIN (SELECT page_id, MIN(metric_date) AS d
                           FROM pos_page_daily_metrics WHERE revenue > 0
                          GROUP BY page_id) ptn
                     ON ptn.page_id = l.ads_page_id
              {wsql}
             ORDER BY l.received_at DESC""", params)
        rows = cur.fetchall()

    NHAN = {"co_pos": "ĐÃ lên POS", "chua_co_pos": "CHƯA lên POS",
            "khach_cu": "Khách cũ — đơn ngày khác", "cho_kiem_tra": "Chờ dò",
            "bo_qua": "Bỏ qua"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Đối chiếu đơn Ladi"
    head = ["ID", "Thời gian khách đặt", "Khách hàng", "SĐT", "Địa chỉ", "Size",
            "Màu", "Combo/SP", "Tiền (COD)", "TRẠNG THÁI", "Đơn POS",
            "Ghi chú đối soát", "Đã trôi (giờ)", "Landing (nguồn đơn)",
            "Page FB", "Landing của ai", "Sale kiểm tra (điền tay)"]
    ws.append(head)
    hf = PatternFill("solid", fgColor="1F4E79")
    for i in range(1, len(head) + 1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color="FFFFFF", size=11)
        c.fill = hf
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"

    do = PatternFill("solid", fgColor="FFC7CE")     # chưa lên POS
    vang = PatternFill("solid", fgColor="FFEB9C")   # khách cũ
    for r in rows:
        page_info = ""
        if r[16]:
            _wt = "WIN" if (r[17] and r[1] and r[17] <= r[1].date()) else "TEST"
            page_info = f"{_wt} {r[16]}"
        ws.append([
            r[0],
            r[1].strftime("%d/%m/%Y %H:%M") if r[1] else "",
            r[2] or "", r[3] or "", r[4] or "", r[5] or "", r[6] or "",
            r[7] or "", int(r[8] or 0), NHAN.get(r[11], r[11]),
            r[12] or "", r[13] or "", int(r[14] or 0), r[10] or "", page_info, r[15] or "", "",
        ])
        if r[11] == "chua_co_pos":
            for i in range(1, len(head) + 1):
                ws.cell(row=ws.max_row, column=i).fill = do
        elif r[11] == "khach_cu":
            for i in range(1, len(head) + 1):
                ws.cell(row=ws.max_row, column=i).fill = vang

    for i, w in enumerate([7, 18, 22, 14, 40, 10, 14, 26, 13, 24, 12, 34, 12, 30, 22, 18, 24], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    ten = f"doi-chieu-don-ladipage-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    from flask import send_file
    return send_file(bio, as_attachment=True, download_name=ten,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@ladipage_bp.route("/gan-chu-landing", methods=["POST"])
def gan_chu_landing():
    """Gán 1 landing LadiPage cho 1 nhân viên — để biết đơn của ai."""
    if not _can_view():
        abort(403)
    domain = (request.form.get("domain") or "").strip().lower()
    uid = (request.form.get("user_id") or "").strip()
    if not domain:
        flash("Đơn này không có nguồn landing", "warning")
        return redirect(url_for("ladipage.index", **request.args))
    with get_conn() as conn, conn.cursor() as cur:
        if uid:
            cur.execute("""
                INSERT INTO ladipage_landing_owner (domain, user_id, updated_by, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (domain) DO UPDATE
                   SET user_id=EXCLUDED.user_id, updated_by=EXCLUDED.updated_by,
                       updated_at=NOW()""",
                        (domain, int(uid), session.get("username", "")))
            flash(f"Đã gán landing {domain} cho nhân viên", "success")
        else:
            cur.execute("DELETE FROM ladipage_landing_owner WHERE domain=%s", (domain,))
            flash(f"Đã gỡ chủ của landing {domain}", "info")
        conn.commit()
    return redirect(url_for("ladipage.index", **request.args))
