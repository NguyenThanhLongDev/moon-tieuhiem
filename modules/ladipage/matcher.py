# -*- coding: utf-8 -*-
"""Đối soát đơn LadiPage ↔ đơn POS — sếp Phong 12/08."""

import logging
import re as _re

logger = logging.getLogger("ladipage.matcher")
MATCH_DAYS = 3
CANH_BAO_GIO = 3

_UTM_RE = _re.compile(r"utm_(?:id|campaign|content|term|source_id)=(\d{6,})", _re.I)

def norm_phone(s) -> str:
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    if d.startswith("84") and len(d) >= 11: d = "0" + d[2:]
    elif d and not d.startswith("0") and len(d) == 9: d = "0" + d
    return d

# Phải KHỚP norm_phone() ở trên: (1) 84xxxxxxxxx → 0xxxxxxxxx; (2) 9 số không có
# 0 đầu → thêm 0 (POS/Excel hay rụng số 0: '855700487' — Long 07/09, 3 đơn báo oan).
_SQL_NORM = ("CASE WHEN regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') LIKE '84%%' AND length(regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g')) >= 11 THEN '0' || substring(regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') from 3) "
             "WHEN length(regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g')) = 9 AND regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') NOT LIKE '0%%' THEN '0' || regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') "
             "ELSE regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') END")

# Số ảo hay dùng khi test form Ladi (Long duyệt 07/09 — 62 đơn test làm bẩn tab "chưa lên POS")
_SO_TEST = {"0987654321", "0123456789", "0000000000", "0909090909", "0987765432", "0912123321"}
_TEN_TEST = _re.compile(r"\btest\b|laditest|^ẩn$", _re.IGNORECASE)


def la_don_test(ho_ten, so_dien_thoai) -> bool:
    """Đơn TEST (tên chứa 'test' / số ảo / số 1 chữ số lặp) → tự vào 'Bỏ qua', không đối soát."""
    p = norm_phone(so_dien_thoai)
    ten = str(ho_ten or "").strip()
    if p in _SO_TEST:
        return True
    if p and len(set(p)) == 1:          # 0000000000, 1111111111...
        return True
    return bool(_TEN_TEST.search(ten))


def _pos_shop_ids(cur) -> list:
    cur.execute("SELECT id FROM shops WHERE COALESCE(status,'active')='active' AND COALESCE(pancake_shop_id,'') <> ''")
    return [r[0] for r in cur.fetchall()]

def run_match(limit: int = 500) -> dict:
    out = {"kiem_tra": 0, "co_pos": 0, "chua_co": 0, "khach_cu": 0}
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, COALESCE(NULLIF(phone_norm,''), so_dien_thoai), received_at FROM ladipage_inbound_orders WHERE COALESCE(match_status,'cho_kiem_tra') IN ('cho_kiem_tra','chua_co_pos') AND (pos_order_id IS NULL OR pos_order_id = '') ORDER BY received_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            shop_ids = _pos_shop_ids(cur)
        for row_id, phone, received_at in rows:
            p = norm_phone(phone)
            out["kiem_tra"] += 1
            if not p or len(p) < 9: continue
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, COALESCE(order_code, external_order_id) FROM orders WHERE {_SQL_NORM} = %s AND shop_id = ANY(%s) AND created_at_pos BETWEEN %s - INTERVAL '2 hours' AND %s + INTERVAL '{MATCH_DAYS} days' ORDER BY created_at_pos LIMIT 1", (p, shop_ids, received_at, received_at))
                hit = cur.fetchone()
                if not hit:
                    # so sánh created_at_pos > received_at làm ngay trong SQL vì 2 cột
                    # khác kiểu (naive vs aware) — Python so là nổ TypeError
                    # Long 07/09: KHÔNG lấy "đơn gần nhất tuyệt đối" nữa — khách mua nhiều
                    # lần thì đơn gần nhất hay là đơn CŨ trước ngày đặt → bị gán "khách cũ"
                    # dù đã có đơn mới sau đó (5 đơn báo oan). Ưu tiên đơn POS ĐẦU TIÊN
                    # SAU lúc đặt (sale lên trễ); chỉ khi không có mới xét đơn cũ gần nhất.
                    cur.execute(f"SELECT id, COALESCE(order_code, external_order_id), to_char(created_at_pos, 'DD/MM/YYYY'), (created_at_pos > %s) FROM orders WHERE {_SQL_NORM} = %s AND shop_id = ANY(%s) AND created_at_pos > %s - INTERVAL '2 hours' ORDER BY created_at_pos ASC LIMIT 1", (received_at, p, shop_ids, received_at))
                    gan = cur.fetchone()
                    if not gan:
                        cur.execute(f"SELECT id, COALESCE(order_code, external_order_id), to_char(created_at_pos, 'DD/MM/YYYY'), (created_at_pos > %s) FROM orders WHERE {_SQL_NORM} = %s AND shop_id = ANY(%s) ORDER BY created_at_pos DESC LIMIT 1", (received_at, p, shop_ids))
                        gan = cur.fetchone()
                    # Đơn POS gần nhất nằm SAU lúc khách đặt = chính đơn này, sale
                    # lên TRỄ quá cửa sổ 3 ngày → vẫn tính ĐÃ lên POS (sếp Phong
                    # 27/08: "khách đặt 21 - 24/8 có đơn trên pos → là có trên pos").
                    if gan and gan[3]:
                        cur.execute("UPDATE ladipage_inbound_orders SET match_status='co_pos', matched_order_id=%s, matched_order_code=%s, matched_at=NOW(), checked_at=NOW(), phone_norm=%s, pos_error=%s WHERE id=%s", (gan[0], gan[1], p, "sale len tre - don POS " + (gan[2] or "?"), row_id))
                        out["co_pos"] += 1
                        continue
                    if gan:
                        cur.execute("UPDATE ladipage_inbound_orders SET match_status='chua_co_pos', matched_order_id=NULL, matched_order_code=NULL, checked_at=NOW(), phone_norm=%s, pos_error=%s WHERE id=%s", (p, "khach cu - don POS gan nhat " + (gan[2] or "?") + " (#" + str(gan[1] or "") + ")", row_id))
                        out["chua_co"] += 1
                        continue
                if hit:
                    cur.execute("UPDATE ladipage_inbound_orders SET match_status='co_pos', matched_order_id=%s, matched_order_code=%s, matched_at=NOW(), checked_at=NOW(), phone_norm=%s WHERE id=%s", (hit[0], hit[1], p, row_id))
                    out["co_pos"] += 1
                else:
                    cur.execute("UPDATE ladipage_inbound_orders SET match_status='chua_co_pos', checked_at=NOW(), phone_norm=%s WHERE id=%s", (p, row_id))
                    out["chua_co"] += 1
            conn.commit()
    logger.info("ladipage match: %s", out)
    return out

