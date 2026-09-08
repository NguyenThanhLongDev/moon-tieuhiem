BEGIN;

CREATE TABLE IF NOT EXISTS wh_stocktakes (
    id              BIGSERIAL PRIMARY KEY,
    stocktake_code  VARCHAR(64) NOT NULL UNIQUE,
    status          VARCHAR(16) NOT NULL DEFAULT 'draft',
    note            TEXT,
    created_by      VARCHAR(128),
    confirmed_by    VARCHAR(128),
    confirmed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_stocktakes_status_chk CHECK (status IN ('draft', 'confirmed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS wh_stocktake_items (
    id              BIGSERIAL PRIMARY KEY,
    stocktake_id    BIGINT NOT NULL REFERENCES wh_stocktakes(id) ON DELETE CASCADE,
    product_id      BIGINT NOT NULL,
    sku             VARCHAR(255) NOT NULL,
    product_name    TEXT NOT NULL,
    unit            VARCHAR(64),
    category        VARCHAR(128),
    qty_system      BIGINT NOT NULL DEFAULT 0,
    qty_pos         BIGINT NOT NULL DEFAULT 0,
    qty_counted     BIGINT NOT NULL DEFAULT 0,
    diff            BIGINT NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_stocktake_items_qty_chk CHECK (qty_counted >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_stocktake_items_st_product
    ON wh_stocktake_items (stocktake_id, product_id);

CREATE INDEX IF NOT EXISTS idx_wh_stocktake_items_st
    ON wh_stocktake_items (stocktake_id);

COMMIT;
