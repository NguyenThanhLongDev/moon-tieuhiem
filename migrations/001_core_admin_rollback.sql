-- Rollback for Phase 1 core admin migration.
-- Use only if Phase 1 needs full rollback.

BEGIN;

DROP TRIGGER IF EXISTS trg_webs_updated_at ON webs;
DROP TRIGGER IF EXISTS trg_shops_updated_at ON shops;
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
DROP TRIGGER IF EXISTS trg_teams_updated_at ON teams;

DROP INDEX IF EXISTS idx_webs_shop_id;
DROP INDEX IF EXISTS idx_user_shop_assignments_shop_id;
DROP INDEX IF EXISTS idx_user_shop_assignments_user_id;
DROP INDEX IF EXISTS idx_shops_status;
DROP INDEX IF EXISTS idx_shops_team_id;
DROP INDEX IF EXISTS idx_users_status;
DROP INDEX IF EXISTS idx_users_role;
DROP INDEX IF EXISTS idx_users_team_id;

DROP TABLE IF EXISTS webs;
DROP TABLE IF EXISTS user_shop_assignments;
DROP TABLE IF EXISTS shops;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS teams;

-- Keep enums to avoid impacting other migrations/scripts.
-- DROP TYPE IF EXISTS user_role;
-- DROP TYPE IF EXISTS record_status;

COMMIT;
