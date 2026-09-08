-- 055_lan_broadcasts.sql
-- Audit log cho thông báo sếp giao Lan gửi qua Zalo bridge.

CREATE TABLE IF NOT EXISTS lan_broadcasts (
    id              BIGSERIAL   PRIMARY KEY,
    sender_uid      VARCHAR(50),               -- UID Zalo của sếp
    sender_name     VARCHAR(255),
    target_label    VARCHAR(255),              -- Nhãn target (vd "nam", "cty", "kinh doanh")
    target_groups   JSONB        NOT NULL,     -- List {thread_id, label} đã gửi
    message_text    TEXT         NOT NULL,
    sent_at         TIMESTAMP    NOT NULL DEFAULT NOW(),
    results         JSONB,                     -- {success_count, fail_count, errors[]}
    success_count   INT          DEFAULT 0,
    fail_count      INT          DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lan_broadcasts_sent ON lan_broadcasts(sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_lan_broadcasts_sender ON lan_broadcasts(sender_uid);
