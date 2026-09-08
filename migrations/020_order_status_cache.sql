-- Migration 020: Bảng cache aggregate order status per shop per date
-- Nhẹ hơn nhiều so với bảng orders (lưu từng đơn) - chỉ lưu aggregate counts
-- Được sync mỗi 10-15 phút từ Pancake API

BEGIN;

CREATE TABLE IF NOT EXISTS shop_order_status_cache (
    id               BIGSERIAL PRIMARY KEY,
    shop_id          BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    metric_date      DATE NOT NULL,
    new_orders       INTEGER NOT NULL DEFAULT 0,
    confirmed_orders INTEGER NOT NULL DEFAULT 0,
    sent_orders      INTEGER NOT NULL DEFAULT 0,
    received_orders  INTEGER NOT NULL DEFAULT 0,
    returning_orders INTEGER NOT NULL DEFAULT 0,
    returned_orders  INTEGER NOT NULL DEFAULT 0,
    cancelled_orders INTEGER NOT NULL DEFAULT 0,
    synced_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(shop_id, metric_date)
);

CREATE INDEX IF NOT EXISTS idx_shop_order_status_cache_date
    ON shop_order_status_cache(metric_date);

CREATE INDEX IF NOT EXISTS idx_shop_order_status_cache_shop_date
    ON shop_order_status_cache(shop_id, metric_date);

COMMIT;
