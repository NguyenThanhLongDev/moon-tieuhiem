-- Rollback 040
DROP TRIGGER IF EXISTS trg_hr_profiles_updated ON hr_profiles;
DROP FUNCTION IF EXISTS hr_profiles_set_updated_at();
DROP TABLE IF EXISTS hr_profiles;
