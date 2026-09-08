-- 047: Đánh dấu chi phí gửi RIÊNG qua chat 1-1 với Lan (không qua group).
-- Forward về inbox sẽ có prefix 🔒 + UI có badge để kế toán biết.

ALTER TABLE company_expense_items
    ADD COLUMN IF NOT EXISTS is_private BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_ce_private
    ON company_expense_items(is_private) WHERE is_private = TRUE AND deleted_at IS NULL;
