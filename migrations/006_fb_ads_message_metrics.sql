BEGIN;

ALTER TABLE fb_ads_daily_metrics
    ADD COLUMN IF NOT EXISTS message_count BIGINT NULL;

COMMENT ON COLUMN fb_ads_daily_metrics.message_count IS
    'Messaging conversations started (7d window); NULL = unknown/legacy. Cost/message = spend/count in UI.';

COMMIT;
