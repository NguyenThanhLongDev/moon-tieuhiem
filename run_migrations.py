"""
Chạy tất cả SQL migrations theo thứ tự trên database mới.

Dùng khi:
  - Triển khai lên máy chủ mới
  - Database trống chưa có bảng

Cách dùng:
  python3 run_migrations.py

Yêu cầu:
  - DATABASE_URL phải được set trong môi trường
  - pip install psycopg2-binary
"""
from __future__ import annotations

import os
import sys
import re
from pathlib import Path

import psycopg2

BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BASE_DIR / "migrations"


def get_conn():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL không được set.", file=sys.stderr)
        print("  Chạy: export DATABASE_URL='postgresql://user:pass@host:5432/dbname'", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(db_url)


def ensure_migrations_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id          SERIAL PRIMARY KEY,
            filename    TEXT NOT NULL UNIQUE,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def get_applied(cur) -> set[str]:
    cur.execute("SELECT filename FROM _migrations")
    return {row[0] for row in cur.fetchall()}


def get_migration_files() -> list[Path]:
    files = sorted(
        f for f in MIGRATIONS_DIR.glob("*.sql")
        if not f.name.endswith("_rollback.sql")
        and re.match(r"^\d+_", f.name)
    )
    return files


def run_migrations():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    ensure_migrations_table(cur)
    conn.commit()

    applied = get_applied(cur)
    files = get_migration_files()

    if not files:
        print("Không tìm thấy file migration nào trong thư mục migrations/")
        return

    new_count = 0
    for path in files:
        if path.name in applied:
            print(f"  [SKIP] {path.name}")
            continue

        print(f"  [RUN]  {path.name} ...", end=" ", flush=True)
        sql = path.read_text(encoding="utf-8")
        try:
            cur.execute(sql)
            cur.execute("INSERT INTO _migrations (filename) VALUES (%s)", (path.name,))
            conn.commit()
            print("OK")
            new_count += 1
        except psycopg2.errors.DuplicateTable as e:
            conn.rollback()
            cur.execute("INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (path.name,))
            conn.commit()
            print(f"ALREADY EXISTS (marked done)")
        except psycopg2.errors.DuplicateObject as e:
            conn.rollback()
            cur.execute("INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (path.name,))
            conn.commit()
            print(f"ALREADY EXISTS (marked done)")
        except Exception as e:
            conn.rollback()
            err = str(e).strip()
            if any(phrase in err.lower() for phrase in ["already exists", "duplicate", "does not exist"]):
                cur.execute("INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (path.name,))
                conn.commit()
                print(f"SKIPPED (schema mismatch, marked done)")
            else:
                print(f"FAILED\n       {e}", file=sys.stderr)
                sys.exit(1)

    if new_count == 0:
        print("Tất cả migrations đã được apply trước đó. Không có gì mới.")
    else:
        print(f"\nHoàn tất: {new_count} migration(s) mới applied.")

    cur.close()
    conn.close()


def _bootstrap_kho_vat_ly_background() -> None:
    """Chạy trong thread nền: sync POS inventory + outbound orders cho Kho vật lý."""
    import logging
    import subprocess
    log = logging.getLogger("auto_bootstrap_kho")
    env = os.environ.copy()
    base = str(BASE_DIR)

    log.info("[auto_bootstrap_kho] Bắt đầu bootstrap_kho_vat_ly_data.py...")
    try:
        r = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "bootstrap_kho_vat_ly_data.py")],
            cwd=base, env=env, capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0:
            log.info("[auto_bootstrap_kho] bootstrap_kho_vat_ly_data OK: %s", r.stdout.strip()[-200:])
        else:
            log.warning("[auto_bootstrap_kho] bootstrap_kho_vat_ly_data WARN: %s", r.stderr[-300:])
    except Exception as e:
        log.warning("[auto_bootstrap_kho] ERROR: %s", e)

    log.info("[auto_bootstrap_kho] Hoàn tất bootstrap Kho vật lý.")


def _bootstrap_returns_background() -> None:
    """Chạy trong thread nền: sync order_status_cache + wh_return_receipts."""
    import logging
    import subprocess
    log = logging.getLogger("auto_bootstrap_returns")
    env = os.environ.copy()
    base = str(BASE_DIR)

    log.info("[auto_bootstrap_returns] Bắt đầu sync order_status_cache...")
    try:
        import datetime
        today = datetime.date.today().isoformat()
        r = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "sync_order_status_cache.py"),
             "--date", today, "--days", "7"],
            cwd=base, env=env, capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0:
            log.info("[auto_bootstrap_returns] sync_order_status_cache OK")
        else:
            log.warning("[auto_bootstrap_returns] sync_order_status_cache WARN: %s", r.stderr[-300:])
    except Exception as e:
        log.warning("[auto_bootstrap_returns] sync_order_status_cache ERROR: %s", e)

    log.info("[auto_bootstrap_returns] Bắt đầu sync + backfill wh_return_receipts...")
    try:
        r = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "sync_wh_returns_pg.py")],
            cwd=base, env=env, capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0:
            log.info("[auto_bootstrap_returns] sync_wh_returns_pg OK")
        else:
            log.warning("[auto_bootstrap_returns] sync_wh_returns_pg WARN: %s", r.stderr[-300:])
    except Exception as e:
        log.warning("[auto_bootstrap_returns] sync_wh_returns_pg ERROR: %s", e)

    log.info("[auto_bootstrap_returns] Hoàn tất bootstrap returns.")


