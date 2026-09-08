-- Rollback 061
ALTER TABLE mb_order_attribution DROP COLUMN IF EXISTS ad_source_origin;
DROP TABLE IF EXISTS mb_attribution_inbox;
