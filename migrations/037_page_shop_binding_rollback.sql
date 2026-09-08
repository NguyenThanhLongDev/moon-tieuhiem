-- Rollback 037_page_shop_binding.sql
DROP INDEX IF EXISTS uq_psb_active;
DROP INDEX IF EXISTS idx_psb_window;
DROP INDEX IF EXISTS idx_psb_shop;
DROP INDEX IF EXISTS idx_psb_page;
DROP TABLE IF EXISTS fb_page_shop_binding;
