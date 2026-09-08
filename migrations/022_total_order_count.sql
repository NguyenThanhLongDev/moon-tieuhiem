-- Migration 022: Add total_order_count to daily_shop_metrics
-- total_order_count = Pancake field "total_order_count" (ALL orders created that day)
-- vs order_count = only revenue/successful orders

BEGIN;

ALTER TABLE daily_shop_metrics
    ADD COLUMN IF NOT EXISTS total_order_count INTEGER NOT NULL DEFAULT 0;

COMMIT;
