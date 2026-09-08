"""
Sync tồn kho từ Pancake POS API → wh_shop_inventory (theo từng shop).

Khi POS không còn trả về một sản phẩm/biến thể cho shop đó, sau sync sẽ
DELETE các dòng wh_shop_inventory thừa (shop_id + product_id) — bên phần mềm
“xóa theo” tồn online theo shop, không xóa danh mục wh_products hay đơn hàng.

Adapted từ posbot/sync_pos.py — nhận conn là _Conn wrapper từ wh_db.
"""
from __future__ import annotations

import json
import re
import requests
from datetime import datetime
from pathlib import Path
try:
    from tz_utils import now_hcm
except ImportError:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
    from tz_utils import now_hcm

PANCAKE_API_BASE = "https://pos.pages.fm/api/v1"

# ── Master API key ──────────────────────────────────────────────────────────
# Đọc từ DB (wh_shops.pos_api_key), fallback về shops.json.
_MASTER_API_KEY: str | None = None

def get_master_api_key() -> str:
    """Đọc master api_key từ DB (shop đầu tiên có api_key).
    Cache kết quả để không query DB lại mỗi lần.
    """
    global _MASTER_API_KEY
    if _MASTER_API_KEY is not None:
        return _MASTER_API_KEY
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from shop_helpers import load_all_shops
        for s in load_all_shops():
            key = str(s.get("pos_api_key") or "").strip()
            if key:
                _MASTER_API_KEY = key
                return _MASTER_API_KEY
    except Exception:
        pass
    # Fallback: đọc từ shops.json
    try:
        shops_file = Path(__file__).resolve().parents[2] / "shops.json"
        shops = json.loads(shops_file.read_text(encoding="utf-8"))
        for s in shops:
            key = str(s.get("pos_api_key") or "").strip()
            if key:
                _MASTER_API_KEY = key
                return _MASTER_API_KEY
    except Exception:
        pass
    _MASTER_API_KEY = ""
    return _MASTER_API_KEY


def load_stock_from_api(pos_shop_id: str, api_key: str) -> list:
    """Gọi Pancake POS API lấy danh sách sản phẩm + tồn kho của shop."""
    all_products = []
    page = 1
    while True:
        try:
            url = f"{PANCAKE_API_BASE}/shops/{pos_shop_id}/products"
            resp = requests.get(url, params={
                "api_key": api_key,
                "page": page,
                "limit": 200,
            }, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            # Kiểm tra lỗi API-level (HTTP 200 nhưng success=false)
            if data.get("success") is False or data.get("error_code"):
                msg = data.get("message") or data.get("error") or "API trả lỗi không xác định"
                return {"error": f"{msg} (shop {pos_shop_id})"}
            products = data.get("data") or []
            all_products.extend(products)
            if page >= data.get("total_pages", 1):
                break
            page += 1
        except Exception as e:
            return {"error": str(e)}
    return all_products


_UUID_SEG = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}')


def _is_proper_sku(code: str) -> bool:
    """Mã SKU thật: có cả chữ lẫn số, KHÔNG chứa đoạn UUID (xxxxxxxx-xxxx)."""
    if not code:
        return False
    if _UUID_SEG.search(code):
        return False
    return bool(re.search(r'[A-Za-z]', code) and re.search(r'[0-9]', code))


def _is_usable_code(code: str) -> bool:
    """Mã DÙNG ĐƯỢC làm SKU: không rỗng, không UUID, có ít nhất 1 ký tự chữ HOẶC số.
    Không yêu cầu phải có cả chữ và số (lỏng hơn _is_proper_sku) — dùng cho display_id
    vì Pancake hay đặt display_id là "BAOGO", "ABC", "68" v.v.
    """
    if not code:
        return False
    if _UUID_SEG.search(code):
        return False
    return bool(re.search(r'[A-Za-z0-9]', code))


