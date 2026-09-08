BEGIN;

ALTER TABLE fb_ads_daily_metrics
    DROP COLUMN IF EXISTS purchase_cpa,
    DROP COLUMN IF EXISTS purchase_count;

COMMIT;
