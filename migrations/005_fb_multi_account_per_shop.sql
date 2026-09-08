BEGIN;

ALTER TABLE fb_ad_account_mappings
    DROP CONSTRAINT IF EXISTS fb_ad_account_mappings_shop_id_key;

ALTER TABLE fb_ad_account_mappings
    DROP CONSTRAINT IF EXISTS fb_ad_account_mappings_fb_ad_account_id_key;

ALTER TABLE fb_ad_account_mappings
    ADD CONSTRAINT uq_fb_ad_account_mappings_shop_account
    UNIQUE (shop_id, fb_ad_account_id);

CREATE INDEX IF NOT EXISTS idx_fb_ad_account_mappings_shop_status
    ON fb_ad_account_mappings(shop_id, status);

ALTER TABLE fb_ads_daily_metrics
    ADD COLUMN IF NOT EXISTS account_name VARCHAR(255) NOT NULL DEFAULT '';

COMMIT;
