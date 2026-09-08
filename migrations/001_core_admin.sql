-- Phase 1: Core admin tables only (safe, backward-compatible).
-- This migration does NOT touch transaction-heavy tables.

BEGIN;

-- 1) Enums
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('admin', 'leader', 'staff');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'record_status') THEN
        CREATE TYPE record_status AS ENUM ('active', 'inactive');
    END IF;
END$$;

-- 2) Core admin/permission tables
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
    DROP CONSTRAINT IF EXISTS fk_teams_leader_user;

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

-- 3) Indexes
CREATE INDEX IF NOT EXISTS idx_users_team_id ON users(team_id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_shops_team_id ON shops(team_id);
CREATE INDEX IF NOT EXISTS idx_shops_status ON shops(status);
CREATE INDEX IF NOT EXISTS idx_user_shop_assignments_user_id ON user_shop_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_user_shop_assignments_shop_id ON user_shop_assignments(shop_id);
CREATE INDEX IF NOT EXISTS idx_webs_shop_id ON webs(shop_id);

-- 4) Updated-at trigger
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_teams_updated_at') THEN
        CREATE TRIGGER trg_teams_updated_at
        BEFORE UPDATE ON teams
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_users_updated_at') THEN
        CREATE TRIGGER trg_users_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_shops_updated_at') THEN
        CREATE TRIGGER trg_shops_updated_at
        BEFORE UPDATE ON shops
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_webs_updated_at') THEN
        CREATE TRIGGER trg_webs_updated_at
        BEFORE UPDATE ON webs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END$$;

COMMIT;
