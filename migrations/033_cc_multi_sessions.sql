-- Migration 033: Bảng phiên chấm công (multi check-in/out per day)
-- Cho phép nhân viên check-in, check-out nhiều lần trong ngày

CREATE TABLE IF NOT EXISTS cc_attendance_sessions (
    id               SERIAL PRIMARY KEY,
    user_id          VARCHAR(64)  NOT NULL,
    date             DATE         NOT NULL,
    check_in         TIMESTAMPTZ,
    check_out        TIMESTAMPTZ,
    check_in_lat     DOUBLE PRECISION,
    check_in_lng     DOUBLE PRECISION,
    check_in_distance_m INTEGER,
    created_at       TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cc_sessions_user_date
    ON cc_attendance_sessions (user_id, date);
