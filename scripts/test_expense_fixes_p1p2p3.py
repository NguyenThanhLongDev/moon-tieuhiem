#!/usr/bin/env python3
"""Test 3 fix P1+P2+P3 cho modules/expense_chat — chạy local, không Zalo.

P1: _parse_expense_text reject AI hallucination khi body không có số
P2: _find_duplicate_item bắt exact match trong 48h
P3: _merge_into_existing lấy số mới + upgrade category/date

Usage:
    cd /home/admin1/tieuhiemsoft/posbottieuhiem
    .venv/bin/python scripts/test_expense_fixes_p1p2p3.py
"""
import os
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load env như production
ENV_FILE = ROOT / "deploy" / "pos-dashboard.env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from modules.expense_chat import (
    _parse_expense_text,
    _find_duplicate_item,
    _merge_into_existing,
)
from db import get_conn

OK, FAIL = "✅ PASS", "❌ FAIL"


def t_p1():
    """P1: AI không được bịa amount khi body trống số."""
    print("\n— P1: Anti-hallucination —")
    cases = [
        ("Tiền điện kho", 0, "không có số → reject"),
        ("VPP văn phòng", 0, "không có số → reject"),
        ("trả tiền điện", 0, "không có số → reject"),
        ("Mua 500k giấy", 500_000, "có '500k' → parse OK"),
        ("Sửa lại 2.886.728đ", 2_886_728, "có số đầy đủ → parse OK"),
        ("ba triệu rưỡi", None, "tiếng Việt → AI có thể parse được"),
    ]
    for body, expected_amount, desc in cases:
        out = _parse_expense_text(body)
        amt = int(out.get("amount_vnd") or 0)
        halluc = out.get("_hallucination", False)
        if expected_amount is None:
            ok = True  # accept any answer (just verify no crash)
            verdict = "(AI tự quyết, không assert)"
        elif expected_amount == 0:
            ok = (amt == 0)
            verdict = f"halluc={halluc}, amt={amt}"
        else:
            # accept ±10% biên độ vì model có thể parse hơi lệch
            ok = abs(amt - expected_amount) / expected_amount < 0.1
            verdict = f"amt={amt} (expect ~{expected_amount})"
        print(f"  {OK if ok else FAIL}  {body!r:<32} {verdict}  — {desc}")


def t_p2():
    """P2: Dup check bắt exact match trong 48h, không chỉ 10 phút."""
    print("\n— P2: Dup check 48h exact —")
    SENDER = "test_sender_p2_xxxx"  # uid giả, dọn cuối
    AMT = 1_234_567
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            # Insert 1 item từ 6 GIỜ TRƯỚC (ngoài cửa sổ 10 phút cũ, trong cửa sổ 48h mới)
            cur.execute("""
                INSERT INTO company_expense_items
                    (occurred_date, amount_vnd, category, note, source,
                     zalo_thread_id, zalo_sender_id, zalo_sender_name,
                     status, created_at, updated_at)
                VALUES (CURRENT_DATE, %s, 'other', 'TEST P2 base', 'text',
                        '0000', %s, 'test', 'pending',
                        NOW() - INTERVAL '6 hours', NOW())
                RETURNING id
            """, (AMT, SENDER))
            base_id = cur.fetchone()[0]
            conn.commit()
        # Test 1: exact match (6h trước, exact amount) → phải bắt
        with conn.cursor() as cur:
            hit = _find_duplicate_item(cur, SENDER, AMT)
        if hit and hit["id"] == base_id:
            print(f"  {OK}  Exact match 6h trước → bắt dup (id={hit['id']})")
        else:
            print(f"  {FAIL}  Exact match 6h trước → KHÔNG bắt (hit={hit})")
        # Test 2: ±5% nhưng 6h trước → ngoài 10 phút, không exact → KHÔNG bắt
        with conn.cursor() as cur:
            hit = _find_duplicate_item(cur, SENDER, AMT + 50_000)
        if not hit:
            print(f"  {OK}  ±5% nhưng 6h trước → không bắt (đúng — chỉ exact mới bắt rộng)")
        else:
            print(f"  {FAIL}  ±5% nhưng 6h trước → bắt nhầm (hit={hit})")
        # Test 3: 50h trước (ngoài 48h) → không bắt
        with conn.cursor() as cur:
            cur.execute("UPDATE company_expense_items SET created_at = NOW() - INTERVAL '50 hours' WHERE id=%s", (base_id,))
            conn.commit()
        with conn.cursor() as cur:
            hit = _find_duplicate_item(cur, SENDER, AMT)
        if not hit:
            print(f"  {OK}  Exact match 50h trước → không bắt (ngoài cửa sổ 48h)")
        else:
            print(f"  {FAIL}  Exact match 50h trước → bắt nhầm (hit={hit})")
        # Cleanup
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            conn.commit()


