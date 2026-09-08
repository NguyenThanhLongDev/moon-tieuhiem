-- Rollback for Phase B priority metrics migration.

BEGIN;

DROP TRIGGER IF EXISTS trg_daily_shop_metrics_updated_at ON daily_shop_metrics;
DROP TRIGGER IF EXISTS trg_order_items_updated_at ON order_items;
DROP TRIGGER IF EXISTS trg_orders_updated_at ON orders;

DROP INDEX IF EXISTS idx_daily_shop_metrics_loss_flag;
DROP INDEX IF EXISTS idx_daily_shop_metrics_date;
DROP INDEX IF EXISTS idx_daily_shop_metrics_shop_date;
DROP INDEX IF EXISTS idx_order_items_shop_id;
DROP INDEX IF EXISTS idx_order_items_order_id;
DROP INDEX IF EXISTS idx_orders_payment_status;
DROP INDEX IF EXISTS idx_orders_status;
DROP INDEX IF EXISTS idx_orders_created_at_pos;
DROP INDEX IF EXISTS idx_orders_shop_created_at;

DROP TABLE IF EXISTS daily_shop_metrics;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;

-- Keep enums to avoid breaking shared schema.
-- DROP TYPE IF EXISTS payment_status_type;
-- DROP TYPE IF EXISTS order_status_type;

COMMIT;
