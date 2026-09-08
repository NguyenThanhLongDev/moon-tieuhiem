-- Rollback 038
DROP INDEX IF EXISTS uq_wh_stocktake_items_st_product_variant;
ALTER TABLE wh_stocktake_items
    ADD CONSTRAINT uq_wh_stocktake_items_st_product UNIQUE (stocktake_id, product_id);
ALTER TABLE wh_stocktake_items DROP COLUMN IF EXISTS variant_key;
ALTER TABLE wh_stocktake_items DROP COLUMN IF EXISTS pos_variation_id;
