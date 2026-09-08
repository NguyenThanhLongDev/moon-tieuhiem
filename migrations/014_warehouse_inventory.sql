-- Module Kho hàng (warehouse): bảng nội bộ, tách khỏi tồn kho JSON/POS cũ.
BEGIN;

CREATE TABLE IF NOT EXISTS wh_warehouses (
    id          BIGSERIAL PRIMARY KEY,
    code        VARCHAR(64) NOT NULL UNIQUE,
    name        VARCHAR(255) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wh_inventory_ledger (
    id              BIGSERIAL PRIMARY KEY,
    warehouse_id    BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    sku             VARCHAR(255) NOT NULL,
    movement_type   VARCHAR(32) NOT NULL,
    qty_delta       BIGINT NOT NULL,
    qty_before      BIGINT NOT NULL,
    qty_after       BIGINT NOT NULL,
    source_system   VARCHAR(32) NOT NULL,
    source_ref_type VARCHAR(64),
    source_ref_id   VARCHAR(128),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_inventory_ledger_movement_type_chk CHECK (movement_type IN (
        'opening', 'inbound', 'outbound_confirmed', 'return_received', 'adjust_up', 'adjust_down'
    )),
    CONSTRAINT wh_inventory_ledger_source_system_chk CHECK (source_system IN (
        'POS', 'WAREHOUSE', 'SYSTEM', 'MANUAL'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_inventory_ledger_wh_created
    ON wh_inventory_ledger (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_wh_inventory_ledger_sku
    ON wh_inventory_ledger (sku);

CREATE TABLE IF NOT EXISTS wh_inventory_balance (
    id              BIGSERIAL PRIMARY KEY,
    warehouse_id    BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    sku             VARCHAR(255) NOT NULL,
    on_hand         BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (warehouse_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_wh_inventory_balance_wh
    ON wh_inventory_balance (warehouse_id);

COMMIT;
