-- 038_stocktake_items_variant.sql
-- Thêm cột variant_key + pos_variation_id vào wh_stocktake_items để hỗ trợ kiểm kho per-biến thể.
-- Mỗi SKU có thể có nhiều mẫu mã (vd: "Màu Đen", "Màu Trắng") — trước đây 1 row/product là sai.

ALTER TABLE wh_stocktake_items
    ADD COLUMN IF NOT EXISTS variant_key VARCHAR(255),
    ADD COLUMN IF NOT EXISTS pos_variation_id VARCHAR(64);

-- Đổi UNIQUE: từ (stocktake_id, product_id) → (stocktake_id, product_id, COALESCE(pos_variation_id,''))
-- Lưu ý: UNIQUE cũ đang là INDEX (không phải CONSTRAINT) — dùng DROP INDEX.
ALTER TABLE wh_stocktake_items
    DROP CONSTRAINT IF EXISTS uq_wh_stocktake_items_st_product;
DROP INDEX IF EXISTS uq_wh_stocktake_items_st_product;

CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_stocktake_items_st_product_variant
    ON wh_stocktake_items (stocktake_id, product_id, COALESCE(pos_variation_id, ''));
