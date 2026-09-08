BEGIN;

ALTER TABLE fb_ads_daily_metrics
    DROP COLUMN IF EXISTS account_name;

ALTER TABLE fb_ad_account_mappings
    DROP CONSTRAINT IF EXISTS uq_fb_ad_account_mappings_shop_account;

ALTER TABLE fb_ad_account_mappings
    ADD CONSTRAINT fb_ad_account_mappings_shop_id_key UNIQUE (shop_id);

ALTER TABLE fb_ad_account_mappings
    ADD CONSTRAINT fb_ad_account_mappings_fb_ad_account_id_key UNIQUE (fb_ad_account_id);

COMMIT;
