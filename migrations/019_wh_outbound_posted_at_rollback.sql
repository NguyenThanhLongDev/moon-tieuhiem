BEGIN;
DROP INDEX IF EXISTS idx_wh_outbound_requests_posted_at;
ALTER TABLE wh_outbound_requests DROP COLUMN IF EXISTS posted_to_ledger_at;
COMMIT;