def auto_bootstrap_if_empty() -> None:
    """
    Nếu DB vừa được tạo mới (shops trống / daily_shop_metrics trống /
    wh_return_receipts trống), tự động chạy bootstrap từ JSON files.
    Gọi sau auto_migrate().
    """
    import logging
    import subprocess
    import threading
    log = logging.getLogger("auto_bootstrap")
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        return
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM shops")
        shop_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM daily_shop_metrics")
        metrics_count = cur.fetchone()[0]

        # wh_shops — bảng riêng cho module Kho vật lý (không dùng bảng admin shops)
        wh_shops_count = 0
        try:
            cur.execute("SELECT COUNT(*) FROM wh_shops")
            wh_shops_count = cur.fetchone()[0]
        except Exception:
            wh_shops_count = 0

        # ── Kiểm tra wh_return_receipts ──────────────────────────────────────────
        returns_count = 0
        try:
            cur.execute("SELECT COUNT(*) FROM wh_return_receipts")
            returns_count = cur.fetchone()[0]
        except Exception:
            returns_count = 0  # bảng chưa tồn tại

        # ── Kiểm tra shop_order_status_cache.total_returned_all (migration 024) ─
        # Triggers khi: bảng có rows nhưng total_returned_all toàn NULL
        # (migration 024 mới được apply trên server cũ đã có dữ liệu)
        cache_needs_sync = False
        try:
            cur.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN total_returned_all IS NULL THEN 1 ELSE 0 END) as nulls "
                "FROM shop_order_status_cache"
            )
            row = cur.fetchone()
            total_cache, null_cache = (row[0] or 0), (row[1] or 0)
            if total_cache > 0 and null_cache == total_cache:
                cache_needs_sync = True
        except Exception:
            pass  # bảng chưa tồn tại hoặc cột chưa có → bỏ qua

        cur.close()
        conn.close()

        if shop_count == 0:
            log.info("[auto_bootstrap] shops trống → chạy bootstrap_core_admin_from_json.py")
            result = subprocess.run(
                [sys.executable, str(BASE_DIR / "scripts" / "bootstrap_core_admin_from_json.py")],
                cwd=str(BASE_DIR), env=os.environ.copy(),
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                log.info("[auto_bootstrap] bootstrap_core_admin OK")
            else:
                log.warning("[auto_bootstrap] bootstrap_core_admin WARN: %s", result.stderr[-500:])

        if metrics_count == 0:
            log.info("[auto_bootstrap] daily_shop_metrics trống → chạy bootstrap_daily_shop_metrics_from_json.py")
            result = subprocess.run(
                [sys.executable, str(BASE_DIR / "scripts" / "bootstrap_daily_shop_metrics_from_json.py")],
                cwd=str(BASE_DIR), env=os.environ.copy(),
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                log.info("[auto_bootstrap] bootstrap_daily_shop_metrics OK")
            else:
                log.warning("[auto_bootstrap] bootstrap_daily_shop_metrics WARN: %s", result.stderr[-500:])

        # wh_shops: luôn upsert từ shops.json để đảm bảo pos_api_key luôn đúng
        # (ON CONFLICT DO UPDATE — an toàn khi chạy nhiều lần)
        log.info("[auto_bootstrap] upsert wh_shops từ shops.json (%d rows hiện tại)...", wh_shops_count)
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "bootstrap_wh_shops_from_shops_json.py")],
            cwd=str(BASE_DIR), env=os.environ.copy(),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.info("[auto_bootstrap] bootstrap_wh_shops OK: %s", result.stdout.strip())
        else:
            log.warning("[auto_bootstrap] bootstrap_wh_shops WARN: %s", result.stderr[-500:])

        # wh_outbound_requests: nếu trống → bootstrap POS inventory + outbound trong nền
        outbound_count = 0
        try:
            conn2 = psycopg2.connect(db_url)
            cur2 = conn2.cursor()
            cur2.execute("SELECT COUNT(*) FROM wh_outbound_requests")
            outbound_count = cur2.fetchone()[0]
            cur2.close()
            conn2.close()
        except Exception:
            outbound_count = 0

        if outbound_count == 0:
            log.info("[auto_bootstrap] wh_outbound_requests trống → bootstrap Kho vật lý trong nền...")
            t_kho = threading.Thread(target=_bootstrap_kho_vat_ly_background, daemon=True, name="bootstrap_kho")
            t_kho.start()
            log.info("[auto_bootstrap] Thread bootstrap_kho đã khởi động (sync POS inventory + 30 ngày outbound).")
        else:
            log.info("[auto_bootstrap] wh_outbound_requests OK (%d rows) → bỏ qua bootstrap kho.", outbound_count)

        # Trigger sync returns nếu:
        #   (A) wh_return_receipts rỗng → server mới cài lần đầu
        #   (B) total_returned_all toàn NULL → migration 024 vừa apply trên server cũ
        need_returns_sync = (returns_count == 0) or cache_needs_sync
        if need_returns_sync:
            reason = "wh_return_receipts rỗng" if returns_count == 0 else "total_returned_all chưa có (migration 024 mới apply)"
            log.info("[auto_bootstrap] %s → chạy sync returns trong nền...", reason)
            t = threading.Thread(target=_bootstrap_returns_background, daemon=True, name="bootstrap_returns")
            t.start()
            log.info("[auto_bootstrap] Thread bootstrap_returns đã khởi động (chạy nền ~5-10 phút).")
        else:
            log.info("[auto_bootstrap] wh_return_receipts OK (%d rows), total_returned_all OK → bỏ qua bootstrap returns.", returns_count)

    except Exception as exc:
        log.error("[auto_bootstrap] Lỗi: %s", exc)


