-- POS product → payroll product mapping (phase 1).
-- Bridge between raw POS keys (external_product_id / product_id / sku / name)
-- và bảng sản phẩm KPI `product_salary_configs`.
--
-- Mục tiêu:
-- - Một pos_product_key có thể map 1-1 sang 1 product_salary_configs.id.
-- - Không buộc mapping chiều ngược lại (một sản phẩm có thể nhận nhiều key POS).
-- - Không xóa dữ liệu cũ khi đổi mapping: chỉ cập nhật dòng hiện tại.
--
-- pos_product_key nên dùng cùng định nghĩa với ads_allocation.repository.fetch_pos_products_for_shop_date:
--   COALESCE(
--     NULLIF(TRIM(external_product_id), ''),
--     product_id::text,
--     NULLIF(TRIM(sku), ''),
--     'name:' || product_name
--   )

BEGIN;

CREATE TABLE IF NOT EXISTS pos_product_mappings (
    pos_product_key VARCHAR(512) PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES product_salary_configs(id) ON DELETE RESTRICT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    note            TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_pos_product_mappings_updated_at') THEN
        CREATE TRIGGER trg_pos_product_mappings_updated_at
        BEFORE UPDATE ON pos_product_mappings
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

COMMIT;

