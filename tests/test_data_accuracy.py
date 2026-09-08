"""
Bộ kiểm tra độ chính xác số liệu — Posbot POS Dashboard
=========================================================
Chạy độc lập (không cần Flask app running):
    cd posbottieuhiem
    DATABASE_URL=... python3 -m pytest tests/test_data_accuracy.py -v

Hoặc với env file (VPS):
    set -a && source deploy/pos-dashboard.env && set +a
    python3 -m pytest tests/test_data_accuracy.py -v --tb=short

Có thể chạy ngầm (background):
    python3 -m pytest tests/test_data_accuracy.py -v --tb=short > /tmp/posbot_test.log 2>&1 &

Yêu cầu:
    pip install pytest psycopg2-binary requests

Cờ hữu ích:
    -v          verbose, hiện tên từng test
    --tb=short  traceback ngắn khi fail
    -x          dừng ngay khi test đầu tiên fail
    -k "kho"    chỉ chạy test có chữ "kho"
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ─── Path setup ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _wh_db():
    from modules.kho_vat_ly.wh_db import wh_db
    return wh_db


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 1: CẤU HÌNH & FILE JSON
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigFiles:
    """Kiểm tra các file cấu hình JSON cốt lõi."""

    def test_shops_json_exists(self):
        """shops.json phải tồn tại."""
        assert (BASE_DIR / "shops.json").exists(), "shops.json không tìm thấy"

    def test_shops_json_valid_format(self):
        """shops.json phải là array, mỗi item có shop_key + shop_id."""
        shops = _load_json(BASE_DIR / "shops.json")
        assert isinstance(shops, list), "shops.json phải là list"
        assert len(shops) > 0, "shops.json không có shop nào"
        for s in shops:
            assert "shop_key" in s, f"Shop thiếu shop_key: {s}"
            assert "shop_id" in s, f"Shop thiếu shop_id: {s}"

    def test_shops_json_has_active_shops(self):
        """Phải có ít nhất 1 shop active."""
        shops = _load_json(BASE_DIR / "shops.json")
        active = [s for s in shops if s.get("status") == "active"]
        assert len(active) >= 1, f"Không có shop active nào (tổng: {len(shops)})"

    def test_shops_json_no_duplicate_keys(self):
        """Không có shop_key trùng nhau."""
        shops = _load_json(BASE_DIR / "shops.json")
        keys = [s.get("shop_key") for s in shops if s.get("shop_key")]
        assert len(keys) == len(set(keys)), f"Có shop_key trùng: {[k for k in keys if keys.count(k) > 1]}"

    def test_shops_json_no_duplicate_ids(self):
        """Không có shop_id trùng nhau (với shop active)."""
        shops = _load_json(BASE_DIR / "shops.json")
        ids = [s.get("shop_id") for s in shops if s.get("status") == "active" and s.get("shop_id")]
        dupes = [i for i in ids if ids.count(i) > 1]
        assert len(dupes) == 0, f"Có shop_id trùng nhau: {list(set(dupes))}"

    def test_session_json_exists(self):
        """session.json phải tồn tại (chứa Pancake credentials)."""
        assert (BASE_DIR / "session.json").exists(), "session.json không tìm thấy"

    def test_session_json_has_credentials(self):
        """session.json phải có access_token và cookie không rỗng."""
        session = _load_json(BASE_DIR / "session.json")
        assert isinstance(session, dict), "session.json phải là dict"
        token = str(session.get("access_token", "") or "").strip()
        cookie = str(session.get("cookie", "") or "").strip()
        assert len(token) > 10, "access_token rỗng hoặc quá ngắn (< 10 ký tự)"
        assert len(cookie) > 10, "cookie rỗng hoặc quá ngắn (< 10 ký tự)"

    def test_users_json_exists(self):
        """users.json phải tồn tại."""
        assert (BASE_DIR / "users.json").exists(), "users.json không tìm thấy"

    def test_users_json_has_admin(self):
        """Phải có ít nhất 1 user với role admin hoặc superadmin."""
        users = _load_json(BASE_DIR / "users.json")
        if isinstance(users, list):
            admin_roles = {"admin", "superadmin"}
            admins = [u for u in users if u.get("role") in admin_roles]
            assert len(admins) >= 1, "Không có user admin/superadmin nào"
        elif isinstance(users, dict):
            admin_roles = {"admin", "superadmin"}
            admins = [k for k, v in users.items() if isinstance(v, dict) and v.get("role") in admin_roles]
            assert len(admins) >= 1, "Không có user admin/superadmin nào"

    def test_env_database_url(self):
        """DATABASE_URL phải được set trong environment."""
        db_url = os.environ.get("DATABASE_URL", "")
        assert db_url.strip(), (
            "DATABASE_URL chưa được set. "
            "Chạy: set -a && source deploy/pos-dashboard.env && set +a"
        )
        assert "postgresql" in db_url or "postgres" in db_url, (
            f"DATABASE_URL có vẻ sai format (phải là postgresql://...): {db_url[:30]}..."
        )


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 2: KẾT NỐI DATABASE
# ══════════════════════════════════════════════════════════════════════════════

class TestDatabaseConnectivity:
    """Kiểm tra kết nối và cấu trúc database."""

    def test_db_connection(self):
        """Kết nối PostgreSQL phải thành công."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            assert row is not None
            assert row["ok"] == 1

    def test_wh_tables_exist(self):
        """Tất cả bảng wh_* phải tồn tại."""
        required_tables = [
            "wh_outbound_requests",
            "wh_return_receipts",
            "wh_shop_inventory",
            "wh_stock_movements",
            "wh_inventory",
            "wh_products",
            "wh_shops",
        ]
        wh_db = _wh_db()
        with wh_db() as conn:
            for table in required_tables:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s",
                    [table],
                ).fetchone()
                assert row and int(row["c"]) == 1, f"Bảng '{table}' không tồn tại trong DB"

    def test_wh_outbound_columns(self):
        """wh_outbound_requests phải có đủ cột quan trọng."""
        required_cols = [
            "id", "order_code", "order_id_external", "shop_name",
            "product_sku", "qty_ordered", "pancake_status",
            "carrier_picked_up_at", "order_inserted_at",
        ]
        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='wh_outbound_requests'"
            ).fetchall()
            existing_cols = {r["column_name"] for r in rows}
            for col in required_cols:
                assert col in existing_cols, (
                    f"Cột '{col}' thiếu trong wh_outbound_requests. "
                    f"Cột hiện có: {sorted(existing_cols)}"
                )

    def test_wh_return_receipts_columns(self):
        """wh_return_receipts phải có đủ cột quan trọng."""
        required_cols = [
            "id", "order_code", "order_id_external", "shop_name",
            "product_sku", "qty_expected", "pancake_return_status",
            "status", "returned_at",
        ]
        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='wh_return_receipts'"
            ).fetchall()
            existing_cols = {r["column_name"] for r in rows}
            for col in required_cols:
                assert col in existing_cols, (
                    f"Cột '{col}' thiếu trong wh_return_receipts. "
                    f"Cột hiện có: {sorted(existing_cols)}"
                )

    def test_wh_shops_has_data(self):
        """wh_shops phải có ít nhất 1 shop (sau bootstrap)."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM wh_shops").fetchone()
            count = int(row["c"] or 0)
            assert count >= 1, (
                f"wh_shops rỗng ({count} records). "
                "Chạy: python3 scripts/bootstrap_wh_shops_from_shops_json.py"
            )

    def test_unique_index_wh_outbound(self):
        """Index unique (order_id_external, product_sku) phải tồn tại."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='wh_outbound_requests' AND indexname='uq_wh_ob_ext_sku'"
            ).fetchone()
            assert row is not None, (
                "Thiếu unique index 'uq_wh_ob_ext_sku' trên wh_outbound_requests. "
                "Chạy migration hoặc init_wh_tables()"
            )


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 3: SỐ LIỆU KHO VẬT LÝ
# ══════════════════════════════════════════════════════════════════════════════