def verify_pos_direct(days: int = 14, limit: int = 800, nghi: float = 0.05) -> dict:
    """DỨT ĐIỂM (Long 07/09): đơn còn 'chua_co_pos' sau khi dò DB → hỏi THẲNG Pancake POS.

    Bảng `orders` chỉ là bản sao — đã 3 lần sai vì sync trễ/thiếu/sai số. POS là
    nguồn gốc: search theo SĐT (Pancake tự khớp cả số rụng 0), lấy đơn POS đầu tiên
    tạo SAU lúc khách đặt (trừ 2h lệch giờ) → đánh dấu ĐÃ lên POS. Quét ~500 đơn
    ≈ 2-3 phút, chạy sau run_match mỗi 15 phút.
    """
    import requests
    import time as _t
    out = {"quet": 0, "co_pos": 0, "loi": 0}
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pos_shop_id, api_key FROM ladipage_shops WHERE active LIMIT 1")
            shop = cur.fetchone()
            if not shop:
                logger.warning("verify_pos_direct: không có ladipage_shops active")
                return out
            sid, key = shop
            cur.execute(
                "SELECT id, COALESCE(NULLIF(phone_norm,''), so_dien_thoai), received_at "
                "FROM ladipage_inbound_orders "
                "WHERE COALESCE(match_status,'')='chua_co_pos' AND received_at >= NOW() - INTERVAL '%s days' "
                "ORDER BY received_at DESC LIMIT %s" % (int(days), int(limit)))
            rows = cur.fetchall()
        for row_id, phone, received_at in rows:
            p = norm_phone(phone)
            if not p or len(p) < 9:
                continue
            out["quet"] += 1
            try:
                r = requests.get(f"{_POS_BASE}/shops/{sid}/orders",
                                 params={"api_key": key, "search": p, "page_size": 10},
                                 timeout=20).json()
            except Exception as exc:
                out["loi"] += 1
                logger.warning("verify_pos_direct #%s lỗi API: %s", row_id, exc)
                continue
            moc = (received_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(received_at, "strftime") else str(received_at))[:19]
            hit = None
            for d in sorted((r.get("data") or []), key=lambda x: str(x.get("inserted_at") or "")):
                ins = str(d.get("inserted_at") or "")[:19].replace("T", " ")
                if ins and ins >= moc:
                    hit = (str(d.get("id") or ""), ins[:10])
                    break
            if hit:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ladipage_inbound_orders SET match_status='co_pos', matched_order_id=NULL, "
                        "matched_order_code=%s, matched_at=NOW(), checked_at=NOW(), phone_norm=%s, pos_error=%s "
                        "WHERE id=%s",
                        (hit[0], p, "xac nhan THANG POS - don " + hit[1], row_id))
                out["co_pos"] += 1
            _t.sleep(nghi)
        conn.commit()
    logger.info("verify_pos_direct: %s", out)
    return out


