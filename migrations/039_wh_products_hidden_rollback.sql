DROP INDEX IF EXISTS idx_wh_products_hidden;
ALTER TABLE wh_products DROP COLUMN IF EXISTS hidden;