class TestKhoVatLyData:
    """Kiểm tra tính chính xác số liệu kho vật lý."""

    VALID_STATUSES = {"waiting", "confirmed", "shipped", "received", "returned", "returning", "cancelled"}

    def _get_outbound_counts(self) -> Dict[str, int]:
        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT pancake_status, COUNT(DISTINCT order_code) AS c "
                "FROM wh_outbound_requests GROUP BY pancake_status"
            ).fetchall()
            return {r["pancake_status"]: int(r["c"] or 0) for r in rows}

    def test_outbound_has_data(self):
        """wh_outbound_requests phải có ít nhất 1000 đơn (hệ thống đã hoạt động)."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM wh_outbound_requests").fetchone()
            count = int(row["c"] or 0)
            assert count >= 1000, (
                f"wh_outbound_requests chỉ có {count} records — quá ít. "
                "Cần chạy sync kho outbound từ Pancake."
            )

    def test_outbound_statuses_are_valid(self):
        """Tất cả giá trị pancake_status phải nằm trong tập hợp hợp lệ."""
        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT pancake_status FROM wh_outbound_requests"
            ).fetchall()
            db_statuses = {r["pancake_status"] for r in rows if r["pancake_status"]}
            invalid = db_statuses - self.VALID_STATUSES
            assert not invalid, (
                f"Có pancake_status không hợp lệ: {invalid}. "
                f"Các status hợp lệ: {self.VALID_STATUSES}"
            )

    def test_outbound_no_negative_qty(self):
        """qty_ordered không được âm."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests WHERE qty_ordered < 0"
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, f"{count} dòng có qty_ordered < 0"

    def test_outbound_shipped_count_reasonable(self):
        """Số đơn 'shipped' (đang giao) phải > 0 và < 50000 (thực tế)."""
        counts = self._get_outbound_counts()
        shipped = counts.get("shipped", 0)
        assert shipped > 0, (
            "Không có đơn 'shipped' nào. "
            "Kiểm tra sync kho outbound có chạy chưa."
        )
        assert shipped < 50000, (
            f"Số đơn 'shipped' = {shipped} — cao bất thường (> 50,000). "
            "Kiểm tra có bị duplicate không."
        )

    def test_outbound_received_greater_than_shipped(self):
        """Số đơn 'received' (đã nhận) nên >= shipped vì tích lũy từ đầu."""
        counts = self._get_outbound_counts()
        shipped = counts.get("shipped", 0)
        received = counts.get("received", 0)
        assert received >= shipped, (
            f"received={received} < shipped={shipped} — bất thường. "
            "Hệ thống tích lũy nên received luôn >= shipped."
        )

    def test_outbound_no_duplicate_order_sku(self):
        """Không có (order_id_external, product_sku) trùng nhau (unique constraint)."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT order_id_external, product_sku, COUNT(*) AS cnt
                    FROM wh_outbound_requests
                    WHERE order_id_external != ''
                    GROUP BY order_id_external, product_sku
                    HAVING COUNT(*) > 1
                ) dupes
                """
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, (
                f"Có {count} cặp (order_id_external, product_sku) bị duplicate. "
                "Cần kiểm tra unique index và dedup query."
            )

    def test_outbound_all_have_order_code(self):
        """Tất cả dòng phải có order_code không rỗng."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests "
                "WHERE order_code IS NULL OR order_code = ''"
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, f"{count} dòng thiếu order_code"

    def test_outbound_all_have_shop_name(self):
        """Tất cả dòng phải có shop_name (trừ các dòng cũ có thể để trống)."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests "
                "WHERE shop_name IS NULL OR shop_name = ''"
            ).fetchone()
            count = int(row["c"] or 0)
            # Cho phép <= 1% records thiếu shop_name (dữ liệu cũ)
            total_row = conn.execute("SELECT COUNT(*) AS c FROM wh_outbound_requests").fetchone()
            total = int(total_row["c"] or 1)
            pct = count / total * 100
            assert pct <= 1.0, (
                f"{count}/{total} dòng ({pct:.1f}%) thiếu shop_name — vượt ngưỡng 1%"
            )

    def test_get_kho_shipping_stats_function(self):
        """Hàm get_kho_shipping_stats() phải trả về dict với các key đúng."""
        from web_app import get_kho_shipping_stats
        result = get_kho_shipping_stats(allowed_shop_keys=None)
        assert isinstance(result, dict), f"Kết quả phải là dict, nhận: {type(result)}"
        assert "shipped" in result, f"Thiếu key 'shipped' trong kết quả: {result}"
        assert "confirmed" in result, f"Thiếu key 'confirmed' trong kết quả: {result}"
        assert result["shipped"] >= 0, f"shipped < 0: {result['shipped']}"
        assert result["confirmed"] >= 0, f"confirmed < 0: {result['confirmed']}"

    def test_kho_stats_consistent_with_db(self):
        """Kết quả get_kho_shipping_stats() phải khớp với query DB trực tiếp."""
        from web_app import get_kho_shipping_stats
        stats = get_kho_shipping_stats(allowed_shop_keys=None)

        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT order_code) AS c FROM wh_outbound_requests "
                "WHERE pancake_status = 'shipped'"
            ).fetchone()
            db_shipped = int(row["c"] or 0)

        assert stats["shipped"] == db_shipped, (
            f"get_kho_shipping_stats()['shipped']={stats['shipped']} "
            f"!= DB count={db_shipped}. Có thể đang bị cache sai."
        )

    def test_outbound_shops_match_active_shops(self):
        """Shop names trong wh_outbound_requests phải là subset của active shops."""
        shops_data = _load_json(BASE_DIR / "shops.json")
        active_shop_names = {
            s.get("shop_name", "").strip()
            for s in shops_data
            if s.get("status") == "active" and s.get("shop_name")
        }

        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT shop_name FROM wh_outbound_requests "
                "WHERE shop_name IS NOT NULL AND shop_name != ''"
            ).fetchall()
            db_shop_names = {r["shop_name"] for r in rows}

        unknown = db_shop_names - active_shop_names
        if unknown:
            # Warning chứ không fail — có thể có shop đã inactive nhưng vẫn còn đơn cũ
            print(f"\n[WARN] Shop trong DB không có trong active shops.json: {unknown}")

    def test_outbound_recent_data_exists(self):
        """Phải có đơn xuất được insert trong 7 ngày gần nhất."""
        cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests "
                "WHERE order_inserted_at >= %s",
                [cutoff],
            ).fetchone()
            count = int(row["c"] or 0)
            assert count > 0, (
                f"Không có đơn nào được insert trong 7 ngày qua (từ {cutoff}). "
                "Kiểm tra scheduler sync_kho_outbound có chạy không."
            )


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 4: SỐ LIỆU ĐƠN TRẢ HÀNG
# ══════════════════════════════════════════════════════════════════════════════

