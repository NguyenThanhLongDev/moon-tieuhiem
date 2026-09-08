-- Phase 3: Hàng hoàn — tín hiệu POS ≠ nhận kho thực tế; chỉ cộng tồn khi kho xác nhận received_good_qty.
BEGIN;

CREATE TABLE IF NOT EXISTS wh_return_receipts (
    id                      BIGSERIAL PRIMARY KEY,
    shop_id                 VARCHAR(64) NOT NULL,
    warehouse_id            BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    order_code              VARCHAR(128),
    return_ref_external     VARCHAR(128) NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'pending',
    signaled_at             TIMESTAMPTZ,
    received_at             TIMESTAMPTZ,
    note                    TEXT,
    raw_payload_json        JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, return_ref_external),
    CONSTRAINT wh_return_receipts_status_chk CHECK (status IN (
        'pending', 'received_ok', 'received_partial', 'received_damaged', 'exception'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_return_receipts_shop_signaled
    ON wh_return_receipts (shop_id, signaled_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_wh_return_receipts_status
    ON wh_return_receipts (status);

-- good_ledger_posted_qty: SL tốt đã cộng vào tồn (idempotent)
CREATE TABLE IF NOT EXISTS wh_return_receipt_items (
    id                      BIGSERIAL PRIMARY KEY,
    return_receipt_id       BIGINT NOT NULL REFERENCES wh_return_receipts(id) ON DELETE CASCADE,
    sku                     VARCHAR(255) NOT NULL,
    product_name            TEXT NOT NULL,
    expected_qty            BIGINT NOT NULL,
    received_good_qty       BIGINT NOT NULL DEFAULT 0,
    received_damaged_qty    BIGINT NOT NULL DEFAULT 0,
    missing_qty             BIGINT NOT NULL DEFAULT 0,
    good_ledger_posted_qty  BIGINT NOT NULL DEFAULT 0,
    line_status             VARCHAR(32) NOT NULL DEFAULT 'pending',
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_return_receipt_items_line_status_chk CHECK (line_status IN (
        'pending', 'ok', 'partial', 'exception'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_return_receipt_items_receipt
    ON wh_return_receipt_items (return_receipt_id);

COMMIT;
