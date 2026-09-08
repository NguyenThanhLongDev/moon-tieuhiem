-- 059: Bảng nhập liệu Page Test (thay Excel "BẢNG TỔNG HỢP SẢN PHẨM TEST")
-- Mỗi dòng = 1 ngày × 1 page × 1 sản phẩm. Chỉ chứa phần NV nhập tay
-- (sản phẩm, giá bán, đơn, số lượng, DT tuỳ chỉnh). Spend/VAT/TK/NV/team
-- join đọc-only từ bảng có sẵn; trạng thái Test/Win dùng chung
-- fb_page_auto_phan_loai với trang Chi phí QC.

CREATE TABLE IF NOT EXISTS page_test_entries (
    id               BIGSERIAL PRIMARY KEY,
    entry_date       DATE          NOT NULL,
    page_id          VARCHAR(64)   NOT NULL,
    page_name        TEXT          NOT NULL DEFAULT '',
    product_name     TEXT          NOT NULL,
    gia_ban          NUMERIC(14,0) NOT NULL DEFAULT 0,
    don_hang         INTEGER       NOT NULL DEFAULT 0,
    so_luong         INTEGER       NOT NULL DEFAULT 0,
    doanh_thu_custom NUMERIC(14,0),
    note             TEXT,
    created_by       VARCHAR(64)   NOT NULL DEFAULT '',
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    UNIQUE (entry_date, page_id, product_name)
);

CREATE INDEX IF NOT EXISTS idx_pte_date ON page_test_entries (entry_date);
CREATE INDEX IF NOT EXISTS idx_pte_page ON page_test_entries (page_id);
