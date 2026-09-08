"""Facebook Graph API helpers cho module Page & Tài khoản."""
from __future__ import annotations
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v20.0"
_DELAY = 0.25  # giây giữa các API call để tránh rate limit


def _get(url: str, params: dict, timeout: int = 20) -> dict:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception as e:
        logger.warning("FB API GET error %s: %s", url, e)
        return {}


def _paginate(url: str, params: dict) -> list[dict]:
    """Lấy tất cả items qua cursor pagination."""
    items: list[dict] = []
    next_url: str | None = url
    p = dict(params)
    while next_url:
        data = _get(next_url, p)
        items.extend(data.get("data", []))
        paging = data.get("paging", {})
        next_url = paging.get("next")
        p = {}  # next URL đã chứa params
        time.sleep(_DELAY)
    return items


def get_user_info(token: str) -> dict:
    """Lấy thông tin user FB từ token."""
    return _get(f"{GRAPH}/me", {"fields": "id,name,email", "access_token": token})


def get_businesses(token: str) -> list[dict]:
    """Danh sách Business Manager user có quyền."""
    return _paginate(
        f"{GRAPH}/me/businesses",
        {"fields": "id,name,permitted_roles,created_time", "access_token": token, "limit": 100},
    )


def get_bm_pages(bm_id: str, token: str) -> list[dict]:
    """Pages owned + client pages của 1 BM."""
    fields = "id,name,category,picture,is_published,tasks,username"
    owned = _paginate(
        f"{GRAPH}/{bm_id}/owned_pages",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for p in owned:
        p["_relation"] = "owned"

    time.sleep(_DELAY)
    client = _paginate(
        f"{GRAPH}/{bm_id}/client_pages",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for p in client:
        p["_relation"] = "client"

    # Dedupe theo page_id, ưu tiên owned
    seen: dict[str, dict] = {}
    for p in owned + client:
        pid = str(p.get("id", ""))
        if pid and pid not in seen:
            seen[pid] = p
    return list(seen.values())


def get_bm_ad_accounts(bm_id: str, token: str) -> list[dict]:
    """Ad accounts owned + client của 1 BM."""
    fields = "id,name,account_status,currency,timezone_name,business"
    owned = _paginate(
        f"{GRAPH}/{bm_id}/owned_ad_accounts",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for a in owned:
        a["_relation"] = "owned"

    time.sleep(_DELAY)
    client = _paginate(
        f"{GRAPH}/{bm_id}/client_ad_accounts",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for a in client:
        a["_relation"] = "client"

    seen: dict[str, dict] = {}
    for a in owned + client:
        aid = str(a.get("id", ""))
        if aid and aid not in seen:
            seen[aid] = a
    return list(seen.values())


def get_user_ad_accounts(token: str) -> list[dict]:
    """Ad accounts mà token (kể cả System User) truy cập TRỰC TIẾP — không qua BM.
    Cần cho System User Token vì /me/businesses thường rỗng."""
    fields = "id,name,account_status,currency,timezone_name,business"
    items = _paginate(
        f"{GRAPH}/me/adaccounts",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for a in items:
        a["_relation"] = "owned"
    return items


def get_user_pages(token: str) -> list[dict]:
    """Pages mà token quản trị TRỰC TIẾP (/me/accounts) — không qua BM."""
    fields = "id,name,category,picture,is_published,tasks,username"
    items = _paginate(
        f"{GRAPH}/me/accounts",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for p in items:
        p["_relation"] = "owned"
    return items


def get_page_basic(page_id: str, token: str) -> dict | None:
    """Đọc TÊN + ẢNH 1 page theo id (/{page_id}?fields=name,picture).
    Chỉ chạy được khi page ĐÃ gán cho token (nếu chưa gán → lỗi #10, trả None).
    Dùng backfill cho page mồ côi sau khi user gán vào System User/BM."""
    data = _get(
        f"{GRAPH}/{page_id}",
        {"fields": "id,name,category,picture.type(large)", "access_token": token},
    )
    if not data or data.get("error") or not data.get("id"):
        return None
    return data


def get_account_promote_pages(account_id: str, token: str) -> list[dict]:
    """Pages mà 1 ad account có thể quảng cáo (/act_<id>/promote_pages).
    Trả về id+name+picture — dùng BACKFILL tên/ảnh cho page KHÔNG quản trực tiếp
    (page không nằm trong /me/accounts). CHỈ cần quyền ads_read — không cần
    pages_read_engagement."""
    acc = str(account_id)
    if not acc.startswith("act_"):
        acc = f"act_{acc}"
    fields = "id,name,picture.type(large)"
    items = _paginate(
        f"{GRAPH}/{acc}/promote_pages",
        {"fields": fields, "access_token": token, "limit": 200},
    )
    for p in items:
        p["_relation"] = "promote"
    return items


ACCOUNT_STATUS_LABEL = {
    1: ("Hoạt động", "success"),
    2: ("Đã tắt", "secondary"),
    3: ("Chưa thanh toán", "danger"),
    7: ("Đang xem xét", "warning"),
    8: ("Chờ thanh toán", "warning"),
    9: ("Trong giai đoạn ân hạn", "warning"),
    100: ("Đã đóng", "secondary"),
    101: ("Hết hạn thanh toán", "danger"),
    201: ("Chờ xử lý", "warning"),
}