def _is_bad_sku(sku: str) -> bool:
    """SKU xấu: rỗng, bắt đầu bằng POS-, hoặc chứa đoạn UUID.

    KHÔNG enumerate pattern variant-name nữa — sync chỉ tạo product khi
    `prod.custom_id` từ POS hợp lệ, nên fake variant-name không thể lọt qua.
    """
    if not sku:
        return True
    s = sku.strip()
    if s.startswith("POS-") or _UUID_SEG.search(s):
        return True
    return False


def _sanitize_sku_fragment(text: str) -> str:
    """Chuẩn hoá 1 đoạn text thành fragment dùng được trong SKU:
    bỏ dấu tiếng Việt, uppercase, giữ A-Z0-9 và dấu gạch ngang/gạch dưới.
    """
    _VN_MAP = str.maketrans(
        "àáảãạăắặằẳẵâấầậẩẫèéẻẽẹêếềệểễìíỉĩịòóỏõọôốồộổỗơớờợởỡùúủũụưứừựửữỳýỷỹỵđ"
        "ÀÁẢÃẠĂẮẶẰẲẴÂẤẦẬẨẪÈÉẺẼẸÊẾỀỆỂỄÌÍỈĨỊÒÓỎÕỌÔỐỒỘỔỖƠỚỜỢỞỠÙÚỦŨỤƯỨỪỰỬỮỲÝỶỸỴĐ",
        "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
        "AAAAAAAAAAAAAAAAAEEEEEEEEEEEIIIIIOOOOOOOOOOOOOOOOOUUUUUUUUUUUYYYYYD",
    )
    text = text.translate(_VN_MAP)
    text = re.sub(r'[^A-Za-z0-9\-_]', '', text)
    return text.upper()


def _get_sku(var: dict, prod: dict) -> str:
    """
    Lấy SKU duy nhất cho variation. Pancake có 2 field mã: custom_id (shop tự đặt)
    và display_id (mã hiển thị). Phải đọc cả hai để không bỏ sót:

      1. variation.custom_id là mã proper (chữ+số) → dùng trực tiếp
      2. variation.display_id usable (vd "BAOGO") → dùng trực tiếp
      2.5. variation.name usable → sanitize rồi dùng (fallback khi display_id rỗng)
      3. product.custom_id proper + variation có tên thường → PRODCODE-VARNAME
      4. product.custom_id proper + variation UUID → PRODCODE-{uuid_short}
      5. product.custom_id proper, variation rỗng → product code
      6. product.display_id usable + variation hint → ghép
      7. product.display_id usable đơn lẻ → dùng
      8. Fallback → POS-{variation_uuid}
    """
    var_custom = str(var.get("custom_id") or "").strip()
    prod_custom = str(prod.get("custom_id") or "").strip()
    var_display = str(var.get("display_id") or "").strip()
    var_name   = str(var.get("name") or "").strip()
    prod_display = str(prod.get("display_id") or "").strip()
    var_upper = var_custom.upper()
    prod_upper = prod_custom.upper()
    var_disp_upper = var_display.upper()
    prod_disp_upper = prod_display.upper()
    var_id = str(var.get("id") or "")

    # 1. variation.custom_id chuẩn → dùng
    if _is_proper_sku(var_upper):
        return var_upper

    # 2. variation.display_id usable → dùng (vd "BAOGO", "ABC123")
    if _is_usable_code(var_disp_upper):
        return var_disp_upper

    # 2.5. variation.name usable (display_id rỗng, nhưng name có ý nghĩa) → sanitize + dùng
    if var_name and var_name.lower() not in ("1", "default", ""):
        sanitized = _sanitize_sku_fragment(var_name)
        if sanitized and len(sanitized) >= 2:
            # Nếu product có display_id thì ghép: PRODCODE-VARNAME
            if _is_usable_code(prod_disp_upper):
                return f"{prod_disp_upper}-{sanitized}"
            # Không thì dùng VARNAME thôi
            return sanitized

    # 3-5. product.custom_id chuẩn → ghép theo trường hợp
    if _is_proper_sku(prod_upper):
        if var_upper and not _UUID_SEG.search(var_upper):
            return f"{prod_upper}-{var_upper}"
        elif var_upper and _UUID_SEG.search(var_upper):
            return f"{prod_upper}-{var_id[:8]}" if var_id else prod_upper
        else:
            return prod_upper

    # 6-7. product.display_id usable → ghép với hint của variation hoặc dùng đơn lẻ
    if _is_usable_code(prod_disp_upper):
        if var_upper and not _UUID_SEG.search(var_upper):
            return f"{prod_disp_upper}-{var_upper}"
        return prod_disp_upper

    # 8. Fallback
    return f"POS-{var_id}" if var_id else "POS-unknown"


