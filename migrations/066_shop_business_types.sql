-- 066_shop_business_types.sql
-- Loại shop (business_type) động — khách tự thêm/sửa/ẩn thay vì hard-code cty/hkd/pos3.
-- Bảng giữ mã + nhãn + icon + màu (text & nền) để render tag + nút lọc Doanh thu.

CREATE TABLE IF NOT EXISTS shop_business_types (
    code        VARCHAR(32)  PRIMARY KEY,          -- mã lưu ở shops.business_type (lowercase)
    label       VARCHAR(100) NOT NULL,             -- tên hiển thị đầy đủ
    icon        VARCHAR(16)  NOT NULL DEFAULT '',  -- emoji
    color       VARCHAR(16)  NOT NULL DEFAULT '#1d4ed8',  -- màu chữ tag
    bg_color    VARCHAR(16)  NOT NULL DEFAULT '#dbeafe',  -- màu nền tag
    sort_order  INTEGER      NOT NULL DEFAULT 100,
    active      BOOLEAN      NOT NULL DEFAULT TRUE, -- ẩn loại không dùng (không xoá)
    is_builtin  BOOLEAN      NOT NULL DEFAULT FALSE,-- cty/hkd/pos3 — không cho xoá, vẫn sửa/ẩn được
    created_at  TIMESTAMP    NOT NULL DEFAULT now()
);

-- Seed 3 loại mặc định (giữ nguyên hành vi cũ). ON CONFLICT để re-run an toàn.
INSERT INTO shop_business_types (code, label, icon, color, bg_color, sort_order, is_builtin) VALUES
    ('cty',  'Công ty',         '🏢', '#1d4ed8', '#dbeafe', 10, TRUE),
    ('hkd',  'Hộ kinh doanh',   '🏪', '#b45309', '#fef3c7', 20, TRUE),
    ('pos3', 'POS 3',           '📦', '#15803d', '#dcfce7', 30, TRUE)
ON CONFLICT (code) DO NOTHING;
