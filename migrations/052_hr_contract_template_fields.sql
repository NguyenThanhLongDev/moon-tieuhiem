-- 052_hr_contract_template_fields.sql
-- Bổ sung field để HĐ match đúng template kế toán đã duyệt
-- (xem PDF mẫu "HĐ LĐ ĐÀO THỊ NỤ").

-- ── Công ty: thêm thông tin GĐ + thành phố ký ────────────────────────────
ALTER TABLE hr_companies
  ADD COLUMN IF NOT EXISTS legal_rep_dob       DATE,        -- Ngày sinh GĐ
  ADD COLUMN IF NOT EXISTS legal_rep_address   TEXT,        -- Địa chỉ cư trú GĐ
  ADD COLUMN IF NOT EXISTS signing_city        VARCHAR(100); -- VD "Hưng Yên"

-- ── HĐLĐ: thêm field chi tiết template ──────────────────────────────────
ALTER TABLE hr_contracts
  ADD COLUMN IF NOT EXISTS job_duties              TEXT,         -- Công việc phải làm
  ADD COLUMN IF NOT EXISTS equipment_provided      TEXT,         -- "Được cấp phát những dụng cụ làm việc gồm..."
  ADD COLUMN IF NOT EXISTS pay_day                 INT,          -- Trả lương vào ngày N tháng sau (1-31)
  ADD COLUMN IF NOT EXISTS pay_method              VARCHAR(50),  -- "Chuyển khoản" | "Tiền mặt"
  ADD COLUMN IF NOT EXISTS transport_mode          VARCHAR(100), -- "Tự túc"
  ADD COLUMN IF NOT EXISTS signing_city            VARCHAR(100), -- Override company signing_city

  -- Snapshot NV info bổ sung (để HĐ giữ nguyên kể cả khi NV sửa hồ sơ)
  ADD COLUMN IF NOT EXISTS snapshot_place_of_birth     TEXT,    -- Nơi sinh (sau "Sinh ngày: dd/mm/yyyy Tại:")
  ADD COLUMN IF NOT EXISTS snapshot_id_card_date       DATE,    -- Ngày cấp CCCD
  ADD COLUMN IF NOT EXISTS snapshot_id_card_place      TEXT,    -- Nơi cấp CCCD
  ADD COLUMN IF NOT EXISTS snapshot_nationality        VARCHAR(50) DEFAULT 'Việt Nam',
  ADD COLUMN IF NOT EXISTS snapshot_work_permit_no     VARCHAR(100), -- Số GPLĐ
  ADD COLUMN IF NOT EXISTS snapshot_work_permit_date   DATE,
  ADD COLUMN IF NOT EXISTS snapshot_work_permit_place  TEXT;
