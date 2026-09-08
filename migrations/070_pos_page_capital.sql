-- 070: thêm giá vốn (capital) cho pos_page_daily_metrics
ALTER TABLE pos_page_daily_metrics ADD COLUMN IF NOT EXISTS capital NUMERIC DEFAULT 0;
