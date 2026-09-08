ALTER TABLE cc_employees
    DROP COLUMN IF EXISTS resigned_at,
    DROP COLUMN IF EXISTS resigned_note,
    DROP COLUMN IF EXISTS resigned_team,
    DROP COLUMN IF EXISTS resigned_leader,
    DROP COLUMN IF EXISTS resigned_by;
