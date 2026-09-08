-- Rollback 044
DROP TABLE IF EXISTS zalo_pending_senders;
DROP INDEX IF EXISTS idx_users_zalo_uid;
-- Không drop users.zalo_uid để tránh mất data đã map.
