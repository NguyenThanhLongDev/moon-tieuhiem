-- Migration 032: Bảng văn phòng / địa điểm làm việc

-- Danh sách văn phòng/địa điểm
CREATE TABLE IF NOT EXISTS cc_offices (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    lat        NUMERIC(10,7) NOT NULL,
    lng        NUMERIC(10,7) NOT NULL,
    radius_m   INTEGER NOT NULL DEFAULT 300,
    note       TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Gán văn phòng cho nhân viên
ALTER TABLE cc_employees ADD COLUMN IF NOT EXISTS office_id INTEGER REFERENCES cc_offices(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cc_employees_office ON cc_employees(office_id);