class TestReturnData:
    """Kiểm tra tính chính xác số liệu đơn trả hàng."""

    def test_returns_has_data(self):
        """wh_return_receipts phải có data."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM wh_return_receipts").fetchone()
            count = int(row["c"] or 0)
            assert count > 0, "wh_return_receipts rỗng — kiểm tra sync returns"

    def test_returns_no_negative_qty(self):
        """qty_expected không được âm."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_return_receipts WHERE qty_expected < 0"
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, f"{count} dòng có qty_expected < 0 trong wh_return_receipts"

    def test_returns_no_duplicate_order_sku(self):
        """Không có (order_id_external, product_sku) trùng trong returns."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT order_id_external, product_sku, COUNT(*) AS cnt
                    FROM wh_return_receipts
                    WHERE order_id_external != ''
                    GROUP BY order_id_external, product_sku
                    HAVING COUNT(*) > 1
                ) dupes
                """
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, f"{count} cặp duplicate trong wh_return_receipts"

    def test_returns_pancake_status_values(self):
        """pancake_return_status phải là NULL hoặc integer 0-6."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_return_receipts "
                "WHERE pancake_return_status IS NOT NULL "
                "AND (pancake_return_status < 0 OR pancake_return_status > 6)"
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, f"{count} dòng có pancake_return_status ngoài phạm vi [0,6]"


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 5: SCHEDULER & JOBS
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerConfig:
    """Kiểm tra cấu hình scheduler jobs."""

    def _get_scheduler_source(self) -> str:
        with open(BASE_DIR / "scheduler.py", encoding="utf-8") as f:
            return f.read()

    def test_scheduler_py_exists(self):
        assert (BASE_DIR / "scheduler.py").exists(), "scheduler.py không tồn tại"

    def test_sync_kho_outbound_job_registered(self):
        """Job sync_kho_outbound phải được đăng ký trong start_scheduler()."""
        src = self._get_scheduler_source()
        assert "sync_kho_outbound" in src, "Không tìm thấy sync_kho_outbound trong scheduler.py"
        assert "job_sync_kho_outbound" in src

    def test_sync_kho_outbound_runs_inprocess(self):
        """job_sync_kho_outbound phải gọi sync_outbound_for_date_range (in-process)."""
        src = self._get_scheduler_source()
        assert "sync_outbound_for_date_range" in src, (
            "job_sync_kho_outbound không gọi sync_outbound_for_date_range in-process. "
            "Cần sửa để chạy in-process thay vì subprocess."
        )

    def test_sync_kho_outbound_interval_max_30min(self):
        """Interval sync kho outbound phải <= 30 phút."""
        src = self._get_scheduler_source()
        # Tìm dòng CronTrigger gần sync_kho_outbound
        lines = src.splitlines()
        in_block = False
        for i, line in enumerate(lines):
            if "sync_kho_outbound" in line and "job_sync_kho_outbound" not in line:
                continue
            if "id=\"sync_kho_outbound\"" in line or "id='sync_kho_outbound'" in line:
                # Tìm CronTrigger trong 10 dòng trước đó
                block = "\n".join(lines[max(0, i-10):i+3])
                # */30 = mỗi 30p, */15 = mỗi 15p, */10 = mỗi 10p
                is_frequent = any(
                    f"*/{m}" in block for m in ["10", "15", "20", "30"]
                ) or ("minute" in block and "hour" not in block.lower().split("minute")[1][:50])
                assert is_frequent or "*/3" not in block, (
                    "sync_kho_outbound vẫn đang chạy mỗi 3 giờ — cần đổi sang <= 30 phút"
                )
                break

    def test_python_for_subjobs_function_exists(self):
        """Hàm _python_for_subjobs() phải tồn tại để chọn đúng Python interpreter."""
        src = self._get_scheduler_source()
        assert "_python_for_subjobs" in src, (
            "_python_for_subjobs() không tồn tại trong scheduler.py. "
            "Cần thêm hàm này để chọn đúng venv Python trên VPS."
        )

    def test_scheduler_import_ok(self):
        """scheduler.py phải import được không lỗi."""
        import importlib
        spec = importlib.util.spec_from_file_location("scheduler", BASE_DIR / "scheduler.py")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            pytest.fail(f"Import scheduler.py lỗi: {e}")
        assert hasattr(mod, "start_scheduler"), "start_scheduler() không tìm thấy trong scheduler.py"
        assert hasattr(mod, "job_sync_kho_outbound"), "job_sync_kho_outbound() không tìm thấy"


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 6: SYNC FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncFunctions:
    """Kiểm tra hàm sync từ Pancake API."""

    def test_sync_outbound_import(self):
        """sync_outbound_for_date_range phải import được."""
        try:
            from modules.kho_vat_ly.wh_sync_orders import sync_outbound_for_date_range
        except ImportError as e:
            pytest.fail(f"Không import được sync_outbound_for_date_range: {e}")

    def test_sync_outbound_lock_available(self):
        """SYNC_LOCK phải ở trạng thái unlocked khi không có sync đang chạy."""
        from modules.kho_vat_ly.wh_db import SYNC_LOCK
        locked = not SYNC_LOCK.acquire(blocking=False)
        if not locked:
            SYNC_LOCK.release()
        assert not locked, (
            "SYNC_LOCK đang bị giữ — có sync đang chạy hoặc bị deadlock. "
            "Restart app để giải phóng."
        )

    def test_sync_wh_returns_import(self):
        """sync_returns_for_date_range phải import được."""
        try:
            from modules.kho_vat_ly.wh_sync_returns import sync_returns_for_date_range
        except ImportError as e:
            pytest.fail(f"Không import được sync_returns_for_date_range: {e}")

    def test_fetch_configs_returns_credentials(self):
        """fetch_configs() phải trả về access_token và cookie hợp lệ."""
        try:
            from modules.kho_vat_ly.wh_sync_orders import fetch_configs
            config = fetch_configs()
            token = str(config.get("access_token", "") or "").strip()
            cookie = str(config.get("cookie", "") or "").strip()
            assert len(token) > 10, f"access_token quá ngắn: '{token[:20]}'"
            assert len(cookie) > 10, f"cookie quá ngắn: '{cookie[:20]}'"
        except Exception as e:
            pytest.fail(f"fetch_configs() lỗi: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 7: CONSISTENCY CHECKS
# ══════════════════════════════════════════════════════════════════════════════

class TestDataConsistency:
    """Kiểm tra tính nhất quán dữ liệu giữa các bảng."""

    def test_wh_shops_vs_shops_json(self):
        """Số shop active trong DB phải khớp ± 10% với shops.json."""
        shops_data = _load_json(BASE_DIR / "shops.json")
        active_count_json = len([s for s in shops_data if s.get("status") == "active"])

        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_shops WHERE status='active'"
            ).fetchone()
            active_count_db = int(row["c"] or 0)

        diff_pct = abs(active_count_db - active_count_json) / max(active_count_json, 1) * 100
        assert diff_pct <= 10, (
            f"Số shop active: DB={active_count_db} vs shops.json={active_count_json} "
            f"(chênh {diff_pct:.0f}%). Cần chạy bootstrap_wh_shops_from_shops_json.py"
        )

    def test_outbound_total_equals_sum_of_statuses(self):
        """Tổng COUNT(*) phải bằng tổng COUNT theo từng status."""
        wh_db = _wh_db()
        with wh_db() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests"
            ).fetchone()
            total = int(total_row["c"] or 0)

            status_rows = conn.execute(
                "SELECT pancake_status, COUNT(*) AS c FROM wh_outbound_requests GROUP BY pancake_status"
            ).fetchall()
            sum_by_status = sum(int(r["c"] or 0) for r in status_rows)

        assert total == sum_by_status, (
            f"Tổng rows={total} != tổng theo status={sum_by_status}. "
            "Có thể có NULL pancake_status."
        )

    def test_no_null_pancake_status(self):
        """pancake_status không được NULL trong wh_outbound_requests."""
        wh_db = _wh_db()
        with wh_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests WHERE pancake_status IS NULL"
            ).fetchone()
            count = int(row["c"] or 0)
            assert count == 0, (
                f"{count} dòng có pancake_status = NULL. "
                "Cần UPDATE wh_outbound_requests SET pancake_status='shipped' WHERE pancake_status IS NULL"
            )

    def test_returns_total_vs_outbound_sanity(self):
        """Số đơn returns phải < tổng outbound (không thể hoàn nhiều hơn xuất)."""
        wh_db = _wh_db()
        with wh_db() as conn:
            outbound_row = conn.execute(
                "SELECT COUNT(DISTINCT order_code) AS c FROM wh_outbound_requests"
            ).fetchone()
            returns_row = conn.execute(
                "SELECT COUNT(DISTINCT order_code) AS c FROM wh_return_receipts"
            ).fetchone()

        outbound_total = int(outbound_row["c"] or 0)
        returns_total = int(returns_row["c"] or 0)

        assert returns_total <= outbound_total, (
            f"Số đơn returns ({returns_total}) > outbound ({outbound_total}) — bất thường"
        )

    def test_query_performance_kho_stats(self):
        """Query get_kho_shipping_stats phải xong trong < 5 giây."""
        wh_db = _wh_db()
        start = time.time()
        with wh_db() as conn:
            conn.execute(
                "SELECT pancake_status, COUNT(DISTINCT order_code) AS c "
                "FROM wh_outbound_requests GROUP BY pancake_status"
            ).fetchall()
        elapsed = time.time() - start
        assert elapsed < 5.0, (
            f"Query kho stats mất {elapsed:.2f}s — quá chậm (> 5s). "
            "Kiểm tra index trên pancake_status."
        )


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 8: MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestMigrations:
    """Kiểm tra migration files và trạng thái schema."""

    def test_migration_files_sequential(self):
        """Migration files phải có số thứ tự liên tục, không bị gap."""
        migrations_dir = BASE_DIR / "migrations"
        if not migrations_dir.exists():
            pytest.skip("Không có thư mục migrations/")

        sql_files = sorted([
            f.name for f in migrations_dir.glob("*.sql")
            if not f.name.endswith("_rollback.sql")
            and not f.name.startswith("salary_")
            and not f.name.startswith("README")
        ])
        nums = []
        for f in sql_files:
            try:
                num = int(f.split("_")[0])
                nums.append(num)
            except (ValueError, IndexError):
                continue
        nums.sort()
        if len(nums) < 2:
            return
        gaps = [nums[i+1] - nums[i] for i in range(len(nums)-1) if nums[i+1] - nums[i] > 1]
        assert not gaps, (
            f"Migration files có gap ở số: "
            f"{[nums[i] for i in range(len(nums)-1) if nums[i+1]-nums[i] > 1]}"
        )

    def test_main_tables_exist(self):
        """Các bảng chính của app (không phải wh_) phải tồn tại."""
        expected = ["orders", "order_items", "daily_shop_metrics", "facebook_ads_summary"]
        wh_db = _wh_db()
        with wh_db() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
            existing = {r["table_name"] for r in rows}

        missing = [t for t in expected if t not in existing]
        if missing:
            # Một số bảng có thể chưa có nếu module chưa được cài đặt — warning thôi
            print(f"\n[WARN] Các bảng chưa tồn tại: {missing}")


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 9: THREAD SAFETY
# ══════════════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    """Kiểm tra SYNC_LOCK hoạt động đúng khi có concurrent sync."""

    def test_sync_lock_prevents_concurrent_sync(self):
        """SYNC_LOCK phải chặn thread thứ 2 acquire ngay lập tức."""
        from modules.kho_vat_ly.wh_db import SYNC_LOCK

        results = []

        def try_acquire():
            acquired = SYNC_LOCK.acquire(blocking=False)
            results.append(acquired)
            if acquired:
                time.sleep(0.1)
                SYNC_LOCK.release()

        # Thread 1 acquire trước
        SYNC_LOCK.acquire()
        try:
            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=2)
            assert results == [False], (
                f"Thread 2 acquire được lock dù thread 1 đang giữ. "
                f"results={results}"
            )
        finally:
            SYNC_LOCK.release()


# ══════════════════════════════════════════════════════════════════════════════
# NHÓM 10: SMOKE TEST (nhanh, gọi API nội bộ)
# ══════════════════════════════════════════════════════════════════════════════

class TestSmokeTests:
    """Smoke tests nhẹ — kiểm tra import và basic function calls."""

    def test_import_web_app_functions(self):
        """Các hàm core của web_app.py phải import được."""
        try:
            from web_app import (
                get_kho_shipping_stats,
                load_shop_meta_map,
                load_config,
            )
        except ImportError as e:
            pytest.fail(f"Import web_app functions lỗi: {e}")

    def test_load_shop_meta_map_returns_data(self):
        """load_shop_meta_map() phải trả về dict không rỗng."""
        from web_app import load_shop_meta_map
        meta = load_shop_meta_map()
        assert isinstance(meta, dict), f"Kết quả phải là dict: {type(meta)}"
        assert len(meta) > 0, "load_shop_meta_map() trả về dict rỗng"

    def test_load_config_returns_dict(self):
        """load_config() phải trả về dict."""
        from web_app import load_config
        config = load_config()
        assert isinstance(config, dict), f"load_config() trả về {type(config)}, cần dict"

    def test_perm_utils_import(self):
        """perm_utils phải import được."""
        try:
            import perm_utils
            assert hasattr(perm_utils, "get_allowed_shop_keys"), (
                "perm_utils thiếu get_allowed_shop_keys()"
            )
        except ImportError as e:
            pytest.fail(f"Import perm_utils lỗi: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT (chạy trực tiếp không qua pytest)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=str(BASE_DIR),
    )
    sys.exit(result.returncode)
