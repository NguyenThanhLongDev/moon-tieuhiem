-- 068: Danh sách shop nhận đơn ladipage (độc lập wh_shops để không đụng dashboard)
CREATE TABLE IF NOT EXISTS ladipage_shops (
    pos_shop_id  TEXT PRIMARY KEY,
    shop_name    TEXT,
    api_key      TEXT NOT NULL,
    warehouse_id TEXT,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
