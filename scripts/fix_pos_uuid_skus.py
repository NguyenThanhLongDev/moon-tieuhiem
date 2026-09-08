#!/usr/bin/env python3
"""
Fix SKU dạng POS-UUID cho 60 sản phẩm không có custom_id/display_id từ POS.
Sinh SKU sạch từ variant_name trong wh_variation_map.

Chạy: .venv/bin/python3 scripts/fix_pos_uuid_skus.py [--dry-run]
"""
import os, sys, re, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── Transliterate Vietnamese ──────────────────────────────────────────────────
_VN_MAP = str.maketrans(
    "àáảãạăắặằẳẵâấầậẩẫèéẻẽẹêếềệểễìíỉĩịòóỏõọôốồộổỗơớờợởỡùúủũụưứừựửữỳýỷỹỵđ"
    "ÀÁẢÃẠĂẮẶẰẲẴÂẤẦẬẨẪÈÉẺẼẸÊẾỀỆỂỄÌÍỈĨỊÒÓỎÕỌÔỐỒỘỔỖƠỚỜỢỞỠÙÚỦŨỤƯỨỪỰỬỮỲÝỶỸỴĐ",
    "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
    "AAAAAAAAAAAAAAAAAEEEEEEEEEEEIIIIIOOOOOOOOOOOOOOOOOUUUUUUUUUUUYYYYYD",
)


def sanitize(text: str) -> str:
    """Bỏ dấu tiếng Việt, uppercase, giữ A-Z0-9 dấu gạch."""
    text = text.translate(_VN_MAP)
    text = re.sub(r'[^A-Za-z0-9\-_]', '', text)
    return text.upper()


def make_sku_from_variant(product_name: str, variant_name: str) -> str | None:
    """
    Sinh SKU từ variant_name:
    - Nếu variant_name vô nghĩa ("1", "default") → dùng product_name (phần trước dấu '(')
    - Truncate về 24 ký tự
    """
    vn = variant_name.strip()
    if not vn or vn in ("1", "default"):
        # Dùng tên sản phẩm (bỏ phần trong ngoặc)
        base = re.sub(r'\s*\(.*\)\s*$', '', product_name).strip()
        sanitized = sanitize(base)
    else:
        sanitized = sanitize(vn)

    sanitized = sanitized[:24].rstrip('-_')
    return sanitized if sanitized else None


def ensure_unique(cur, proposed: str, product_id: int) -> str:
    """Nếu SKU đã tồn tại (SP khác), thêm hậu tố số."""
    base = proposed
    suffix = 1
    while True:
        cur.execute(
            "SELECT id FROM wh_products WHERE sku = %s AND id != %s",
            (proposed, product_id)
        )
        row = cur.fetchone()
        if not row:
            return proposed
        proposed = f"{base[:20]}-{suffix}"
        suffix += 1
        if suffix > 99:
            raise RuntimeError(f"Không thể tìm SKU duy nhất cho '{base}'")


def main():
    parser = argparse.ArgumentParser(description="Fix POS-UUID SKUs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Chỉ in ra, không ghi DB")
    args = parser.parse_args()

    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    if not DATABASE_URL:
        env_file = Path(__file__).resolve().parents[1] / "deploy" / "pos-dashboard.env"
        for line in env_file.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                DATABASE_URL = line.split("=", 1)[1].strip()
                break
    if not DATABASE_URL:
        sys.exit("Không tìm thấy DATABASE_URL")

    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    _UUID_PAT = re.compile(
        r'^POS-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    )

    cur.execute("""
        SELECT p.id AS product_id, p.sku AS old_sku, p.name AS product_name,
               vm.variant_name
        FROM wh_products p
        LEFT JOIN wh_variation_map vm
               ON vm.pos_variation_id = p.pos_variation_id
        WHERE p.sku ~ '^POS-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ORDER BY p.name
    """)
    rows = cur.fetchall()
    print(f"Tìm thấy {len(rows)} sản phẩm POS-UUID")
    print()

    updated = 0
    skipped = 0
    errors = []

    for row in rows:
        pid = row["product_id"]
        old_sku = row["old_sku"]
        variant_name = row["variant_name"] or ""
        product_name = row["product_name"] or ""

        new_sku = make_sku_from_variant(product_name, variant_name)
        if not new_sku:
            print(f"  SKIP  [{pid}] {product_name!r} — không tạo được SKU")
            skipped += 1
            continue

        # Đảm bảo unique
        try:
            final_sku = ensure_unique(cur, new_sku, pid)
        except RuntimeError as e:
            errors.append(f"[{pid}] {e}")
            continue

        conflict_note = f" (→ {final_sku} vì conflict)" if final_sku != new_sku else ""
        print(f"  {'DRY ' if args.dry_run else 'FIX '}[{pid}] {old_sku[:22]}... → {final_sku}{conflict_note}  | {product_name}")

        if not args.dry_run:
            cur.execute(
                "UPDATE wh_products SET sku = %s WHERE id = %s",
                (final_sku, pid)
            )
        updated += 1

    print()
    print(f"Kết quả: {updated} sẽ cập nhật, {skipped} bỏ qua, {len(errors)} lỗi")
    if errors:
        for e in errors:
            print(f"  LỖI: {e}")

    if not args.dry_run and updated > 0:
        conn.commit()
        print("✅ Đã commit vào DB.")
    else:
        conn.rollback()
        if args.dry_run:
            print("(dry-run — không ghi gì)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
