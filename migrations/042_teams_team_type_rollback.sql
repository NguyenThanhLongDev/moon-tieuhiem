-- Rollback 042
ALTER TABLE teams DROP CONSTRAINT IF EXISTS teams_team_type_chk;
DROP INDEX IF EXISTS idx_teams_team_type;
ALTER TABLE teams DROP COLUMN IF EXISTS team_type;
