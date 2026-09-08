"""
Redis cache tập trung — thay thế in-process dict cache trong mỗi worker.

Lý do:
- In-process cache (dict): mỗi worker giữ bản copy riêng → 3 workers = 3 lần compute
- Redis cache: 1 bản dùng chung → compute 1 lần / TTL, tất cả workers đọc cùng key

Dùng:
    from redis_cache import cache_get, cache_set, cache_delete, cache_get_or_set

Fallback: nếu Redis không kết nối được → fallback về dict trong memory (không crash).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Optional

log = logging.getLogger("redis_cache")

# CPQC (moon) dùng Redis DB 9 RIÊNG — KHÔNG dùng chung DB 0 với POS gốc (tieuhiem).
# Default để /9 (không phải /0) để dù script chạy thiếu REDIS_URL trong env
# cũng không bao giờ đọc/ghi nhầm cache của tieuhiem. Xem CLAUDE.md §6.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/9")
_KEY_PREFIX = "pos:"

# ── Lazy connection ────────────────────────────────────────────────────────────
_redis_client = None
_redis_available = None   # None = chưa thử, True/False = đã thử


def _get_redis():
    global _redis_client, _redis_available
    if _redis_available is False:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis as _redis
        client = _redis.Redis.from_url(
            _REDIS_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        client.ping()
        _redis_client = client
        _redis_available = True
        log.info("[redis_cache] Connected to %s", _REDIS_URL)
        return client
    except Exception as e:
        _redis_available = False
        log.warning("[redis_cache] Redis unavailable (%s) — falling back to in-process cache", e)
        return None


# ── In-process fallback ────────────────────────────────────────────────────────
_local_cache: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)


def _local_get(key: str) -> Any:
    item = _local_cache.get(key)
    if item is None:
        return None
    value, exp = item
    if exp and time.time() > exp:
        del _local_cache[key]
        return None
    return value


def _local_set(key: str, value: Any, ttl: int) -> None:
    _local_cache[key] = (value, time.time() + ttl if ttl else 0)


def _local_delete(key: str) -> None:
    _local_cache.pop(key, None)


# ── Public API ─────────────────────────────────────────────────────────────────

def cache_get(key: str) -> Optional[Any]:
    """Lấy value từ Redis (hoặc fallback). Trả None nếu miss."""
    full_key = _KEY_PREFIX + key
    r = _get_redis()
    if r:
        try:
            raw = r.get(full_key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as e:
            log.debug("[redis_cache] get error %s: %s", key, e)
            return None
    return _local_get(full_key)


def cache_set(key: str, value: Any, ttl: int = 300) -> None:
    """Lưu value vào Redis (hoặc fallback). ttl tính bằng giây."""
    full_key = _KEY_PREFIX + key
    r = _get_redis()
    if r:
        try:
            r.setex(full_key, ttl, json.dumps(value, default=str))
            return
        except Exception as e:
            log.debug("[redis_cache] set error %s: %s", key, e)
    _local_set(full_key, value, ttl)


def cache_delete(key: str) -> None:
    """Xóa key khỏi cache."""
    full_key = _KEY_PREFIX + key
    r = _get_redis()
    if r:
        try:
            r.delete(full_key)
            return
        except Exception as e:
            log.debug("[redis_cache] delete error %s: %s", key, e)
    _local_delete(full_key)


def cache_delete_pattern(pattern: str) -> None:
    """Xóa tất cả keys khớp pattern (Redis SCAN, không dùng KEYS)."""
    full_pattern = _KEY_PREFIX + pattern
    r = _get_redis()
    if r:
        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=full_pattern, count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
            return
        except Exception as e:
            log.debug("[redis_cache] delete_pattern error %s: %s", pattern, e)
    # Fallback: clear matching local keys
    to_del = [k for k in _local_cache if k.startswith(_KEY_PREFIX + pattern.rstrip("*"))]
    for k in to_del:
        del _local_cache[k]


def cache_get_or_set(key: str, fn: Callable[[], Any], ttl: int = 300) -> Any:
    """
    Trả về cached value nếu có, không thì gọi fn() → lưu → trả về.
    Pattern chuẩn để cache kết quả của hàm nặng.

    Ví dụ:
        data = cache_get_or_set(
            f"ads_delay|{scope_key}",
            lambda: build_ads_delay_data(allowed_shop_keys),
            ttl=600,
        )
    """
    cached = cache_get(key)
    if cached is not None:
        return cached
    value = fn()
    if value is not None:
        cache_set(key, value, ttl)
    return value


def cache_stats() -> dict:
    """Trả về thống kê Redis để debug."""
    r = _get_redis()
    if r:
        try:
            info = r.info("memory")
            return {
                "backend": "redis",
                "used_memory_human": info.get("used_memory_human"),
                "connected_clients": r.info("clients").get("connected_clients"),
            }
        except Exception:
            pass
    return {
        "backend": "local_fallback",
        "local_keys": len(_local_cache),
    }
