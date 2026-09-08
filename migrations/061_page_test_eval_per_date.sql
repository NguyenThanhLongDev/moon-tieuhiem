-- 061: Bổ sung sản phẩm WIN cho đánh giá Page Test.
-- Giữ nguyên logic cũ (đánh giá theo page, tổng cộng cả page). CHỈ thêm: lưu sản phẩm
-- của đúng dòng lúc bấm "Đạt" → tab SP Win hiển thị đúng sản phẩm đó thay vì gộp hết
-- sản phẩm mọi ngày. Khóa chính vẫn là page_id.
ALTER TABLE page_test_eval ADD COLUMN IF NOT EXISTS win_product TEXT;
ALTER TABLE page_test_eval ADD COLUMN IF NOT EXISTS win_date    DATE;
