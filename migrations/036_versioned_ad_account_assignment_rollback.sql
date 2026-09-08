-- Rollback 036_versioned_ad_account_assignment.sql

-- Restore UNIQUE constraint trên fb_ad_account_mappings
DROP INDEX IF EXISTS uq_fbam_active;
DROP INDEX IF EXISTS idx_fbam_window;
ALTER TABLE fb_ad_account_mappings
    DROP COLUMN IF EXISTS assigned_from,
    DROP COLUMN IF EXISTS assigned_to,
    DROP COLUMN IF EXISTS derived_from_user_id;
-- Chỉ thêm lại constraint nếu chưa tồn tại
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_fb_ad_account_mappings_shop_account'
    ) THEN
        ALTER TABLE fb_ad_account_mappings
            ADD CONSTRAINT uq_fb_ad_account_mappings_shop_account
            UNIQUE (shop_id, fb_ad_account_id);
    END IF;
END $$;

-- Drop bảng versioned
DROP INDEX IF EXISTS uq_uaa_active;
DROP INDEX IF EXISTS idx_uaa_window;
DROP INDEX IF EXISTS idx_uaa_user;
DROP INDEX IF EXISTS idx_uaa_account;
DROP TABLE IF EXISTS user_ad_account_assignments;