def parse_shop_stock(products: list, pos_shop_id: str) -> list:
    """
    Parse danh sách sản phẩm từ Pancake POS API.
    API endpoint là /shops/{pos_shop_id}/products nên toàn bộ variations_warehouses
    đã thuộc về shop đó — không cần filter theo shop_id.
    """
    result = []
    for prod in products:
        prod_name = prod.get("name") or ""
        # Thuế sản phẩm: Pancake để ở field categories (shop TH ghi name = "8%"...).
        # Lấy category đầu tiên làm product_tax (text). Shop không set → "".
        _cats = prod.get("categories") or []
        product_tax = (str(_cats[0].get("name") or "").strip() if _cats else "")
        for var in prod.get("variations") or []:
            var_id = var.get("id") or ""
            sku = _get_sku(var, prod)
            var_name = str(var.get("display_id") or var.get("name") or "").strip()
            # §18: wh_products.name = prod.name thuần, KHÔNG nhúng (variant_name).
            # Variant name lưu riêng ở wh_variation_map.variant_name.
            display_name = prod_name

            # Lấy qty = SUM(actual_remain_quantity) qua TẤT CẢ warehouse của shop
            # (1 shop POS có thể dùng nhiều kho — đọc [0] sẽ MISS tồn ở kho khác).
            # Bug fix 2026-05-21: TÀI5 ở shop Tài ĐN có 2 WH [0,201] → đọc [0]=0
            vw_list = var.get("variations_warehouses") or []
            if vw_list:
                qty = sum(int(w.get("actual_remain_quantity") or 0) for w in vw_list)
            else:
                qty = var.get("remain_quantity") or 0

            # Giá nhập và giá bán từ POS
            try:
                gia_nhap = float(var.get("average_imported_price") or 0)
            except (TypeError, ValueError):
                gia_nhap = 0.0
            try:
                gia_ban = float(var.get("retail_price") or 0)
            except (TypeError, ValueError):
                gia_ban = 0.0

            result.append({
                "variation_id": var_id,
                "sku": sku,
                "var_name": var_name,
                "product_name": display_name,
                "pos_product_id": prod.get("id") or "",
                "pos_product_custom_id": str(prod.get("custom_id") or "").strip(),
                "qty": qty,
                "gia_nhap": gia_nhap,
                "gia_ban": gia_ban,
                # Nút bật/tắt SP trên Pancake (cấp biến thể). True = shop ngừng bán biến thể này.
                "is_locked": bool(var.get("is_locked")),
                # Thuế SP (Pancake categories[].name, vd "8%") — để tính lương theo SP.
                "product_tax": product_tax,
            })
    return result


