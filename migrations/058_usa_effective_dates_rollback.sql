-- Rollback 058: bỏ ngày hiệu lực gán shop→NV
-- CẢNH BÁO: rollback sẽ mất lịch sử chuyển nhượng (row đã đóng). Chỉ giữ row active.
DELETE FROM user_shop_assignments WHERE assigned_to IS NOT NULL;
DROP INDEX IF EXISTS idx_usa_window;
DROP INDEX IF EXISTS uq_usa_active_pair;
ALTER TABLE user_shop_assignments DROP CONSTRAINT IF EXISTS chk_usa_window;
ALTER TABLE user_shop_assignments DROP COLUMN IF EXISTS reason;
ALTER TABLE user_shop_assignments DROP COLUMN IF EXISTS assigned_to;
ALTER TABLE user_shop_assignments DROP COLUMN IF EXISTS assigned_from;
ALTER TABLE user_shop_assignments
    ADD CONSTRAINT user_shop_assignments_user_id_shop_id_key UNIQUE (user_id, shop_id);
