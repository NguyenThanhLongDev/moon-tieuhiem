-- Ads → product mapping layer (independent from payroll). FK to existing raw: fb_ads_daily_metrics.
-- Product dimension: product_salary_configs (KPI products).

BEGIN;

CREATE TABLE IF NOT EXISTS ads_product_mapping_rules (
    id                  BIGSERIAL PRIMARY KEY,
    fb_ad_account_id    VARCHAR(64) NULL,
    match_level         VARCHAR(32) NOT NULL
        CHECK (match_level IN ('ad', 'adset', 'campaign', 'name_pattern')),
    campaign_id         VARCHAR(128) NULL,
    campaign_name       VARCHAR(512) NULL,
    adset_id            VARCHAR(128) NULL,
    adset_name          VARCHAR(512) NULL,
    ad_id               VARCHAR(128) NULL,
    ad_name             VARCHAR(512) NULL,
    name_pattern        VARCHAR(512) NULL,
    product_id          BIGINT NOT NULL REFERENCES product_salary_configs(id) ON DELETE RESTRICT,
    priority            INT NOT NULL DEFAULT 100,
    effective_from      DATE NULL,
    effective_to        DATE NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    note                TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ads_map_rules_active_priority
    ON ads_product_mapping_rules(is_active, priority, id);
CREATE INDEX IF NOT EXISTS idx_ads_map_rules_account
    ON ads_product_mapping_rules(fb_ad_account_id) WHERE fb_ad_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ads_product_mapping_results (
    id                  BIGSERIAL PRIMARY KEY,
    ads_raw_id          BIGINT NOT NULL REFERENCES fb_ads_daily_metrics(id) ON DELETE CASCADE,
    stat_date           DATE NOT NULL,
    product_id          BIGINT NULL REFERENCES product_salary_configs(id) ON DELETE SET NULL,
    mapping_rule_id     BIGINT NULL REFERENCES ads_product_mapping_rules(id) ON DELETE SET NULL,
    mapping_method      VARCHAR(32) NOT NULL
        CHECK (mapping_method IN ('manual', 'ad_id', 'adset_id', 'campaign_id', 'name_pattern', 'unmapped')),
    confidence          NUMERIC(6,2) NOT NULL DEFAULT 0,
    status              VARCHAR(32) NOT NULL
        CHECK (status IN ('mapped', 'unmapped', 'low_confidence')),
    match_detail        TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ads_raw_id)
);

CREATE INDEX IF NOT EXISTS idx_ads_map_results_stat_date ON ads_product_mapping_results(stat_date);
CREATE INDEX IF NOT EXISTS idx_ads_map_results_product ON ads_product_mapping_results(product_id) WHERE product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ads_map_results_status ON ads_product_mapping_results(status);

CREATE TABLE IF NOT EXISTS ads_unmapped_queue (
    id                  BIGSERIAL PRIMARY KEY,
    ads_raw_id          BIGINT NOT NULL REFERENCES fb_ads_daily_metrics(id) ON DELETE CASCADE,
    stat_date           DATE NOT NULL,
    reason              TEXT NOT NULL,
    status              VARCHAR(32) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'ignored')),
    review_note         TEXT NULL,
    reviewed_by         VARCHAR(128) NULL,
    reviewed_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ads_raw_id, stat_date)
);

CREATE INDEX IF NOT EXISTS idx_ads_unmapped_queue_status_date ON ads_unmapped_queue(status, stat_date);

CREATE TABLE IF NOT EXISTS product_ads_cost_daily (
    id                  BIGSERIAL PRIMARY KEY,
    stat_date           DATE NOT NULL,
    product_id          BIGINT NOT NULL REFERENCES product_salary_configs(id) ON DELETE RESTRICT,
    total_spend         NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_vat           NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_cost          NUMERIC(18,2) NOT NULL DEFAULT 0,
    source_rows_count   INT NOT NULL DEFAULT 0,
    calc_version        INT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stat_date, product_id, calc_version)
);

CREATE INDEX IF NOT EXISTS idx_product_ads_cost_daily_date ON product_ads_cost_daily(stat_date);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_product_mapping_rules_updated_at') THEN
        CREATE TRIGGER trg_ads_product_mapping_rules_updated_at
        BEFORE UPDATE ON ads_product_mapping_rules
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_product_mapping_results_updated_at') THEN
        CREATE TRIGGER trg_ads_product_mapping_results_updated_at
        BEFORE UPDATE ON ads_product_mapping_results
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_unmapped_queue_updated_at') THEN
        CREATE TRIGGER trg_ads_unmapped_queue_updated_at
        BEFORE UPDATE ON ads_unmapped_queue
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_product_ads_cost_daily_updated_at') THEN
        CREATE TRIGGER trg_product_ads_cost_daily_updated_at
        BEFORE UPDATE ON product_ads_cost_daily
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

COMMIT;
