-- Manual "Phân bổ Ads theo sản phẩm" (per day, per employee shop + FB account).
-- Không nối payroll. employees(employee_code) có thể chưa tồn tại — không dùng FK cứng.
--
-- Trigger: dùng EXECUTE PROCEDURE (PG 11–13 + 14+); tránh EXECUTE FUNCTION (chỉ PG 14+).
-- Hàm set_updated_at: idempotent với migrations/001_core_admin.sql (tạo nếu DB cũ thiếu).

BEGIN;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS employee_ads_shop_mapping (
    id                  BIGSERIAL PRIMARY KEY,
    employee_code       VARCHAR(100) NOT NULL,
    shop_key            VARCHAR(100) NOT NULL,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    fb_ad_account_id    VARCHAR(64) NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (employee_code, shop_id, fb_ad_account_id)
);

CREATE INDEX IF NOT EXISTS idx_employee_ads_shop_mapping_emp
    ON employee_ads_shop_mapping(employee_code) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_employee_ads_shop_mapping_shop
    ON employee_ads_shop_mapping(shop_id) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS ads_product_allocation_headers (
    id                      BIGSERIAL PRIMARY KEY,
    allocation_date         DATE NOT NULL,
    employee_code         VARCHAR(100) NOT NULL,
    shop_key                VARCHAR(100) NOT NULL,
    shop_id                 BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    fb_ad_account_id        VARCHAR(64) NOT NULL,
    total_ads_from_facebook NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_ads_vat           NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_ads_with_vat      NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_allocated_amount  NUMERIC(18,2) NOT NULL DEFAULT 0,
    variance_amount         NUMERIC(18,2) NOT NULL DEFAULT 0,
    status                  VARCHAR(32) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'balanced', 'locked')),
    created_by              VARCHAR(128) NOT NULL DEFAULT '',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (allocation_date, employee_code, shop_id, fb_ad_account_id)
);

CREATE INDEX IF NOT EXISTS idx_ads_alloc_headers_date ON ads_product_allocation_headers(allocation_date);
CREATE INDEX IF NOT EXISTS idx_ads_alloc_headers_emp ON ads_product_allocation_headers(employee_code);

CREATE TABLE IF NOT EXISTS ads_product_allocation_lines (
    id                      BIGSERIAL PRIMARY KEY,
    header_id               BIGINT NOT NULL REFERENCES ads_product_allocation_headers(id) ON DELETE CASCADE,
    pos_product_key         VARCHAR(512) NOT NULL,
    external_product_id     VARCHAR(100) NULL,
    product_name            VARCHAR(512) NOT NULL DEFAULT '',
    quantity                NUMERIC(18,2) NOT NULL DEFAULT 0,
    unit_price              NUMERIC(18,2) NOT NULL DEFAULT 0,
    revenue                 NUMERIC(18,2) NOT NULL DEFAULT 0,
    allocated_ads_amount    NUMERIC(18,2) NOT NULL DEFAULT 0,
    note                    TEXT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (header_id, pos_product_key)
);

CREATE INDEX IF NOT EXISTS idx_ads_alloc_lines_header ON ads_product_allocation_lines(header_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_employee_ads_shop_mapping_updated_at') THEN
        CREATE TRIGGER trg_employee_ads_shop_mapping_updated_at
        BEFORE UPDATE ON employee_ads_shop_mapping
        FOR EACH ROW EXECUTE PROCEDURE set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_product_allocation_headers_updated_at') THEN
        CREATE TRIGGER trg_ads_product_allocation_headers_updated_at
        BEFORE UPDATE ON ads_product_allocation_headers
        FOR EACH ROW EXECUTE PROCEDURE set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_product_allocation_lines_updated_at') THEN
        CREATE TRIGGER trg_ads_product_allocation_lines_updated_at
        BEFORE UPDATE ON ads_product_allocation_lines
        FOR EACH ROW EXECUTE PROCEDURE set_updated_at();
    END IF;
END$$;

COMMIT;
