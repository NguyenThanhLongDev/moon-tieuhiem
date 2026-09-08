ALTER TABLE hr_contracts DROP CONSTRAINT IF EXISTS hr_contracts_status_chk;
ALTER TABLE hr_contracts ADD CONSTRAINT hr_contracts_status_chk CHECK (
    status IN ('draft', 'pending_employee_sign', 'pending_sign', 'signed', 'terminated', 'expired')
);
