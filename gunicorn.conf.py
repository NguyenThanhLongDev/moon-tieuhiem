"""Gunicorn config — chạy với --preload để tiết kiệm RAM (COW).

Master load app 1 lần, workers fork ra. Sau fork, RESET các connection pool
module-level vì psycopg2/redis pool KHÔNG fork-safe (TCP sockets share giữa
master + workers sẽ corrupt data).

Dùng:
  exec gunicorn --config gunicorn.conf.py --preload web_app:app
"""
import ctypes as _ctypes
import gc as _gc

try:
    _libc = _ctypes.CDLL("libc.so.6")
except Exception:
    _libc = None


def _malloc_trim() -> None:
    """Yêu cầu glibc trả các heap pages đã free về OS.
    Python tích luỹ RAM vì pymalloc giữ pages cho lần dùng sau.
    malloc_trim(0) ép OS reclaim ngay — giảm RSS worker đáng kể.
    """
    if _libc:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass


def post_fork(server, worker):
    """Sau khi fork, reset các pool/client lazy-init về None.

    Lần request đầu tiên trong worker sẽ tạo pool/client mới (fresh
    socket per worker → an toàn).
    """
    # 1) PostgreSQL ThreadedConnectionPool
    try:
        import db
        db._pool = None
    except Exception as exc:
        worker.log.warning("post_fork: reset db._pool failed: %s", exc)

    # 2) Redis client
    try:
        import redis_cache
        redis_cache._redis_client = None
        redis_cache._redis_available = None
    except Exception as exc:
        worker.log.warning("post_fork: reset redis_cache failed: %s", exc)

    worker.log.info("[post_fork] pid=%s reset db._pool + redis_client OK", worker.pid)


def post_request(worker, req, environ, resp):
    """Sau mỗi request, force glibc trả freed pages về OS.

    Không dùng gc.collect() ở đây (chậm, Python tự GC đủ).
    malloc_trim(0) chỉ trả pages đã free — không ảnh hưởng logic.
    Gọi mỗi request thay vì theo interval vì overhead rất thấp (~0.1ms).
    """
    _malloc_trim()


def when_ready(server):
    server.log.info("[when_ready] master pid=%s preload=ON workers=%s",
                    server.pid, server.cfg.workers)
