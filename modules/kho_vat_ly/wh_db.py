"""
Database wrapper cho module Kho Vật Lý — dùng PostgreSQL pool của Posbot Web App.
Cung cấp interface giống sqlite3 (execute/fetchone/fetchall/row['col']) để
routes và sync modules không phải viết lại từ đầu.
"""
from __future__ import annotations

import re
import os
import threading
from contextlib import contextmanager
from typing import Any, Sequence

# Lock cho auto-sync outbound (chạy nền 15 phút/lần)
SYNC_LOCK = threading.Lock()
# Lock riêng cho manual outbound sync — không bị block bởi auto-sync
OUTBOUND_MANUAL_SYNC_LOCK = threading.Lock()
# Lock riêng cho fast shipped-only sync (2 phút) — không bị block bởi full sync
FAST_SHIPPED_SYNC_LOCK = threading.Lock()
# Lock riêng cho fast-new-orders (status 9 only) — độc lập với fast-shipped
FAST_NEW_ORDERS_SYNC_LOCK = threading.Lock()
# Lock riêng cho returns sync — hoàn toàn độc lập với outbound
RETURNS_SYNC_LOCK = threading.Lock()
# Lock riêng cho fast returns sync (2 phút) — không bị block bởi full returns sync
FAST_RETURNS_SYNC_LOCK = threading.Lock()

import psycopg2.extras  # type: ignore

from db import get_conn  # posbottieuhiem's ThreadedConnectionPool


# ─────────────────────────────────────────
# SQL ADAPTER: SQLite syntax → PostgreSQL
# ─────────────────────────────────────────

_TABLE_MAP = [
    ("return_receipts",   "wh_return_receipts"),
    ("outbound_requests", "wh_outbound_requests"),
    ("shop_inventory",    "wh_shop_inventory"),
    ("stock_movements",   "wh_stock_movements"),
    ("inventory",         "wh_inventory"),
    ("products",          "wh_products"),
    ("warehouses",        "wh_warehouses"),
    ("phanbo",            "wh_phanbo"),
    ("shops",             "wh_shops"),
]
_TABLE_RE = [(re.compile(rf"\b{src}\b"), dst) for src, dst in _TABLE_MAP]


def _adapt(sql: str) -> str:
    sql = sql.replace("?", "%s")
    for pattern, replacement in _TABLE_RE:
        sql = pattern.sub(replacement, sql)
    return sql


# ─────────────────────────────────────────
# ROW WRAPPER — hỗ trợ row["col"] và row.col
# ─────────────────────────────────────────

class _Row(dict):
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            return None


