-- Phase 6: Đối soát POS vs kho nội bộ (chỉ đọc + lưu snapshot, không push ngược POS).
BEGIN;

CREATE TABLE IF NOT EXISTS wh_reconciliation_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    scope_type      VARCHAR(32) NOT NULL DEFAULT 'shop_warehouse',
    shop_id         VARCHAR(64) NOT NULL,
    warehouse_id    BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    status          VARCHAR(16) NOT NULL DEFAULT 'draft',
    summary_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_reconciliation_runs_status_chk CHECK (status IN ('draft', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_wh_recon_runs_lookup
    ON wh_reconciliation_runs (run_date DESC, shop_id, warehouse_id, created_at DESC);

CREATE TABLE IF NOT EXISTS wh_reconciliation_items (
    id                          BIGSERIAL PRIMARY KEY,
    run_id                      BIGINT NOT NULL REFERENCES wh_reconciliation_runs(id) ON DELETE CASCADE,
    shop_id                     VARCHAR(64) NOT NULL,
    warehouse_id                BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    sku                         VARCHAR(255) NOT NULL,
    pos_qty                     BIGINT,
    system_qty                  BIGINT NOT NULL DEFAULT 0,
    physical_qty                BIGINT,
    diff_pos_vs_system          BIGINT,
    diff_system_vs_physical     BIGINT,
    status                      VARCHAR(24) NOT NULL,
    note                        TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_reconciliation_items_status_chk CHECK (status IN (
        'match', 'mismatch', 'pending_physical', 'exception'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_recon_items_run
    ON wh_reconciliation_items (run_id);

CREATE INDEX IF NOT EXISTS idx_wh_recon_items_run_sku
    ON wh_reconciliation_items (run_id, sku);

COMMIT;
