-- 046: Dedup tin Zalo theo zalo_msg_id để catchup không tạo trùng.
-- Bridge restart / scheduler scan định kỳ sẽ replay tin → cần unique index.

ALTER TABLE budget_chat_messages ADD COLUMN IF NOT EXISTS zalo_msg_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_bcm_zalo_msg_id
    ON budget_chat_messages(zalo_msg_id) WHERE zalo_msg_id IS NOT NULL;

ALTER TABLE company_expense_items ADD COLUMN IF NOT EXISTS zalo_msg_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_zalo_msg_id
    ON company_expense_items(zalo_msg_id) WHERE zalo_msg_id IS NOT NULL;
