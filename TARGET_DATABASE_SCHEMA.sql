-- =====================================================
-- POS ERP / DASHBOARD FULL DATABASE SCHEMA
-- Target DB: PostgreSQL
-- =====================================================

-- Optional
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =====================================================
-- 1. ENUMS
-- =====================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('admin', 'leader', 'staff');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'record_status') THEN
        CREATE TYPE record_status AS ENUM ('active', 'inactive');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status_type') THEN
        CREATE TYPE order_status_type AS ENUM (
            'new',
            'confirmed',
            'shipping',
            'delivered',
            'cancelled',
            'returned',
            'unknown'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status_type') THEN
        CREATE TYPE payment_status_type AS ENUM (
            'unpaid',
            'partial',
            'paid',
            'refunded',
            'unknown'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'inventory_txn_type') THEN
        CREATE TYPE inventory_txn_type AS ENUM (
            'import',
            'export',
            'sale',
            'return',
            'adjust',
            'transfer_in',
            'transfer_out'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sync_status_type') THEN
        CREATE TYPE sync_status_type AS ENUM (
            'success',
            'failed',
            'partial',
            'running'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sync_type_enum') THEN
        CREATE TYPE sync_type_enum AS ENUM (
            'shops',
            'products',
            'orders',
            'inventory',
            'daily_metrics',
            'ads_metrics',
            'telegram',
            'full_sync'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'telegram_scope_type') THEN
        CREATE TYPE telegram_scope_type AS ENUM (
            'system',
            'team',
            'shop'
        );
    END IF;
END$$;

-- =====================================================
-- 2. CORE ADMIN / PERMISSION TABLES
-- =====================================================

CREATE TABLE IF NOT EXISTS teams (
    id                  BIGSERIAL PRIMARY KEY,
    team_code           VARCHAR(50) UNIQUE,
    team_name           VARCHAR(150) NOT NULL,
    status              record_status NOT NULL DEFAULT 'active',
    leader_user_id      BIGINT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    username            VARCHAR(100) NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    full_name           VARCHAR(150),
    role                user_role NOT NULL DEFAULT 'staff',
    team_id             BIGINT NULL REFERENCES teams(id) ON DELETE SET NULL,
    status              record_status NOT NULL DEFAULT 'active',
    email               VARCHAR(150),
    phone               VARCHAR(50),
    last_login_at       TIMESTAMP NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE teams
    ADD CONSTRAINT fk_teams_leader_user
    FOREIGN KEY (leader_user_id) REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS shops (
    id                  BIGSERIAL PRIMARY KEY,
    shop_name           VARCHAR(150) NOT NULL,
    shop_code           VARCHAR(100),
    shop_key            VARCHAR(150) UNIQUE,
    team_id             BIGINT NULL REFERENCES teams(id) ON DELETE SET NULL,
    manager_user_id     BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    status              record_status NOT NULL DEFAULT 'active',
    pos_platform        VARCHAR(50) DEFAULT 'pancake',
    timezone            VARCHAR(100) DEFAULT 'Asia/Ho_Chi_Minh',
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_shop_assignments (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    assigned_by         BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, shop_id)
);

CREATE TABLE IF NOT EXISTS webs (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    web_name            VARCHAR(150) NOT NULL,
    domain              VARCHAR(255),
    status              record_status NOT NULL DEFAULT 'active',
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================
-- 3. TELEGRAM / SYSTEM SETTINGS
-- =====================================================

CREATE TABLE IF NOT EXISTS telegram_configs (
    id                  BIGSERIAL PRIMARY KEY,
    scope_type          telegram_scope_type NOT NULL,
    scope_id            BIGINT NOT NULL,
    bot_token           TEXT,
    chat_id             VARCHAR(100),
    status              record_status NOT NULL DEFAULT 'active',
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS system_settings (
    id                  BIGSERIAL PRIMARY KEY,
    setting_key         VARCHAR(150) NOT NULL UNIQUE,
    setting_value       TEXT,
    updated_by          BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================
-- 4. MASTER DATA: WAREHOUSES / PRODUCTS
-- =====================================================

CREATE TABLE IF NOT EXISTS warehouses (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    warehouse_code      VARCHAR(100),
    warehouse_name      VARCHAR(150) NOT NULL,
    status              record_status NOT NULL DEFAULT 'active',
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, warehouse_code)
);

CREATE TABLE IF NOT EXISTS products (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    external_product_id VARCHAR(100),
    sku                 VARCHAR(100),
    product_name        VARCHAR(255) NOT NULL,
    category_name       VARCHAR(150),
    brand_name          VARCHAR(150),
    status              record_status NOT NULL DEFAULT 'active',
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, external_product_id)
);

CREATE TABLE IF NOT EXISTS product_variants (
    id                  BIGSERIAL PRIMARY KEY,
    product_id          BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    external_variant_id VARCHAR(100),
    sku                 VARCHAR(100),
    variant_name        VARCHAR(255),
    barcode             VARCHAR(100),
    status              record_status NOT NULL DEFAULT 'active',
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (product_id, external_variant_id)
);

-- =====================================================
-- 5. TRANSACTION TABLES: ORDERS / ORDER ITEMS
-- =====================================================

CREATE TABLE IF NOT EXISTS orders (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    external_order_id   VARCHAR(100) NOT NULL,
    order_code          VARCHAR(100),
    customer_name       VARCHAR(255),
    customer_phone      VARCHAR(50),
    order_status        order_status_type NOT NULL DEFAULT 'unknown',
    payment_status      payment_status_type NOT NULL DEFAULT 'unknown',

    created_at_pos      TIMESTAMP NOT NULL,
    confirmed_at        TIMESTAMP NULL,
    shipped_at          TIMESTAMP NULL,
    delivered_at        TIMESTAMP NULL,
    cancelled_at        TIMESTAMP NULL,

    subtotal_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    shipping_fee        NUMERIC(18,2) NOT NULL DEFAULT 0,
    other_fee           NUMERIC(18,2) NOT NULL DEFAULT 0,
    total_amount        NUMERIC(18,2) NOT NULL DEFAULT 0,
    net_revenue         NUMERIC(18,2) NOT NULL DEFAULT 0,

    pos_profit_loss     NUMERIC(18,2) NULL,

    raw_payload_json    JSONB,
    synced_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (shop_id, external_order_id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id          BIGINT NULL REFERENCES products(id) ON DELETE SET NULL,
    variant_id          BIGINT NULL REFERENCES product_variants(id) ON DELETE SET NULL,

    external_product_id VARCHAR(100),
    external_variant_id VARCHAR(100),

    product_name        VARCHAR(255) NOT NULL,
    variant_name        VARCHAR(255),
    sku                 VARCHAR(100),
    quantity            NUMERIC(18,2) NOT NULL DEFAULT 0,
    unit_price          NUMERIC(18,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(18,2) NOT NULL DEFAULT 0,
    line_total          NUMERIC(18,2) NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================
-- 6. INVENTORY TABLES
-- =====================================================

CREATE TABLE IF NOT EXISTS inventory_transactions (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    warehouse_id        BIGINT NULL REFERENCES warehouses(id) ON DELETE SET NULL,
    product_id          BIGINT NULL REFERENCES products(id) ON DELETE SET NULL,
    variant_id          BIGINT NULL REFERENCES product_variants(id) ON DELETE SET NULL,

    transaction_type    inventory_txn_type NOT NULL,
    quantity_change     NUMERIC(18,2) NOT NULL DEFAULT 0,
    reference_type      VARCHAR(100),
    reference_id        VARCHAR(100),
    transaction_time    TIMESTAMP NOT NULL,
    notes               TEXT,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    warehouse_id        BIGINT NULL REFERENCES warehouses(id) ON DELETE SET NULL,
    product_id          BIGINT NULL REFERENCES products(id) ON DELETE SET NULL,
    variant_id          BIGINT NULL REFERENCES product_variants(id) ON DELETE SET NULL,

    snapshot_date       DATE NOT NULL,
    stock_quantity      NUMERIC(18,2) NOT NULL DEFAULT 0,
    reserved_quantity   NUMERIC(18,2) NOT NULL DEFAULT 0,
    available_quantity  NUMERIC(18,2) NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (shop_id, warehouse_id, variant_id, snapshot_date)
);

-- =====================================================
-- 7. ADS / DAILY METRICS / REPORTING TABLES
-- =====================================================

CREATE TABLE IF NOT EXISTS ads_daily_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    metric_date         DATE NOT NULL,
    ads_cost            NUMERIC(18,2) NOT NULL DEFAULT 0,
    campaign_count      INTEGER NOT NULL DEFAULT 0,
    data_filled_flag    BOOLEAN NOT NULL DEFAULT FALSE,
    last_filled_at      TIMESTAMP NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (shop_id, metric_date)
);

CREATE TABLE IF NOT EXISTS daily_shop_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    shop_id             BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    metric_date         DATE NOT NULL,

    order_count         INTEGER NOT NULL DEFAULT 0,
    new_order_count     INTEGER NOT NULL DEFAULT 0,
    confirmed_count     INTEGER NOT NULL DEFAULT 0,
    shipping_count      INTEGER NOT NULL DEFAULT 0,
    delivered_count     INTEGER NOT NULL DEFAULT 0,
    returned_count      INTEGER NOT NULL DEFAULT 0,
    cancelled_count     INTEGER NOT NULL DEFAULT 0,

    gross_revenue       NUMERIC(18,2) NOT NULL DEFAULT 0,
    net_revenue         NUMERIC(18,2) NOT NULL DEFAULT 0,
    ads_cost            NUMERIC(18,2) NOT NULL DEFAULT 0,

    pos_profit_loss     NUMERIC(18,2) NOT NULL DEFAULT 0,
    pos_avg_profit_per_order NUMERIC(18,2) NOT NULL DEFAULT 0,
    loss_flag           BOOLEAN NOT NULL DEFAULT FALSE,

    inventory_value     NUMERIC(18,2) NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (shop_id, metric_date)
);

-- =====================================================
-- 8. SYNC / AUDIT
-- =====================================================

CREATE TABLE IF NOT EXISTS sync_logs (
    id                  BIGSERIAL PRIMARY KEY,
    sync_type           sync_type_enum NOT NULL,
    shop_id             BIGINT NULL REFERENCES shops(id) ON DELETE SET NULL,
    status              sync_status_type NOT NULL DEFAULT 'running',
    started_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMP NULL,
    records_count       INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    action              VARCHAR(150) NOT NULL,
    target_type         VARCHAR(100),
    target_id           VARCHAR(100),
    details             JSONB,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================
-- 9. INDEXES
-- =====================================================

CREATE INDEX IF NOT EXISTS idx_users_team_id
    ON users(team_id);

CREATE INDEX IF NOT EXISTS idx_users_role
    ON users(role);

CREATE INDEX IF NOT EXISTS idx_users_status
    ON users(status);

CREATE INDEX IF NOT EXISTS idx_shops_team_id
    ON shops(team_id);

CREATE INDEX IF NOT EXISTS idx_shops_status
    ON shops(status);

CREATE INDEX IF NOT EXISTS idx_user_shop_assignments_user_id
    ON user_shop_assignments(user_id);

CREATE INDEX IF NOT EXISTS idx_user_shop_assignments_shop_id
    ON user_shop_assignments(shop_id);

CREATE INDEX IF NOT EXISTS idx_webs_shop_id
    ON webs(shop_id);

CREATE INDEX IF NOT EXISTS idx_warehouses_shop_id
    ON warehouses(shop_id);

CREATE INDEX IF NOT EXISTS idx_products_shop_id
    ON products(shop_id);

CREATE INDEX IF NOT EXISTS idx_products_sku
    ON products(sku);

CREATE INDEX IF NOT EXISTS idx_product_variants_product_id
    ON product_variants(product_id);

CREATE INDEX IF NOT EXISTS idx_product_variants_sku
    ON product_variants(sku);

CREATE INDEX IF NOT EXISTS idx_orders_shop_created_at
    ON orders(shop_id, created_at_pos);

CREATE INDEX IF NOT EXISTS idx_orders_created_at_pos
    ON orders(created_at_pos);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(order_status);

CREATE INDEX IF NOT EXISTS idx_orders_payment_status
    ON orders(payment_status);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id
    ON order_items(order_id);

CREATE INDEX IF NOT EXISTS idx_order_items_shop_id
    ON order_items(shop_id);

CREATE INDEX IF NOT EXISTS idx_order_items_product_id
    ON order_items(product_id);

CREATE INDEX IF NOT EXISTS idx_inventory_transactions_shop_time
    ON inventory_transactions(shop_id, transaction_time);

CREATE INDEX IF NOT EXISTS idx_inventory_transactions_variant_id
    ON inventory_transactions(variant_id);

CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_shop_date
    ON inventory_snapshots(shop_id, snapshot_date);

CREATE INDEX IF NOT EXISTS idx_ads_daily_metrics_shop_date
    ON ads_daily_metrics(shop_id, metric_date);

CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_shop_date
    ON daily_shop_metrics(shop_id, metric_date);

CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_date
    ON daily_shop_metrics(metric_date);

CREATE INDEX IF NOT EXISTS idx_daily_shop_metrics_loss_flag
    ON daily_shop_metrics(loss_flag);

CREATE INDEX IF NOT EXISTS idx_sync_logs_shop_id
    ON sync_logs(shop_id);

CREATE INDEX IF NOT EXISTS idx_sync_logs_status
    ON sync_logs(status);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id
    ON audit_logs(user_id);

-- =====================================================
-- 10. UPDATED_AT TRIGGER
-- =====================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_teams_updated_at'
    ) THEN
        CREATE TRIGGER trg_teams_updated_at
        BEFORE UPDATE ON teams
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_users_updated_at'
    ) THEN
        CREATE TRIGGER trg_users_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_shops_updated_at'
    ) THEN
        CREATE TRIGGER trg_shops_updated_at
        BEFORE UPDATE ON shops
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_webs_updated_at'
    ) THEN
        CREATE TRIGGER trg_webs_updated_at
        BEFORE UPDATE ON webs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_telegram_configs_updated_at'
    ) THEN
        CREATE TRIGGER trg_telegram_configs_updated_at
        BEFORE UPDATE ON telegram_configs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_system_settings_updated_at'
    ) THEN
        CREATE TRIGGER trg_system_settings_updated_at
        BEFORE UPDATE ON system_settings
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_warehouses_updated_at'
    ) THEN
        CREATE TRIGGER trg_warehouses_updated_at
        BEFORE UPDATE ON warehouses
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_products_updated_at'
    ) THEN
        CREATE TRIGGER trg_products_updated_at
        BEFORE UPDATE ON products
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_product_variants_updated_at'
    ) THEN
        CREATE TRIGGER trg_product_variants_updated_at
        BEFORE UPDATE ON product_variants
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_orders_updated_at'
    ) THEN
        CREATE TRIGGER trg_orders_updated_at
        BEFORE UPDATE ON orders
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_order_items_updated_at'
    ) THEN
        CREATE TRIGGER trg_order_items_updated_at
        BEFORE UPDATE ON order_items
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_inventory_snapshots_updated_at'
    ) THEN
        CREATE TRIGGER trg_inventory_snapshots_updated_at
        BEFORE UPDATE ON inventory_snapshots
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ads_daily_metrics_updated_at'
    ) THEN
        CREATE TRIGGER trg_ads_daily_metrics_updated_at
        BEFORE UPDATE ON ads_daily_metrics
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_daily_shop_metrics_updated_at'
    ) THEN
        CREATE TRIGGER trg_daily_shop_metrics_updated_at
        BEFORE UPDATE ON daily_shop_metrics
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

