BEGIN;

CREATE TABLE IF NOT EXISTS fb_ad_account_mappings (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    fb_ad_account_id    VARCHAR(64) NOT NULL,
    account_name        VARCHAR(255) NOT NULL DEFAULT '',
    status              VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id),
    UNIQUE (fb_ad_account_id)
);

CREATE TABLE IF NOT EXISTS fb_ads_daily_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    metric_date         DATE NOT NULL,
    fb_ad_account_id    VARCHAR(64) NOT NULL,
    spend               NUMERIC(18,2) NOT NULL DEFAULT 0,
    impressions         BIGINT NOT NULL DEFAULT 0,
    clicks              BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, metric_date, fb_ad_account_id)
);

CREATE INDEX IF NOT EXISTS idx_fb_ad_account_mappings_shop_id
    ON fb_ad_account_mappings(shop_id);

CREATE INDEX IF NOT EXISTS idx_fb_ad_account_mappings_status
    ON fb_ad_account_mappings(status);

CREATE INDEX IF NOT EXISTS idx_fb_ads_daily_metrics_shop_date
    ON fb_ads_daily_metrics(shop_id, metric_date);

CREATE INDEX IF NOT EXISTS idx_fb_ads_daily_metrics_account_date
    ON fb_ads_daily_metrics(fb_ad_account_id, metric_date);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_fb_ad_account_mappings_updated_at') THEN
        CREATE TRIGGER trg_fb_ad_account_mappings_updated_at
        BEFORE UPDATE ON fb_ad_account_mappings
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_fb_ads_daily_metrics_updated_at') THEN
        CREATE TRIGGER trg_fb_ads_daily_metrics_updated_at
        BEFORE UPDATE ON fb_ads_daily_metrics
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

COMMIT;
