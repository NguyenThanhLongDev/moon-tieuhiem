BEGIN;

ALTER TABLE fb_ads_daily_metrics
    DROP COLUMN IF EXISTS message_count;

COMMIT;
