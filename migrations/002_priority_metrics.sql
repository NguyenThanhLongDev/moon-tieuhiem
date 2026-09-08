-- Phase B: priority business tables only.
-- Scope: orders, order_items, daily_shop_metrics
-- Safe mode: no route switch, no runtime behavior change.

BEGIN;

-- 1) Enums required by orders
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status_type') THEN
        CREATE TYPE order_status_type AS ENUM (
            'new',
            'confirmed',
            'shipping',
            'delivered',
            'cancelled',
            'returned',
            'unknown'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status_type') THEN
        CREATE TYPE payment_status_type AS ENUM (
            'unpaid',
            'partial',
            'paid',
            'refunded',
            'unknown'
        );
    END IF;
END$$;

-- 2) Orders (normalized transaction header)
CREATE TABLE IF NOT EXISTS orders (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    external_order_id   VARCHAR(100) NOT NULL,
    order_code          VARCHAR(100),
    customer_name       VARCHAR(255),
    customer_phone      VARCHAR(50),
    order_status        order_status_type NOT NULL DEFAULT 'unknown',
    payment_status      payment_status_type NOT NULL DEFAULT 'unknown',

    created_at_pos      TIMESTAMP NOT NULL,
    confirmed_at        TIMESTAMP NULL,
    shipped_at          TIMESTAMP NULL,
    delivered_at        TIMESTAMP NULL,
    cancelled_at        TIMESTAMP NULL,

    subtotal_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    shipping_fee        NUMERIC(18,2) NOT NULL DEFAULT 0,
    other_fee           NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_amount        NUMERIC(18,2) NOT NULL DEFAULT 0,
    net_revenue         NUMERIC(18,2) NOT NULL DEFAULT 0,
    pos_profit_loss     NUMERIC(18,2) NULL,

    raw_payload_json    JSONB,
    synced_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (shop_id, external_order_id)
);

-- 3) Order items (line-level)
CREATE TABLE IF NOT EXISTS order_items (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id          BIGINT NULL,
    variant_id          BIGINT NULL,

    external_product_id VARCHAR(100),
    external_variant_id VARCHAR(100),
    product_name        VARCHAR(255) NOT NULL,
    variant_name        VARCHAR(255),
    sku                 VARCHAR(100),
    quantity            NUMERIC(18,2) NOT NULL DEFAULT 0,
    unit_price          NUMERIC(18,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    line_total          NUMERIC(18,2) NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 4) Daily shop metrics (pre-aggregated reporting)
CREATE TABLE IF NOT EXISTS daily_shop_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    metric_date         DATE NOT NULL,

    order_count         INTEGER NOT NULL DEFAULT 0,
    new_order_count     INTEGER NOT NULL DEFAULT 0,
    confirmed_count     INTEGER NOT NULL DEFAULT 0,
    shipping_count      INTEGER NOT NULL DEFAULT 0,
    delivered_count     INTEGER NOT NULL DEFAULT 0,
    returned_count      INTEGER NOT NULL DEFAULT 0,
    cancelled_count     INTEGER NOT NULL DEFAULT 0,

    gross_revenue       NUMERIC(18,2) NOT NULL DEFAULT 0,
    net_revenue         NUMERIC(18,2) NOT NULL DEFAULT 0,
    ads_cost            NUMERIC(18,2) NOT NULL DEFAULT 0,
    pos_profit_loss     NUMERIC(18,2) NOT NULL DEFAULT 0,
    loss_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    inventory_value     NUMERIC(18,2) NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (shop_id, metric_date)
);

-- 5) Indexes
CREATE INDEX IF NOT EXISTS idx_orders_shop_created_at
    ON orders(shop_id, created_at_pos);
CREATE INDEX IF NOT EXISTS idx_orders_created_at_pos
    ON orders(created_at_pos);
CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(order_status);
CREATE INDEX IF NOT EXISTS idx_orders_payment_status
    ON orders(payment_status);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id
    ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_shop_id
    ON order_items(shop_id);

CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_shop_date
    ON daily_shop_metrics(shop_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_date
    ON daily_shop_metrics(metric_date);
CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_loss_flag
    ON daily_shop_metrics(loss_flag);

-- 6) updated_at triggers for new tables
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_orders_updated_at') THEN
        CREATE TRIGGER trg_orders_updated_at
        BEFORE UPDATE ON orders
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_order_items_updated_at') THEN
        CREATE TRIGGER trg_order_items_updated_at
        BEFORE UPDATE ON order_items
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_daily_shop_metrics_updated_at') THEN
        CREATE TRIGGER trg_daily_shop_metrics_updated_at
        BEFORE UPDATE ON daily_shop_metrics
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

COMMIT;
