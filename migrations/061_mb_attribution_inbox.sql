-- 061: Marketing Brain — nhận attribution ads từ shipcod.vn (Cách B: webhook)
-- Bối cảnh: bỏ Pancake Chat → đơn POS mất ad_id. shipcod hứng referral FB rồi
-- bắn (pos_shop_id, pancake_order_id, ad_id...) sang /api/mb/attribution.

-- 1. Hộp thư đến (staging): shipcod ghi vào đây, ĐỘC LẬP với việc đơn đã sync
--    vào bảng `orders` chưa (giải quyết lệch thời điểm: shipcod có thể bắn TRƯỚC
--    khi cron sync kéo đơn về). Khóa theo (pos_shop_id, pancake_order_id) vì
--    external_order_id KHÔNG unique toàn hệ — trùng giữa các shop.
CREATE TABLE IF NOT EXISTS mb_attribution_inbox (
    pos_shop_id      VARCHAR(64)  NOT NULL,   -- shops.pancake_shop_id (vd '1942953484')
    pancake_order_id VARCHAR(100) NOT NULL,   -- = orders.external_order_id trong shop đó
    ad_id            VARCHAR(64)  NOT NULL DEFAULT '',
    post_id          VARCHAR(160) NOT NULL DEFAULT '',
    page_id          VARCHAR(64)  NOT NULL DEFAULT '',
    conversation_id  VARCHAR(160) NOT NULL DEFAULT '',
    ad_source        VARCHAR(32)  NOT NULL DEFAULT '',   -- 'ADS' | 'ORGANIC'
    occurred_at      TIMESTAMPTZ,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    applied_at       TIMESTAMPTZ,             -- thời điểm đã merge vào mb_order_attribution
    PRIMARY KEY (pos_shop_id, pancake_order_id)
);
CREATE INDEX IF NOT EXISTS idx_mb_inbox_unapplied
    ON mb_attribution_inbox (received_at) WHERE applied_at IS NULL;

-- 2. Đánh dấu NGUỒN attribution trên bảng read chính — để cron sync KHÔNG ghi đè
--    dữ liệu shipcod bằng ad_id rỗng từ payload (sau khi bỏ Chat, payload hết ad_id).
--    'shipcod' = bất khả xâm phạm bởi sync; 'pancake_payload' = sync ghi như cũ.
ALTER TABLE mb_order_attribution
    ADD COLUMN IF NOT EXISTS ad_source_origin VARCHAR(20) NOT NULL DEFAULT 'pancake_payload';
