-- 040_hr_profiles.sql
-- Module HR Phase 1: bảng hồ sơ nhân sự + workflow duyệt.
-- 1-1 với users (PK = user_id). Mỗi NV có tối đa 1 hồ sơ HR.

CREATE TABLE IF NOT EXISTS hr_profiles (
    user_id                BIGINT      PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

    -- ── Thông tin cá nhân ──
    dob                    DATE,
    gender                 VARCHAR(10),                       -- 'male' | 'female' | 'other'
    id_card_number         VARCHAR(20),                       -- CMND/CCCD
    hometown_address       TEXT,                              -- Quê quán / địa chỉ thường trú
    current_address        TEXT,                              -- Nơi ở hiện tại
    phone                  VARCHAR(20),
    personal_email         VARCHAR(255),

    -- ── Liên hệ khẩn cấp ──
    emergency_contact_name VARCHAR(255),
    emergency_contact_phone VARCHAR(20),

    -- ── Công việc ──
    hire_date              DATE,                              -- Ngày vào công ty

    -- ── File scan ──
    photo_path             TEXT,                              -- Ảnh chân dung (selfie)
    id_card_front_path     TEXT,                              -- CCCD mặt trước
    id_card_back_path      TEXT,                              -- CCCD mặt sau

    -- ── Workflow duyệt ──
    -- 'draft'     : NV đang khai báo, chưa gửi
    -- 'submitted' : NV đã gửi, chờ admin duyệt
    -- 'approved'  : Admin đã duyệt → hồ sơ hợp lệ
    -- 'rejected'  : Admin yêu cầu NV bổ sung (kèm admin_note)
    status                 VARCHAR(20) NOT NULL DEFAULT 'draft',
    admin_note             TEXT,                              -- Ghi chú của admin khi reject
    submitted_at           TIMESTAMP,
    approved_at            TIMESTAMP,
    approved_by            BIGINT      REFERENCES users(id),  -- ai duyệt
    rejected_at            TIMESTAMP,
    rejected_by            BIGINT      REFERENCES users(id),  -- ai yêu cầu bổ sung

    created_at             TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMP   NOT NULL DEFAULT NOW(),

    CONSTRAINT hr_profiles_status_chk CHECK (status IN ('draft','submitted','approved','rejected'))
);

CREATE INDEX IF NOT EXISTS idx_hr_profiles_status ON hr_profiles(status);
CREATE INDEX IF NOT EXISTS idx_hr_profiles_updated ON hr_profiles(updated_at DESC);

-- Trigger auto-update updated_at
CREATE OR REPLACE FUNCTION hr_profiles_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_hr_profiles_updated ON hr_profiles;
CREATE TRIGGER trg_hr_profiles_updated
    BEFORE UPDATE ON hr_profiles
    FOR EACH ROW EXECUTE FUNCTION hr_profiles_set_updated_at();