class _Cursor:
    def __init__(self, pg_cur):
        self._cur = pg_cur

    def fetchone(self):
        row = self._cur.fetchone()
        return _Row(row) if row else None

    def fetchall(self):
        return [_Row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for row in self._cur:
            yield _Row(row)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


# ─────────────────────────────────────────
# CONNECTION WRAPPER
# ─────────────────────────────────────────

class _Conn:
    """Wraps psycopg2 connection với giao diện giống sqlite3."""

    def __init__(self, pg_conn):
        self._pg = pg_conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        adapted = _adapt(sql)
        cur = self._pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(adapted, params if params else None)
        return _Cursor(cur)

    def executemany(self, sql: str, params_list) -> None:
        adapted = _adapt(sql)
        cur = self._pg.cursor()
        cur.executemany(adapted, params_list)


@contextmanager
def wh_db():
    """Context manager cho warehouse DB — auto commit/rollback qua pool."""
    with get_conn() as pg_conn:
        yield _Conn(pg_conn)


# ─────────────────────────────────────────
# KHỞI TẠO BẢNG POSTGRESQL
# ─────────────────────────────────────────

def init_wh_tables():
    """Tạo bảng warehouse trong PostgreSQL nếu chưa có.
    Tự retry tối đa 5 lần nếu gặp deadlock với pos-scheduler đang chạy.
    """
    import time as _t
    stmts = [
            """
            CREATE TABLE IF NOT EXISTS wh_products (
                id SERIAL PRIMARY KEY,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                unit TEXT DEFAULT 'cái',
                category TEXT DEFAULT '',
                pos_product_id TEXT DEFAULT '',
                pos_variation_id TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_shops (
                id SERIAL PRIMARY KEY,
                shop_key TEXT NOT NULL UNIQUE,
                shop_name TEXT NOT NULL,
                pos_shop_id TEXT NOT NULL,
                pos_api_key TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                status TEXT DEFAULT 'active'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_inventory (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES wh_products(id),
                qty INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_shop_inventory (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES wh_shops(id),
                product_id INTEGER NOT NULL REFERENCES wh_products(id),
                qty_pos INTEGER NOT NULL DEFAULT 0,
                synced_at TEXT DEFAULT '',
                UNIQUE(shop_id, product_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_stock_movements (
                id SERIAL PRIMARY KEY,
                type TEXT NOT NULL,
                product_id INTEGER NOT NULL REFERENCES wh_products(id),
                shop_id INTEGER REFERENCES wh_shops(id),
                qty INTEGER NOT NULL,
                qty_before INTEGER NOT NULL DEFAULT 0,
                qty_after INTEGER NOT NULL DEFAULT 0,
                ref_order_id TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_by TEXT DEFAULT 'system',
                created_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_outbound_requests (
                id SERIAL PRIMARY KEY,
                order_code TEXT NOT NULL,
                order_id_external TEXT DEFAULT '',
                shop_id INTEGER REFERENCES wh_shops(id),
                shop_name TEXT DEFAULT '',
                product_id INTEGER REFERENCES wh_products(id),
                product_sku TEXT DEFAULT '',
                product_name TEXT DEFAULT '',
                qty_ordered INTEGER NOT NULL DEFAULT 0,
                qty_confirmed INTEGER DEFAULT 0,
                carrier_name TEXT DEFAULT '',
                tracking_code TEXT DEFAULT '',
                carrier_picked_up_at TEXT DEFAULT '',
                order_inserted_at TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                pancake_status TEXT DEFAULT 'shipped',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_return_receipts (
                id SERIAL PRIMARY KEY,
                order_code TEXT NOT NULL,
                shop_id INTEGER REFERENCES wh_shops(id),
                shop_name TEXT DEFAULT '',
                product_id INTEGER REFERENCES wh_products(id),
                product_sku TEXT DEFAULT '',
                product_name TEXT DEFAULT '',
                qty_expected INTEGER NOT NULL DEFAULT 0,
                qty_received INTEGER DEFAULT 0,
                return_reason TEXT DEFAULT '',
                carrier_name TEXT DEFAULT '',
                tracking_code TEXT DEFAULT '',
                order_id_external TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                received_at TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                returned_at TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',
                received_good_qty INTEGER DEFAULT 0,
                received_damaged_qty INTEGER DEFAULT 0,
                missing_qty INTEGER DEFAULT 0,
                good_ledger_posted_qty INTEGER DEFAULT 0,
                signaled_at TEXT DEFAULT ''
            )
            """,
            # ── wh_warehouses: quản lý nhiều kho vật lý ──
            """
            CREATE TABLE IF NOT EXISTS wh_warehouses (
                id SERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                address TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT ''
            )
            """,
            # Migration: thêm các cột còn thiếu vào wh_warehouses
            "ALTER TABLE wh_warehouses ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''",
            "ALTER TABLE wh_warehouses ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
            "ALTER TABLE wh_warehouses ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT ''",
            # Seed kho mặc định (Vĩnh Xá = kho cũ)
            """
            INSERT INTO wh_warehouses (code, name, address, status)
            VALUES ('KHO_VX', 'Kho Vĩnh Xá', 'Vĩnh Xá', 'active')
            ON CONFLICT (code) DO NOTHING
            """,
            # wh_inventory: thêm warehouse_id, giữ tương thích cũ
            """
            ALTER TABLE wh_inventory ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)
            """,
            # Gán toàn bộ tồn kho cũ vào Kho Vĩnh Xá
            """
            UPDATE wh_inventory SET warehouse_id = (SELECT id FROM wh_warehouses WHERE code='KHO_VX')
            WHERE warehouse_id IS NULL
            """,
            # wh_stock_movements: thêm warehouse_id
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            # wh_phanbо: phân bổ từ kho vật lý → shop POS
            """
            CREATE TABLE IF NOT EXISTS wh_phanbo (
                id SERIAL PRIMARY KEY,
                warehouse_id INTEGER NOT NULL REFERENCES wh_warehouses(id),
                shop_id INTEGER NOT NULL REFERENCES wh_shops(id),
                product_id INTEGER NOT NULL REFERENCES wh_products(id),
                product_sku TEXT DEFAULT '',
                qty INTEGER NOT NULL DEFAULT 0,
                note TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_by TEXT DEFAULT 'system',
                created_at TEXT DEFAULT '',
                synced_at TEXT DEFAULT ''
            )
            """,
            # Migration: thêm các cột mới nếu chưa có (bảng cũ tạo trước khi có cột này)
            # Migration: thêm cột giá nhập / giá bán vào wh_products
            "ALTER TABLE wh_products ADD COLUMN IF NOT EXISTS gia_nhap NUMERIC DEFAULT 0",
            "ALTER TABLE wh_products ADD COLUMN IF NOT EXISTS gia_ban NUMERIC DEFAULT 0",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS order_inserted_at TEXT DEFAULT ''",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS pancake_status TEXT DEFAULT 'shipped'",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_order_code ON wh_outbound_requests(order_code)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_ext_id ON wh_outbound_requests(order_id_external)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_status ON wh_outbound_requests(status)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_pancake ON wh_outbound_requests(pancake_status)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_pickup ON wh_outbound_requests(carrier_picked_up_at)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ob_inserted ON wh_outbound_requests(order_inserted_at)",
            "ALTER TABLE wh_return_receipts ADD COLUMN IF NOT EXISTS pancake_return_status INTEGER DEFAULT NULL",
            # warehouse_id theo shop — gán kho vật lý cho từng shop
            "ALTER TABLE wh_shops ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            # warehouse_id trên đơn xuất/hoàn — kế thừa từ shop
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS confirmed_by TEXT DEFAULT ''",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS confirmed_at TEXT DEFAULT ''",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS pre_confirmed_at TEXT DEFAULT ''",
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS pre_confirmed_by TEXT DEFAULT ''",
            "ALTER TABLE wh_return_receipts ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            "ALTER TABLE wh_return_receipts ADD COLUMN IF NOT EXISTS confirmed_by TEXT DEFAULT ''",
            "ALTER TABLE wh_return_receipts ADD COLUMN IF NOT EXISTS pos_variation_id TEXT DEFAULT ''",
            "ALTER TABLE wh_return_receipts ADD COLUMN IF NOT EXISTS variant_name TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_wh_ret_order ON wh_return_receipts(order_code)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ret_status ON wh_return_receipts(status)",
            "CREATE INDEX IF NOT EXISTS idx_wh_ret_pancake_status ON wh_return_receipts(pancake_return_status)",
            "CREATE INDEX IF NOT EXISTS idx_wh_mov_type ON wh_stock_movements(type)",
            "CREATE INDEX IF NOT EXISTS idx_wh_mov_created ON wh_stock_movements(created_at)",
            # wh_variation_map: ánh xạ TOÀN BỘ variation UUID (S/M/L) → wh_products.id
            # Giải quyết case variations không có custom_id riêng (dùng chung SKU sản phẩm)
            """
            CREATE TABLE IF NOT EXISTS wh_variation_map (
                pos_variation_id TEXT PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES wh_products(id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_wh_varmap_product ON wh_variation_map(product_id)",
            # pos_variation_id trên đơn xuất kho — dùng dedup khi product_sku rỗng
            "ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS pos_variation_id TEXT DEFAULT ''",
            # Dedup + UNIQUE index — ngăn duplicate khi 2 sync chạy song song
            """
            DELETE FROM wh_outbound_requests a USING wh_outbound_requests b
            WHERE a.id < b.id
              AND a.shop_id = b.shop_id
              AND a.order_id_external = b.order_id_external
              AND a.product_sku = b.product_sku
              AND a.order_id_external != ''
            """,
            # ❗ UNIQUE PHẢI có shop_id: order_id_external đếm độc lập theo từng shop POS
            # (shop A đơn 1 + shop B đơn 1 cùng SKU = 2 đơn khác nhau).
            # Bug fix 2026-05-21: constraint cũ (order_id_external, pos_variation_id)
            # gây UniqueViolation cross-shop → fast-sync 10s đứng từ vài ngày qua.
            "DROP INDEX IF EXISTS uq_wh_ob_ext_sku",
            "DROP INDEX IF EXISTS uq_wh_ob_ext_varid",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_ob_shop_ext_varid
            ON wh_outbound_requests(shop_id, order_id_external, pos_variation_id)
            WHERE order_id_external != '' AND pos_variation_id != ''
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_ob_shop_ext_sku
            ON wh_outbound_requests(shop_id, order_id_external, product_sku)
            WHERE order_id_external != ''
              AND (pos_variation_id = '' OR pos_variation_id IS NULL)
            """,
            """
            DELETE FROM wh_return_receipts a USING wh_return_receipts b
            WHERE a.id < b.id
              AND a.shop_id = b.shop_id
              AND a.order_id_external = b.order_id_external
              AND a.product_sku = b.product_sku
              AND a.order_id_external != ''
            """,
            "DROP INDEX IF EXISTS uq_wh_ret_ext_sku",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_ret_shop_ext_sku
            ON wh_return_receipts(shop_id, order_id_external, product_sku)
            WHERE order_id_external != ''
            """,
            # wh_stocktakes: bảng phiếu kiểm kho — tạo nếu chưa có
            """
            CREATE TABLE IF NOT EXISTS wh_stocktakes (
                id SERIAL PRIMARY KEY,
                stocktake_code TEXT NOT NULL UNIQUE,
                warehouse_id INTEGER REFERENCES wh_warehouses(id),
                status TEXT DEFAULT 'draft',
                note TEXT DEFAULT '',
                created_by TEXT DEFAULT 'admin',
                confirmed_by TEXT DEFAULT '',
                confirmed_at TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wh_stocktake_items (
                id SERIAL PRIMARY KEY,
                stocktake_id INTEGER NOT NULL REFERENCES wh_stocktakes(id),
                product_id INTEGER REFERENCES wh_products(id),
                warehouse_id INTEGER REFERENCES wh_warehouses(id),
                sku TEXT DEFAULT '',
                product_name TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                category TEXT DEFAULT '',
                qty_system INTEGER DEFAULT 0,
                qty_pos INTEGER DEFAULT 0,
                qty_counted INTEGER DEFAULT 0,
                diff INTEGER DEFAULT 0,
                UNIQUE(stocktake_id, product_id)
            )
            """,
            # Migration: thêm warehouse_id vào bảng cũ nếu chưa có
            "ALTER TABLE wh_stocktakes ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            "ALTER TABLE wh_stocktake_items ADD COLUMN IF NOT EXISTS warehouse_id INTEGER REFERENCES wh_warehouses(id)",
            # Track push-to-POS trên từng phiếu nhập
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS pushed_at TEXT DEFAULT NULL",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS pushed_qty INTEGER DEFAULT 0",
            # Ngưỡng cảnh báo tồn kho thấp
            "ALTER TABLE wh_products ADD COLUMN IF NOT EXISTS min_qty INTEGER DEFAULT 0",
            # Huỷ / xoá phiếu nhập kho
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS cancelled_at TEXT DEFAULT NULL",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS cancelled_by TEXT DEFAULT NULL",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS cancel_reason TEXT DEFAULT NULL",
            # Lưu purchase_id trên POS Pancake để huỷ đồng bộ
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS pos_purchase_ids TEXT DEFAULT NULL",
            # Batch id/source để hoàn tác 1 cú nhập Excel / 1 lần submit cart
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS batch_id TEXT DEFAULT NULL",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS batch_source TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_wh_mov_batch ON wh_stock_movements(batch_id)",
            # Giá nhập / giá bán THEO PHIẾU — NULL = không đổi (không sync POS)
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS gia_nhap NUMERIC DEFAULT NULL",
            "ALTER TABLE wh_stock_movements ADD COLUMN IF NOT EXISTS gia_ban  NUMERIC DEFAULT NULL",
            # sync_state: trạng thái sync per-shop — smart sync tự phát hiện lần đầu
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                id               SERIAL PRIMARY KEY,
                source_name      TEXT NOT NULL,
                shop_id          INTEGER REFERENCES wh_shops(id),
                shop_key         TEXT DEFAULT '',
                last_full_sync_at  TIMESTAMPTZ DEFAULT NULL,
                last_today_sync_at TIMESTAMPTZ DEFAULT NULL,
                status           TEXT DEFAULT 'pending',
                last_error       TEXT DEFAULT '',
                run_count        INTEGER DEFAULT 0,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                updated_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(source_name, shop_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sync_state_source ON sync_state(source_name)",
    ]
    for _attempt in range(5):
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                # P0-8 phòng ngừa: lock_timeout=15s — nếu ALTER không lấy được AccessExclusiveLock
                # trong 15s (do zombie idle-in-transaction holding lock) → fail nhanh thay vì hang
                # toàn web. Kết hợp với idle_in_transaction_session_timeout=300s ở DB level.
                cur.execute("SET lock_timeout = '15s'")
                for stmt in stmts:
                    cur.execute(stmt)
            return
        except Exception as _e:
            _msg = str(_e).lower()
            if ("deadlock" in _msg or "lock_timeout" in _msg or "canceling statement" in _msg) and _attempt < 4:
                print(f"[wh_db] init_wh_tables {_msg[:80]}, thử lại {_attempt + 1}/5 sau 3s...", flush=True)
                _t.sleep(3)
            else:
                raise
