-- Bổ sung công: nhân viên gửi đề nghị bù/chỉnh công, leader/kế toán/admin duyệt.
CREATE TABLE IF NOT EXISTS cc_attendance_requests (
    id          SERIAL PRIMARY KEY,
    user_id     VARCHAR     NOT NULL,           -- NV gửi đề nghị
    work_date   DATE        NOT NULL,           -- ngày cần bổ sung công
    sessions    JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{"check_in":"HH:MM","check_out":"HH:MM"}, ...]
    reason      TEXT        DEFAULT '',          -- lý do NV gửi
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / cancelled
    reviewed_by VARCHAR,                          -- user_id người duyệt
    reviewed_at TIMESTAMP,
    review_note TEXT        DEFAULT '',           -- ghi chú khi duyệt / lý do từ chối
    created_at  TIMESTAMP   NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ccar_status     ON cc_attendance_requests(status);
CREATE INDEX IF NOT EXISTS idx_ccar_user       ON cc_attendance_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_ccar_work_date  ON cc_attendance_requests(work_date);
