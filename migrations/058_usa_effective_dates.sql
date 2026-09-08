-- 058: Thêm ngày hiệu lực cho gán shop→NV (versioned, giống user_ad_account_assignments)
-- Vì sao (2026-06-11): user_shop_assignments không có chiều thời gian → chuyển shop
-- giữa 2 NV (kenleader → minhdatth 6/6) làm doanh thu lịch sử chạy theo người giữ
-- hiện tại + shop treo 2 người cùng lúc → lương 2B double cùng 1 cục doanh thu.

ALTER TABLE user_shop_assignments ADD COLUMN IF NOT EXISTS assigned_from date;
ALTER TABLE user_shop_assignments ADD COLUMN IF NOT EXISTS assigned_to   date;
ALTER TABLE user_shop_assignments ADD COLUMN IF NOT EXISTS reason        text;

-- Backfill: row hiện có coi như hiệu lực từ xa xưa (giữ nguyên behavior cũ
-- cho mọi shop chưa từng chuyển nhượng), NULL assigned_to = đang hiệu lực.
UPDATE user_shop_assignments SET assigned_from = '2020-01-01' WHERE assigned_from IS NULL;

ALTER TABLE user_shop_assignments ALTER COLUMN assigned_from SET NOT NULL;
ALTER TABLE user_shop_assignments ALTER COLUMN assigned_from SET DEFAULT CURRENT_DATE;

ALTER TABLE user_shop_assignments
    ADD CONSTRAINT chk_usa_window CHECK (assigned_to IS NULL OR assigned_to >= assigned_from);

-- Bỏ unique cứng (user_id, shop_id) — chặn NV quay lại shop cũ ở giai đoạn sau.
-- Thay bằng partial unique: 1 cặp (user, shop) chỉ 1 row ĐANG hiệu lực.
ALTER TABLE user_shop_assignments DROP CONSTRAINT IF EXISTS user_shop_assignments_user_id_shop_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_usa_active_pair
    ON user_shop_assignments (user_id, shop_id) WHERE assigned_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_usa_window
    ON user_shop_assignments (shop_id, assigned_from, assigned_to);
