-- Rollback 043
DROP INDEX IF EXISTS idx_bi_missing_date;
-- Cảnh báo: nếu có row for_date NULL thì lệnh dưới sẽ fail.
-- DELETE thủ công hoặc fill ngày trước khi rollback.
ALTER TABLE budget_items ALTER COLUMN for_date SET NOT NULL;
