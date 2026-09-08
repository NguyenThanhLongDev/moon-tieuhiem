-- Rollback 061: bỏ cột sản phẩm win.
ALTER TABLE page_test_eval DROP COLUMN IF EXISTS win_product;
ALTER TABLE page_test_eval DROP COLUMN IF EXISTS win_date;