-- =====================================================
-- 11. HELPFUL VIEW: ACTIVE SHOPS
-- =====================================================

CREATE OR REPLACE VIEW vw_active_shops AS
SELECT
    s.id,
    s.shop_name,
    s.shop_code,
    s.shop_key,
    s.team_id,
    t.team_name,
    s.manager_user_id,
    s.status
FROM shops s
LEFT JOIN teams t ON t.id = s.team_id
WHERE s.status = 'active';

-- =====================================================
-- 12. HELPFUL VIEW: USER VISIBLE SHOPS BASE
-- Note: actual app logic should still handle role-based filtering.
-- =====================================================

CREATE OR REPLACE VIEW vw_shop_team_info AS
SELECT
    s.id AS shop_id,
    s.shop_name,
    s.shop_key,
    s.status AS shop_status,
    s.team_id,
    t.team_name
FROM shops s
LEFT JOIN teams t ON t.id = s.team_id;

-- =====================================================
-- 13. FACEBOOK PAGES MODULE TABLES
-- Managed by modules/fb_pages/__init__.py migrations
-- =====================================================

CREATE TABLE IF NOT EXISTS fb_pages (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) UNIQUE NOT NULL,
    page_name       VARCHAR(255) NOT NULL,
    page_category   VARCHAR(255),
    picture_url     TEXT,
    access_token    TEXT,
    fb_user_id      VARCHAR(100),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fb_page_assignments (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) NOT NULL REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (page_id, user_id)
);

