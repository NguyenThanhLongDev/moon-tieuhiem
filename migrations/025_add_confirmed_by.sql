-- Thêm cột confirmed_by cho xuất hàng và hàng hoàn
ALTER TABLE wh_outbound_requests ADD COLUMN IF NOT EXISTS confirmed_by TEXT DEFAULT '';
ALTER TABLE wh_return_receipts   ADD COLUMN IF NOT EXISTS confirmed_by TEXT DEFAULT '';
