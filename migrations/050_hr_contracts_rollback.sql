-- Rollback 050_hr_contracts.sql
DROP TRIGGER IF EXISTS trg_hr_contracts_updated ON hr_contracts;
DROP FUNCTION IF EXISTS hr_contracts_set_updated_at();
DROP TRIGGER IF EXISTS trg_hr_companies_updated ON hr_companies;
DROP FUNCTION IF EXISTS hr_companies_set_updated_at();
DROP TABLE IF EXISTS hr_contracts;
DROP TABLE IF EXISTS hr_companies;
