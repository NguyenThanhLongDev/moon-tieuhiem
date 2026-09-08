-- 065: Thêm cột product_tax cho wh_products.
-- Lưu "thuế sản phẩm" lấy từ Pancake field product.categories[].name (shop TH ghi vd "8%").
-- Dùng để tính lương theo sản phẩm. Lưu nguyên text (vd "8%"); shop không set → để trống.
ALTER TABLE wh_products ADD COLUMN IF NOT EXISTS product_tax TEXT NOT NULL DEFAULT '';
