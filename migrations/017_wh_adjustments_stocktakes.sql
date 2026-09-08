-- Phase 5: Phiếu điều chỉnh tồn + kiểm kho cơ bản (chỉ ghi tồn qua ledger).
BEGIN;

CREATE TABLE IF NOT EXISTS wh_adjustments (
    id                  BIGSERIAL PRIMARY KEY,
    warehouse_id        BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    adjustment_code     VARCHAR(64) NOT NULL UNIQUE,
    adjustment_type     VARCHAR(8) NOT NULL,
    reason_code         VARCHAR(32) NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'draft',
    note                TEXT,
    created_by          VARCHAR(128),
    confirmed_by        VARCHAR(128),
    confirmed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_adjustments_type_chk CHECK (adjustment_type IN ('up', 'down')),
    CONSTRAINT wh_adjustments_reason_chk CHECK (reason_code IN (
        'mat_hang', 'hu_hong', 'kiem_kho_chenh_lech', 'nhap_bu', 'khac'
    )),
    CONSTRAINT wh_adjustments_status_chk CHECK (status IN ('draft', 'confirmed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_wh_adjustments_wh
    ON wh_adjustments (warehouse_id);
CREATE INDEX IF NOT EXISTS idx_wh_adjustments_status_created
    ON wh_adjustments (status, created_at DESC);

CREATE TABLE IF NOT EXISTS wh_adjustment_items (
    id                  BIGSERIAL PRIMARY KEY,
    adjustment_id       BIGINT NOT NULL REFERENCES wh_adjustments(id) ON DELETE CASCADE,
    sku                 VARCHAR(255) NOT NULL,
    qty                 BIGINT NOT NULL,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_adjustment_items_qty_chk CHECK (qty > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_adjustment_items_adj_sku
    ON wh_adjustment_items (adjustment_id, sku);

CREATE INDEX IF NOT EXISTS idx_wh_adjustment_items_adj
    ON wh_adjustment_items (adjustment_id);

CREATE TABLE IF NOT EXISTS wh_stocktakes (
    id                  BIGSERIAL PRIMARY KEY,
    warehouse_id        BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    stocktake_code      VARCHAR(64) NOT NULL UNIQUE,
    status              VARCHAR(16) NOT NULL DEFAULT 'draft',
    note                TEXT,
    created_by          VARCHAR(128),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_stocktakes_status_chk CHECK (status IN ('draft', 'completed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_wh_stocktakes_wh
    ON wh_stocktakes (warehouse_id);

CREATE TABLE IF NOT EXISTS wh_stocktake_items (
    id                  BIGSERIAL PRIMARY KEY,
    stocktake_id        BIGINT NOT NULL REFERENCES wh_stocktakes(id) ON DELETE CASCADE,
    sku                 VARCHAR(255) NOT NULL,
    counted_qty         BIGINT NOT NULL,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_stocktake_items_qty_chk CHECK (counted_qty >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wh_stocktake_items_st_sku
    ON wh_stocktake_items (stocktake_id, sku);

CREATE INDEX IF NOT EXISTS idx_wh_stocktake_items_st
    ON wh_stocktake_items (stocktake_id);

COMMIT;
