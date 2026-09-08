-- Idempotent: safe if 006_fb_ads_message_metrics.sql was already applied.
BEGIN;

ALTER TABLE fb_ads_daily_metrics
    ADD COLUMN IF NOT EXISTS message_count BIGINT NULL;

COMMIT;