_POS_BASE = "https://pos.pages.fm/api/v1"


def canh_bao_text(gio: int = CANH_BAO_GIO, limit: int = 20) -> str:
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, ho_ten, so_dien_thoai, tien, source_name, to_char(received_at AT TIME ZONE 'Asia/Ho_Chi_Minh', 'DD/MM HH24:MI') FROM ladipage_inbound_orders WHERE match_status='chua_co_pos' AND received_at < NOW() - INTERVAL '%s hours' AND received_at > NOW() - INTERVAL '7 days' ORDER BY received_at DESC" % int(gio))
        rows = cur.fetchall()
    if not rows: return ""
    L = ["⚠️ ĐƠN LADIPAGE CHƯA THẤY TRÊN POS: %s đơn" % len(rows), "(khach da dien form hon %s tieng ma chua co don tuong ung)" % gio, ""]
    for r in rows[:limit]:
        tien = f"{int(r[3] or 0):,}đ".replace(",", ".") if r[3] else "—"
        L.append("• #%s %s · %s · %s · %s" % (r[0], r[1] or "(chua ro ten)", r[2] or "—", tien, r[5]) + (" · " + r[4] if r[4] else ""))
    if len(rows) > limit: L.append("… va %s don nua" % (len(rows) - limit))
    L += ["", "👉 Nho sale kiem tra: moon.tieuhiem.com/ladipage/"]
    return "\n".join(L)

def ids_tu_link(url: str) -> list:
    return list({m for m in _UTM_RE.findall(url or "")})


def _lay_domain(url):
    m = _re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""

def _fallback_domain_link(cur, row_id, url, pid, uid):
    """Fallback: neu chua co page, dò bang ten mien trong fb_ad_landing_links
    (bang nay co domain -> page_id, account_id, do synced tu Facebook)."""
    if pid:
        return
    dom = _lay_domain(url)
    if not dom:
        return
    cur.execute("""
        SELECT page_id, account_id FROM fb_ad_landing_links
         WHERE domain = %s AND COALESCE(page_id,'') <> ''
         GROUP BY 1,2 ORDER BY COUNT(1) DESC LIMIT 1""", (dom,))
    r = cur.fetchone()
    if not r:
        return
    pid = str(r[0])
    acct = str(r[1]) if r[1] else None
    uid = uid or None
    if acct and not uid:
        cur.execute("""SELECT user_id FROM user_ad_account_assignments
                         WHERE REPLACE(ad_account_id,'act_','') = REPLACE(%s,'act_','')
                           AND assigned_to IS NULL LIMIT 1""", (acct,))
        r2 = cur.fetchone()
        if r2: uid = r2[0]
    cur.execute("UPDATE ladipage_inbound_orders SET ads_account_id=%s, ads_page_id=%s WHERE id=%s",
                (acct, pid, row_id))


