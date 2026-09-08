-- 062: Đánh giá Page Test theo TỪNG NGÀY (page + ngày) thay vì theo page.
-- Trước (061): 1 đánh giá/page → bấm Đạt 1 ngày là cả 4 ngày sáng "Đạt".
-- Sau: PK = (page_id, entry_date) → mỗi ngày 1 đánh giá độc lập; ngày chưa đánh thì trống.
-- SP Win lấy SP của đúng ngày bấm Đạt; chi phí vẫn cộng đủ cả kỳ (suy ở tầng app).
ALTER TABLE page_test_eval ADD COLUMN IF NOT EXISTS entry_date DATE;

-- Backfill đánh giá cũ (giữ lại, không mất): ưu tiên win_date (ngày đã bấm Đạt từ 061),
-- rồi ngày test gần nhất của page, cuối cùng là ngày cập nhật đánh giá.
UPDATE page_test_eval SET entry_date = win_date
 WHERE entry_date IS NULL AND win_date IS NOT NULL;
UPDATE page_test_eval e
   SET entry_date = sub.maxd
  FROM (SELECT page_id, MAX(entry_date) AS maxd FROM page_test_entries GROUP BY page_id) sub
 WHERE e.entry_date IS NULL AND e.page_id = sub.page_id;
UPDATE page_test_eval
   SET entry_date = COALESCE(updated_at::date, CURRENT_DATE)
 WHERE entry_date IS NULL;

ALTER TABLE page_test_eval ALTER COLUMN entry_date SET NOT NULL;
ALTER TABLE page_test_eval DROP CONSTRAINT IF EXISTS page_test_eval_pkey;
ALTER TABLE page_test_eval ADD PRIMARY KEY (page_id, entry_date);