def auto_migrate() -> None:
    """Chạy migrations tự động khi app khởi động — không raise exception."""
    import logging
    log = logging.getLogger("auto_migrate")
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        log.warning("[auto_migrate] DATABASE_URL chưa set — bỏ qua migrations")
        return
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
        ensure_migrations_table(cur)
        conn.commit()
        applied = get_applied(cur)
        files = get_migration_files()
        new_count = 0
        for path in files:
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                cur.execute(sql)
                cur.execute("INSERT INTO _migrations (filename) VALUES (%s)", (path.name,))
                conn.commit()
                log.info("[auto_migrate] OK: %s", path.name)
                new_count += 1
            except Exception as e:
                conn.rollback()
                err = str(e).strip()
                cur.execute("INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (path.name,))
                conn.commit()
                if any(p in err.lower() for p in ["already exists", "duplicate"]):
                    log.info("[auto_migrate] SKIP (exists): %s", path.name)
                else:
                    log.warning("[auto_migrate] WARN %s: %s", path.name, err)
        if new_count:
            log.info("[auto_migrate] Hoàn tất: %d migration(s) mới.", new_count)
        cur.close()
        conn.close()
    except Exception as exc:
        log.error("[auto_migrate] Không thể chạy migrations: %s", exc)


if __name__ == "__main__":
    print(f"Migrations directory: {MIGRATIONS_DIR}")
    print(f"Database: {os.getenv('DATABASE_URL','')[:40]}...")
    print()
    run_migrations()
