"""
Nguồn duy nhất cho Pancake POS API key.

Cách dùng:
    from pancake_auth import get_api_key, get_shop_api_key, clear_cache

Thứ tự ưu tiên khi lấy key cho từng shop:
  1. Bảng wh_shops trong DB (pos_api_key, hex 32 ký tự)
  2. File shops.json (trường pos_api_key, hex 32 ký tự)

Key hợp lệ: chuỗi hex 32 ký tự (ví dụ: aa4d860a...).
JWT cũ (bắt đầu bằng eyJ) được tự động lọc bỏ.
"""
import json
import os

_KEY_CACHE: "str | None" = None
_SHOP_KEY_CACHE: "dict | None" = None
_SHOPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shops.json")


def is_valid_hex_api_key(key: str) -> bool:
    """Kiểm tra key có phải hex 32 ký tự hợp lệ (không phải JWT cũ)."""
    if not key:
        return False
    k = str(key).strip()
    return len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k)


def get_api_key() -> str:
    """Trả về Pancake POS master API key hợp lệ (hex 32 ký tự). Dùng làm fallback."""
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    keys = _load_shop_keys()
    if keys:
        _KEY_CACHE = next(iter(keys.values()), "")
    else:
        _KEY_CACHE = ""
    return _KEY_CACHE


def get_shop_api_key(pos_shop_id: str) -> str:
    """Trả về API key riêng của shop. Ưu tiên DB, fallback shops.json.
    Trả về "" nếu không tìm thấy key hợp lệ.
    """
    global _SHOP_KEY_CACHE
    if _SHOP_KEY_CACHE is None:
        _SHOP_KEY_CACHE = _load_shop_keys()
    return _SHOP_KEY_CACHE.get(str(pos_shop_id), "")


def _load_shop_keys() -> dict:
    """Load tất cả per-shop API keys hợp lệ.
    Ưu tiên bảng wh_shops (DB), fallback sang shops.json khi DB không có key.
    Chỉ trả về key hex 32 ký tự — tự động bỏ qua JWT cũ.
    """
    db_keys = _load_from_db()
    json_keys = _load_from_json()
    # Merge: DB ưu tiên, json bổ sung khi DB thiếu
    merged = {**json_keys, **db_keys}
    return merged


def _load_from_db() -> dict:
    """Đọc hex API keys từ bảng wh_shops trong DB."""
    try:
        import psycopg2
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            return {}
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT pos_shop_id, pos_api_key FROM wh_shops "
            "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' AND status='active'"
        )
        result = {
            str(r[0]): str(r[1]).strip()
            for r in cur.fetchall()
            if r[0] and is_valid_hex_api_key(str(r[1] or ""))
        }
        cur.close()
        conn.close()
        return result
    except Exception:
        return {}


def _load_from_json() -> dict:
    """Đọc hex API keys từ shops.json, keyed theo pos_shop_id (hoặc shop_id)."""
    try:
        with open(_SHOPS_PATH, encoding="utf-8") as f:
            shops = json.load(f)
        result = {}
        for s in shops:
            if s.get("status") != "active":
                continue
            key = str(s.get("pos_api_key") or "").strip()
            if not is_valid_hex_api_key(key):
                continue
            # Lấy pos_shop_id: thử cả 2 tên field
            pos_id = str(s.get("pos_shop_id") or s.get("shop_id") or "").strip()
            if pos_id:
                result[pos_id] = key
        return result
    except Exception:
        return {}


def sync_keys_from_json_to_db() -> int:
    """
    Đồng bộ hex API keys từ shops.json vào bảng wh_shops (DB).
    Chỉ update những shop CHƯA có key hợp lệ trong DB.
    Trả về số shop đã được cập nhật.
    """
    json_keys = _load_from_json()
    if not json_keys:
        return 0
    updated = 0
    try:
        import psycopg2
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            return 0
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        for pos_id, key in json_keys.items():
            # Chỉ update nếu DB chưa có key hợp lệ
            cur.execute(
                "UPDATE wh_shops SET pos_api_key=%s "
                "WHERE pos_shop_id=%s AND status='active' "
                "AND (pos_api_key IS NULL OR pos_api_key='' OR LENGTH(pos_api_key)!=32)",
                (key, pos_id)
            )
            updated += cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        # Xoá cache để reload
        clear_cache()
        return updated
    except Exception:
        return 0


def clear_cache() -> None:
    """Xoá cache sau khi cập nhật key mới — lần gọi tiếp sẽ reload."""
    global _KEY_CACHE, _SHOP_KEY_CACHE
    _KEY_CACHE = None
    _SHOP_KEY_CACHE = None
