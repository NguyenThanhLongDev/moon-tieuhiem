-- 056: Sổ đăng ký ad → TK QC (Marketing Brain / Lương)
-- Nguồn: build_ad_page_map của sync_fb_ads_by_page fetch TOÀN BỘ ads mỗi TK
-- (kể cả ad đã tắt) — lưu lại để tra ngược "ad thuộc TK nào" cho ad không còn
-- spend trong kỳ (đơn về trễ sau khi ad tắt → vẫn quy được về NV khi tính lương).
CREATE TABLE IF NOT EXISTS mb_fb_ads_registry (
    ad_id      VARCHAR(64) PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL,
    page_id    VARCHAR(64) NOT NULL DEFAULT '',
    post_id    VARCHAR(160) NOT NULL DEFAULT '',
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mb_far_account ON mb_fb_ads_registry (account_id);