def sync_shop_inventory_to_db(conn, shop_db_id: int, pos_shop_id: str,
                               api_key: str | None = None) -> dict:
    """
    Đồng bộ tồn POS **theo 1 shop** từ Pancake API vào DB.

    - UPSERT `shop_inventory` (→ wh_shop_inventory): qty_pos + synced_at cho từng
      product_id còn trên POS. qty_pos = TỔNG tồn của mọi biến thể thuộc SP đó
      (ghi 1 lần/SP sau vòng lặp — không ghi per-biến-thể kẻo đè lẫn nhau).
    - **DELETE** các dòng `shop_inventory` của shop này mà **không còn** trong
      phản hồi API (POS đã bỏ SP/variation khỏi shop → phần mềm xóa dòng tồn theo shop).
    - `wh_products`: chỉ tạo/cập nhật SKU/tên/giá — không xóa master.
    - Không đụng đơn xuất / wh_outbound_requests.

    Trả về có thêm khóa `removed`: số dòng shop_inventory đã xóa.

    conn là _Conn wrapper từ wh_db — execute() dùng ? placeholders.
    """
    if not api_key:
        return {"synced": 0, "errors": [f"Shop {pos_shop_id} chưa có API key."]}

    raw_products = load_stock_from_api(pos_shop_id, api_key)
    if isinstance(raw_products, dict) and "error" in raw_products:
        return {"synced": 0, "errors": [raw_products["error"]]}
    if not raw_products:
        return {"synced": 0, "errors": [f"Không có data cho shop {pos_shop_id}"]}

    stock_items = parse_shop_stock(raw_products, pos_shop_id)
    # API trả sản phẩm nhưng không còn variation nào → tồn POS shop = rỗng
    if not stock_items:
        cur = conn.execute("DELETE FROM shop_inventory WHERE shop_id=?", (shop_db_id,))
        removed0 = int(cur.rowcount or 0)
        return {
            "synced": 0, "errors": [], "skipped": 0, "removed": removed0,
            "new_products": 0, "new_sku_list": [],
        }

    synced = 0
    skipped = 0
    new_products = 0
    new_sku_list = []
    seen_product_ids: set[int] = set()
    # Tồn POS theo sản phẩm = TỔNG qty của TẤT CẢ biến thể (stock_items là
    # per-biến-thể — SP đa mẫu mã có N item cùng product_id, phải cộng dồn).
    qty_pos_by_product: dict[int, int] = {}
    now = now_hcm().strftime("%Y-%m-%d %H:%M:%S")
    pg = conn._pg  # raw psycopg2 connection để dùng SAVEPOINT

    for idx, item in enumerate(stock_items):
        sku = item["sku"]
        variation_id = item["variation_id"]
        product_name = item["product_name"]
        sp = f"sp_{idx}"

        # Savepoint per-item: lỗi 1 item không phá vỡ cả transaction
        with pg.cursor() as _c:
            _c.execute(f"SAVEPOINT {sp}")
        try:
            # ── LOOKUP CHIẾN LƯỢC MỚI (2026-05-11, fix triệt để fake product) ──
            # Trước đây lookup `wh_products.pos_variation_id=variation_id` (cột legacy)
            # → dính fake products cũ giữ pvid bừa bãi → sync tạo lại fake mỗi 2h.
            # Giờ thứ tự:
            #   1. wh_variation_map.pos_variation_id → product_id (single source of truth)
            #   2. wh_products WHERE UPPER(sku) = UPPER(prod.custom_id) (POS SKU thực)
            #   3. CHỈ tạo product MỚI nếu prod.custom_id hợp lệ (không phải tên biến thể)
            #   KHÔNG fallback theo _get_sku() (có thể trả display_id → tạo fake).
            pos_prod_custom = (item.get("pos_product_custom_id") or "").strip()
            prod_row = None

            # 1. vmap lookup (truth source)
            _vm = conn.execute(
                "SELECT product_id FROM wh_variation_map WHERE pos_variation_id=?",
                (variation_id,)
            ).fetchone()
            if _vm and _vm.get("product_id"):
                prod_row = conn.execute(
                    "SELECT id, sku FROM products WHERE id=?", (_vm["product_id"],)
                ).fetchone()

            # 2. Match theo prod.custom_id (POS SKU thực của product cha)
            if not prod_row and pos_prod_custom:
                prod_row = conn.execute(
                    "SELECT id, sku FROM products WHERE UPPER(sku)=UPPER(?)", (pos_prod_custom,)
                ).fetchone()

            if prod_row:
                prod_db_id = prod_row["id"]
                old_sku = str(prod_row.get("sku") or "")
                # CHỈ update tên + pos_variation_id (legacy column) khi PVID này là pvid
                # ĐẦU TIÊN của product (single-variant). Multi-variant: column này
                # giữ pvid của 1 biến thể bất kỳ → vô nghĩa, dùng vmap thay.
                # Sync SKU nếu cũ xấu (POS-UUID).
                if _is_bad_sku(old_sku) and pos_prod_custom and not _is_bad_sku(pos_prod_custom):
                    conn.execute(
                        "UPDATE products SET sku=?, name=? WHERE id=?",
                        (pos_prod_custom, product_name, prod_db_id)
                    )
                else:
                    conn.execute(
                        "UPDATE products SET name=? WHERE id=?",
                        (product_name, prod_db_id)
                    )
            else:
                # 3. Tạo product MỚI — CHỈ KHI prod.custom_id hợp lệ (không phải tên biến thể)
                # Tránh tạo fake SKU="MÀU ĐEN", "MẪU 1", v.v.
                _create_sku = pos_prod_custom if pos_prod_custom and not _is_bad_sku(pos_prod_custom) else None
                if not _create_sku:
                    # Không có custom_id hợp lệ → skip, log
                    with pg.cursor() as _c:
                        _c.execute(f"RELEASE SAVEPOINT {sp}")
                    skipped += 1
                    continue
                _existed = conn.execute(
                    "SELECT id FROM products WHERE UPPER(sku)=UPPER(?)", (_create_sku,)
                ).fetchone()
                conn.execute("""
                    INSERT INTO products (sku, name, pos_product_id, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(sku) DO UPDATE SET name=EXCLUDED.name
                """, (_create_sku, product_name, item["pos_product_id"], now))
                prod_row = conn.execute(
                    "SELECT id FROM products WHERE UPPER(sku)=UPPER(?)", (_create_sku,)
                ).fetchone()
                if not prod_row:
                    with pg.cursor() as _c:
                        _c.execute(f"RELEASE SAVEPOINT {sp}")
                    skipped += 1
                    continue
                prod_db_id = prod_row["id"]
                if not _existed:
                    new_products += 1
                    new_sku_list.append({"sku": _create_sku, "name": product_name})

            # Dòng API này đã gắn được product_id — thêm vào tập kỳ vọng TRƯỚC khi ghi DB
            # (nếu bước sau lỗi, vẫn không xóa nhầm tồn cho SP còn trên POS)
            seen_product_ids.add(int(prod_db_id))

            # Ghi variation UUID → product mapping + variant_name + tồn POS
            if variation_id:
                var_name_val = item.get("var_name") or ""
                is_locked = bool(item.get("is_locked"))
                # Locked = shop đã TẮT biến thể này trên Pancake → tồn POS coi như 0
                # (không tính vào số bán). Giữ is_locked để gỡ badge + ẩn khi mọi shop tắt.
                pos_qty = 0 if is_locked else int(item.get("qty") or 0)
                conn.execute("""
                    INSERT INTO wh_variation_map (pos_variation_id, product_id, variant_name, pos_remain_qty, pos_remain_updated_at, is_locked)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (pos_variation_id) DO UPDATE SET
                        product_id          = EXCLUDED.product_id,
                        variant_name        = CASE WHEN EXCLUDED.variant_name != ''
                                                   THEN EXCLUDED.variant_name
                                                   ELSE wh_variation_map.variant_name END,
                        pos_remain_qty      = EXCLUDED.pos_remain_qty,
                        pos_remain_updated_at = EXCLUDED.pos_remain_updated_at,
                        is_locked           = EXCLUDED.is_locked
                """, (variation_id, prod_db_id, var_name_val, pos_qty, now, is_locked))

            # Cập nhật giá nhập / giá bán từ POS vào wh_products
            gia_nhap = item.get("gia_nhap", 0) or 0
            gia_ban  = item.get("gia_ban",  0) or 0
            if gia_nhap > 0 or gia_ban > 0:
                conn.execute(
                    "UPDATE products SET gia_nhap=?, gia_ban=? WHERE id=?",
                    (gia_nhap, gia_ban, prod_db_id)
                )

            # Thuế SP (Pancake categories) — CHỈ ghi khi có giá trị, tránh shop khác
            # (không set thuế) đồng bộ cùng SKU rồi xoá trắng thuế đã có.
            _ptax = (item.get("product_tax") or "").strip()
            if _ptax:
                conn.execute("UPDATE products SET product_tax=? WHERE id=?", (_ptax, prod_db_id))

            # Cộng dồn tồn biến thể này vào tổng của sản phẩm — chỉ khi item
            # xử lý trọn vẹn (nếu lỗi giữa chừng thì rollback savepoint, không cộng).
            # Biến thể LOCKED (shop đã tắt) KHÔNG cộng vào tồn POS. Vẫn setdefault(0) để
            # SP all-locked được ghi shop_inventory=0 (không giữ số stale của lần sync trước).
            _pid_i = int(prod_db_id)
            qty_pos_by_product.setdefault(_pid_i, 0)
            if not item.get("is_locked"):
                qty_pos_by_product[_pid_i] += int(item["qty"] or 0)

            with pg.cursor() as _c:
                _c.execute(f"RELEASE SAVEPOINT {sp}")
            synced += 1

        except Exception as _e:
            with pg.cursor() as _c:
                _c.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                _c.execute(f"RELEASE SAVEPOINT {sp}")
            if skipped < 3:
                import sys
                print(f"  [skip] idx={idx} sku={sku!r}: {type(_e).__name__}: {_e}", file=sys.stderr)
            skipped += 1

    # Ghi tồn POS MỘT LẦN mỗi (shop, sản phẩm) = tổng tồn mọi biến thể.
    # KHÔNG upsert trong vòng lặp per-biến-thể: ON CONFLICT ghi đè lẫn nhau
    # → SP đa mẫu mã chỉ còn tồn của biến thể cuối (bug tồn POS 2026-06-11).
    for _pid, _qty in qty_pos_by_product.items():
        conn.execute("""
            INSERT INTO shop_inventory (shop_id, product_id, qty_pos, synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(shop_id, product_id) DO UPDATE SET
                qty_pos=EXCLUDED.qty_pos,
                synced_at=EXCLUDED.synced_at
        """, (shop_db_id, _pid, _qty, now))

    # Xóa dòng tồn POS cũ: sản phẩm không còn xuất hiện trong phản hồi API lần này
    removed = 0
    if seen_product_ids:
        ids = sorted(seen_product_ids)
        ph = ",".join(["?"] * len(ids))
        cur = conn.execute(
            f"DELETE FROM shop_inventory WHERE shop_id=? AND product_id NOT IN ({ph})",
            [shop_db_id, *ids],
        )
        removed = int(cur.rowcount or 0)

    # Trả về pvid POS đã thấy ở shop này — caller dùng để cleanup ghost vmap
    seen_pvids = {item["variation_id"] for item in stock_items if item.get("variation_id")}
    return {
        "synced": synced, "errors": [], "skipped": skipped, "removed": removed,
        "new_products": new_products, "new_sku_list": new_sku_list,
        "seen_pvids": seen_pvids, "seen_product_ids": seen_product_ids,
    }


