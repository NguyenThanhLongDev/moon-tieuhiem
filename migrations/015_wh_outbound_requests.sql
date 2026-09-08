-- Phase 2: Sổ xuất bán — yêu cầu xuất từ đơn POS, trừ tồn module Kho hàng chỉ khi xác nhận.
BEGIN;

-- shop_id: mã shop Pancake (chuỗi, khớp shops.json / get_orders path), không phải shops.id DB admin
CREATE TABLE IF NOT EXISTS wh_outbound_requests (
    id                      BIGSERIAL PRIMARY KEY,
    shop_id                 VARCHAR(64) NOT NULL,
    warehouse_id            BIGINT NOT NULL REFERENCES wh_warehouses(id) ON DELETE RESTRICT,
    order_code              VARCHAR(128),
    order_id_external       VARCHAR(128) NOT NULL,
    order_status            VARCHAR(64),
    requested_at            TIMESTAMPTZ,
    carrier_picked_up_at    TIMESTAMPTZ,
    status                  VARCHAR(32) NOT NULL DEFAULT 'pending',
    note                    TEXT,
    raw_payload_json        JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, order_id_external),
    CONSTRAINT wh_outbound_requests_status_chk CHECK (status IN (
        'pending', 'partially_confirmed', 'confirmed', 'cancelled', 'exception'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_outbound_requests_shop_requested
    ON wh_outbound_requests (shop_id, requested_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_wh_outbound_requests_status
    ON wh_outbound_requests (status);

-- ledger_posted_qty: số lượng đã trừ tồn (idempotent với confirmed_qty)
CREATE TABLE IF NOT EXISTS wh_outbound_request_items (
    id                      BIGSERIAL PRIMARY KEY,
    outbound_request_id   BIGINT NOT NULL REFERENCES wh_outbound_requests(id) ON DELETE CASCADE,
    sku                     VARCHAR(255) NOT NULL,
    product_name            TEXT NOT NULL,
    requested_qty           BIGINT NOT NULL,
    confirmed_qty           BIGINT NOT NULL DEFAULT 0,
    exception_qty           BIGINT NOT NULL DEFAULT 0,
    ledger_posted_qty       BIGINT NOT NULL DEFAULT 0,
    line_status             VARCHAR(32) NOT NULL DEFAULT 'pending',
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT wh_outbound_request_items_line_status_chk CHECK (line_status IN (
        'pending', 'confirmed', 'partial', 'missing'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wh_outbound_request_items_req
    ON wh_outbound_request_items (outbound_request_id);

COMMIT;
