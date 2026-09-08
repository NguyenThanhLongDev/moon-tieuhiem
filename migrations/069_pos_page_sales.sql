-- 069: thêm cột tiền hàng (sales, trước chiết khấu) cho pos_page_daily_metrics
ALTER TABLE pos_page_daily_metrics ADD COLUMN IF NOT EXISTS sales NUMERIC DEFAULT 0;