def _propagate_domain(cur, row_id, url, pid=None, uid=None):
    """Neu chua co page/user, suy ra tu cac don khac CUNG DOMAIN / CUNG PAGE da co.
    Luon fallback theo domain (nhan vien chiem da so) khi van con thieu user."""
    # 1) Neu thieu user -> tim tu don khac cung page
    if not uid and pid:
        cur.execute("""
            SELECT ads_user_id, COUNT(1) FROM ladipage_inbound_orders
             WHERE ads_page_id = %s AND ads_user_id IS NOT NULL
             GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", (pid,))
        r = cur.fetchone()
        if r:
            uid = r[0]
            cur.execute("UPDATE ladipage_inbound_orders SET ads_user_id=%s WHERE id=%s", (uid, row_id))
    # 2) Neu thieu page -> tim tu don khac cung user
    if not pid and uid:
        cur.execute("""
            SELECT ads_page_id, COUNT(1) FROM ladipage_inbound_orders
             WHERE ads_user_id = %s AND ads_page_id IS NOT NULL
             GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", (uid,))
        r = cur.fetchone()
        if r:
            pid = r[0]
            cur.execute("UPDATE ladipage_inbound_orders SET ads_page_id=%s WHERE id=%s", (pid, row_id))
    # 3) Van con thieu (page hoac user) -> suy theo DOMAIN (nhan vien chiem da so cua domain)
    if (not uid or not pid) and url:
        dom = _lay_domain(url)
        if dom:
            cur.execute("""
                SELECT ads_page_id, ads_user_id, COUNT(1) FROM ladipage_inbound_orders
                 WHERE lower(split_part(regexp_replace(COALESCE(source_url,''),'^https?://',''),'/',1)) = %s
                   AND (ads_page_id IS NOT NULL OR ads_user_id IS NOT NULL)
                 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 1""", (dom,))
            r = cur.fetchone()
            if r and (r[0] or r[1]):
                new_pid = r[0] if not pid else pid
                new_uid = r[1] if not uid else uid
                if new_pid or new_uid:
                    cur.execute("UPDATE ladipage_inbound_orders SET ads_page_id=%s, ads_user_id=%s WHERE id=%s",
                                (new_pid, new_uid, row_id))

def tim_chu_ads(limit: int = 500) -> dict:
    out = {"xet": 0, "ra_chu": 0, "khong_ra": 0, "ra_page": 0}
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, source_url FROM ladipage_inbound_orders WHERE ads_checked_at IS NULL AND COALESCE(source_url,'') <> '' ORDER BY received_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        for row_id, url in rows:
            out["xet"] += 1
            ids = ids_tu_link(url)
            acct = uid = pid = None
            if ids:
                with conn.cursor() as cur:
                    cur.execute("SELECT account_id, page_id FROM mb_fb_entity_daily WHERE (campaign_id = ANY(%s) OR adset_id = ANY(%s) OR ad_id = ANY(%s)) AND COALESCE(page_id,'') <> '' ORDER BY metric_date DESC LIMIT 1", (ids, ids, ids))
                    r = cur.fetchone()
                    if not r:
                        cur.execute("SELECT account_id, NULL FROM mb_fb_entity_daily WHERE campaign_id = ANY(%s) OR adset_id = ANY(%s) OR ad_id = ANY(%s) ORDER BY metric_date DESC LIMIT 1", (ids, ids, ids))
                        r = cur.fetchone()
                    if not r:
                        cur.execute("SELECT account_id, NULL FROM fb_campaign_status WHERE campaign_id = ANY(%s) ORDER BY updated_at DESC LIMIT 1", (ids,))
                        r = cur.fetchone()
                    if r:
                        acct = str(r[0])
                        pid = str(r[1]) if r[1] else None
                        if acct and not pid:
                            cur.execute("SELECT page_id FROM fb_page_ad_account_map WHERE ad_account_id = %s LIMIT 1", (acct,))
                            rp = cur.fetchone()
                            if rp: pid = str(rp[0])
                        cur.execute("SELECT user_id FROM user_ad_account_assignments WHERE REPLACE(ad_account_id,'act_','') = REPLACE(%s,'act_','') AND assigned_to IS NULL LIMIT 1", (acct,))
                        r2 = cur.fetchone()
                        uid = r2[0] if r2 else None
                        # Ghi trực tiếp page/account/nhan-vien tìm được từ campaign NGAY đây,
                        # tránh bị bước "đọc lại từ DB" phía dưới đọc ra NULL rồi ghi đè mất.
                        cur.execute("UPDATE ladipage_inbound_orders SET ads_account_id=%s, ads_page_id=%s, ads_user_id=%s WHERE id=%s",
                                    (acct, pid, uid, row_id))
            if not pid and url:
                dom = ""
                try:
                    m = _re.match(r"https?://([^/]+)", url)
                    if m: dom = m.group(1).replace("www.", "").lower()
                except Exception: pass
                if dom:
                    with conn.cursor() as cur:
                        name_part = dom.split(".")[0][:20]
                        cur.execute("SELECT page_id, page_name, fb_ad_account_id FROM fb_ads_page_daily_spend WHERE LOWER(page_name) LIKE %s ORDER BY metric_date DESC LIMIT 1", ("%" + name_part + "%",))
                        rp = cur.fetchone()
                        if rp:
                            pid = str(rp[0])
                            if not acct and rp[2]: acct = str(rp[2])
                            if not uid and acct:
                                cur.execute("SELECT user_id FROM user_ad_account_assignments WHERE REPLACE(ad_account_id,'act_','') = REPLACE(%s,'act_','') AND assigned_to IS NULL LIMIT 1", (acct,))
                                r2 = cur.fetchone()
                                if r2: uid = r2[0]
            with conn.cursor() as cur:
                _fallback_domain_link(cur, row_id, url, pid, uid)
                _propagate_domain(cur, row_id, url, pid, uid)
            with conn.cursor() as cur:
                cur.execute("SELECT ads_account_id, ads_user_id, ads_page_id FROM ladipage_inbound_orders WHERE id=%s", (row_id,))
                _r = cur.fetchone()
                if _r:
                    acct, uid, pid = _r[0], _r[1], _r[2]
                cur.execute("UPDATE ladipage_inbound_orders SET ads_account_id=%s, ads_user_id=%s, ads_page_id=%s, ads_checked_at=NOW() WHERE id=%s", (acct, uid, pid, row_id))
            conn.commit()
            if uid: out["ra_chu"] += 1
            else: out["khong_ra"] += 1
            if pid: out["ra_page"] += 1
    logger.info("tim_chu_ads: %s", out)
    return out