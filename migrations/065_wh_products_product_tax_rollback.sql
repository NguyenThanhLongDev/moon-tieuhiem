-- Rollback 065: bỏ cột product_tax khỏi wh_products.
ALTER TABLE wh_products DROP COLUMN IF EXISTS product_tax;
