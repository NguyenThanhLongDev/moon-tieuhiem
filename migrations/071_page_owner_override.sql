-- 071: gán CHỦ PAGE thủ công — dùng khi ads chưa map được về page
-- (page bán trên POS nhưng moon không có dòng spend → không suy ra NV được).
CREATE TABLE IF NOT EXISTS page_owner_override (
    page_id     TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
