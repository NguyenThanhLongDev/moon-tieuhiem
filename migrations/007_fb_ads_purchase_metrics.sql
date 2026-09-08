BEGIN;

ALTER TABLE fb_ads_daily_metrics
    ADD COLUMN IF NOT EXISTS purchase_count BIGINT NULL,
    ADD COLUMN IF NOT EXISTS purchase_cpa NUMERIC(18, 4) NULL;

COMMIT;
