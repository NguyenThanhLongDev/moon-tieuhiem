-- 074: Thanh toán thuê bao phần mềm theo tháng (SePay QR) — 1 gói cho cả công ty (bản Moon)
CREATE TABLE IF NOT EXISTS billing_subscription (
    id           INT PRIMARY KEY DEFAULT 1,
    base_fee     BIGINT NOT NULL DEFAULT 2000000,
    addon_fee    BIGINT NOT NULL DEFAULT 500000,
    addon_count  INT NOT NULL DEFAULT 0,
    grace_days   INT NOT NULL DEFAULT 7,
    expires_at   DATE,
    note         TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT billing_singleton CHECK (id = 1)
);
INSERT INTO billing_subscription (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS billing_payments (
    id           BIGSERIAL PRIMARY KEY,
    amount       BIGINT NOT NULL,
    content_code TEXT,
    sepay_tx_id  TEXT UNIQUE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'completed',
    months_added INT DEFAULT 0,
    new_expires  DATE,
    gateway      TEXT,
    raw          JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_billing_pay_created ON billing_payments(created_at DESC);
