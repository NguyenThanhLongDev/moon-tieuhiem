-- 037_page_shop_binding.sql
-- Bảng binding versioned: FB page → POS shop. Cho phép pos_shop_id NULL để
-- mark page test / zombie / marketing chung không thuộc shop nào.
-- Cùng pattern versioned với migration 036 (user_ad_account_assignments).
--
-- Mục đích: spend cấp page (fb_ads_page_daily_spend) được attribute về đúng
-- shop chủ quản theo từng kỳ. Đổi shop chủ quản giữa kỳ → close khoảng cũ,
-- mở khoảng mới; báo cáo lùi quá khứ vẫn truy được shop đúng cho từng ngày.

CREATE TABLE IF NOT EXISTS fb_page_shop_binding (
    id            BIGSERIAL PRIMARY KEY,
    page_id       VARCHAR(100) NOT NULL,
    pos_shop_id   BIGINT REFERENCES shops(id) ON DELETE SET NULL,   -- NULL = page test/zombie (đã review nhưng không thuộc shop)
    assigned_from DATE NOT NULL,
    assigned_to   DATE,                                              -- NULL = đang active
    assigned_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    reason        TEXT,
    note          TEXT,                                              -- ghi chú tự do (vd "page test team Minh")
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT chk_psb_window CHECK (assigned_to IS NULL OR assigned_to >= assigned_from)
);

CREATE INDEX IF NOT EXISTS idx_psb_page   ON fb_page_shop_binding (page_id);
CREATE INDEX IF NOT EXISTS idx_psb_shop   ON fb_page_shop_binding (pos_shop_id);
CREATE INDEX IF NOT EXISTS idx_psb_window ON fb_page_shop_binding (page_id, assigned_from, assigned_to);
-- 1 page chỉ có 1 binding active cùng lúc
CREATE UNIQUE INDEX IF NOT EXISTS uq_psb_active ON fb_page_shop_binding (page_id) WHERE assigned_to IS NULL;
