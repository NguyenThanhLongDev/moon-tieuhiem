-- 039: Thêm cột wh_products.hidden để ẩn SP khỏi danh sách mà KHÔNG xóa khỏi DB.
-- Lý do: SP đã ngừng kinh doanh / không còn ở POS nhưng vẫn có đơn hàng cũ tham chiếu (FK).
-- Hidden=true → trang /kho-vat-ly/san-pham không hiển thị dòng. Đơn + biến thể + lịch sử giữ nguyên.

ALTER TABLE wh_products
  ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_wh_products_hidden ON wh_products(hidden) WHERE hidden = true;
