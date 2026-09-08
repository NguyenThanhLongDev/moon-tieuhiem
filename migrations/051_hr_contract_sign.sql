-- 051_hr_contract_sign.sql
-- Phase 2 HĐLĐ: ký số bên cty (upload) + ký tay NV (signature pad web).

ALTER TABLE hr_contracts
  ADD COLUMN IF NOT EXISTS signed_file_path TEXT,
  ADD COLUMN IF NOT EXISTS signed_file_uploaded_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS signed_file_uploaded_by BIGINT REFERENCES users(id);

-- Bổ sung status mới vào CHECK
ALTER TABLE hr_contracts DROP CONSTRAINT IF EXISTS hr_contracts_status_chk;
ALTER TABLE hr_contracts ADD CONSTRAINT hr_contracts_status_chk CHECK (
    status IN ('draft', 'pending_employee_sign', 'pending_sign', 'signed', 'terminated', 'expired')
);

-- Audit log: ai làm gì với HĐ (tạo / upload / ký NV / ký cty / huỷ)
CREATE TABLE IF NOT EXISTS hr_contract_events (
    id            BIGSERIAL   PRIMARY KEY,
    contract_id   BIGINT      NOT NULL REFERENCES hr_contracts(id) ON DELETE CASCADE,
    event_type    VARCHAR(40) NOT NULL,
                  -- created | uploaded_signed | signed_employee | signed_company | downloaded | deleted
    actor_user_id BIGINT      REFERENCES users(id),
    actor_ip      VARCHAR(50),
    actor_ua      TEXT,
    detail        TEXT,
    created_at    TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hr_contract_events_cid ON hr_contract_events(contract_id, created_at DESC);
