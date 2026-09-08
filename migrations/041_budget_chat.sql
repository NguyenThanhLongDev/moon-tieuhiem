-- 041_budget_chat.sql — Hệ thống chat báo ngân sách FB Ads thay Zalo
--
-- 3 bảng:
--   budget_chat_messages — tin nhắn gốc + AI parse JSON
--   budget_items         — dòng ngân sách đã parse (mỗi TK = 1 row)
--   budget_chat_reads    — track unread per (user, team)
--
-- Source of truth: budget_items. Khi sửa/xóa item → KHÔNG sửa message body.

CREATE TABLE IF NOT EXISTS budget_chat_messages (
    id            BIGSERIAL PRIMARY KEY,
    team_id       TEXT NOT NULL,
    user_id       INT  NOT NULL,
    body          TEXT NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parsed_at     TIMESTAMPTZ,
    parsed_json   JSONB,
    parse_error   TEXT,
    model_used    TEXT,                  -- vd 'deepseek-v4-flash'
    deleted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bcm_team_time ON budget_chat_messages (team_id, sent_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bcm_user      ON budget_chat_messages (user_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS budget_items (
    id                BIGSERIAL PRIMARY KEY,
    message_id        BIGINT REFERENCES budget_chat_messages(id) ON DELETE CASCADE,
    user_id           INT     NOT NULL,
    team_id           TEXT    NOT NULL,
    for_date          DATE    NOT NULL,
    tk_name_raw       TEXT    NOT NULL,
    fb_ad_account_id  TEXT,                       -- match từ pa_ad_accounts (NULL nếu không match)
    card_last4        TEXT,
    amount_vnd        BIGINT  NOT NULL CHECK (amount_vnd >= 0),
    match_confidence  REAL    DEFAULT 0,          -- 0..1, >=0.7 mới auto-attach fb_ad_account_id
    edited_at         TIMESTAMPTZ,
    edited_by         INT,
    deleted_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bi_for_date_team ON budget_items (for_date, team_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bi_user_date     ON budget_items (user_id, for_date) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bi_msg           ON budget_items (message_id);

CREATE TABLE IF NOT EXISTS budget_chat_reads (
    user_id      INT     NOT NULL,
    team_id      TEXT    NOT NULL,
    last_seen_id BIGINT  NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, team_id)
);