def t_p3():
    """P3: Merge lấy số mới + upgrade category + date từ ảnh."""
    print("\n— P3: Merge upgrade —")
    SENDER = "test_sender_p3_xxxx"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            # Item cũ: text, 3.000.000, category=other, ngày=04/06
            cur.execute("""
                INSERT INTO company_expense_items
                    (occurred_date, amount_vnd, category, note, source,
                     zalo_thread_id, zalo_sender_id, zalo_sender_name,
                     status, created_at, updated_at)
                VALUES ('2026-06-04', 3000000, 'other', 'Tiền điện kho', 'text',
                        '0000', %s, 'test', 'pending', NOW(), NOW())
                RETURNING id
            """, (SENDER,))
            old_id = cur.fetchone()[0]
            conn.commit()
        # Merge: new amount = 2.886.728 (nhỏ hơn cũ!) + utility + 02/06 + image
        with conn.cursor() as cur:
            _merge_into_existing(
                cur, old_id,
                new_amount=2_886_728,
                new_note="Thanh toán hoá đơn Điện lực miền Bắc kỳ 05/2026",
                new_source="image",
                new_source_url="https://test.zalo/biên_lai.jpg",
                new_category="utility",
                new_occurred_date="2026-06-02",
            )
            conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT amount_vnd, category, occurred_date, source, source_url, note "
                        "FROM company_expense_items WHERE id=%s", (old_id,))
            r = cur.fetchone()
        amt, cat, dt, src, url, note = r
        checks = [
            ("amount lấy số MỚI (2.886.728), không max() = 3tr", int(amt) == 2_886_728, f"amount={amt}"),
            ("category upgrade 'other' → 'utility'", cat == "utility", f"cat={cat}"),
            ("occurred_date lấy ngày MỚI từ ảnh (02/06)", str(dt) == "2026-06-02", f"date={dt}"),
            ("source upgrade text → image", src == "image", f"src={src}"),
            ("source_url được set", "test.zalo" in (url or ""), f"url={url}"),
            ("note merge có cả 2 nguồn", "Tiền điện kho" in note and "Điện lực miền Bắc" in note, f"note={note[:80]}..."),
        ]
        for label, ok, info in checks:
            print(f"  {OK if ok else FAIL}  {label}  ({info})")
        # Cleanup
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            conn.commit()


def t_p3_no_downgrade():
    """P3 bonus: không downgrade utility → other."""
    print("\n— P3 bonus: Không downgrade category —")
    SENDER = "test_sender_p3b_xxxx"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            cur.execute("""
                INSERT INTO company_expense_items
                    (occurred_date, amount_vnd, category, note, source,
                     zalo_thread_id, zalo_sender_id, zalo_sender_name,
                     status, created_at, updated_at)
                VALUES (CURRENT_DATE, 1000000, 'utility', 'Tiền điện', 'image',
                        '0000', %s, 'test', 'pending', NOW(), NOW())
                RETURNING id
            """, (SENDER,))
            old_id = cur.fetchone()[0]
            conn.commit()
        with conn.cursor() as cur:
            _merge_into_existing(
                cur, old_id,
                new_amount=1_000_000,
                new_note="thêm note",
                new_source="text",
                new_category="other",  # downgrade attempt — phải bị từ chối
            )
            conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT category FROM company_expense_items WHERE id=%s", (old_id,))
            r = cur.fetchone()
        cat = r[0]
        ok = cat == "utility"
        print(f"  {OK if ok else FAIL}  utility KHÔNG bị 'other' override (cat={cat})")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_expense_items WHERE zalo_sender_id = %s", (SENDER,))
            conn.commit()


if __name__ == "__main__":
    t_p1()
    t_p2()
    t_p3()
    t_p3_no_downgrade()
    print("\nDone.")