def reconcile_locked_inventory(conn, product_ids) -> dict:
    """Part B — gộp tồn vật lý sau khi sync cập nhật is_locked.

    Với SP còn ĐÚNG 1 biến thể đang bán (is_locked=false): gộp mọi dòng tồn lẻ
    (biến thể đã tắt + dòng product-level NULL) về biến thể active đó, theo TỪNG kho.
    Tổng tồn KHÔNG đổi → fix "đơn shop B quét không thấy tồn" khi chuyển shop.

    SP còn >1 biến thể active (đa biến thể thật, vd nhiều màu): KHÔNG tự gộp (máy không
    biết màu nào = màu nào, §16) — chỉ ghi cảnh báo nếu có tồn lạc (variant_key NULL
    hoặc không khớp biến thể active) để người duyệt.

    An toàn: idempotent (chạy lại không đổi gì khi đã sạch), chỉ đụng SP có lệch,
    ghi wh_stock_movements truy t('manual_adjust', by 'sync-lock').
    Trả về {merged, warnings:[{product_id, orphan_qty, active_variants}]}.
    """
    merged = 0
    warnings = []
    if not product_ids:
        return {"merged": 0, "warnings": []}
    # Lọc ứng viên: chỉ SP có biến thể locked HOẶC tồn bị phân mảnh (>1 variant_key / có NULL)
    cand = conn.execute(
        "SELECT DISTINCT p.id FROM wh_products p "
        "WHERE p.id = ANY(%s) AND ("
        "  EXISTS (SELECT 1 FROM wh_variation_map v WHERE v.product_id=p.id AND v.is_locked=true)"
        "  OR (SELECT COUNT(DISTINCT COALESCE(variant_key,'∅')) FROM wh_inventory i WHERE i.product_id=p.id) > 1"
        ")",
        (list(product_ids),)
    ).fetchall()
    for row in cand:
        pid = row["id"]
        active_names = [r["variant_name"] for r in conn.execute(
            "SELECT DISTINCT variant_name FROM wh_variation_map "
            "WHERE product_id=%s AND COALESCE(is_locked,false)=false AND COALESCE(variant_name,'')<>''",
            (pid,)
        ).fetchall()]
        inv = conn.execute(
            "SELECT id, warehouse_id, variant_key, qty FROM wh_inventory WHERE product_id=%s",
            (pid,)
        ).fetchall()
        if not inv:
            continue

        if len(active_names) == 1:
            target = active_names[0]
            by_wh = {}
            for r in inv:
                by_wh.setdefault(r["warehouse_id"], []).append(r)
            for wh, rows in by_wh.items():
                others = [r for r in rows if (r["variant_key"] or None) != target]
                if not others:
                    continue
                extra = sum(int(r["qty"] or 0) for r in others)
                tgt = next((r for r in rows if (r["variant_key"] or None) == target), None)
                if tgt:
                    before = int(tgt["qty"] or 0)
                    new_qty = before + extra
                    conn.execute("UPDATE wh_inventory SET qty=%s WHERE id=%s", (new_qty, tgt["id"]))
                else:
                    before = 0
                    new_qty = extra
                    conn.execute(
                        "INSERT INTO wh_inventory (product_id, warehouse_id, qty, variant_key) "
                        "VALUES (%s,%s,%s,%s)", (pid, wh, extra, target)
                    )
                for r in others:
                    conn.execute("DELETE FROM wh_inventory WHERE id=%s", (r["id"],))
                conn.execute(
                    "INSERT INTO wh_stock_movements "
                    "(type, product_id, warehouse_id, qty, qty_before, qty_after, note, created_by, created_at, status) "
                    "VALUES ('manual_adjust',%s,%s,%s,%s,%s,%s,'sync-lock',NOW()::text,'active')",
                    (pid, wh, extra, before, new_qty,
                     f"Auto-gộp tồn lẻ về biến thể đang bán '{target}' (shop khác đã tắt SP trên Pancake). Tổng không đổi.")
                )
                merged += 1
        elif len(active_names) > 1:
            orphan = sum(int(r["qty"] or 0) for r in inv
                         if (r["variant_key"] or None) is None or r["variant_key"] not in active_names)
            if orphan > 0:
                warnings.append({"product_id": pid, "orphan_qty": orphan,
                                 "active_variants": active_names})
    return {"merged": merged, "warnings": warnings}


