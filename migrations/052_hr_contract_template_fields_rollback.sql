-- Rollback 052
ALTER TABLE hr_companies
  DROP COLUMN IF EXISTS legal_rep_dob,
  DROP COLUMN IF EXISTS legal_rep_address,
  DROP COLUMN IF EXISTS signing_city;

ALTER TABLE hr_contracts
  DROP COLUMN IF EXISTS job_duties,
  DROP COLUMN IF EXISTS equipment_provided,
  DROP COLUMN IF EXISTS pay_day,
  DROP COLUMN IF EXISTS pay_method,
  DROP COLUMN IF EXISTS transport_mode,
  DROP COLUMN IF EXISTS signing_city,
  DROP COLUMN IF EXISTS snapshot_place_of_birth,
  DROP COLUMN IF EXISTS snapshot_id_card_date,
  DROP COLUMN IF EXISTS snapshot_id_card_place,
  DROP COLUMN IF EXISTS snapshot_nationality,
  DROP COLUMN IF EXISTS snapshot_work_permit_no,
  DROP COLUMN IF EXISTS snapshot_work_permit_date,
  DROP COLUMN IF EXISTS snapshot_work_permit_place;
