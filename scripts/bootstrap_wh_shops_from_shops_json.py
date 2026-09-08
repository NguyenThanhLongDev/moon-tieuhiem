#!/usr/bin/env python3
"""
Đồng bộ wh_shops từ shops.json — module Kho vật lý chỉ đọc wh_shops.

Logic ưu tiên api_key:
  1. Giữ nguyên pos_api_key đang có trong DB (nếu đã set → không overwrite)
  2. Lấy từ shops.json nếu DB chưa có
  3. Lấy từ bảng `shops` (DB chính) nếu vẫn chưa có — join theo pos_shop_id

Chạy an toàn nhiều lần (UPSERT theo shop_key, bỏ qua shop inactive).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import psycopg2

SHOPS_FILE = BASE_DIR / "shops.json"


def get_conn():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL chưa set.", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(db_url)


def _load_db_api_keys(cur) -> dict[str, str]:
    """
    Trả về {pos_shop_id: api_key} từ bảng shops chính (nếu tồn tại).
    Dùng làm fallback khi shops.json không có api_key.

    Psycopg2: lỗi SQL đặt transaction ở aborted — phải rollback trước INSERT sau.
    """
    keys: dict[str, str] = {}
    try:
        cur.execute("""
            SELECT pos_shop_id, pos_api_key FROM shops
            WHERE pos_api_key IS NOT NULL AND pos_api_key != ''
        """)
        for row in cur.fetchall():
            if row[0] and row[1]:
                keys[str(row[0]).strip()] = row[1].strip()
    except Exception:
        cur.connection.rollback()
    return keys


def main() -> None:
    if not SHOPS_FILE.is_file():
        print(f"ERROR: không tìm thấy {SHOPS_FILE}", file=sys.stderr)
        sys.exit(1)

    with SHOPS_FILE.open(encoding="utf-8") as f:
        rows = json.load(f)

    if not isinstance(rows, list):
        print("ERROR: shops.json phải là list", file=sys.stderr)
        sys.exit(1)

    conn = get_conn()
    cur = conn.cursor()

    # Lấy api_keys từ bảng shops chính (fallback)
    db_api_keys = _load_db_api_keys(cur)
    if db_api_keys:
        print(f"  [INFO] Tìm thấy {len(db_api_keys)} api_key từ bảng shops (DB chính)", file=sys.stderr)

    # Master api_key: tất cả shops dùng chung 1 key (lấy key đầu tiên có trong shops.json)
    master_api_key = next(
        (str(s.get("pos_api_key") or "").strip() for s in rows
         if isinstance(s, dict) and str(s.get("pos_api_key") or "").strip()),
        ""
    )
    if master_api_key:
        print(f"  [INFO] Master api_key tìm thấy trong shops.json — dùng cho tất cả shops chưa có key", file=sys.stderr)

    upserted = 0
    skipped = 0
    try:
        for s in rows:
            if not isinstance(s, dict):
                continue
            status = str(s.get("status") or "active").strip().lower()
            if status == "inactive":
                skipped += 1
                continue

            shop_key  = str(s.get("shop_key")  or "").strip()
            shop_name = str(s.get("shop_name") or "").strip()
            pos_id    = str(s.get("shop_id")   or s.get("pos_shop_id") or "").strip()
            team_id   = str(s.get("team_id")   or "").strip() or None

            # Ưu tiên: shops.json → DB shops table → master key (dùng chung)
            api_key = str(s.get("pos_api_key") or "").strip()
            if not api_key and pos_id in db_api_keys:
                api_key = db_api_keys[pos_id]
            if not api_key:
                api_key = master_api_key

            if not shop_key or not shop_name or not pos_id:
                print(f"  [SKIP] thiếu trường bắt buộc: {s.get('shop_key', '?')}", file=sys.stderr)
                skipped += 1
                continue

            # QUAN TRỌNG: KHÔNG overwrite pos_api_key đã có trong DB
            # Chỉ set nếu DB đang trống (bảo vệ api_key đã set thủ công hoặc từ lần trước)
            cur.execute(
                """
                INSERT INTO wh_shops (shop_key, shop_name, pos_shop_id, pos_api_key, team_id, status)
                VALUES (%s, %s, %s, %s, %s, 'active')
                ON CONFLICT (shop_key) DO UPDATE SET
                    shop_name   = EXCLUDED.shop_name,
                    pos_shop_id = EXCLUDED.pos_shop_id,
                    pos_api_key = CASE
                        WHEN wh_shops.pos_api_key IS NOT NULL AND wh_shops.pos_api_key != ''
                        THEN wh_shops.pos_api_key
                        ELSE EXCLUDED.pos_api_key
                    END,
                    team_id     = EXCLUDED.team_id,
                    status      = 'active'
                """,
                (shop_key, shop_name, pos_id, api_key, team_id),
            )
            upserted += 1
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: bootstrap wh_shops thất bại: {exc}", file=sys.stderr)
        sys.exit(1)

    conn.commit()
    cur.close()
    conn.close()

    print(f"OK: bootstrap wh_shops — {upserted} shop active, {skipped} bỏ qua (inactive/thiếu trường)")


if __name__ == "__main__":
    main()
