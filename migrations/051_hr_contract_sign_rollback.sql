-- Rollback 051
DROP TABLE IF EXISTS hr_contract_events;
ALTER TABLE hr_contracts DROP CONSTRAINT IF EXISTS hr_contracts_status_chk;
ALTER TABLE hr_contracts ADD CONSTRAINT hr_contracts_status_chk CHECK (
    status IN ('draft', 'pending_sign', 'signed', 'terminated', 'expired')
);
ALTER TABLE hr_contracts
  DROP COLUMN IF EXISTS signed_file_path,
  DROP COLUMN IF EXISTS signed_file_uploaded_at,
  DROP COLUMN IF EXISTS signed_file_uploaded_by;
