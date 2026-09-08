-- 045: Chi phí công ty (kế toán) — riêng với chi phí QC FB Ads.
-- NV/admin gửi tin văn bản HOẶC ảnh vào Zalo group riêng → Lan parse → lưu đây.
-- Trang /chi-phi/khai-bao chỉ accountant/admin xem.

CREATE TABLE IF NOT EXISTS company_expense_items (
    id              SERIAL PRIMARY KEY,
    occurred_date   DATE,                             -- ngày phát sinh chi phí (nullable nếu Lan chưa rõ)
    amount_vnd      BIGINT NOT NULL,                  -- số tiền VND
    category        TEXT NOT NULL DEFAULT 'other',    -- ads | salary | office | utility | other
    note            TEXT,                             -- ghi chú / mô tả
    -- Nguồn dữ liệu
    source          TEXT NOT NULL DEFAULT 'text',     -- text | image
    source_url      TEXT,                             -- URL ảnh Zalo (nếu source=image)
    confidence      REAL,                             -- 0..1 từ Gemini OCR
    raw_body        TEXT,                             -- text gốc NV gửi (nếu source=text)
    -- Ai gửi
    zalo_thread_id  TEXT,
    zalo_sender_id  TEXT,
    zalo_sender_name TEXT,
    user_id         INTEGER REFERENCES users(id),     -- match qua zalo_uid (có thể NULL)
    -- Trạng thái
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected
    confirmed_by    INTEGER REFERENCES users(id),
    confirmed_at    TIMESTAMPTZ,
    -- Meta
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ce_occurred ON company_expense_items(occurred_date) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ce_category ON company_expense_items(category) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ce_thread   ON company_expense_items(zalo_thread_id);
CREATE INDEX IF NOT EXISTS idx_ce_status   ON company_expense_items(status) WHERE deleted_at IS NULL;
