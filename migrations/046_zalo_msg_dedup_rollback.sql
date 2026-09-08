DROP INDEX IF EXISTS idx_bcm_zalo_msg_id;
DROP INDEX IF EXISTS idx_ce_zalo_msg_id;
-- Giữ cột zalo_msg_id để không mất data nếu rollback.
