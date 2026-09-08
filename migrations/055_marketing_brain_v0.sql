-- 055: Marketing Brain V0 — Data Layer (GĐ1)
-- 2 bảng mb_*: attribution per-order (từ orders.raw_payload_json) + FB ad-level daily.
-- Nguyên tắc module riêng: KHÔNG FK cứng vào bảng core (soft key), prefix mb_.

-- 1. Attribution per-order: extract từ orders.raw_payload_json
CREATE TABLE IF NOT EXISTS mb_order_attribution (
    order_id        BIGINT PRIMARY KEY,           -- soft key → orders.id
    shop_id         BIGINT NOT NULL DEFAULT 0,    -- soft key → shops.id (denormalize)
    ad_id           VARCHAR(64)  NOT NULL DEFAULT '',
    post_id         VARCHAR(160) NOT NULL DEFAULT '',
    page_id         VARCHAR(64)  NOT NULL DEFAULT '',
    page_name       VARCHAR(255) NOT NULL DEFAULT '',
    conversation_id VARCHAR(160) NOT NULL DEFAULT '',
    ads_source      VARCHAR(64)  NOT NULL DEFAULT '',
    is_livestream   BOOLEAN NOT NULL DEFAULT FALSE,
    -- organic = có page nhưng không gắn ad
    is_organic      BOOLEAN NOT NULL DEFAULT FALSE,
    order_date      DATE,                          -- denormalize từ orders.created_at_pos
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mb_oa_ad   ON mb_order_attribution (ad_id)   WHERE ad_id   <> '';
CREATE INDEX IF NOT EXISTS idx_mb_oa_page ON mb_order_attribution (page_id) WHERE page_id <> '';
CREATE INDEX IF NOT EXISTS idx_mb_oa_post ON mb_order_attribution (post_id) WHERE post_id <> '';
CREATE INDEX IF NOT EXISTS idx_mb_oa_date ON mb_order_attribution (order_date);

-- 2. FB insights ad-level daily (ngừng vứt data từ call level=ad sẵn có)
CREATE TABLE IF NOT EXISTS mb_fb_entity_daily (
    id              BIGSERIAL PRIMARY KEY,
    metric_date     DATE NOT NULL,
    account_id      VARCHAR(64)  NOT NULL DEFAULT '',
    campaign_id     VARCHAR(64)  NOT NULL DEFAULT '',
    campaign_name   VARCHAR(512) NOT NULL DEFAULT '',
    adset_id        VARCHAR(64)  NOT NULL DEFAULT '',
    adset_name      VARCHAR(512) NOT NULL DEFAULT '',
    ad_id           VARCHAR(64)  NOT NULL,
    ad_name         VARCHAR(512) NOT NULL DEFAULT '',
    page_id         VARCHAR(64)  NOT NULL DEFAULT '',  -- từ creative map (object_story_id / spec)
    post_id         VARCHAR(160) NOT NULL DEFAULT '',  -- object_story_id = {page_id}_{post_id}
    spend           NUMERIC(18,2) NOT NULL DEFAULT 0,  -- đã quy VND (cùng convention fb_ads_page_daily_spend, CHƯA VAT)
    impressions     BIGINT NOT NULL DEFAULT 0,
    clicks          BIGINT NOT NULL DEFAULT 0,
    reach           BIGINT,
    frequency       NUMERIC(10,4),
    purchases       BIGINT,
    purchase_value  NUMERIC(18,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT mb_fb_entity_daily_uniq UNIQUE (metric_date, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_mb_fed_date     ON mb_fb_entity_daily (metric_date);
CREATE INDEX IF NOT EXISTS idx_mb_fed_campaign ON mb_fb_entity_daily (campaign_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_mb_fed_adset    ON mb_fb_entity_daily (adset_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_mb_fed_account  ON mb_fb_entity_daily (account_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_mb_fed_page     ON mb_fb_entity_daily (page_id, metric_date);
