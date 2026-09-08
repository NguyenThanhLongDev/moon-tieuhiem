-- 050_hr_contracts.sql
-- Module HR Phase 2: Hợp đồng lao động.
-- - hr_companies: nhiều công ty bên A (kế toán thêm khi cần)
-- - hr_contracts: 1 NV có nhiều HĐLĐ theo thời gian

CREATE TABLE IF NOT EXISTS hr_companies (
    id                  BIGSERIAL   PRIMARY KEY,
    company_name        VARCHAR(255) NOT NULL,
    tax_code            VARCHAR(50),                       -- MST
    address             TEXT,
    legal_rep_name      VARCHAR(255),                      -- Người đại diện pháp luật
    legal_rep_title     VARCHAR(100),                      -- VD: Giám đốc
    legal_rep_id_card   VARCHAR(20),                       -- CCCD của người đại diện
    phone               VARCHAR(20),
    email               VARCHAR(255),
    bank_account        VARCHAR(50),
    bank_name           VARCHAR(255),
    is_default          BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    notes               TEXT,
    created_by          BIGINT      REFERENCES users(id),
    created_at          TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hr_companies_active ON hr_companies(is_active) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS hr_contracts (
    id                      BIGSERIAL   PRIMARY KEY,
    user_id                 BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id              BIGINT      NOT NULL REFERENCES hr_companies(id),

    contract_number         VARCHAR(100) NOT NULL,             -- VD: HĐLĐ-2026/001
    contract_type           VARCHAR(30)  NOT NULL,             -- xac_dinh|khong_xac_dinh|thu_viec|thoi_vu

    start_date              DATE        NOT NULL,
    end_date                DATE,                              -- NULL nếu không xác định
    probation_months        NUMERIC(3,1),                      -- VD 2.0 = 2 tháng thử việc

    -- Vị trí công việc
    position                VARCHAR(255),                      -- Chức vụ (text tự do)
    department              VARCHAR(255),
    workplace               TEXT,

    -- Giờ làm
    work_hours              VARCHAR(255) DEFAULT '08:00 - 17:00, Thứ 2 - Thứ 7',

    -- Lương / phụ cấp (đơn vị VND)
    salary_base             BIGINT,                            -- Lương cơ bản
    salary_allowance_meal   BIGINT,                            -- Phụ cấp ăn
    salary_allowance_fuel   BIGINT,                            -- Phụ cấp xăng xe
    salary_allowance_phone  BIGINT,                            -- Phụ cấp điện thoại
    salary_allowance_other  BIGINT,                            -- Phụ cấp khác
    salary_other_note       TEXT,                              -- Diễn giải phụ cấp khác

    -- Snapshot info NV tại thời điểm ký (để HĐ không bị thay đổi khi NV sửa profile)
    snapshot_employee_name      VARCHAR(255),
    snapshot_employee_dob       DATE,
    snapshot_employee_gender    VARCHAR(10),
    snapshot_employee_id_card   VARCHAR(20),
    snapshot_employee_address   TEXT,
    snapshot_employee_phone     VARCHAR(20),
    snapshot_employee_hometown  TEXT,

    -- File HĐ
    docx_path               TEXT,                              -- Đường dẫn file .docx (relative to /mnt/nvme/hr_uploads)
    pdf_path                TEXT,                              -- PDF nếu đã convert (Phase 2)

    -- Chữ ký (Phase 2 — ký tay online)
    signed_employee         BOOLEAN     NOT NULL DEFAULT FALSE,
    signed_employee_at      TIMESTAMP,
    signed_employee_ip      VARCHAR(50),
    signed_employee_sig_path TEXT,                             -- PNG chữ ký NV
    signed_company          BOOLEAN     NOT NULL DEFAULT FALSE,
    signed_company_at       TIMESTAMP,
    signed_company_by       BIGINT      REFERENCES users(id),
    signed_company_sig_path TEXT,

    status                  VARCHAR(20) NOT NULL DEFAULT 'draft',
                            -- draft|pending_sign|signed|terminated|expired

    notes                   TEXT,
    created_by              BIGINT      REFERENCES users(id),
    created_at              TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP   NOT NULL DEFAULT NOW(),

    CONSTRAINT hr_contracts_type_chk CHECK (
        contract_type IN ('xac_dinh', 'khong_xac_dinh', 'thu_viec', 'thoi_vu')
    ),
    CONSTRAINT hr_contracts_status_chk CHECK (
        status IN ('draft', 'pending_sign', 'signed', 'terminated', 'expired')
    )
);

CREATE INDEX IF NOT EXISTS idx_hr_contracts_user ON hr_contracts(user_id);
CREATE INDEX IF NOT EXISTS idx_hr_contracts_status ON hr_contracts(status);
CREATE INDEX IF NOT EXISTS idx_hr_contracts_company ON hr_contracts(company_id);

CREATE OR REPLACE FUNCTION hr_contracts_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_hr_contracts_updated ON hr_contracts;
CREATE TRIGGER trg_hr_contracts_updated
    BEFORE UPDATE ON hr_contracts
    FOR EACH ROW EXECUTE FUNCTION hr_contracts_set_updated_at();

DROP TRIGGER IF EXISTS trg_hr_companies_updated ON hr_companies;
CREATE OR REPLACE FUNCTION hr_companies_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_hr_companies_updated
    BEFORE UPDATE ON hr_companies
    FOR EACH ROW EXECUTE FUNCTION hr_companies_set_updated_at();
