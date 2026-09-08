from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Generator, Iterable, Optional


def _require_db_url() -> str:
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL is required for DB phase-1 operations.")
    return db_url


# ─────────────────────────────────────────
# CONNECTION POOL (psycopg2)
# ─────────────────────────────────────────

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            import psycopg2.pool  # type: ignore
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "Thiếu driver PostgreSQL: cần gói `psycopg2-binary`.\n"
                "  Cài: pip install psycopg2-binary"
            ) from e

        db_url = _require_db_url()
        # minconn=2: giữ sẵn 2 kết nối; maxconn=10: tối đa 10 kết nối song song
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, db_url)
        return _pool


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # P0-8 phòng ngừa: defensive rollback trước putconn — đảm bảo không trả về
        # pool 1 connection đang "idle in transaction". Bình thường commit/rollback
        # đã xử lý, nhưng safety net này bảo vệ khi yield được break ngầm.
        try:
            if not conn.closed and conn.get_transaction_status() != 0:  # 0 = TRANSACTION_STATUS_IDLE
                conn.rollback()
        except Exception:
            pass
        pool.putconn(conn)


def execute_sql(sql: str, params: Optional[Iterable[Any]] = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))


def query_one(sql: str, params: Optional[Iterable[Any]] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return cur.fetchone()


def query_all(sql: str, params: Optional[Iterable[Any]] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return cur.fetchall()
