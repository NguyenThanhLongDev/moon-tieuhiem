-- Rollback migration 020
BEGIN;
DROP TABLE IF EXISTS shop_order_status_cache CASCADE;
COMMIT;
