-- Mốc thời gian lần đầu ghi ledger cho phiếu xuất (audit / idempotent cùng ledger_posted_qty dòng).
BEGIN;
ALTER TABLE wh_outbound_requests
    ADD COLUMN IF NOT EXISTS posted_to_ledger_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_wh_outbound_requests_posted_at
    ON wh_outbound_requests (posted_to_ledger_at)
    WHERE posted_to_ledger_at IS NOT NULL;
COMMIT;