def sync_all_shops(conn, shops: list) -> dict:
    """Sync tất cả shop active."""
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from pancake_auth import is_valid_hex_api_key as _is_valid
    except Exception:
        _is_valid = lambda k: bool(k and len(str(k).strip()) == 32)
    total_synced = 0
    results = []
    global_pvids: set = set()       # tất cả pvid POS đã thấy ở mọi shop
    global_product_ids: set = set() # tất cả product_id đã sync
    any_error = False
    for shop in shops:
        api_key = str(shop.get("pos_api_key") or "").strip()
        if not _is_valid(api_key):
            any_error = True  # shop không sync được → KHÔNG cleanup ghost (safe)
            results.append({
                "shop": shop["shop_name"],
                "synced": 0,
                "error": "Chưa có api_key hợp lệ — vào Cài đặt để nhập",
            })
            continue
        result = sync_shop_inventory_to_db(
            conn,
            shop_db_id=shop["id"],
            pos_shop_id=shop["pos_shop_id"],
            api_key=api_key,
        )
        total_synced += result["synced"]
        if result.get("errors"):
            any_error = True
        global_pvids.update(result.get("seen_pvids") or set())
        global_product_ids.update(result.get("seen_product_ids") or set())
        results.append({
            "shop": shop["shop_name"],
            "synced": result["synced"],
            "error": result["errors"][0] if result["errors"] else None,
        })

    # GHOST CLEANUP — chỉ chạy khi TẤT CẢ shop sync OK.
    # Logic: pvid không có trong API global → POS đã xóa biến thể.
    #   - Nếu inv qty=0 (không có hàng vật lý) → DELETE vmap (mẫu mã biến mất khỏi UI)
    #   - Nếu inv qty>0 (hàng vật lý còn) → set pos_remain_qty=0 (POS=0 nhưng VL còn)
    #   - Nếu còn đơn active → skip (an toàn)
    ghost_deleted = 0; ghost_zeroed = 0
    if not any_error and global_pvids and global_product_ids:
        pids = list(global_product_ids)
        pvids_list = list(global_pvids)
        ghosts = conn.execute(
            "SELECT v.pos_variation_id, "
            "(SELECT COALESCE(SUM(qty),0)::int FROM wh_inventory WHERE pos_variation_id=v.pos_variation_id) AS inv_qty "
            "FROM wh_variation_map v "
            "WHERE v.product_id = ANY(%s) AND NOT (v.pos_variation_id = ANY(%s))",
            (pids, pvids_list)
        ).fetchall()
        for g in ghosts:
            pvid = g["pos_variation_id"]
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM wh_outbound_requests "
                "WHERE pos_variation_id=%s AND status NOT IN ('confirmed','auto_cleaned','cancelled')",
                (pvid,)
            ).fetchone()
            if active and active["c"] > 0:
                continue
            if g["inv_qty"] == 0:
                # Không hàng vật lý → DELETE vmap (mẫu mã biến mất khỏi UI)
                conn.execute("DELETE FROM wh_inventory WHERE pos_variation_id=%s", (pvid,))
                conn.execute("DELETE FROM wh_variation_map WHERE pos_variation_id=%s", (pvid,))
                ghost_deleted += 1
            else:
                # Hàng vật lý còn → set POS qty=0 nhưng giữ vmap + inv (NV cần biết)
                conn.execute(
                    "UPDATE wh_variation_map SET pos_remain_qty=0, pos_remain_updated_at=NOW()::text "
                    "WHERE pos_variation_id=%s", (pvid,)
                )
                ghost_zeroed += 1

    # Part B — gộp tồn vật lý cho SP có biến thể bị tắt (chuyển shop). Chỉ khi sync sạch.
    reconcile = {"merged": 0, "warnings": []}
    if not any_error and global_product_ids:
        reconcile = reconcile_locked_inventory(conn, global_product_ids)

    return {"total_synced": total_synced, "shops": results,
            "ghost_deleted": ghost_deleted, "ghost_zeroed": ghost_zeroed,
            "locked_merged": reconcile["merged"], "locked_warnings": reconcile["warnings"]}