CREATE TABLE IF NOT EXISTS fb_page_ad_account_map (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) NOT NULL UNIQUE REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    ad_account_id   VARCHAR(100) NOT NULL,
    ad_account_name VARCHAR(255),
    shop_key        VARCHAR(100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- fb_page_spend_declarations: stores daily ad spend declarations per page per user.
-- Columns: amount (legacy, kept for backward compat), plus new spend breakdown columns.
-- New columns added via ALTER in run_migrations() of the fb_pages module.
CREATE TABLE IF NOT EXISTS fb_page_spend_declarations (
    id              BIGSERIAL PRIMARY KEY,
    page_id         VARCHAR(100) NOT NULL REFERENCES fb_pages(page_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    spend_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    amount          NUMERIC(18,2) NOT NULL DEFAULT 0,  -- legacy column, equals thanh_tien for new rows
    note            TEXT,
    ad_account_id   VARCHAR(100),                       -- from fb_page_ad_account_map or manual
    ad_account_name VARCHAR(255),                       -- display name of ad account
    phan_loai       VARCHAR(20) DEFAULT 'ma_win',        -- 'ma_win' | 'ma_test'
    tien_chua_thue  NUMERIC(18,2),                      -- amount before VAT
    thue_rate       NUMERIC(6,4) DEFAULT 0.061,          -- fixed at 6.1%
    thanh_tien      NUMERIC(18,2),                      -- tien_chua_thue * (1 + thue_rate)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
