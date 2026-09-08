-- 067: Đơn ladipage nhận qua webhook — LƯU TRƯỚC, đẩy POS sau (an toàn khi POS lỗi)
CREATE TABLE IF NOT EXISTS ladipage_inbound_orders (
    id              BIGSERIAL PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    shop_key        TEXT,
    pos_shop_id     TEXT,
    -- Khách hàng
    ho_ten          TEXT,
    so_dien_thoai   TEXT,
    dia_chi         TEXT,
    -- Lựa chọn trên form
    size            TEXT,
    mau             TEXT,
    combo           TEXT,
    so_luong        INT DEFAULT 1,
    tien            BIGINT DEFAULT 0,        -- COD, đọc từ combo
    sku             TEXT,                    -- nếu form có gắn mã SP
    selections      TEXT,                    -- toàn bộ lựa chọn (gộp)
    -- Nguồn
    source_url      TEXT,
    source_name     TEXT,
    note            TEXT,                    -- dump đầy đủ đẩy vào POS
    raw_payload     JSONB,                   -- payload gốc — không mất gì
    -- Trạng thái đẩy POS
    pos_status      TEXT NOT NULL DEFAULT 'pending',  -- pending|pushed|failed|manual_done
    pos_order_id    TEXT,
    pos_error       TEXT,
    push_attempts   INT NOT NULL DEFAULT 0,
    pushed_at       TIMESTAMPTZ,
    handled_by      TEXT,                    -- ai bấm "đã xử lý tay"
    handled_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ladi_inbound_status ON ladipage_inbound_orders(pos_status, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_ladi_inbound_recv   ON ladipage_inbound_orders(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_ladi_inbound_phone  ON ladipage_inbound_orders(so_dien_thoai);
