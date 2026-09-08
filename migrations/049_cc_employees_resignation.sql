-- Đánh dấu nghỉ việc cho cc_employees: lưu ngày nghỉ, ghi chú, snapshot team + leader.
ALTER TABLE cc_employees
    ADD COLUMN IF NOT EXISTS resigned_at      DATE,
    ADD COLUMN IF NOT EXISTS resigned_note    TEXT,
    ADD COLUMN IF NOT EXISTS resigned_team    VARCHAR(150),
    ADD COLUMN IF NOT EXISTS resigned_leader  VARCHAR(200),
    ADD COLUMN IF NOT EXISTS resigned_by      VARCHAR(50);

CREATE INDEX IF NOT EXISTS idx_cc_employees_resigned_at ON cc_employees(resigned_at);
