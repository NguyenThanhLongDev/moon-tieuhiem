ALTER TABLE shop_order_status_cache
  ADD COLUMN IF NOT EXISTS total_active_returning INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_returned_all INT DEFAULT 0;
