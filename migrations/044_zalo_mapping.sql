-- 044: Mapping Zalo sender → user DB
-- Bridge tự ghi nhận sender chưa map → admin gán qua /ngan-sach/zalo-mapping

-- 1. Đảm bảo cột zalo_uid trên users (đã tạo bằng ALTER ad-hoc 2026-05-18,
--    migration này đảm bảo deploy mới cũng có)
ALTER TABLE users ADD COLUMN IF NOT EXISTS zalo_uid TEXT;
CREATE INDEX IF NOT EXISTS idx_users_zalo_uid ON users(zalo_uid) WHERE zalo_uid IS NOT NULL;

-- 2. Bảng pending: bridge ghi sender lạ vào đây để admin map sau
CREATE TABLE IF NOT EXISTS zalo_pending_senders (
    sender_uid     TEXT PRIMARY KEY,           -- uidFrom từ Zalo
    sender_name    TEXT NOT NULL,              -- dName Zalo
    last_thread_id TEXT,                       -- group cuối cùng thấy
    message_count  INTEGER NOT NULL DEFAULT 1,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_body_snippet TEXT                     -- 100 char đầu tin gần nhất
);

CREATE INDEX IF NOT EXISTS idx_zalo_pending_last_seen
    ON zalo_pending_senders(last_seen DESC);
