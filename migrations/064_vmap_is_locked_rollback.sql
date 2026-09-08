-- Rollback 064: bỏ cột is_locked khỏi wh_variation_map.
ALTER TABLE wh_variation_map DROP COLUMN IF EXISTS is_locked;
